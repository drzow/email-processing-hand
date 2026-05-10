"""Unit tests for the skeleton sidecar.

Substantive tests land per slice as the subcommands gain real bodies.
The point of the skeleton tests is just to lock the JSON envelope
contract and the dispatch shape so future slices don't drift.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_PY = REPO_ROOT / "tools" / "scan.py"


def run_scan(subcommand: str, request: dict | None = None) -> dict:
    """Invoke scan.py as a subprocess and return the parsed envelope."""
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), subcommand],
        input=json.dumps(request or {}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout, f"scan.py produced no stdout (stderr={proc.stderr!r})"
    return json.loads(proc.stdout.splitlines()[-1])


def test_noop_returns_ok_envelope() -> None:
    env = run_scan("noop")
    assert env["status"] == "ok"
    assert env["subcommand"] == "noop"
    assert env["error"] is None
    assert env["result"]["message"].startswith("sidecar reachable")
    assert env["request_id"], "request_id must be populated"
    assert env["elapsed_ms"] >= 0


def test_envelope_contract_has_all_required_keys() -> None:
    env = run_scan("noop")
    required = {"status", "subcommand", "request_id", "elapsed_ms", "result", "error", "metrics"}
    assert set(env.keys()) == required, f"unexpected keys: {set(env.keys()) ^ required}"


def test_planned_subcommands_return_not_implemented_error() -> None:
    # Each subcommand declared in scan._PLANNED should exit cleanly with a
    # structured "not_implemented" error envelope rather than crashing.
    for name in (
        "fetch-batch",
        "rank-senders",
        "resolve-domain",
        "parse-thread",
        "parse-feedback-reply",
    ):
        env = run_scan(name)
        assert env["status"] == "error", f"{name}: expected error status, got {env}"
        assert env["error"]["code"] == "not_implemented"
        assert name in env["error"]["message"]


def test_unknown_subcommand_is_rejected_by_argparse() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "totally-made-up"],
        input="{}",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "invalid choice" in proc.stderr or "totally-made-up" in proc.stderr


def test_bad_json_input_returns_envelope_with_bad_request_error() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "noop"],
        input="{ this is not json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout
    env = json.loads(proc.stdout.splitlines()[-1])
    assert env["status"] == "error"
    assert env["error"]["code"] == "bad_request"


def test_empty_stdin_is_treated_as_empty_request() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCAN_PY), "noop"],
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    env = json.loads(proc.stdout.splitlines()[-1])
    assert env["status"] == "ok"
