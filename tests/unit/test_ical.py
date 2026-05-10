"""Unit tests for tools/lib/ical.py and the parse-ical subcommand."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))
SCAN_PY = REPO_ROOT / "tools" / "scan.py"

from lib import ical  # noqa: E402


SAMPLE_VEVENT = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Acme//Sieve test//EN
METHOD:REQUEST
BEGIN:VEVENT
UID:abc123@acme.com
DTSTAMP:20260401T120000Z
DTSTART:20260501T140000Z
DTEND:20260501T150000Z
SUMMARY:Q3 planning sync
ORGANIZER;CN=Sam Long:mailto:sam@acme.com
ATTENDEE;CN=Alice;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:mailto:alice@scalesology.com
ATTENDEE;PARTSTAT=ACCEPTED;ROLE=OPT-PARTICIPANT:mailto:bob@scalesology.com
STATUS:CONFIRMED
SEQUENCE:0
END:VEVENT
END:VCALENDAR
"""

LINE_FOLDED = """\
BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
UID:long-uid@example.com
SUMMARY:A summary that wraps
  across multiple lines
DTSTART:20260501T140000Z
END:VEVENT
END:VCALENDAR
"""

TWO_EVENTS = """\
BEGIN:VCALENDAR
METHOD:CANCEL
BEGIN:VEVENT
UID:e1@example.com
DTSTART:20260101T140000Z
SUMMARY:Event one
END:VEVENT
BEGIN:VEVENT
UID:e2@example.com
DTSTART:20260501T140000Z
SUMMARY:Event two
END:VEVENT
END:VCALENDAR
"""

ALL_DAY = """\
BEGIN:VCALENDAR
BEGIN:VEVENT
UID:ad@example.com
DTSTART;VALUE=DATE:20260601
DTEND;VALUE=DATE:20260602
SUMMARY:All-day workshop
END:VEVENT
END:VCALENDAR
"""


# ---------- lib.ical -----------------------------------------------------


def test_parses_method_and_one_event() -> None:
    out = ical.parse(SAMPLE_VEVENT)
    assert len(out["events"]) == 1
    e = out["events"][0]
    assert e["method"] == "REQUEST"
    assert e["uid"] == "abc123@acme.com"
    assert e["summary"] == "Q3 planning sync"
    assert e["dtstart"] == "2026-05-01T14:00:00Z"
    assert e["dtend"] == "2026-05-01T15:00:00Z"
    assert e["status"] == "CONFIRMED"
    assert e["sequence"] == 0


def test_parses_organizer_and_attendees() -> None:
    out = ical.parse(SAMPLE_VEVENT)
    e = out["events"][0]
    assert e["organizer"] == {"name": "Sam Long", "addr": "sam@acme.com"}
    assert e["attendees"] == [
        {
            "name": "Alice",
            "addr": "alice@scalesology.com",
            "partstat": "NEEDS-ACTION",
            "role": "REQ-PARTICIPANT",
        },
        {
            "name": "",
            "addr": "bob@scalesology.com",
            "partstat": "ACCEPTED",
            "role": "OPT-PARTICIPANT",
        },
    ]


def test_unfolds_continuation_lines() -> None:
    out = ical.parse(LINE_FOLDED)
    assert out["events"][0]["summary"] == "A summary that wraps across multiple lines"


def test_multiple_events_max_event_date_picks_latest() -> None:
    out = ical.parse(TWO_EVENTS)
    assert len(out["events"]) == 2
    assert out["max_event_date"] == "2026-05-01T14:00:00Z"


def test_all_day_date_values_parse_to_iso_date() -> None:
    out = ical.parse(ALL_DAY)
    e = out["events"][0]
    assert e["dtstart"] == "2026-06-01"
    assert e["dtend"] == "2026-06-02"


def test_empty_or_no_events_returns_clean_empty_shape() -> None:
    assert ical.parse("") == {"events": [], "max_event_date": None}
    assert ical.parse("garbage with no VCALENDAR") == {"events": [], "max_event_date": None}


# ---------- scan.py parse-ical subcommand --------------------------------


def run_parse_ical(request: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "parse-ical"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout
    return json.loads(proc.stdout.splitlines()[-1])


def test_parse_ical_subcommand_returns_events() -> None:
    env = run_parse_ical({"ical_text": SAMPLE_VEVENT})
    assert env["status"] == "ok"
    assert env["result"]["events"][0]["summary"] == "Q3 planning sync"
    assert env["result"]["max_event_date"] == "2026-05-01T15:00:00Z"


def test_parse_ical_subcommand_requires_text() -> None:
    env = run_parse_ical({})
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"
