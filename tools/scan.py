#!/usr/bin/env python3
"""Sidecar entry point for the email-processing-hand.

Skeleton dispatcher — full subcommand surface lands in upcoming slices
(see docs/design.md §6 for the design). For now only ``noop`` is wired
up so the agent can verify the sidecar plumbing without doing real I/O.

Usage:

    python3 tools/scan.py <subcommand> [--input -]

Reads request JSON from stdin, writes response JSON to stdout. Exit 0
on success, non-zero on a sidecar-internal error (HAND ALWAYS reads
status from the JSON envelope, not from the exit code, since some
subcommands report logical errors with exit 0).
"""

from __future__ import annotations

import argparse
import re
import json
import sys
import time
import uuid
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Response envelope helpers — stable contract with the agent.
# ---------------------------------------------------------------------------


def _envelope(
    subcommand: str,
    request_id: str,
    started_at: float,
    *,
    result: Any = None,
    error: dict[str, str] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return {
        "status": "ok" if error is None else "error",
        "subcommand": subcommand,
        "request_id": request_id,
        "elapsed_ms": elapsed_ms,
        "result": result,
        "error": error,
        "metrics": metrics or {},
    }


def _emit(envelope: dict[str, Any]) -> None:
    json.dump(envelope, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Subcommand registry. Each handler takes the parsed request dict and
# returns a (result, metrics) tuple — or raises SidecarError for a
# structured error response.
# ---------------------------------------------------------------------------


class SidecarError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


Handler = Callable[[dict[str, Any]], tuple[Any, dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}


def register(name: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn

    return deco


# ---------------------------------------------------------------------------
# noop — sanity check the sidecar plumbing without touching MCP.
# ---------------------------------------------------------------------------


@register("noop")
def _noop(_request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    return (
        {"message": "sidecar reachable; skeleton mode"},
        {"messages_scanned": 0, "mcp_calls": 0, "result_bytes": 0},
    )


# ---------------------------------------------------------------------------
# resolve-domain — project-resolution by domain ranking. Pure logic, no
# MCP, no LLM. Powers Phase 1 of the per-message classification pipeline.
# ---------------------------------------------------------------------------


@register("resolve-domain")
def _resolve_domain(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    # Late import so `noop` doesn't pay for it on every invocation.
    from lib.domain import resolve

    try:
        result = resolve(
            from_=request.get("from", []),
            to=request.get("to", []),
            cc=request.get("cc", []),
            user_domains=request.get("user_domains", []),
            exclude_domains=request.get("exclude_domains", []),
            project_map=request.get("project_map", {}),
        )
    except (TypeError, KeyError) as e:
        raise SidecarError("bad_request", f"malformed input: {e}") from e

    return (
        result,
        {
            "messages_scanned": 0,
            "mcp_calls": 0,
            "result_bytes": 0,
            "domains_ranked": len(result["ranked_domains"]),
        },
    )


# ---------------------------------------------------------------------------
# classify-context — bundle everything the LLM classifier needs about one
# message: parsed headers, body, project resolution, cheap signals.
# ---------------------------------------------------------------------------


@register("classify-context")
def _classify_context(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    from lib.domain import resolve
    from lib.headers import classification_set
    from lib.signals import all_signals

    msg = request.get("message")
    if not msg:
        raise SidecarError("bad_request", "message field is required")

    # Accept either a raw RFC 5322 string OR a pre-shaped fetch-batch entry.
    raw = msg.get("raw") or ""
    if "headers" in msg and msg["headers"]:
        headers = msg["headers"]
        body_text = msg.get("body_text", "")
    else:
        if not raw:
            raise SidecarError(
                "bad_request", "message needs either 'raw' or 'headers'"
            )
        headers = classification_set(raw)
        body_text = _split_body(raw)

    ranking = resolve(
        from_=headers.get("from", []),
        to=headers.get("to", []),
        cc=headers.get("cc", []),
        user_domains=request.get("user_domains", []),
        exclude_domains=request.get("exclude_domains", []),
        project_map=request.get("project_map", {}),
    )
    signals = all_signals(
        raw_message=raw,
        headers=headers,
        vips=request.get("vip_senders", []),
        contacts=request.get("contacts", {}),
        user_domains=request.get("user_domains", []),
    )

    result = {
        "headers": headers,
        "body_text": body_text,
        "matched_project": ranking["matched_project"],
        "ranked_domains": ranking["ranked_domains"],
        "decision_trace": ranking["decision_trace"],
        "signals": signals,
    }
    return (
        result,
        {
            "messages_scanned": 1,
            "mcp_calls": 0,
            "result_bytes": len(body_text),
        },
    )


# ---------------------------------------------------------------------------
# fetch-batch — fetch + parse + render a batch of messages by uid.
# v1 supports selector.kind == "uids" only. The agent passes the
# rustymail / ms365 mcp_server config in the request so this slice
# doesn't need to ship a static account-to-server mapping.
# ---------------------------------------------------------------------------


@register("fetch-batch")
def _fetch_batch(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    # Lazy imports keep the noop / resolve-domain hot paths cheap.
    from lib.mcp_client import McpClient, McpClientError

    server_cfg = request.get("mcp_server")
    if not server_cfg or "command" not in server_cfg:
        raise SidecarError("bad_request", "mcp_server.command is required")

    selector = request.get("selector") or {}
    if selector.get("kind") != "uids":
        raise SidecarError(
            "not_implemented",
            f"selector.kind {selector.get('kind')!r} not supported in v1; "
            "only 'uids' is wired",
        )
    uids = selector.get("uids") or []
    folder = selector.get("folder", "INBOX")
    max_messages = int(request.get("max_messages", 50))
    max_body_chars = int(request.get("max_body_chars", 4000))
    fetch_tool = request.get("fetch_tool", "get_email_by_uid")

    uids = [int(u) for u in uids][:max_messages]

    messages: list[dict[str, Any]] = []
    mcp_calls = 0
    fetch_errors: list[dict[str, Any]] = []
    client = McpClient(
        command=server_cfg["command"],
        env=server_cfg.get("env"),
    )
    try:
        client.open()
        for uid in uids:
            mcp_calls += 1
            try:
                tool_result = client.call_tool(fetch_tool, {"folder": folder, "uid": uid})
            except McpClientError as e:
                fetch_errors.append({"uid": uid, "error": str(e)})
                continue
            payload = _extract_payload(tool_result)
            messages.append(_shape_message(uid, folder, payload, max_body_chars))
    finally:
        client.close()

    result_bytes = sum(len(m.get("body_text", "")) for m in messages)
    return (
        {
            "messages": messages,
            "scan_summary": {
                "uids_requested": len(uids),
                "messages_returned": len(messages),
                "errors": fetch_errors,
                "folder": folder,
                "tool": fetch_tool,
            },
        },
        {
            "messages_scanned": len(messages),
            "mcp_calls": mcp_calls,
            "result_bytes": result_bytes,
        },
    )


# ---------------------------------------------------------------------------
# Message-shape normalizers. Real rustymail returns different field
# names depending on which list/search tool you call:
#
#   list_cached_emails / search_cached_emails / search_by_domain →
#       from_address (str), from_name (str), to_addresses ([str]),
#       cc_addresses ([str]), size (int)
#
#   raw RFC 5322 parsing in lib.headers → "from", "to", "cc"
#       (each "Name <addr>, ..." strings) and "size_bytes" set by
#       fetch-batch from get_email_by_uid's payload.
#
# These helpers normalize both shapes so the per-sender / per-recipient
# code doesn't care which list tool fed it.
# ---------------------------------------------------------------------------


def _entry_from_addrs(entry: dict[str, Any], parse_address_list: Any) -> list[dict[str, str]]:
    """Return [{name, addr}, ...] for the From of a message entry."""
    raw_from = entry.get("from") or ""
    if raw_from:
        return parse_address_list(raw_from)
    fa = entry.get("from_address") or ""
    fn = entry.get("from_name") or ""
    if not fa or "@" not in fa:
        return []
    return [{"addr": fa.lower(), "name": fn or ""}]


def _entry_addr_list(
    entry: dict[str, Any], field: str, parse_address_list: Any
) -> list[dict[str, str]]:
    """Return [{name, addr}, ...] for to/cc/etc. on a message entry.

    Honors the raw form (``entry["to"]`` as a comma-joined string) AND
    the cached form (``entry["to_addresses"]`` as a ``[str]``).
    """
    val = entry.get(field)
    if val:
        if isinstance(val, list):
            return [
                {"addr": a.lower(), "name": ""}
                for a in val
                if isinstance(a, str) and "@" in a
            ]
        return parse_address_list(val)
    val = entry.get(f"{field}_addresses")
    if isinstance(val, list):
        return [
            {"addr": a.lower(), "name": ""}
            for a in val
            if isinstance(a, str) and "@" in a
        ]
    return []


def _entry_size(entry: dict[str, Any]) -> int:
    """Message size in bytes, falling back across shape names."""
    return int(entry.get("size_bytes") or entry.get("size") or 0)


def _entry_from_display(entry: dict[str, Any]) -> str:
    """Render the From: as a single ``"Name <addr>"`` (or addr-only) string
    for samples / display rows. Empty string if unset."""
    f = entry.get("from")
    if f:
        return f
    fa = entry.get("from_address") or ""
    fn = entry.get("from_name") or ""
    if fa and fn:
        return f"{fn} <{fa}>"
    return fa or ""


def _paginate_messages(
    client: Any,
    tool_name: str,
    base_args: dict[str, Any],
    *,
    page_size: int = 500,
    max_pages: int = 200,
    rate_limit_retries: int = 5,
) -> list[dict[str, Any]]:
    """Call ``tool_name`` repeatedly with rising ``offset`` until the page
    comes back smaller than ``page_size`` (or we've seen ``total``).

    Returns a single flat list of messages. Lets callers stay agnostic
    about how many pages were needed.

    ``max_pages`` is a defense in case a buggy server returns full pages
    forever — we cap at 200 * page_size = 100k messages by default, which
    is well above any realistic single-folder count.

    ``rate_limit_retries`` is how many times we'll back off and retry
    when the server returns a ``rate_limit_exceeded`` style error. The
    sleep duration comes from the error's ``retry_after`` field when
    present, otherwise 5 seconds.
    """
    import time as _time

    from lib.mcp_client import McpClientError

    out: list[dict[str, Any]] = []
    offset = 0
    pages = 0
    seen_total: int | None = None
    while pages < max_pages:
        args = dict(base_args)
        args["limit"] = page_size
        args["offset"] = offset

        retries_left = rate_limit_retries
        while True:
            try:
                tool_result = client.call_tool(tool_name, args)
                break
            except McpClientError as e:
                msg = str(e).lower()
                if "rate_limit" in msg or "rate limit" in msg:
                    if retries_left <= 0:
                        raise
                    retries_left -= 1
                    # Parse retry_after from the error message if present;
                    # else default to 5s. Cap at 60s.
                    delay = 5
                    m = re.search(r"retry_after=(\d+)", str(e))
                    if not m:
                        m = re.search(r"retry after (\d+)", msg)
                    if m:
                        delay = min(int(m.group(1)) + 1, 60)
                    _time.sleep(delay)
                    continue
                raise

        payload = _extract_payload(tool_result)
        if isinstance(payload, dict):
            messages = payload.get("messages") or []
            if seen_total is None:
                seen_total = payload.get("total")
        elif isinstance(payload, list):
            messages = payload
        else:
            messages = []
        out.extend(messages)
        if len(messages) < page_size:
            break
        if seen_total is not None and len(out) >= seen_total:
            break
        offset += page_size
        pages += 1
    return out


def _extract_payload(tool_result: dict[str, Any]) -> dict[str, Any]:
    """Pull the payload dict out of an MCP tools/call result envelope.

    rustymail-style servers return ``{"content": [{"type": "text",
    "text": "<json>"}], "isError": false}``. The text is the actual
    JSON the tool produced.
    """
    content = tool_result.get("content") or []
    for chunk in content:
        if chunk.get("type") == "text":
            try:
                return json.loads(chunk.get("text", ""))
            except json.JSONDecodeError:
                return {"raw_text": chunk.get("text", "")}
    return {}


def _shape_message(
    uid: int, folder: str, payload: dict[str, Any], max_body_chars: int
) -> dict[str, Any]:
    """Translate one server payload into the canonical fetch-batch entry."""
    from lib.body import render
    from lib.headers import classification_set

    raw = payload.get("raw") or payload.get("rfc822") or ""
    headers = classification_set(raw) if raw else {}
    body_part = render(
        raw_text=payload.get("body_text") or _split_body(raw),
        raw_html=payload.get("body_html"),
        max_chars=max_body_chars,
    )
    return {
        "uid": uid,
        "folder": payload.get("folder", folder),
        "message_id": headers.get("message_id"),
        "headers": headers,
        "body_text": body_part["body_text"],
        "body_truncated": body_part["body_truncated"],
        "body_source": body_part["source"],
        "has_attachments": bool(payload.get("has_attachments")),
        "size_bytes": int(payload.get("size_bytes") or len(raw)),
    }


# ---------------------------------------------------------------------------
# parse-thread — fetch a thread and decide which messages to display
# in full vs. elide to a summary line. Threads over `max_displayed`
# entries keep the newest N as full messages; the older remainder is
# returned as (subject, from, date) tuples sorted oldest-first.
# ---------------------------------------------------------------------------


@register("parse-thread")
def _parse_thread(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    from lib.mcp_client import McpClient, McpClientError

    server_cfg = request.get("mcp_server")
    if not server_cfg or "command" not in server_cfg:
        raise SidecarError("bad_request", "mcp_server.command is required")
    thread_root_uid = request.get("thread_root_uid")
    if thread_root_uid is None:
        raise SidecarError("bad_request", "thread_root_uid is required")
    fetch_tool = request.get("fetch_tool", "get_thread")
    account_id = request.get("account_id")
    max_displayed = int(request.get("max_displayed", 5))

    client = McpClient(command=server_cfg["command"], env=server_cfg.get("env"))
    try:
        client.open()
        try:
            tool_result = client.call_tool(
                fetch_tool,
                {"account_id": account_id, "thread_root_uid": int(thread_root_uid)},
            )
        except McpClientError as e:
            raise SidecarError("mcp_error", str(e)) from e
    finally:
        client.close()

    payload = _extract_payload(tool_result)
    if not isinstance(payload, dict):
        raise SidecarError(
            "protocol_error",
            f"get_thread tool returned a non-object payload: {type(payload).__name__}",
        )
    messages = payload.get("messages") or []
    thread_id = payload.get("thread_id")

    # Newest-first ordering. Empty / missing dates sort to the end.
    sorted_msgs = sorted(
        messages,
        key=lambda m: (m.get("headers") or {}).get("date") or "",
        reverse=True,
    )
    displayed = sorted_msgs[:max_displayed]
    elided_raw = sorted_msgs[max_displayed:]

    # Elided entries sort oldest-first so the agent can render them as
    # a chronological summary above the displayed top.
    elided = [
        {
            "subject": (m.get("headers") or {}).get("subject", ""),
            "from": _first_from((m.get("headers") or {}).get("from") or []),
            "date": (m.get("headers") or {}).get("date", ""),
        }
        for m in sorted(
            elided_raw, key=lambda m: (m.get("headers") or {}).get("date") or ""
        )
    ]

    return (
        {
            "thread_id": thread_id,
            "displayed": displayed,
            "elided": elided,
            "elided_count": len(elided),
            "total_count": len(messages),
        },
        {
            "messages_scanned": len(messages),
            "mcp_calls": 1,
            "result_bytes": 0,
        },
    )


def _first_from(from_field: Any) -> dict[str, str]:
    """Coerce a from-field to a plain {name, addr} pair (or empty)."""
    if isinstance(from_field, list) and from_field:
        head = from_field[0]
        if isinstance(head, dict):
            return {"name": head.get("name", ""), "addr": head.get("addr", "")}
    if isinstance(from_field, str):
        return {"name": "", "addr": from_field}
    return {"name": "", "addr": ""}


# ---------------------------------------------------------------------------
# contacts-bootstrap — walk Sent folders across configured accounts and
# build the global contact list (`email.contacts`). For each address we
# observe in To/Cc, record display name (first observation wins),
# first_seen, last_seen, message_count, and the set of accounts whose
# Sent folder it appeared in. Used during first-run + for the "known
# sender" signal in classify-context.
# ---------------------------------------------------------------------------


@register("contacts-bootstrap")
def _contacts_bootstrap(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    from lib.headers import parse_address_list
    from lib.mcp_client import McpClient, McpClientError

    server_cfg = request.get("mcp_server")
    if not server_cfg or "command" not in server_cfg:
        raise SidecarError("bad_request", "mcp_server.command is required")
    accounts = request.get("accounts")
    if not accounts:
        raise SidecarError(
            "bad_request",
            "accounts list is required: [{account_id, sent_folder}, ...]",
        )
    list_tool = request.get("list_tool", "list_cached_emails")
    since_cutoff = request.get("since")  # ISO-8601 string or None

    contacts: dict[str, dict[str, Any]] = {}
    per_account: list[dict[str, Any]] = []
    total_scanned = 0
    mcp_calls = 0
    page_size = int(request.get("page_size", 500))

    client = McpClient(command=server_cfg["command"], env=server_cfg.get("env"))
    try:
        client.open()
        for acct in accounts:
            account_id = acct.get("account_id") or "?"
            sent_folder = acct.get("sent_folder") or "Sent"
            acct_summary = {
                "account_id": account_id,
                "sent_folder": sent_folder,
                "messages_scanned": 0,
                "addresses_found": 0,
                "error": None,
            }
            try:
                messages = _paginate_messages(
                    client,
                    list_tool,
                    {"folder": sent_folder, "account_id": account_id},
                    page_size=page_size,
                )
            except McpClientError as e:
                acct_summary["error"] = str(e)
                per_account.append(acct_summary)
                continue
            mcp_calls += max(1, (len(messages) + page_size - 1) // page_size)

            seen_in_account: set[str] = set()
            for msg in messages:
                date = msg.get("date") or ""
                if since_cutoff and date and date < since_cutoff:
                    continue
                acct_summary["messages_scanned"] += 1
                total_scanned += 1
                for field in ("to", "cc"):
                    for entry in _entry_addr_list(msg, field, parse_address_list):
                        addr = entry["addr"]
                        if not addr:
                            continue
                        seen_in_account.add(addr)
                        _upsert_contact(
                            contacts,
                            addr=addr,
                            name=entry["name"],
                            date=date,
                            account_id=account_id,
                        )
            acct_summary["addresses_found"] = len(seen_in_account)
            per_account.append(acct_summary)
    finally:
        client.close()

    return (
        {
            "version": 1,
            "contacts": contacts,
            "scan_summary": {
                "sent_messages_scanned": total_scanned,
                "addresses_found": len(contacts),
                "per_account": per_account,
            },
        },
        {
            "messages_scanned": total_scanned,
            "mcp_calls": mcp_calls,
            "result_bytes": 0,
            "unique_addresses": len(contacts),
        },
    )


def _upsert_contact(
    contacts: dict[str, dict[str, Any]],
    *,
    addr: str,
    name: str,
    date: str,
    account_id: str,
) -> None:
    entry = contacts.get(addr)
    if entry is None:
        entry = {
            "display_name": name,
            "first_seen": date or None,
            "last_seen": date or None,
            "message_count": 0,
            "accounts": [],
        }
        contacts[addr] = entry
    entry["message_count"] += 1
    # First-observation wins for display_name unless we never had one.
    if not entry["display_name"] and name:
        entry["display_name"] = name
    if date:
        if not entry["first_seen"] or date < entry["first_seen"]:
            entry["first_seen"] = date
        if not entry["last_seen"] or date > entry["last_seen"]:
            entry["last_seen"] = date
    if account_id not in entry["accounts"]:
        entry["accounts"].append(account_id)


# ---------------------------------------------------------------------------
# submit-unsubscribe — RFC 8058 List-Unsubscribe-Post + GET fallback.
# For mailto: targets, we return the to/subject/body for the agent to
# send via the email MCP server (we don't ship SMTP plumbing here).
# ---------------------------------------------------------------------------


_UNSUB_KEYWORDS = ("unsubscribe", "opt-out", "opt out", "preferences", "manage subscription", "remove me")
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*"([^"]+)"[^>]*>([^<]*)</a>',
    flags=re.IGNORECASE | re.DOTALL,
)


@register("submit-unsubscribe")
def _submit_unsubscribe(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    url = request.get("list_unsubscribe_url")
    mailto = request.get("list_unsubscribe_mailto")
    body_html = request.get("body_html")
    body_text = request.get("body_text")
    from_domain = request.get("from_domain")
    fall_back_to_get = bool(request.get("fall_back_to_get", False))
    timeout_secs = float(request.get("timeout_secs", 15.0))

    # ---- Priority 1: an explicit List-Unsubscribe-Post URL (RFC 8058) -----
    if url:
        if not url.lower().startswith(("http://", "https://")):
            raise SidecarError(
                "bad_request",
                f"unsupported url scheme; expected http(s), got {url!r}",
            )
        post_value = request.get("list_unsubscribe_post")
        result = _try_url(url, post_value, fall_back_to_get, timeout_secs)
        return (
            result,
            {"messages_scanned": 0, "mcp_calls": 0, "result_bytes": 0},
        )

    # ---- Priority 2: explicit List-Unsubscribe mailto: -------------------
    if mailto:
        return (
            _mailto_envelope(
                mailto,
                request.get("list_unsubscribe_mailto_subject", "unsubscribe"),
                request.get("list_unsubscribe_mailto_body", ""),
                source="list_unsubscribe_header",
            ),
            {"messages_scanned": 0, "mcp_calls": 0, "result_bytes": 0},
        )

    # ---- Priority 3: scrape body for unsubscribe-like links --------------
    candidates: list[str] = _scrape_unsubscribe_urls(body_html, body_text)
    if candidates:
        last_result: dict[str, Any] | None = None
        for candidate_url in candidates:
            last_result = _try_url(
                candidate_url, post_value=None,
                fall_back_to_get=fall_back_to_get,
                timeout_secs=timeout_secs,
            )
            last_result["scraped_from"] = "body_html"
            last_result["scraped_candidates"] = candidates
            if last_result["status"] == "submitted":
                break
        assert last_result is not None
        return (
            last_result,
            {"messages_scanned": 0, "mcp_calls": 0, "result_bytes": 0},
        )

    # ---- Priority 4: mailto fallback constructed from sender domain ------
    if from_domain:
        envelope = _mailto_envelope(
            f"unsubscribe@{from_domain}",
            "unsubscribe",
            "",
            source="from_domain_fallback",
        )
        return (
            envelope,
            {"messages_scanned": 0, "mcp_calls": 0, "result_bytes": 0},
        )

    raise SidecarError(
        "bad_request",
        "no unsubscribe pathway found: provide list_unsubscribe_url, "
        "list_unsubscribe_mailto, body_html with an unsubscribe link, "
        "or from_domain for a mailto:unsubscribe@<domain> fallback",
    )


def _try_url(
    url: str,
    post_value: str | None,
    fall_back_to_get: bool,
    timeout_secs: float,
) -> dict[str, Any]:
    """Attempt POST (if post_value) then GET (if allowed); return result envelope."""
    attempted: list[str] = []
    if post_value:
        attempted.append("post")
        code = _http_post(url, post_value, timeout_secs)
        if 200 <= code < 300:
            return _result_envelope("submitted", attempted, code, url)
        if not fall_back_to_get:
            return _result_envelope("failed", attempted, code, url)
    attempted.append("get")
    code = _http_get(url, timeout_secs)
    status = "submitted" if 200 <= code < 300 else "failed"
    return _result_envelope(status, attempted, code, url)


def _scrape_unsubscribe_urls(html: str | None, text: str | None) -> list[str]:
    """Extract unsubscribe-link candidates from a message body.

    Looks at every ``<a href="...">label</a>`` in the HTML body. A link
    is a candidate if EITHER the URL OR the anchor text contains an
    unsubscribe keyword (case-insensitive). Plain-text bodies are
    scanned for bare URLs adjacent to an unsubscribe keyword.

    Returns matches in source order, deduplicated.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _record(u: str) -> None:
        if u.lower().startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            found.append(u)

    if html:
        for href, label in _ANCHOR_RE.findall(html):
            haystack = (href + " " + label).lower()
            if any(k in haystack for k in _UNSUB_KEYWORDS):
                _record(href)

    if text:
        for line in text.splitlines():
            low = line.lower()
            if any(k in low for k in _UNSUB_KEYWORDS):
                for token in line.split():
                    token = token.strip(".,;:()[]<>\"'")
                    if token.lower().startswith(("http://", "https://")):
                        _record(token)

    return found


def _mailto_envelope(
    to: str, subject: str, body: str, *, source: str
) -> dict[str, Any]:
    return {
        "status": "mailto_returned_for_agent",
        "mailto": {"to": to, "subject": subject, "body": body},
        "attempted_methods": [],
        "response_code": None,
        "scraped_from": source,
    }


def _http_post(url: str, body: str, timeout_secs: float) -> int:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except urllib.error.URLError:
        return 599


def _http_get(url: str, timeout_secs: float) -> int:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except urllib.error.URLError:
        return 599


def _result_envelope(
    status: str, attempted: list[str], code: int | None, url: str
) -> dict[str, Any]:
    return {
        "status": status,
        "url": url,
        "attempted_methods": attempted,
        "response_code": code,
    }


# ---------------------------------------------------------------------------
# rank-senders — aggregate by sender across one or more folders, sort by
# count or volume. Powers backlog mode's "top senders by count/volume"
# workflows. No fetch of message bodies — only header-derivable fields.
# ---------------------------------------------------------------------------


_RANK_METRICS = {"count", "volume"}


@register("rank-senders")
def _rank_senders(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    from lib.headers import parse_address_list
    from lib.mcp_client import McpClient, McpClientError

    server_cfg = request.get("mcp_server")
    if not server_cfg or "command" not in server_cfg:
        raise SidecarError("bad_request", "mcp_server.command is required")

    metric = request.get("metric", "count")
    if metric not in _RANK_METRICS:
        raise SidecarError(
            "bad_request",
            f"metric must be one of {sorted(_RANK_METRICS)}; got {metric!r}",
        )

    # Default to rustymail's real tool names. The mock server understands
    # both list_cached_emails and list_emails_in_folder.
    list_tool = request.get("list_tool", "list_cached_emails")
    list_tool_args = dict(request.get("list_tool_args") or {})
    # account_id is required by rustymail's list_cached_emails. Accept it
    # as a top-level field so the agent doesn't have to know about
    # list_tool_args plumbing; merge into list_tool_args.
    account_id = request.get("account_id")
    if account_id and "account_id" not in list_tool_args:
        list_tool_args["account_id"] = account_id
    list_folders_tool = request.get("list_folders_tool", "list_folders_hierarchical")
    limit = int(request.get("limit", 50))
    sample_subjects_max = int(request.get("sample_subjects_max", 3))
    cutoff = request.get("exclude_already_processed_until")

    folders = _resolve_folders(request)

    client = McpClient(command=server_cfg["command"], env=server_cfg.get("env"))
    aggregated: dict[str, dict[str, Any]] = {}
    messages_scanned = 0
    messages_excluded = 0
    mcp_calls = 0

    try:
        client.open()
        # If caller asked for "all_folders" without an explicit list,
        # discover them via list_folders_tool.
        if folders is None:
            mcp_calls += 1
            try:
                folder_result = client.call_tool(list_folders_tool, {})
            except McpClientError as e:
                raise SidecarError("mcp_error", str(e)) from e
            payload = _extract_payload(folder_result)
            folders = payload.get("folders") if isinstance(payload, dict) else None
            if not folders:
                folders = []

        page_size = int(request.get("page_size", 500))
        for folder in folders:
            args = dict(list_tool_args)
            args["folder"] = folder
            try:
                entries = _paginate_messages(
                    client, list_tool, args, page_size=page_size
                )
            except McpClientError as e:
                raise SidecarError("mcp_error", str(e)) from e
            # mcp_calls grows by ceil(len(entries)/page_size); approximate.
            mcp_calls += max(1, (len(entries) + page_size - 1) // page_size)
            for entry in entries:
                messages_scanned += 1
                if cutoff and (entry.get("date") or "") <= cutoff:
                    messages_excluded += 1
                    continue
                _accumulate(aggregated, entry, folder, parse_address_list)
    finally:
        client.close()

    ranking = _finalize_ranking(aggregated, metric, limit, sample_subjects_max)
    return (
        {
            "ranking": ranking,
            "scan_summary": {
                "folders_scanned": folders,
                "messages_scanned": messages_scanned,
                "messages_excluded": messages_excluded,
                "unique_senders": len(aggregated),
                "metric": metric,
            },
        },
        {
            "messages_scanned": messages_scanned,
            "mcp_calls": mcp_calls,
            "result_bytes": 0,
        },
    )


def _resolve_folders(request: dict[str, Any]) -> list[str] | None:
    """Return an explicit folder list, or None to mean 'discover via tool'."""
    explicit = request.get("folders")
    if explicit:
        return list(explicit)
    scope = request.get("scope")
    if scope == "inbox":
        return ["INBOX"]
    if scope == "all_folders" or scope is None:
        return None  # discover via list_folders_tool
    raise SidecarError(
        "bad_request",
        f"scope must be 'inbox', 'all_folders', or a folders list; got {scope!r}",
    )


def _accumulate(
    aggregated: dict[str, dict[str, Any]],
    entry: dict[str, Any],
    folder: str,
    parse_address_list: Any,
) -> None:
    """Add one message entry into the per-sender aggregation."""
    addrs = _entry_from_addrs(entry, parse_address_list)
    if not addrs:
        return
    addr = addrs[0]["addr"]
    name = addrs[0]["name"]

    bucket = aggregated.setdefault(
        addr,
        {
            "sender": addr,
            "name": name,  # first observation wins (overwritten below if blank)
            "message_count": 0,
            "total_bytes": 0,
            "sample_subjects": [],
            "subject_seen": set(),
            "oldest": None,
            "newest": None,
            "folders": set(),
            "sample_uids": [],
        },
    )
    if not bucket["name"] and name:
        bucket["name"] = name

    bucket["message_count"] += 1
    bucket["total_bytes"] += _entry_size(entry)
    subj = entry.get("subject") or ""
    if subj and subj not in bucket["subject_seen"]:
        bucket["subject_seen"].add(subj)
        bucket["sample_subjects"].append(subj)
    date = entry.get("date") or ""
    if date:
        if bucket["oldest"] is None or date < bucket["oldest"]:
            bucket["oldest"] = date
        if bucket["newest"] is None or date > bucket["newest"]:
            bucket["newest"] = date
    bucket["folders"].add(folder)
    uid = entry.get("uid")
    if uid is not None and len(bucket["sample_uids"]) < 3:
        bucket["sample_uids"].append(uid)


def _finalize_ranking(
    aggregated: dict[str, dict[str, Any]],
    metric: str,
    limit: int,
    sample_subjects_max: int,
) -> list[dict[str, Any]]:
    """Sort + slice + serialize."""
    key = "message_count" if metric == "count" else "total_bytes"
    rows = sorted(
        aggregated.values(),
        key=lambda b: (-b[key], b["sender"]),
    )[:limit]
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "sender": r["sender"],
                "name": r["name"],
                "message_count": r["message_count"],
                "total_bytes": r["total_bytes"],
                "sample_subjects": r["sample_subjects"][:sample_subjects_max],
                "oldest": r["oldest"],
                "newest": r["newest"],
                "folders": sorted(r["folders"]),
                "sample_uids": r.get("sample_uids", []),
            }
        )
    return out


# ---------------------------------------------------------------------------
# prepare-bulk-delete — dry-run scan for messages matching a selector.
# Returns count + folder list + samples + estimated storage. NEVER
# deletes anything; the actual delete is the agent's responsibility
# after explicit user confirmation.
# ---------------------------------------------------------------------------


_BULK_DELETE_SELECTORS = {"from_sender", "from_domain", "list_id"}

# Patterns that strongly suggest a message is bulk (newsletter / promo /
# automated mailing) vs transactional (receipt / 1:1 correspondence).
# We use these on the body text because cached search results don't
# surface List-Id / List-Unsubscribe as top-level fields. This is
# heuristic — when in doubt, the agent should sample-confirm with the
# user before bulk-deleting.
_BULK_BODY_HINTS = re.compile(
    r"\bunsubscribe\b|\bopt[- ]out\b|\bmanage[ -]subscription\b|\bview in browser\b|"
    r"\bpreferences\b|<a[^>]*unsubscribe",
    flags=re.IGNORECASE,
)


def _looks_bulk(entry: dict[str, Any]) -> bool:
    """Heuristic: does a cached search result look like bulk mass mail?"""
    haystack = " ".join(
        (entry.get(k) or "")
        for k in ("body_text", "body_html", "subject")
    )
    return bool(_BULK_BODY_HINTS.search(haystack))


@register("prepare-bulk-delete")
def _prepare_bulk_delete(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    from lib.mcp_client import McpClient, McpClientError

    server_cfg = request.get("mcp_server")
    if not server_cfg or "command" not in server_cfg:
        raise SidecarError("bad_request", "mcp_server.command is required")
    selector = request.get("selector") or {}
    kind = selector.get("kind")
    if kind not in _BULK_DELETE_SELECTORS:
        raise SidecarError(
            "bad_request",
            f"selector.kind must be one of {sorted(_BULK_DELETE_SELECTORS)}; got {kind!r}",
        )

    search_tool = request.get("search_tool", "search_by_sender")
    search_args = dict(request.get("search_args") or {})
    search_args.setdefault("value", selector.get("value", ""))
    search_args.setdefault("scope", request.get("scope", "inbox"))
    sample_size = int(request.get("sample_size", 5))
    bulk_only = bool(request.get("bulk_only", False))

    client = McpClient(command=server_cfg["command"], env=server_cfg.get("env"))
    try:
        client.open()
        try:
            tool_result = client.call_tool(search_tool, search_args)
        except McpClientError as e:
            raise SidecarError("mcp_error", str(e)) from e
    finally:
        client.close()

    payload = _extract_payload(tool_result)
    matches = payload.get("matches") if isinstance(payload, dict) else None
    if matches is None:
        matches = payload if isinstance(payload, list) else []

    folders: set[str] = set()
    total_bytes = 0
    samples: list[dict[str, Any]] = []
    bulk_uids: list[int] = []
    transactional_uids: list[int] = []
    transactional_samples: list[dict[str, Any]] = []

    for entry in matches:
        folder = entry.get("folder", "INBOX")
        folders.add(folder)
        total_bytes += _entry_size(entry)
        uid = entry.get("uid")
        is_bulk = (not bulk_only) or _looks_bulk(entry)
        sample_entry = {
            "uid": uid,
            "folder": folder,
            "subject": entry.get("subject"),
            "from": _entry_from_display(entry),
            "date": entry.get("date"),
        }
        if is_bulk:
            if uid is not None:
                bulk_uids.append(int(uid))
            if len(samples) < sample_size:
                samples.append(sample_entry)
        else:
            if uid is not None:
                transactional_uids.append(int(uid))
            if len(transactional_samples) < sample_size:
                transactional_samples.append(sample_entry)

    result = {
        "dry_run": True,
        "selector": selector,
        "match_count": len(matches),
        "folders": sorted(folders),
        "samples": samples,
        "estimated_storage_freed_bytes": total_bytes,
        "bulk_uids": bulk_uids,
        "bulk_count": len(bulk_uids),
        "transactional_uids": transactional_uids,
        "transactional_count": len(transactional_uids),
        "transactional_samples": transactional_samples,
        "bulk_only": bulk_only,
        "bulk_detection_method": "body-keyword-heuristic"
        if bulk_only
        else "none (every match treated as bulk)",
    }

    return (
        result,
        {
            "messages_scanned": len(matches),
            "mcp_calls": 1,
            "result_bytes": 0,
        },
    )


# ---------------------------------------------------------------------------
# parse-ical — extract VEVENT fields from an iCalendar blob (no MCP).
# ---------------------------------------------------------------------------


@register("parse-ical")
def _parse_ical(request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    from lib.ical import parse

    text = request.get("ical_text")
    if text is None:
        raise SidecarError("bad_request", "ical_text field is required")
    result = parse(text)
    return (
        result,
        {
            "messages_scanned": 0,
            "mcp_calls": 0,
            "result_bytes": 0,
            "events_parsed": len(result["events"]),
        },
    )


def _split_body(raw: str) -> str:
    """Split RFC 5322 message into header/body and return the body."""
    if not raw:
        return ""
    parts = raw.split("\r\n\r\n", 1)
    if len(parts) == 2:
        return parts[1]
    parts = raw.split("\n\n", 1)
    return parts[1] if len(parts) == 2 else ""


# Future subcommands — declared here so the dispatcher's "unknown
# subcommand" message lists them, even though they raise NotImplemented
# until their slice ships. Mirrors design.md §6.
_PLANNED = [
    "contacts-refresh",
    "parse-feedback-reply",
]

for _name in _PLANNED:
    def _make_pending(n: str) -> Handler:
        def _pending(_req: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
            raise SidecarError(
                "not_implemented",
                f"subcommand {n!r} is declared but not implemented yet",
            )

        return _pending

    HANDLERS[_name] = _make_pending(_name)


# ---------------------------------------------------------------------------
# CLI dispatch.
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="scan.py",
        description="email-processing-hand sidecar (skeleton)",
    )
    p.add_argument(
        "subcommand",
        choices=sorted(HANDLERS.keys()),
        help="which sidecar operation to run",
    )
    p.add_argument(
        "--input",
        default="-",
        help="path to request JSON, or '-' for stdin (default)",
    )
    return p.parse_args(argv)


def _read_request(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    started = time.monotonic()
    request_id = uuid.uuid4().hex

    try:
        request = _read_request(args.input)
    except json.JSONDecodeError as e:
        _emit(
            _envelope(
                args.subcommand,
                request_id,
                started,
                error={"code": "bad_request", "message": f"input JSON parse error: {e}"},
            )
        )
        return 2

    handler = HANDLERS[args.subcommand]
    try:
        result, metrics = handler(request)
    except SidecarError as e:
        _emit(
            _envelope(
                args.subcommand,
                request_id,
                started,
                error={"code": e.code, "message": e.message},
            )
        )
        return 1
    except Exception as e:  # noqa: BLE001
        _emit(
            _envelope(
                args.subcommand,
                request_id,
                started,
                error={"code": "internal_error", "message": repr(e)},
            )
        )
        return 1

    _emit(
        _envelope(
            args.subcommand,
            request_id,
            started,
            result=result,
            metrics=metrics,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
