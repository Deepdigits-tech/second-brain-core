#!/usr/bin/env python3
"""Daily reflection — promote yesterday's daily log items to MEMORY.md.

Scheduled at 8 AM PST via launchd. Uses Agent SDK with Read + Edit tools.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
DAILY_DIR = VAULT_DIR / "daily"
DATA_DIR = PROJECT_ROOT / ".claude" / "data"
ERROR_LOG_PATH = DATA_DIR / "reflect_errors.log"
DEBUG_LOG = "/tmp/cortex_hook_debug.log"

from shared import PST, owner_name

# Reflection runs on ONE machine. Both used to run it at 08:00 and both rewrote
# vault/MEMORY.md, which is git-tracked and synced every two minutes; the loser
# spent its turn budget resolving the conflict its twin created. The
# coordination file is gitignored (per-machine), so this is a platform gate —
# the same mechanism heartbeat.BRIEF_OWNER_IS_VPS uses for the briefs. Force
# ownership for a local test with CORTEX_REFLECTION_OWNER=1.
REFLECTION_OWNER_IS_VPS = sys.platform != "darwin"


def is_reflection_owner() -> bool:
    override = os.environ.get("CORTEX_REFLECTION_OWNER")
    if override is not None:
        return override.strip() == "1"
    return REFLECTION_OWNER_IS_VPS

# How many distinct files a name must already appear in before reflection may
# create a new page for it. Below the threshold the item stays a line in
# Active Items — a page is a commitment, and one mention is not evidence of
# recurrence.
MENTION_THRESHOLD = 3

SYSTEM_PROMPT_TEMPLATE = """You are the Cortex OS reflection agent — a memory curator for {owner}'s AI second brain.

MEMORY.md is a small INDEX, not a store of facts. The detail lives on pages:
`vault/entities/<name>.md` (people, companies, tools), `vault/topics/<name>.md`
(themes — architecture, a recurring lesson, a subsystem), `vault/decisions/YYYY-Qn.md`
(quarterly decision archives), and `vault/opportunities/<slug>/index.md` (deals — already
exist, never created by you). Your job: review yesterday's (or the last 1-3 days') daily
log(s) and route each item to the right place. Curation and routing, not accretion.

## Routing rules — apply in order for every item:

1. **Entity item** (a person, company, tool, or project did/decided something): look for an
   existing page in `vault/entities/` (or a matching `vault/opportunities/<slug>/index.md` if
   it's a deal). If one exists, Edit it: update `## Current Status` in place (replace the line
   for that fact, stamp `(YYYY-MM-DD)`) and append durable decisions to `## Key Decisions`.
   Bump `updated:` in frontmatter. If no page exists, run the mention-count check (below)
   before creating one.

2. **Theme item** (an architectural pattern, a recurring lesson, a subsystem's behavior):
   same rule against `vault/topics/`. Existing page → Edit `## Current Status` / `## Key
   Decisions`, bump `updated:`. No page → mention-count check before creating one.

3. **Time-bound item** (a thing due or relevant only until a date — a follow-up, a thing to
   check back on): put it in MEMORY.md's `## Active Items` as a dated line, e.g.
   `- (Sep 4) Confirm invoice with Acme — see [[entities/acme]]`. Link to the entity/topic
   page if one exists; if not, it's just a bare line until it earns a page.

4. **Completed decision with no future action** (a thing that happened and is now closed —
   nothing left to track going forward): append it to the current quarter's decision archive,
   `vault/decisions/YYYY-Qn.md` (e.g. `2026-Q3.md` for Jul-Sep). If that file doesn't exist,
   create it with frontmatter (see below) before appending.

## Before creating any new page — the 3-file threshold:
Do not create a page for a name that has appeared in fewer than 3+ distinct files.
Run `Grep` with the exact subject name (case-insensitive, e.g. pattern `-i "Bob"`) across
`vault/` and count DISTINCT FILES in the results (not occurrences — three mentions in one
file is still one file). If the file count is 3 or more, create the page. If it is
below 3, put the item in `## Active Items` instead — a page is a commitment to
recurrence, and recurrence needs evidence.

## Page frontmatter (verbatim shape):
Entity pages (people, companies, tools, products) carry a `category`; topic pages do not.

Entity page:
```
---
title: <Human Readable Name>
type: entity
category: person | project | company | product
created: YYYY-MM-DD
updated: YYYY-MM-DD
related: [other-page-slug, another-slug]
tags: [tag-one, tag-two]
---
```

Topic page:
```
---
title: <Human Readable Name>
type: topic
created: YYYY-MM-DD
updated: YYYY-MM-DD
related: [other-page-slug, another-slug]
tags: [tag-one, tag-two]
---
```

Decision archive pages use `type: decisions` and drop both `category` and `related`/`tags`
unless useful; frontmatter there is otherwise the same shape.

`updated:` MUST be bumped to today's date on every edit to an existing page — this is how
staleness is tracked, so never skip it, including on a page you are not otherwise changing much.

Every page carries `## Current Status` (state — replace in place, dated) and `## Key
Decisions` (events — append only, never edit/delete a past entry). `decisions/YYYY-Qn.md`
pages hold entries under their own dated headings instead of `## Current Status`, since a
quarter archive is entirely events.

## Active Items is a managed queue, not a scratchpad:
Before adding anything, scan MEMORY.md's `## Active Items` and resolve entries whose
date has passed (move them: to a page's Key Decisions if still worth recording, or drop them
if they're stale and done) or whose subject has since earned its own page (replace the line
with a link to the page, or remove it if the page's Current Status already covers it).

## State vs event — decide this for EVERY item BEFORE you write it:
- **STATE** = one current value that changes (a status, a count, an owner, a next step, a
  file path). It lives in a page's `## Current Status` (or MEMORY.md's `## Preferences` for
  durable operating facts about {owner} personally). Find the existing line for that subject
  and **REPLACE it in place**. Never add a second line for the same subject. Stamp `(YYYY-MM-DD)`.
- **EVENT** = a thing that happened (a decision, a lesson, a delivery). **APPEND** it to a
  page's `## Key Decisions`, or to the quarter's `decisions/YYYY-Qn.md` if it closes out with
  no page of its own. Never edit or delete an existing entry.
- If unsure which it is, append it as an event and say so in your response. A missing state
  update is recoverable; a rewritten history is not.
- NEVER write a live counter (credit balances, quota remaining, row counts) as a value
  anywhere. It decrements without anyone writing it back. Record where to look it up instead.

## Rules:
- Only ever Write or Edit files under `vault/`. Never create or modify anything outside it.
- NEVER edit SOUL.md, USER.md, or core-memories.md
- NEVER let a Current Status / Key Decisions line exceed 280 chars
- MEMORY.md (the index) must stay under 5 KB. If your edit would push it over, say by how
  much and resolve Active Items instead — move each to its page's `## Key Decisions` or the
  quarter archive, or drop it only if it is stale and already done. Never delete a fact to
  make room.
- Do NOT invent new top-level sections in MEMORY.md. It has exactly: `## Active Items`,
  `## Entity Pages`, `## Topic Pages`, `## Decision Archives`, `## Preferences`,
  `## Live counters — look up, never cache here`.
- The `## Entity Pages` / `## Topic Pages` / `## Decision Archives` sections are just an
  index of `[[name]]` links — add a link there when you create a page, don't duplicate its
  content into MEMORY.md.
- If nothing in the recent log(s) meets the bar for any route, just say so. Forcing items in
  defeats the purpose.

If you prune or resolve something in Active Items, briefly note in your response WHAT and WHY
(so we can spot over-aggressive pruning).
"""


def system_prompt() -> str:
    """Rendered at call time: OWNER_NAME is only in the env after load_env()."""
    return SYSTEM_PROMPT_TEMPLATE.replace("{owner}", owner_name())


def debug(msg: str):
    with open(DEBUG_LOG, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] memory_reflect: {msg}\n")


def log_error(msg: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ERROR_LOG_PATH, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def _alert(title: str, exc: Exception, consequence: str) -> None:
    """Push a failure to macOS + Telegram. Never raises.

    Reflection used to fail into reflect_errors.log and nowhere else, so a job
    that stopped updating MEMORY.md looked identical to one that had nothing to
    say. Silence is now reserved for success.
    """
    try:
        from notify import notify

        notify(title, f"{type(exc).__name__}: {exc} — {consequence}")
    except Exception:
        pass  # a failed notification must not mask the original failure


def _propose_content_ideas(text: str) -> None:
    """Parse CONTENT_IDEA lines from reflection output into the content queue.

    Never raises — a content-intake failure must not break memory reflection.
    """
    from shared import lane_available
    if not lane_available("content", "tasks"):
        debug("optional module content/tasks not present — content idea intake skipped")
        return
    try:
        import content
        import tasks

        for idea in content.parse_content_ideas(text):
            path = content.propose_idea(
                idea["title"], source="reflection", material=idea.get("rationale", "")
            )
            if path:
                tasks.propose(
                    f"Review Substack idea: {idea['title']}",
                    labels=["content"],
                    quadrant="Q2",
                    source="reflection",
                )
                debug(f"content idea proposed: {idea['title']}")
    except Exception as exc:
        # Loud on purpose. This used to log a bare str(exc) and stop there. On
        # 2026-08-14 it swallowed content ideas four times with only "disk full"
        # and no traceback, so the real cause could not be identified — and the
        # disk had 22 GB free at the time, so the message was misleading too.
        log_error(
            f"content idea intake failed: {exc!r}\n{traceback.format_exc()}"
        )
        debug(f"content idea intake failed: {exc!r}")
        _alert(
            "Cortex: content intake FAILED",
            exc,
            "ideas dropped; traceback in .claude/data/reflect_errors.log",
        )


def get_yesterday_log() -> str:
    """Read yesterday's daily log file."""
    yesterday = (datetime.now(PST) - timedelta(days=1)).strftime("%Y-%m-%d")
    path = DAILY_DIR / f"{yesterday}.md"
    if path.exists():
        return path.read_text()
    return ""


def get_recent_daily_logs(days: int = 3) -> str:
    """Best-effort context beyond yesterday: the two days before it, if present.

    Reflection's routing decisions (mention counts, Active Items resolution)
    read better with a few days of trailing context than one day alone. This
    never gates the run — get_yesterday_log() alone still decides whether
    there's anything to do.
    """
    from shared import MAX_DAILY_CHARS, cut_at_line_boundary

    parts = []
    for i in range(days, 1, -1):
        d = (datetime.now(PST) - timedelta(days=i)).strftime("%Y-%m-%d")
        path = DAILY_DIR / f"{d}.md"
        if path.exists():
            text = path.read_text()
            if len(text) > MAX_DAILY_CHARS:
                text = cut_at_line_boundary(text, MAX_DAILY_CHARS)
                text += f"\n[log truncated at {MAX_DAILY_CHARS} chars]"
            parts.append(f"### {d}\n\n{text}")
    return "\n\n".join(parts)


def page_listing(vault: Path) -> str:
    """Bulleted names of every existing page reflection can route to.

    Entities/topics/decisions are named by their file stem
    (`entities/project-alpha`); opportunities are named by their slug
    (`opportunities/acme`, not `.../acme/index`) since the slug is the
    addressable unit and `[[name]]` resolves opportunities via their index.
    Missing directories (pre-migration, or a thin test vault) are silently
    empty — this is a listing, not a validator.
    """
    lines: list[str] = []
    for kind in ("entities", "topics", "decisions"):
        for path in sorted((vault / kind).glob("*.md")):
            lines.append(f"- {kind}/{path.stem}")
    for path in sorted((vault / "opportunities").glob("*/index.md")):
        lines.append(f"- opportunities/{path.parent.name}")
    if not lines:
        return "(no pages yet)"
    return "\n".join(lines)


def mention_count(vault: Path, name: str) -> int:
    """Count distinct markdown files under `vault` that mention `name`.

    Case-insensitive; counts FILES, not occurrences — three mentions in one
    file is still one file. Used to gate page creation at MENTION_THRESHOLD.
    The caller (the model, via Grep) excludes the page being created simply
    because it doesn't exist yet at count time.
    """
    needle = name.lower()
    count = 0
    for path in vault.rglob("*.md"):
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if needle in text.lower():
            count += 1
    return count


async def reflect(dry_run: bool = False):
    yesterday_log = get_yesterday_log()
    if not yesterday_log.strip():
        debug("no yesterday log — skipping")
        return

    memory_path = VAULT_DIR / "MEMORY.md"
    memory_content = memory_path.read_text() if memory_path.exists() else ""
    soul_content = (VAULT_DIR / "SOUL.md").read_text() if (VAULT_DIR / "SOUL.md").exists() else ""
    user_content = (VAULT_DIR / "USER.md").read_text() if (VAULT_DIR / "USER.md").exists() else ""
    listing = page_listing(VAULT_DIR)
    earlier_logs = get_recent_daily_logs()

    prompt = (
        f"Current MEMORY.md index ({len(memory_content.splitlines())} lines):\n\n{memory_content}\n\n"
        f"USER.md (read-only — context, never edit):\n\n{user_content}\n\n"
        f"SOUL.md (read-only — context, never edit):\n\n{soul_content}\n\n"
        f"Existing pages you can route to:\n\n{listing}\n\n"
        + (f"Earlier daily log context (read-only, for routing evidence):\n\n{earlier_logs}\n\n" if earlier_logs else "")
        + f"Yesterday's daily log (the one to act on):\n\n{yesterday_log}\n\n"
        "Task:\n"
        "1. ROUTE: for every item in yesterday's log, apply the routing rules — entity, "
        "theme, time-bound, or completed decision. Before creating any new entity/topic page, "
        "Grep the exact subject name (case-insensitive) across vault/ and count DISTINCT FILES "
        "in the results; only create the page at the mention threshold, otherwise add a dated "
        "line to MEMORY.md's `## Active Items` instead.\n"
        "2. MANAGE ACTIVE ITEMS: resolve entries in `## Active Items` whose date has passed or "
        "whose subject now has its own page.\n"
        "3. CONTENT: if yesterday's log shows a shipped system, a client result "
        "with real numbers, or a build decision worth showing publicly, emit exactly "
        "one line: CONTENT_IDEA: <post title> | <one-line why now>. High bar — at "
        "most one per day; emit nothing if no candidate clears it.\n"
        "If nothing in the log meets the bar for any route, just say so — don't force changes."
    )

    if dry_run:
        print("=== DRY RUN — prompt that would be sent (no model call, no edits) ===")
        print(prompt)
        return

    if not is_reflection_owner():
        debug("not the reflection owner on this machine — skipping (set CORTEX_REFLECTION_OWNER=1 to force)")
        return

    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    os.environ["CLAUDE_INVOKED_BY"] = "reflection"

    from sdk_stderr import make_stderr_capture
    import sdk_cli
    sdk_stderr_cb, get_sdk_stderr_tail = make_stderr_capture(context="memory_reflect")

    collected: list[str] = []
    async for msg in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            system_prompt=system_prompt(),
            allowed_tools=["Read", "Edit", "Write", "Grep", "Glob"],
            # 25 was enough for a flat MEMORY.md (read, prune, promote, append).
            # Routing now touches multiple pages per run — Grep a mention count,
            # Read or create an entity/topic page, Edit its Current Status and
            # Key Decisions, bump its frontmatter, then resolve Active Items in
            # the index — each of which is its own tool turn. 40 gives headroom
            # for a multi-item day without reopening the error_max_turns failure
            # mode the 10→25 bump was fixing (see the comment history above).
            max_turns=40,
            cwd=str(PROJECT_ROOT),
            stderr=sdk_stderr_cb,
            **sdk_cli.cli_kwargs(),
        ),
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    collected.append(block.text)
                    debug(f"reflection output: {block.text[:200]}")
        elif isinstance(msg, ResultMessage):
            if msg.is_error:
                tail = " | ".join(get_sdk_stderr_tail()[-5:]) or "(no stderr)"
                import agent_result

                reason, is_auth = agent_result.describe_failure(
                    msg, "\n".join(collected), tail
                )
                log_error(f"Agent SDK error: {reason}")
                if is_auth:
                    agent_result.alert_auth_failure(f"reflection: {reason}")

    # One owner means one proposer. The platform gate that stopped the VPS run
    # from double-proposing is gone with the double run.
    _propose_content_ideas("\n".join(collected))


async def run_weekly_body_report_if_monday():
    """On Mondays (PST), generate the weekly body report alongside reflection."""
    today = datetime.now(PST)
    if today.weekday() != 0:  # 0 = Monday
        return
    from shared import lane_available
    if not lane_available("weekly_body_report"):
        debug("optional module weekly_body_report not present — weekly body report skipped")
        return
    try:
        import weekly_body_report
        path = await weekly_body_report.generate(notify=True)
        debug(f"weekly body report written: {path}")
    except Exception:
        tb = traceback.format_exc()
        debug(f"weekly body report failed: {tb}")
        log_error(f"Weekly body report failed:\n{tb}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Daily reflection — promote yesterday's log into MEMORY.md")
    parser.add_argument("--test", action="store_true", dest="dry_run",
                        help="Print the prompt reflection would send and exit; no model call, no edits")
    args = parser.parse_args()

    debug("reflection started" + (" (dry run)" if args.dry_run else ""))
    os.environ["CLAUDE_INVOKED_BY"] = "reflection"

    # launchd gives us a bare environment, so the Agent SDK's CLI auth token
    # has to come from .claude/.env the same way heartbeat.py loads it.
    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
    from integrations.registry import load_env

    load_env()

    try:
        asyncio.run(reflect(dry_run=args.dry_run))
        debug("reflection complete")
    except Exception as exc:
        tb = traceback.format_exc()
        debug(f"reflection failed: {tb}")
        log_error(f"Reflection failed:\n{tb}")
        _alert("Cortex: REFLECTION FAILED", exc, "MEMORY.md was not updated today")

    # Monday-only: weekly body report. Separate try/except so a reflection
    # failure doesn't kill the body report and vice versa.
    try:
        asyncio.run(run_weekly_body_report_if_monday())
    except Exception as exc:
        tb = traceback.format_exc()
        log_error(f"Monday body report wrapper failed:\n{tb}")
        _alert("Cortex: weekly body report FAILED", exc, "no report written")


if __name__ == "__main__":
    main()
