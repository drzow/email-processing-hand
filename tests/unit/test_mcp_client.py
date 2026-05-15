"""Integration tests for tools/lib/mcp_client.py against the mock MCP server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from lib.mcp_client import McpClient, McpClientError  # noqa: E402

MOCK_SERVER = REPO_ROOT / "tests" / "fixtures" / "mock_mcp_server.py"


def _client() -> McpClient:
    return McpClient(command=[sys.executable, str(MOCK_SERVER)])


def test_initialize_handshake_succeeds() -> None:
    with _client() as c:
        # Just opening drives the handshake; success means no exception.
        assert c is not None


def test_call_tool_round_trips_args() -> None:
    with _client() as c:
        result = c.call_tool("echo", {"hello": "world", "n": 7})
    text = result["content"][0]["text"]
    assert json.loads(text) == {"hello": "world", "n": 7}


def test_call_tool_propagates_server_errors() -> None:
    with _client() as c:
        with pytest.raises(McpClientError) as excinfo:
            c.call_tool("raise_error", {"code": -32602, "message": "bad input"})
    assert "bad input" in str(excinfo.value)
    assert "-32602" in str(excinfo.value)


def test_call_tool_unknown_tool_is_an_error() -> None:
    with _client() as c:
        with pytest.raises(McpClientError) as excinfo:
            c.call_tool("does-not-exist", {})
    assert "does-not-exist" in str(excinfo.value)


def test_call_tool_raises_when_result_carries_is_error_true() -> None:
    """Real rustymail returns id-matched 'success' responses with
    isError=true in the result envelope when a tool fails (e.g.,
    unknown tool name). Treat those as errors, not empty success."""
    with _client() as c:
        with pytest.raises(McpClientError) as excinfo:
            c.call_tool("tool_returns_is_error", {})
    msg = str(excinfo.value)
    assert "isError" in msg or "Tool execution failed" in msg


def test_get_email_by_uid_returns_canned_message() -> None:
    with _client() as c:
        result = c.call_tool("get_email_by_uid", {"folder": "INBOX", "uid": 1})
    payload = json.loads(result["content"][0]["text"])
    assert payload["uid"] == 1
    assert payload["folder"] == "INBOX"
    assert "Q3 plan questions" in payload["raw"]


def test_close_is_idempotent() -> None:
    c = _client()
    c.open()
    c.close()
    c.close()  # no-op, no exception


def test_double_open_raises() -> None:
    c = _client()
    c.open()
    try:
        with pytest.raises(McpClientError):
            c.open()
    finally:
        c.close()
