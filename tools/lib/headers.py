"""RFC 5322 / 2047 / 8058 header parsing helpers for the sidecar.

The sidecar always exposes the same canonical header set per
`docs/design.md` §6: From / To / Cc / Subject / Date / List-ID /
List-Unsubscribe / In-Reply-To / References / Importance /
X-OpenFang-Digest-ID. Callers consume the dict produced by
:func:`classification_set`.

Pure-stdlib implementation — no third-party deps.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import re
from typing import Any


# ---------- subject + display-name decoding (RFC 2047) -------------------


def decode_subject(value: str | None) -> str:
    """Decode possibly RFC-2047-encoded text into a plain Python str.

    Accepts ``None`` (returns ``""``) so callers can feed the result of
    ``msg.get("Subject")`` directly without a guard.
    """
    if not value:
        return ""
    try:
        chunks = email.header.decode_header(value)
    except (UnicodeDecodeError, LookupError):
        return value
    out: list[str] = []
    for chunk, charset in chunks:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


# ---------- address-list parsing -----------------------------------------


def parse_address_list(value: str | None) -> list[dict[str, str]]:
    """Split a To/From/Cc/Reply-To header into ``[{name, addr}]``.

    Display names are RFC-2047-decoded; addresses are lowercased so
    domain-equality comparisons in :mod:`lib.domain` are case-insensitive.
    Malformed addresses are skipped silently.
    """
    if not value:
        return []
    parsed = email.utils.getaddresses([value])
    out: list[dict[str, str]] = []
    for raw_name, raw_addr in parsed:
        if not raw_addr or "@" not in raw_addr:
            continue
        out.append(
            {
                "name": decode_subject(raw_name).strip(),
                "addr": raw_addr.lower(),
            }
        )
    return out


def domain_of(addr: str | None) -> str:
    """Return the lowercased domain of an email address, or ``""``."""
    if not addr or "@" not in addr:
        return ""
    return addr.split("@", 1)[1].lower().strip()


# ---------- List-Unsubscribe (RFC 2369) and -Post (RFC 8058) -------------


_URI_RE = re.compile(r"<([^>]+)>")


def parse_list_unsubscribe(value: str | None) -> dict[str, list[str]]:
    """Split a List-Unsubscribe header into ``{urls, mailtos}``.

    Both halves are returned as plain lists so callers can pick whichever
    they support. mailto: URIs land in ``mailtos`` (without the prefix);
    everything else lands in ``urls`` verbatim.
    """
    out: dict[str, list[str]] = {"urls": [], "mailtos": []}
    if not value:
        return out
    for match in _URI_RE.finditer(value):
        uri = match.group(1).strip()
        if uri.lower().startswith("mailto:"):
            out["mailtos"].append(uri[7:])
        else:
            out["urls"].append(uri)
    return out


def is_one_click_unsubscribe(post_value: str | None) -> bool:
    """RFC 8058 §3.2: header value is exactly ``List-Unsubscribe=One-Click``."""
    if not post_value:
        return False
    return post_value.strip().lower() == "list-unsubscribe=one-click"


# ---------- canonical header set for the classifier ----------------------


_REFERENCES_RE = re.compile(r"<[^>]+>")


def classification_set(raw_message: str) -> dict[str, Any]:
    """Parse the canonical header set used everywhere by the classifier.

    `raw_message` may be a full RFC 5322 message (headers + blank line +
    body) or a header-only block — only the headers are inspected.
    """
    msg = email.message_from_string(raw_message)

    importance = msg.get("Importance")
    in_reply_to = msg.get("In-Reply-To")
    return {
        "from": parse_address_list(msg.get("From")),
        "to": parse_address_list(msg.get("To")),
        "cc": parse_address_list(msg.get("Cc")),
        "subject": decode_subject(msg.get("Subject")),
        "date": msg.get("Date"),
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "list_id": _strip_brackets(msg.get("List-Id") or msg.get("List-ID")),
        "list_unsubscribe": parse_list_unsubscribe(msg.get("List-Unsubscribe")),
        "list_unsubscribe_post_one_click": is_one_click_unsubscribe(
            msg.get("List-Unsubscribe-Post")
        ),
        "in_reply_to": (in_reply_to or "").strip() or None,
        "references": _REFERENCES_RE.findall(msg.get("References") or ""),
        "importance": importance.lower() if importance else None,
        "x_openfang_digest_id": msg.get("X-OpenFang-Digest-ID"),
    }


def _strip_brackets(value: str | None) -> str | None:
    """List-Id values are usually wrapped in ``<...>`` — trim them."""
    if not value:
        return None
    v = value.strip()
    m = re.search(r"<([^>]+)>", v)
    return m.group(1) if m else v
