"""Unit tests for the classify-context subcommand."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_PY = REPO_ROOT / "tools" / "scan.py"


def run_classify(request: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "classify-context"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout, f"stderr={proc.stderr!r}"
    return json.loads(proc.stdout.splitlines()[-1])


# ---------- happy path ---------------------------------------------------


def test_known_sender_within_project_resolves_cleanly() -> None:
    raw = (
        "From: sam@acme.com\r\n"
        "To: alice@scalesology.com\r\n"
        "Subject: Q3 plan\r\n"
        "Date: Mon, 01 Apr 2026 09:30:00 -0500\r\n"
        "\r\n"
        "Hello\r\n"
    )
    env = run_classify(
        {
            "message": {"raw": raw},
            "user_domains": ["scalesology.com"],
            "exclude_domains": [],
            "project_map": {"acme.com": "Acme"},
            "contacts": {"sam@acme.com": {"display_name": "Sam Long"}},
        }
    )
    assert env["status"] == "ok"
    r = env["result"]
    assert r["matched_project"] == "Acme"
    assert r["signals"]["contact_status"] == "known"
    assert r["signals"]["is_invite"] is False
    assert r["signals"]["is_mass_mail"] is False
    assert r["signals"]["vip_match"] is None
    assert r["signals"]["urgency_flags"] == []


# ---------- mass-mail detection -----------------------------------------


def test_list_id_triggers_mass_mail_signal() -> None:
    raw = (
        "From: marketing@vendor.com\r\n"
        "To: alice@scalesology.com\r\n"
        "Subject: Newsletter\r\n"
        "List-Id: <news.vendor.com>\r\n"
        "\r\n"
    )
    env = run_classify(
        {"message": {"raw": raw}, "user_domains": ["scalesology.com"]}
    )
    assert env["result"]["signals"]["is_mass_mail"] is True


def test_list_unsubscribe_triggers_mass_mail_signal() -> None:
    raw = (
        "From: marketing@vendor.com\r\n"
        "To: alice@scalesology.com\r\n"
        "Subject: Sale\r\n"
        "List-Unsubscribe: <https://vendor.com/unsub>\r\n"
        "\r\n"
    )
    env = run_classify({"message": {"raw": raw}})
    assert env["result"]["signals"]["is_mass_mail"] is True


# ---------- urgency signals (server flags only; tone is the LLM's job) ---


def test_importance_high_appears_in_urgency_flags() -> None:
    raw = (
        "From: boss@acme.com\r\n"
        "To: alice@scalesology.com\r\n"
        "Subject: Need this today\r\n"
        "Importance: high\r\n"
        "\r\n"
    )
    env = run_classify({"message": {"raw": raw}})
    flags = env["result"]["signals"]["urgency_flags"]
    assert "importance:high" in flags


def test_subject_urgent_keyword_appears_in_urgency_flags() -> None:
    raw = (
        "From: boss@acme.com\r\n"
        "Subject: URGENT: production is down\r\n"
        "\r\n"
    )
    env = run_classify({"message": {"raw": raw}})
    assert any("urgent" in f for f in env["result"]["signals"]["urgency_flags"])


# ---------- VIP override -------------------------------------------------


def test_vip_sender_match_is_surfaced() -> None:
    raw = "From: ceo@acme.com\r\nTo: me@scalesology.com\r\nSubject: x\r\n\r\n"
    env = run_classify(
        {
            "message": {"raw": raw},
            "user_domains": ["scalesology.com"],
            "vip_senders": ["ceo@acme.com", "spouse@example.com"],
        }
    )
    assert env["result"]["signals"]["vip_match"] == "ceo@acme.com"


def test_vip_match_is_case_insensitive() -> None:
    raw = "From: CEO@Acme.COM\r\nSubject: x\r\n\r\n"
    env = run_classify(
        {
            "message": {"raw": raw},
            "vip_senders": ["ceo@acme.com"],
        }
    )
    assert env["result"]["signals"]["vip_match"] == "ceo@acme.com"


# ---------- invite detection --------------------------------------------


def test_text_calendar_content_type_triggers_invite_signal() -> None:
    raw = (
        "From: organizer@acme.com\r\n"
        "Content-Type: text/calendar; method=REQUEST\r\n"
        "Subject: Meeting Tuesday\r\n"
        "\r\n"
        "BEGIN:VCALENDAR\r\n"
    )
    env = run_classify({"message": {"raw": raw}})
    assert env["result"]["signals"]["is_invite"] is True


def test_ics_filename_in_multipart_triggers_invite() -> None:
    raw = (
        "From: organizer@acme.com\r\n"
        "Subject: Meeting\r\n"
        'Content-Type: multipart/mixed; boundary="bnd"\r\n'
        "\r\n"
        "--bnd\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "Plain body\r\n"
        "--bnd\r\n"
        'Content-Type: application/ics; name="invite.ics"\r\n\r\n'
        "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        "--bnd--\r\n"
    )
    env = run_classify({"message": {"raw": raw}})
    assert env["result"]["signals"]["is_invite"] is True


# ---------- contact status ----------------------------------------------


def test_unknown_sender_is_marked_unknown() -> None:
    raw = "From: stranger@nowhere.com\r\nSubject: x\r\n\r\n"
    env = run_classify({"message": {"raw": raw}, "contacts": {}})
    assert env["result"]["signals"]["contact_status"] == "unknown"


def test_self_sender_is_marked_self_when_in_user_domains() -> None:
    raw = "From: me@scalesology.com\r\nSubject: x\r\n\r\n"
    env = run_classify(
        {
            "message": {"raw": raw},
            "user_domains": ["scalesology.com"],
        }
    )
    assert env["result"]["signals"]["contact_status"] == "self"


# ---------- inputs --------------------------------------------------------


def test_accepts_pre_shaped_message_from_fetch_batch() -> None:
    """The agent can pass fetch-batch's per-message shape directly."""
    pre_shaped = {
        "headers": {
            "from": [{"name": "Sam", "addr": "sam@acme.com"}],
            "to": [{"name": "", "addr": "me@scalesology.com"}],
            "cc": [],
            "subject": "Q3",
            "list_id": None,
            "list_unsubscribe": {"urls": [], "mailtos": []},
            "list_unsubscribe_post_one_click": False,
            "importance": None,
            "in_reply_to": None,
            "references": [],
            "message_id": "<x@acme.com>",
        },
        "body_text": "Hi",
        "body_truncated": False,
    }
    env = run_classify(
        {
            "message": pre_shaped,
            "user_domains": ["scalesology.com"],
            "project_map": {"acme.com": "Acme"},
        }
    )
    assert env["status"] == "ok"
    assert env["result"]["matched_project"] == "Acme"


def test_missing_message_field_is_bad_request() -> None:
    env = run_classify({})
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"
