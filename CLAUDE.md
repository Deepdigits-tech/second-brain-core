# Second Brain — Personal AI Assistant on Claude Code

A local-first memory system with proactive monitoring, email triage assistance,
hybrid memory search, and a chat interface. Built on Claude Code.

## Key Paths

- Memory vault: `vault/`
- Core files: `vault/SOUL.md`, `vault/USER.md`, `vault/MEMORY.md`
- `vault/core-memories.md` — user-initiated, tiny, always loaded
- Daily logs: `vault/daily/YYYY-MM-DD.md`
- Hooks: `.claude/hooks/`
- Scripts: `.claude/scripts/`
- Integrations: `.claude/scripts/integrations/`
- Skills: `.claude/skills/`
- Data/state: `.claude/data/`
- Chat server: `.claude/chat/`
- Launch unit templates: `.claude/launchd/`, `.claude/systemd/`
- Credentials file: under `.claude/` (gitignored — never commit it, never print it)
- Setup guide: `docs/SETUP.md` — read this first when scheduled jobs go quiet;
  most outages at this layer are credentials, paths, or environment, not code

## Conventions

- Timezone: read from `TIMEZONE` (IANA name, e.g. `America/Los_Angeles`) — never hard-coded
- Mode: Advisor — draft but never send or post on the owner's behalf without
  explicit approval
- No secrets in the vault — the credentials file stays outside `vault/`
- The memory index file (`MEMORY.md`) stays small — target well under the size
  that would blow the per-session context budget; detail lives in pages read on
  demand, not preloaded
- Daily logs are append-only, timestamped entries
- YAML frontmatter on all draft files
- `CLAUDE_INVOKED_BY` env var prevents hook recursion — a background agent sets
  it, and flush/reflection hooks check it before spawning another background
  agent off their own session
- Vault writes use file locking for concurrent-safety (two processes should never
  interleave an append to the same daily log)
- Background agents set `CLAUDE_INVOKED_BY` to enable write-protection on the
  identity files (`SOUL.md`, etc.) — only an interactive session can edit those
- State diffing: proactive checks only invoke the model when data has actually
  changed since the last run — near-zero cost when idle

## Security Boundaries

- Never send emails or messages without explicit permission
- Never post to social media
- Never modify files outside the memory vault without confirmation
- Never expose API keys or credentials to LLM context
- Never overwrite `SOUL.md`, `USER.md`, or `MEMORY.md` without confirmation
- Never push to the main branch without confirmation
- Never delete anything without confirmation

## Build Commands

Use a virtualenv Python (`.venv/bin/python3`) for everything below — a bare system
Python will be missing dependencies this project needs.

- `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

### Integrations

- Status: `.venv/bin/python3 .claude/scripts/query.py status`
- Gmail triage: `.venv/bin/python3 .claude/scripts/query.py gmail triage [--account NAME]`
- Gmail read: `.venv/bin/python3 .claude/scripts/query.py gmail read --id <msg_id>`
- Gmail draft: `.venv/bin/python3 .claude/scripts/query.py gmail draft --to <email> --subject <subj> --body <body>`
- Calendar today: `.venv/bin/python3 .claude/scripts/query.py gcal today`
- Calendar upcoming: `.venv/bin/python3 .claude/scripts/query.py gcal upcoming --days 3`
- Telegram send: `.venv/bin/python3 .claude/scripts/query.py telegram send --text "message"`
- Slack send: `.venv/bin/python3 .claude/scripts/query.py slack send --text "message"`
- Env vars: the credentials file under `.claude/` (gitignored)

All integrations are optional. The integration registry detects what's configured
and skips anything that isn't — a missing integration should degrade loudly (a
visible "not configured" state), never silently do nothing.

### Chat Interface

- Start chat server: `.venv/bin/python3 .claude/chat/server.py`
- Multi-adapter: the server runs Slack and/or Telegram in the same process —
  starts whichever has credentials configured
- **Slack**: Socket Mode — no public URL needed. Bot responds to DMs and
  @mentions. Each Slack thread is a persistent Agent SDK conversation.
- **Telegram**: long-polling. Replying to a bot message carries that message in
  as quoted context; every DM from the same chat resolves to the same
  conversation thread key — a fresh message does not start a new session.
- Chat sessions rotate on an idle timeout and a max age, checked on read (not
  only at server start), so a long-lived session doesn't quietly become stale
  context the model treats as current.
- An owner-restriction env var per platform (e.g. a single allowed user/chat id)
  is optional but recommended.

## Writing to the Vault

Every fact is **state** or **event**. The two have opposite update rules, and the
structure of the file should carry the rule — not a comment reminding you of it.

- **State** — one current value that changes: a status, a count, an owner, a
  rate, a next step, a file path. Find the matching line in the target file's
  `## Current State` and **replace** it. Always date it `(YYYY-MM-DD)`. Never add
  a second line for the same subject.
- **Event** — a thing that happened: a decision, a delivery, a lesson, a build
  phase. **Append** it to the `## Log` (or the daily log). Never edit or delete
  an existing entry.

If unsure, append to the log and say so. A missing state update is recoverable; a
rewritten history is not.

Where each lives:

| File | Kind | Rule |
|---|---|---|
| `vault/MEMORY.md` (index) | state | replace, dated; pointers only |
| `vault/entities/`, `vault/topics/`, `vault/decisions/` | mixed | detail pages, read on demand |
| `vault/daily/YYYY-MM-DD.md` | event | append only |
| `vault/core-memories.md` | remember-forever | user-initiated only; reflection never writes it |
| This file | state | replace — current conventions and commands only |

Never promote a live counter (credit balances, quota remaining, row counts) into
a state file. It decrements without anyone writing it back. Record where to look
it up instead.

## Proactive Systems

- Heartbeat run: `.venv/bin/python3 .claude/scripts/heartbeat.py`
- Reflection run: `.venv/bin/python3 .claude/scripts/memory_reflect.py`
- Reflection dry run (prompt only, no model, no edits): add `--test`
- Heartbeat state: `.claude/data/state/`
- Feature toggles and per-feature timing/failure tracking live in
  `.claude/scripts/feature_run.py` — a feature that fails repeatedly in a row
  should alert once, then auto-disable rather than fail silently forever
- Install/inspect scheduled units: `.venv/bin/python3 .claude/scripts/install_units.py {list|render|install|diff} --platform {launchd,systemd} [--user <service-user>]` (`--user` is for `systemd` deployments — the service user the units should run as)
- SOUL.md write-protection: a `PreToolUse` hook blocks background agents
  (heartbeat, reflection) from editing the identity files (`SOUL.md`, `USER.md`,
  `MEMORY.md`, `core-memories.md`) — only reflection may write `MEMORY.md`

### Heartbeat tick order and reliability

A tick gathers data, builds and diffs a snapshot against the last saved one,
and — only if something actually changed or a scheduled brief is due — runs
the guardrail gate and then the main agent. An idle tick costs zero model and
zero guardrail calls.

- **Fetch failures are distinguishable from an empty result.** Gmail/Calendar
  reads report `{account: "ok"|"failed"}` back to the caller
  (`gather_data()`'s `data["fetch_status"]`). A failed account's ids and
  calendar fingerprints from the previous snapshot are carried forward
  unchanged rather than diffed as "everything vanished" — a Composio outage on
  one account must never read as a wave of cancellations or a wiped inbox on
  that account. A consecutive-failure streak per source/account is tracked in
  sticky state for future alerting. The main agent's prompt gets a "Data gaps
  this run" section naming any account that failed to fetch, so it never
  claims an inbox or a calendar is empty when the truth is "didn't get an
  answer".
- **A moved or cancelled meeting is a real change, not silence.** The snapshot
  fingerprints each calendar event's start, end, status, location, and the
  owner's own RSVP; the diff between ticks reports `changed_events` (start
  moved, or another fingerprinted field changed) and `removed_events`
  (cancelled, or disappeared from the fetch window while still in the future —
  an event dropping off after it has already happened is normal, not a
  cancellation) separately from "new". The prompt renders both under their own
  headings.
- **The "N new since last check" count in the prompt matches what's actually
  listed.** The raw diff count is computed before the guardrail gate runs;
  what reaches the main agent is whatever survived the gate. The header is
  recomputed from the ids actually present in the gate-filtered data, not the
  pre-gate diff count, so it can never claim more items than are shown.
- **Send first, stamp only on success.** Every "did this actually reach the
  owner" check (a health-alert cooldown, a scheduled brief's once-a-day claim,
  a parsed `NOTIFICATION:` line's dedup) calls `notify()`, checks
  `notify.delivered()` on the result, and only then records the send. A local
  macOS notification succeeding is not enough on its own to count as
  delivered when Telegram was one of the attempted channels — Telegram is the
  channel every stamp actually promises, so its own result is authoritative
  whenever it was attempted; a channel-less or empty result is never
  delivered. A failed send leaves the dedup/cooldown/claim untouched so the
  next tick retries instead of silently losing the message for the rest of
  its cooldown window. A scheduled brief's claim is taken before the model
  runs (the prompt has to know whether to ask for one); if the model succeeds
  but the actual send then fails, the claim is released and the day's
  brief-sent stamp rolled back so the next tick reclaims and retries.
- **Per-thread ordering in the chat server.** `handle_message()` acquires an
  `asyncio.Lock` keyed by thread (refcounted so the lock dict doesn't grow
  without bound) before doing any work, so two messages arriving for the same
  conversation are processed one at a time — resuming the same Agent SDK
  session from two concurrent turns would let the later save clobber the
  earlier one. Different threads still run concurrently. An SDK error or a
  timeout deletes the session outright (a poisoned or partially-run session
  must not be resumed) and is never re-saved or refreshed afterward; a timeout
  reply says explicitly that some actions may already have run, since the
  turn could have executed tool calls before the cutoff.
- **Locking and writes.** `shared.file_lock()` is reentrant per thread (keyed
  by lock path) — a second acquire of the same path from the same thread
  inside the first is a no-op, not a second `flock()` call, which would
  otherwise deadlock the instant anything nests. Never `await` inside it: the
  reentrancy counter is per OS thread, and every coroutine on an event loop
  shares one thread. `shared.append_daily_log()` is the one path every daily-
  log writer (heartbeat, memory flush, chat capture) uses, so two different
  locking schemes can't fail to keep each other out; daily-log timestamps and
  filenames are always built from the owner's configured timezone, never a
  naive/UTC clock, so a server running in UTC doesn't write to the wrong day
  or stamp the wrong hour. `shared.atomic_write_text()` writes to a temp file
  in the same directory, fsyncs, then renames — no writer of a git-tracked
  JSON store should ever leave it torn by a crash mid-write.

- **The main model call has an overall timeout** (`HEARTBEAT_MODEL_TIMEOUT_S`,
  900s — comfortably under the 30-minute tick interval), wrapped in
  `asyncio.wait_for` and run under its own `feature_run("heartbeat.model_call",
  ...)` name so a hung call is visible to the same failure-streak watcher as
  every other lane, instead of blocking the tick (and the next scheduled
  timer run) forever. A timeout is handled exactly like an empty or failed
  model reply. `heartbeat.notify_dispatch` wraps the notification-send step
  the same way. `main()` now returns `True`/`False`/`None` on purpose — a
  quiet, nothing-to-do tick stays `None` (falsy, but not a failure), and only
  the outer exception handler returns `False`; `sys.exit(1 if main() is False
  else 0)` checks that exact value, not truthiness, so systemd/launchd can
  finally tell a crashed tick apart from an ordinary quiet one. The systemd
  heartbeat unit carries `TimeoutStartSec=1500`, comfortably above the
  model's own 900s budget so the unit's timeout is never what cuts a call off.
- **`CORTEX_BRIEF_OWNER`** overrides the platform rule (`is_brief_owner()`,
  used everywhere `BRIEF_OWNER_IS_VPS` used to be read directly) that decides
  which machine sends briefs and owns every Telegram push lane: `1` forces
  this machine to own them, `0` forces it off, unset keeps the platform
  default. Lets a backup machine take over when the usual owner's heartbeat
  is down, with no code edit — set it explicitly on both machines or you get
  either no briefs or duplicates.
- **Cross-machine dead-man's switch** (`peer_watch.py`, heartbeat lane
  `health.peer_watch`): each machine writes a tiny "I am alive" liveness
  stamp under `.claude/data/liveness/<machine>.json` (git-tracked — the
  existing 2-minute vault sync carries it to the other machine, no SSH
  involved) and, on the OTHER machine, checks whether its peer's stamp looks
  stale, is failing its model calls, or is missing today's brief. Staleness
  thresholds respect each machine's own active hours (a laptop sleeping
  overnight, or closed for an ordinary weekend, is not a problem; a
  should-be-always-on host going quiet during its own waking hours is) and a
  peer's stamp is treated as untrusted input — a clock more than 10 minutes
  in the future, or a timezone-less timestamp, is flagged rather than trusted.
  **Off by default on a single-machine install**: unset `CORTEX_PEER_MACHINE`
  means this stays a complete no-op — no state file is ever written, so a
  peer that will never exist can never be reported as overdue. Set
  `CORTEX_PEER_MACHINE` (to any non-empty value) on every machine once a real
  second one is in the picture. On first contact between two machines,
  neither has the other's stamp yet; a missing stamp is quiet until this
  machine has been watching for at least that peer's own stale threshold, so
  the very first tick after wiring two machines together does not false-alarm.
- **Nightly database backup** (`db_backup.py`, scheduled unit
  `com.secondbrain.db-backup` / `secondbrain-db-backup.service`+`.timer`):
  backs up sqlite databases under `.claude/data/` that are not tracked in
  git and not trivially rebuildable — `chat.db` here (`memory.db` is
  deliberately excluded: it is a derived search index, fully rebuildable
  from the vault's own markdown). Backups land under `~/.cortex-backups/
  <hostname>/<date>/`, verified with a sqlite integrity check, pruned past 14
  days, symlink-safe (never follows a link named like a date into deleting
  something else), and the run exits non-zero if nothing was actually backed
  up. Override the backup root with `CORTEX_BACKUP_DIR` (a bare `/` is
  refused).

## Memory Search

- Full reindex: `.venv/bin/python3 .claude/scripts/memory_index.py`
- Incremental reindex: add `--incremental`
- Search: `.venv/bin/python3 .claude/scripts/memory_search.py "query"`
- Scoped search: `--path-prefix <folder>`
- A search result is a **pointer, not a payload** — file, heading, and line range
  to judge relevance and then read directly, not the full chunk text by default.
- Each markdown note is chunked into passages; each chunk is stored twice — once
  as words (keyword/FTS index), once as meaning (local vector embeddings) — and a
  search blends both (weighted toward the vector side) so an exact-word miss can
  still surface a conceptually relevant note.
- Every chunk's text is a verbatim slice of its source file at a fixed line
  range — never reassembled from split pieces. Changing the chunking scheme
  forces a full reindex; an incremental run cannot repair a changed fingerprint.
- The embedding model runs entirely locally — no API call, no key, no network
  dependency for search itself.

### Guardrail (`guardrail.py`, `heartbeat.run_guardrail_gate`)

A no-tools, one-turn model call screens external content — Gmail + Calendar
fields only (`heartbeat.external_text_for_guardrail`) — before the main
heartbeat agent (which has Write/Edit) runs. Verdicts: `pass` (silent),
`suspicious` (a warning is prepended to the main agent's context prompt),
`fail` (the run is skipped, logged to the daily log under `## HH:MM —
Guardrail`, and Telegram-notified). Anything that is not a clean, parseable
verdict — an unreadable reply, an exception, an empty reply, an unknown word —
is `suspicious`: never promoted to `pass` (an outage must not disable the
defense) and never to `fail` (an outage must not silence the heartbeat). A
truncated model reply (cut off mid-`reason`) still recovers its verdict rather
than reading as unparseable.

The digest is capped at `GUARDRAIL_MAX_CHARS` (20,000); if the external content
for a tick would exceed it, the cut is made visible instead of silent — a
`pass` verdict on a truncated digest is demoted to `suspicious` with a reason
naming the truncation, since content past the cap was never actually screened.

Provider chain, same as the memory flush: OpenRouter single-shot when a key is
set, Agent SDK with no tools (`tools=[]`, not just `allowed_tools=[]` — the
latter only pre-approves, it does not remove the SDK's built-in tool surface)
and one turn as the fallback. Gate: `feature_run("guardrail.precheck")`,
placed after the "nothing changed" early exit so idle ticks cost nothing.
Manual probe: `.venv/bin/python3 .claude/scripts/guardrail.py < poisoned.txt`.

Not yet ported from the private build this project was extracted from:
per-item screening (chunking each email/event separately so one poisoned
message doesn't quarantine a whole batch), named-item quarantine vs.
whole-chunk defer, and the inbox classifier filtering that goes with it. The
guardrail here screens the whole external digest as one call.

## Hook Architecture

- Credential-guard hook: runs on every file-touching tool call, in every context
  (interactive or background). Blocks read/write access to the credentials file
  and other sensitive paths, and blocks a short list of destructive or
  credential-leaking shell command patterns.
- Identity write-protection hook: runs on Edit/Write/MultiEdit/NotebookEdit AND
  on Bash, background agents only (checked via `CLAUDE_INVOKED_BY`; an
  interactive session is never blocked). The Bash branch exists because the
  Edit/Write check alone does not see a shell command that reaches the same
  guarded files through `cp`, `mv`, `dd`, `tee`, `sed -i`, an inline
  interpreter one-liner, or plain `>`/`>>` redirection — it blocks on two
  independent signals: a guarded basename plus a write-capable shell
  construct, or an inline interpreter (`-c`/`-e`/heredoc) combined with a
  file-write primitive, which catches an obfuscated target that a name match
  alone would miss. A harmless redirect to `/dev/null` does not count as a
  write. This is defense in depth, not a sandbox — a write staged across two
  separate Bash calls, or through a construct this hook doesn't recognize,
  isn't caught by it.
- These two hooks are independent — one does not replace the other.
- **Hooks must never hard-require a virtualenv.** Invoke every hook through an
  interpreter fallback (project venv Python, else system `python3`) — a
  virtualenv is typically gitignored, so a fresh checkout or a git worktree has
  none, and a hook that can't start is skipped silently while the command runs
  anyway. Keep hook scripts stdlib-only so the fallback path is always safe.

## API Key Isolation

Integration scripts handle authentication and API calls directly. The LLM only
ever sees data returned by those scripts — never raw tokens or credentials. The
credentials file is loaded only by the integration registry's env loader, early,
before anything else runs.

## Deployment

- Local scheduling: launch unit templates in `.claude/launchd/` (macOS) or a
  cron/systemd equivalent, rendered and installed by `install_units.py`
- VPS/server deployment: run the chat server as a persistent service (systemd or
  equivalent) so chat threads stay responsive without a local machine open
- Vault sync (`vault_sync.sh`): Git-based sync between machines on a short
  interval. What it auto-commits is an **allowlist** (`vault`, `CLAUDE.md`,
  `.claude/data`) — everything else (scripts, hooks, chat code, skills, unit
  templates, docs) is left uncommitted for a human with a real commit
  message, logged rather than silently swept in. A stash-pop conflict is
  resolved, never discarded: a path matching `vault/daily/*.md` or
  `vault/MEMORY.md` (mirroring `vault/.gitattributes`' `merge=union` entries)
  keeps both sides' lines, everything else keeps the pulled version and saves
  the local one under `.claude/data/state/sync-conflicts/<ts>/`. A
  single-run lock (mkdir-based) keeps two ticks from racing each other; a
  human-owned merge/cherry-pick/revert/rebase is left untouched and reported
  once, never auto-aborted; a commit on `main` that auto-sync did not make
  itself holds the push (not the pull) until a person pushes it by hand, so
  code review isn't quietly bypassed by the sync cron.
- Each machine builds and holds its own local search index — the index is not
  synced via Git, only the vault markdown is
- Cross-machine liveness: see `peer_watch.py` under Proactive Systems above —
  off by default, opt in with `CORTEX_PEER_MACHINE` once a second machine
  exists
- Nightly database backup: see `db_backup.py` under Proactive Systems above
- See `docs/SETUP.md` for what a clone does not give you (CLI auth, scheduling
  environment, integration OAuth) and how to bring a fresh machine up
