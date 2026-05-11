"""Unit tests for the rank-senders subcommand."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_PY = REPO_ROOT / "tools" / "scan.py"
MOCK_SERVER = REPO_ROOT / "tests" / "fixtures" / "mock_mcp_server.py"


def run_rank(request: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "rank-senders"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout, f"stderr={proc.stderr!r}"
    return json.loads(proc.stdout.splitlines()[-1])


def _mock_server_cfg() -> dict:
    return {"command": [sys.executable, str(MOCK_SERVER)]}


# ---------- happy path ---------------------------------------------------


def test_ranks_senders_by_count_default() -> None:
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "scope": "inbox",
            "metric": "count",
            "limit": 5,
        }
    )
    assert env["status"] == "ok", env
    ranking = env["result"]["ranking"]
    # Mock fixture has marketing@vendor.com as the top sender by count.
    assert ranking[0]["sender"] == "marketing@vendor.com"
    assert ranking[0]["message_count"] >= ranking[-1]["message_count"]
    # Each entry has the expected shape.
    for entry in ranking:
        for key in (
            "sender",
            "name",
            "message_count",
            "total_bytes",
            "sample_subjects",
            "oldest",
            "newest",
            "folders",
        ):
            assert key in entry, f"missing key {key} in {entry}"


def test_ranks_by_volume_orders_by_bytes() -> None:
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "scope": "inbox",
            "metric": "volume",
            "limit": 5,
        }
    )
    ranking = env["result"]["ranking"]
    # Each entry's total_bytes >= the next entry's.
    for prev, nxt in zip(ranking, ranking[1:]):
        assert prev["total_bytes"] >= nxt["total_bytes"]


def test_limit_caps_returned_senders() -> None:
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "scope": "inbox",
            "limit": 2,
        }
    )
    assert len(env["result"]["ranking"]) <= 2


def test_all_folders_scope_walks_every_folder() -> None:
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "list_folders_tool": "list_folders",
            "scope": "all_folders",
            "metric": "count",
            "limit": 10,
        }
    )
    summary = env["result"]["scan_summary"]
    # Mock now has INBOX, Archive, Sent, Sent Items for the contacts
    # fixture — all_folders should walk all of them.
    assert set(summary["folders_scanned"]) >= {"INBOX", "Archive"}
    # marketing@vendor.com appears in INBOX + Archive (the only sender
    # the test cares about); Sent folders contain alice's outbound mail
    # not the marketing newsletters.
    top = next(
        e for e in env["result"]["ranking"] if e["sender"] == "marketing@vendor.com"
    )
    assert {"INBOX", "Archive"} <= set(top["folders"])


def test_explicit_folders_list_overrides_scope() -> None:
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "folders": ["Archive"],
            "metric": "count",
            "limit": 5,
        }
    )
    assert env["result"]["scan_summary"]["folders_scanned"] == ["Archive"]


# ---------- sample subjects + sender display names ----------------------


def test_sample_subjects_dedup_and_truncate() -> None:
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "scope": "inbox",
            "metric": "count",
            "limit": 5,
            "sample_subjects_max": 3,
        }
    )
    top = env["result"]["ranking"][0]
    assert len(top["sample_subjects"]) <= 3
    # Subjects come in dedup'd / unique order — set semantics over the
    # short list should hold.
    assert len(top["sample_subjects"]) == len(set(top["sample_subjects"]))


def test_sender_display_name_picked_from_first_observation() -> None:
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "scope": "inbox",
            "metric": "count",
            "limit": 5,
        }
    )
    top = env["result"]["ranking"][0]
    # Mock fixture sends "Marketing Bot <marketing@vendor.com>" as From.
    assert top["sender"] == "marketing@vendor.com"
    assert top["name"] == "Marketing Bot"


# ---------- exclude-already-processed-until ------------------------------


def test_exclude_processed_until_drops_messages_at_or_before() -> None:
    """Messages with internaldate <= the cutoff are not aggregated."""
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "scope": "inbox",
            "metric": "count",
            # Mock's INBOX has one message dated 2026-04-15.
            "exclude_already_processed_until": "2026-04-30T00:00:00Z",
            "limit": 50,
        }
    )
    summary = env["result"]["scan_summary"]
    assert summary["messages_excluded"] >= 1


# ---------- error paths --------------------------------------------------


def test_requires_mcp_server_command() -> None:
    env = run_rank({"scope": "inbox"})
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"


def test_rejects_unknown_metric() -> None:
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "scope": "inbox",
            "metric": "rage",
        }
    )
    assert env["status"] == "error"
    assert "metric" in env["error"]["message"]


def test_pagination_aggregates_across_multiple_pages() -> None:
    """The BigBox fixture has 12 messages from 4 senders. With page_size=5
    the sidecar must call the list tool 3 times (5 + 5 + 2) and aggregate
    all 12 to produce correct per-sender counts."""
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "folders": ["BigBox"],
            "metric": "count",
            "limit": 10,
            "page_size": 5,
        }
    )
    assert env["status"] == "ok"
    summary = env["result"]["scan_summary"]
    assert summary["messages_scanned"] == 12
    # 4 senders, each seen 3 times (BigBox round-robins 4 senders × 3).
    by_sender = {r["sender"]: r["message_count"] for r in env["result"]["ranking"]}
    assert by_sender == {
        "bigsender@example.com": 3,
        "frequent@example.com": 3,
        "occasional@example.com": 3,
        "rare@example.com": 3,
    }
    # We made multiple pages of MCP calls (3 for BigBox).
    assert env["metrics"]["mcp_calls"] >= 3


def test_propagates_mcp_error_from_list_tool() -> None:
    env = run_rank(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "raise_error",
            "list_tool_args": {"code": -32603, "message": "list failed"},
            "folders": ["INBOX"],
        }
    )
    assert env["status"] == "error"
    assert "list failed" in env["error"]["message"]
