# Email Processing

Classifies and processes incoming email across two accounts (work MS365 +
personal IMAP/mailbox.org). Backlog mode is the interactive training UX
driven from the OpenFang dashboard chat; incremental mode runs autonomously
every few minutes, applies learned rules, and falls back to a single LLM
classification call for novel senders.

**Status:** skeleton — dispatch + scheduling wired up, per-bucket handlers
deferred to upcoming slices. See `docs/design.md` §15 implementation order.

## Routing buckets (target shape)

The classifier emits one of 13 buckets per inbound message:

| #  | Bucket           | Action shape                                                |
|----|------------------|-------------------------------------------------------------|
| 1  | Blacklist        | Bulk-delete + Sieve `discard;`                              |
| 2  | Unsubscribe-Block | RFC 8058 unsubscribe + bulk-delete past mass mail          |
| 3  | Filter            | Move to a folder (Sieve `fileinto`)                         |
| 4  | Invite out-of-date | Archive or delete (no rule)                                |
| 5  | Invite ask        | Leave in inbox + add to digest                              |
| 6  | Invite accept     | Auto-accept on no calendar conflict                         |
| 7  | Invite unknown    | Spam-or-ask                                                 |
| 8  | Summarize         | Rich summary → digest, archive original                     |
| 9  | Informational     | Rich summary → digest, **leave in inbox** until read        |
| 10 | Respond daily     | Flag + digest entry (drafting deferred to v2)               |
| 11 | Respond urgent    | Slack alert + flag + digest                                 |
| 12 | Action            | Slack alert (v1) → Jira ticket (v2)                         |
| 13 | Skip / Unknown    | Leave in inbox + digest "couldn't classify" section         |

## Inbox-as-work-queue invariant

A message stays in the inbox if and only if **someone (the hand or the
user) still owes action on it**. The hand keeps Bucket 9 (Informational)
in the inbox as a deliberate exception — reading it IS the action you owe.

## Cost model

Most messages match an existing per-sender rule and bypass the classifier
entirely (zero LLM tokens). Novel senders trigger one classification call
(~2k tokens). Bulk operations (Sent-walk, ranking) live in a Python
sidecar with no LLM cost. The single largest LLM expense is the daily
digest writer — one consolidating call per project per account per day.

## MCP server requirements

| Server         | Used for                                                  |
|----------------|-----------------------------------------------------------|
| `rustymail`    | IMAP fetch + ManageSieve (`sieve_*` tools)                |
| `ms365`        | Graph API for the work account                            |
| `slack`        | Urgent-bucket alerts                                      |
| `mcp-atlassian`| Jira project list bootstrap; v2 ticketing                 |

A missing MCP server is logged but doesn't crash the hand; the affected
account / feature degrades gracefully.

## State (memory_store keys)

Detailed in design.md §7. Skeleton uses just two:

- `email_bootstrap_progress` — onboarding state machine, set on first run.
- `email_processing_state`   — last_run_iso, last_run_outcome, last_tick_kind.

## Operational notes

- Cursor advances **only** at the end of a successful incremental cycle.
- Backlog sessions claim messages via a per-message `training_lock` so
  the incremental cycle skips them.
- Per-tick caps in HAND.toml: max_messages_per_tick (50),
  max_novel_classifications_per_tick (10), max_digest_summary_calls_per_tick (30).
- Cleanup of leftover Sieve scripts is a manual action — see
  `tools/scan.py sieve-state` once that subcommand lands.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| No tick events on dashboard | `processing_mode = paused`, or schedules failed to register |
| `email_metrics_health = "skeleton: ..."` | Per-bucket handlers not yet implemented (expected) |
| `email_metrics_last_run` stale | Hand crashed mid-cycle, or kernel didn't deliver tick — check OpenFang logs |
| Sieve push failing | Verify `rustymail` MCP is reachable (`curl http://localhost:9437/health`) |
