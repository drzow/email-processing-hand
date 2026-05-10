"""Minimal RFC 5545 iCalendar parser — VEVENT fields only.

Hand-rolled to avoid a third-party dep for the few properties the
classifier needs: METHOD, UID, SUMMARY, ORGANIZER, ATTENDEE, DTSTART,
DTEND, STATUS, SEQUENCE.

iCalendar's line-folding rule: a continuation line begins with a single
space or tab. We unfold those before splitting on ``:``. We deliberately
do NOT support iana-token escaping for VEVENT bodies — the data we care
about doesn't use it in the wild.
"""

from __future__ import annotations

import re
from typing import Any


_DT_RE = re.compile(
    r"^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})"
    r"(?:T(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?P<utc>Z)?)?$"
)


def _unfold(body: str) -> list[str]:
    """RFC 5545 §3.1: a CRLF + WSP starts a continuation line."""
    lines: list[str] = []
    for raw in body.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    """Split one iCalendar line into ``(name, params, value)``."""
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for raw_param in parts[1:]:
        if "=" not in raw_param:
            continue
        k, v = raw_param.split("=", 1)
        params[k.strip().upper()] = v.strip().strip('"')
    return name, params, value


def _parse_dt(value: str) -> str | None:
    """Convert a DATE-TIME / DATE value into an ISO-8601 string. Returns
    ``None`` if the value doesn't look like either format."""
    if not value:
        return None
    m = _DT_RE.match(value.strip())
    if not m:
        return None
    parts = m.groupdict()
    if parts["hour"] is None:
        return f"{parts['year']}-{parts['month']}-{parts['day']}"
    iso = (
        f"{parts['year']}-{parts['month']}-{parts['day']}"
        f"T{parts['hour']}:{parts['minute']}:{parts['second']}"
    )
    if parts["utc"]:
        iso += "Z"
    return iso


def parse(text: str) -> dict[str, Any]:
    """Parse a VCALENDAR blob; return ``{events: [...], max_event_date: str?}``."""
    if not text:
        return {"events": [], "max_event_date": None}

    lines = _unfold(text)
    method: str | None = None
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    attendees: list[dict[str, str]] = []

    for line in lines:
        prop = _split_property(line)
        if prop is None:
            continue
        name, params, value = prop

        if name == "METHOD":
            method = value.strip().upper()
            continue
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            current = {
                "uid": None,
                "summary": None,
                "organizer": None,
                "attendees": [],
                "dtstart": None,
                "dtend": None,
                "status": None,
                "sequence": None,
                "method": method,
            }
            attendees = []
            continue
        if name == "END" and value.strip().upper() == "VEVENT":
            if current is not None:
                current["attendees"] = attendees
                events.append(current)
                current = None
                attendees = []
            continue
        if current is None:
            continue

        if name == "UID":
            current["uid"] = value.strip()
        elif name == "SUMMARY":
            current["summary"] = value
        elif name == "ORGANIZER":
            current["organizer"] = _addr_from_uri(value, params)
        elif name == "ATTENDEE":
            attendees.append(_attendee_from(value, params))
        elif name == "DTSTART":
            current["dtstart"] = _parse_dt(value)
        elif name == "DTEND":
            current["dtend"] = _parse_dt(value)
        elif name == "STATUS":
            current["status"] = value.strip().upper()
        elif name == "SEQUENCE":
            try:
                current["sequence"] = int(value.strip())
            except ValueError:
                pass

    max_date: str | None = None
    for e in events:
        dt = e.get("dtend") or e.get("dtstart")
        if dt and (max_date is None or dt > max_date):
            max_date = dt
    return {"events": events, "max_event_date": max_date}


def _addr_from_uri(value: str, params: dict[str, str]) -> dict[str, str]:
    addr = value.strip()
    if addr.lower().startswith("mailto:"):
        addr = addr[7:]
    return {"name": params.get("CN", ""), "addr": addr.lower()}


def _attendee_from(value: str, params: dict[str, str]) -> dict[str, str]:
    addr = _addr_from_uri(value, params)
    addr["partstat"] = params.get("PARTSTAT", "")
    addr["role"] = params.get("ROLE", "")
    return addr
