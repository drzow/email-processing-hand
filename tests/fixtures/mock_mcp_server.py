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


def _thread_msg(uid: int, root: int, date: str, body: str = "body") -> dict:
    """Builder for the parse-thread fixture data."""
    return {
        "uid": uid,
        "folder": "INBOX",
        "headers": {
            "from": [{"name": "Sam", "addr": "sam@acme.com"}],
            "to": [{"name": "", "addr": "alice@scalesology.com"}],
            "cc": [],
            "subject": f"Re: Q3 plan (msg {uid})",
            "date": date,
            "message_id": f"<msg-{uid}@acme.com>",
            "in_reply_to": f"<msg-{root}@acme.com>" if uid != root else None,
            "references": [],
            "list_id": None,
            "list_unsubscribe": {"urls": [], "mailtos": []},
            "list_unsubscribe_post_one_click": False,
            "importance": None,
        },
        "body_text": body,
        "body_truncated": False,
    }


MOCK_THREADS: dict[int, dict] = {
    1001: {
        "thread_id": "thread-1001",
        "messages": [
            _thread_msg(1001, 1001, "2026-05-01T10:00:00Z"),
            _thread_msg(1002, 1001, "2026-05-02T10:00:00Z"),
            _thread_msg(1003, 1001, "2026-05-03T10:00:00Z"),
        ],
    },
    1002: {
        "thread_id": "thread-1002",
        "messages": [
            _thread_msg(2000 + i, 1002, f"2026-04-{i+1:02d}T10:00:00Z")
            for i in range(10)
        ],
    },
}


# Folder listings for rank-senders tests. Each "message" carries enough
# metadata to aggregate by sender without a separate fetch.
# Big synthetic folder for pagination tests. 12 messages from 4 senders
# so a page_size of 5 forces exactly 3 paginated calls (5 + 5 + 2).
_BIG_BOX: list[dict] = []
for _i in range(12):
    _sender_idx = _i % 4
    _sender = ["bigsender@example.com", "frequent@example.com",
               "occasional@example.com", "rare@example.com"][_sender_idx]
    _BIG_BOX.append(
        {
            "uid": 700 + _i,
            "from": _sender,
            "subject": f"Item {_i}",
            "date": f"2026-03-{_i+1:02d}T10:00:00Z",
            "size_bytes": 1000 + _i,
        }
    )


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
    "Sent": [
        {
            "uid": 500,
            "from": "Alice Brugger <alice@scalesology.com>",
            "to": "Sam Long <sam@acme.com>",
            "cc": "",
            "subject": "Re: Q3 plan questions",
            "date": "2026-04-01T10:00:00Z",
            "size_bytes": 5000,
        },
        {
            "uid": 501,
            "from": "alice@scalesology.com",
            "to": "Sam Long <sam@acme.com>, Alex Lee <alex@partner.com>",
            "cc": "Bob <bob@partner.com>",
            "subject": "Joint planning sync",
            "date": "2026-04-15T11:00:00Z",
            "size_bytes": 6000,
        },
        {
            "uid": 502,
            "from": "alice@scalesology.com",
            "to": "alex@partner.com",
            "cc": "",
            "subject": "FYI",
            "date": "2026-04-20T09:00:00Z",
            "size_bytes": 4000,
        },
    ],
    "BigBox": _BIG_BOX,
    # Mirrors real rustymail list_cached_emails output shape:
    # from_address / from_name / to_addresses / cc_addresses / size
    # (instead of from / to / cc / size_bytes).
    "CachedShape": [
        {
            "uid": 800,
            "from_address": "sam@acme.com",
            "from_name": "Sam Long",
            "to_addresses": ["alice@scalesology.com"],
            "cc_addresses": [],
            "subject": "Q3 plan",
            "date": "2026-05-01T09:00:00Z",
            "size": 5000,
        },
        {
            "uid": 801,
            "from_address": "sam@acme.com",
            "from_name": "Sam Long",
            "to_addresses": ["alice@scalesology.com"],
            "cc_addresses": ["bob@acme.com"],
            "subject": "Re: Q3 plan",
            "date": "2026-05-02T09:00:00Z",
            "size": 6000,
        },
        {
            "uid": 802,
            "from_address": "carol@partner.com",
            "from_name": "",
            "to_addresses": ["alice@scalesology.com"],
            "cc_addresses": [],
            "subject": "Intro",
            "date": "2026-05-03T09:00:00Z",
            "size": 3500,
        },
    ],
    "Sent Items": [
        {
            "uid": 600,
            "from": "alice@bruggerink.com",
            "to": "Sam Long <sam@acme.com>",
            "cc": "",
            "subject": "Personal follow-up",
            "date": "2026-04-05T19:00:00Z",
            "size_bytes": 3000,
        },
        {
            "uid": 601,
            "from": "alice@bruggerink.com",
            "to": "Friend <pal@example.com>",
            "cc": "",
            "subject": "Hey",
            "date": "2026-04-10T18:00:00Z",
            "size_bytes": 2500,
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
            all_msgs = FOLDER_MESSAGES.get(folder, [])
            # Honor limit + offset so pagination tests can verify the
            # subcommands actually call us repeatedly with rising offsets.
            offset = int(args.get("offset", 0) or 0)
            limit = args.get("limit")
            if limit is None:
                page = all_msgs[offset:]
            else:
                page = all_msgs[offset : offset + int(limit)]
            _result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"messages": page, "total": len(all_msgs)}
                            ),
                        }
                    ]
                },
            )
            return
        if name == "get_thread":
            root_uid = int(args.get("thread_root_uid", 0))
            thread = MOCK_THREADS.get(root_uid)
            if thread is None:
                _error(req_id, -32602, f"unknown thread root uid {root_uid}")
                return
            _result(
                req_id,
                {"content": [{"type": "text", "text": json.dumps(thread)}]},
            )
            return
        if name == "search_by_sender":
            # Canned hits for marketing@vendor.com, otherwise empty.
            if args.get("value") == "marketing@vendor.com":
                hits = [
                    {
                        "uid": 100,
                        "folder": "INBOX",
                        "subject": "May sale — unsubscribe",
                        "from": "marketing@vendor.com",
                        "date": "Mon, 01 Apr 2026 09:30:00 -0500",
                        "size_bytes": 11000,
                        "body_text": "Get 50% off. To unsubscribe click here.",
                    },
                    {
                        "uid": 101,
                        "folder": "INBOX",
                        "subject": "April sale",
                        "from": "marketing@vendor.com",
                        "date": "Sun, 01 Mar 2026 09:30:00 -0500",
                        "size_bytes": 12000,
                        "body_html": "<p>Sale!</p><a href='x'>Unsubscribe</a>",
                    },
                    {
                        "uid": 102,
                        "folder": "Archive",
                        "subject": "Order receipt #ABC-123",
                        "from": "marketing@vendor.com",
                        "date": "Wed, 01 Feb 2026 09:30:00 -0500",
                        "size_bytes": 9000,
                        "body_text": "Your order has shipped. Tracking: 1Z...",
                    },
                ]
            elif args.get("value") == "@shaggymax.example":
                # 5 messages: 4 bulk (newsletter), 1 transactional (receipt).
                hits = [
                    {
                        "uid": 200, "folder": "INBOX",
                        "subject": "Spring Newsletter",
                        "from": "news@shaggymax.example",
                        "date": "Mon, 01 May 2026 09:00:00 -0500",
                        "size_bytes": 8000,
                        "body_text": "Big spring savings. Unsubscribe here.",
                    },
                    {
                        "uid": 201, "folder": "INBOX",
                        "subject": "Summer deals",
                        "from": "news@shaggymax.example",
                        "date": "Mon, 01 Apr 2026 09:00:00 -0500",
                        "size_bytes": 8500,
                        "body_html": "<a href='#'>unsubscribe</a> from list",
                    },
                    {
                        "uid": 202, "folder": "Archive",
                        "subject": "Order #5512 confirmation",
                        "from": "orders@shaggymax.example",
                        "date": "Tue, 12 Feb 2026 14:00:00 -0500",
                        "size_bytes": 4000,
                        "body_text": "Thanks for your order #5512.",
                    },
                    {
                        "uid": 203, "folder": "Archive",
                        "subject": "Black Friday",
                        "from": "news@shaggymax.example",
                        "date": "Fri, 27 Nov 2025 10:00:00 -0500",
                        "size_bytes": 9000,
                        "body_text": "70% off everything. opt-out at preferences.",
                    },
                    {
                        "uid": 204, "folder": "Archive",
                        "subject": "Holiday catalogue",
                        "from": "news@shaggymax.example",
                        "date": "Tue, 01 Dec 2024 10:00:00 -0500",
                        "size_bytes": 12000,
                        "body_html": "<p>shop now</p><a>unsubscribe</a>",
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
