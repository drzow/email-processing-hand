"""Unit tests for the contacts-bootstrap subcommand."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_PY = REPO_ROOT / "tools" / "scan.py"
MOCK_SERVER = REPO_ROOT / "tests" / "fixtures" / "mock_mcp_server.py"


def run_contacts(request: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "contacts-bootstrap"],
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


def test_builds_contacts_from_one_account_sent_folder() -> None:
    env = run_contacts(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "accounts": [
                {"account_id": "alice@scalesology.com", "sent_folder": "Sent"}
            ],
        }
    )
    assert env["status"] == "ok", env
    contacts = env["result"]["contacts"]
    # Mock fixture: Sent has 3 messages with To: sam, alex, bob.
    assert "sam@acme.com" in contacts
    assert "alex@partner.com" in contacts
    assert contacts["sam@acme.com"]["accounts"] == ["alice@scalesology.com"]
    assert contacts["sam@acme.com"]["message_count"] >= 1


def test_aggregates_across_multiple_accounts() -> None:
    env = run_contacts(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "accounts": [
                {"account_id": "alice@scalesology.com", "sent_folder": "Sent"},
                {"account_id": "alice@bruggerink.com", "sent_folder": "Sent Items"},
            ],
        }
    )
    assert env["status"] == "ok"
    contacts = env["result"]["contacts"]
    # Each address has an accounts list with one or more account_ids.
    for entry in contacts.values():
        assert isinstance(entry["accounts"], list)
        assert all(isinstance(a, str) for a in entry["accounts"])
    # sam@acme.com appears in both account's Sent folders.
    assert set(contacts["sam@acme.com"]["accounts"]) == {
        "alice@scalesology.com",
        "alice@bruggerink.com",
    }


def test_message_count_increments_per_observation() -> None:
    env = run_contacts(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "accounts": [
                {"account_id": "alice@scalesology.com", "sent_folder": "Sent"}
            ],
        }
    )
    # sam@acme.com appears as To: on 2 of the 3 Sent messages in the fixture.
    assert env["result"]["contacts"]["sam@acme.com"]["message_count"] == 2


def test_first_seen_and_last_seen_track_oldest_and_newest() -> None:
    env = run_contacts(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "accounts": [
                {"account_id": "alice@scalesology.com", "sent_folder": "Sent"}
            ],
        }
    )
    sam = env["result"]["contacts"]["sam@acme.com"]
    # The fixture has Sam as To: on a 2026-04-01 and a 2026-04-15 message.
    assert sam["first_seen"] <= sam["last_seen"]
    assert sam["first_seen"].startswith("2026-04-01")
    assert sam["last_seen"].startswith("2026-04-15")


def test_display_name_picked_from_first_observation() -> None:
    env = run_contacts(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "accounts": [
                {"account_id": "alice@scalesology.com", "sent_folder": "Sent"}
            ],
        }
    )
    # Fixture: first time we see sam@acme.com is To: "Sam Long <sam@acme.com>"
    assert env["result"]["contacts"]["sam@acme.com"]["display_name"] == "Sam Long"


def test_cc_addresses_also_included() -> None:
    env = run_contacts(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "accounts": [
                {"account_id": "alice@scalesology.com", "sent_folder": "Sent"}
            ],
        }
    )
    # Fixture: bob@partner.com appears only on Cc:.
    assert "bob@partner.com" in env["result"]["contacts"]


def test_scan_summary_reports_counts() -> None:
    env = run_contacts(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "accounts": [
                {"account_id": "alice@scalesology.com", "sent_folder": "Sent"}
            ],
        }
    )
    summary = env["result"]["scan_summary"]
    assert summary["sent_messages_scanned"] >= 3
    assert summary["addresses_found"] >= 3
    assert summary["per_account"][0]["account_id"] == "alice@scalesology.com"
    assert summary["per_account"][0]["sent_folder"] == "Sent"


# ---------- since cutoff -------------------------------------------------


def test_since_filter_drops_older_messages() -> None:
    env = run_contacts(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "since": "2026-04-10T00:00:00Z",  # cutoff after April 1
            "accounts": [
                {"account_id": "alice@scalesology.com", "sent_folder": "Sent"}
            ],
        }
    )
    # sam@acme.com on 2026-04-01 should be dropped; only the 2026-04-15
    # message remains, so message_count should be 1 instead of 2.
    sam = env["result"]["contacts"].get("sam@acme.com")
    assert sam is not None
    assert sam["message_count"] == 1


# ---------- error paths --------------------------------------------------


def test_requires_mcp_server_command() -> None:
    env = run_contacts(
        {"accounts": [{"account_id": "x@y.com", "sent_folder": "Sent"}]}
    )
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"


def test_requires_accounts_list() -> None:
    env = run_contacts({"mcp_server": _mock_server_cfg()})
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"
    assert "accounts" in env["error"]["message"]


def test_per_account_mcp_error_does_not_kill_run() -> None:
    """If one account fails, the other still completes; the failing
    account is named in scan_summary.per_account[*].error."""
    env = run_contacts(
        {
            "mcp_server": _mock_server_cfg(),
            "list_tool": "list_emails_in_folder",
            "accounts": [
                {"account_id": "alice@scalesology.com", "sent_folder": "Sent"},
                # _UNKNOWN_FOLDER triggers the mock's no-such-folder branch (empty list).
                {"account_id": "ghost@nowhere.com", "sent_folder": "_NO_SUCH"},
            ],
        }
    )
    assert env["status"] == "ok"
    contacts = env["result"]["contacts"]
    assert "sam@acme.com" in contacts
