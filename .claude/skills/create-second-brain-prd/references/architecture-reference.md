# Architecture reference

What the generic core actually does, component by component, and the design
choices behind it. The skill reads this to ground every phase it writes in
mechanisms that exist and have been run in production, rather than in ideas.

## Project configuration

### CLAUDE.md

The one file the model reads at the start of every session. It holds current
conventions, commands, and boundaries. It is a state document: replace stale
lines, never append history to it. Build history lives elsewhere.

### Owner configuration

Every personal value is an environment variable, read once through a small
config module and cached. Name, timezone, account aliases, project roster,
notification targets, server details. Nothing personal is a literal in code, and
a test scans the generic files for known personal strings so it stays that way.

## Core components

### Memory layer

Plain markdown in a `vault/` folder, tracked in Git.

- `SOUL.md`: the assistant's voice and hard rules.
- `USER.md`: who the owner is.
- `MEMORY.md`: a short index, kept under 5 KB, whose job is to point at pages.
- `core-memories.md`: a few lines the owner writes by hand. Automation never edits it.
- `entities/`, `topics/`, `decisions/`: one page per subject with a dated
  "current status" section that gets replaced and a "decisions" list that only
  grows.
- `daily/YYYY-MM-DD.md`: append-only log. Everything automated writes here first.

Why an index plus pages instead of one growing file: one file becomes thousands
of lines, gets loaded whole every session, and eventually holds contradictions
nobody notices. An index stays small, pages are read only when their subject
comes up, and each fact has one home.

### Hooks

Three Claude Code hooks carry memory across sessions and guard the vault:

- Session start injects the identity files, the index, and the last few daily
  logs under a fixed character budget, cutting at a line boundary. A first-run
  onboarding file, if present, is injected first and asks the model to interview
  the owner and then delete it.
- Session end and pre-compaction flush a short summary of the session to the
  daily log, using a cheap model where one is configured.
- A pre-tool hook refuses reads and writes of credential files, dangerous shell
  patterns, and deletions under the vault. A second hook blocks background agents
  from editing the identity files. Hooks must not depend on the project's
  virtual environment, or they silently fail to start in a worktree.

### Memory search

A local index over the vault: full-text keyword search plus a small on-device
embedding model, blended. Results are pointers (file, heading, line range,
excerpt), not whole chunks, so the model reads the file for the rest. The
crawler indexes what Git considers part of the vault, so ignored and vendored
material stays out. Each machine builds its own index; the index is never synced.

### Integrations

One module per platform behind a registry that reports which are configured.
Gmail and Calendar go through a hosted OAuth broker so no token files live on
disk. Every integration wraps external text in the sanitizer before returning
it. A missing integration is a visible "not configured" state, never a silent gap.

### Heartbeat

The scheduled loop, every 30 minutes inside active hours:

1. Gather from every configured integration.
2. Diff against the previous snapshot. If nothing changed, stop. This is what
   keeps the idle cost near zero and stops repeat alerts.
3. Run the guardrail: a no-tools model call that reads only the external fields
   and returns pass, suspicious, or fail. Fail skips the run and logs why.
   Anything unclear counts as suspicious, never as pass.
4. Run the main agent over the delta, with tools.
5. Notify, and append to the daily log.

Every lane inside the loop runs through a feature wrapper that gives it an
on/off toggle and writes one timing line per run, including "disabled", so
"never ran" and "ran and did nothing" are distinguishable.

### Reflection

Once a day, a tool-using agent reads the daily log and moves durable facts to
the right page. A new page is created only after a subject appears in three
separate files; before that, it stays as a line in the index's active-items
queue. It may edit the index and pages, nothing else.

### Drafts

Outbound messages are files that move through `active`, `sent`, and `expired`
folders. Nothing is sent by the system. When the owner replies on the real
platform, the actual sent text is captured next to the draft, which is the
signal for improving the drafting instructions later.

### Chat

A long-running process that speaks Telegram or Slack. Each thread maps to a
persistent agent session. Sessions rotate after a few hours idle or a day old,
because a week-old session restates old web results as current news.

### Security, four independent layers

1. The credential hook: cannot read secrets, cannot run listed shell patterns.
2. The sanitizer: flags known injection phrasing in place instead of deleting
   it, escapes structure that could break out of a quote, and wraps external text
   in a labelled boundary tag. The trust instruction is placed after the data.
3. The guardrail: the only semantic check, before the main agent.
4. The write-protection hook: background agents cannot touch identity files.

### Infrastructure

Scheduled units for macOS and Linux are Git-tracked templates rendered per
machine by one script, with a diff command that must print "same" for every
unit. A second machine syncs the vault over Git every two minutes; daily logs
merge with an append-only merge driver.

### Shared utilities

Config access, vault paths, atomic JSON writes, optional-import helpers that let
a whole lane be absent, and the context builder the session hook uses.

## Design principles

- Markdown is the database. Anything derived (search index, snapshots) can be
  rebuilt from it and is never the source of truth.
- Advisor, not actor. It drafts and surfaces; a person sends.
- Every external byte is untrusted until it has passed through the sanitizer
  and the guardrail.
- Loud degradation. A missing key, module, or connection produces a visible
  state, never a quiet skip.
- Nothing personal in code. Names, accounts, and rosters are configuration.
- Small always-loaded context, large on-demand context.
