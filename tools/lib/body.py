"""Body rendering helpers for fetch-batch.

Stdlib-only first pass: HTML→plaintext via a regex strip + entity
decode, plus a length cap. Quoted-reply stripping and
``html2text`` quality rendering are deferred to a follow-up slice;
the goal here is just to feed the classifier a usable, bounded
plaintext body.
"""

from __future__ import annotations

import html
import re
from typing import Any

# Strip <script> and <style> bodies before the generic tag stripper —
# their contents are noise that the classifier doesn't want to see.
_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def html_to_text(html_str: str) -> str:
    """Cheap HTML→plaintext. Sufficient for classification, not display."""
    if not html_str:
        return ""
    out = _SCRIPT_OR_STYLE.sub("", html_str)
    # Insert newlines around block-level closers so the stripped output
    # keeps some structure.
    out = re.sub(r"<\s*br\s*/?>", "\n", out, flags=re.IGNORECASE)
    out = re.sub(r"</\s*(p|div|li|tr|h\d)\s*>", "\n", out, flags=re.IGNORECASE)
    out = _TAG.sub("", out)
    out = html.unescape(out)
    out = _WHITESPACE.sub(" ", out)
    out = _BLANK_LINES.sub("\n\n", out)
    return out.strip()


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Cap length, signaling whether truncation happened."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def render(*, raw_text: str | None, raw_html: str | None, max_chars: int) -> dict[str, Any]:
    """Return ``{body_text, body_truncated, source}``.

    Prefers plaintext if present; otherwise renders HTML. Empty bodies
    return ``("", False, "none")``.
    """
    if raw_text:
        text, truncated = truncate(raw_text, max_chars)
        return {"body_text": text, "body_truncated": truncated, "source": "text"}
    if raw_html:
        rendered = html_to_text(raw_html)
        text, truncated = truncate(rendered, max_chars)
        return {"body_text": text, "body_truncated": truncated, "source": "html"}
    return {"body_text": "", "body_truncated": False, "source": "none"}
