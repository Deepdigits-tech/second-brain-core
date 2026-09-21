"""MCP tool server exposing Cortex OS capabilities to the Agent SDK."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool


def create_cortex_tools(project_root: Path) -> McpSdkServerConfig:
    """Create MCP server with Cortex OS tools for the chat agent."""
    scripts_dir = project_root / ".claude" / "scripts"
    vault_dir = project_root / "vault"

    # Ensure scripts are importable
    scripts_str = str(scripts_dir)
    if scripts_str not in sys.path:
        sys.path.insert(0, scripts_str)

    from shared import owner_name, gmail_accounts, primary_account, lane_available, projects as _cortex_projects

    # Built once per server start, after registry.load_env() has run (see
    # server.py: load_env() precedes create_cortex_tools()) — never a
    # hard-coded alias list. Empty CORTEX_GMAIL_ACCOUNTS degrades to a
    # generic phrase rather than crashing tool registration.
    _gmail_aliases = ", ".join(f"'{a}'" for a in gmail_accounts()) or "the configured accounts"
    _default_account = primary_account()
    _projects_roster = ", ".join(
        f"{slug} ({desc})" if desc else slug for slug, desc in _cortex_projects().items()
    )

    @tool(
        "memory_search",
        "Search the vault using hybrid vector + keyword search. Returns POINTERS, not "
        "content: each hit gives a file path, the line range that matched, and a short "
        "excerpt. Use the excerpt to judge relevance, then use vault_read on the path "
        "when you need the full passage.",
        {
            "query": Annotated[str, "Search query text"],
            "limit": Annotated[int, "Max results to return (default 5)"],
            "path_prefix": Annotated[str, "Optional path prefix filter (e.g. 'vault/investments')"],
        },
    )
    async def memory_search(args: dict) -> dict:
        from memory_search import hybrid_search

        results = hybrid_search(
            query_text=args["query"],
            limit=args.get("limit", 5),
            path_prefix=args.get("path_prefix"),
        )
        return {"content": [{"type": "text", "text": json.dumps(results, indent=2, default=str)}]}

    @tool(
        "gmail_triage",
        "List recent emails from a Gmail account. Returns subject, sender, date, and snippet.",
        {
            "account": Annotated[str, f"Gmail account alias: {_gmail_aliases}. Default: {_default_account}."],
            "max_results": Annotated[int, "Max emails to return (default 10)"],
            "query": Annotated[str, "Optional Gmail search query (e.g. 'is:unread')"],
        },
    )
    async def gmail_triage(args: dict) -> dict:
        from integrations.gmail import triage

        results = triage(
            account=args.get("account", "personal"),
            max_results=args.get("max_results", 10),
            query=args.get("query"),
        )
        return {"content": [{"type": "text", "text": json.dumps(results, indent=2, default=str)}]}

    @tool(
        "gmail_read",
        "Read a specific email by message ID. Returns full email content.",
        {
            "message_id": Annotated[str, "Gmail message ID"],
            "account": Annotated[str, f"Gmail account alias: {_gmail_aliases}. Default: {_default_account}."],
        },
    )
    async def gmail_read(args: dict) -> dict:
        from integrations.gmail import read_message

        result = read_message(args["message_id"], account=args.get("account", "personal"))
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]}

    @tool(
        "gcal_today",
        "Get today's calendar events.",
        {
            "account": Annotated[str, f"Calendar account alias: {_gmail_aliases} (default: {_default_account})"],
        },
    )
    async def gcal_today(args: dict) -> dict:
        from integrations.gcal import agenda_today

        results = agenda_today(account=args.get("account", "personal"))
        return {"content": [{"type": "text", "text": json.dumps(results, indent=2, default=str)}]}

    @tool(
        "gcal_upcoming",
        "Get upcoming calendar events for the next N days.",
        {
            "account": Annotated[str, f"Calendar account alias: {_gmail_aliases} (default: {_default_account})"],
            "days": Annotated[int, "Number of days to look ahead (default 3)"],
        },
    )
    async def gcal_upcoming(args: dict) -> dict:
        from integrations.gcal import agenda_upcoming

        results = agenda_upcoming(days=args.get("days", 3), account=args.get("account", "personal"))
        return {"content": [{"type": "text", "text": json.dumps(results, indent=2, default=str)}]}

    @tool(
        "vault_read",
        "Read a file from the vault. Path is relative to vault/ directory.",
        {
            "path": Annotated[str, "File path relative to vault/ (e.g. 'MEMORY.md', 'investments/watchlist.md')"],
        },
    )
    async def vault_read(args: dict) -> dict:
        rel_path = args["path"]
        # Security: prevent absolute paths and path traversal — 2026-09-20 security fix
        if rel_path.startswith("/"):
            return {"content": [{"type": "text", "text": "Error: absolute paths not allowed"}]}
        if ".." in rel_path:
            return {"content": [{"type": "text", "text": "Error: path traversal not allowed"}]}
        target = vault_dir / rel_path
        resolved = target.resolve()
        # Use is_relative_to() instead of startswith() to prevent sibling directory bypass
        vault_resolved = vault_dir.resolve()
        try:
            resolved.relative_to(vault_resolved)
        except ValueError:
            return {"content": [{"type": "text", "text": "Error: path outside vault"}]}
        if not target.exists():
            return {"content": [{"type": "text", "text": f"File not found: vault/{rel_path}"}]}
        content = target.read_text()
        return {"content": [{"type": "text", "text": content}]}

    @tool(
        "integration_status",
        "Check which Cortex OS integrations (Gmail, GCal, Telegram, Slack) are available.",
        {},
    )
    async def integration_status(args: dict) -> dict:
        from integrations.registry import check_all

        status = check_all()
        return {"content": [{"type": "text", "text": json.dumps(status, indent=2)}]}

    @tool(
        "capture_insight",
        "Capture a quick thought or idea to today's daily log.",
        {
            "text": Annotated[str, "The insight to capture"],
        },
    )
    async def capture_insight(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        from shared import append_daily_log, owner_timezone

        pst = owner_timezone()
        today = datetime.now(pst).strftime("%Y-%m-%d")
        daily_path = vault_dir / "daily" / f"{today}.md"

        now = datetime.now(pst).strftime("%H:%M")
        from sanitize import strip_injection_patterns
        clean_text = strip_injection_patterns(args['text'])
        entry = f"\n## {now} — Insight (via Chat)\n\n{clean_text}\n"

        append_daily_log(str(daily_path), entry)

        return {"content": [{"type": "text", "text": f"Captured to {daily_path}"}]}

    @tool(
        "gmail_inbox_status",
        "Get inbox management status: mode (supervised/auto), pending review count, graduation progress for each account.",
        {
            "account": Annotated[str, f"Optional: {_gmail_aliases}. Returns all accounts if omitted."],
        },
    )
    async def gmail_inbox_status(args: dict) -> dict:
        from inbox.classifier import inbox_status_summary

        summary = inbox_status_summary()
        account_filter = args.get("account")
        if account_filter and account_filter in summary:
            summary = {account_filter: summary[account_filter]}

        return {"content": [{"type": "text", "text": json.dumps(summary, indent=2, default=str)}]}

    @tool(
        "gmail_inbox_classify",
        "Classify a specific email by ID and return its tier assignment. Useful for ad-hoc classification via chat.",
        {
            "message_id": Annotated[str, "Gmail message ID to classify"],
            "account": Annotated[str, f"Gmail account alias: {_gmail_aliases}. Default: {_default_account}."],
        },
    )
    async def gmail_inbox_classify(args: dict) -> dict:
        from integrations.gmail import read_message
        from inbox.classifier import classify_email, _load_rules

        account = args.get("account", "personal")
        message_id = args["message_id"]

        msg = read_message(message_id, account=account, metadata_only=True)
        if not msg:
            return {"content": [{"type": "text", "text": f"Could not fetch message {message_id}"}]}

        rules = _load_rules()
        result = classify_email(msg, account=account, rules=rules)

        output = {
            "message_id": message_id,
            "from": msg.get("from", ""),
            "subject": msg.get("subject", ""),
            "tier": result.tier,
            "method": result.method,
            "rule_match": result.rule_match,
            "extract_insights": result.extract_insights,
        }
        return {"content": [{"type": "text", "text": json.dumps(output, indent=2)}]}

    @tool(
        "save_reminder",
        "Save a reminder to fire at a specific time. Convert natural-language times (e.g. 'tomorrow 9am', 'in 2 hours') to ISO 8601 WITH timezone, using the [Current time: ...] line in the user's message as reference. Pacific is -07:00 (PDT) or -08:00 (PST). Example: '2026-04-17T09:00:00-07:00'.",
        {
            "text": Annotated[str, f"What to remind {owner_name()} about — short, direct phrasing"],
            "fire_at_iso": Annotated[str, "ISO 8601 timestamp with timezone when the reminder should fire"],
        },
    )
    async def save_reminder(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        from reminders import add_reminder

        try:
            r = add_reminder(args["text"], args["fire_at_iso"])
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}]}
        return {"content": [{"type": "text", "text": f"Reminder saved (id={r['id']}) — fires at {r['fire_at']}"}]}

    @tool(
        "list_reminders",
        "List all pending reminders sorted by fire time.",
        {},
    )
    async def list_reminders(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        from reminders import list_pending

        items = list_pending()
        if not items:
            return {"content": [{"type": "text", "text": "No pending reminders."}]}
        return {"content": [{"type": "text", "text": json.dumps(items, indent=2, default=str)}]}

    @tool(
        "delete_reminder",
        "Cancel a pending reminder by its ID.",
        {
            "reminder_id": Annotated[str, "Short reminder ID (shown when saved or listed)"],
        },
    )
    async def delete_reminder(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        from reminders import delete_reminder as _delete

        ok = _delete(args["reminder_id"])
        return {"content": [{"type": "text", "text": "Deleted." if ok else f"No reminder with id={args['reminder_id']}"}]}

    @tool(
        "tasks_list",
        "List tasks in the review queue (proposed + parked). Returns task IDs, content, labels, due, source.",
        {},
    )
    async def tasks_list(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import tasks as tq
        tq.resurface_parked()
        proposed = tq.list_by_status("proposed")
        parked = tq.list_by_status("parked")
        return {"content": [{"type": "text", "text": json.dumps({"proposed": proposed, "parked": parked}, indent=2, default=str)}]}

    @tool(
        "tasks_propose",
        "Propose a new task to the review queue. Deduped against existing tasks. Returns the created/existing task, or null if previously declined.",
        {
            "content": Annotated[str, "Task title/content"],
            "labels": Annotated[list, "Optional free-form label list for grouping. Usually leave empty."],
            "due_string": Annotated[str, "Natural-language due date (e.g. 'tomorrow', 'next monday'). Optional."],
            "priority": Annotated[int, "Legacy numeric priority 1 (low) to 4 (high). Usually skip; use quadrant instead."],
            "quadrant": Annotated[str, "Eisenhower quadrant: Q1 urgent+important, Q2 important-not-urgent (protect this), Q3 urgent-not-important, Q4 neither. Optional."],
            "duration_minutes": Annotated[int, "Estimated task duration in minutes — used to size the calendar block when the task is scheduled. Min 15. Typical: tick-box 15, short 30, focused 60, deep work 90-120."],
            "source": Annotated[str, "Source tag — e.g. 'heartbeat:gmail:personal' or 'chat:manual'"],
            "project": Annotated[str, f"Project slug: {_projects_roster}. Inferred from source when omitted."],
            "task_type": Annotated[str, f"action (default — something to do), idea (someday/maybe, no deadline), waiting ({owner_name()} sent something and is blocked on a reply), delegated (handed off, needs follow-up)."],
            "who": Annotated[str, "For waiting/delegated tasks: the person it's blocked on."],
            "due_date": Annotated[str, "Explicit due date YYYY-MM-DD. Optional — otherwise derived from due_string."],
        },
    )
    async def tasks_propose(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import tasks as tq
        try:
            t = tq.propose(
                content=args["content"],
                labels=args.get("labels") or None,
                due_string=args.get("due_string"),
                priority=args.get("priority"),
                quadrant=args.get("quadrant") or None,
                duration_minutes=args.get("duration_minutes"),
                source=args.get("source") or "chat:manual",
                project=args.get("project") or None,
                task_type=args.get("task_type") or None,
                who=args.get("who") or None,
                due_date=args.get("due_date") or None,
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Invalid input: {e}"}]}
        tq.render_queue()
        return {"content": [{"type": "text", "text": json.dumps(t, indent=2, default=str) if t else "Skipped — deduplicated against a previously declined task."}]}

    @tool(
        "tasks_approve",
        f"Accept a task into the active list — {owner_name()} has committed to doing it. Optional quadrant overrides the Eisenhower quadrant at approval. Optional duration_minutes overrides the estimated block length (min 15). Does not schedule it; use tasks_schedule to block calendar time.",
        {
            "task_id": Annotated[str, "Task id (e.g. t_a1b2c3d4)"],
            "quadrant": Annotated[str, "Override/set Eisenhower quadrant: Q1/Q2/Q3/Q4. Optional — defaults to whatever is on the task."],
            "duration_minutes": Annotated[int, "Override/set estimated duration in minutes. Min 15. Optional — defaults to whatever is on the task, or 15 if unset."],
        },
    )
    async def tasks_approve(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import tasks as tq

        t = tq.get(args["task_id"])
        if not t:
            return {"content": [{"type": "text", "text": f"Task not found: {args['task_id']}"}]}

        override_quadrant = args.get("quadrant") or None
        effective_quadrant = override_quadrant or t.get("quadrant")

        override_duration_raw = args.get("duration_minutes")
        try:
            override_duration = tq.parse_duration(override_duration_raw) if override_duration_raw else None
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Invalid duration: {e}"}]}
        effective_duration = override_duration or t.get("duration_minutes") or tq.MIN_DURATION_MINUTES

        try:
            tq.approve(
                args["task_id"],
                quadrant=override_quadrant,
                duration_minutes=override_duration,
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Invalid input: {e}"}]}

        tq.render_queue()
        tq.render_archive()
        qtag = f" [{effective_quadrant}]" if effective_quadrant else ""
        dtag = f" {effective_duration}m" if effective_duration else ""
        return {"content": [{"type": "text", "text": f"Approved{qtag}{dtag}: {t['content']}"}]}

    @tool(
        "tasks_park",
        "Park a task until a future date. Hidden from queue until then.",
        {
            "task_id": Annotated[str, "Task id"],
            "until": Annotated[str, "YYYY-MM-DD or relative (e.g. '3d', '7d')"],
        },
    )
    async def tasks_park(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import tasks as tq
        result = tq.park(args["task_id"], until=args["until"])
        if result is None:
            return {"content": [{"type": "text", "text": f"Task not found: {args['task_id']}"}]}
        tq.render_queue()
        return {"content": [{"type": "text", "text": f"Parked until {result['parked_until']}"}]}

    @tool(
        "tasks_decline",
        f"Permanently decline a task. Won't re-propose the same thing (dedup blocks it). Use when {owner_name()} explicitly says he is NOT going to do something.",
        {
            "task_id": Annotated[str, "Task id"],
        },
    )
    async def tasks_decline(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import tasks as tq
        result = tq.decline(args["task_id"])
        if result is None:
            return {"content": [{"type": "text", "text": f"Task not found: {args['task_id']}"}]}
        tq.render_queue()
        tq.render_archive()
        return {"content": [{"type": "text", "text": f"Declined: {result['content']}"}]}

    @tool(
        "tasks_complete",
        f"Mark a task as already done (completed outside Cortex, e.g. done manually). Use when {owner_name()} says 'done', 'finished', 'already did this', or similar. Archives the task and blocks re-proposal. Different from decline — this is a win, not a skip.",
        {
            "task_id": Annotated[str, "Task id (e.g. t_a1b2c3d4)"],
        },
    )
    async def tasks_complete(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import tasks as tq
        result = tq.complete(args["task_id"])
        if result is None:
            return {"content": [{"type": "text", "text": f"Task not found: {args['task_id']}"}]}
        tq.render_queue()
        tq.render_archive()
        return {"content": [{"type": "text", "text": f"Marked complete: {result['content']}"}]}

    @tool(
        "tasks_touch",
        f"Reset the staleness clock on a waiting/delegated task — use when {owner_name()} says he chased it, or a reply came in but the task isn't finished. Stops it being flagged as silent.",
        {
            "task_id": Annotated[str, "Task id (e.g. t_a1b2c3d4)"],
        },
    )
    async def tasks_touch(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import tasks as tq
        result = tq.touch(args["task_id"])
        if result is None:
            return {"content": [{"type": "text", "text": f"Task not found: {args['task_id']}"}]}
        tq.render_queue()
        return {"content": [{"type": "text", "text": f"Staleness reset: {result['content']}"}]}

    @tool(
        "tasks_today",
        f"What {owner_name()} should actually do today — deadline-driven first, then this week's focus, then Eisenhower order, sized to the hours still free on his calendar. Use when he asks what to work on, what's the plan, or what matters today. This is computed, not your judgement: present it as-is.",
        {
            "open_hours": Annotated[float, "Override the free-hours estimate instead of reading the calendar. Optional."],
            "account": Annotated[str, "Calendar account to read (default: personal)."],
        },
    )
    async def tasks_today(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        from datetime import datetime
        import tasks as tq
        import focus as fc

        if args.get("open_hours") is not None:
            open_minutes = int(float(args["open_hours"]) * 60)
        else:
            from integrations.gcal import agenda_today
            try:
                events = agenda_today(account=args.get("account") or "personal")
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Couldn't read the calendar: {e}"}]}
            open_minutes = tq.free_minutes(events, now=datetime.now(tq.PST))

        focus_texts = fc.texts()
        picks = tq.pick_today(tq.load_all(), open_minutes=open_minutes, focus_texts=focus_texts)
        payload = {
            "open_minutes": open_minutes,
            "focus": focus_texts,
            "picks": [
                {
                    "id": t["id"],
                    "content": t["content"],
                    "minutes": t.get("duration_minutes") or tq.MIN_DURATION_MINUTES,
                    "reason": tq.pick_reason(t, focus_texts),
                    "project": t.get("project"),
                }
                for t in picks
            ],
        }
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]}

    @tool(
        "tasks_schedule",
        f"Block time on {owner_name()}'s calendar for tasks — this CREATES real calendar events. Use only when he explicitly asks to schedule or block time. Omit task_ids to schedule today's suggested picks. Confirm with him before calling if there is any doubt.",
        {
            "task_ids": Annotated[list, "Task ids to schedule. Omit to use today's suggested picks."],
            "at": Annotated[str, "Force the first block to start at HH:MM (24h). Optional."],
            "account": Annotated[str, "Calendar account to write to (default: personal)."],
        },
    )
    async def tasks_schedule(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        from datetime import datetime
        import tasks as tq
        import focus as fc
        from integrations.gcal import agenda_today, create_event

        account = args.get("account") or "personal"
        try:
            events = agenda_today(account=account)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Couldn't read the calendar: {e}"}]}

        now = datetime.now(tq.PST)
        if args.get("at"):
            try:
                hh, mm = (int(x) for x in str(args["at"]).split(":"))
                now = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            except (ValueError, TypeError):
                return {"content": [{"type": "text", "text": f"Invalid 'at' time: {args['at']!r} (use HH:MM)"}]}

        slots = tq.free_slots(events, now=now)
        if not slots:
            return {"content": [{"type": "text", "text": "No free working time left today."}]}

        ids = args.get("task_ids") or []
        if ids:
            chosen = [tq.get(i) for i in ids]
            missing = [i for i, t in zip(ids, chosen) if not t]
            if missing:
                return {"content": [{"type": "text", "text": f"Task(s) not found: {', '.join(missing)}"}]}
        else:
            chosen = tq.pick_today(
                tq.load_all(),
                open_minutes=tq.free_minutes(events, now=now),
                focus_texts=fc.texts(),
            )
            if not chosen:
                return {"content": [{"type": "text", "text": "Nothing to schedule — the daily pick is empty."}]}

        placed = tq.assign_slots(chosen, slots)
        if not placed:
            return {"content": [{"type": "text", "text": "Nothing fits in the remaining free time."}]}

        lines, created = [], 0
        for t, start, end in placed:
            try:
                ev = create_event(
                    account=account,
                    summary=t["content"][:200],
                    description=f"Cortex task {t['id']}",
                    start_iso=start.isoformat(),
                    end_iso=end.isoformat(),
                )
            except Exception as e:
                lines.append(f"FAILED {t['content'][:40]}: {e}")
                continue
            if not ev:
                lines.append(f"FAILED {t['content'][:40]}: calendar rejected the event")
                continue
            tq.mark_scheduled(t["id"], event_id=str(ev.get("id", "")), start_iso=start.isoformat())
            created += 1
            lines.append(f"{start:%-I:%M%p}-{end:%-I:%M%p} {t['content'][:60]}")

        unplaced = [t for t in chosen if not any(p[0]["id"] == t["id"] for p in placed)]
        if unplaced:
            lines.append(f"({len(unplaced)} didn't fit today)")
        tq.render_queue()
        header = f"Blocked {created} of {len(placed)} on the {account} calendar:"
        return {"content": [{"type": "text", "text": header + "\n" + "\n".join(lines)}]}

    @tool(
        "focus_show",
        f"Show {owner_name()}'s focus for the current week — the 1-3 things this week is for. Returns null if none is set (or if the stored one is from a past week). Check this before advising on priorities.",
        {},
    )
    async def focus_show(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import focus as fc
        data = fc.current()
        if not data:
            stale = fc.load()
            note = (
                f"No focus for this week. Last set for week of {stale['week_of']}."
                if stale else "No focus set."
            )
            return {"content": [{"type": "text", "text": note}]}
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2, default=str)}]}

    @tool(
        "focus_set",
        f"Set {owner_name()}'s focus for the current week (1-3 items, replaces any existing). Use when he says what this week is for — 'this week is about X', 'my focus is Y'. Extras beyond 3 are dropped.",
        {
            "items": Annotated[list, f"1-3 focus items. Each is either a plain string, or {{text, project}} where project is a slug like {_projects_roster}."],
        },
    )
    async def focus_set(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import focus as fc
        import tasks as tq
        try:
            data = fc.set_focus(args.get("items") or [])
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Invalid focus: {e}"}]}
        tq.render_queue()
        summary = "; ".join(i["text"] for i in data["items"])
        return {"content": [{"type": "text", "text": f"Focus set for week of {data['week_of']}: {summary}"}]}

    @tool(
        "tasks_stats",
        "Eisenhower breakdown of approved/parked/declined tasks over the past N days (default 7). Returns quadrant counts, firefighting_ratio (Q1+Q3 approved / total), and a one-line summary suitable for a weekly brief.",
        {
            "days": Annotated[int, "Lookback window in days. Default 7."],
        },
    )
    async def tasks_stats(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import tasks as tq
        stats = tq.weekly_stats(days=args.get("days") or 7)
        return {"content": [{"type": "text", "text": json.dumps(stats, indent=2, default=str)}]}

    # ── Practices: StateShift-inspired somatic library ────────────────────

    @tool(
        "practice_list",
        "List somatic / nervous-system regulation practices in the library. Returns name, duration, category, and which states each serves. Optionally filter by category ('Feel Calm', 'Relax', 'Super Focus').",
        {
            "category": Annotated[str, "Optional category filter: 'Feel Calm', 'Relax', or 'Super Focus'. Omit to return all."],
        },
    )
    async def practice_list(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import practices as pr
        library = pr.load_library(vault_dir)
        category_filter = args.get("category")
        if category_filter:
            library = [p for p in library if p.category.lower() == category_filter.lower()]
        result = [
            {
                "slug": p.slug,
                "name": p.name,
                "duration_min": p.duration_min,
                "category": p.category,
                "current_states": p.current_states,
                "target_states": p.target_states,
            }
            for p in library
        ]
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

    @tool(
        "practice_recommend",
        f"Recommend somatic practices that bridge a current state to a target state. StateShift-style: e.g. anxious->calm returns Physiological Sigh, Cadence Breathing, etc. Returns the full practice body (instructions) so you can deliver it inline. If current_state or target_state is omitted, returns nothing — ask {owner_name()} what he wants to shift toward instead of guessing.",
        {
            "current_state": Annotated[str, f"How {owner_name()} feels right now: anxious, tired, stressed, overwhelmed, restless, angry, numb, lonely, hopeless, jealous, insecure, bored, confused, unmotivated."],
            "target_state": Annotated[str, f"How {owner_name()} wants to feel: calm, level, relaxed, focused, energized, motivated, grateful, joyful, hopeful, loved, inspired, happy, curious, creative, chill, confident."],
            "limit": Annotated[int, "Max practices to return (default 3)."],
        },
    )
    async def practice_recommend(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import practices as pr
        library = pr.load_library(vault_dir)
        recent = pr.recent_practice_slugs(vault_dir, library, hours=24)
        matches = pr.recommend(
            library,
            args["current_state"],
            args["target_state"],
            recent_practice_slugs=recent,
        )
        limit = args.get("limit") or 3
        result = []
        for p in matches[:limit]:
            title, message = pr.format_nudge(
                p, args["current_state"], args["target_state"], name=owner_name()
            )
            result.append({
                "slug": p.slug,
                "name": p.name,
                "duration_min": p.duration_min,
                "category": p.category,
                "framing_title": title,
                "framing_message": message,
                "body": p.body,
                "done_recently": p.slug in recent,
            })
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

    @tool(
        "practice_log",
        f"Log a completed practice session to vault/practices/sessions.md. Use after {owner_name()} says 'done', 'did it', or describes how a practice went. Pass whatever fields you have — name is required, the rest are optional.",
        {
            "name": Annotated[str, "Practice name (e.g. 'Physiological Sigh'). Must match the library exactly for stats; freeform OK if it's something else."],
            "current_state": Annotated[str, f"Optional: state {owner_name()} was in before."],
            "target_state": Annotated[str, "Optional: state he was shifting toward."],
            "duration_min": Annotated[int, "Optional: actual duration in minutes."],
            "trigger": Annotated[str, "Where the practice came from: 'chat' (pull), 'heartbeat' (push), or 'manual'. Default 'chat'."],
            "outcome": Annotated[str, "Optional: free-text outcome — 'felt calmer', 'RHR settled', 'didn't help'. Honest is better than positive."],
        },
    )
    async def practice_log(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import practices as pr
        path = pr.log_session(
            vault_dir,
            practice_name=args["name"],
            current_state=args.get("current_state"),
            target_state=args.get("target_state"),
            duration_min=args.get("duration_min"),
            trigger=args.get("trigger") or "chat",
            outcome=args.get("outcome"),
        )
        return {"content": [{"type": "text", "text": f"Logged to {path}"}]}

    # ── Opportunities: lightweight CRM ────────────────────────────────────

    @tool(
        "opportunity_list",
        "List opportunities from vault/opportunities/. By default returns open + active. Set 'stale': true to surface ones not touched in 21+ days, or 'due': true for ones with next_step_due in the next 2 days. Use 'status' to filter by exact status.",
        {
            "status": Annotated[str, "Filter by status: open | active | won | lost | dormant. Optional."],
            "stale": Annotated[bool, "If true, return only stale (open, no touch in threshold_days, no future due). Default false."],
            "due": Annotated[bool, "If true, return only those with next_step_due in next 2 days. Default false."],
            "all": Annotated[bool, "If true, include closed (won/lost/dormant). Default false."],
        },
    )
    async def opportunity_list(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import opportunities as ops
        if args.get("stale"):
            items = ops.list_stale()
        elif args.get("due"):
            items = ops.list_due()
        elif args.get("status"):
            items = [o for o in ops.load_all() if o.status == args["status"]]
        elif args.get("all"):
            items = ops.load_all()
        else:
            items = ops.list_open()
        result = [ops.summary_dict(o) for o in items]
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]}

    @tool(
        "opportunity_show",
        f"Show one opportunity in full — frontmatter + Context/Shape/Log body. Use when {owner_name()} asks 'what's the status with X' or 'remind me about the Matt thing'.",
        {
            "slug": Annotated[str, "Opportunity slug (e.g. 'matt-baba'). Use opportunity_list to discover slugs."],
        },
    )
    async def opportunity_show(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import opportunities as ops
        o = ops.load(args["slug"])
        if not o:
            return {"content": [{"type": "text", "text": f"Not found: {args['slug']}"}]}
        return {"content": [{"type": "text", "text": json.dumps({**ops.summary_dict(o), "body": o.body}, indent=2, default=str)}]}

    @tool(
        "opportunity_add",
        f"Create a new opportunity in vault/opportunities/. Use this when {owner_name()} describes a new conversation, intro, or potential deal that should be tracked. Slug is auto-derived from name. Stage: spark (just emerged) → qualifying → shaped → proposed → committed. Status: open (active in pipeline) → active (work in flight) → won/lost/dormant. Value band is qualitative (nano/small/medium/large/strategic). For milestone-gated next steps, prefix next_step with 'When ' (e.g. 'When AIOS v1 ships') so it skips staleness nudges.",
        {
            "name": Annotated[str, "Display name. For person-keyed opportunities use the person's name."],
            "source": Annotated[str, "How it surfaced (e.g. 'in-person', 'network-intro', 'cold-email-reply')."],
            "status": Annotated[str, "open | active | won | lost | dormant. Default 'open'."],
            "stage": Annotated[str, "spark | qualifying | shaped | proposed | committed. Default 'spark'."],
            "relationship": Annotated[str, "cold | warm | hot. Default 'warm'."],
            "value_band": Annotated[str, "Qualitative: nano | small | medium | large | strategic. Skip if unclear."],
            "shape": Annotated[list, "Deal shapes on the table (e.g. ['collab', 'white-label', 'referrer'])."],
            "next_step": Annotated[str, "Next move. Prefix with 'When ' for milestone-gated."],
            "next_step_due": Annotated[str, "YYYY-MM-DD due date. Optional."],
            "tags": Annotated[list, "Free-form tags."],
            "initial_log": Annotated[str, "First log entry — what triggered this opportunity. Today's date is prepended automatically."],
            "initial_context": Annotated[str, "Initial Context section — why this is interesting, who introduced who."],
        },
    )
    async def opportunity_add(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import opportunities as ops
        try:
            o = ops.add(
                name=args["name"],
                source=args.get("source") or "chat",
                status=args.get("status") or "open",
                stage=args.get("stage") or "spark",
                relationship=args.get("relationship") or "warm",
                value_band=args.get("value_band"),
                shape=args.get("shape"),
                next_step=args.get("next_step"),
                next_step_due=args.get("next_step_due"),
                tags=args.get("tags"),
                initial_log=args.get("initial_log"),
                initial_context=args.get("initial_context"),
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Invalid input: {e}"}]}
        return {"content": [{"type": "text", "text": json.dumps(ops.summary_dict(o), indent=2, default=str)}]}

    @tool(
        "opportunity_touch",
        f"Append a log entry to an opportunity and bump last_touched. Use whenever {owner_name()} mentions a new development on an existing opportunity ('texted Matt today', 'Jay replied', 'sent the deck'). Optionally advance stage or status at the same time.",
        {
            "slug": Annotated[str, "Opportunity slug."],
            "log_entry": Annotated[str, "The log line — short, one sentence. Today's date is prepended automatically."],
            "advance_stage": Annotated[str, "Optionally advance to: spark | qualifying | shaped | proposed | committed."],
            "advance_status": Annotated[str, "Optionally change to: open | active | won | lost | dormant. (Use opportunity_close for terminal status with a closing note.)"],
        },
    )
    async def opportunity_touch(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import opportunities as ops
        try:
            o = ops.touch(
                args["slug"],
                args["log_entry"],
                advance_stage=args.get("advance_stage"),
                advance_status=args.get("advance_status"),
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Invalid input: {e}"}]}
        if not o:
            return {"content": [{"type": "text", "text": f"Not found: {args['slug']}"}]}
        return {"content": [{"type": "text", "text": json.dumps(ops.summary_dict(o), indent=2, default=str)}]}

    @tool(
        "opportunity_update",
        "Patch an opportunity's frontmatter without adding a log entry. Use for: changing next_step / next_step_due, updating shape list, adjusting value_band as info firms up. Pass 'null' (string) for next_step_due to clear it.",
        {
            "slug": Annotated[str, "Opportunity slug."],
            "name": Annotated[str, "New display name."],
            "status": Annotated[str, "open | active | won | lost | dormant."],
            "stage": Annotated[str, "spark | qualifying | shaped | proposed | committed."],
            "relationship": Annotated[str, "cold | warm | hot."],
            "value_band": Annotated[str, "nano | small | medium | large | strategic."],
            "shape": Annotated[list, "Replaces existing shape list."],
            "next_step": Annotated[str, "New next step (prefix 'When ' for milestone-gated)."],
            "next_step_due": Annotated[str, "YYYY-MM-DD or 'null' to clear."],
            "tags": Annotated[list, "Replaces existing tags."],
            "bump_last_touched": Annotated[bool, "Set last_touched to today. Default false."],
        },
    )
    async def opportunity_update(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import opportunities as ops
        try:
            o = ops.update(
                args["slug"],
                name=args.get("name"),
                status=args.get("status"),
                stage=args.get("stage"),
                relationship=args.get("relationship"),
                value_band=args.get("value_band"),
                shape=args.get("shape"),
                next_step=args.get("next_step"),
                next_step_due=args.get("next_step_due"),
                tags=args.get("tags"),
                bump_last_touched=bool(args.get("bump_last_touched")),
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Invalid input: {e}"}]}
        if not o:
            return {"content": [{"type": "text", "text": f"Not found: {args['slug']}"}]}
        return {"content": [{"type": "text", "text": json.dumps(ops.summary_dict(o), indent=2, default=str)}]}

    @tool(
        "opportunity_close",
        "Close an opportunity as won, lost, or dormant. Appends a closure log entry. Dormant = paused indefinitely (re-openable later); lost = decided no; won = signed/committed.",
        {
            "slug": Annotated[str, "Opportunity slug."],
            "status": Annotated[str, "Terminal status: won | lost | dormant."],
            "note": Annotated[str, "Optional closure note — what happened, what was learned."],
        },
    )
    async def opportunity_close(args: dict) -> dict:
        sys.path.insert(0, str(scripts_dir))
        import opportunities as ops
        try:
            o = ops.close(args["slug"], args["status"], note=args.get("note"))
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Invalid input: {e}"}]}
        if not o:
            return {"content": [{"type": "text", "text": f"Not found: {args['slug']}"}]}
        return {"content": [{"type": "text", "text": json.dumps(ops.summary_dict(o), indent=2, default=str)}]}

    # Generic tools always present. Everything below is layer-10+: a lane
    # whose backing module (tasks, focus, opportunities, practices, reminders,
    # inbox) will not exist in a shareable/extracted copy of Cortex. Each
    # group is appended only when its module actually imports — a missing
    # module means the lane's tools simply aren't offered to the model,
    # rather than crashing server startup.
    cortex_tool_list = [
        memory_search, gmail_triage, gmail_read, gcal_today, gcal_upcoming,
        vault_read, integration_status, capture_insight,
    ]
    if lane_available("inbox"):
        cortex_tool_list += [gmail_inbox_status, gmail_inbox_classify]
    if lane_available("reminders"):
        cortex_tool_list += [save_reminder, list_reminders, delete_reminder]
    if lane_available("tasks"):
        cortex_tool_list += [
            tasks_list, tasks_propose, tasks_approve, tasks_park, tasks_decline,
            tasks_complete, tasks_touch, tasks_stats,
        ]
    if lane_available("focus"):
        cortex_tool_list.append(focus_show)
    if lane_available("tasks", "focus"):
        cortex_tool_list += [tasks_today, tasks_schedule, focus_set]
    if lane_available("practices"):
        cortex_tool_list += [practice_list, practice_recommend, practice_log]
    if lane_available("opportunities"):
        cortex_tool_list += [
            opportunity_list, opportunity_show, opportunity_add,
            opportunity_touch, opportunity_update, opportunity_close,
        ]

    return create_sdk_mcp_server(
        name="cortex",
        tools=cortex_tool_list,
    )
