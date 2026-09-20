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
- Vault sync: Git-based sync between machines on a short interval, with a custom
  merge driver that auto-resolves append-only daily-log conflicts by
  concatenating both sides rather than failing the merge
- Each machine builds and holds its own local search index — the index is not
  synced via Git, only the vault markdown is
- See `docs/SETUP.md` for what a clone does not give you (CLI auth, scheduling
  environment, integration OAuth) and how to bring a fresh machine up
