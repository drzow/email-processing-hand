"""Unit tests for tools/lib/body.py."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from lib import body  # noqa: E402


def test_html_to_text_strips_tags_and_decodes_entities() -> None:
    h = "<p>Hello, &amp; goodbye.</p>"
    assert body.html_to_text(h) == "Hello, & goodbye."


def test_html_to_text_drops_script_and_style_blocks() -> None:
    h = "before<script>alert(1)</script>middle<style>p{}</style>after"
    assert body.html_to_text(h) == "beforemiddleafter"


def test_html_to_text_preserves_some_structure() -> None:
    h = "<p>line one</p><p>line two</p>"
    assert "line one" in body.html_to_text(h)
    assert "line two" in body.html_to_text(h)


def test_html_to_text_handles_br() -> None:
    h = "first<br>second<br/>third"
    rendered = body.html_to_text(h)
    assert "first" in rendered and "second" in rendered and "third" in rendered


def test_truncate_returns_full_text_under_cap() -> None:
    text, was_truncated = body.truncate("hello", 100)
    assert text == "hello"
    assert was_truncated is False


def test_truncate_caps_long_text_and_signals() -> None:
    text, was_truncated = body.truncate("0123456789", 5)
    assert text == "01234"
    assert was_truncated is True


def test_truncate_zero_or_negative_cap_passes_through() -> None:
    assert body.truncate("hello", 0) == ("hello", False)


def test_render_prefers_plaintext_over_html_when_both_present() -> None:
    out = body.render(raw_text="plaintext body", raw_html="<p>html</p>", max_chars=100)
    assert out["source"] == "text"
    assert out["body_text"] == "plaintext body"
    assert out["body_truncated"] is False


def test_render_falls_back_to_html_when_no_plaintext() -> None:
    out = body.render(raw_text=None, raw_html="<p>hi</p>", max_chars=100)
    assert out["source"] == "html"
    assert out["body_text"] == "hi"


def test_render_returns_empty_when_both_absent() -> None:
    out = body.render(raw_text=None, raw_html=None, max_chars=100)
    assert out["body_text"] == ""
    assert out["body_truncated"] is False
    assert out["source"] == "none"


def test_render_truncates_long_text() -> None:
    out = body.render(raw_text="a" * 200, raw_html=None, max_chars=50)
    assert len(out["body_text"]) == 50
    assert out["body_truncated"] is True
