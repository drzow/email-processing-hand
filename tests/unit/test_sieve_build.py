"""Unit tests for tools/lib/sieve.py — Sieve script body generator."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from lib import sieve  # noqa: E402


def _rule(**over) -> dict:
    base = {
        "rule_id": "r_2026-05-11_001",
        "scope": "sender",
        "value": "x@example.com",
        "kind": "deterministic",
        "bucket": "Filter",
        "options": {"folder": "Inbox/Filed"},
        "learned_at": "2026-05-11T07:00:00-05:00",
        "source_message_id": "<x@example.com>",
        "confirmed_count": 1,
        "corrected_count": 0,
    }
    base.update(over)
    return base


# ---------- empty + header -----------------------------------------------


def test_empty_rules_emits_just_require_and_header() -> None:
    body = sieve.build_script_body([], generated_at="2026-05-11T07:00:00-05:00")
    assert body.startswith("require ")
    assert "DO NOT EDIT BY HAND" in body
    assert "Last updated: 2026-05-11T07:00:00-05:00" in body
    assert "Rule count: 0" in body
    # No `if` statements when there are no rules.
    assert "\nif " not in body


def test_header_lists_required_extensions() -> None:
    body = sieve.build_script_body([])
    # Extensions the generator emits should be in the require line.
    require_line = body.splitlines()[0]
    for ext in ("fileinto", "imap4flags", "envelope"):
        assert f'"{ext}"' in require_line


# ---------- Filter buckets ----------------------------------------------


def test_filter_sender_rule_renders_fileinto_and_seen() -> None:
    body = sieve.build_script_body(
        [_rule(bucket="Filter", scope="sender",
               value="receipts@stripe.com",
               options={"folder": "Receipts"})]
    )
    assert 'if address :is "from" "receipts@stripe.com" {' in body
    assert 'fileinto "Receipts";' in body
    assert 'addflag "\\\\Seen";' in body
    assert "stop;" in body


def test_filter_domain_rule_uses_is_domain() -> None:
    body = sieve.build_script_body(
        [_rule(bucket="Filter", scope="domain",
               value="vendor.com",
               options={"folder": "Vendors"})]
    )
    assert 'if address :is :domain "from" "vendor.com" {' in body
    assert 'fileinto "Vendors";' in body


def test_filter_mark_seen_false_omits_addflag() -> None:
    body = sieve.build_script_body(
        [_rule(bucket="Filter",
               options={"folder": "Inbox/Quiet", "mark_seen": False})]
    )
    assert 'fileinto "Inbox/Quiet";' in body
    assert "addflag" not in body


def test_filter_with_slash_in_folder_quotes_correctly() -> None:
    body = sieve.build_script_body(
        [_rule(bucket="Filter",
               options={"folder": "Projects/Acme/2026"})]
    )
    assert 'fileinto "Projects/Acme/2026";' in body


# ---------- Blacklist + UnsubAndBlock buckets ----------------------------


def test_blacklist_sender_rule_renders_discard() -> None:
    body = sieve.build_script_body(
        [_rule(bucket="Blacklist", scope="sender",
               value="spam@evil.example")]
    )
    assert 'if address :is "from" "spam@evil.example" {' in body
    assert "discard;" in body
    assert "stop;" in body


def test_blacklist_domain_rule_uses_envelope_domain() -> None:
    """Domain blacklist uses envelope :domain so internal forwarders'
    headers don't bypass it."""
    body = sieve.build_script_body(
        [_rule(bucket="Blacklist", scope="domain",
               value="spam-vendor.example")]
    )
    assert 'if envelope :domain "from" "spam-vendor.example" {' in body


def test_unsub_and_block_treated_like_blacklist() -> None:
    body = sieve.build_script_body(
        [_rule(bucket="UnsubAndBlock", scope="domain", value="newsletter.example")]
    )
    assert 'if envelope :domain "from" "newsletter.example" {' in body
    assert "discard;" in body


# ---------- skipped buckets ---------------------------------------------


def test_non_pushable_buckets_skipped_with_a_comment() -> None:
    """Buckets that need post-classification logic (RespondUrgent etc.)
    aren't pushable to Sieve — emit a comment so the body reflects the
    rule's existence without firing on it."""
    body = sieve.build_script_body(
        [
            _rule(bucket="Filter", value="ok@example.com"),
            _rule(rule_id="r_002", bucket="RespondUrgent", value="boss@example.com"),
            _rule(rule_id="r_003", bucket="InviteAsk", value="invites@example.com"),
            _rule(rule_id="r_004", bucket="Skip"),
        ]
    )
    # Filter rule produced a real if-block.
    assert 'fileinto' in body
    # Non-pushable rules appear only as comments.
    assert "# r_002" in body
    assert "RespondUrgent" in body
    assert "# r_003" in body
    # No discard/fileinto for the non-pushable rules. The require line
    # also contains "fileinto" so we count just the action use.
    assert body.count("    fileinto ") == 1
    assert "    discard;" not in body


# ---------- escaping + ordering -----------------------------------------


def test_quoted_string_escapes_quotes_and_backslashes() -> None:
    body = sieve.build_script_body(
        [_rule(bucket="Filter",
               value='spam "quoted"\\path@example.com',
               options={"folder": 'has "quote" and \\back'})]
    )
    # Address: " → \" and \ → \\
    assert 'spam \\"quoted\\"\\\\path@example.com' in body
    assert 'has \\"quote\\" and \\\\back' in body


def test_rules_emitted_in_rule_id_order() -> None:
    body = sieve.build_script_body(
        [
            _rule(rule_id="r_b", bucket="Filter",
                  value="b@example.com", options={"folder": "B"}),
            _rule(rule_id="r_a", bucket="Filter",
                  value="a@example.com", options={"folder": "A"}),
        ]
    )
    a_pos = body.index('"a@example.com"')
    b_pos = body.index('"b@example.com"')
    assert a_pos < b_pos, "rules should sort by rule_id ascending"


def test_rule_id_appears_in_block_comment_for_auditability() -> None:
    body = sieve.build_script_body(
        [_rule(rule_id="r_xyz_1234", bucket="Filter",
               value="x@example.com", options={"folder": "X"})]
    )
    assert "r_xyz_1234" in body


# ---------- validity check helper ----------------------------------------


def test_validates_at_least_one_pushable_rule_via_summary() -> None:
    """build_summary reports how many rules were emitted vs skipped."""
    summary = sieve.build_summary(
        [
            _rule(bucket="Filter"),
            _rule(rule_id="r_b", bucket="Blacklist", scope="sender", value="x@y.com"),
            _rule(rule_id="r_c", bucket="RespondUrgent"),
        ]
    )
    assert summary["emitted_count"] == 2
    assert summary["skipped_count"] == 1
    assert "RespondUrgent" in summary["skipped_buckets"]


def test_invalid_sender_name_skipped_with_warning() -> None:
    """Sieve names can't contain control chars per RFC 5804 §1.6."""
    body, summary = sieve.build_with_summary(
        [_rule(bucket="Filter",
               value="bad\x01name@example.com",
               options={"folder": "X"})]
    )
    assert "bad\x01" not in body
    assert summary["skipped_count"] == 1
    assert any("control" in r.lower() or "invalid" in r.lower()
               for r in summary["skip_reasons"])
