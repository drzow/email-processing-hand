#!/usr/bin/env python3
"""Sidecar entry point for the email-processing-hand.

Skeleton dispatcher — full subcommand surface lands in upcoming slices
(see docs/design.md §6 for the design). For now only ``noop`` is wired
up so the agent can verify the sidecar plumbing without doing real I/O.

Usage:

    python3 tools/scan.py <subcommand> [--input -]

Reads request JSON from stdin, writes response JSON to stdout. Exit 0
on success, non-zero on a sidecar-internal error (HAND ALWAYS reads
status from the JSON envelope, not from the exit code, since some
subcommands report logical errors with exit 0).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Response envelope helpers — stable contract with the agent.
# ---------------------------------------------------------------------------


def _envelope(
    subcommand: str,
    request_id: str,
    started_at: float,
    *,
    result: Any = None,
    error: dict[str, str] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return {
        "status": "ok" if error is None else "error",
        "subcommand": subcommand,
        "request_id": request_id,
        "elapsed_ms": elapsed_ms,
        "result": result,
        "error": error,
        "metrics": metrics or {},
    }


def _emit(envelope: dict[str, Any]) -> None:
    json.dump(envelope, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Subcommand registry. Each handler takes the parsed request dict and
# returns a (result, metrics) tuple — or raises SidecarError for a
# structured error response.
# ---------------------------------------------------------------------------


class SidecarError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


Handler = Callable[[dict[str, Any]], tuple[Any, dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}


def register(name: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn

    return deco


# ---------------------------------------------------------------------------
# noop — sanity check the sidecar plumbing without touching MCP.
# ---------------------------------------------------------------------------


@register("noop")
def _noop(_request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    return (
        {"message": "sidecar reachable; skeleton mode"},
        {"messages_scanned": 0, "mcp_calls": 0, "result_bytes": 0},
    )


# ---------------------------------------------------------------------------
# resolve-domain — project-resolution by domain ranking. Pure logic, no
# MCP, no LLM. Powers Phase 1 of the per-message classification pipeline.
# ---------------------------------------------------------------------------


@register("resolve-domain")
def _resolve_domain(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    # Late import so `noop` doesn't pay for it on every invocation.
    from lib.domain import resolve

    try:
        result = resolve(
            from_=request.get("from", []),
            to=request.get("to", []),
            cc=request.get("cc", []),
            user_domains=request.get("user_domains", []),
            exclude_domains=request.get("exclude_domains", []),
            project_map=request.get("project_map", {}),
        )
    except (TypeError, KeyError) as e:
        raise SidecarError("bad_request", f"malformed input: {e}") from e

    return (
        result,
        {
            "messages_scanned": 0,
            "mcp_calls": 0,
            "result_bytes": 0,
            "domains_ranked": len(result["ranked_domains"]),
        },
    )


# ---------------------------------------------------------------------------
# fetch-batch — fetch + parse + render a batch of messages by uid.
# v1 supports selector.kind == "uids" only. The agent passes the
# rustymail / ms365 mcp_server config in the request so this slice
# doesn't need to ship a static account-to-server mapping.
# ---------------------------------------------------------------------------


@register("fetch-batch")
def _fetch_batch(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    # Lazy imports keep the noop / resolve-domain hot paths cheap.
    from lib.mcp_client import McpClient, McpClientError

    server_cfg = request.get("mcp_server")
    if not server_cfg or "command" not in server_cfg:
        raise SidecarError("bad_request", "mcp_server.command is required")

    selector = request.get("selector") or {}
    if selector.get("kind") != "uids":
        raise SidecarError(
            "not_implemented",
            f"selector.kind {selector.get('kind')!r} not supported in v1; "
            "only 'uids' is wired",
        )
    uids = selector.get("uids") or []
    folder = selector.get("folder", "INBOX")
    max_messages = int(request.get("max_messages", 50))
    max_body_chars = int(request.get("max_body_chars", 4000))
    fetch_tool = request.get("fetch_tool", "get_email_by_uid")

    uids = [int(u) for u in uids][:max_messages]

    messages: list[dict[str, Any]] = []
    mcp_calls = 0
    fetch_errors: list[dict[str, Any]] = []
    client = McpClient(
        command=server_cfg["command"],
        env=server_cfg.get("env"),
    )
    try:
        client.open()
        for uid in uids:
            mcp_calls += 1
            try:
                tool_result = client.call_tool(fetch_tool, {"folder": folder, "uid": uid})
            except McpClientError as e:
                fetch_errors.append({"uid": uid, "error": str(e)})
                continue
            payload = _extract_payload(tool_result)
            messages.append(_shape_message(uid, folder, payload, max_body_chars))
    finally:
        client.close()

    result_bytes = sum(len(m.get("body_text", "")) for m in messages)
    return (
        {
            "messages": messages,
            "scan_summary": {
                "uids_requested": len(uids),
                "messages_returned": len(messages),
                "errors": fetch_errors,
                "folder": folder,
                "tool": fetch_tool,
            },
        },
        {
            "messages_scanned": len(messages),
            "mcp_calls": mcp_calls,
            "result_bytes": result_bytes,
        },
    )


def _extract_payload(tool_result: dict[str, Any]) -> dict[str, Any]:
    """Pull the payload dict out of an MCP tools/call result envelope.

    rustymail-style servers return ``{"content": [{"type": "text",
    "text": "<json>"}], "isError": false}``. The text is the actual
    JSON the tool produced.
    """
    content = tool_result.get("content") or []
    for chunk in content:
        if chunk.get("type") == "text":
            try:
                return json.loads(chunk.get("text", ""))
            except json.JSONDecodeError:
                return {"raw_text": chunk.get("text", "")}
    return {}


def _shape_message(
    uid: int, folder: str, payload: dict[str, Any], max_body_chars: int
) -> dict[str, Any]:
    """Translate one server payload into the canonical fetch-batch entry."""
    from lib.body import render
    from lib.headers import classification_set

    raw = payload.get("raw") or payload.get("rfc822") or ""
    headers = classification_set(raw) if raw else {}
    body_part = render(
        raw_text=payload.get("body_text") or _split_body(raw),
        raw_html=payload.get("body_html"),
        max_chars=max_body_chars,
    )
    return {
        "uid": uid,
        "folder": payload.get("folder", folder),
        "message_id": headers.get("message_id"),
        "headers": headers,
        "body_text": body_part["body_text"],
        "body_truncated": body_part["body_truncated"],
        "body_source": body_part["source"],
        "has_attachments": bool(payload.get("has_attachments")),
        "size_bytes": int(payload.get("size_bytes") or len(raw)),
    }


def _split_body(raw: str) -> str:
    """Split RFC 5322 message into header/body and return the body."""
    if not raw:
        return ""
    parts = raw.split("\r\n\r\n", 1)
    if len(parts) == 2:
        return parts[1]
    parts = raw.split("\n\n", 1)
    return parts[1] if len(parts) == 2 else ""


# Future subcommands — declared here so the dispatcher's "unknown
# subcommand" message lists them, even though they raise NotImplemented
# until their slice ships. Mirrors design.md §6.
_PLANNED = [
    "contacts-bootstrap",
    "contacts-refresh",
    "rank-senders",
    "parse-thread",
    "prepare-bulk-delete",
    "parse-feedback-reply",
    "submit-unsubscribe",
    "parse-ical",
]

for _name in _PLANNED:
    def _make_pending(n: str) -> Handler:
        def _pending(_req: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
            raise SidecarError(
                "not_implemented",
                f"subcommand {n!r} is declared but not implemented yet",
            )

        return _pending

    HANDLERS[_name] = _make_pending(_name)


# ---------------------------------------------------------------------------
# CLI dispatch.
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="scan.py",
        description="email-processing-hand sidecar (skeleton)",
    )
    p.add_argument(
        "subcommand",
        choices=sorted(HANDLERS.keys()),
        help="which sidecar operation to run",
    )
    p.add_argument(
        "--input",
        default="-",
        help="path to request JSON, or '-' for stdin (default)",
    )
    return p.parse_args(argv)


def _read_request(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    started = time.monotonic()
    request_id = uuid.uuid4().hex

    try:
        request = _read_request(args.input)
    except json.JSONDecodeError as e:
        _emit(
            _envelope(
                args.subcommand,
                request_id,
                started,
                error={"code": "bad_request", "message": f"input JSON parse error: {e}"},
            )
        )
        return 2

    handler = HANDLERS[args.subcommand]
    try:
        result, metrics = handler(request)
    except SidecarError as e:
        _emit(
            _envelope(
                args.subcommand,
                request_id,
                started,
                error={"code": e.code, "message": e.message},
            )
        )
        return 1
    except Exception as e:  # noqa: BLE001
        _emit(
            _envelope(
                args.subcommand,
                request_id,
                started,
                error={"code": "internal_error", "message": repr(e)},
            )
        )
        return 1

    _emit(
        _envelope(
            args.subcommand,
            request_id,
            started,
            result=result,
            metrics=metrics,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
