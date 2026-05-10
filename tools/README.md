# Sidecar (`tools/scan.py`)

Mechanical bulk-operation runner for the email-processing-hand. The
agent calls subcommands via `shell_exec`; results come back as a
stable JSON envelope. Skeleton currently ships only `noop` —
substantive subcommands are added per the slice plan in
`../docs/design.md`.

## Running standalone

```bash
echo '{}' | python3 tools/scan.py noop
# {"status":"ok","subcommand":"noop","request_id":"...","elapsed_ms":0,
#  "result":{"message":"sidecar reachable; skeleton mode"},"error":null,
#  "metrics":{"messages_scanned":0,"mcp_calls":0,"result_bytes":0}}
```

## Response envelope

```json
{
  "status":      "ok" | "error",
  "subcommand":  "<name>",
  "request_id":  "<uuid hex>",
  "elapsed_ms":  1234,
  "result":      { ... } | null,
  "error":       { "code": "...", "message": "..." } | null,
  "metrics":     { "messages_scanned": N, "mcp_calls": M, "result_bytes": N }
}
```

The agent **always** reads `status` from the envelope rather than the
process exit code — some subcommands return logical-error envelopes
with exit 0.

## Testing

```bash
cd tools && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest ../tests/unit
```
