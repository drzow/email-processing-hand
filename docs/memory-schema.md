# Memory schema

Every persisted key the hand owns. All blobs carry a top-level
`version` field so future schema migrations are visible. The schema is
duplicated lightly between this doc and the design (§7) — the design
is the source of truth for *intent*; this file is the operational
reference for *what's currently shipped*.

## Mixed isolation

Contacts and a few global flags are shared across accounts; everything
else is namespaced per account so two accounts can't trample each
other's rules.

| Key                                                | Scope        | Status   |
|----------------------------------------------------|--------------|----------|
| `email.contacts`                                   | global       | reserved |
| `email.exclude_domains`                            | global       | reserved |
| `email.user_domains`                               | global       | reserved |
| `email.global.run_metrics`                         | global       | reserved |
| `email.global.last_error`                          | global       | reserved |
| `email.global.bootstrap_progress`                  | global       | **live** |
| `email.account.<id>.sender_rules`                  | per-account  | **live** |
| `email.account.<id>.project_map`                   | per-account  | reserved |
| `email.account.<id>.cursor`                        | per-account  | **live** |
| `email.account.<id>.digest_state`                  | per-account  | reserved |
| `email.account.<id>.sieve_state`                   | per-account  | reserved |
| `email.account.<id>.training_state`                | per-account  | reserved |
| `email.account.<id>.alert_log`                     | per-account  | **live** |
| `email.account.<id>.unsubscribe_log`               | per-account  | reserved |
| `email.account.<id>.classify_only_log`             | per-account  | **live** |
| `email_processing_state`                           | global       | **live** |
| `email_metrics_*`                                  | global       | **live** |

**live** = read or written by the current slice. **reserved** = key
name is locked but the slice that writes it hasn't shipped.

## Key shapes

### `email.global.bootstrap_progress`

```json
{
  "version": 1,
  "step": 0,
  "completed_steps": [],
  "pending_user_input": null,
  "started_at": "2026-05-10T07:00:00-05:00"
}
```

Steps land in `completed_steps` as the bootstrap state machine
advances. Once all 6 steps from design §8 are present, normal cycles
ignore this blob.

### `email.account.<id>.sender_rules`

```json
{
  "version": 1,
  "rules": [
    {
      "rule_id": "r_2026-05-10_001",
      "scope": "sender",
      "value": "noreply@github.com",
      "kind": "deterministic",
      "bucket": "Filter",
      "options": {"folder": "GitHub"},
      "learned_at": "2026-05-10T07:00:00-05:00",
      "source_message_id": "<gh-pr-1@github.com>",
      "confirmed_count": 1,
      "corrected_count": 0
    }
  ]
}
```

`scope` ∈ {`sender`, `domain`, `thread`}. `kind` ∈
{`deterministic`, `content_dependent`}. Only deterministic rules
get pushed to Sieve.

### `email.account.<id>.cursor`

```json
{
  "version": 1,
  "last_processed_uid": 42891,
  "last_processed_internaldate": "2026-05-10T06:55:01Z",
  "last_run": "2026-05-10T07:00:00-05:00",
  "last_run_outcome": "ok"
}
```

`last_run_outcome` ∈ {`ok`, `partial`, `error`, `skeleton-noop`,
`paused-noop`, `classify-only`}.

### `email.account.<id>.alert_log`

Ring buffer of the most recent 200 urgent alerts. Used for dedupe — if
the same `message_ref` was alerted in the last 24h, we don't alert
again.

```json
{
  "version": 1,
  "entries": [
    {
      "alerted_at": "2026-05-10T07:00:00-05:00",
      "channel": "slack",
      "bucket": "RespondUrgent",
      "message_ref": "<urgent@acme.com>",
      "subject": "DB connection failures since 9am"
    }
  ]
}
```

### `email.account.<id>.classify_only_log`

Append-only journal of what the hand WOULD have done while
`classify_only_mode = true`. The operator reviews this before flipping
the toggle.

```json
{
  "version": 1,
  "entries": [
    {
      "ts": "2026-05-10T07:00:00-05:00",
      "message_ref": "<gh-pr-1@github.com>",
      "headers_subject": "[GitHub] New PR review",
      "bucket": "Informational",
      "confidence": 0.92,
      "would_do": "append to digest; leave in inbox",
      "rule_learned": null
    }
  ]
}
```

`rule_learned` is set when the classifier would have created a new
rule. Operators inspect this to decide whether the proposed rule
makes sense before clearing `classify_only_mode`.

### `email_processing_state` and `email_metrics_*`

Existing keys, written every cycle:

```json
// email_processing_state
{
  "last_run_iso": "2026-05-10T07:00:00-05:00",
  "last_run_outcome": "classify-only",
  "last_tick_kind": "incremental"
}

// email_metrics_last_run, email_metrics_health: scalar strings/numbers
// that map directly to the dashboard widgets in HAND.toml.
```

## Versioning + atomicity

Every blob has a `version`. The hand's Phase 0 startup compares the
version of each blob to its known-supported range; an unknown future
version logs an error and refuses to process that blob's domain (e.g.,
unknown `sender_rules` version → classifier still runs but no rules
are applied; falls back to "everything is novel").

There is no cross-key atomicity. The cursor is updated **last** at the
end of a successful cycle; every other write is idempotent (rule
upserts, ring-buffer pushes, classification log appends). A crash
between any two writes is safe to recover from by re-running the
cycle.
