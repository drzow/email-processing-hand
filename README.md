# email-processing-hand

OpenFang Hand that classifies, monitors, and processes inbound email
across two accounts (work MS365 + personal IMAP/mailbox.org). See
`docs/design.md` for the full design, `SKILL.md` for the operator
summary.

## Status

**Skeleton.** HAND.toml + dispatch wiring are in place. Per-bucket
handlers, Python sidecar, and Sieve regeneration are upcoming slices.

## Install

1. Clone this repo somewhere stable, e.g. `~/projects/email-processing-hand/`.
2. Symlink the hand into OpenFang's discovery directory:

   ```bash
   mkdir -p ~/.openfang/hands/email-processing
   ln -s ~/projects/email-processing-hand/HAND.toml \
         ~/.openfang/hands/email-processing/HAND.toml
   ln -s ~/projects/email-processing-hand/SKILL.md  \
         ~/.openfang/hands/email-processing/SKILL.md
   ```

3. Make sure these MCP servers are configured in `~/.openfang/config.toml`:

   - `rustymail` — IMAP + ManageSieve
   - `ms365` — Microsoft Graph (work account)
   - `slack` — urgent alerts
   - `mcp-atlassian` — Jira project bootstrap

4. Restart OpenFang:

   ```bash
   sudo systemctl restart openfang.service
   ```

5. Open the dashboard, find **Email Processing Hand**, and review settings.
   `processing_mode` defaults to `paused` so nothing happens until you
   flip it to `incremental`.

## Setup checklist

Once the per-bucket handlers ship you'll need:

- [ ] mailbox.org IMAP credentials in rustymail's `config/accounts.json`
      (or env-var override). Sieve runs on the same host (port 4190).
- [ ] MS365 OAuth flow completed via the `ms365` MCP server.
- [ ] Slack token + channel id for urgent alerts (or DM-to-self).
- [ ] User domains list (your own organization's email domains).
- [ ] Jira projects to digest (set during the bootstrap flow on first run).

## Layout

```
.
├── HAND.toml      Hand manifest — settings, dashboard metrics, system prompt
├── SKILL.md       Operator-facing notes
├── docs/
│   └── design.md  Full design spec
├── tools/
│   ├── scan.py    Python sidecar entry point (skeleton)
│   ├── lib/       Sidecar helper modules (per design §6)
│   └── requirements.txt
├── tests/
│   ├── unit/      Sidecar pytest unit tests
│   ├── fixtures/  Sample raw RFC 5322 messages
│   └── prompt/    Hand-curated classification eval cases
└── examples/
    └── sieve_scripts/   Sample Sieve scripts for reference
```

## Development

The Python sidecar will live in `tools/`. Bring up the venv with:

```bash
cd tools && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/unit
```

For prompt-eval the harness in `tests/prompt/classification_eval.md`
runs in CI when the system prompt changes.

## License

Apache 2.0 (matches Scalesology's other open hands).
