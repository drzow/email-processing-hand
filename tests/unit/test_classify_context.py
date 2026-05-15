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


# ---------- rustymail flat-entry shape ----------------------------------


def test_accepts_rustymail_get_email_by_uid_flat_shape() -> None:
    """Real rustymail's get_email_by_uid returns a flat dict with
    from_address / from_name / to_addresses[] / cc_addresses[] /
    subject / date / message_id / body_text / body_html — no `raw`
    field, no nested `headers` dict. classify-context must accept it
    by synthesizing the standard headers shape."""
    rustymail_entry = {
        "uid": 18092,
        "folder_id": 1,
        "from_address": "notifications@github.com",
        "from_name": "Jason-Lu-Scalesology",
        "to_addresses": ["insights2action@noreply.github.com"],
        "cc_addresses": ["subscribed@noreply.github.com"],
        "subject": "Re: [Scalesology/insights2action] PR merged",
        "date": "2026-05-14T21:41:14Z",
        "message_id": "<scalesology/event/25549621628@github.com>",
        "in_reply_to": "<scalesology/pr/656@github.com>",
        "references_header": " <scalesology/pr/656@github.com>\r\n",
        "body_text": "Merged #656.\r\n\r\nUnsubscribe at github.com/unsub",
        "body_html": "<p>Merged #656.</p>",
        "size": 11774,
    }
    env = run_classify(
        {
            "message": rustymail_entry,
            "user_domains": ["scalesology.com", "bruggerink.com"],
        }
    )
    assert env["status"] == "ok", env
    r = env["result"]
    # Headers were synthesized correctly.
    assert r["headers"]["from"] == [
        {"name": "Jason-Lu-Scalesology", "addr": "notifications@github.com"}
    ]
    assert r["headers"]["subject"] == "Re: [Scalesology/insights2action] PR merged"
    assert r["headers"]["message_id"].startswith("<")
    assert r["headers"]["in_reply_to"].startswith("<")
    # to_addresses → headers.to (list of {name, addr})
    assert len(r["headers"]["to"]) == 1
    assert r["headers"]["to"][0]["addr"] == "insights2action@noreply.github.com"
    # cc_addresses similarly
    assert len(r["headers"]["cc"]) == 1
    # body_text passed through
    assert "Merged #656" in r["body_text"]


def test_signals_work_on_synthesized_headers() -> None:
    """The signals layer downstream of header synthesis should produce
    sensible values for a flat-shape rustymail entry."""
    entry = {
        "uid": 1,
        "from_address": "marketing@vendor.com",
        "from_name": "Vendor Marketing",
        "to_addresses": ["alice@scalesology.com"],
        "subject": "URGENT: limited time deal",
        "date": "2026-05-14T10:00:00Z",
        "body_text": "Buy now! Click here to unsubscribe.",
        "body_html": '<p>Buy</p><a href="x">Unsubscribe</a>',
    }
    env = run_classify(
        {
            "message": entry,
            "user_domains": ["scalesology.com"],
        }
    )
    sigs = env["result"]["signals"]
    # subject-keyword urgency
    assert any("urgent" in f for f in sigs["urgency_flags"])
    # contact_status — sender not in contacts → "unknown"
    assert sigs["contact_status"] == "unknown"


def test_headers_passed_through_when_already_well_shaped() -> None:
    """When the caller passes message.headers as a proper dict, skip
    synthesis (don't overwrite it)."""
    pre = {
        "headers": {
            "from": [{"name": "Sam", "addr": "sam@acme.com"}],
            "to": [{"name": "", "addr": "me@scalesology.com"}],
            "cc": [],
            "subject": "Q3",
            "list_id": None,
            "list_unsubscribe": {"urls": [], "mailtos": []},
            "list_unsubscribe_post_one_click": False,
            "in_reply_to": None,
            "references": [],
            "importance": None,
            "message_id": "<x@acme.com>",
        },
        "body_text": "Hi",
    }
    env = run_classify(
        {
            "message": pre,
            "user_domains": ["scalesology.com"],
            "project_map": {"acme.com": "Acme"},
        }
    )
    assert env["status"] == "ok"
    assert env["result"]["matched_project"] == "Acme"


def test_headers_with_string_from_falls_back_to_synthesis_from_flat() -> None:
    """Defense: if someone passes message={headers: {from: 'Sam <sam@acme.com>', ...}}
    (i.e., raw-RFC-2822 strings rather than parsed [{name, addr}] lists),
    fall back to treating the message as a flat-shape entry. Don't
    crash."""
    env = run_classify(
        {
            "message": {
                "headers": {"from": "Sam Long <sam@acme.com>"},
                "from_address": "sam@acme.com",
                "from_name": "Sam Long",
                "to_addresses": ["me@scalesology.com"],
                "cc_addresses": [],
                "subject": "Hi",
                "date": "2026-05-14T10:00:00Z",
                "body_text": "hi",
            },
            "user_domains": ["scalesology.com"],
        }
    )
    assert env["status"] == "ok"
    assert env["result"]["headers"]["from"][0]["addr"] == "sam@acme.com"
