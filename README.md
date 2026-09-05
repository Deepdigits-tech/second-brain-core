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

The onboarding file asks the assistant to delete itself when done — that's an
instruction the model follows, not code that runs automatically. If
`vault/BOOTSTRAP.md` is still there after your first session, delete it by hand
so onboarding doesn't trigger again.

Then bring the system up:

```bash
.venv/bin/python3 .claude/scripts/memory_index.py       # build the search index
.venv/bin/python3 .claude/scripts/heartbeat.py           # first heartbeat run (test it live)
.venv/bin/python3 .claude/scripts/memory_search.py "test" # confirm search returns something

.venv/bin/python3 .claude/scripts/install_units.py install --platform launchd   # macOS
.venv/bin/python3 .claude/scripts/install_units.py install --platform systemd --user <service-user>  # Linux
```

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

## Contributing

Not currently accepting external contributions — this is a reference architecture,
not a maintained package. Fork it and make it yours.

## License

Add your own license here before publishing.

## Origins and license

This started from the Dynamous community workshop
[claude-code-second-brain](https://github.com/dynamous-community/workshops/tree/bcce0e4c3c3125bfe9bcbf66bae2a0e206fc1cd8/claude-code-second-brain)
and grew into a hardened, generic core over several months of daily use.
Released under the MIT License (see `LICENSE`).
