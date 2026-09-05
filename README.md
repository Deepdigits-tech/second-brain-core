# Second Brain — a proactive AI assistant on Claude Code

A local-first AI assistant built on Claude Code. It runs scheduled heartbeats over
your email and calendar, keeps long-term memory in a plain markdown vault, searches
that vault with hybrid keyword + semantic search, talks back over Telegram or Slack,
and reviews its own daily logs to promote what matters into long-term memory. Every
integration is optional — wire up what you use, skip the rest, and a missing one
degrades loudly instead of silently doing nothing.

> **This is a generic core, not a finished product.** It ships the mechanism —
> heartbeat, memory, search, chat, security — with no names, accounts, or personal
> content baked in. Config and memory files are where you make it yours.

### Want to build your own? → the PRD skill

This repo includes `.claude/skills/create-second-brain-prd/`, a skill that turns a
short requirements template into a phased build plan tailored to your platforms and
preferences.

1. Fill out the blank template the skill ships (`my-second-brain-requirements.md`) —
   your name, timezone, integrations, top tasks, proactivity level.
2. Run the skill in Claude Code: `/create-second-brain-prd ./my-second-brain-requirements.md`.
3. You get a multi-phase build plan with enough detail that Claude Code can implement
   each phase with minimal hand-holding.

Use the rest of this repo as a working reference for how the pieces fit together —
then build from your own PRD so the result reflects your own workflow, not this one.

## What it does

- **Proactive heartbeat** — checks email and calendar on a schedule, diffs state
  between runs so only new or changed items are surfaced, and runs a security
  guardrail over anything external before it reaches the main agent.
- **Long-term memory** — decisions, preferences, and context live as markdown files
  in a vault, synced across machines with Git.
- **Memory index + pages** — `SOUL.md`, `USER.md`, and `MEMORY.md` are read into
  every session; the rest of the vault is organized into entity, topic, and decision
  pages that are read on demand, not preloaded.
- **Hybrid search** — keyword (FTS) and semantic (local vector embeddings) search
  over the vault, blended and scoped to a folder when you need it. No API calls, no
  external index.
- **Chat interface** — talk to your assistant over Telegram or Slack; each
  conversation thread is a persistent Agent SDK session.
- **Daily reflection** — a scheduled pass reviews the day's logs and promotes what's
  durable into long-term memory, leaving the rest as history.
- **Draft management** — outbound email/message drafts move through active → sent or
  expired states so nothing is sent without you and nothing silently rots.
- **Guardrail + sanitizer** — a two-stage defense (pattern matching, then a
  cheap-model semantic check) screens all external content before the main agent
  sees it, and every integration wraps external text in a sanitizing, trust-boundary
  layer before it enters the conversation.
- **State diffing** — heartbeat snapshots are compared run over run so nothing
  repeats and nothing is missed.
- **Vault sync** — Git-based sync between machines with a merge driver that
  auto-resolves append-only daily-log conflicts instead of failing the merge.
- **Onboarding** — on first run, Claude interviews you to fill in your memory files
  and preferences, then is instructed to delete the onboarding file so it doesn't
  trigger again (check it did — see Quick Start).

## Architecture

| Component | Path | Purpose |
|---|---|---|
| Memory files | `vault/SOUL.md`, `USER.md`, `MEMORY.md` | Identity, user context, long-term memory index — read into every session |
| Daily logs | `vault/daily/YYYY-MM-DD.md` | Append-only event log; heartbeat and reflection write here |
| Heartbeat | `.claude/scripts/heartbeat.py` | Scheduled gather → diff → guardrail → notify loop |
| Reflection | `.claude/scripts/memory_reflect.py` | Daily pass promoting durable items from logs into memory |
| Memory index | `.claude/scripts/memory_index.py` | Builds the hybrid keyword + vector index over the vault |
| Memory search | `.claude/scripts/memory_search.py` | Queries the index; returns pointers, not payloads |
| Sanitizer | `.claude/scripts/sanitize.py` | Pattern-strip, markdown-escape, trust-boundary wrap for external text |
| Guardrail | `.claude/scripts/guardrail.py` | Deterministic + semantic screen on external content before the main agent |
| Hooks | `.claude/hooks/` | Session-start context injection, credential/command guardrail, write-protection |
| Chat server | `.claude/chat/` | Telegram/Slack bot, one persistent session per thread |
| Integrations | `.claude/scripts/integrations/` | Gmail, Calendar, Telegram, Slack wrappers; auto-detected via a registry |
| Feature runner | `.claude/scripts/feature_run.py` | Per-feature toggles, timing log, failure alerting |
| Launch units | `.claude/launchd/`, `.claude/systemd/` | Scheduling templates for macOS/Linux, rendered by `install_units.py` |
| PRD skill | `.claude/skills/create-second-brain-prd/` | Generates a personalized build plan from a filled-out requirements template |
| Build commands | `.claude/commands/build/` | prime / plan / execute / review slash commands for changing the system safely |
| Vault templates | `templates/vault/`, `templates/memory/` | Starter vault structure and memory files copied on first setup |

## Quick Start

```bash
git clone <this-repo>
cd <this-repo>

cp master.env.example master.env
# edit master.env with your own values

python3 setup_workspace.py
# creates vault/ from templates, writes the credentials file under .claude/,
# substitutes {{OWNER_NAME}} / {{TIMEZONE}} from master.env

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

claude
# first session runs the conversational onboarding (BOOTSTRAP.md)
```

> **Prerequisites:** Python 3.12+, Git, and the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
> logged in (run `claude` once and follow the browser prompt). For Gmail/Calendar you
> also need a free [Composio](https://composio.dev) account; it holds the Google OAuth
> grants so no token files land on disk here.

The onboarding file asks the assistant to delete itself when done — that's an
instruction the model follows, not code that runs automatically. If
`vault/BOOTSTRAP.md` is still there after your first session, delete it by hand
so onboarding doesn't trigger again.

Then bring the system up, one step at a time:

```bash
# 1. Connect Google accounts (only if you set COMPOSIO_API_KEY)
.venv/bin/python3 .claude/scripts/composio_setup.py --urls   # prints one OAuth link per account
.venv/bin/python3 .claude/scripts/composio_setup.py --list   # confirm each shows connected

# 2. Build the search index (first run downloads a ~45 MB local embedding model)
.venv/bin/python3 .claude/scripts/memory_index.py
.venv/bin/python3 .claude/scripts/memory_search.py "onboarding"   # should return a hit

# 3. Run one heartbeat by hand and read what it wrote to vault/daily/
.venv/bin/python3 .claude/scripts/heartbeat.py

# 4. Check everything at once
.venv/bin/python3 .claude/scripts/query.py --format text status
.venv/bin/python3 .claude/scripts/health_check.py

# 5. Schedule the background jobs
.venv/bin/python3 .claude/scripts/install_units.py list                    # what would be installed
.venv/bin/python3 .claude/scripts/install_units.py install --platform launchd                       # macOS
.venv/bin/python3 .claude/scripts/install_units.py install --platform systemd --user <service-user> # Linux
.venv/bin/python3 .claude/scripts/install_units.py diff                    # must print "same" for every unit
```

### What runs, and when

| Job | Schedule | What it does |
|---|---|---|
| Heartbeat | every 30 min, 6 AM to 10 PM | Pulls email + calendar, diffs against the last run, screens external text, notifies you only when something changed |
| Indexer | every 15 min | Incrementally updates the search index as the vault changes |
| Vault sync | every 2 min | Commits and pushes/pulls the vault so a second machine stays in step |
| Reflection | daily, 8 AM | Reads yesterday's daily log and promotes durable facts into memory pages |
| Hygiene | Sundays, 8 AM | Checks the memory index size, dead links, stale items, oversized pages |
| Chat server | always on (server) | Telegram/Slack bot; one persistent conversation per thread |

Each job is wrapped by `feature_run.py`, which writes one line per run to
`.claude/data/feature-timings.jsonl` (`ok`, `error`, or `disabled`). Toggle any job
off in `.claude/data/state/feature-toggles.json` without touching its code.

### Integrations

All integrations are optional. The registry in `.claude/scripts/integrations/`
auto-detects what's configured and skips the rest — a missing integration produces a
visible "not configured" state, never a silent gap.

| Integration | Config | Notes |
|---|---|---|
| Gmail / Calendar | `COMPOSIO_API_KEY` + per-account ids in `CORTEX_GMAIL_ACCOUNTS` | Routed through Composio; supports multiple accounts |
| Telegram | Bot token + chat id | Long-polling chat interface |
| Slack | Bot token (Socket Mode) | DM/mention chat interface |

## Personalizing

### Memory files

Everything the assistant knows about you lives in `vault/`. Onboarding fills in the
essentials; edit any of them directly at any time.

| File | What goes in it | Loaded when |
|---|---|---|
| `vault/SOUL.md` | The assistant's voice, values, and hard rules (advisor mode: it drafts, never sends) | every session |
| `vault/USER.md` | Who you are, what you work on, how you like to be talked to | every session |
| `vault/MEMORY.md` | A short **index** of subjects, each pointing at a page below; kept under 5 KB | every session |
| `vault/core-memories.md` | A handful of things you never want forgotten; only you write here, never automation | every session |
| `vault/HEARTBEAT.md` | The checklist each heartbeat works through; rendered from config, regenerate rather than edit | heartbeat runs |
| `vault/entities/`, `vault/topics/`, `vault/decisions/` | Detail pages: current status plus a dated decision log per subject | on demand |
| `vault/daily/YYYY-MM-DD.md` | Append-only event log; heartbeat, session flushes, and reflection all write here | on demand, via search |
| `vault/drafts/active`, `sent`, `expired` | Outbound drafts moving through their lifecycle | heartbeat runs |

Session start injects SOUL, USER, MEMORY, core-memories and the last three daily
logs, capped at 20,000 characters. Pages are never preloaded: the index tells the
model where to look, and it reads the page when a subject comes up.

### Other knobs

- **Memory files** — `vault/SOUL.md` (personality, values, advisor-mode rules),
  `vault/USER.md` (who you are, what matters), `vault/MEMORY.md` (the long-term
  index — kept small, pointing at detail pages). Edit directly or let onboarding
  fill them in.
- **Adding/removing integrations** — set or clear the relevant env vars; the
  registry and heartbeat pick up the change on the next run with no code edit.
- **Feature toggles** — each heartbeat lane is gated in `feature_run.py`; disable a
  lane without touching its logic.
- **Owner configuration** — `OWNER_NAME`, `TIMEZONE`, and the `CORTEX_*` /
  integration env vars in your credentials file are the whole personalization
  surface; nothing else needs editing to make this yours.

## Security

Four independent defenses, each doing one job:

1. **Credential-guard hook** — a `PreToolUse` hook blocks Read/Bash/Grep/Edit/Write
   access to the credentials file, tokens, keys, and other sensitive paths, and
   blocks a short list of destructive or credential-leaking shell patterns.
2. **Sanitizer** — every integration wraps external text (email, chat, calendar) in
   a three-layer pipeline: flag known injection patterns in place, escape markdown
   that could break out of context, and wrap the result in an explicit
   `<external_content source="...">` trust-boundary tag. Note the flag-not-strip
   design: the tags name the content as untrusted: they don't try to make it safe,
   they make sure the model never mistakes it for an instruction.
3. **Guardrail screener** — a fast, no-tool model pass reviews external content for
   injection attempts before the main agent ever sees it, as a second, semantic
   layer behind the deterministic sanitizer.
4. **Write-protection hook** — background agents (heartbeat, reflection) are
   blocked from editing the identity files (`SOUL.md`, etc.); only an interactive
   session, with a human present, can change them.

## VPS / always-on deployment

For a persistent instance that runs when your machine is off:

- Render systemd units from the same templates as the local launchd ones via
  `install_units.py`, so both machines run from one source of schedule truth.
- Run the chat server (`.claude/chat/`) as a long-lived service so Telegram/Slack
  threads stay responsive without a local machine open.
- Vault sync keeps the two machines' memory consistent over Git; each machine
  builds its own local search index rather than sharing one.

## Building on it: the four-command loop

`.claude/commands/build/` ships a small, opinionated way to change this system
without losing the thread between sessions. Each is a slash command in Claude Code.

| Command | When | What it does |
|---|---|---|
| `/build:prime` | start of every coding session | Reads CLAUDE.md, the core scripts, and runs the tests once so the model knows the baseline before touching anything |
| `/build:plan <request>` | before any change | Writes `docs/plans/<date>-<name>.md`: one task per test-plus-commit, with the real code in it, for a reader with no memory of the conversation |
| `/build:execute <plan>` | to build | Works the plan in order on a branch: failing test, smallest fix, full suite, commit. Refuses to improvise past the plan |
| `/build:review` | before merging | Reads the whole diff as the person who will maintain it, runs the suite, and returns a verdict with file-and-line findings |

The loop is deliberately boring: a fresh session, prime, plan, execute, review,
merge by hand. Long sessions drift; short ones with a written plan do not.

## Command reference

```bash
P=.venv/bin/python3

# Search the vault
$P .claude/scripts/memory_search.py "what did we decide about pricing"
$P .claude/scripts/memory_search.py -p vault/daily "standup"          # scope to a folder
$P .claude/scripts/memory_search.py -w type=decision "vendor"           # filter on frontmatter
$P .claude/scripts/memory_search.py --mode keyword "exact phrase"       # one half of the hybrid, for debugging
$P .claude/scripts/memory_index.py --incremental                        # refresh the index
$P .claude/scripts/memory_links.py dead                                 # wiki-links that point nowhere

# Ask the integrations directly (same data the heartbeat sees)
$P .claude/scripts/query.py --format text status
$P .claude/scripts/query.py --format text gmail triage --account personal
$P .claude/scripts/query.py --format text gmail read --id <message_id>
$P .claude/scripts/query.py --format text gcal today
$P .claude/scripts/query.py --format text gcal upcoming --days 3
$P .claude/scripts/query.py telegram send --text "hello from the CLI"

# Background jobs, by hand
$P .claude/scripts/heartbeat.py
$P .claude/scripts/memory_reflect.py --test        # print the reflection prompt, change nothing
$P .claude/scripts/hygiene.py --dry-run
$P .claude/scripts/health_check.py

# Security probe: feed the guardrail a suspicious text and read its verdict
$P .claude/scripts/guardrail.py < some-email.txt

# Chat server (foreground; on a server run it as a service instead)
$P .claude/chat/server.py
```

## Configuration reference

All of these go in `master.env` before running `setup_workspace.py`, which copies
them into the credentials file under `.claude/` (gitignored). Blank means "off".

| Variable | Required | Purpose |
|---|---|---|
| `OWNER_NAME` | yes | Your name, used in prompts and to tell your own messages from a correspondent's |
| `TIMEZONE` | yes | IANA name, e.g. `Europe/Berlin`. Drives daily-log dates, brief times, and the heartbeat's active hours |
| `CLAUDE_CODE_OAUTH_TOKEN` | for scheduled jobs | Long-lived Claude Code token so cron/launchd runs can authenticate without a browser; see `docs/SETUP.md` |
| `COMPOSIO_API_KEY` | for Gmail/Calendar | Composio holds the Google OAuth grants; no token files land on disk |
| `CORTEX_GMAIL_ACCOUNTS` | with Composio | `alias=composio_user_id,...`, one entry per inbox, e.g. `personal=me@example.com,work=me@example.org` |
| `CORTEX_CALENDAR_ACCOUNTS` | no | Aliases to read calendars from; defaults to the Gmail aliases |
| `CORTEX_OWNER_EMAILS` | with Composio | Your own addresses, so sent mail is not mistaken for a reply |
| `CORTEX_PRIMARY_ACCOUNT` | no | Default alias when a command names none; defaults to the first alias |
| `CORTEX_BUSINESS_ACCOUNT` | no | Where bookings and intros arrive, if not the primary |
| `CORTEX_PROJECTS` | no | `slug=description;...`, the project list prompts render so the model can file things |
| `CORTEX_COMPOSIO_CONNECTIONS` | no | Per-account connection ids for `composio_setup.py`; without it the script tells you which are missing |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | no | Cheap model for single-shot calls (guardrail, log flush). Unset = falls back to the Agent SDK |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | for Telegram | Bot token from BotFather; chat id restricts the bot to you |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID`, `SLACK_OWNER_USER_ID` | for Slack | Socket Mode bot (no public URL); owner id restricts it to you |
| `CORTEX_VPS_SSH_ALIAS`, `CORTEX_VPS_REPO_ROOT`, `CORTEX_VPS_USER` | for two machines | Lets the health check confirm the other machine is reachable |
| `CORTEX_DISABLE_MODULES` | no | Comma-separated kill switch for optional lanes, e.g. `drafts,guardrail` |

Everything above is the whole personalization surface. Nothing in the code needs
editing to make this yours.

## Troubleshooting

**Background jobs went quiet after a clone or a reboot.** Nine times out of ten it
is credentials or `PATH`, not code. `docs/SETUP.md` walks the four things a clone
does not give you, in the order they break.

**Search returns nothing.** The index has not been built on this machine, or the
embedding model is still downloading on first use. Run
`memory_index.py` in full, then search again. The index is machine-local by design;
a second machine builds its own.

**`query.py status` says an integration is not configured.** The matching variable
is blank or the Composio connection has not been made. `composio_setup.py --list`
shows the real connection state; `--urls` prints the fix.

**Heartbeat runs but never notifies.** That is usually correct: it only speaks when
something changed since the last run, and only between 6 AM and 10 PM in your
timezone. Read `vault/daily/<today>.md` to see what it saw.

**Heartbeat wrote a "Guardrail" entry and stopped.** The screener judged some
external text as an injection attempt and skipped that run on purpose. The daily
log names the source. Nothing was lost; the next tick re-evaluates.

**Onboarding fires every session.** `vault/BOOTSTRAP.md` still exists. Delete it.

**Chat bot receives but never replies.** On a server this is almost always an
expired Claude login. Refresh `CLAUDE_CODE_OAUTH_TOKEN` and restart the chat
service.

**Two machines fight over the vault.** Daily logs merge automatically (append-only
merge driver in `templates/vault/.gitattributes`). Anything else conflicting is a
real conflict: resolve it once, and prefer letting reflection, not both machines,
edit memory pages.

## Contributing

Not currently accepting external contributions — this is a reference architecture,
not a maintained package. Fork it and make it yours.

## License

MIT License (see `LICENSE`).
