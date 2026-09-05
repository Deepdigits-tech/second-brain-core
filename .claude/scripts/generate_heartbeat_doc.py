#!/usr/bin/env python3
"""Generate vault/HEARTBEAT.md from live code and config.

Auto-rendered — do not edit vault/HEARTBEAT.md directly, it will be overwritten.
Run manually or let the hygiene timer call this before reporting.

Usage:
    python3 .claude/scripts/generate_heartbeat_doc.py
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from shared import PST  # noqa: F401 (module-level constant, see shared.owner_timezone)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
DATA_DIR = PROJECT_ROOT / ".claude" / "data"
STATE_DIR = DATA_DIR / "state"
HEARTBEAT_PY = PROJECT_ROOT / ".claude" / "scripts" / "heartbeat.py"
FEATURE_TOGGLES_PATH = STATE_DIR / "feature-toggles.json"
OUTPUT_PATH = VAULT_DIR / "HEARTBEAT.md"

# Human-readable descriptions for each feature key
FEATURE_DESCRIPTIONS: dict[str, dict] = {
    "reminders.fire_due": {
        "label": "Due Reminders",
        "what": "Fires reminder notifications for tasks in vault/reminders.json that are past their due time.",
        "output": "macOS/Telegram notification",
    },
    "invest.alerts": {
        "label": "Market Rule Alerts",
        "what": "Refreshes the market snapshot and announces only the alert rules the owner set in vault/investments/watchlist.md that actually tripped, plus insider-cluster and regime signals detected today. Day-scoped state diffing means nothing repeats. Analysis only — never a buy/sell instruction. Mac-only (the market DB is machine-local).",
        "output": ".claude/data/state/market.json; Telegram alert only when a rule trips",
    },
    "invest.weekly": {
        "label": "Sunday Investing Review",
        "what": "Sunday-only nudge for the calm rules session: current market regime in plain words, anything still unset in vault/investments/plan.md, and a prompt to review the week and set rules. Buying happens on a different day by design.",
        "output": "Telegram nudge (Sundays, once)",
    },
    "health_checks": {
        "label": "Health Checks",
        "what": "Checks integration status (Gmail, GCal, Telegram, Slack, Garmin) and alerts on auth failures or stale tokens.",
        "output": "Logged to heartbeat-state.json; Telegram alert on failure",
    },
    "inbox.ensure_labels": {
        "label": "Gmail Label Setup",
        "what": "Creates the required Gmail labels (VIP, Triage/*, Digest, etc.) on all accounts if missing.",
        "output": "Gmail label structure",
    },
    "inbox.classify": {
        "label": "Inbox Classifier",
        "what": "Runs 5-layer email classification: VIP → Gmail categories → subject patterns → sender reputation DB → LLM. Assigns each message to a tier (VIP/Action/Digest/Noise/Reference).",
        "output": "Classification result dict passed to downstream steps",
    },
    "inbox.scan": {
        "label": "Inbox Review File",
        "what": "Writes vault/inbox-review.md with the triage queue for supervised decisions (Telegram-based) on borderline emails.",
        "output": "vault/inbox-review.md (gitignored)",
    },
    "inbox.execute_auto": {
        "label": "Auto-Label Execution",
        "what": "Applies Gmail labels to auto-classified messages (Noise → mute, Digest → label). No deletions.",
        "output": "Gmail label changes",
    },
    "inbox.process_review": {
        "label": "Supervised Queue Processor",
        "what": "Processes Telegram-reviewed triage decisions — applies approved labels to messages in the supervised queue.",
        "output": "Gmail label changes based on prior Telegram approvals",
    },
    "inbox.update_review": {
        "label": "Review File Refresh",
        "what": "Updates vault/inbox-review.md after processing to reflect current queue state.",
        "output": "vault/inbox-review.md update",
    },
    "digest.email_2x_daily": {
        "label": "Noon/Evening Telegram Digest",
        "what": "Pushes a Telegram digest at noon and 8 PM PST: action-tier emails (rep/llm-classified strangers included), urgent items flagged first, an Unsure section, and — evenings only — a receipt of the last 12h of applied actions.",
        "output": "Telegram message(s)",
    },
    "meeting_prep.handle_bookings": {
        "label": "Meeting Prep — Booking Detection",
        "what": "Detects cal.com booking confirmation emails, parses attendee info, enriches company data via LeadMagic, generates a structured prep brief.",
        "output": "vault/meetings/YYYY-MM-DD-HHMM-<name>.md",
    },
    "meeting_prep.refresh_upcoming": {
        "label": "Meeting Prep — T-2h Refresh",
        "what": "For meetings within the 2-hour window: fetches fresh Gmail context from the attendee and appends to the prep file. Sends Telegram/macOS ping.",
        "output": "Appended content to existing meeting prep file + notification",
    },
    "tasks.parse_proposals": {
        "label": "Task Proposal Parser",
        "what": "Parses TASK_PROPOSAL: lines from the heartbeat LLM output and calls tasks.propose() with dedup. Proposed tasks land in vault/tasks/queue.md for review.",
        "output": ".claude/data/tasks.json + vault/tasks/queue.md (auto-rendered, gitignored)",
    },
    "practices.stamp_cooldown": {
        "label": "Practice Cooldown Stamp",
        "what": "After a practice nudge is dispatched, stamps last_practice_offered in heartbeat-state.json to enforce the 4-hour cooldown between nudges.",
        "output": "heartbeat-state.json update",
    },
}


def load_feature_toggles() -> dict[str, bool]:
    if not FEATURE_TOGGLES_PATH.exists():
        return {}
    try:
        data = json.loads(FEATURE_TOGGLES_PATH.read_text())
        return {k: v for k, v in data.items() if isinstance(v, bool)}
    except Exception:
        return {}


def extract_heartbeat_constants() -> dict:
    """Parse key constants from heartbeat.py via regex (avoids importing the module)."""
    if not HEARTBEAT_PY.exists():
        return {}
    src = HEARTBEAT_PY.read_text()
    consts = {}

    patterns = {
        "active_hours_start": r"ACTIVE_HOURS\s*=\s*\((\d+),",
        "active_hours_end": r"ACTIVE_HOURS\s*=\s*\(\d+,\s*(\d+)\)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, src)
        if m:
            consts[key] = int(m.group(1))
    return consts


def render_doc(toggles: dict[str, bool], consts: dict) -> str:
    from shared import gmail_accounts, optional_import

    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M PST")
    active_start = consts.get("active_hours_start", 6)
    active_end = consts.get("active_hours_end", 22)

    def fmt_hour(h: int) -> str:
        suffix = "AM" if h < 12 else "PM"
        display = h if h <= 12 else h - 12
        display = 12 if display == 0 else display
        return f"{display} {suffix}"

    lines = [
        "# Heartbeat — What It Does",
        "",
        f"> Auto-generated from `heartbeat.py` and `feature-toggles.json`. Last updated: {now}",
        "> Do not edit this file directly — run `.claude/scripts/generate_heartbeat_doc.py` to refresh.",
        "",
        "## Schedule",
        "",
        f"- **Runs:** Every 30 minutes, {fmt_hour(active_start)}–{fmt_hour(active_end)} PST (outside these hours: skips silently)",
        f"- **Morning brief:** First run of each day generates a summary brief",
        f"- **Email digest:** Telegram push at noon and 8 PM PST (VPS-owned)",
        "",
        "## Notification Channels",
        "",
        "- macOS system notification (osascript)",
        "- Telegram bot message (personal chat)",
        "- Draft emails saved to `vault/drafts/active/` (never sent automatically)",
        "",
        "## Features",
        "",
        "Each feature can be toggled via `.claude/data/state/feature-toggles.json` (machine-local, gitignored).",
        "Missing key or missing file = **enabled** (safe default).",
        "",
    ]

    # Group by namespace
    namespaces: dict[str, list[str]] = {}
    all_keys = list(FEATURE_DESCRIPTIONS.keys())
    for key in all_keys:
        ns = key.split(".")[0]
        namespaces.setdefault(ns, []).append(key)

    ns_labels = {
        "reminders": "Reminders",
        "health": "Health Checks",
        "health_checks": "Health Checks",
        "inbox": "Inbox Management (5-Layer Pipeline)",
        "digest": "Daily Digest",
        "meeting_prep": "Meeting Prep Agent",
        "tasks": "Task Capture",
        "practices": "Practice Library (StateShift)",
    }

    for ns, keys in namespaces.items():
        section_label = ns_labels.get(ns, ns.title())
        lines.append(f"### {section_label}")
        lines.append("")
        for key in keys:
            desc = FEATURE_DESCRIPTIONS.get(key, {})
            enabled = toggles.get(key, True)
            status = "✓ enabled" if enabled else "✗ disabled"
            label = desc.get("label", key)
            what = desc.get("what", "—")
            output = desc.get("output", "—")
            lines.append(f"**`{key}`** — {status}")
            lines.append(f"- {what}")
            lines.append(f"- _Output:_ {output}")
            lines.append("")
        lines.append("")

    lines += [
        "## State Files",
        "",
        "| File | Purpose |",
        "|------|---------|",
        "| `.claude/data/state/heartbeat-state.json` | Previous-run snapshot for diffing |",
        "| `.claude/data/state/feature-toggles.json` | Per-feature kill switches (machine-local) |",
        "| `.claude/data/state/inbox-mode.json` | Triage enabled/disabled flag (`triage_enabled`) |",
        "| `.claude/data/tasks.json` | Task proposal queue (tracked in git — syncs Mac↔VPS) |",
        "| `.claude/data/heartbeat_errors.log` | Runtime errors (gitignored) |",
        "| `.claude/data/feature-timings.jsonl` | Per-feature timing log for performance review (gitignored) |",
        "",
        "## Integrations Used",
        "",
        f"- **Gmail** (Composio SDK) — {', '.join(gmail_accounts()) or 'configured accounts'}",
        "- **Google Calendar** (Composio SDK) — upcoming event detection for meeting prep",
        "- **Telegram** — notification delivery",
    ]
    if optional_import("integrations.garmin") is not None:
        lines.append("- **Garmin** — body signal data for practice nudges")
    lines += [
        "- **LeadMagic** — company enrichment for meeting prep",
        "",
        "## Triage Kill Switch",
        "",
        "Inbox triage can be toggled without restarting:",
        "```json",
        "// .claude/data/state/inbox-mode.json",
        '{ "triage_enabled": false }',
        "```",
        "Currently: triage is **disabled** on both Mac and VPS.",
        "",
        "## Launchd",
        "",
        "- **Mac:** `~/Library/LaunchAgents/com.secondbrain.heartbeat.plist`",
        "  - Load: `launchctl load ~/Library/LaunchAgents/com.secondbrain.heartbeat.plist`",
        "  - Unload: `launchctl unload ~/Library/LaunchAgents/com.secondbrain.heartbeat.plist`",
        "- **VPS:** `systemctl status secondbrain-heartbeat.timer`",
        "  - Restart: `systemctl restart secondbrain-heartbeat.timer`",
    ]

    return "\n".join(lines) + "\n"


def main():
    toggles = load_feature_toggles()
    consts = extract_heartbeat_constants()
    doc = render_doc(toggles, consts)
    OUTPUT_PATH.write_text(doc)
    print(f"Written: {OUTPUT_PATH}")
    print(f"Features: {len(FEATURE_DESCRIPTIONS)} documented, {sum(1 for v in toggles.values() if not v)} disabled")


if __name__ == "__main__":
    main()
