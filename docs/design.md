# Email Processing Hand — Design Spec

**Date:** 2026-05-03
**Status:** Design approved, awaiting user spec review before implementation planning
**Owner:** Terry Brugger
**Repo target:** `~/projects/email-processing-hand/` (new standalone Hand repo)
**Related repos:** `~/projects/rustymail/` (extension), `~/projects/ms-365-mcp-server/` (existing)

## 1. Overview

A new OpenFang Hand that classifies, monitors, and processes email across two accounts (one Microsoft 365 work account, one mailbox.org IMAP personal account). The hand runs in two modes:

1. **Backlog mode** — interactive training session driven from the OpenFang dashboard chat, sorting the inbox by user-selected criteria and learning per-sender / per-domain rules from user confirmation.
2. **Incremental mode** — autonomous scheduled processing of new mail (every 5 minutes by default) using rules learned during training and the LLM classifier for novel senders.

Source flexibility is achieved through MCP servers: the existing `ms-365-mcp-server` for the work account, and `rustymail` (extended with ManageSieve support) for the IMAP account. Bulk operations that don't require LLM judgment (sent-items walks for contacts, top-sender ranking, batch fetches) are offloaded to a Python sidecar to keep agent-loop token costs low.

## 2. Goals and non-goals

### Goals (v1)
- Process both MS365 and IMAP sources via MCP server abstraction.
- Classify every inbound message into one of 13 buckets and execute the matching action where appropriate.
- Support both backlog (interactive) and incremental (autonomous) processing in a single Hand.
- Generate per-account, per-project daily digests, sent self-to-self at a configurable time.
- Capture user feedback via a reply-to-digest correction loop with In-Reply-To matching.
- Raise urgent alerts via Slack with configurable channel + fallback chain.
- Synthesize Sieve scripts on the IMAP server for server-side filter rules; equivalent Graph rules on MS365.
- Maintain the invariant: **inbox = work queue.** A message remains in inbox iff some action is still owed (by hand or user); once handled, it leaves the inbox or stays only because the user still owes it action (read it / reply to it / decide on it).

### Non-goals (deferred to v2)
- Drafting response text for buckets 10 (Respond daily) and 11 (Respond urgent).
- Knowledge-base read (RAG grounding for drafts) or write (sanitized digest archiving to project SharePoint folders).
- Bucket 12 (Action) ticketing — creating or commenting on Jira tickets. v1 captures the classification and alerts; manual ticket creation.
- Personnel-sanitization pass for KB-archived digests.
- Tiered-model routing across Fast/Smart/Frontier tiers — v1 uses a single configured model. Revisit if token costs become painful.
- Multi-tenant; this hand is single-user.

### Out of scope entirely
- Direct IMAP/Graph access from anywhere except MCP servers. All email I/O goes through MCP, including from the sidecar.
- Replacing OpenFang's existing bundled `email-assistant` agent. That is an interactive chat-style agent; this Hand is autonomous and complementary.

## 3. Architecture overview

### Component layout

```
┌─────────────────────────────────────────────────────────────────────┐
│                       email-processing-hand                         │
│                       (~/projects/email-processing-hand)            │
│                                                                     │
│   HAND.toml — agent config, settings, dashboard metrics             │
│   SKILL.md  — operator notes, algorithms, troubleshooting           │
│   tools/    — Python sidecar scripts (mechanical bulk operations)   │
│   tests/    — sidecar unit tests + prompt eval harness              │
│   docs/     — design doc, this spec                                 │
└────────────────────┬────────────────────────────────────────────────┘
                     │
                     │ runs as agent inside openfang kernel
                     │
        ┌────────────┴────────────────────────────────────┐
        │                                                 │
        │   Hand agent (single LLM tier, configured       │
        │   in HAND.toml)                                 │
        │                                                 │
        │   Decisions: classify, resolve project,         │
        │   parse digest replies, draft digest text,      │
        │   ambiguous-message Q&A in chat                 │
        │                                                 │
        └────┬───────────┬─────────────┬──────────────────┘
             │           │             │
   MCP calls │           │ shell_exec  │ memory_store / memory_recall
   ─────────▼─────       ▼             ▼
   ┌────────────┐  ┌──────────────┐  ┌───────────────────────┐
   │ rustymail  │  │ tools/scan.py│  │ openfang memory store │
   │ (IMAP+     │  │ (bulk fetch, │  │  contacts (global)    │
   │  Sieve)    │  │  Sent scan,  │  │  sender_rules.<acct>  │
   ├────────────┤  │  top-sender, │  │  project_map.<acct>   │
   │ ms365-mcp  │  │  bulk-delete │  │  digest_state.<acct>  │
   │ (Graph API)│  │  prep)       │  │  cursors.<acct>       │
   ├────────────┤  └──────────────┘  │  exclude_domains      │
   │ slack-mcp  │                    │  alert_log            │
   │ (alerts)   │                    └───────────────────────┘
   ├────────────┤
   │ mcp-       │
   │  atlassian │  (project list bootstrap, v2 ticketing)
   └────────────┘
```

### Responsibility split

- **Hand agent (LLM)**: makes *decisions* — classify a single message, resolve a project when deterministic rules can't, write summaries for digest entries, parse free-text correction replies, hold conversations in dashboard chat. Each agent loop iteration does at most one or two decisions, then defers back to mechanical work.
- **Sidecar (`tools/scan.py`)**: does the *iteration* — fetches batches, parses headers, walks Sent items, ranks senders, scans folders for cleanup, returns structured JSON. No classification logic, no LLM calls except for the dedicated `parse-feedback-reply` subcommand. Idempotent and stateless.
- **MCP servers**: do the *I/O* — RustyMail (IMAP read/write/Sieve), ms-365-mcp-server, slack-mcp-server, mcp-atlassian. All external email/network operations through MCP, including from the sidecar (sidecar imports a thin MCP stdio client).
- **Memory store**: holds *state* — global contact list, per-account rules and project maps, per-account digest drafts, UID cursors, training stats.

### Cost model intuition

Most messages match an existing per-sender deterministic rule and skip the classifier entirely. Novel senders trigger one LLM classification call (~2k tokens). Bulk operations (Sent-scan, ranking) cost zero LLM tokens because the sidecar handles them. Daily digest text generation is the largest single LLM cost — one consolidating call per project per account per day.

## 4. Repository layout

```
~/projects/email-processing-hand/
├── HAND.toml                  Hand manifest — agent config, settings, dashboard metrics, MCP server requirements
├── SKILL.md                   Operator-facing notes — algorithms, troubleshooting, schema reference
├── README.md                  Quick install + setup checklist (mailbox.org creds, MS365 OAuth, Slack token)
├── LICENSE
├── tools/
│   ├── scan.py                Sidecar entry point, dispatches subcommands
│   ├── lib/
│   │   ├── __init__.py
│   │   ├── mcp_client.py      Thin MCP stdio client used by the sidecar to call rustymail/ms365
│   │   ├── contacts.py        Sent-items walk → contacts list builder
│   │   ├── ranking.py         Top-sender-by-count and by-volume aggregators
│   │   ├── headers.py         Header parsing (From/To/Cc, List-ID, List-Unsubscribe, Importance, In-Reply-To)
│   │   ├── thread.py          Thread/conversation grouping + "show top 5 + summarize rest" helper
│   │   ├── domain.py          Project-resolution domain ranking + exclude-list logic
│   │   └── output.py          JSON envelope helpers (stable contract with the agent)
│   ├── requirements.txt       Python deps (imapclient, msal, html2text)
│   └── README.md              How to invoke the sidecar standalone (for debugging)
├── tests/
│   ├── unit/                  Sidecar unit tests (pytest)
│   ├── fixtures/              Sample raw RFC 5322 messages — mailing list, receipt, urgent, calendar invite, thread
│   ├── prompt/
│   │   └── classification_eval.md   Hand-curated test cases for prompt evaluation (per-bucket goldens)
│   └── README.md
├── examples/
│   └── sieve_scripts/         Sample Sieve scripts the hand might generate
└── docs/
    └── design.md              This spec, copied into the hand repo on creation
```

Key choices:
- **`tools/scan.py` as a single dispatcher.** Agent calls `python3 tools/scan.py <subcommand> --input -` and reads JSON from stdout.
- **Sidecar uses MCP for I/O too.** Maintains "all email I/O through MCP" and benefits from RustyMail's connection pooling.
- **No Cargo.toml.** This Hand is pure prompt + Python. Rust work lives in the rustymail repo.

## 5. RustyMail ManageSieve extension

A new module added to the existing `~/projects/rustymail/` repo. ~1–2 days of work.

### Files added

```
~/projects/rustymail/src/
├── managesieve/                       NEW: ManageSieve client
│   ├── mod.rs                         pub use of client + types
│   ├── client.rs                      Async ManageSieve client (TCP+TLS+SASL+command loop)
│   ├── commands.rs                    LISTSCRIPTS, GETSCRIPT, PUTSCRIPT, SETACTIVE, DELETESCRIPT, CHECKSCRIPT, HAVESPACE, NOOP, LOGOUT
│   ├── parser.rs                      Wraps the `managesieve` crate; converts wire types to rustymail's domain types
│   ├── types.rs                       SieveScript { name, body, active }, Capabilities, Error
│   └── error.rs                       Error variants mapped from RFC 5804 response codes
└── mcp/adapters/sieve.rs              NEW: MCP tool definitions for sieve operations
```

### MCP tools added

| Tool | Purpose | Maps to |
|------|---------|---------|
| `sieve_list_scripts` | List scripts on the server with active flag | `LISTSCRIPTS` |
| `sieve_get_script` | Fetch a script's body by name | `GETSCRIPT name` |
| `sieve_put_script` | Upload or replace a script (also implicit syntax check) | `PUTSCRIPT name body` |
| `sieve_check_script` | Validate without uploading | `CHECKSCRIPT body` |
| `sieve_set_active` | Activate (or deactivate with empty name) | `SETACTIVE name` |
| `sieve_delete_script` | Remove a script (must not be active) | `DELETESCRIPT name` |
| `sieve_capabilities` | Return server capability list | Initial greeting |

### Hand-side Sieve management policy

- The hand owns **one script** named `openfang-managed.sieve` containing all hand-managed rules.
- The hand reads the script at startup (or creates it empty if missing).
- It keeps a copy in memory (`sieve_state` key) with metadata: rule_id → (sender_or_domain, action, learned_at, source_message_id).
- On every rule mutation the hand regenerates the entire script body deterministically from in-memory state, runs `sieve_check_script` to validate, then `sieve_put_script` followed by `sieve_set_active` to atomically swap.
- The hand never touches scripts it didn't create. If the user has their own active script, the hand activates `openfang-managed.sieve` only after asking and offers to merge or `include` the user's script.

### Sieve script template

```sieve
require ["fileinto", "imap4flags", "envelope", "regex"];

# ─── Hand-managed rules ─── DO NOT EDIT BY HAND
# This script is regenerated by openfang email-processing-hand.
# Last updated: <ISO timestamp>
# Rule count: <N>

# Rule openfang-rule-0001 (Blacklist: domain @noreply.spam-vendor.example)
if envelope :domain "from" "noreply.spam-vendor.example" {
    discard;
    stop;
}

# Rule openfang-rule-0002 (Filter: file from receipts@stripe.com to "Receipts")
if address :is "from" "receipts@stripe.com" {
    fileinto "Receipts";
    addflag "\\Seen";
    stop;
}
```

### RustyMail testing

- **Unit:** parser round-trips per command/response pair using `managesieve` crate's wire grammar.
- **Integration:** Docker fixture using Dovecot+Pigeonhole image gated behind `--features integration-sieve`.
- **Manual:** `cargo run --bin sieve-cli -- --account default --command list-scripts` for development.

## 6. Python sidecar (`tools/scan.py`)

### Invocation contract

```bash
python3 tools/scan.py <subcommand> --input - <<<"<json-input>"
```

stdin = input JSON, stdout = output JSON, exit 0 = success. Stable response envelope:

```json
{
  "status": "ok" | "error",
  "subcommand": "<name>",
  "request_id": "<uuid>",
  "elapsed_ms": 1234,
  "result": { ... } | null,
  "error": { "code": "...", "message": "..." } | null,
  "metrics": { "messages_scanned": N, "mcp_calls": M, "result_bytes": N, "result_tokens_estimated": N }
}
```

The request envelope includes a `result_format` field (default `"json"`, reserved alternative `"toon"`) so the result body's encoding is swappable without changing the wrapper. v1 ships with JSON only; TOON is held as a future optimization for array-heavy subcommands (`fetch-batch`, `rank-senders`, `contacts-bootstrap`) gated on observed token-cost data — see Section 14 future-revisit notes.

### Subcommands

#### 1. `contacts-bootstrap`
Walk Sent items in both accounts, build the global contact list. Input: `{"accounts": [...], "since": null}`. Output: `{"contacts": [...], "scan_summary": {...}}`. Agent stores result in `email.contacts`.

#### 2. `contacts-refresh`
Incremental update for new sent messages since last bootstrap. Input: `{"accounts": [...], "since": "<iso>"}`. Output: `{"new_addresses": [...], "updated_addresses": [...]}`.

#### 3. `rank-senders`
Powers backlog-mode sort orders 3–6. Input: `{"account": "...", "scope": "inbox|all_folders", "metric": "count|volume", "limit": 100, "exclude_already_processed_until": "<iso>"}`. Output: `{"ranking": [{sender, message_count, total_bytes, sample_subjects, oldest, newest}, ...], "scan_summary": {...}}`.

#### 4. `fetch-batch`
Fetch + parse + render batches of messages. Input includes `selector` (uids / search / thread / from_sender), `max_messages`, `render_body`, `strip_quoted`, `max_body_chars`. Output: `{"messages": [{uid, message_id, headers, body_text, body_truncated, has_attachments, size_bytes, folder}, ...]}`. Header set always includes `To, From, Cc, Subject, Date, List-ID, List-Unsubscribe, In-Reply-To, References, Importance, X-OpenFang-Digest-ID`. Body rendering: HTML→plaintext via `html2text`, quoted-reply stripping via heuristics.

#### 5. `parse-thread`
Group messages into threads, return "show top 5 + summarize rest" structure. Input: `{"account": "...", "thread_root_uid": N, "max_displayed": 5}`. Output: `{thread_id, displayed: [...full messages...], elided: [{subject, from, date}, ...], elided_count, total_count}`.

#### 6. `resolve-domain`
Project-resolution domain ranking — zero LLM cost. Input: `{from, to, cc, user_domains, exclude_domains, project_map}`. Output: `{matched_project, ranked_domains, decision_trace}`. The trace explains why a project was picked or not.

#### 7. `prepare-bulk-delete`
Dry-run scan before bulk deletion of Blacklisted sender/domain. Input: `{account, criteria, scope}`. Output: `{match_count, folders, samples, estimated_storage_freed_bytes}`. Agent presents to user, calls actual delete only after explicit confirmation.

#### 8. `parse-feedback-reply`
Parse a digest correction reply. **Only subcommand that calls the LLM** (centralized prompt for digest-reply parsing). Input: `{digest_id, digest_entries, reply_text}`. Output: `{corrections: [{entry_id, new_bucket} | {rule: {...}}, ...]}`.

#### 9. `submit-unsubscribe`
RFC 8058 List-Unsubscribe-Post handling for Bucket 2 actions. Input: `{list_unsubscribe_url, list_unsubscribe_post}`. Output: `{status, attempted_methods, response_code}`.

#### 10. `parse-ical`
Parse iCalendar attachments for Bucket 4–7 (calendar-invite handling). Input: `{message_uid, account}`. Output: `{events: [{uid, summary, organizer, attendees, dtstart, dtend, status, sequence, method}, ...], max_event_date}`.

### Sidecar testing

Unit tests with `MockMcpClient` that replays canned MCP responses. Coverage targets:
- `test_headers.py` — RFC 8058, multi-line headers, encoded subjects.
- `test_domain.py` — ranking with various combinations: user-only, all-vendor, mixed, ties.
- `test_thread.py` — out-of-order, missing References, broken In-Reply-To chains.
- `test_ranking.py` — count + volume aggregation across folders.
- `test_contacts.py` — Sent-items walk, dedup, name normalization, multi-account merge.
- `test_feedback_parsing.py` — golden cases (uses real LLM with cached responses for deterministic CI).

Target: 80%+ branch coverage.

## 7. Memory schema

All persisted state in openfang's `memory_store`. Mixed-isolation: contacts global, everything else per-account.

| Key | Scope | Shape |
|-----|-------|-------|
| `email.contacts` | global | `{version, addresses: {<email>: {display_name, first_seen, last_seen, message_count, accounts}}, last_full_scan}` |
| `email.exclude_domains` | global | `{version, domains: [...], added_by_user_corrections: [...]}` |
| `email.user_domains` | global | `{domains: ["scalesology.com", "bruggerink.com", "fait.app"]}` |
| `email.account.<account>.sender_rules` | per-account | `{version, rules: [{rule_id, scope, value, kind: "deterministic"\|"content_dependent", bucket?, eligible_buckets?, ineligible_buckets?, options?, learned_at, source_message_id, confirmed_count, corrected_count}]}` |
| `email.account.<account>.project_map` | per-account | `{version, domain_to_project, fallback_to_chat_count, last_jira_refresh}` |
| `email.account.<account>.cursor` | per-account | `{last_processed_uid, last_processed_internaldate, last_run, last_run_outcome}` |
| `email.account.<account>.digest_state` | per-account | `{drafts: {<project_or_personal>: {draft_id, ms365_draft_id?, imap_draft_uid?, entries: [{entry_id, message_ref, bucket, rich_summary, added_at, thread_id}], last_updated, scheduled_send}}}` |
| `email.account.<account>.sieve_state` | per-account, IMAP only | `{version, active_script_name, rules_in_script: [{rule_id, sieve_block_hash}], last_sync_to_server}` |
| `email.account.<account>.training_state` | per-account | `{current_session: null \| {started_at, sort_order, current_position, messages_processed, rules_learned}, lifetime_stats}` |
| `email.account.<account>.alert_log` | per-account | ring buffer (last 200) `[{alerted_at, channel, bucket, message_ref, subject}]` |
| `email.account.<account>.unsubscribe_log` | per-account | `{<list_id>: {last_attempt, status}}` |
| `email.global.run_metrics` | global | rolling 30-day `{<date>: {incremental_runs, messages_classified, rules_applied, corrections_received, digests_sent, alerts_sent, sidecar_calls}}` |
| `email.global.last_error` | global | last error only (history goes to event bus) |
| `email.global.bootstrap_progress` | global | `{step: 1..6, completed_steps: [...], pending_user_input}` |

### Versioning, atomicity, retention

- Every blob carries a `version`. Hand startup checks each blob and either upgrades or refuses to start with a clear error if from a future version.
- No cross-key atomicity. Cursor updated **last** at the end of each cycle; all email-server actions are idempotent (move-to-already-there is no-op, delete-of-deleted is no-op). Sender rule writes happen **before** Sieve push; Sieve is a regenerated artifact, not a correctness primitive.
- `run_metrics` is a 30-day rolling window. `alert_log` is a 200-entry ring buffer. `digest_state.archive` retains 14 days for late corrections. Everything else retained indefinitely (training data).

### Scaling

- Contact list ~5 KB / 100 contacts → 10k lifetime contacts ≈ 500 KB. Within memory_store limits.
- Sender rules ~few hundred bytes / rule → 5k rules ≈ 1.5 MB. Monitor.
- If memory_store size limits hit, future migration: chunk by hash prefix (`email.contacts.<aa-zz>`).

## 8. Mode lifecycle

### Cursor partitioning

- **Incremental mode** processes messages *after* the cursor (cron-driven, autonomous, ~5 min).
- **Backlog mode** works on messages *at or behind* the cursor (chat-driven, interactive).
- Cursor only advances during incremental cycles.
- Per-message `training_lock` lets backlog session claim specific messages so incremental skips them.

### Incremental cycle

```
Phase 0 — State recovery
  • memory_recall: contacts, exclude_domains, user_domains
  • Per account: sender_rules, project_map, cursor, digest_state, training_state
  • Read HAND.toml settings
  • If training_state.current_session is fresh (<10 min) → skip-overlap mode

Phase 1 — Per-account delta fetch
  For each account:
    • Sidecar fetch-batch with selector "since cursor", max_messages=50
    • If 0 new → next account

Phase 2 — Pre-classification routing
  For each message (date ascending):
    • Digest reply? (In-Reply-To matches our digest_id)
        → sidecar parse-feedback-reply → apply corrections → archive → continue
    • Sender on training_lock? → skip → continue
    • Deterministic rule applies? → execute action (no LLM call) → continue
    • Content-dependent rule applies? → classifier with restricted eligible_buckets
                                       → execute action → continue
    • Otherwise: full classification pipeline (Section 9)

Phase 3 — Urgent dispatch
  • Bucket 11 (Respond urgent) / Bucket 12 (Action — v1 alerts only, no ticket creation) → Slack via slack-mcp
  • Dedup against alert_log

Phase 4 — Sieve regen (IMAP only)
  • If sender_rules changed and account is IMAP:
      → regenerate openfang-managed.sieve from in-memory state
      → sieve_check_script → sieve_put_script → sieve_set_active
      → update sieve_state

Phase 5 — Cursor + metrics commit
  • Update cursor, run_metrics, last_run, last_run_outcome
  • If errors but partial work done → outcome=partial
```

### Per-tick budget caps

- `max_messages_per_tick = 50` per account.
- `max_novel_classifications_per_tick = 10` per account.
- `max_digest_summary_calls_per_tick = 30`.

If a budget is hit, cycle exits Phase 2 mid-batch, commits cursor only for fully-processed messages, logs `outcome=partial`.

### Backlog session

User-initiated via dashboard chat ("Train", "Backlog mode work, top sender by count", etc.).

```
Step A — Session initiation
  • Hand asks for account + sort order (1 of 6 from spec)
  • training_state.current_session set; incremental defers overlapping messages

Step B — Cohort fetch
  • Sort orders 1–2: sidecar fetch-batch in chunks of 25, sorted by date
  • Sort orders 3–6: sidecar rank-senders → iterate sender-by-sender → fetch-batch per sender

Step C — Per-message presentation loop
  • Thread > 5: sidecar parse-thread → display top 5 + elided summary
  • Render to chat: requested headers + rendered body
  • Hand proposes bucket with brief reasoning (LLM call)
  • User confirms / corrects / asks question / pauses / skips
  • On confirm: apply action, persist rule (asks scope if uncertain), advance
  • On correct: same with corrected bucket
  • On pause: save current_position, exit cleanly (resumable)

Step D — Round transition
  • Hand summarizes round
  • "Continue / change sort / pause?"

Step E — Session end
  • Update training_state.lifetime_stats
  • Sieve regen if IMAP rules learned
  • Publish summary event
  • current_session = null
```

### Schedule setup

The hand declares `schedule_create`, `schedule_list`, `schedule_delete` in its `tools = [...]` list and sets `schedule_mode = "reactive"` in `[agent]` (matching the timesheet-sync and system-update patterns). The schedules themselves are created at first-run by the system prompt's Phase 1 logic, which calls `schedule_create` for each:

| Schedule name | Interval | Trigger message | Phase |
|---------------|----------|-----------------|-------|
| `email-incremental-tick` | `interval_minutes = 5` | `"tick: incremental"` | Always-on |
| `email-daily-digest-tick` | `interval_minutes = 1440`, note `"07:00 daily"` | `"tick: daily-digest"` | Always-on |
| `email-contacts-refresh-tick` | `interval_minutes = 1440`, note `"03:00 daily"` | `"tick: contacts-refresh"` | Always-on |

On every first-run, the hand calls `schedule_list` to check for existing schedules with these names, then `schedule_create` for any missing. On settings change (e.g., `incremental_interval_minutes` updated), the hand calls `schedule_delete` followed by a fresh `schedule_create` to reflect the new interval.

The system prompt dispatches on the trigger message text — `if message starts with "tick: incremental"` → run incremental phases; `"tick: daily-digest"` → digest writer; `"tick: contacts-refresh"` → sidecar contacts-refresh; otherwise → conversational mode (training, ambiguous handling, ad-hoc questions).

### First-run bootstrap

Triggered when cursor empty for any account. State stored in `email.global.bootstrap_progress` (resumable).

1. **MCP server connectivity check** — ping rustymail, ms365, slack, atlassian; report failures with remediation hints.
2. **Account configuration** — confirm both accounts, IMAP server (`imap.mailbox.org`), ManageSieve server (`sieve.mailbox.org:4190`), creds reference.
3. **User identity** — confirm `user_domains` (`scalesology.com, bruggerink.com, fait.app`); ask for additions.
4. **Slack alert target** — channel ID + send test alert; confirm receipt.
5. **Project bootstrapping** — pull active Jira projects via mcp-atlassian; ask which warrant their own digest; ask for non-Jira projects with their domain/keyword mappings; persist `project_map`.
6. **Initial scan trigger** — sidecar `contacts-bootstrap` (full sent-items walk); sidecar `rank-senders` for high-confidence rule suggestions; cursor set to "now".

### Failure recovery

- Hand crashes mid-cycle → cursor not updated → next tick reprocesses; idempotent actions make this safe.
- MCP server down → abort that account/component; continue others; retry next tick.
- LLM down/rate-limited → cycle aborts after retry; Slack alert if downtime > 30 min.
- Sidecar fails → cycle aborts; full stack to event bus + dashboard health red; pause scheduling until recovery.
- Sieve push fails → rules still applied client-side via classifier; retry next cycle.

## 9. Classification pipeline

### Per-message decision flow (no existing rule)

```
Step 1 — Build classification context
  • Sidecar fetch-batch (already done in Phase 1)
  • Sidecar resolve-domain → matched_project, ranked_domains
  • email.contacts lookup → contact_status
  • Calendar invite detection (Content-Type: text/calendar / .ics / Method)
  • Mass-mail signal (List-ID / List-Unsubscribe / sender-role pattern)
  • Urgency signal — TONE-AND-CONTENT IS PRIMARY:
      Server flags (Importance: high, X-Priority: 1, "URGENT" in subject)
      are NOISY. The classifier prompt explicitly down-weights them and
      relies on tone, content, sender baseline, project context.
      Cheap-heuristic VIP override (CEO / spouse / on-call) bypasses
      the LLM and routes to RespondUrgent immediately. VIP list is small
      and explicit; not learned from headers.

Step 2 — Single LLM classification call
  Input: context + recent rules summary + bucket definitions
  Output: {
    bucket: "<one of 13>",
    confidence: 0.0..1.0,
    rule_scope: "sender" | "domain" | "thread" | "none",
    rule_kind: "deterministic" | "content_dependent",
    rule_value: "...",
    eligible_buckets?: [...],   // when content_dependent
    reasoning: "<one sentence>",
    options: { ...bucket-specific... }
  }

Step 3 — Confidence gate
  • ≥ 0.85 → execute action immediately
  • 0.6..0.85 → execute + flag in digest spot-check section
  • < 0.6 → reroute to Bucket 13 + post to dashboard chat

Step 4 — Action handler dispatch (one per bucket)

Step 5 — Persist learning
  • Add/update rule per scope+value+kind
  • Increment confirmed_count or corrected_count
  • If IMAP and deterministic → mark sieve dirty
```

Confidence thresholds are HAND.toml settings, tunable.

### Rule kinds

```json
// Deterministic — same bucket every time (Blacklist, Filter, Invite-Accept, etc.)
{
  "rule_id": "...",
  "scope": "sender" | "domain",
  "value": "...",
  "kind": "deterministic",
  "bucket": "Filter",
  "options": { "folder": "..." }
}

// Content-dependent — sender approved but each message's bucket varies
{
  "rule_id": "...",
  "scope": "sender" | "thread",
  "value": "...",
  "kind": "content_dependent",
  "eligible_buckets": ["Summarize", "Informational", "RespondDaily", "RespondUrgent", "Action", "Skip"],
  "ineligible_buckets": ["Blacklist", "UnsubAndBlock", "Filter"]
}
```

**Effect:**
- Deterministic match → execute action, no LLM call. Cheap path. Most messages.
- Content-dependent match → classifier still runs but restricted to `eligible_buckets`. Faster, more accurate.
- No rule → full pipeline.
- Only deterministic rules push to Sieve / Graph rules.

### Rule scope decision

When LLM is uncertain about scope (sender vs. domain vs. thread), hand asks user during training. In incremental mode with confidence ≥ 0.85, trust the LLM's scope guess; corrections refine later.

### Per-bucket action handlers (v1)

#### 1. Blacklist
- On rule creation: sidecar `prepare-bulk-delete` → ask via chat to confirm folder-wide deletion + optional domain expansion.
- Execute deletion; persist sender or domain rule; Sieve `discard; stop;` block; MS365 add-to-blocked-senders via Graph.
- Rule already exists: delete + mark Seen, no chat. Sieve already rejecting at server.

#### 2. Unsubscribe and Block
- Heuristics: mass-mail (List-ID / List-Unsubscribe / sender role) vs. specific-to-user (no List-* / `orders@` / `support@`).
- Clean pattern → domain-scoped rule targeting only mass-mail role.
- Messy pattern → per-message rule + ongoing case-by-case in digest.
- Submit unsubscribe via sidecar `submit-unsubscribe` (RFC 8058) for active lists (within `unsubscribe_lookback_months`).
- Bulk delete past mass-mail (sidecar prepare → confirm → execute).
- Keep specific-to-user.

#### 3. Filter
- Hand asks: "File from `<sender>` to which folder?" with tab-completion of existing folders.
- Move + flag Seen; persist deterministic rule.
- Sieve `fileinto` (IMAP) / Graph `create-mail-rule` (MS365).

#### 4. Invite out-of-date
- Sidecar `parse-ical` → max event date.
- If past: archive (if contact) or delete.
- No rule (per-message logic).

#### 5. Invite ask
- Leave in inbox.
- Append to digest "Pending invites — your call".
- Persist sender rule `{kind: deterministic, bucket: InviteAsk}` (per spec: always ask once categorized).

#### 6. Invite accept
- Check calendar conflict via MS365 `get-calendar-view`.
- No conflict → `accept-calendar-event`.
- Conflict → re-bucket as Invite ask.
- Persist sender rule `{kind: deterministic, bucket: InviteAccept}`.

#### 7. Invite unknown
- Classifier already evaluated. Clear spam → delete (no response).
- Else → re-bucket as Invite ask.
- No rule.

#### 8. Summarize
- LLM call → rich-detail summary (see two-stage summarization below).
- Append to project digest.
- Archive original.
- Persist rule (often content-dependent for senders whose mail varies).

#### 9. Informational ⚠ inbox-invariant exception
- LLM call → rich-detail summary.
- Append to digest.
- **Leave in inbox** — reading is the action you owe. Inbox-as-work-queue invariant generalizes to: in-inbox iff someone (hand or user) still owes action.
- Persist rule.
- Auto-archive after `informational_inbox_retention_days` if still in inbox (configurable, default 14).

#### 10. Respond daily (v1: classify + flag; drafting deferred to v2)
- Set Importance/Flag.
- Append to digest "Awaiting your reply".
- Persist rule (often `scope: thread` since respond-vs-not depends on content).

#### 11. Respond urgent (v1: classify + flag + alert; drafting deferred to v2)
- Set Importance/Flag.
- Slack alert with `urgent_alert_channel`; payload includes sender, subject, summary, dashboard + native-client links.
- Append to digest highlighted section.
- Dedupe via `alert_log`.

#### 12. Action / ticketing (v1: classify + alert; ticketing deferred to v2)
- Slack alert (treated as urgent per user decision: a "system is down" auto-ticket can't wait until 7am).
- Append to digest "Action items pending ticketing".
- Persist as classified-Action message.

#### 13. Skip / Unknown
- Leave in inbox unchanged.
- Append to digest "Couldn't classify".
- No rule.
- Re-prompt for classification after `skip_inbox_retention_days` (default 7).

### Rule scope guidance per bucket

- **Buckets 1, 2, 3, 6, 7**: deterministic, sender or domain.
- **Bucket 4**: no rule (per-message).
- **Bucket 5**: deterministic, sender.
- **Buckets 8, 9, 10, 11, 12**: most often content-dependent (same sender, different content). Pure sender-level deterministic only when sender's bucket is uniform.
- **Bucket 13**: no rule.

## 10. Daily digest + correction loop

### Two-stage summarization

**Per-message summarizer (classification time)**: errs toward more detail. Captures every distinct point — questions, answers, decisions, dates, action items, blockers. Output is a structured `rich_summary` blob (~600 tokens), not narrative prose:

```json
{
  "entry_id": "#xyz",
  "message_ref": "...",
  "from": "client@acme.com",
  "subject": "Q3 plan questions",
  "added_at": "<iso>",
  "thread_id": "...",
  "bucket": "Informational",
  "rich_summary": {
    "headline": "Client asked 10 questions about Q3 deliverables",
    "questions_asked": [...],
    "answers_given": [],
    "decisions": [],
    "action_items": [],
    "open_issues": [],
    "dates_referenced": [...]
  }
}
```

**Daily digest writer (7am)**: errs toward less, salient. Walks all entries per project, groups by thread, reduces. Computes deltas (questions − answers = outstanding) deterministically before LLM call when possible. Example output:

> **Acme Q3 questions** *(thread, 2 messages today)* — client asked 10 questions; team answered 7. Three still open: Q3, Q7, Q9.

### Digest scope and routing

- Per-account, per-project. Work account → one digest per active Jira project + Misc catch-all. Personal → one personal digest.
- Sent self-to-self per account.
- Subject prefix `[Digest – Acme] 2026-05-04`.
- Empty days for a project → no digest sent.

### Digest assembly is incremental

Built throughout the day in a Drafts-folder draft. 7am send is "finalize and send what's already 95% built" — fast LLM consolidating pass.

### Digest structure

```
[Digest – Acme] 2026-05-04

Overview
────────
• 17 messages processed for Acme today. 12 auto-handled, 4 need your eyes,
  1 ambiguous.

Urgent — replied?           ← still in inbox, Bucket 11
─────────────────
• Sam Long <sam@acme.com> — "DB connection failures since 9am" (in inbox, flagged)
  Sam reports intermittent timeouts on the staging DB starting 9:00 ET.

Awaiting your reply         ← still in inbox, Bucket 10
───────────────────
• Pam Masterson <pam@acme.com> — "Renewal terms"
  Pam asks for confirmation on renewal dates and pricing tier. Last reply 3 days ago.

Pending invites — your call ← still in inbox, Bucket 5
──────────────────────────
• Tuesday May 6 10am — "Acme Q2 review" from Pam (no conflict)

Action items pending ticketing ← Bucket 12 v1: alerts only
─────────────────────────────
• Sam — "Migration job failing" — would have opened a Jira ticket;
  please create manually (v1 limitation).

Informational  ← Bucket 9; messages still in inbox
─────────────
• Acme product release notes (Q1) — three new features summarized: X, Y, Z.

Summarized & archived  ← Bucket 8
─────────────────────
• Daily standup notes (Sam) — Sprint progress: 3/8 stories complete.

I auto-handled these — correct me if wrong  ← spot-check section
─────────────────────────────────────────
[#a] noreply@github.com → Filter to "GitHub-Acme" — first time seen
[#b] marketing@vendor.com → Unsubscribe-and-Block — clear list pattern, unsubscribed

Couldn't classify  ← Bucket 13
─────────────────
[#d] Unknown sender — "RE: Q3 planning" — could be follow-up to a thread I
     don't have context on. Left in inbox.

How to correct
──────────────
Reply to this digest with corrections in plain English:
"#a should be Skip" / "vendor.com → Blacklist" / "the Pam invite is always Accept"

— Email Processing Hand · auto-classified 14, learned 2 new rules today
```

`[#x]` letter-tags are stable per-digest identifiers cycling daily.

### Digest scheduling

Cron `daily-digest-tick` (default 7am local) → hand reads each draft, LLM consolidating pass, sends, moves draft to `digest_state.archive` (14-day retention for late corrections), starts tomorrow's draft.

### Correction loop

```
You hit Reply on a digest, type corrections in plain English.
        │
        ▼
Reply lands in inbox.
        │
        ▼
Next incremental tick (≤5 min):
        │
        ▼
Phase 2 sees In-Reply-To matches a digest_id from digest_state.archive
        │
        ▼
Skip normal classification → feedback handler.
        │
        ▼
Sidecar parse-feedback-reply (loads digest's entries, strips quoted
digest from your reply, calls LLM for structured corrections).
        │
        ▼
LLM returns structured:
  [
    { "entry_id": "#a", "new_bucket": "Skip" },
    { "rule": { "scope": "domain", "value": "vendor.com", "always_bucket": "Blacklist" } },
    { "entry_id": "#pam-invite", "rule_upgrade": {...} }
  ]
        │
        ▼
Hand applies each correction:
  • Update sender_rule (or domain_rule)
  • Re-action mis-classified messages (idempotent, graceful if already-actioned)
  • Increment corrected_count
  • Regenerate Sieve if applicable
        │
        ▼
Hand archives the feedback message (out of inbox).
        │
        ▼
Hand publishes "corrections applied" event to dashboard.
        │
        ▼
No acknowledgement email sent — keeps inbox clean.
```

### Correction loop edge cases

- Multiple replies to same digest → last-write-wins per entry.
- Unparsable reply → sidecar returns `corrections: []` with `unparsed_text`; hand posts to dashboard chat for clarification, archives.
- Question instead of correction → LLM detects question-shape, hand answers in dashboard chat, archives.
- Reply to digest > 7 days old → recover from `digest_state.archive` (14-day retention).
- Correction targets already-actioned message → re-action handlers idempotent, log "already-actioned", continue.

## 11. Alerting, bootstrap, and HAND.toml settings

### Slack alerting

Single setting `urgent_alert_channel` (Slack channel ID or DM target). Recommendation: dedicated DM-to-self channel.

Alert payload:

```
:rotating_light: *Email needs immediate attention*
*From:* Sam Long <sam@acme.com>
*Subject:* DB connection failures since 9am
*Bucket:* Respond urgent
*Project:* Acme

> Sam reports intermittent timeouts on the staging DB starting 9:00 ET.
> Asking if you want a quick rollback or a full investigation.

:openfang: <https://localhost:4200/agents/email-processing-hand/threads/abc123|Open in OpenFang>
:envelope: <ms-outlook://...|Open in Outlook>
```

Dedupe via `alert_log` (24h window per `message_ref`). Channel fallback: slack → event_publish (always available) → optional keybase/email.

### HAND.toml settings (v1 surface)

```toml
[[settings]]
key = "accounts"
default = ["work-ms365", "personal-imap"]

[[settings]]
key = "user_domains"
default = ["scalesology.com", "bruggerink.com", "fait.app"]

[[settings]]
key = "processing_mode"
options = ["incremental", "backlog", "paused"]
default = "incremental"

[[settings]]
key = "incremental_interval_minutes"
default = 5

[[settings]]
key = "digest_send_time"
default = "07:00"

[[settings]]
key = "digest_timezone"
default = "America/Chicago"

[[settings]]
key = "classification_confidence_high"
default = 0.85

[[settings]]
key = "classification_confidence_medium"
default = 0.60

[[settings]]
key = "vip_senders"
default = []

[[settings]]
key = "max_messages_per_tick"
default = 50

[[settings]]
key = "max_novel_classifications_per_tick"
default = 10

[[settings]]
key = "max_digest_summary_calls_per_tick"
default = 30

[[settings]]
key = "urgent_alert_channel"
options = ["slack", "keybase", "email", "event"]
default = "slack"

[[settings]]
key = "urgent_alert_target"

[[settings]]
key = "urgent_alert_fallback"
default = ["event"]

[[settings]]
key = "sieve_enabled"
default = true

[[settings]]
key = "sieve_managed_script_name"
default = "openfang-managed.sieve"

[[settings]]
key = "blacklist_require_confirmation"
default = false

[[settings]]
key = "unsubscribe_lookback_months"
default = 4

[[settings]]
key = "informational_inbox_retention_days"
default = 14

[[settings]]
key = "skip_inbox_retention_days"
default = 7

[dashboard]
[[dashboard.metrics]]
label = "Messages classified today"
memory_key = "email_metrics_classified_today"
format = "number"

[[dashboard.metrics]]
label = "Auto-handled today"
memory_key = "email_metrics_auto_handled_today"
format = "number"

[[dashboard.metrics]]
label = "Awaiting your reply"
memory_key = "email_metrics_awaiting_reply"
format = "number"

[[dashboard.metrics]]
label = "Rules learned (lifetime)"
memory_key = "email_metrics_rules_learned"
format = "number"

[[dashboard.metrics]]
label = "Last incremental run"
memory_key = "email_metrics_last_run"
format = "text"

[[dashboard.metrics]]
label = "Health"
memory_key = "email_metrics_health"
format = "text"

[[dashboard.metrics]]
label = "Tokens today (classification)"
memory_key = "email_metrics_tokens_classification_today"
format = "number"

[[dashboard.metrics]]
label = "Tokens today (digest writer)"
memory_key = "email_metrics_tokens_digest_today"
format = "number"

[[dashboard.metrics]]
label = "Tokens today (sidecar responses ingested)"
memory_key = "email_metrics_tokens_sidecar_today"
format = "number"
```

Setting types `multiselect` and `list_of_strings` may need to be added to OpenFang's settings system. Fallback: comma-separated strings.

## 12. Error handling and observability

### Error tiers

- **Tier 1 — transient.** Retry with exponential backoff (1s, 4s, 16s, max 3). Drop to Tier 2 on persistent failure. No user-visible alert.
- **Tier 2 — one-cycle persistent.** Abort affected component, log structured error to event bus, mark `last_run_outcome=error`, continue with other components. No alert.
- **Tier 3 — sustained outage (≥3 consecutive cycles).** Slack alert via fallback chain, dashboard health amber, continue retrying.
- **Tier 4 — corrupting/unsafe state.** Halt all email actions, dashboard health red, Slack alert with diagnostic. Continue dry-run classification so rules can still be queued for execution after resolution.

### Idempotency invariants

| Action | Mechanism |
|--------|-----------|
| Move to folder | Check current folder; no-op if already there |
| Delete message | Check existence; no-op if gone |
| Set flag | Naturally idempotent in IMAP/Graph |
| Sieve regen | Whole-script regen from in-memory state — same input, same output |
| Submit unsubscribe | Track `unsubscribe_log`; skip if attempted in last 30 days |
| Append to digest | Upsert by message_ref |
| Send Slack alert | Check `alert_log` for same message_ref in last 24h |
| Persist rule | Upsert by `(scope, value)` |

### Observability events

Hand emits structured events to OpenFang event bus:

- `email_classification_decision` — per-message
- `email_action_executed` — per-action
- `email_rule_learned` — per rule mutation
- `email_correction_applied` — per digest-reply parsing
- `email_alert_sent` — per Slack alert
- `email_digest_sent` — per dispatched digest
- `email_processing_error` — per Tier 2+ error
- `email_cycle_complete` — per tick

These feed dashboard panels and let future hands (e.g., "weekly review") subscribe.

## 13. Testing strategy

### Layer 1 — Sidecar unit tests (`tests/unit/test_*.py`)

Pytest, fast, no network. Each subcommand has fixture-based tests using `MockMcpClient`. Target: 80%+ branch coverage.

### Layer 2 — RustyMail ManageSieve tests

Already described in Section 5. Unit tests on parser; optional integration tests behind `--features integration-sieve`.

### Layer 3 — Prompt evaluation (`tests/prompt/classification_eval.md` + harness)

Curated test cases per bucket (one canonical per bucket = 13 cases). Plus edge cases:
- Urgent flag set on non-urgent message (must NOT classify as urgent).
- Urgent content with no flag (MUST classify as urgent).
- Invite-from-unknown clear-spam vs. legitimate.

Harness runs in CI when system prompt changes. Target: 90%+ accuracy. New misclassifications added when discovered in production via correction replies.

### Layer 4 — Live integration tests (manual, once per release)

Documented in `tests/README.md`. Test mailbox.org account + test MS365 account. Walk through bootstrap → backlog session → incremental tick → digest reply → urgent alert.

## 14. Deferred to v2 — explicit inventory

| Capability | v1 behavior | v2 work |
|------------|-------------|---------|
| Draft response generation (Bucket 10) | Flag + digest entry | Sidecar pulls thread + KB; LLM drafts; Drafts folder + linked from digest |
| Draft response generation (Bucket 11) | Flag + Slack + digest | Same as B10 + Slack includes draft preview |
| Knowledge base read | None | mcp-atlassian + SharePoint + repo retrieval; RAG context for drafts |
| Knowledge base write | None | Sanitized digest copies posted to `/{project-name}/daily-emails` |
| Personnel sanitization for KB digests | N/A | Sanitization pass removes performance-related staff content before KB write |
| Bucket 12 ticketing | Slack alert + digest "would have created ticket" | Search Jira → comment-on-existing or create-new → reply with ticket # → team-wide alert |
| Tiered model routing | Single tier | Three-tier: Fast for incremental, Smart for novel-drain, Frontier for digest |
| Source abstraction trait | Two MCPs called directly with conditional logic | `EmailSource` trait in shared crate; both MCPs implement it |
| Multi-tenant | Single user, two accounts | Future architectural shift |

### Future-revisit notes (not v2 commitments)

- Tiered model routing — revisit if token costs become painful.
- Memory store sharding — revisit if `email.contacts` exceeds 500 KB.
- IMAP-side filter coverage — revisit when Sieve regex limits become a blocker.
- Cross-thread conversation merging — currently a thread is a single In-Reply-To chain; per-sender / per-project running summary may be needed for long-term correspondences.
- Sidecar response format optimization — sidecar response envelope ships with `result_format: "json"` (default) and reserves `"toon"` as an alternative. If dashboard token-spend metrics show sidecar responses contributing >5% of total hand token cost, add a TOON encoder to `tools/lib/output.py` and toggle it on for the array-heavy subcommands (`fetch-batch`, `rank-senders`, `contacts-bootstrap`). Envelope and small responses stay JSON. Estimated work: ~half a day plus test fixture additions for the new format.

## 15. Implementation order (high-level)

This spec doesn't dictate the implementation plan (that's the next step), but the dependency order is:

1. **RustyMail ManageSieve extension** — unblocks IMAP-side filter installation. ~1–2 days, isolated PR in rustymail repo.
2. **Hand repo skeleton + HAND.toml + system prompt scaffolding** — get the hand registering with OpenFang and able to run a "tick: incremental" no-op cycle.
3. **Sidecar core (`scan.py`) — `fetch-batch`, `headers`, `resolve-domain`** — minimum to do real classification.
4. **Single-message classification pipeline + per-bucket action handlers** — the heart of the hand.
5. **Memory schema + rule persistence + Sieve regen** — durable behavior across restarts.
6. **Sidecar bulk subcommands (`contacts-bootstrap`, `rank-senders`, `prepare-bulk-delete`, `parse-thread`, `submit-unsubscribe`, `parse-ical`)** — needed for backlog mode and full bucket coverage.
7. **Backlog mode interactive flow + training_lock concurrency** — the user-driven training UX.
8. **Daily digest assembly + send + correction loop** — the core feedback mechanism.
9. **Slack alerting + dashboard metrics + observability events** — visibility.
10. **First-run bootstrap state machine** — onboarding experience.
11. **Live integration testing + prompt eval suite** — ship readiness.

## 16. Open questions resolved during design

The spec started with two open questions; both resolved:

- **Open Q1 (alerting mechanism):** configurable per `urgent_alert_channel`, default `slack`, fallback chain ending in `event` (always available).
- **Open Q2 (agent communication):** OpenFang dashboard chat, with the hand's agent. Same channel handles training feedback, ambiguous-message decisions, ad-hoc questions. Digest-reply correction loop complements with async-friendly text feedback. Inbox-as-work-queue invariant (with the generalization that "in-inbox iff someone owes action") provides the unifying mental model.
