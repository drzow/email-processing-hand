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
