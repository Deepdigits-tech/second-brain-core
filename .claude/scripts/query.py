#!/usr/bin/env python3
"""Unified CLI for Cortex OS integrations.

Usage:
    query.py gmail triage [--account <alias>] [--max 20] [--query "is:unread"]
    query.py gmail read --id <msg_id> [--account personal]
    query.py gmail draft --to <email> --subject <subj> --body <body> [--account personal] [--html]
    query.py gmail labels [--account personal]
    query.py gmail triage-all [--max 20]
    query.py gcal today [--account personal]
    query.py gcal upcoming [--days 3] [--account personal]
    query.py gcal week [--account personal]
    query.py gcal calendars [--account personal]
    query.py telegram send --text <message> [--chat-id <id>]
    query.py slack send --text <message> [--channel <id>]
    query.py youtube add <url> [--kb gtm|aios] [--slug <slug>] [--lang en] [--overwrite]
    query.py youtube transcript <url> [--no-timestamps]
    query.py youtube probe <url>
    query.py status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add scripts dir to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from integrations.registry import load_env, check_all
from shared import lane_available, projects as _cortex_projects, absent_modules as _cortex_absent_modules


def _projects_roster_text() -> str:
    """Render the configured project roster for CLI help text (Rule 1: no
    client/business slug list hardcoded here — see shared.projects())."""
    return ", ".join(_cortex_projects())


def cmd_gmail(args):
    from integrations.gmail import triage, read_message, create_draft, list_labels, triage_all_accounts, ensure_labels

    if args.gmail_cmd == "triage":
        results = triage(account=args.account, max_results=args.max, query=args.query)
        _output(results, args.format)
    elif args.gmail_cmd == "read":
        result = read_message(args.id, account=args.account)
        _output(result, args.format)
    elif args.gmail_cmd == "draft":
        result = create_draft(to=args.to, subject=args.subject, body=args.body, account=args.account, html=args.html)
        _output(result, args.format)
    elif args.gmail_cmd == "labels":
        results = list_labels(account=args.account)
        _output(results, args.format)
    elif args.gmail_cmd == "triage-all":
        results = triage_all_accounts(max_results=args.max)
        _output(results, args.format)
    elif args.gmail_cmd == "inbox-setup":
        _cmd_inbox_setup(args)
    elif args.gmail_cmd == "inbox-status":
        _cmd_inbox_status(args)
    elif args.gmail_cmd == "inbox-review":
        _cmd_inbox_review(args)
    elif args.gmail_cmd == "inbox-history":
        _cmd_inbox_history(args)
    elif args.gmail_cmd == "inbox-rules":
        _cmd_inbox_rules(args)


def _cmd_inbox_setup(args):
    """Create Gmail labels and initialize state files."""
    from integrations.gmail import ensure_labels, ACCOUNT_USER_IDS
    from inbox.classifier import load_mode, save_mode, _default_account_mode

    print("Setting up inbox management...")
    for account in ACCOUNT_USER_IDS:
        labels = ensure_labels(account=account)
        print(f"  [{account}] Labels created/verified: {list(labels.keys())}")

    # Initialize mode if not exists
    mode = load_mode()
    for account in ACCOUNT_USER_IDS:
        if account not in mode:
            mode[account] = _default_account_mode()
    save_mode(mode)
    print("  State files initialized.")
    print("Done. Run 'query.py gmail inbox-status' to check status.")


def _cmd_inbox_status(args):
    """Show mode, pending reviews, and graduation progress."""
    from inbox.classifier import inbox_status_summary

    summary = inbox_status_summary()
    if args.format == "json":
        _output(summary, "json")
        return

    for account, info in summary.items():
        print(f"\n=== {account} ===")
        print(f"  Mode: {info['mode']}")
        print(f"  Auto tiers: {info['auto_tiers'] or 'none'}")
        print(f"  Days supervised: {info['days_supervised']}")
        print(f"  Pending review: {info['pending_review']}")
        print(f"  Recent actions (24h): {info['recent_actions_24h']}")
        print(f"  Approvals: {info['approvals']}")
        print(f"  Rejections: {info['rejections']}")
        if info['graduation_eligible']:
            print(f"  *** GRADUATION ELIGIBLE: {info['graduation_eligible']} ***")


def _cmd_inbox_review(args):
    """Print pending review items from vault/inbox-review.md."""
    from pathlib import Path
    from inbox.classifier import REVIEW_FILE_PATH

    if not REVIEW_FILE_PATH.exists():
        print("No review file found. Run heartbeat to generate proposals.")
        return
    print(REVIEW_FILE_PATH.read_text())


def _cmd_inbox_history(args):
    """Show recent actions from the audit log."""
    from inbox.classifier import get_recent_actions

    hours = getattr(args, "hours", 24)
    recent = get_recent_actions(hours=hours)
    if not recent:
        print(f"No actions in the last {hours}h.")
        return
    _output(recent, args.format)


def _cmd_inbox_rules(args):
    """Print current rules summary."""
    from inbox.classifier import _load_rules, RULES_PATH

    if not RULES_PATH.exists():
        print(f"Rules file not found: {RULES_PATH}")
        return

    rules = _load_rules()
    if args.format == "json":
        _output(rules, "json")
        return

    for account, acct_rules in rules.get("accounts", {}).items():
        print(f"\n=== {account} ===")
        tier_rules = acct_rules.get("tier_rules", {})
        for tier, trules in tier_rules.items():
            domains = trules.get("domains", [])
            senders = trules.get("senders", [])
            patterns = trules.get("subject_patterns", [])
            cats = trules.get("gmail_categories", [])
            total = len(domains) + len(senders) + len(patterns) + len(cats)
            if total:
                print(f"  {tier}: {total} rules ({len(domains)} domains, {len(senders)} senders, {len(patterns)} patterns, {len(cats)} categories)")
        vips = acct_rules.get("vip_senders", [])
        if vips:
            print(f"  VIP senders: {vips}")


def cmd_gcal(args):
    from integrations.gcal import agenda_today, agenda_upcoming, agenda_week, list_calendars

    if args.gcal_cmd == "today":
        results = agenda_today(account=args.account)
        _output(results, args.format)
    elif args.gcal_cmd == "upcoming":
        results = agenda_upcoming(days=args.days, account=args.account)
        _output(results, args.format)
    elif args.gcal_cmd == "week":
        results = agenda_week(account=args.account)
        _output(results, args.format)
    elif args.gcal_cmd == "calendars":
        results = list_calendars(account=args.account)
        _output(results, args.format)


def cmd_telegram(args):
    from integrations.telegram_bot import send_message_sync

    if args.telegram_cmd == "send":
        result = send_message_sync(text=args.text, chat_id=args.chat_id)
        _output(result, args.format)


def cmd_slack(args):
    from integrations.slack_bot import send_message

    if args.slack_cmd == "send":
        result = send_message(text=args.text, channel=args.channel)
        _output(result, args.format)


def cmd_smartlead(args):
    from integrations.smartlead import (
        list_campaigns, get_campaign, get_campaign_stats,
        list_leads, add_leads, pause_campaign, resume_campaign,
        get_lead, get_email_accounts,
    )

    if args.sl_cmd == "campaigns":
        results = list_campaigns(status=args.status)
        _output(results, args.format)
    elif args.sl_cmd == "campaign":
        result = get_campaign(args.id)
        _output(result, args.format)
    elif args.sl_cmd == "stats":
        result = get_campaign_stats(args.id)
        _output(result, args.format)
    elif args.sl_cmd == "leads":
        results = list_leads(args.id, offset=args.offset, limit=args.limit)
        _output(results, args.format)
    elif args.sl_cmd == "add-leads":
        raw = json.loads(args.leads_json)
        result = add_leads(args.id, raw)
        _output(result, args.format)
    elif args.sl_cmd == "pause":
        result = pause_campaign(args.id)
        _output(result, args.format)
    elif args.sl_cmd == "resume":
        result = resume_campaign(args.id)
        _output(result, args.format)
    elif args.sl_cmd == "lead":
        result = get_lead(args.email)
        _output(result, args.format)
    elif args.sl_cmd == "email-accounts":
        results = get_email_accounts()
        _output(results, args.format)


def cmd_body(args):
    """Body / weekly health report commands."""
    if args.body_cmd == "weekly":
        import asyncio
        import weekly_body_report
        from datetime import date
        end_day = date.fromisoformat(args.end) if args.end else None
        path = asyncio.run(weekly_body_report.generate(end_day=end_day, notify=not args.no_notify))
        print(f"Weekly body report written: {path}")
    elif args.body_cmd == "weekly-dossier":
        # Dossier only, no LLM call — fast smoke test
        from datetime import date
        from weekly_body_report import (
            _week_bounds, aggregate_garmin_week, aggregate_tasks_week,
            aggregate_calendar_week, build_dossier,
        )
        end_day = date.fromisoformat(args.end) if args.end else None
        start, end = _week_bounds(end_day)
        g = aggregate_garmin_week(start, end)
        t = aggregate_tasks_week()
        c = aggregate_calendar_week(start, end)
        print(build_dossier(g, t, c))


def cmd_dashboard(args):
    from dashboard_snapshot import SNAPSHOT_PATH, write_snapshot
    import json as _json

    if args.dashboard_cmd == "refresh":
        path = write_snapshot()
        _output({"written": str(path)}, args.format)
    elif args.dashboard_cmd == "show":
        if not SNAPSHOT_PATH.exists():
            _output({"error": "no snapshot yet — run: query.py dashboard refresh"}, args.format)
            return
        _output(_json.loads(SNAPSHOT_PATH.read_text()), args.format)


def cmd_invest(args):
    """invest-os — market snapshot, regime read, insider scan.

    Analysis only: nothing here places an order or touches an account.
    """
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent / "invest"))
    import market as _market

    if args.invest_cmd == "refresh":
        snap = _market.build_snapshot()
        path = _market.write_snapshot(snap)
        _output({"written": str(path), "instruments": len(snap["instruments"]),
                 "alerts": len(snap["alerts"]), "degraded": snap["degraded"],
                 "unavailable": snap["unavailable"]}, args.format)
    elif args.invest_cmd == "show":
        snap = _market.read_snapshot()
        if snap is None:
            _output({"error": "no snapshot yet — run: query.py invest refresh"}, args.format)
            return
        if args.format == "text":
            print(_market.render_text(snap))
        else:
            _output(snap, args.format)
    elif args.invest_cmd == "brief":
        snap = _market.read_snapshot() or _market.build_snapshot()
        print(_market.render_text(snap))
    elif args.invest_cmd == "regime":
        _output(_market.latest_regime() or {"error": "no regime read yet"}, args.format)
    elif args.invest_cmd == "signals":
        _output(_market.recent_signals(days=args.days, limit=args.limit), args.format)


def cmd_garmin(args):
    from integrations.garmin import (
        today_snapshot, daily_steps, sleep_summary, hrv_summary,
        body_battery, stress_summary, recent_activities, GarminAuthError,
    )

    try:
        if args.garmin_cmd == "today":
            result = today_snapshot()
        elif args.garmin_cmd == "steps":
            result = daily_steps(args.day)
        elif args.garmin_cmd == "sleep":
            result = sleep_summary(args.day)
        elif args.garmin_cmd == "hrv":
            result = hrv_summary(args.day)
        elif args.garmin_cmd == "battery":
            result = body_battery(args.day)
        elif args.garmin_cmd == "stress":
            result = stress_summary(args.day)
        elif args.garmin_cmd == "activities":
            result = recent_activities(limit=args.limit)
        else:
            print(f"Unknown garmin command: {args.garmin_cmd}")
            return
    except GarminAuthError as e:
        print(f"Garmin auth error: {e}", file=sys.stderr)
        sys.exit(1)

    _output(result, args.format)


def cmd_chat(args):
    # Add .claude to path so chat package resolves
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from chat.server import main as chat_main
    if args.chat_cmd == "start":
        chat_main()


def cmd_health(args):
    from health_check import run_all_checks

    morning = getattr(args, "full", False)
    report = run_all_checks(morning=morning)

    if args.format == "json":
        print(json.dumps(report, indent=2))
        return

    status_icon = {"healthy": "+", "degraded": "~", "critical": "!"}
    print(f"System Health: [{status_icon.get(report['status'], '?')}] {report['status'].upper()}")
    print()
    for name, result in report["checks"].items():
        icon = "+" if result["status"] == "ok" else "~" if result["status"] == "warn" else "!"
        print(f"  [{icon}] {name}: {result['detail']}")
    if report["alerts"]:
        print(f"\nAlerts ({len(report['alerts'])}):")
        for alert in report["alerts"]:
            print(f"  - {alert}")


def cmd_status(args):
    status = check_all(verify=not getattr(args, "no_verify", False))
    if args.format == "json":
        print(json.dumps(status, indent=2))
    else:
        print("Integration Status:")
        for name, info in status.items():
            icon = "+" if info["available"] else "-"
            print(f"  [{icon}] {name}: {info['reason']}")
            for alias, ok in sorted(info.get("accounts", {}).items()):
                print(f"        [{'+' if ok else '-'}] {alias}")

        absent = _cortex_absent_modules()
        if absent:
            print(f"\noptional lanes not installed: {', '.join(sorted(absent))}")


def cmd_tasks(args):
    """Task queue commands."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tasks as tq

    if args.tasks_cmd == "list":
        tq.resurface_parked()
        tq.render_queue()
        proposed = tq.list_by_status("proposed")
        parked = tq.list_by_status("parked")
        if not proposed and not parked:
            print("No tasks in queue.")
            return

        if proposed:
            print(f"## Proposed ({len(proposed)})")
            # Group by quadrant for the same "shape of your week at a glance" view as queue.md
            groups = {q: [] for q in tq.QUADRANTS}
            groups["U"] = []
            for t in proposed:
                q = t.get("quadrant")
                groups[q if q in tq.QUADRANTS else "U"].append(t)

            section_order = [
                ("Q1", f"Q1 · {tq.QUADRANT_LABELS['Q1']}", ""),
                ("Q2", f"Q2 · {tq.QUADRANT_LABELS['Q2']}", "  <- protect this"),
                ("Q3", f"Q3 · {tq.QUADRANT_LABELS['Q3']}", ""),
                ("Q4", f"Q4 · {tq.QUADRANT_LABELS['Q4']}", ""),
                ("U", "Unassigned", ""),
            ]
            for key, header, marker in section_order:
                bucket = groups[key]
                if not bucket:
                    continue
                print(f"\n  {header} ({len(bucket)}){marker}")
                for t in bucket:
                    idx = proposed.index(t) + 1
                    meta = []
                    if t.get("labels"):
                        meta.append(", ".join(t["labels"]))
                    if t.get("due_string"):
                        meta.append(f"due: {t['due_string']}")
                    meta_str = f" [{' · '.join(meta)}]" if meta else ""
                    print(f"    {idx}. {t['content']}{meta_str}")
                    print(f"       id={t['id']}  src={t['source']}")

        if parked:
            print(f"\n## Parked ({len(parked)})")
            for t in parked:
                qtag = f" [{t['quadrant']}]" if t.get("quadrant") else ""
                print(f"  {t['content']}{qtag} (until {t['parked_until']})  id={t['id']}")

    elif args.tasks_cmd == "today":
        from datetime import datetime

        import focus as fc

        if args.open_hours is not None:
            open_minutes = int(args.open_hours * 60)
            source_note = f"assuming {args.open_hours}h free"
        else:
            try:
                from integrations.gcal import agenda_today
                events = agenda_today(account=args.account)
            except Exception as e:
                print(f"Couldn't read the calendar ({e}). Re-run with --open-hours to override.")
                return
            open_minutes = tq.free_minutes(events, now=datetime.now(tq.PST))
            source_note = f"{round(open_minutes / 60, 1)}h open on the {args.account} calendar"

        focus_texts = fc.texts()
        picks = tq.pick_today(tq.load_all(), open_minutes=open_minutes, focus_texts=focus_texts)

        if focus_texts:
            print("Focus: " + " · ".join(focus_texts))
        if not picks:
            print(f"Nothing fits today ({source_note}).")
            return
        print(f"Today ({source_note}):")
        total = 0
        for i, t in enumerate(picks, 1):
            dur = t.get("duration_minutes") or tq.MIN_DURATION_MINUTES
            total += dur
            print(f"  {i}. {t['content']}  [{dur}m · {tq.pick_reason(t, focus_texts)}]  id={t['id']}")
        print(f"  — {total}m planned of {open_minutes}m open")

    elif args.tasks_cmd == "schedule":
        from datetime import datetime, time as _time

        import focus as fc
        from integrations.gcal import agenda_today

        try:
            events = agenda_today(account=args.account)
        except Exception as e:
            print(f"Couldn't read the calendar ({e}). Nothing scheduled.")
            return

        now = datetime.now(tq.PST)
        if args.at:
            try:
                hh, mm = (int(x) for x in args.at.split(":"))
                now = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            except (ValueError, TypeError):
                print(f"Invalid --at time: {args.at!r} (use HH:MM)")
                return

        slots = tq.free_slots(events, now=now)
        if not slots:
            print("No free working time left today. Nothing scheduled.")
            return

        # Explicit ids win; otherwise schedule what the daily pick chose.
        if args.ids:
            chosen = []
            for raw in args.ids:
                t = tq.get_by_index(int(raw)) if raw.isdigit() else tq.get(raw)
                if not t:
                    print(f"Task not found: {raw}")
                    return
                chosen.append(t)
        else:
            open_minutes = tq.free_minutes(events, now=now)
            chosen = tq.pick_today(tq.load_all(), open_minutes=open_minutes, focus_texts=fc.texts())
            if not chosen:
                print("Nothing to schedule — the daily pick is empty.")
                return

        placed = tq.assign_slots(chosen, slots)
        unplaced = [t for t in chosen if not any(p[0]["id"] == t["id"] for p in placed)]

        if not placed:
            print("Nothing fits in the remaining free time.")
            return

        for t, start, end in placed:
            print(f"  {start:%-I:%M%p}–{end:%-I:%M%p}  {t['content']}")
        if unplaced:
            print(f"  ({len(unplaced)} didn't fit: " + "; ".join(t["content"][:40] for t in unplaced) + ")")

        if args.dry_run:
            print("Dry run — no events created.")
            return

        from integrations.gcal import create_event

        created = 0
        for t, start, end in placed:
            try:
                ev = create_event(
                    account=args.account,
                    summary=t["content"][:200],
                    description=f"Cortex task {t['id']}",
                    start_iso=start.isoformat(),
                    end_iso=end.isoformat(),
                )
            except Exception as e:
                print(f"Failed to create event for {t['id']}: {e}")
                continue
            if not ev:
                print(f"Calendar rejected the event for {t['id']} — not marked as scheduled.")
                continue
            tq.mark_scheduled(t["id"], event_id=str(ev.get("id", "")), start_iso=start.isoformat())
            created += 1

        tq.render_queue()
        print(f"Blocked {created} of {len(placed)} on the {args.account} calendar.")

    elif args.tasks_cmd == "stats":
        stats = tq.weekly_stats(days=args.days)
        print(f"Eisenhower — past {stats['window_days']} days")
        print(f"  {stats['summary']}")
        print(f"  Firefighting ratio (Q1+Q3 / total approved): {stats['firefighting_ratio']}")
        if stats.get("by_project"):
            by_proj = ", ".join(f"{k} {v}" for k, v in sorted(stats["by_project"].items()))
            print(f"  By project: {by_proj}")
        if args.verbose:
            print(f"  Approved  by quadrant: {stats['approved']}")
            print(f"  Parked    by quadrant: {stats['parked']}")
            print(f"  Declined  by quadrant: {stats['declined']}")

    elif args.tasks_cmd == "propose":
        try:
            t = tq.propose(
                content=args.content,
                labels=args.labels.split(",") if args.labels else None,
                due_string=args.due,
                priority=args.priority,
                quadrant=args.quadrant,
                duration_minutes=args.duration,
                source=args.source or "cli",
                project=getattr(args, "project", None),
                task_type=getattr(args, "task_type", None),
                who=getattr(args, "who", None),
                due_date=getattr(args, "due_date", None),
            )
        except ValueError as e:
            print(f"Invalid input: {e}")
            return
        if t is None:
            print("Skipped (duplicate of a previously declined task).")
        else:
            qtag = f" [{t['quadrant']}]" if t.get("quadrant") else ""
            print(f"Proposed: {t['id']}{qtag} — {t['content']}")
        tq.render_queue()

    elif args.tasks_cmd in ("approve", "park", "decline", "complete", "touch"):
        # Accept either full id (t_xxxxxxxx) or 1-based index
        target_id = args.id
        if target_id.isdigit():
            t = tq.get_by_index(int(target_id))
            if not t:
                print(f"No proposed task at index {target_id}")
                return
            target_id = t["id"]

        t = tq.get(target_id)
        if not t:
            print(f"Task not found: {target_id}")
            return

        if args.tasks_cmd == "approve":
            # If user passed --quadrant, override; otherwise use existing quadrant on task
            effective_quadrant = args.quadrant or t.get("quadrant")

            # Duration: CLI flag overrides task's stored duration; fall back to minimum if neither.
            try:
                override_dur = tq.parse_duration(args.duration) if args.duration else None
            except ValueError as e:
                print(f"Invalid duration: {e}")
                return
            effective_duration = override_dur or t.get("duration_minutes") or tq.MIN_DURATION_MINUTES

            try:
                tq.approve(
                    target_id,
                    quadrant=args.quadrant,
                    duration_minutes=override_dur,
                )
            except ValueError as e:
                print(f"Invalid input: {e}")
                return
            qtag = f" [{effective_quadrant}]" if effective_quadrant else ""
            dtag = f" {effective_duration}m" if effective_duration else ""
            print(f"Approved{qtag}{dtag}: {t['content']}")

        elif args.tasks_cmd == "park":
            tq.park(target_id, until=args.until)
            print(f"Parked: {t['content']} (until resolved date)")

        elif args.tasks_cmd == "decline":
            tq.decline(target_id)
            print(f"Declined: {t['content']}")

        elif args.tasks_cmd == "complete":
            tq.complete(target_id)
            print(f"Marked complete: {t['content']}")

        elif args.tasks_cmd == "touch":
            tq.touch(target_id)
            print(f"Touched (staleness reset): {t['content']}")

        tq.render_queue()
        tq.render_archive()

    else:
        print(f"Unknown tasks command: {args.tasks_cmd}")


def cmd_content(args):
    """Substack content pipeline (ideas + notes)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import content

    if args.content_cmd == "add":
        path = content.propose_idea(
            args.title, source="cli", material=args.material, links=args.link or []
        )
        print(f"Created {path}" if path else "Deduped — idea (or published post) with that slug exists")
    elif args.content_cmd == "list":
        ideas = content.list_ideas(status=args.status)
        if not ideas:
            print("No ideas" + (f" with status '{args.status}'" if args.status else ""))
        for i, idea in enumerate(ideas, 1):
            status = idea.get("status", "?")
            slug = idea.get("slug", "")
            title = idea.get("title", "")
            source = idea.get("source", "?")
            print(f"{i}. [{status:9}] {slug} — {title} (src: {source})")
    elif args.content_cmd == "status":
        idea = content.set_status(args.slug, args.status)
        print(f"{idea['slug']} → {idea['status']}")
    elif args.content_cmd == "published":
        dest = content.archive_published(args.slug, args.url)
        print(f"Archived to {dest}")
    elif args.content_cmd == "note":
        path = content.add_note(args.text)
        print(f"Queued {path.name}")
    elif args.content_cmd == "notes":
        notes = content.list_notes(status="proposed")
        if not notes:
            print("No proposed notes")
        for n in notes:
            print(f"{n['name']}: {n['body'].strip()[:100]}")
    elif args.content_cmd == "note-posted":
        content.mark_note_posted(args.name)
        print(f"{args.name} → posted")
    else:
        print(f"Unknown content command: {args.content_cmd}")


def cmd_youtube(args):
    """YouTube transcript ingestion into vault/kb/<domain>/sources/raw/."""
    from integrations.youtube import YouTubeError, ingest, probe, transcript_text

    try:
        if args.youtube_cmd == "add":
            result = ingest(
                args.url,
                domain=args.kb,
                slug=args.slug,
                languages=[args.lang] if args.lang else None,
                overwrite=args.overwrite,
            )
            if args.format == "json":
                _output(result, args.format)
                return
            if result["existed"]:
                print(f"Already ingested — {result['path']} (use --overwrite to refetch)")
                return
            print(f"{result['title']} — {result['author']}")
            print(f"  {result['words']} words -> {result['path']}")
            print(f"  Next: write the analysis note at "
                  f"vault/kb/{args.kb}/sources/{result['slug']}.md, then add it to "
                  f"vault/kb/{args.kb}/sources/INDEX.md")
        elif args.youtube_cmd == "transcript":
            print(transcript_text(args.url, languages=[args.lang] if args.lang else None,
                                  timestamps=not args.no_timestamps))
        elif args.youtube_cmd == "probe":
            _output(probe(args.url), args.format)
        else:
            print(f"Unknown youtube command: {args.youtube_cmd}")
    except YouTubeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_focus(args):
    """Weekly focus CLI."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import focus as fc

    if args.focus_cmd == "set":
        items = []
        for raw in args.items:
            # 'work:Ship phase 2' tags the item to a project; a bare string doesn't.
            slug, sep, text = raw.partition(":")
            if sep and slug.strip().lower() in _PROJECT_SLUGS():
                items.append({"text": text.strip(), "project": slug.strip().lower()})
            else:
                items.append({"text": raw.strip(), "project": None})
        try:
            data = fc.set_focus(items)
        except ValueError as e:
            print(f"Invalid focus: {e}")
            return
        print(f"Focus set for week of {data['week_of']}:")
        for i, item in enumerate(data["items"], 1):
            tag = f" [{item['project']}]" if item.get("project") else ""
            print(f"  {i}. {item['text']}{tag}")
        _rerender_queue()

    elif args.focus_cmd == "show":
        data = fc.current()
        if not data:
            stale = fc.load()
            if stale:
                print(f"No focus for this week. Last set for week of {stale['week_of']}.")
            else:
                print("No focus set.")
            return
        print(f"Week of {data['week_of']}:")
        for i, item in enumerate(data["items"], 1):
            tag = f" [{item['project']}]" if item.get("project") else ""
            print(f"  {i}. {item['text']}{tag}")

    elif args.focus_cmd == "clear":
        fc.clear()
        print("Focus cleared.")
        _rerender_queue()

    else:
        print(f"Unknown focus command: {args.focus_cmd}")


def _PROJECT_SLUGS() -> tuple:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tasks as tq

    return tq.KNOWN_PROJECTS


def _rerender_queue() -> None:
    """Keep queue.md's focus block in step with the stored focus."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tasks as tq

    tq.render_queue()


def cmd_opportunities(args):
    """Opportunity tracker CLI."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import opportunities as ops

    def _split_csv(value):
        if not value:
            return None
        return [s.strip() for s in value.split(",") if s.strip()]

    def _print_one_line(o):
        meta = []
        if o.stage:
            meta.append(o.stage)
        if o.relationship:
            meta.append(o.relationship)
        if o.value_band:
            meta.append(o.value_band)
        meta_str = " · ".join(meta)
        next_bits = []
        if o.next_step:
            next_bits.append(o.next_step)
        if o.next_step_due:
            next_bits.append(f"by {o.next_step_due}")
        next_str = " — ".join(next_bits) if next_bits else "—"
        print(f"  {o.slug}  [{o.status} · {meta_str}]  last: {o.last_touched}")
        print(f"    next: {next_str}")

    if args.opps_cmd == "list":
        if args.stale:
            items = ops.list_stale(threshold_days=args.threshold)
            label = f"Stale ({args.threshold}d threshold)"
        elif args.due:
            items = ops.list_due(window_days=args.window)
            label = f"Due (within {args.window}d)"
        elif args.status:
            items = [o for o in ops.load_all() if o.status == args.status]
            label = f"Status: {args.status}"
        elif args.all:
            items = ops.load_all()
            label = "All"
        else:
            items = ops.list_open()
            label = "Open + Active"
        if not items:
            print(f"{label}: (none)")
            return
        print(f"{label} ({len(items)}):")
        for o in items:
            _print_one_line(o)

    elif args.opps_cmd == "show":
        o = ops.load(args.slug)
        if not o:
            print(f"Not found: {args.slug}")
            return
        path = ops._path(args.slug)
        print(path.read_text())

    elif args.opps_cmd == "add":
        try:
            o = ops.add(
                args.name,
                source=args.source,
                status=args.status,
                stage=args.stage,
                relationship=args.relationship,
                value_band=args.value_band,
                shape=_split_csv(args.shape),
                next_step=args.next_step,
                next_step_due=args.next_step_due,
                linked_meetings=args.linked_meeting,
                linked_intros=args.linked_intro,
                linked_drafts=args.linked_draft,
                tags=_split_csv(args.tags),
                initial_log=args.log,
                initial_context=args.context,
            )
        except ValueError as e:
            print(f"Invalid input: {e}")
            return
        print(f"Added: {o.slug} — {o.name}")

    elif args.opps_cmd == "touch":
        try:
            o = ops.touch(
                args.slug,
                args.log,
                advance_stage=args.advance_stage,
                advance_status=args.advance_status,
            )
        except ValueError as e:
            print(f"Invalid input: {e}")
            return
        if not o:
            print(f"Not found: {args.slug}")
            return
        print(f"Touched: {o.slug} (last_touched={o.last_touched}, stage={o.stage}, status={o.status})")

    elif args.opps_cmd == "update":
        try:
            o = ops.update(
                args.slug,
                name=args.name,
                status=args.status,
                stage=args.stage,
                relationship=args.relationship,
                value_band=args.value_band,
                shape=_split_csv(args.shape),
                next_step=args.next_step,
                next_step_due=args.next_step_due,
                tags=_split_csv(args.tags),
                bump_last_touched=args.bump,
            )
        except ValueError as e:
            print(f"Invalid input: {e}")
            return
        if not o:
            print(f"Not found: {args.slug}")
            return
        print(f"Updated: {o.slug}")

    elif args.opps_cmd == "close":
        try:
            o = ops.close(args.slug, args.status, note=args.note)
        except ValueError as e:
            print(f"Invalid input: {e}")
            return
        if not o:
            print(f"Not found: {args.slug}")
            return
        print(f"Closed: {o.slug} -> {o.status}")

    else:
        print(f"Unknown opportunities command: {args.opps_cmd}")


def _output(data, fmt: str):
    if fmt == "json":
        print(json.dumps(data, indent=2, default=str))
    else:
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    for k, v in item.items():
                        print(f"  {k}: {v}")
                    print()
                else:
                    print(item)
        elif isinstance(data, dict):
            for k, v in data.items():
                print(f"  {k}: {v}")
        elif data is None:
            print("No result")
        else:
            print(data)


def main():
    from integrations.gmail import ACCOUNT_USER_IDS as _GMAIL_USER_IDS
    from integrations.gcal import CALENDAR_ACCOUNTS as _GCAL_USER_IDS
    GMAIL_ACCOUNTS = list(_GMAIL_USER_IDS.keys())
    GCAL_ACCOUNTS = list(_GCAL_USER_IDS)

    parser = argparse.ArgumentParser(description="Second Brain integration CLI")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format (default: json)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- Gmail ---
    gmail_parser = subparsers.add_parser("gmail", help="Gmail operations")
    gmail_sub = gmail_parser.add_subparsers(dest="gmail_cmd", required=True)

    p = gmail_sub.add_parser("triage", help="List recent emails")
    p.add_argument("--account", default="personal", choices=GMAIL_ACCOUNTS)
    p.add_argument("--max", type=int, default=20)
    p.add_argument("--query", default=None)

    p = gmail_sub.add_parser("read", help="Read a specific email")
    p.add_argument("--id", required=True)
    p.add_argument("--account", default="personal", choices=GMAIL_ACCOUNTS)

    p = gmail_sub.add_parser("draft", help="Create a draft email")
    p.add_argument("--to", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--account", default="personal", choices=GMAIL_ACCOUNTS)
    p.add_argument("--html", action="store_true")

    p = gmail_sub.add_parser("labels", help="List Gmail labels")
    p.add_argument("--account", default="personal", choices=GMAIL_ACCOUNTS)

    p = gmail_sub.add_parser("triage-all", help="Triage all accounts")
    p.add_argument("--max", type=int, default=20)

    # inbox-* subcommands depend on the inbox classifier module — a layer-10+
    # lane absent from a shareable/extracted copy of Cortex.
    if lane_available("inbox"):
        gmail_sub.add_parser("inbox-setup", help="Create Gmail labels, initialize state files")

        gmail_sub.add_parser("inbox-status", help="Show inbox mode, pending reviews, graduation progress")

        gmail_sub.add_parser("inbox-review", help="Print pending review items")

        p = gmail_sub.add_parser("inbox-history", help="Show recent actions from audit log")
        p.add_argument("--hours", type=int, default=24)

        gmail_sub.add_parser("inbox-rules", help="Print current rules summary")

    # --- GCal ---
    gcal_parser = subparsers.add_parser("gcal", help="Google Calendar operations")
    gcal_sub = gcal_parser.add_subparsers(dest="gcal_cmd", required=True)

    p = gcal_sub.add_parser("today", help="Today's events")
    p.add_argument("--account", default="personal", choices=GCAL_ACCOUNTS)

    p = gcal_sub.add_parser("upcoming", help="Upcoming events")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--account", default="personal", choices=GCAL_ACCOUNTS)

    p = gcal_sub.add_parser("week", help="This week's events")
    p.add_argument("--account", default="personal", choices=GCAL_ACCOUNTS)

    p = gcal_sub.add_parser("calendars", help="List calendars")
    p.add_argument("--account", default="personal", choices=GCAL_ACCOUNTS)

    # --- Telegram ---
    tg_parser = subparsers.add_parser("telegram", help="Telegram operations")
    tg_sub = tg_parser.add_subparsers(dest="telegram_cmd", required=True)

    p = tg_sub.add_parser("send", help="Send a message")
    p.add_argument("--text", required=True)
    p.add_argument("--chat-id", default=None)

    # --- Slack ---
    slack_parser = subparsers.add_parser("slack", help="Slack operations")
    slack_sub = slack_parser.add_subparsers(dest="slack_cmd", required=True)

    p = slack_sub.add_parser("send", help="Send a message")
    p.add_argument("--text", required=True)
    p.add_argument("--channel", default=None)

    # --- Smartlead ---
    if lane_available("integrations.smartlead"):
        sl_parser = subparsers.add_parser("smartlead", help="Smartlead cold email operations")
        sl_sub = sl_parser.add_subparsers(dest="sl_cmd", required=True)

        p = sl_sub.add_parser("campaigns", help="List all campaigns")
        p.add_argument("--status", default=None, help="Filter by status (e.g. ACTIVE, PAUSED)")

        p = sl_sub.add_parser("campaign", help="Get a single campaign")
        p.add_argument("--id", type=int, required=True, help="Campaign ID")

        p = sl_sub.add_parser("stats", help="Get campaign analytics")
        p.add_argument("--id", type=int, required=True, help="Campaign ID")

        p = sl_sub.add_parser("leads", help="List leads for a campaign")
        p.add_argument("--id", type=int, required=True, help="Campaign ID")
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--limit", type=int, default=100)

        p = sl_sub.add_parser("add-leads", help="Add leads to a campaign (JSON array)")
        p.add_argument("--id", type=int, required=True, help="Campaign ID")
        p.add_argument("--leads-json", required=True, help='JSON array of lead objects, e.g. \'[{"email":"someone@example.com"}]\'')

        p = sl_sub.add_parser("pause", help="Pause a campaign")
        p.add_argument("--id", type=int, required=True, help="Campaign ID")

        p = sl_sub.add_parser("resume", help="Resume (start) a campaign")
        p.add_argument("--id", type=int, required=True, help="Campaign ID")

        p = sl_sub.add_parser("lead", help="Look up a lead by email")
        p.add_argument("--email", required=True, help="Lead email address")

        sl_sub.add_parser("email-accounts", help="List email sending accounts")

    # --- Body (weekly report) ---
    if lane_available("weekly_body_report"):
        body_parser = subparsers.add_parser("body", help="Weekly body report (cross-signal health synthesis)")
        body_sub = body_parser.add_subparsers(dest="body_cmd", required=True)

        p = body_sub.add_parser("weekly", help="Generate the weekly body report (writes to vault/weekly/, sends Telegram)")
        p.add_argument("--end", help="End date YYYY-MM-DD (default: yesterday)")
        p.add_argument("--no-notify", action="store_true", help="Skip Telegram notification")

        p = body_sub.add_parser("weekly-dossier", help="Print the dossier only (deterministic, no LLM call) — fast smoke test")
        p.add_argument("--end", help="End date YYYY-MM-DD (default: yesterday)")

    # --- Garmin ---
    if lane_available("integrations.garmin"):
        garmin_parser = subparsers.add_parser("garmin", help="Garmin Connect health data")
        garmin_sub = garmin_parser.add_subparsers(dest="garmin_cmd", required=True)

        garmin_sub.add_parser("today", help="Composite snapshot: steps + sleep + HRV + body battery + stress")

        p = garmin_sub.add_parser("steps", help="Daily step count")
        p.add_argument("--day", default=None, help="today | yesterday | YYYY-MM-DD (default: today)")

        p = garmin_sub.add_parser("sleep", help="Last night's sleep summary")
        p.add_argument("--day", default=None, help="today | yesterday | YYYY-MM-DD (default: today)")

        p = garmin_sub.add_parser("hrv", help="Overnight HRV reading")
        p.add_argument("--day", default=None, help="today | yesterday | YYYY-MM-DD (default: today)")

        p = garmin_sub.add_parser("battery", help="Body Battery for a day")
        p.add_argument("--day", default=None, help="today | yesterday | YYYY-MM-DD (default: today)")

        p = garmin_sub.add_parser("stress", help="Daily stress summary")
        p.add_argument("--day", default=None, help="today | yesterday | YYYY-MM-DD (default: today)")

        p = garmin_sub.add_parser("activities", help="Recent workouts/activities")
        p.add_argument("--limit", type=int, default=10)

    # --- Chat ---
    chat_parser = subparsers.add_parser("chat", help="Chat interface")
    chat_sub = chat_parser.add_subparsers(dest="chat_cmd", required=True)
    chat_sub.add_parser("start", help="Start Slack chat server")

    # --- Health ---
    health_parser = subparsers.add_parser("health", help="System health checks")
    health_parser.add_argument("--full", action="store_true", help="Include tier 2 checks (SSH to VPS)")

    # --- Status ---
    status_parser = subparsers.add_parser("status", help="Check integration availability")
    status_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the Composio connection lookup (config-only check, no network)",
    )

    # --- Tasks ---
    if lane_available("tasks"):
        tasks_parser = subparsers.add_parser("tasks", help="Task queue operations")
        tasks_sub = tasks_parser.add_subparsers(dest="tasks_cmd", required=True)

        tasks_sub.add_parser("list", help="List proposed + parked tasks (grouped by Eisenhower quadrant)")

        p = tasks_sub.add_parser("stats", help="Show past-N-day Eisenhower breakdown")
        p.add_argument("--days", type=int, default=7, help="Lookback window in days (default 7)")
        p.add_argument("-v", "--verbose", action="store_true", help="Include per-quadrant breakdown")

        p = tasks_sub.add_parser("propose", help="Manually propose a task")
        p.add_argument("--content", required=True, help="Task content/title")
        p.add_argument("--labels", help="Comma-separated free-form labels")
        p.add_argument("--due", help="Natural-language due (e.g. 'tomorrow')")
        p.add_argument("--priority", type=int, choices=[1, 2, 3, 4], help="Legacy numeric priority (usually skip; use --quadrant)")
        p.add_argument("--quadrant", choices=["Q1", "Q2", "Q3", "Q4", "q1", "q2", "q3", "q4", "1", "2", "3", "4"], help="Eisenhower quadrant")
        p.add_argument("--duration", help="Estimated duration: '30m', '1h', '2h30m', or bare minutes. Clamped to 15m minimum.")
        p.add_argument("--source", default="cli", help="Source tag for dedup")
        p.add_argument("--project", help=f"Project slug: {_projects_roster_text()}. Inferred from --source if omitted.")
        p.add_argument("--type", dest="task_type", choices=["action", "idea", "waiting", "delegated"], help="Task type (default: action)")
        p.add_argument("--who", help="Person a waiting/delegated task is blocked on")
        p.add_argument("--due-date", dest="due_date", help="Explicit due date YYYY-MM-DD (overrides --due parsing)")

        p = tasks_sub.add_parser("today", help="What to do today — deadline first, then weekly focus, then quadrant")
        p.add_argument("--open-hours", type=float, help="Assume this many free hours instead of reading the calendar")
        p.add_argument("--account", default="personal", help="Calendar account to read (default: personal)")

        p = tasks_sub.add_parser("schedule", help="Block calendar time for today's picks (or specific tasks)")
        p.add_argument("ids", nargs="*", help="Task ids or indices. Omit to schedule today's suggested picks.")
        p.add_argument("--at", help="Force the first block to start at HH:MM (24h)")
        p.add_argument("--account", default="personal", help="Calendar account to write to (default: personal)")
        p.add_argument("--dry-run", action="store_true", help="Show the proposed blocks without creating events")

        p = tasks_sub.add_parser("touch", help="Mark a waiting/delegated task as chased — resets its staleness clock")
        p.add_argument("id", help="Task id (t_xxxxxxxx) or 1-based index from list")

        p = tasks_sub.add_parser("approve", help="Accept a task into the active list")
        p.add_argument("id", help="Task id (t_xxxxxxxx) or 1-based index from list")
        p.add_argument("--quadrant", choices=["Q1", "Q2", "Q3", "Q4", "q1", "q2", "q3", "q4", "1", "2", "3", "4"], help="Override/set Eisenhower quadrant at approval")
        p.add_argument("--duration", help="Override/set estimated duration at approval: '30m', '1h', '2h30m', or bare minutes. Clamped to 15m minimum.")

        p = tasks_sub.add_parser("park", help="Park a task until a later date")
        p.add_argument("id", help="Task id or 1-based index")
        p.add_argument("--until", required=True, help="YYYY-MM-DD or Nd (e.g. '3d')")

        p = tasks_sub.add_parser("decline", help="Permanently decline a task (won't re-propose)")
        p.add_argument("id", help="Task id or 1-based index")

        p = tasks_sub.add_parser("complete", help="Mark a task as already done outside Cortex (won't re-propose)")
        p.add_argument("id", help="Task id or 1-based index")

    # --- Weekly focus ---
    if lane_available("focus"):
        focus_parser = subparsers.add_parser("focus", help="Weekly focus — the 1-3 things this week is for")
        focus_sub = focus_parser.add_subparsers(dest="focus_cmd", required=True)

        p = focus_sub.add_parser("set", help="Set this week's focus (replaces any existing)")
        p.add_argument("items", nargs="+", help="1-3 focus items. Prefix with 'slug:' to tag a project, e.g. 'work:Ship phase 2'")

        focus_sub.add_parser("show", help="Show this week's focus")
        focus_sub.add_parser("clear", help="Clear the stored focus")

    # --- Opportunities ---
    if lane_available("opportunities"):
        opps_parser = subparsers.add_parser("opportunities", aliases=["opps"], help="Opportunity tracker (vault/opportunities/)")
        opps_sub = opps_parser.add_subparsers(dest="opps_cmd", required=True)

        p = opps_sub.add_parser("list", help="List opportunities (default: open + active)")
        p.add_argument("--status", help="Filter by status (open/active/won/lost/dormant)")
        p.add_argument("--stale", action="store_true", help="Only show stale (open, no touch in N days)")
        p.add_argument("--due", action="store_true", help="Only show with next_step_due in window")
        p.add_argument("--threshold", type=int, default=21, help="Stale threshold in days (default 21)")
        p.add_argument("--window", type=int, default=2, help="Due window in days (default 2)")
        p.add_argument("--all", action="store_true", help="Include closed (won/lost/dormant)")

        p = opps_sub.add_parser("show", help="Print one opportunity (frontmatter + body)")
        p.add_argument("slug", help="Opportunity slug (e.g. matt-baba)")

        p = opps_sub.add_parser("add", help="Create a new opportunity")
        p.add_argument("--name", required=True, help="Display name (also seeds slug)")
        p.add_argument("--source", default="cli", help="How it surfaced (e.g. network-intro, in-person)")
        p.add_argument("--status", choices=list(("open", "active", "won", "lost", "dormant")), default="open")
        p.add_argument("--stage", choices=list(("spark", "qualifying", "shaped", "proposed", "committed")), default="spark")
        p.add_argument("--relationship", choices=["cold", "warm", "hot"], default="warm")
        p.add_argument("--value-band", choices=["nano", "small", "medium", "large", "strategic"], help="Qualitative value band")
        p.add_argument("--shape", help="Comma-separated deal shapes (e.g. 'collab,white-label,referrer')")
        p.add_argument("--next-step", help="Next move (prefix with 'When ' to mark milestone-gated)")
        p.add_argument("--next-step-due", help="YYYY-MM-DD due date (optional)")
        p.add_argument("--linked-meeting", action="append", default=[], help="Meeting filename (repeatable)")
        p.add_argument("--linked-intro", action="append", default=[], help="Intro filename (repeatable)")
        p.add_argument("--linked-draft", action="append", default=[], help="Draft filename (repeatable)")
        p.add_argument("--tags", help="Comma-separated tags")
        p.add_argument("--log", help="Initial log entry (today's date prepended)")
        p.add_argument("--context", help="Initial Context section body")

        p = opps_sub.add_parser("touch", help="Append a log entry + bump last_touched")
        p.add_argument("slug")
        p.add_argument("--log", required=True, help="Log entry text")
        p.add_argument("--advance-stage", choices=list(("spark", "qualifying", "shaped", "proposed", "committed")), help="Advance stage at the same time")
        p.add_argument("--advance-status", choices=list(("open", "active", "won", "lost", "dormant")), help="Advance status at the same time")

        p = opps_sub.add_parser("update", help="Patch frontmatter fields without logging")
        p.add_argument("slug")
        p.add_argument("--name")
        p.add_argument("--status", choices=list(("open", "active", "won", "lost", "dormant")))
        p.add_argument("--stage", choices=list(("spark", "qualifying", "shaped", "proposed", "committed")))
        p.add_argument("--relationship", choices=["cold", "warm", "hot"])
        p.add_argument("--value-band", choices=["nano", "small", "medium", "large", "strategic"])
        p.add_argument("--shape", help="Comma-separated (replaces existing)")
        p.add_argument("--next-step")
        p.add_argument("--next-step-due", help="YYYY-MM-DD or 'null' to clear")
        p.add_argument("--tags", help="Comma-separated (replaces existing)")
        p.add_argument("--bump", action="store_true", help="Also bump last_touched to today")

        p = opps_sub.add_parser("close", help="Close as won / lost / dormant")
        p.add_argument("slug")
        p.add_argument("--status", required=True, choices=["won", "lost", "dormant"])
        p.add_argument("--note", help="Optional closure note")

    # --- Content pipeline (Substack proof engine) ---
    if lane_available("content"):
        content_parser = subparsers.add_parser("content", help="Substack content pipeline")
        content_sub = content_parser.add_subparsers(dest="content_cmd", required=True)

        p = content_sub.add_parser("add", help="Propose a content idea")
        p.add_argument("--title", required=True)
        p.add_argument("--material", default="", help="Raw material / notes for the post")
        p.add_argument("--link", action="append", help="Vault artifact path (repeatable)")

        p = content_sub.add_parser("list", help="List content ideas")
        p.add_argument("--status", choices=sorted(
            {"proposed", "briefed", "drafting", "review", "published", "declined"}
        ))

        p = content_sub.add_parser("status", help="Set idea status")
        p.add_argument("slug")
        p.add_argument("status")

        p = content_sub.add_parser("published", help="Archive an idea as published")
        p.add_argument("slug")
        p.add_argument("url")

        p = content_sub.add_parser("note", help="Queue a short Note")
        p.add_argument("text")

        content_sub.add_parser("notes", help="List proposed Notes")

        p = content_sub.add_parser("note-posted", help="Mark a Note as posted")
        p.add_argument("name")

    # --- YouTube -> KB ingestion ---
    if lane_available("integrations.youtube"):
        from integrations.youtube import VAULT_KB as _VAULT_KB
        KB_DOMAINS = sorted(d.name for d in _VAULT_KB.iterdir() if d.is_dir()) or ["gtm"]
        yt_parser = subparsers.add_parser("youtube", help="Ingest YouTube transcripts into vault/kb/")
        yt_sub = yt_parser.add_subparsers(dest="youtube_cmd", required=True)

        p = yt_sub.add_parser("add", help="Fetch a video transcript into a KB's sources/raw/")
        p.add_argument("url", help="YouTube URL or 11-char video ID")
        p.add_argument("--kb", default="gtm", choices=KB_DOMAINS, help="Target knowledge base (default: gtm)")
        p.add_argument("--slug", help="Override the auto-generated slug")
        p.add_argument("--lang", help="Transcript language code (default: en)")
        p.add_argument("--overwrite", action="store_true", help="Refetch even if the raw file exists")

        p = yt_sub.add_parser("transcript", help="Print a transcript to stdout (no vault write)")
        p.add_argument("url")
        p.add_argument("--lang")
        p.add_argument("--no-timestamps", action="store_true")

        p = yt_sub.add_parser("probe", help="Metadata + transcript availability, no write")
        p.add_argument("url")

    # --- Dashboard ---
    if lane_available("dashboard_snapshot"):
        dash_parser = subparsers.add_parser("dashboard", help="Obsidian command center snapshot")
        dash_sub = dash_parser.add_subparsers(dest="dashboard_cmd", required=True)
        dash_sub.add_parser("refresh", help="Regenerate the snapshot now")
        dash_sub.add_parser("show", help="Print the current snapshot")

    # --- invest-os (analysis only — no order path anywhere) ---
    if lane_available("invest_brief"):
        inv_parser = subparsers.add_parser("invest", help="Market watch, regime read, signals")
        inv_sub = inv_parser.add_subparsers(dest="invest_cmd", required=True)
        inv_sub.add_parser("refresh", help="Fetch quotes, evaluate rules, write snapshot")
        inv_sub.add_parser("show", help="Print the current snapshot")
        inv_sub.add_parser("brief", help="Plain-language market section for the brief")
        inv_sub.add_parser("regime", help="Current macro regime read")
        p = inv_sub.add_parser("signals", help="Recent signals (insider clusters, regime changes)")
        p.add_argument("--days", type=int, default=7)
        p.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()

    # Load env before dispatching
    load_env()

    dispatch = {
        "gmail": cmd_gmail,
        "gcal": cmd_gcal,
        "telegram": cmd_telegram,
        "slack": cmd_slack,
        "smartlead": cmd_smartlead,
        "garmin": cmd_garmin,
        "body": cmd_body,
        "chat": cmd_chat,
        "health": cmd_health,
        "status": cmd_status,
        "tasks": cmd_tasks,
        "focus": cmd_focus,
        "opportunities": cmd_opportunities,
        "opps": cmd_opportunities,
        "content": cmd_content,
        "youtube": cmd_youtube,
        "dashboard": cmd_dashboard,
        "invest": cmd_invest,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
