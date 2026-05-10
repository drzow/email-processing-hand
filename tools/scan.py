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


# Future subcommands — declared here so the dispatcher's "unknown
# subcommand" message lists them, even though they raise NotImplemented
# until their slice ships. Mirrors design.md §6.
_PLANNED = [
    "contacts-bootstrap",
    "contacts-refresh",
    "rank-senders",
    "fetch-batch",
    "parse-thread",
    "resolve-domain",
    "prepare-bulk-delete",
    "parse-feedback-reply",
    "submit-unsubscribe",
    "parse-ical",
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
