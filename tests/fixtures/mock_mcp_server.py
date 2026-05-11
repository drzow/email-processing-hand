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


# Folder listings for rank-senders tests. Each "message" carries enough
# metadata to aggregate by sender without a separate fetch.
FOLDER_MESSAGES: dict[str, list[dict]] = {
    "INBOX": [
        {
            "uid": 10,
            "from": "Marketing Bot <marketing@vendor.com>",
            "subject": "May sale",
            "date": "2026-05-01T09:00:00Z",
            "size_bytes": 11000,
        },
        {
            "uid": 11,
            "from": "Marketing Bot <marketing@vendor.com>",
            "subject": "April sale",
            "date": "2026-04-15T09:00:00Z",
            "size_bytes": 12000,
        },
        {
            "uid": 12,
            "from": "marketing@vendor.com",  # no display name
            "subject": "Mid-April special",
            "date": "2026-04-10T09:00:00Z",
            "size_bytes": 10000,
        },
        {
            "uid": 13,
            "from": "Sam Long <sam@acme.com>",
            "subject": "Q3 plan questions",
            "date": "2026-05-02T10:00:00Z",
            "size_bytes": 5000,
        },
        {
            "uid": 14,
            "from": "Sam Long <sam@acme.com>",
            "subject": "Re: Q3 plan",
            "date": "2026-05-03T10:00:00Z",
            "size_bytes": 5200,
        },
        {
            "uid": 15,
            "from": "alice@scalesology.com",  # self
            "subject": "Note to self",
            "date": "2026-05-04T11:00:00Z",
            "size_bytes": 800,
        },
    ],
    "Archive": [
        {
            "uid": 100,
            "from": "marketing@vendor.com",
            "subject": "March sale",
            "date": "2026-03-15T09:00:00Z",
            "size_bytes": 9500,
        },
        {
            "uid": 101,
            "from": "marketing@vendor.com",
            "subject": "Feb sale",
            "date": "2026-02-15T09:00:00Z",
            "size_bytes": 9200,
        },
        {
            "uid": 102,
            "from": "marketing@vendor.com",
            "subject": "Jan sale",
            "date": "2026-01-15T09:00:00Z",
            "size_bytes": 9100,
        },
        {
            "uid": 103,
            "from": "marketing@vendor.com",
            "subject": "Holiday sale",
            "date": "2025-12-15T09:00:00Z",
            "size_bytes": 9300,
        },
        {
            "uid": 104,
            "from": "marketing@vendor.com",
            "subject": "Black Friday",
            "date": "2025-11-26T09:00:00Z",
            "size_bytes": 9400,
        },
        {
            "uid": 200,
            "from": "Boss Person <boss@acme.com>",
            "subject": "Year end review",
            "date": "2025-12-20T15:00:00Z",
            "size_bytes": 6000,
        },
    ],
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
        if name == "list_folders":
            _result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"folders": sorted(FOLDER_MESSAGES.keys())}
                            ),
                        }
                    ]
                },
            )
            return
        if name == "list_emails_in_folder":
            folder = args.get("folder", "INBOX")
            messages = FOLDER_MESSAGES.get(folder, [])
            _result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"messages": messages, "total": len(messages)}
                            ),
                        }
                    ]
                },
            )
            return
        if name == "search_by_sender":
            # Canned hits for marketing@vendor.com, otherwise empty.
            if args.get("value") == "marketing@vendor.com":
                hits = [
                    {
                        "uid": 100,
                        "folder": "INBOX",
                        "subject": "May sale",
                        "from": "marketing@vendor.com",
                        "date": "Mon, 01 Apr 2026 09:30:00 -0500",
                        "size_bytes": 11000,
                    },
                    {
                        "uid": 101,
                        "folder": "INBOX",
                        "subject": "April sale",
                        "from": "marketing@vendor.com",
                        "date": "Sun, 01 Mar 2026 09:30:00 -0500",
                        "size_bytes": 12000,
                    },
                    {
                        "uid": 102,
                        "folder": "Archive",
                        "subject": "March sale",
                        "from": "marketing@vendor.com",
                        "date": "Wed, 01 Feb 2026 09:30:00 -0500",
                        "size_bytes": 9000,
                    },
                ]
            else:
                hits = []
            payload = {"matches": hits, "total": len(hits)}
            _result(
                req_id,
                {"content": [{"type": "text", "text": json.dumps(payload)}]},
            )
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
