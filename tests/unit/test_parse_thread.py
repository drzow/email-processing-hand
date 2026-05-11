"""Unit tests for the parse-thread subcommand."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_PY = REPO_ROOT / "tools" / "scan.py"
MOCK_SERVER = REPO_ROOT / "tests" / "fixtures" / "mock_mcp_server.py"


def run_thread(request: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "parse-thread"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout, f"stderr={proc.stderr!r}"
    return json.loads(proc.stdout.splitlines()[-1])


def _mock_server_cfg() -> dict:
    return {"command": [sys.executable, str(MOCK_SERVER)]}


# ---------- short thread (≤ max_displayed) ------------------------------


def test_short_thread_returns_all_displayed_none_elided() -> None:
    env = run_thread(
        {
            "mcp_server": _mock_server_cfg(),
            "fetch_tool": "get_thread",
            "account_id": "alice@scalesology.com",
            "thread_root_uid": 1001,  # mock fixture: 3 messages
            "max_displayed": 5,
        }
    )
    assert env["status"] == "ok", env
    r = env["result"]
    assert r["total_count"] == 3
    assert len(r["displayed"]) == 3
    assert r["elided_count"] == 0
    assert r["elided"] == []
    # Each displayed message keeps full headers + body.
    for m in r["displayed"]:
        assert "headers" in m
        assert "body_text" in m


# ---------- long thread: top 5 displayed, rest elided -------------------


def test_long_thread_truncates_to_max_displayed_keeping_newest() -> None:
    env = run_thread(
        {
            "mcp_server": _mock_server_cfg(),
            "fetch_tool": "get_thread",
            "account_id": "alice@scalesology.com",
            "thread_root_uid": 1002,  # mock fixture: 10 messages
            "max_displayed": 5,
        }
    )
    r = env["result"]
    assert r["total_count"] == 10
    assert len(r["displayed"]) == 5
    assert r["elided_count"] == 5
    # Displayed should be the NEWEST 5 (descending date).
    displayed_dates = [m["headers"]["date"] for m in r["displayed"]]
    assert displayed_dates == sorted(displayed_dates, reverse=True)
    # Elided entries are summary-only (subject, from, date) — no body.
    for e in r["elided"]:
        assert set(e.keys()) == {"subject", "from", "date"}
    # Elided are the OLDEST messages.
    elided_dates = [e["date"] for e in r["elided"]]
    newest_elided = max(elided_dates)
    oldest_displayed = min(displayed_dates)
    assert newest_elided < oldest_displayed


def test_elided_messages_sorted_oldest_first() -> None:
    env = run_thread(
        {
            "mcp_server": _mock_server_cfg(),
            "fetch_tool": "get_thread",
            "account_id": "alice@scalesology.com",
            "thread_root_uid": 1002,
            "max_displayed": 3,
        }
    )
    elided = env["result"]["elided"]
    dates = [e["date"] for e in elided]
    assert dates == sorted(dates)


# ---------- thread_id propagation ---------------------------------------


def test_thread_id_pulled_from_fetched_payload() -> None:
    env = run_thread(
        {
            "mcp_server": _mock_server_cfg(),
            "fetch_tool": "get_thread",
            "account_id": "alice@scalesology.com",
            "thread_root_uid": 1001,
        }
    )
    # Mock fixture sets thread_id = "thread-1001".
    assert env["result"]["thread_id"] == "thread-1001"


# ---------- summary metric ----------------------------------------------


def test_summary_counts_match_displayed_plus_elided() -> None:
    env = run_thread(
        {
            "mcp_server": _mock_server_cfg(),
            "fetch_tool": "get_thread",
            "account_id": "alice@scalesology.com",
            "thread_root_uid": 1002,
            "max_displayed": 5,
        }
    )
    r = env["result"]
    assert r["total_count"] == len(r["displayed"]) + r["elided_count"]


# ---------- error paths --------------------------------------------------


def test_requires_mcp_server() -> None:
    env = run_thread({"thread_root_uid": 1001})
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"


def test_requires_thread_root_uid() -> None:
    env = run_thread(
        {"mcp_server": _mock_server_cfg(), "account_id": "x@y.com"}
    )
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"
    assert "thread_root_uid" in env["error"]["message"]


def test_unknown_thread_returns_error() -> None:
    env = run_thread(
        {
            "mcp_server": _mock_server_cfg(),
            "fetch_tool": "get_thread",
            "account_id": "x@y.com",
            "thread_root_uid": 99999,
        }
    )
    # Mock returns -32602 unknown thread; sidecar surfaces it.
    assert env["status"] == "error"
    assert "99999" in env["error"]["message"] or "unknown" in env["error"]["message"]
