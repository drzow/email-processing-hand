"""Minimal stdio MCP JSON-RPC client.

Spawns an MCP server subprocess, performs the ``initialize`` handshake,
and exposes ``call_tool`` to invoke tools. Synchronous; one request in
flight at a time. No streaming / progress handling — that's a v2
concern.

This is what the sidecar uses to talk to rustymail (and eventually
ms365). The stdio proxy binaries provided by both projects accept
JSON-RPC over stdin and emit responses on stdout, with stderr reserved
for diagnostics.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any


class McpClientError(RuntimeError):
    """Raised on transport / protocol failures with the MCP server."""


class McpClient:
    """Lifecycle: ``open()`` → ``call_tool()`` × N → ``close()``.

    ``open()`` spawns the subprocess and runs the ``initialize`` handshake;
    omitting it lets the constructor be ``__init__``-only for testing
    (callers can mock ``_transact`` directly).
    """

    JSONRPC = "2.0"
    PROTOCOL_VERSION = "2024-11-05"

    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        client_name: str = "email-processing-hand-sidecar",
        client_version: str = "0.1.0",
        timeout_secs: float = 30.0,
    ) -> None:
        self._command = list(command)
        self._env = self._merge_env(env)
        self._client_name = client_name
        self._client_version = client_version
        self._timeout_secs = timeout_secs
        self._proc: subprocess.Popen[bytes] | None = None
        self._next_id = 1

    # ---- public API -----------------------------------------------------

    def open(self) -> None:
        """Spawn the subprocess and run the JSON-RPC initialize handshake."""
        if self._proc is not None:
            raise McpClientError("already open")
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env,
            bufsize=0,
        )
        result = self._transact(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": self._client_name,
                    "version": self._client_version,
                },
            },
        )
        # Servers expect a notifications/initialized after a successful
        # initialize. Best-effort — we don't care about errors here.
        try:
            self._notify("notifications/initialized", {})
        except McpClientError:
            pass
        # Sanity-check the protocol version. Older / newer servers may
        # reply with their own; we tolerate that but require *some*
        # version field so we know we got a real handshake.
        if "protocolVersion" not in result:
            raise McpClientError(
                f"initialize reply lacked protocolVersion: {result!r}"
            )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke ``tools/call``. Returns the server's ``result`` block.

        The server's result for ``tools/call`` is typically:

        ``{"content": [{"type": "text", "text": "..."}], "isError": false}``
        """
        return self._transact("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=2)
        self._proc = None

    def __enter__(self) -> "McpClient":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- internals ------------------------------------------------------

    def _merge_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = dict(os.environ)
        if extra:
            env.update(extra)
        return env

    def _transact(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise McpClientError("client is not open")
        request_id = self._next_id
        self._next_id += 1
        request = {
            "jsonrpc": self.JSONRPC,
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            self._proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise McpClientError(f"write failed for {method}: {e}") from e

        line = self._proc.stdout.readline()
        if not line:
            stderr = self._drain_stderr()
            raise McpClientError(
                f"server closed stdout before responding to {method}; stderr={stderr!r}"
            )
        try:
            response = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise McpClientError(
                f"non-JSON response from {method}: {line!r} ({e})"
            ) from e

        if response.get("id") != request_id:
            raise McpClientError(
                f"id mismatch for {method}: sent {request_id}, got {response.get('id')}"
            )
        if "error" in response:
            err = response["error"]
            raise McpClientError(
                f"server error on {method}: code={err.get('code')} {err.get('message')}"
            )
        return response.get("result", {})

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Fire-and-forget notification (no id, no response expected)."""
        if self._proc is None or self._proc.stdin is None:
            raise McpClientError("client is not open")
        notification = {"jsonrpc": self.JSONRPC, "method": method, "params": params}
        try:
            self._proc.stdin.write((json.dumps(notification) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise McpClientError(f"notification {method} failed: {e}") from e

    def _drain_stderr(self) -> str:
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            data = self._proc.stderr.read(4096)
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace") if data else ""
