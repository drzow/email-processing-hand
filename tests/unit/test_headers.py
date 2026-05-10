"""Unit tests for tools/lib/headers.py — header parsing helpers."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from lib import headers  # noqa: E402


# ---------- decode_subject (RFC 2047) ------------------------------------


def test_decode_subject_passes_plain_ascii_through() -> None:
    assert headers.decode_subject("Hello world") == "Hello world"


def test_decode_subject_handles_q_encoded_word() -> None:
    enc = "=?UTF-8?Q?Re:_caf=C3=A9?="
    assert headers.decode_subject(enc) == "Re: café"


def test_decode_subject_handles_b_encoded_word() -> None:
    # base64 of "Hello, 世界"
    enc = "=?UTF-8?B?SGVsbG8sIOS4lueVjA==?="
    assert headers.decode_subject(enc) == "Hello, 世界"


def test_decode_subject_handles_mixed_encoded_and_plain() -> None:
    enc = "Re: =?UTF-8?Q?caf=C3=A9?= update"
    assert headers.decode_subject(enc) == "Re: café update"


def test_decode_subject_handles_none_and_empty() -> None:
    assert headers.decode_subject(None) == ""
    assert headers.decode_subject("") == ""


# ---------- parse_address_list -------------------------------------------


def test_parse_address_list_single_plain() -> None:
    assert headers.parse_address_list("alice@example.com") == [
        {"name": "", "addr": "alice@example.com"},
    ]


def test_parse_address_list_with_display_name() -> None:
    assert headers.parse_address_list("Alice <alice@example.com>") == [
        {"name": "Alice", "addr": "alice@example.com"},
    ]


def test_parse_address_list_with_quoted_display_name() -> None:
    assert headers.parse_address_list('"Brugger, Terry" <terry@example.com>') == [
        {"name": "Brugger, Terry", "addr": "terry@example.com"},
    ]


def test_parse_address_list_multiple() -> None:
    raw = "Alice <alice@example.com>, bob@example.com, Carol <carol@example.com>"
    assert headers.parse_address_list(raw) == [
        {"name": "Alice", "addr": "alice@example.com"},
        {"name": "", "addr": "bob@example.com"},
        {"name": "Carol", "addr": "carol@example.com"},
    ]


def test_parse_address_list_handles_rfc2047_in_display_name() -> None:
    raw = "=?UTF-8?Q?caf=C3=A9?= <cafe@example.com>"
    assert headers.parse_address_list(raw) == [
        {"name": "café", "addr": "cafe@example.com"},
    ]


def test_parse_address_list_empty_or_none() -> None:
    assert headers.parse_address_list("") == []
    assert headers.parse_address_list(None) == []


def test_parse_address_list_normalizes_addr_to_lowercase() -> None:
    assert headers.parse_address_list("Alice <Alice@Example.COM>") == [
        {"name": "Alice", "addr": "alice@example.com"},
    ]


# ---------- domain_of -----------------------------------------------------


def test_domain_of_extracts_lowercase_domain() -> None:
    assert headers.domain_of("alice@Example.COM") == "example.com"


def test_domain_of_handles_subdomain() -> None:
    assert headers.domain_of("noreply@email.GitHub.com") == "email.github.com"


def test_domain_of_returns_empty_for_invalid_address() -> None:
    assert headers.domain_of("not-an-email") == ""
    assert headers.domain_of("") == ""
    assert headers.domain_of(None) == ""


# ---------- list_unsubscribe ---------------------------------------------


def test_list_unsubscribe_extracts_mailto_and_url() -> None:
    raw = "<mailto:unsub@example.com>, <https://example.com/unsub?id=42>"
    parsed = headers.parse_list_unsubscribe(raw)
    assert parsed["urls"] == ["https://example.com/unsub?id=42"]
    assert parsed["mailtos"] == ["unsub@example.com"]


def test_list_unsubscribe_strips_whitespace_in_urls() -> None:
    raw = "<https://example.com/unsub>"
    parsed = headers.parse_list_unsubscribe(raw)
    assert parsed["urls"] == ["https://example.com/unsub"]
    assert parsed["mailtos"] == []


def test_list_unsubscribe_post_detected_per_rfc8058() -> None:
    # RFC 8058 specifies the literal value "List-Unsubscribe=One-Click".
    assert headers.is_one_click_unsubscribe("List-Unsubscribe=One-Click") is True
    assert headers.is_one_click_unsubscribe("list-unsubscribe=one-click") is True
    assert headers.is_one_click_unsubscribe("foo") is False
    assert headers.is_one_click_unsubscribe(None) is False
    assert headers.is_one_click_unsubscribe("") is False


# ---------- header_set_for_classification --------------------------------


def test_header_set_returns_canonical_keys_for_known_message() -> None:
    raw = (
        "From: Alice <alice@example.com>\r\n"
        "To: bob@example.com, Carol <carol@example.com>\r\n"
        "Cc: dave@example.com\r\n"
        "Subject: =?UTF-8?Q?caf=C3=A9?= update\r\n"
        "Date: Tue, 01 Apr 2025 12:00:00 -0500\r\n"
        "List-Id: <list.example.com>\r\n"
        "List-Unsubscribe: <mailto:unsub@example.com>\r\n"
        "List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n"
        "In-Reply-To: <abc@example.com>\r\n"
        "References: <abc@example.com> <def@example.com>\r\n"
        "Importance: high\r\n"
        "X-OpenFang-Digest-ID: digest-2026-05-10-acme\r\n"
        "\r\n"
        "Body content"
    )
    h = headers.classification_set(raw)
    assert h["from"] == [{"name": "Alice", "addr": "alice@example.com"}]
    assert len(h["to"]) == 2
    assert h["cc"] == [{"name": "", "addr": "dave@example.com"}]
    assert h["subject"] == "café update"
    assert h["list_id"] == "list.example.com"
    assert h["list_unsubscribe"]["urls"] == []
    assert h["list_unsubscribe"]["mailtos"] == ["unsub@example.com"]
    assert h["list_unsubscribe_post_one_click"] is True
    assert h["in_reply_to"] == "<abc@example.com>"
    assert h["references"] == ["<abc@example.com>", "<def@example.com>"]
    assert h["importance"] == "high"
    assert h["x_openfang_digest_id"] == "digest-2026-05-10-acme"


def test_header_set_handles_minimal_message() -> None:
    raw = "From: alice@example.com\r\nSubject: test\r\n\r\nbody"
    h = headers.classification_set(raw)
    assert h["from"] == [{"name": "", "addr": "alice@example.com"}]
    assert h["subject"] == "test"
    assert h["to"] == []
    assert h["cc"] == []
    assert h["list_id"] is None
    assert h["list_unsubscribe"]["urls"] == []
    assert h["in_reply_to"] is None
    assert h["references"] == []
    assert h["importance"] is None
