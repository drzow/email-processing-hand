"""Cheap signal extraction for classification context.

Lives between `headers.classification_set` (raw header parse) and the
LLM classifier. Provides the binary/cheap signals the classifier prompt
relies on:

* ``is_invite``       — message carries an iCalendar invite (Content-Type
                         text/calendar or an .ics part).
* ``is_mass_mail``    — RFC 2369 List-* headers present.
* ``urgency_flags``   — server-set flags that *might* indicate urgency.
                         These are noisy (every sales email sets
                         ``Importance: high``) — the classifier prompt
                         is responsible for actually down-weighting them
                         in favor of content/tone.
* ``vip_match``       — sender address matches the user's VIP list.
* ``contact_status``  — known / unknown / self based on the contact
                         table and user_domains.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from lib.headers import domain_of

_URGENT_WORDS = ("urgent", "asap", "emergency", "critical", "immediately")
_INVITE_CT = re.compile(r"text/calendar", re.IGNORECASE)
_ICS_FILENAME = re.compile(r"\.ics\b", re.IGNORECASE)
_APPLICATION_ICS_CT = re.compile(r"application/ics", re.IGNORECASE)


def is_invite(raw_message: str) -> bool:
    """Detect calendar invite by Content-Type or an .ics-named part."""
    if not raw_message:
        return False
    if _INVITE_CT.search(raw_message):
        return True
    if _APPLICATION_ICS_CT.search(raw_message):
        return True
    if _ICS_FILENAME.search(raw_message):
        return True
    return False


def is_mass_mail(headers: dict[str, Any]) -> bool:
    """RFC 2369 cues: a List-Id or any List-Unsubscribe destination."""
    if headers.get("list_id"):
        return True
    lu = headers.get("list_unsubscribe") or {}
    if lu.get("urls") or lu.get("mailtos"):
        return True
    return False


def urgency_flags(headers: dict[str, Any]) -> list[str]:
    """Server-set urgency cues — the LLM is the real judge of urgency."""
    out: list[str] = []
    imp = (headers.get("importance") or "").lower().strip()
    if imp == "high":
        out.append("importance:high")
    subject = (headers.get("subject") or "").lower()
    for word in _URGENT_WORDS:
        if word in subject:
            out.append(f"subject:{word}")
            break
    return out


def vip_match(headers: dict[str, Any], vips: Iterable[str]) -> str | None:
    """Return the canonical lowercased VIP address that matches, or None."""
    vip_set = {v.strip().lower() for v in vips if v and v.strip()}
    if not vip_set:
        return None
    senders = headers.get("from") or []
    for s in senders:
        addr = (s.get("addr") or "").lower()
        if addr in vip_set:
            return addr
    return None


def contact_status(
    headers: dict[str, Any],
    *,
    contacts: dict[str, Any],
    user_domains: Iterable[str],
) -> str:
    """Classify the sender as 'self', 'known', or 'unknown'."""
    user_set = {d.lower() for d in user_domains if d}
    senders = headers.get("from") or []
    if not senders:
        return "unknown"
    primary = senders[0].get("addr", "").lower()
    if not primary:
        return "unknown"
    if domain_of(primary) in user_set:
        return "self"
    if primary in contacts:
        return "known"
    return "unknown"


def all_signals(
    *,
    raw_message: str | None,
    headers: dict[str, Any],
    vips: Iterable[str],
    contacts: dict[str, Any],
    user_domains: Iterable[str],
) -> dict[str, Any]:
    """Bundle every cheap signal into one dict for the agent."""
    return {
        "is_invite": is_invite(raw_message or ""),
        "is_mass_mail": is_mass_mail(headers),
        "urgency_flags": urgency_flags(headers),
        "vip_match": vip_match(headers, vips),
        "contact_status": contact_status(
            headers, contacts=contacts, user_domains=user_domains
        ),
    }
