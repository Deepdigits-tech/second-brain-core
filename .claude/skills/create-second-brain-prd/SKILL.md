---
name: create-second-brain-prd
description: Generate a personalized Second Brain PRD from a completed requirements template. Use when the user has filled out my-second-brain-requirements.md and wants to generate their build plan. Triggers on "create my second brain PRD", "generate my PRD", "build my second brain plan", "/create-second-brain-prd", or after completing the requirements template.
argument-hint: <path-to-requirements> [output-path]
---

# Second Brain PRD Generator

Generate a personalized Product Requirements Document for building an AI Second Brain, based on the user's completed requirements template.

**A blank template is bundled with this skill at `${CLAUDE_SKILL_DIR}/my-second-brain-requirements.md`.** Copy it to your workspace and fill it out before running this skill.

## Parameters

- **`$0`** (required) — Path to the filled-out requirements file (e.g., `./my-second-brain-requirements.md`)
- **`$1`** (optional) — Output path for the PRD. Defaults to `.agent/plans/second-brain-prd.md`

## Workflow

1. **Read the requirements** — Read the filled-out requirements file at `$0`. If no argument was provided, ask the user for the path. If they haven't filled one out yet, tell them a blank template is available at `${CLAUDE_SKILL_DIR}/my-second-brain-requirements.md` — they should copy it to their workspace and fill it out first.

2. **Load the architecture reference** — Read `${CLAUDE_SKILL_DIR}/references/architecture-reference.md` for the blueprint.

3. **Research ALL tools and APIs** — Do not assume familiarity with any platform or library. Even common APIs like Gmail or Slack have nuances, rate limits, and SDK-specific patterns that matter for implementation. For every tool in the user's stack, do web research to ensure the PRD contains accurate, specific guidance.

   **Always research these core dependencies:**
   - **Claude Agent SDK** — How to create conversations, system_prompt presets, setting_sources, allowed_tools, streaming responses, credential handling
   - **FastEmbed** — ONNX model loading, batch embedding API, model cache configuration, supported models
   - **Hook system** — Claude Code hook types (PreToolUse, PostToolUse, etc.), callback signatures, settings.json configuration

   **For every platform the user selected (Gmail, Slack, Linear, HubSpot, etc.):**
   - Authentication method (OAuth2 flow, API tokens, bot tokens, etc.)
   - Key SDK/library to use (e.g., google-api-python-client, slack_sdk, etc.)
   - The specific API endpoints needed for the user's top tasks
   - Platform-specific setup requirements (e.g., Slack Socket Mode needs an App Token + Bot Token, Gmail needs OAuth consent screen published to Production for custom domains)
   - Rate limits, pagination patterns, and common gotchas

   **The goal:** Every phase in the PRD should contain enough technical specificity that a coding agent can implement it without guessing. Don't bloat the PRD with raw research - distill it into actionable implementation notes per phase.

4. **Generate the PRD** — Create a phased build plan at the output path ($1, or `.agent/plans/second-brain-prd.md` if not specified) with these sections:

### PRD Structure

The output PRD should have:

**Header:**
- Project name (personalized: "[User's Name]'s Second Brain")
- Date generated
- Summary: 1-2 sentences based on their top tasks

**Phase 1: Foundation (Memory Layer)**
- Set up the memory vault folder using the name they specified in "Memory vault folder name" (e.g., `MyVault/Memory/`). Do NOT hardcode a fixed vault name — always use their chosen name.
- If they're using Obsidian, mention it as the viewer; if not, note that the vault is just a folder of markdown files that works with any editor.
- Create SOUL.md, USER.md, BOOTSTRAP.md, `daily/` structure, and — if they picked the index + pages layout in the requirements — MEMORY.md as a **≤5 KB index** (Active Items as a managed queue, plus links out) with detail pages under `entities/`, `topics/`, `decisions/`, and a small `core-memories.md` for explicit "remember this forever" facts, instead of one flat growing file. If they did not pick it, MEMORY.md is a single flat file and the pages/index split is out of scope for v1.
- Owner configuration lives in environment variables read at startup, not hardcoded constants: `OWNER_NAME` (human-readable), plus one `CORTEX_*`-style var per account/credential the user's platforms need (e.g. an accounts list for each multi-account integration, primary/business account designations, any remote-host aliases). Every script that needs to address "the owner" or pick a default account reads these — never a literal name or address baked into a module.
- BOOTSTRAP.md is a first-run onboarding script: on the user's very first Claude Code session, it drives an interactive conversation to personalize USER.md, SOUL.md, and HEARTBEAT.md (asks about name, timezone, role, communication style, integrations, proactivity preferences — one question at a time). It deletes itself after onboarding completes. If a session ends mid-onboarding, the file persists and picks up next time. The SessionStart hook should detect BOOTSTRAP.md and inject it into context.
- Create **CLAUDE.md** at the repo root — this is Claude Code's project instruction file, loaded into every conversation. Initialize it with: project description, key paths (vault, scripts, hooks, skills, data directories), project conventions (timezone, advisor mode, no secrets in vault, checkbox syntax, YAML frontmatter), and a "Completed Phases" section to be updated after each phase. Also add a "Build Commands" section — start with placeholder entries and populate with real commands as each phase introduces them. This file is the agent's reference for how to interact with the project.
- Customize each file based on their "About You" and "Memory Categories" answers
- Key files to create, estimated complexity: Low

**Phase 2: Hooks (Context Persistence)**
- SessionStart hook (inject memory into every conversation — should also detect and inject BOOTSTRAP.md for first-run onboarding). Session-start context is loaded under a fixed character budget (~20,000 chars): SOUL, USER, MEMORY (or the index, if the pages layout was chosen), core-memories, then recent daily logs, truncated at the last complete line. Pages/entities/topics are never loaded here — only the index points at them, so a large memory vault does not blow the budget.
- PreCompact hook (extract conversation context → spawn background `memory_flush.py`)
- SessionEnd hook (same pattern — extract context → spawn background flush)
- `memory_flush.py`: Background Agent SDK script spawned by PreCompact/SessionEnd. Uses Claude with `allowed_tools=[]` (pure reasoning, no tools) to intelligently decide what decisions, lessons, and facts from the conversation are worth saving. The raw model reply is never appended to the daily log directly — it passes through a `normalize_flush_output()` guard first, which strips code fences, drops "Here's what I extracted:"-style preambles, and treats any reply with no bullet points (including a decorated "nothing notable" reply) as nothing-to-write. Has two layers of deduplication — a session-keyed dedup (skip if the same session flushed within the last N seconds) and a content-keyed dedup (skip if the same normalized content, hashed, was already written within that window, catching concurrent sessions producing identical output) — plus file locking. This is critical — without normalization and dedup, daily logs contain mechanical transcript excerpts and duplicate entries instead of intelligent, once-only summaries that the daily reflection can actually promote to MEMORY.md.
- **Hook recursion prevention**: Every Agent SDK session (heartbeat, reflection, chat, memory flush) must set `os.environ["CLAUDE_INVOKED_BY"] = "<name>"`. SessionEnd and PreCompact hooks check this env var and skip if set — this prevents Agent SDK exits from triggering additional flushes, which would cause duplicate log entries or infinite recursion.
- **Shared utilities** (`shared.py`): Cross-platform file locking (`file_lock()` using `msvcrt` on Windows / `fcntl` on Unix) for concurrent write safety, retry with exponential backoff (`with_retry()`) for external API calls, and atomic state writes. Multiple processes (heartbeat, reflection, chat, flush) write to daily logs and state files concurrently — without file locking, they corrupt each other.
- **Agent SDK CLI selection** (`sdk_cli.py`): every Agent SDK call site — heartbeat, reflection, chat, memory flush — must pass through `sdk_cli.cli_kwargs()` (or `sdk_cli.apply_cli_path()` for dict-built options), which picks the highest-versioned Claude CLI actually installed on the machine rather than trusting whatever the SDK bundles. An outdated bundled CLI can intermittently reject well-formed requests with a bodyless 400 before the model ever sees them; `sdk_cli.run_with_retry()` retries only that specific failure mode (a 400 with a real body means the request was wrong and retrying won't help). A failure that survives retries is logged, not swallowed.
- Key files, estimated complexity: Medium

**Phase 3: Memory Search (Hybrid RAG)**
- Set up chunking + embedding pipeline
- SQLite + sqlite-vec + FTS5 (local) or Postgres + pgvector (VPS)
- Hybrid search: 0.7 vector + 0.3 keyword
- **Chunk identity is `sha256(source + "\0" + text)`** — the source path is part of the fingerprint, not just the text, so identical text in two different files gets two distinct rows instead of colliding on one (a text-only hash silently drops every file but the first to write that chunk). Changing the fingerprint scheme or the chunker forces a full reindex; there is no incremental repair path for that case.
- **Incremental indexing prunes.** Every incremental run removes chunks (from the vector table, the FTS table, and metadata) whose fingerprint a fresh crawl no longer produces — a file that was edited or deleted must not leave stale rows behind. Pruning is skipped on an empty crawl, since an empty crawl is far more often a broken path than an actually-emptied vault.
- **A search result is a pointer, not a payload.** Each hit returns `{source, heading, start_line, end_line, excerpt, score}` — where the answer lives, plus a short excerpt to judge relevance — not the full chunk text. The caller reads the file at that line range for the rest. An opt-in "full text" flag exists for callers that cannot read the file themselves, but it is not the default; returning full payloads by default wastes tokens on text mostly never read.
- **The FTS query is tried raw against the engine first** (so `OR` / `NOT` / phrase operators and the plain multi-word AND join work), and only falls back to a quoted, escaped phrase when the raw query would be a syntax error (hyphens, stray quotes, bare operators in a query that wasn't meant as a query language). Unconditionally quoting every query turns every search into an exact-phrase match and silently disables the operators — don't do that.
- Key files, estimated complexity: Medium

**Phase 4: Integrations (Their Top 3 First)**
- Use their "Integration Priority" rankings
- For each: auth setup, API module, registry entry, query.py subcommand
- Reference the integration_template.py pattern
- Key files per integration, estimated complexity: Medium per integration

**Phase 5: Skills (Starter Pack)**
- Vault structure skill (teach agent their file organization)
- At least one custom skill based on their "Top Tasks"
- Skill anatomy: SKILL.md + scripts/ + references/
- Key files, estimated complexity: Low-Medium

**Phase 6: Proactive Systems (Heartbeat + Reflection)**
- The heartbeat is a **staged pipeline**, not one monolithic call: (1) Python **gathers** data from every integration → (2) **state-diffing** — `build_snapshot(gmail_data, calendar_data, ...)` and `diff_snapshots(current, previous)` produce a hashable snapshot per integration and the delta against the last run, persisted as JSON state (via the atomic writes in `shared.py`) under the project's local state directory; only the delta (new emails, newly overdue tasks, newly unread messages) is ever passed forward — without this, every run re-surfaces the same items and the user gets paged endlessly → (3) a **pre-flight guardrail check** (`run_guardrail_check(context)` — a separate, no-tools Claude call, `allowed_tools=[]`, that receives the sanitized delta and returns `{"verdict": "pass"|"fail"|"suspicious", "reason": ..., "flags": [...]}`) → (4) the main Claude Agent SDK reasoning call, with tools, that only ever sees a `pass` or `suspicious` context and never a `fail` → (5) notify. `fail` aborts the run and logs what was blocked; `suspicious` proceeds with a warning recorded in the daily log. This ordering is not optional and must not be collapsed into the main agent call — it is the only semantic (model-judged) check in the security stack; every other layer is pattern-based and can be evaded by content that reads fine to a regex but not to a reader. Function names (`build_snapshot`, `diff_snapshots`, `run_guardrail_check`) should appear in the generated PRD exactly as given here so a coding agent can trace the pattern.
- Every heartbeat sub-flow runs through `feature_run()`, which wraps it with a machine-local on/off toggle and an append-only timing log (one JSON line per run: `{ts, feature, status: ok|error|disabled, duration_ms, error?}`). This makes "a feature was disabled" and "a feature ran and did nothing" distinguishable after the fact, and gives the dashboard/health-check layer a single place to read run history from — don't build a second ad hoc logging path per feature.
- On a two-machine setup (local + VPS), whichever machine is *not* the day's owner for a given daily artifact (the morning brief, the digest) must skip generating it, not generate a duplicate. `brief_coord` provides this per-machine "first claim of the day wins" coordination — `claim_brief(name)` / `release_brief(name)` keyed by the local calendar date — so heartbeat runs on both machines without both of them writing the same brief.
- Set schedule based on their proactivity level
- Daily reflection: promote important daily log items into memory — if the index + pages layout was chosen in Phase 1, reflection routes each item to the matching page under `entities/`, `topics/`, or `decisions/` by mention count, and creates a **new page only once a subject has appeared in 3 or more separate files** (checked with a repo-wide count, not a guess) — anything short of that threshold stays as a line in the index's managed Active Items queue instead of spawning a one-off page. Must include **SOUL.md write-protection** — a PreToolUse hook that blocks the reflection agent from editing SOUL.md. If the reflection wants to suggest changes to the agent's identity or rules, it writes those suggestions to the daily log instead. This prevents "soul drift" where the agent gradually rewrites its own personality without user approval.
- Map their proactivity level choice to specific heartbeat behaviors
- **Draft management** (required for Advisor/Assistant/Partner proactivity levels): The heartbeat must implement a full draft lifecycle system. Specifically: (1) scan integration data for emails, DMs, and community posts needing a reply, (2) generate draft replies in the user's voice using RAG on `drafts/sent/` for voice-matching (`memory_search.py --path-prefix drafts/sent`), (3) write drafts as markdown files in `drafts/active/` with YAML frontmatter (type, source_id, recipient, subject, context, created, status) + Original Message + Draft Reply sections, (4) expire drafts >24h old with no action by moving to `drafts/expired/`, (5) detect when the user has actually replied on the platform and move the file to `drafts/sent/` capturing their real reply text. The heartbeat Agent SDK session needs Write/Edit tools to create draft files — read-only tools are insufficient. Drafting criteria should be defined in USER.md (what to draft, what to skip).
- **Habits tracking**: HABITS.md with customizable pillars, auto-detection rules for objective achievements, daily reset by heartbeat, late-day nudges for unchecked pillars
- Key files, estimated complexity: High

**Phase 7: Chat Interface (Optional)**
- Only include if they checked Chat/Messaging in platforms
- Slack/Discord/Telegram bot with persistent conversations
- Platform adapter pattern for extensibility
- Key files, estimated complexity: High

**Phase 8: Security Hardening**
- **Credential protection hook** (`block-secrets.py`): a single PreToolUse hook that intercepts ALL file-access tools (Read, Bash, Grep, Edit, Write, Glob) and blocks access to .env files, API tokens, OAuth credentials, SSH keys, and other secrets. It also scans the *content* being written or executed — not just file paths — for patterns that would expose or exfiltrate a secret (a Bash command that dumps environment variables, a script written to disk that reads and prints a credential file). Path and command matching are done against a **cwd-resolved** view of the command: it tracks a virtual working directory across any `cd` in the command plus the tool call's real cwd, and re-checks every relative-looking argument against that resolved path — a bare delete run from inside the vault folder is caught the same way the equivalent absolute-path delete is from the project root, because both resolve to the same protected path. This is the most critical security component — without it, the LLM can accidentally read and expose every API key. **This single hook is the deliberate replacement for having a separate `DANGEROUS_BASH_PATTERNS` list wired in as its own mechanism**: one hook, covering both credential-file protection and destructive/dangerous-command matching, gets tested once and runs on every tool call, instead of two overlapping pattern lists that can drift out of sync with each other. If the user's security answers call for additional destructive-command categories beyond what ships here (package installs, privilege escalation, outbound calls to non-allowlisted hosts), add those patterns to this same hook's pattern list — do not stand up a second hook to hold them.
- Sanitize all external data (3-layer defense: pattern detection → markdown escaping → XML trust boundaries), paired with an explicit trust-boundary instruction in the system prompt telling Claude that anything inside the external-content tags is data, not instructions — the wrapper without the instruction is half a defense.
- **Pre-flight guardrail agent** (`run_guardrail_check`, Phase 6) — the only semantic check in the security stack; the rest of this list is pattern-based.
- Command guardrails based on their Security Boundaries answers, added to `block-secrets.py`'s pattern list per the note above.
- API key isolation (Python CLI wrapper pattern)
- Key files, estimated complexity: Medium-High

**Phase 9: Deployment**
- Based on their Infrastructure answers
- Local: OS scheduler setup (Windows Task Scheduler / cron / launchd)
- If VPS: server setup, vault sync, SSH tunnel
- **Vault sync via a Git `merge=union` driver** (required if user picked "Local + VPS"): sync the vault via Git on a short timer (e.g. every 2 minutes) on both machines. Append-only paths — daily logs and any other files multiple processes append to concurrently on both machines — get a `.gitattributes` entry setting `merge=union` for that glob, so a merge keeps every line either side added instead of conflicting. This is a built-in Git merge strategy (no custom driver script to write or register), and it is what keeps concurrent append-only writes from breaking sync within the first day of real use. Must be a named deliverable: the exact glob patterns getting `merge=union` and the `.gitattributes` entries, committed to the repo (not applied only via local `git config`, which wouldn't travel with a clone).
- Cost estimate based on their choices

**Each phase includes:**
- What to build (1-2 sentences)
- Key files to create (with paths)
- Dependencies (which phases must come first)
- Estimated complexity (Low / Medium / High)
- Personalization notes (how their specific answers shape this phase)
- **CLAUDE.md update reminder**: Every phase must end by updating CLAUDE.md with any new paths, build commands, and conventions introduced. This keeps the agent's project reference current — if a command exists but isn't in CLAUDE.md, the agent won't know about it.

**Footer:**
- Recommended build order (phases are mostly sequential but some can parallel)
- "This PRD was generated from your requirements. Revisit and update as your system evolves."

5. **Confirm output** — Tell the user where the PRD was saved (the output path) and suggest they start with Phase 1.

## Personalization Rules

- Use THEIR vault folder name everywhere in file paths — use whatever they wrote in "Memory vault folder name". For example, if they wrote "SecondBrain", all paths should be `SecondBrain/Memory/`, `SecondBrain/Memory/daily/`, etc.
- If they are NOT using Obsidian, don't mention Obsidian in the PRD — just refer to the vault as a "memory vault" or "markdown folder". If they ARE using Obsidian, mention it as the viewer/editor.
- If they did NOT opt into the index + pages memory layout, keep MEMORY.md as one flat file in Phase 1 and drop the entities/topics/decisions/core-memories structure and the 3-file routing threshold from Phase 6 — that whole mechanism is opt-in, not mandatory.
- Use THEIR platform names everywhere (not generic "email" — use "Gmail" if that's what they chose)
- Map their proactivity level to concrete behaviors:
  - Observer → heartbeat notifications only, no drafting, no habit tracking
  - Advisor → heartbeat + draft emails/messages for review, habit tracking with suggestions but no auto-check
  - Assistant → heartbeat + drafts + auto-organize files + auto-log, habit auto-detection for objective pillars
  - Partner → all of the above + send low-risk messages + auto-complete routine tasks + full habit auto-detection
- Map their security boundaries directly into Phase 8 guardrail rules
- Use their memory categories to structure the vault folders in Phase 1
- Their integration priority determines Phase 4 order
- Never hardcode a name, account, or company into generated code or docs — every owner-specific value (name, email accounts, primary account, remote host aliases) is a config value read from `OWNER_NAME` / a `CORTEX_*`-style environment variable, filled in from their answers. This is the same discipline a shared/open-sourced version of this system needs: a ratchet test that fails the build the moment a personal string leaks into a generic module is worth adding once any part of this codebase is meant to be reused across owners.
