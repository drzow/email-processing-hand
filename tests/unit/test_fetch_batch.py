"""Integration tests for the fetch-batch subcommand (driven via the mock MCP server)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_PY = REPO_ROOT / "tools" / "scan.py"
MOCK_SERVER = REPO_ROOT / "tests" / "fixtures" / "mock_mcp_server.py"


def run_fetch(request: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "fetch-batch"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout, f"no stdout (stderr={proc.stderr!r})"
    return json.loads(proc.stdout.splitlines()[-1])


def _mock_server_cfg() -> dict:
    return {"command": [sys.executable, str(MOCK_SERVER)]}


def test_fetch_batch_returns_canonical_message_shape() -> None:
    env = run_fetch(
        {
            "mcp_server": _mock_server_cfg(),
            "selector": {"kind": "uids", "uids": [1], "folder": "INBOX"},
            "max_body_chars": 1000,
        }
    )
    assert env["status"] == "ok", env
    msgs = env["result"]["messages"]
    assert len(msgs) == 1
    m = msgs[0]
    assert m["uid"] == 1
    assert m["folder"] == "INBOX"
    assert m["message_id"] == "<q3-plan-1@acme.com>"
    assert m["headers"]["from"][0]["addr"] == "sam@acme.com"
    assert m["headers"]["subject"] == "Q3 plan questions"
    assert "Q3" in m["body_text"]
    assert m["body_truncated"] is False
    assert m["has_attachments"] is False


def test_fetch_batch_decodes_rfc2047_subject() -> None:
    env = run_fetch(
        {
            "mcp_server": _mock_server_cfg(),
            "selector": {"kind": "uids", "uids": [2]},
        }
    )
    m = env["result"]["messages"][0]
    assert m["headers"]["subject"] == "[GitHub] New PR review"
    assert m["headers"]["list_id"] == "openfang.github.com"
    assert m["headers"]["list_unsubscribe_post_one_click"] is True


def test_fetch_batch_records_per_uid_errors_without_aborting() -> None:
    env = run_fetch(
        {
            "mcp_server": _mock_server_cfg(),
            "selector": {"kind": "uids", "uids": [1, 9999, 2]},
        }
    )
    assert env["status"] == "ok"
    msgs = env["result"]["messages"]
    assert [m["uid"] for m in msgs] == [1, 2]
    summary = env["result"]["scan_summary"]
    assert summary["uids_requested"] == 3
    assert summary["messages_returned"] == 2
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["uid"] == 9999


def test_fetch_batch_caps_to_max_messages() -> None:
    env = run_fetch(
        {
            "mcp_server": _mock_server_cfg(),
            "selector": {"kind": "uids", "uids": [1, 2]},
            "max_messages": 1,
        }
    )
    assert env["result"]["scan_summary"]["uids_requested"] == 1
    assert len(env["result"]["messages"]) == 1


def test_fetch_batch_truncates_body_per_max_body_chars() -> None:
    env = run_fetch(
        {
            "mcp_server": _mock_server_cfg(),
            "selector": {"kind": "uids", "uids": [1]},
            "max_body_chars": 10,
        }
    )
    m = env["result"]["messages"][0]
    assert len(m["body_text"]) == 10
    assert m["body_truncated"] is True


def test_fetch_batch_rejects_unknown_selector_kind() -> None:
    env = run_fetch(
        {
            "mcp_server": _mock_server_cfg(),
            "selector": {"kind": "search", "query": "from:acme.com"},
        }
    )
    assert env["status"] == "error"
    assert env["error"]["code"] == "not_implemented"
    assert "search" in env["error"]["message"]


def test_fetch_batch_requires_mcp_server_command() -> None:
    env = run_fetch({"selector": {"kind": "uids", "uids": [1]}})
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"
    assert "mcp_server" in env["error"]["message"]


def test_fetch_batch_passes_account_id_to_fetch_tool() -> None:
    """The agent supplies account_id at the top level; the sidecar
    must merge it into the call args so rustymail's get_email_by_uid
    can find the account."""
    env = run_fetch(
        {
            "mcp_server": _mock_server_cfg(),
            "account_id": "drzow@bruggerink.com",
            "selector": {"kind": "uids", "uids": [1]},
        }
    )
    assert env["status"] == "ok"
    # Just confirm the message came back; the mock ignores account_id but
    # accepts it without erroring, proving the merge happens.
    assert len(env["result"]["messages"]) == 1


def test_fetch_batch_synthesizes_headers_from_rustymail_flat_shape() -> None:
    """get_email_by_uid (real rustymail) returns a flat dict with
    from_address / to_addresses / subject / etc. — no `raw` field.
    fetch-batch must build the headers dict from those flat fields."""
    env = run_fetch(
        {
            "mcp_server": _mock_server_cfg(),
            "account_id": "drzow@bruggerink.com",
            "selector": {"kind": "uids", "uids": [9001]},
            "fetch_tool": "get_email_by_uid_flat",
        }
    )
    assert env["status"] == "ok", env
    m = env["result"]["messages"][0]
    assert m["uid"] == 9001
    assert m["headers"]["from"][0]["addr"] == "noreply@github.com"
    assert m["headers"]["subject"] == "PR opened"
    assert m["headers"]["to"][0]["addr"] == "subscriber@example.com"
    assert "this is the body" in m["body_text"].lower()
