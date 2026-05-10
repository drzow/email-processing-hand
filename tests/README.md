# Tests

Layered per `docs/design.md` §13:

| Layer | Path | What |
|-------|------|------|
| 1 | `unit/` | Sidecar pytest unit tests with `MockMcpClient` |
| 2 | (rustymail repo) | ManageSieve client tests |
| 3 | `prompt/` | Hand-curated classification eval cases — runs in CI when the system prompt changes |
| 4 | (manual) | Live integration once per release against real test mailbox |

## Run unit tests

```bash
cd tools && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest ../tests/unit -v
```

## Fixtures

`fixtures/` holds sample raw RFC 5322 messages — mailing list, receipt,
urgent, calendar invite, threaded reply. Each subcommand pulls from
these so tests don't depend on a live mailbox.
