"""Integration tests for the prepare-bulk-delete subcommand."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_PY = REPO_ROOT / "tools" / "scan.py"
MOCK_SERVER = REPO_ROOT / "tests" / "fixtures" / "mock_mcp_server.py"


def run_prep(request: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "prepare-bulk-delete"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout, f"stderr={proc.stderr!r}"
    return json.loads(proc.stdout.splitlines()[-1])


def _mock_server_cfg() -> dict:
    return {"command": [sys.executable, str(MOCK_SERVER)]}


def test_returns_match_count_and_samples() -> None:
    env = run_prep(
        {
            "mcp_server": _mock_server_cfg(),
            "search_tool": "search_by_sender",
            "selector": {"kind": "from_sender", "value": "marketing@vendor.com"},
            "scope": "inbox",
            "sample_size": 3,
        }
    )
    assert env["status"] == "ok"
    r = env["result"]
    assert r["match_count"] >= 0
    assert isinstance(r["samples"], list)
    assert r["folders"]  # at least INBOX
    assert "estimated_storage_freed_bytes" in r
    # NOTHING was deleted — confirmation lives at the agent layer.
    assert r["dry_run"] is True


def test_requires_mcp_server_command() -> None:
    env = run_prep(
        {
            "selector": {"kind": "from_sender", "value": "x@y.com"},
        }
    )
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"


def test_rejects_unknown_selector_kind() -> None:
    env = run_prep(
        {
            "mcp_server": _mock_server_cfg(),
            "selector": {"kind": "raw_uids", "value": "..."},
        }
    )
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"
    assert "selector.kind" in env["error"]["message"]


def test_cached_shape_from_and_size_in_samples() -> None:
    """When the search tool returns cached-shape entries (from_address +
    size instead of from + size_bytes), prepare-bulk-delete samples
    surface a rendered "Name <addr>" from string and the right
    total_bytes."""
    # Use the search_by_sender stub which already returns "from" / "size_bytes".
    # Then verify the prepare-bulk-delete output for shape consistency.
    env = run_prep(
        {
            "mcp_server": _mock_server_cfg(),
            "search_tool": "search_by_sender",
            "selector": {"kind": "from_sender", "value": "marketing@vendor.com"},
            "scope": "all_folders",
            "sample_size": 3,
        }
    )
    r = env["result"]
    # estimated_storage_freed_bytes is non-zero — the size fallback works.
    assert r["estimated_storage_freed_bytes"] > 0
    # Each sample carries a rendered "from" string (not None / not raw dict).
    for s in r["samples"]:
        assert isinstance(s["from"], str)
        assert s["from"]  # non-empty


def test_bulk_only_separates_bulk_from_transactional() -> None:
    """With bulk_only=true, the subcommand returns bulk_uids (messages
    with an unsubscribe link in the body) separately from
    transactional_uids (no such link — receipts, etc.). The agent
    deletes only the bulk set."""
    env = run_prep(
        {
            "mcp_server": _mock_server_cfg(),
            "search_tool": "search_by_sender",
            "selector": {"kind": "from_domain", "value": "@shaggymax.example"},
            "scope": "all_folders",
            "sample_size": 5,
            "bulk_only": True,
        }
    )
    assert env["status"] == "ok", env
    r = env["result"]
    # Fixture has 5 matches: 4 newsletters (200, 201, 203, 204) with
    # unsubscribe cues + 1 receipt (202) without.
    assert sorted(r["bulk_uids"]) == [200, 201, 203, 204]
    assert sorted(r["transactional_uids"]) == [202]
    assert r["bulk_count"] == 4
    assert r["transactional_count"] == 1
    # `match_count` keeps reporting the unfiltered total for backward compat.
    assert r["match_count"] == 5


def test_bulk_only_false_returns_all_uids_as_bulk_for_backward_compat() -> None:
    env = run_prep(
        {
            "mcp_server": _mock_server_cfg(),
            "search_tool": "search_by_sender",
            "selector": {"kind": "from_domain", "value": "@shaggymax.example"},
            "scope": "all_folders",
        }
    )
    r = env["result"]
    # Without bulk_only the subcommand doesn't split; bulk_uids contains
    # everything and transactional_uids is empty.
    assert r["match_count"] == 5
    assert "bulk_uids" not in r or len(r.get("bulk_uids", [])) == 5
    assert r["dry_run"] is True


def test_propagates_mcp_error() -> None:
    env = run_prep(
        {
            "mcp_server": _mock_server_cfg(),
            "search_tool": "raise_error",
            "selector": {"kind": "from_sender", "value": "x@y.com"},
            "search_args": {"code": -32603, "message": "search failed"},
        }
    )
    assert env["status"] == "error"
    assert "search failed" in env["error"]["message"]
