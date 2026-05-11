"""Unit tests for the submit-unsubscribe subcommand."""

from __future__ import annotations

import http.server
import json
import socketserver
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_PY = REPO_ROOT / "tools" / "scan.py"


def run_unsub(request: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "submit-unsubscribe"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout, f"stderr={proc.stderr!r}"
    return json.loads(proc.stdout.splitlines()[-1])


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Captures the request shape into ``self.server.captured``."""

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.server.captured.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "method": "POST",
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body.decode("utf-8", errors="replace"),
            }
        )
        self.send_response(self.server.response_code)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self.server.captured.append(  # type: ignore[attr-defined]
            {"path": self.path, "method": "GET"}
        )
        self.send_response(self.server.response_code)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args) -> None:  # silence stderr noise
        return


@contextmanager
def http_capture(response_code: int = 200):
    """Spin up a localhost HTTP server that captures incoming requests."""
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _RecordingHandler)
    httpd.captured = []  # type: ignore[attr-defined]
    httpd.response_code = response_code  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _url_for(httpd, path: str = "/unsub") -> str:
    host, port = httpd.server_address
    return f"http://{host}:{port}{path}"


# ---------- happy paths --------------------------------------------------


def test_one_click_post_sends_rfc8058_body() -> None:
    with http_capture() as srv:
        env = run_unsub(
            {
                "list_unsubscribe_url": _url_for(srv, "/u/1"),
                "list_unsubscribe_post": "List-Unsubscribe=One-Click",
            }
        )
    assert env["status"] == "ok", env
    r = env["result"]
    assert r["status"] == "submitted"
    assert "post" in r["attempted_methods"]
    assert r["response_code"] == 200
    # Server saw the body exactly as RFC 8058 §3.2 specifies.
    captured = srv.captured  # type: ignore[attr-defined]
    assert len(captured) == 1
    assert captured[0]["method"] == "POST"
    assert captured[0]["path"] == "/u/1"
    assert captured[0]["body"] == "List-Unsubscribe=One-Click"
    assert captured[0]["headers"].get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    )


def test_get_fallback_when_no_one_click_post() -> None:
    with http_capture() as srv:
        env = run_unsub({"list_unsubscribe_url": _url_for(srv, "/u/2")})
    assert env["result"]["status"] == "submitted"
    captured = srv.captured  # type: ignore[attr-defined]
    assert captured[0]["method"] == "GET"
    assert captured[0]["path"] == "/u/2"


def test_https_attempted_even_when_post_param_says_one_click() -> None:
    """Per RFC 8058: ONLY POST is valid for one-click; we do not fall to GET
    after a POST failure unless the caller asks."""
    with http_capture(response_code=500) as srv:
        env = run_unsub(
            {
                "list_unsubscribe_url": _url_for(srv, "/u/3"),
                "list_unsubscribe_post": "List-Unsubscribe=One-Click",
                "fall_back_to_get": False,
            }
        )
    assert env["result"]["status"] == "failed"
    assert env["result"]["response_code"] == 500
    captured = srv.captured  # type: ignore[attr-defined]
    assert len(captured) == 1
    assert captured[0]["method"] == "POST"


def test_fall_back_to_get_after_post_failure_when_enabled() -> None:
    with http_capture(response_code=500) as srv:
        env = run_unsub(
            {
                "list_unsubscribe_url": _url_for(srv, "/u/4"),
                "list_unsubscribe_post": "List-Unsubscribe=One-Click",
                "fall_back_to_get": True,
            }
        )
    captured = srv.captured  # type: ignore[attr-defined]
    methods = [c["method"] for c in captured]
    assert methods == ["POST", "GET"]
    assert "post" in env["result"]["attempted_methods"]
    assert "get" in env["result"]["attempted_methods"]


# ---------- mailto: handling --------------------------------------------


def test_mailto_returns_unsupported_status() -> None:
    """We do not (yet) wire SMTP send for mailto: unsubscribes; the agent
    is expected to compose+send via the email MCP server. submit-unsubscribe
    reports the recipient + body it would have sent."""
    env = run_unsub(
        {
            "list_unsubscribe_mailto": "unsub@example.com",
            "list_unsubscribe_mailto_subject": "unsubscribe",
        }
    )
    assert env["status"] == "ok"
    assert env["result"]["status"] == "mailto_returned_for_agent"
    assert env["result"]["mailto"] == {
        "to": "unsub@example.com",
        "subject": "unsubscribe",
        "body": "",
    }


# ---------- error paths --------------------------------------------------


def test_requires_url_or_mailto() -> None:
    env = run_unsub({})
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"


def test_rejects_non_http_url() -> None:
    env = run_unsub(
        {
            "list_unsubscribe_url": "ftp://example.com/unsub",
        }
    )
    assert env["status"] == "error"
    assert "http" in env["error"]["message"].lower()


def test_failed_response_code_marks_failed_status() -> None:
    with http_capture(response_code=404) as srv:
        env = run_unsub({"list_unsubscribe_url": _url_for(srv, "/missing")})
    assert env["result"]["status"] == "failed"
    assert env["result"]["response_code"] == 404
