#!/usr/bin/env python3
"""Tiny stdio MCP server for the sidecar's mcp_client tests.

Speaks the same JSON-RPC line-delimited dialect rustymail does. Tools
it implements:

* ``echo``         — returns its arguments verbatim under
                     ``content[0].text`` as a JSON-encoded string.
* ``raise_error``  — replies with a JSON-RPC error response.
* ``get_email_by_uid`` — returns a canned RFC 5322 message so
                     ``fetch-batch`` integration tests can drive a
                     real fetch path without a real mail server.

Read CANNED_MESSAGES at the top to add fixtures for new test cases.
"""

from __future__ import annotations

import json
import sys


CANNED_MESSAGES: dict[int, str] = {
    1: (
        "From: Sam Long <sam@acme.com>\r\n"
        "To: alice@scalesology.com\r\n"
        "Subject: Q3 plan questions\r\n"
        "Date: Mon, 01 Apr 2026 09:30:00 -0500\r\n"
        "Message-ID: <q3-plan-1@acme.com>\r\n"
        "\r\n"
        "Hi Alice,\r\n\r\nA few questions about Q3 — when can we sync?\r\n"
    ),
    2: (
        "From: noreply@github.com\r\n"
        "To: alice@scalesology.com\r\n"
        "Subject: =?UTF-8?Q?[GitHub]_New_PR_review?=\r\n"
        "Date: Mon, 01 Apr 2026 10:00:00 -0500\r\n"
        "Message-ID: <gh-pr-1@github.com>\r\n"
        "List-Id: <openfang.github.com>\r\n"
        "List-Unsubscribe: <https://github.com/unsub>, <mailto:u@github.com>\r\n"
        "List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n"
        "\r\n"
        "PR review requested on openfang.\r\n"
    ),
}


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _result(req_id: int | str, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: int | str, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle(req: dict) -> None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        _result(
            req_id,
            {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mock-mcp", "version": "0.1.0"},
            },
        )
        return

    if req_id is None:
        # Notification — nothing to reply to.
        return

    if method == "tools/list":
        _result(
            req_id,
            {
                "tools": [
                    {"name": "echo", "description": "...", "inputSchema": {}},
                    {"name": "raise_error", "description": "...", "inputSchema": {}},
                    {"name": "get_email_by_uid", "description": "...", "inputSchema": {}},
                ]
            },
        )
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            _result(
                req_id,
                {"content": [{"type": "text", "text": json.dumps(args)}]},
            )
            return
        if name == "raise_error":
            _error(req_id, args.get("code", -32603), args.get("message", "boom"))
            return
        if name == "get_email_by_uid":
            uid = int(args.get("uid", 0))
            raw = CANNED_MESSAGES.get(uid)
            if raw is None:
                _error(req_id, -32602, f"unknown uid {uid}")
                return
            payload = {
                "uid": uid,
                "folder": args.get("folder", "INBOX"),
                "raw": raw,
                "size_bytes": len(raw),
                "has_attachments": False,
            }
            _result(
                req_id,
                {"content": [{"type": "text", "text": json.dumps(payload)}]},
            )
            return
        _error(req_id, -32601, f"unknown tool {name!r}")
        return

    _error(req_id, -32601, f"method {method!r} not implemented in mock")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle(req)
    return 0


if __name__ == "__main__":
    sys.exit(main())
