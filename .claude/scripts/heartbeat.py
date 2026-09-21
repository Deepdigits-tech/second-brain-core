#!/usr/bin/env python3
"""Heartbeat — proactive monitoring agent for Cortex OS.

Runs every 30 minutes (6 AM – 10 PM PST) via launchd.
Gathers Gmail, GCal data → diffs against previous state →
invokes Agent SDK to generate notifications and email drafts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shared import owner_name, gmail_accounts, business_account, lane_available, projects as _cortex_projects, account_projects as _cortex_account_projects, PST, calendar_accounts
from guardrail import run_guardrail_check
from notify import notify, delivered as _delivered

# ── Constants ──────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
DAILY_DIR = VAULT_DIR / "daily"
DATA_DIR = PROJECT_ROOT / ".claude" / "data"
STATE_DIR = DATA_DIR / "state"
STATE_PATH = STATE_DIR / "heartbeat-state.json"
# Separate from heartbeat-state.json, which is rewritten wholesale mid-run and
# would drop a key stamped at the top of the tick.
THROTTLE_STATE_PATH = STATE_DIR / "heartbeat-throttle.json"
# Per-check alert history, so one flapping check can't re-send the others.
HEALTH_ALERT_STATE_PATH = STATE_DIR / "health-alert-state.json"
ACTIVE_HOURS = (6, 22)  # 6 AM – 10 PM PST — outside this the heartbeat does nothing

# The owner's working day (7 AM – 9 PM, every day). Inside it the heartbeat keeps
# its 30-minute cadence; outside it drops to hourly. The launchd job still
# fires every 30 min — the tick is skipped in code, so the schedule lives in
# one place instead of being split across two plists.
WORK_HOURS = (7, 21)
OFF_HOURS_GAP_MINUTES = 55  # under 60 so clock drift can't skip an hour entirely
# Which machine sends briefs and digests. Mac and VPS both tick every 30 min,
# and the cross-machine guard they used to rely on — claim_brief() writing
# .claude/data/brief-coordination.json — cannot work: that file is in
# .gitignore ("never sync"), so each machine only ever sees its own claims.
# On 2026-08-19 that shipped the owner two morning briefs (VPS 06:01, Mac 08:09)
# and two noon digests (VPS 12:01, Mac 12:26). Ownership by machine has no
# race to lose: the VPS is always on, so it owns the outbound lanes. Same
# split already used for capture.email_poll (VPS) and invest.alerts (Mac).
BRIEF_OWNER_IS_VPS = sys.platform != "darwin"

DEBUG_LOG = "/tmp/cortex_hook_debug.log"
ERROR_LOG_PATH = DATA_DIR / "heartbeat_errors.log"

INBOX_REVIEW_PATH = VAULT_DIR / "inbox-review.md"
INSIGHTS_DIR = VAULT_DIR / "insights" / "newsletters"

# ── Logging ────────────────────────────────────────────────────────────


def debug(msg: str):
    with open(DEBUG_LOG, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] heartbeat: {msg}\n")


def log_error(msg: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ERROR_LOG_PATH, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


# ── Data Gathering (pure Python, no LLM) ──────────────────────────────


def gather_data() -> dict:
    """Gather all monitoring data from integrations and vault files."""
    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
    from integrations.registry import load_env, check_all
    from integrations.gmail import triage_all_accounts
    from integrations.gcal import agenda_today_all_accounts

    load_env()
    status = check_all()
    data: dict = {
        "gmail": {}, "gcal": [], "daily_log": "", "heartbeat_config": "", "config_warnings": [],
        # Per-source, per-account fetch outcome (2026-09-20) — "an empty
        # inbox" and "Gmail didn't answer" used to look identical downstream
        # (both `[]`), so an outage could overwrite the last-good snapshot
        # with nothing and the brief would say "nothing needs a reply".
        # Populated only for accounts actually attempted; a lane that's off
        # entirely (no config_warnings account) is not a "failure", it's
        # unconfigured.
        "fetch_status": {"gmail": {}, "gcal": {}},
    }

    if not gmail_accounts():
        debug("CORTEX_GMAIL_ACCOUNTS not set — Gmail lane disabled")
        data["config_warnings"].append("CORTEX_GMAIL_ACCOUNTS not set — Gmail lane disabled")

    if status["gmail"]["available"]:
        try:
            data["gmail"] = triage_all_accounts(max_results=20, status_out=data["fetch_status"]["gmail"])
        except Exception as e:
            debug(f"gmail gather failed: {e}")
            # Unknown which account(s) got through before the exception — mark
            # every configured account failed rather than silently leaving
            # fetch_status empty (which would read as "nothing attempted").
            for account in gmail_accounts():
                data["fetch_status"]["gmail"].setdefault(account, "failed")

    data["stale_gmail"] = {}
    from integrations.gmail import find_stale_unread, ACCOUNT_USER_IDS
    notified = {}
    if STATE_PATH.exists():
        try:
            notified = json.loads(STATE_PATH.read_text()).get("notified_email_ids", {})
        except (json.JSONDecodeError, OSError):
            notified = {}
    exclude_ids = set(notified.keys())
    for account in ACCOUNT_USER_IDS:
        try:
            data["stale_gmail"][account] = find_stale_unread(
                account=account, hours=4, exclude_ids=exclude_ids, max_results=10,
            )
        except Exception as e:
            debug(f"stale_unread {account} failed: {e}")
            data["stale_gmail"][account] = []

    # Merge stale messages into data["gmail"] so classify_inbox can find them
    for account, stale_msgs in data["stale_gmail"].items():
        existing_ids = {m.get("id") for m in data["gmail"].get(account, [])}
        for sm in stale_msgs:
            if sm.get("id") not in existing_ids:
                data["gmail"].setdefault(account, []).append(sm)

    if status["gcal"]["available"]:
        try:
            data["gcal"] = agenda_today_all_accounts(status_out=data["fetch_status"]["gcal"])
        except Exception as e:
            debug(f"gcal gather failed: {e}")
            for account in calendar_accounts():
                data["fetch_status"]["gcal"].setdefault(account, "failed")

    daily_path = DAILY_DIR / f"{datetime.now(PST).strftime('%Y-%m-%d')}.md"
    if daily_path.exists():
        data["daily_log"] = daily_path.read_text()

    heartbeat_path = VAULT_DIR / "HEARTBEAT.md"
    if heartbeat_path.exists():
        data["heartbeat_config"] = heartbeat_path.read_text()

    return data


# ── State Snapshot & Diffing ───────────────────────────────────────────


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def prune_notified_ids(notified: dict, max_age_days: int = 7) -> dict:
    """Return a new dict with entries older than max_age_days removed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return {
        msg_id: ts for msg_id, ts in notified.items()
        if (dt := _parse_iso(ts)) and dt >= cutoff
    }


def update_notified_ids(state: dict, diff: dict) -> dict:
    """Record IDs surfaced to the LLM this run so they don't repeatedly trigger stale-UNREAD."""
    notified = state.get("notified_email_ids", {}) or {}
    notified = prune_notified_ids(notified)
    now_iso = datetime.now(timezone.utc).isoformat()
    for msg_id in diff.get("new_email_ids", []):
        notified[msg_id] = now_iso
    for msg_id in diff.get("stale_email_ids", []):
        notified[msg_id] = now_iso
    state["notified_email_ids"] = notified
    return state


def _event_start_str(event: dict) -> str:
    """The event's start as one string, dateTime or all-day date."""
    start = event.get("start")
    if isinstance(start, dict):
        return start.get("dateTime") or start.get("date") or ""
    return str(start or "")


def _event_end_str(event: dict) -> str:
    end = event.get("end")
    if isinstance(end, dict):
        return end.get("dateTime") or end.get("date") or ""
    return str(end or "")


def _event_fingerprint(event: dict) -> str:
    """Short hash of the fields that mean "this meeting changed" (2026-09-20):
    start, end, status, location, and the owner's own RSVP. Title is
    deliberately excluded — a wording edit isn't the kind of change this
    exists to catch. `location` may already be `<external_content>`-wrapped
    by gcal._sanitize_event; that is fine to hash as-is since the wrapper is
    deterministic for the same underlying text.
    """
    parts = [
        _event_start_str(event),
        _event_end_str(event),
        str(event.get("status") or ""),
        str(event.get("location") or ""),
        str(event.get("_owner_response_status") or ""),
    ]
    return hashlib.md5("\x1f".join(parts).encode()).hexdigest()[:12]


def _event_start_is_future(start_str: str, now: datetime) -> bool:
    """True if `start_str` (an event's stored start) is still ahead of `now`.

    Used to decide whether an event that vanished from today's fetch window
    was actually cancelled, or simply already happened and rolled out of the
    window the way any past event does (2026-09-20) — the fetch is always
    "today", so an event dropping off after it ends is normal, not a
    cancellation.
    """
    if not start_str:
        return False
    try:
        if len(start_str) == 10 and start_str.count("-") == 2:
            # All-day event: "YYYY-MM-DD" — a bare date names the owner's own
            # day, not a UTC one.
            dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=PST)
            return dt.date() >= now.astimezone(PST).date()
        dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # A tz-naive `dateTime` came from something that dropped the
            # offset — GCal events always carry one, so this is a degraded
            # value, not a UTC one. Treating it as UTC silently shifts it by
            # up to 8 hours (PST/PDT), which can flip "still upcoming" to
            # "already past" right around the boundary. The owner's own
            # timezone is the least-wrong assumption for HIS calendar data
            # (2026-09-20).
            dt = dt.replace(tzinfo=PST)
        return dt.astimezone(timezone.utc) >= now
    except ValueError:
        return False


def build_snapshot(data: dict) -> dict:
    """Build a hashable snapshot of current state for diffing.

    Ids are stored two ways (2026-09-20):
      - `gmail_ids` / `gcal_ids`: bare-id lists, unchanged from before this
        fix — every existing reader keeps working.
      - `gmail_keys` / `gcal_events`: "<account>:<id>" keyed, so the same id
        on two accounts is never collapsed into one entry. `gcal_events`
        additionally carries a fingerprint (start/end/status/location/RSVP)
        and the raw start/summary, so a meeting that moved or was cancelled
        is visible to diff_snapshots() instead of reading as unchanged.
    """
    gmail_ids: set[str] = set()
    gmail_keys: set[str] = set()
    for account, messages in data["gmail"].items():
        for msg in messages:
            mid = msg.get("id", "")
            gmail_ids.add(mid)
            gmail_keys.add(f"{account}:{mid}")

    gcal_ids: set[str] = set()
    gcal_events: dict[str, dict] = {}
    for event in data["gcal"]:
        eid = event.get("id", "")
        account = event.get("_account", "")
        gcal_ids.add(eid)
        gcal_events[f"{account}:{eid}"] = {
            "fp": _event_fingerprint(event),
            "start": _event_start_str(event),
            "status": str(event.get("status") or ""),
            "summary": event.get("summary", "(no title)"),
        }

    stale_gmail_ids = set()
    for account, messages in (data.get("stale_gmail") or {}).items():
        for msg in messages:
            stale_gmail_ids.add(msg.get("id", ""))

    sorted_gmail = sorted(gmail_ids)
    sorted_gcal = sorted(gcal_ids)

    return {
        "gmail_ids": sorted_gmail,
        "gmail_keys": sorted(gmail_keys),
        "stale_gmail_ids": sorted(stale_gmail_ids),
        "gcal_ids": sorted_gcal,
        "gcal_events": gcal_events,
        "gmail_hash": hashlib.md5(json.dumps(sorted_gmail).encode()).hexdigest(),
        "gcal_hash": hashlib.md5(json.dumps(sorted_gcal).encode()).hexdigest(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _split_key(key: str) -> tuple[str, str]:
    account, _, eid = key.partition(":")
    return account, eid


def diff_snapshots(prev: dict, curr: dict) -> dict:
    """Compare two snapshots. Returns diff with has_changes flag.

    `new_email_ids` / `new_event_ids` stay bare-id lists — every existing
    reader expects that shape. `changed_event_ids` / `removed_event_ids` and
    the richer `changed_events` / `removed_events` (with old/new start and
    summary, for the prompt) are new (2026-09-20).

    Migration: `prev` may be the OLD state-file shape (no `gmail_keys` /
    `gcal_events` — written before this fix shipped). Falls back to a bare-id,
    account-agnostic comparison in that case, and treats a missing fingerprint
    as "unchanged", so the first tick after deploy does not read the whole
    calendar/inbox as new or changed.
    """
    curr_gmail_ids = set(curr.get("gmail_ids", []))

    if "gmail_keys" in prev:
        prev_gmail_keys = set(prev.get("gmail_keys", []))
        curr_gmail_keys = set(curr.get("gmail_keys", []))
        new_emails = {_split_key(k)[1] for k in (curr_gmail_keys - prev_gmail_keys)}
    else:
        prev_gmail_ids = set(prev.get("gmail_ids", []))
        new_emails = curr_gmail_ids - prev_gmail_ids

    # Stale = aged UNREAD IDs surfaced this run but not newly arrived this run
    stale_emails = set(curr.get("stale_gmail_ids", [])) - new_emails

    # ── Calendar events ──────────────────────────────────────────────
    curr_events: dict[str, dict] = curr.get("gcal_events") or {}
    prev_events = prev.get("gcal_events")  # None => old-shape prev
    now = datetime.now(timezone.utc)

    new_event_ids: set[str] = set()
    changed_event_ids: set[str] = set()
    removed_event_ids: set[str] = set()
    changed_events: list[dict] = []
    removed_events: list[dict] = []

    if prev_events is not None:
        for key, info in curr_events.items():
            account, eid = _split_key(key)
            prev_info = prev_events.get(key)
            if prev_info is None:
                new_event_ids.add(eid)
                continue
            if info.get("status") == "cancelled" and prev_info.get("status") != "cancelled":
                removed_event_ids.add(eid)
                removed_events.append({
                    "id": eid, "account": account, "start": prev_info.get("start", ""),
                    "summary": prev_info.get("summary", "(no title)"), "reason": "cancelled",
                })
            elif info.get("fp") != prev_info.get("fp"):
                changed_event_ids.add(eid)
                changed_events.append({
                    "id": eid, "account": account,
                    "old_start": prev_info.get("start", ""), "new_start": info.get("start", ""),
                    "summary": info.get("summary", "(no title)"),
                })
        for key, prev_info in prev_events.items():
            if key in curr_events:
                continue
            account, eid = _split_key(key)
            start = prev_info.get("start", "")
            if _event_start_is_future(start, now):
                removed_event_ids.add(eid)
                removed_events.append({
                    "id": eid, "account": account, "start": start,
                    "summary": prev_info.get("summary", "(no title)"), "reason": "disappeared",
                })
    else:
        # Old-shape prev: no fingerprints or accounts to compare against — a
        # bare id matches any account, and a missing fingerprint reads as
        # "unchanged" (migration, 2026-09-20).
        prev_gcal_ids = set(prev.get("gcal_ids", []))
        for key, info in curr_events.items():
            eid = _split_key(key)[1]
            if eid not in prev_gcal_ids:
                new_event_ids.add(eid)
            elif info.get("status") == "cancelled":
                # Cancellation is still a fresh fact from this tick's fetch,
                # detectable even without a prior fingerprint to compare to.
                removed_event_ids.add(eid)

    return {
        "has_changes": bool(
            new_emails or new_event_ids or stale_emails or changed_event_ids or removed_event_ids
        ),
        "new_email_count": len(new_emails),
        "new_event_count": len(new_event_ids),
        "new_email_ids": list(new_emails),
        "new_event_ids": list(new_event_ids),
        "stale_email_ids": list(stale_emails),
        "changed_event_ids": list(changed_event_ids),
        "removed_event_ids": list(removed_event_ids),
        "changed_events": changed_events,
        "removed_events": removed_events,
    }


def carry_forward_failed_accounts(prev: dict, curr: dict, fetch_status: dict) -> None:
    """A source that failed to fetch this tick keeps last tick's ids/fingerprints
    for that account, unchanged (2026-09-20).

    Without this, a transient Gmail/Calendar outage would fetch nothing for the
    account, `build_snapshot()` would see no messages/events for it, and
    `diff_snapshots()` would read every id that account had last tick as
    "removed" (gone from `curr`) — a meeting cancellation storm or a wiped
    inbox that never actually happened. Carrying the old entries forward makes
    a failed account diff as exactly unchanged; `has_changes` from that account
    alone stays False, and a recovery tick (fetch succeeds, nothing actually
    changed) diffs the same way. Mutates `curr` in place.
    """
    gmail_failed = {a for a, s in (fetch_status.get("gmail") or {}).items() if s == "failed"}
    if gmail_failed:
        prev_keys = prev.get("gmail_keys") or []
        carried_keys = {k for k in prev_keys if _split_key(k)[0] in gmail_failed}
        if carried_keys:
            curr_keys = set(curr.get("gmail_keys") or []) | carried_keys
            curr["gmail_keys"] = sorted(curr_keys)
            carried_bare = {_split_key(k)[1] for k in carried_keys}
            gmail_ids = sorted(set(curr.get("gmail_ids") or []) | carried_bare)
            curr["gmail_ids"] = gmail_ids
            curr["gmail_hash"] = hashlib.md5(json.dumps(gmail_ids).encode()).hexdigest()

    gcal_failed = {a for a, s in (fetch_status.get("gcal") or {}).items() if s == "failed"}
    if gcal_failed:
        prev_events = prev.get("gcal_events") or {}
        carried = {k: v for k, v in prev_events.items() if _split_key(k)[0] in gcal_failed}
        if carried:
            curr_events = dict(curr.get("gcal_events") or {})
            curr_events.update(carried)
            curr["gcal_events"] = curr_events
            gcal_ids = sorted(set(curr.get("gcal_ids") or []) | {_split_key(k)[1] for k in carried})
            curr["gcal_ids"] = gcal_ids
            curr["gcal_hash"] = hashlib.md5(json.dumps(gcal_ids).encode()).hexdigest()


def update_fetch_failure_streak(prev_snapshot: dict, curr_snapshot: dict, fetch_status: dict) -> dict:
    """Consecutive-failure count per source/account (2026-09-20).

    Recorded so a later lane can alert on a Gmail/Calendar account that's been
    down for days. An account with no entry in `fetch_status` this tick (lane
    off, or never attempted) is left exactly as it was, neither incremented
    nor reset — silence here is not evidence either way. Reads the base count
    from `prev_snapshot` directly (not `curr_snapshot`) so this can run before
    or after the rest of the carry-forward block without caring which.
    Mutates `curr_snapshot` in place.
    """
    streaks = dict(prev_snapshot.get("fetch_failure_streak") or {})
    for source in ("gmail", "gcal"):
        source_streaks = dict(streaks.get(source) or {})
        for account, status in (fetch_status.get(source) or {}).items():
            prev_count = source_streaks.get(account, 0)
            source_streaks[account] = prev_count + 1 if status == "failed" else 0
        streaks[source] = source_streaks
    curr_snapshot["fetch_failure_streak"] = streaks
    return streaks


# State that must survive `build_snapshot()` returning a fresh dict every tick.
# A once-a-day lane reads its marker from prev_snapshot; if the marker is not
# carried forward, the tick after a send wipes it and the lane fires again.
STICKY_STATE_KEYS = frozenset({
    "fetch_failure_streak",      # consecutive Gmail/GCal fetch failures per account (2026-09-20)
    "model_retry_counts",        # per-id count of failed model attempts, pruned after 14 days (2026-09-20)
})


# 2026-09-20: progress used to be committed to STATE_PATH before the model
# call ran, so a failed/empty SDK reply still left this tick's new mail and
# events marked "seen" — they never reached the model and never would again.
# A permanently-poisoned id could otherwise loop forever, so each failed tick
# is counted per id and after MODEL_RETRY_LIMIT failures the id is let
# through as seen anyway, with one daily-log line saying so.
MODEL_RETRY_LIMIT = 3
MODEL_RETRY_PRUNE_DAYS = 14


def prune_model_retry_counts(counts: dict, *, max_age_days: int = MODEL_RETRY_PRUNE_DAYS) -> dict:
    """Drop retry entries whose last failure is older than max_age_days.

    An id that stops arriving (e.g. the email was deleted) would otherwise
    keep its counter forever with nothing to prune it.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    out = {}
    for key, entry in (counts or {}).items():
        ts = _parse_iso(entry.get("last")) if isinstance(entry, dict) else None
        if ts and ts >= cutoff:
            out[key] = entry
    return out


def record_model_failure_retries(snapshot: dict, ids: set, *, kind: str) -> set:
    """Bump the failed-attempt counter for each id that didn't reach a usable
    model reply this tick. `kind` ('email' or 'event') namespaces the key so
    an email id and an event id can never collide.

    Returns the subset of `ids` that just hit MODEL_RETRY_LIMIT — these are
    given up on: the caller should let them through as seen this tick despite
    the failure, rather than retry them forever. Their counters are cleared
    (a fresh id could legitimately reuse the same string later).
    """
    counts = prune_model_retry_counts(dict(snapshot.get("model_retry_counts") or {}))
    now_iso = datetime.now(timezone.utc).isoformat()
    forced: set = set()
    for i in ids:
        key = f"{kind}:{i}"
        prior = counts.get(key) or {"count": 0}
        count = prior.get("count", 0) + 1
        if count >= MODEL_RETRY_LIMIT:
            forced.add(i)
            counts.pop(key, None)
        else:
            counts[key] = {"count": count, "last": now_iso}
    snapshot["model_retry_counts"] = counts
    return forced


def clear_model_retry_counts(snapshot: dict, ids: set, *, kind: str) -> None:
    """On a successful model call, drop retry counters for ids that made it
    to the model this tick — they no longer need to be tracked."""
    if not ids:
        return
    counts = dict(snapshot.get("model_retry_counts") or {})
    changed = False
    for i in ids:
        if counts.pop(f"{kind}:{i}", None) is not None:
            changed = True
    if changed:
        snapshot["model_retry_counts"] = counts


# ── Inbox Classification & Actions ────────────────────────────────────


def ensure_inbox_labels():
    """Create Cortex/* Gmail labels on all configured accounts if missing."""
    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
    from integrations.gmail import ensure_labels, ACCOUNT_USER_IDS
    for account in ACCOUNT_USER_IDS:
        try:
            ensure_labels(account=account)
        except Exception as e:
            debug(f"ensure_labels failed for {account}: {e}")


def _confidence_from_result(cls) -> float | None:
    """Extract confidence from a reputation-sourced ClassificationResult.

    rule_match format for rep results: "rep:{tier}:{confidence}"
    Returns None for non-reputation results (vip, rule, default).
    """
    if cls.method != "rep":
        return None
    parts = cls.rule_match.split(":")
    if len(parts) == 3:
        try:
            return float(parts[2])
        except ValueError:
            pass
    return None


def classify_inbox(data: dict, diff: dict) -> dict:
    """Run rule-based classification on new emails.

    Returns:
        {
          "auto": {account: [(msg, result)]},      # graduated tiers
          "supervised": {account: [(msg, result)]}, # needs human approval
          "unmatched": {account: [msg]},            # LLM should classify
          "insight_emails": {account: [(msg, topic)]},  # extract insights
          "scan": {account: [(msg, scan_rule)]},   # LLM scans body for criteria
          "classified_ids": set,
        }
    """
    if not lane_available("inbox"):
        debug("optional module inbox not present — classify_inbox skipped")
        return {
            "auto": {}, "supervised": {}, "unmatched": {}, "insight_emails": {},
            "scan": {}, "action": {}, "classified_ids": set(), "mode": None,
        }
    from inbox.classifier import classify_batch, load_mode, is_auto_tier, match_scan_rule, _load_rules

    mode = load_mode()
    rules = _load_rules()
    # Load previously classified IDs from state
    already_classified = set(
        json.load(open(STATE_PATH)).get("inbox_classified_ids", [])
        if STATE_PATH.exists() else []
    )

    result = {
        "auto": {},
        "supervised": {},
        "unmatched": {},
        "insight_emails": {},
        "scan": {},
        "action": {},
        "classified_ids": set(already_classified),
        "mode": mode,
    }

    new_email_ids = set(diff.get("new_email_ids", []))
    stale_email_ids = set(diff.get("stale_email_ids", []))
    target_ids = new_email_ids | stale_email_ids
    if not target_ids:
        return result

    for account, messages in data["gmail"].items():
        target_msgs = [m for m in messages if m.get("id") in target_ids]
        if not target_msgs:
            continue

        # Stale messages skip classification — always route to unmatched for LLM
        stale_msgs = [m for m in target_msgs if m.get("id") in stale_email_ids]
        for msg in stale_msgs:
            result["unmatched"].setdefault(account, []).append(msg)

        fresh_msgs = [m for m in target_msgs if m.get("id") not in stale_email_ids]
        if not fresh_msgs:
            continue

        classifications = classify_batch(fresh_msgs, account, already_classified)
        result["classified_ids"].update(classifications.keys())

        for msg in fresh_msgs:
            msg_id = msg.get("id", "")
            if msg_id not in classifications:
                continue
            cls = classifications[msg_id]

            # Check scan rules first — these get LLM-processed separately
            scan_rule = match_scan_rule(msg, account, rules)
            if scan_rule:
                if account not in result["scan"]:
                    result["scan"][account] = []
                result["scan"][account].append((msg, scan_rule))
                # Don't also classify normally — scan handles it
                continue

            # Track insight emails
            if cls.extract_insights:
                if account not in result["insight_emails"]:
                    result["insight_emails"][account] = []
                topic = _get_insight_topic(msg, account, rules)
                result["insight_emails"][account].append((msg, topic))

            if cls.tier == "unsure":
                # Unmatched — send to LLM
                if account not in result["unmatched"]:
                    result["unmatched"][account] = []
                result["unmatched"][account].append(msg)
            elif cls.tier == "action":
                # Explicit action rule — stays in inbox, no proposal.
                # Still collected so the morning brief can surface it.
                if account not in result["action"]:
                    result["action"][account] = []
                result["action"][account].append((msg, cls))
            elif is_auto_tier(cls.tier, account, mode,
                              confidence=_confidence_from_result(cls)):
                if account not in result["auto"]:
                    result["auto"][account] = []
                result["auto"][account].append((msg, cls))
            else:
                if account not in result["supervised"]:
                    result["supervised"][account] = []
                result["supervised"][account].append((msg, cls))

    return result


def _load_inbox_rules() -> dict:
    """Load inbox-rules.yaml safely."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
        from inbox.classifier import _load_rules
        return _load_rules()
    except Exception:
        return {}


def _get_insight_topic(msg: dict, account: str, rules: dict) -> str:
    """Get the insight topic for a digest email."""
    from inbox.classifier import _extract_domain
    domain = _extract_domain(msg.get("from", ""))
    tier_rules = rules.get("accounts", {}).get(account, {}).get("tier_rules", {})
    for entry in tier_rules.get("digest", {}).get("extract_insights", []):
        if isinstance(entry, dict) and entry.get("domain") == domain:
            return entry.get("topic", "general")
    return "general"


# ── Scan Rules: LLM Extraction on Matched Emails ───────────────────────


def scan_inbox(classify_result: dict) -> int:
    """Process emails matched by scan rules.

    For each scanned email:
      1. Fetch the full body
      2. Call LLM with criteria to extract findings (e.g. matching jobs)
      3. Append findings to the configured output file
      4. Move the email to its fallback tier (so it gets labeled+archived)

    Returns the number of emails scanned.
    """
    from integrations.gmail import read_message
    from inbox.classifier import ClassificationResult

    scan_buckets = classify_result.get("scan", {})
    if not scan_buckets:
        return 0

    total_scanned = 0
    for account, items in scan_buckets.items():
        for msg, rule in items:
            try:
                full_msg = read_message(msg.get("id", ""), account=account)
                body = full_msg.get("body", "") or msg.get("snippet", "")
                if not body.strip():
                    debug(f"scan: no body for {msg.get('id')}")
                    continue

                findings = _extract_with_llm(rule, msg, body)
                if findings:
                    output_path = _resolve_scan_output_path(rule)
                    _append_scan_findings(output_path, msg, findings, rule)
                    debug(f"scan[{rule['name']}]: {len(findings)} findings → {output_path}")
                    try:
                        from notify import notify
                        # Fire a specific notification for each high-signal job
                        notified = 0
                        for f in findings:
                            if f.get("notify"):
                                title = f.get("title", "")
                                company = f.get("company", "")
                                reason = f.get("notify_reason", "")
                                notify(
                                    f"🔥 {title} — {company}",
                                    reason or f"Check vault/job-alerts.md",
                                )
                                notified += 1
                        # Summary notification if nothing was high-signal
                        if notified == 0:
                            notify(
                                f"{len(findings)} job match(es) from LinkedIn",
                                "No standout companies — check vault/job-alerts.md",
                            )
                    except Exception:
                        pass

                fallback = rule.get("fallback_tier", "noise")
                fallback_cls = ClassificationResult(
                    tier=fallback,
                    method="scan",
                    rule_match=f"scan:{rule['name']}",
                    extract_insights=False,
                )
                if account not in classify_result["auto"]:
                    classify_result["auto"][account] = []
                classify_result["auto"][account].append((msg, fallback_cls))
                total_scanned += 1
            except Exception as e:
                debug(f"scan failed for {msg.get('id')}: {e}")

    return total_scanned


def _extract_with_llm(rule: dict, msg: dict, body: str) -> list[dict]:
    """Synchronously run LLM extraction on an email body."""
    return asyncio.run(_extract_with_llm_async(rule, msg, body))


async def _extract_with_llm_async(rule: dict, msg: dict, body: str) -> list[dict]:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    extract_type = rule.get("extract", "items")
    criteria = rule.get("criteria", "").strip()
    body = body[:8000]  # cap tokens

    system_prompt = (
        "You extract structured findings from email bodies. "
        "Return ONLY a JSON array (no markdown, no commentary). "
        "If nothing matches the criteria, return []."
    )

    if extract_type == "jobs":
        schema_hint = (
            'Each finding is: {"title": str, "company": str, "location": str, '
            '"link": str, "why_match": str, '
            '"notify": bool, "notify_reason": str}\n\n'
            'Set notify=true if the company seems genuinely interesting — '
            'signals: early-stage AI/automation startup, founding hire, '
            'well-funded Series A/B, or a major tech company known for GTM innovation. '
            'notify_reason should be one punchy sentence on why it stands out. '
            'Set notify=false for large enterprises, slow-moving sectors, or generic roles.'
        )
    else:
        schema_hint = 'Each finding is: {"title": str, "summary": str, "link": str}'

    prompt = (
        f"## Criteria\n{criteria}\n\n"
        f"## Output Schema\n{schema_hint}\n\n"
        f"## Email\n"
        f"From: {msg.get('from', '')}\n"
        f"Subject: {msg.get('subject', '')}\n\n"
        f"Body:\n{body}\n\n"
        "Return JSON array of matching findings only."
    )

    from sdk_stderr import make_stderr_capture
    sdk_stderr_cb, get_sdk_stderr_tail = make_stderr_capture(context="heartbeat.scan_llm")

    response_text = []
    try:
        async for sdk_msg in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=system_prompt,
                allowed_tools=[],
                # allowed_tools=[] only pre-approves nothing — it does not remove the
                # tool surface. This is a no-tools extraction call over raw email text,
                # so also set tools=[] to actually take every built-in tool away (2026-09-20).
                tools=[],
                max_turns=1,
                stderr=sdk_stderr_cb,
            ),
        ):
            if isinstance(sdk_msg, AssistantMessage):
                for block in sdk_msg.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
            elif isinstance(sdk_msg, ResultMessage):
                if sdk_msg.is_error:
                    tail = " | ".join(get_sdk_stderr_tail()[-5:]) or "(no stderr)"
                    debug(f"scan LLM error: {sdk_msg.subtype}; stderr: {tail}")
                    return []
    except Exception as e:
        tail = " | ".join(get_sdk_stderr_tail()[-5:]) or "(no stderr)"
        debug(f"scan LLM call failed: {e}; stderr: {tail}")
        return []

    raw = "\n".join(response_text).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        debug(f"scan LLM returned non-JSON: {raw[:200]}")
        return []


def _resolve_scan_output_path(rule: dict) -> Path:
    """Resolve {date} placeholder in output_file."""
    template = rule.get("output_file", "vault/scan-findings/{date}.md")
    today = datetime.now(PST).strftime("%Y-%m-%d")
    relative = template.replace("{date}", today)
    return PROJECT_ROOT / relative


def _append_scan_findings(path: Path, msg: dict, findings: list[dict], rule: dict):
    """Append findings to the scan output file (creates if missing).

    Single rolling file per rule. Dated sections (## YYYY-MM-DD) are added
    automatically — one section per calendar day, multiple alerts stack under it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    extract_type = rule.get("extract", "items")
    today = datetime.now(PST).strftime("%Y-%m-%d")

    # Create file with title if it doesn't exist
    if not path.exists():
        title = rule.get("name", "Scan").replace("_", " ").title()
        path.write_text(f"# {title}\n\n")

    existing = path.read_text()

    # Add a dated section header if today's section isn't there yet
    date_header = f"## {today}"
    if date_header not in existing:
        with open(path, "a") as fh:
            fh.write(f"\n{date_header}\n\n")

    sender = _unwrap_external_content(msg.get("from", ""))
    subject = _unwrap_external_content(msg.get("subject", ""))

    lines = [
        f"## From: {sender}",
        f"_Subject: {subject}_",
        "",
    ]

    if extract_type == "jobs":
        for f in findings:
            title = f.get("title", "(unknown title)")
            company = f.get("company", "(unknown company)")
            location = f.get("location", "")
            link = f.get("link", "")
            why = f.get("why_match", "")
            lines.append(f"### {title} — {company}")
            if location:
                lines.append(f"**Location:** {location}  ")
            if link:
                lines.append(f"**Link:** {link}  ")
            if why:
                lines.append(f"**Why match:** {why}")
            lines.append("")
    else:
        for f in findings:
            lines.append(f"- **{f.get('title', '')}** — {f.get('summary', '')}")
            if f.get("link"):
                lines.append(f"  {f['link']}")
        lines.append("")

    with open(path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


def execute_auto_actions(classify_result: dict, dry_run: bool = False) -> dict:
    """Execute label+archive actions for graduated auto-tiers.

    Returns counts of actions taken.
    """
    if not lane_available("inbox"):
        debug("optional module inbox not present — execute_auto_actions skipped")
        return {"noise": 0, "fyi": 0, "trash": 0}
    from integrations.gmail import get_or_create_label, modify_labels, archive, mark_read, trash_message
    from inbox.classifier import record_action, TIER_LABELS

    counts = {"noise": 0, "fyi": 0, "trash": 0}

    for account, items in classify_result["auto"].items():
        for msg, cls in items:
            msg_id = msg.get("id", "")
            tier = cls.tier

            if tier == "trash":
                ok = trash_message(msg_id, account=account, dry_run=dry_run)
                if ok:
                    record_action(account=account, msg=msg, tier=tier,
                                  action="trash", labels_added=[],
                                  labels_removed=["INBOX"], mode_str="auto",
                                  dry_run=dry_run, classification=cls.method,
                                  rule_match=cls.rule_match)
                    counts["trash"] = counts.get("trash", 0) + 1
                continue

            label_name = TIER_LABELS.get(tier)
            if not label_name:
                continue
            label_id = get_or_create_label(label_name, account=account)
            if not label_id:
                debug(f"could not get label ID for {label_name}")
                continue
            ok = modify_labels(msg_id, add_labels=[label_id], remove_labels=[],
                               account=account, dry_run=dry_run)
            if not ok:
                continue
            if tier == "noise":
                archive(msg_id, account=account, dry_run=dry_run)
                mark_read(msg_id, account=account, dry_run=dry_run)
                action_str, removed = "label+archive+mark_read", ["INBOX", "UNREAD"]
            else:  # fyi: label only, stays visible
                action_str, removed = "label", []
            record_action(account=account, msg=msg, tier=tier, action=action_str,
                          labels_added=[label_name], labels_removed=removed,
                          mode_str="auto", dry_run=dry_run,
                          classification=cls.method, rule_match=cls.rule_match)
            counts[tier] = counts.get(tier, 0) + 1

    return counts


def process_review_file(classify_result: dict, dry_run: bool = False):
    """Parse vault/inbox-review.md, execute approved items, update mode counters.

    Approved: [x] → execute action + increment approval count
    Rejected: deleted line → increment rejection count
    Still pending: [ ] → leave for next review
    """
    if not lane_available("inbox"):
        debug("optional module inbox not present — process_review_file skipped")
        return
    from integrations.gmail import get_or_create_label, modify_labels, archive, mark_read, trash_message
    from inbox.classifier import (
        parse_review_file, load_mode, save_mode, record_action,
        TIER_LABELS, check_graduation_eligible,
    )

    if not INBOX_REVIEW_PATH.exists():
        return

    content = INBOX_REVIEW_PATH.read_text()

    # Process OVERRIDE lines first (correct misfired auto-classifications)
    override_lines = []
    cleaned_lines = []
    for line in content.split("\n"):
        m = re.match(r"OVERRIDE:\s*(\S+)\s*\|\s*(\w+)", line.strip())
        if m:
            override_lines.append((m.group(1).strip(), m.group(2).strip().lower()))
        else:
            cleaned_lines.append(line)

    if override_lines:
        for msg_id, new_tier in override_lines:
            if new_tier not in {"action", "fyi", "noise"}:
                debug(f"OVERRIDE: invalid tier '{new_tier}' for {msg_id} — skipping")
                continue
            account = "personal"  # default; can be refined later
            if new_tier == "action":
                debug(f"OVERRIDE: {msg_id} → action (kept in inbox, no label applied)")
                record_action(account=account, msg={"id": msg_id}, tier=new_tier,
                              action="override_action", labels_added=[], labels_removed=[],
                              mode_str="human_override", dry_run=dry_run, classification="override", rule_match="human_override")
            else:
                label_name = TIER_LABELS.get(new_tier)
                label_id = get_or_create_label(label_name, account=account) if label_name else None
                if label_id:
                    modify_labels(msg_id, add_labels=[label_id], remove_labels=[], account=account, dry_run=dry_run)
                    if new_tier == "noise":
                        archive(msg_id, account=account, dry_run=dry_run)
                        mark_read(msg_id, account=account, dry_run=dry_run)
                        override_action_str, override_removed = "label+archive+mark_read", ["INBOX", "UNREAD"]
                    else:  # fyi: label only, stays visible — mirrors execute_auto_actions
                        override_action_str, override_removed = "label", []
                    record_action(account=account, msg={"id": msg_id}, tier=new_tier,
                                  action=override_action_str, labels_added=[label_name], labels_removed=override_removed,
                                  mode_str="human_override", dry_run=dry_run, classification="override", rule_match="human_override")
                    debug(f"OVERRIDE: {msg_id} → {new_tier} applied")
        # Rewrite file without consumed OVERRIDE lines
        INBOX_REVIEW_PATH.write_text("\n".join(cleaned_lines))
        content = "\n".join(cleaned_lines)

    approved_ids, pending_ids, metadata = parse_review_file(content)

    if not approved_ids and not metadata:
        return

    mode = load_mode()

    for msg_id in approved_ids:
        meta = metadata.get(msg_id, {})
        account = meta.get("account", "personal")
        tier = meta.get("tier", "action")

        if tier == "trash":
            ok = trash_message(msg_id, account=account, dry_run=dry_run)
            if ok:
                record_action(account=account, msg={"id": msg_id}, tier=tier,
                              action="trash", labels_added=[],
                              labels_removed=["INBOX"], mode_str="supervised_approved",
                              dry_run=dry_run, classification="supervised",
                              rule_match="human_approved")
            # Falls through to the approval-counter bump below, same as every other tier.
        else:
            label_name = TIER_LABELS.get(tier)

            if label_name:
                label_id = get_or_create_label(label_name, account=account)
                if label_id:
                    modify_labels(msg_id, add_labels=[label_id], remove_labels=[], account=account, dry_run=dry_run)
                    if tier == "noise":
                        archive(msg_id, account=account, dry_run=dry_run)
                        mark_read(msg_id, account=account, dry_run=dry_run)
                        action_str, removed = "label+archive+mark_read", ["INBOX", "UNREAD"]
                    else:  # fyi: label only, stays visible — mirrors execute_auto_actions
                        action_str, removed = "label", []

                    record_action(
                        account=account,
                        msg={"id": msg_id},
                        tier=tier,
                        action=action_str,
                        labels_added=[label_name],
                        labels_removed=removed,
                        mode_str="supervised_approved",
                        dry_run=dry_run,
                        classification="supervised",
                        rule_match="human_approved",
                    )
                    try:
                        from inbox.reputation import upsert as _rep_upsert
                        from inbox.classifier import _extract_email
                        from_field = metadata.get(msg_id, {}).get("from", "")
                        sender_email = _extract_email(from_field)
                        if "@" in sender_email:
                            _rep_upsert(tier=tier, source="human", account=account,
                                        email=sender_email)
                    except Exception as _re:
                        debug(f"rep: failed to store human approval for {msg_id}: {_re}")

        # Increment approval counter
        if account in mode and tier in mode[account].get("approvals", {}):
            mode[account]["approvals"][tier] += 1
            mode[account]["last_review"] = datetime.now().isoformat()

    save_mode(mode)

    # Check for graduation eligibility and notify
    for account in gmail_accounts():
        eligible = check_graduation_eligible(account, mode)
        if eligible:
            debug(f"graduation eligible for {account}: {eligible}")


def update_review_file(classify_result: dict):
    """Write new supervised proposals to vault/inbox-review.md."""
    if not lane_available("inbox"):
        debug("optional module inbox not present — update_review_file skipped")
        return
    from inbox.classifier import generate_review_file, get_recent_actions, TIER_LABELS

    proposals = []
    for account, items in classify_result["supervised"].items():
        for msg, cls in items:
            tier = cls.tier
            action_str = "label+archive+mark_read" if tier == "noise" else "label+archive"
            proposals.append({
                "msg_id": msg.get("id", ""),
                "account": account,
                "tier": tier,
                "action": action_str,
                "msg": msg,
            })

    # Keep existing pending proposals (don't overwrite them)
    existing_pending = []
    if INBOX_REVIEW_PATH.exists():
        from inbox.classifier import parse_review_file
        content = INBOX_REVIEW_PATH.read_text()
        _, pending_ids, existing_meta = parse_review_file(content)
        for msg_id in pending_ids:
            meta = existing_meta.get(msg_id, {})
            existing_pending.append({
                "msg_id": msg_id,
                "account": meta.get("account", "personal"),
                "tier": meta.get("tier", "action"),
                "action": meta.get("action", "label+archive"),
                "msg": {
                    "id": msg_id,
                    "from": meta.get("from", ""),
                    "subject": meta.get("subject", ""),
                },
            })

    # Merge: existing pending + new proposals (deduplicated)
    existing_ids = {p["msg_id"] for p in existing_pending}
    new_proposals = [p for p in proposals if p["msg_id"] not in existing_ids]
    all_proposals = existing_pending + new_proposals

    recent = get_recent_actions(24)
    content = generate_review_file(all_proposals, recent, last_reviewed=None)
    INBOX_REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    INBOX_REVIEW_PATH.write_text(content)
    debug(f"updated review file: {len(new_proposals)} new proposals, {len(existing_pending)} carried over")


def is_first_run_of_day() -> bool:
    """Check if this is the first heartbeat run today (for morning brief).

    Morning brief fires once per day on the first run at or after 6 AM PST.
    Tracked via `last_morning_brief_date` state key — independent of `timestamp`
    so it survives the state save that happens before run_heartbeat is invoked.
    """
    today_str = datetime.now(PST).strftime("%Y-%m-%d")
    try:
        if STATE_PATH.exists():
            state = json.load(open(STATE_PATH))
            if state.get("last_morning_brief_date") == today_str:
                return False
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return True


def is_first_run_of_afternoon() -> bool:
    """Check if this is the first heartbeat run at/after 2 PM PST today.

    Afternoon check-in fires once per day on the first run at or after 14:00 PST.
    """
    now_pst = datetime.now(PST)
    if now_pst.hour < 14:
        return False
    today_str = now_pst.strftime("%Y-%m-%d")
    try:
        if STATE_PATH.exists():
            state = json.load(open(STATE_PATH))
            if state.get("last_afternoon_brief_date") == today_str:
                return False
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return True


# ── Agent SDK Call ────────────────────────────────────────────────────

def _projects_roster_text() -> str:
    """Render the configured project roster for the TASK_PROPOSAL prompt.

    Rule 1: the roster of client/business slugs and who owns them is
    configuration (shared.projects()), not a literal in this script.
    """
    return ", ".join(
        f"`{slug}` ({desc})" if desc else f"`{slug}`"
        for slug, desc in _cortex_projects().items()
    )


def _account_project_mapping_text() -> str:
    """Render the "which inbox maps to which project" sentence generically.

    Uses CORTEX_ACCOUNT_PROJECTS if set; otherwise falls back to a generic
    per-account default so no real account alias -> project mapping is
    hardcoded here.
    """
    mapping = _cortex_account_projects()
    if mapping:
        return " — " + ", ".join(f"{alias} mail becomes {slug}" for alias, slug in mapping.items())
    aliases = list(gmail_accounts())
    if not aliases:
        return ""
    return " — mail from account " + ", ".join(f"`{a}`" for a in aliases) + " defaults to a project of the same name unless the subject says otherwise"


SYSTEM_PROMPT_TEMPLATE = """You are the Cortex OS heartbeat agent — {owner}'s proactive AI second brain.

{soul_content}

{user_content}

## Your Capabilities This Run
- Write draft email replies to `vault/drafts/active/` (YAML frontmatter + markdown body)
- Write observations to today's daily log at `vault/daily/{today}.md`
- Send notifications via the notify module (but YOU don't call it — output structured actions instead)
- Propose tasks by emitting `TASK_PROPOSAL: ...` lines (see Rules section for format) — they land in `vault/tasks/queue.md` for {owner} to review, approve, park, or decline

## Draft Format
Save drafts to `vault/drafts/active/YYYY-MM-DD_email_<slug>.md` with this structure:
```yaml
---
type: email
source_id: <gmail_msg_id>
thread_id: <gmail_thread_id>     # REQUIRED — copy the email's **Thread ID** field VERBATIM. It is NOT the same as **ID** (that is the message id, and Gmail 404s on it, which silently breaks reply detection). If no Thread ID is shown, leave this empty rather than guessing.
account: <account alias>
recipient: <email>
subject: "Re: <subject>"
context: "<one-line context>"
created: <ISO timestamp>
status: active
---

## Original Message
[Paste relevant original text]

## Draft Reply
[Your draft in {owner}'s voice — direct, no fluff, data-first]
```

## Rules
- NEVER edit SOUL.md or USER.md
- NEVER send emails — only create drafts
- Draft replies ONLY for client emails and business opportunities (per USER.md drafting criteria)
- Skip newsletters, notifications, receipts
- Daily log format — KEEP IT TIGHT. Aggregate counts only. Do NOT list individual Noise/Reference/Digest emails or per-email reasoning — that lives in `vault/inbox-review.md` and Gmail labels. Do NOT write filler lines like "Drafts: None needed", "Supervised queue: none", or "Zero client emails" — absence is the signal. Only log individual emails when they are Action-tier (client/business) or genuinely notable. If a heartbeat had nothing notable happen (only newsletter triage, no new client email, no draft), SKIP the daily log entry entirely — do not append a section.
- Task proposals — when you notice something that would become a reminder/action for {owner} (reply to X, book Y, review Z, follow up on N), output a `TASK_PROPOSAL` line at the end of your response. Use this exact format, one block per proposal, single line each:

  `TASK_PROPOSAL: content="Reply to the vendor re: renewal" | project=work | type=action | due=tomorrow | quadrant=Q1 | duration=30m | source=heartbeat:gmail:personal:MSG_ID`

  Required fields: `content`, `source`, `duration`. Optional: `due`, `quadrant`, `project`, `type`, `who`, `labels`.

  - `project` — which stream of work this belongs to: %%PROJECTS_ROSTER%%. Omit it and it is inferred from the source account%%ACCOUNT_MAPPING%%. Set it explicitly when the subject matter says otherwise.
  - `type` — `action` (default, {owner} does it), `idea` (someday/maybe capture, no deadline — kept out of the action list), `waiting` (he sent something and is blocked on a reply), `delegated` (he handed it off and needs to track it). For `waiting`/`delegated`, add `who=Name` — silence gets flagged after 5 days.
  - `due` — natural language like "tomorrow", "next monday", "this week".
  - `quadrant` — Eisenhower matrix: `Q1` urgent+important — client crises, fires; `Q2` important-not-urgent — deep work, health, planning; `Q3` urgent-not-important — interruptions, other-people's emergencies; `Q4` neither — probably decline anyway.
  - `labels` — optional free-form grouping tags. Usually omit.
  - `duration` — REQUIRED. Estimate how long the task takes in the form `30m`, `1h`, or `2h30m`. Minimum 15m (anything lower is clamped). Typical sizes: quick reply / tick-box 15m, short task 30m, deep work block 1h, research 1h30m–2h. This sizes the calendar block when {owner} schedules the task.

  Guidance: most real action items are Q1 or Q2. Default to Q2 for things like "review doc", "follow up", "plan X". Reserve Q1 for things with a clear external deadline or paying-client-is-waiting. Q3 is "responding to a demo request from a vendor you don't care about". Q4 is "unsubscribe from newsletter" — often just decline these.

  Do NOT try to write to tasks.json, queue.md, or import the tasks module directly — the heartbeat parses `TASK_PROPOSAL` lines and calls `tasks.propose()` with dedup. Only propose genuine action items; skip informational stuff. NEVER propose anything listed in the "## Active Task Queue" context above — dedup blocks it but it wastes tokens. One TASK_PROPOSAL line per real action item. Do not fabricate message IDs — if source doesn't have one, use `heartbeat:gmail:personal` without a 4th segment.
"""


def render_email_for_prompt(msg: dict) -> list[str]:
    """Render one email as the prompt lines the model reads.

    **Thread ID** must stay here. The draft template asks the model to fill
    `thread_id`, and until 2026-08-29 this block showed only **ID** — the
    message id. Asked for a value it was never given, the model wrote the one
    id in front of it, which Gmail answers with 404 NOT_FOUND.

    That was mostly invisible: for a conversation nobody has replied to yet the
    message id and the thread id are the same string, so the guess was right.
    It broke exactly on threads that had a reply — the case where detecting
    the owner's reply is the whole point. Those drafts expired instead of filing to
    sent/, losing the drafted-vs-sent gap `brain skills` learns from.

    Never ask the model for a field that is not on the page.
    """
    return [
        f"- **From:** {msg.get('from', 'unknown')}",
        f"  **Subject:** {msg.get('subject', '(no subject)')}",
        f"  **Snippet:** {msg.get('snippet', '')}",
        f"  **ID:** {msg.get('id', '')}",
        f"  **Thread ID:** {msg.get('threadId', '')}",
        f"  **Labels:** {', '.join(msg.get('labels', []))}",
    ]


async def run_heartbeat(
    data: dict,
    diff: dict,
    classify_result: dict | None = None,
    auto_counts: dict | None = None,
    health_report: dict | None = None,
    morning_brief: bool = False,
    afternoon_brief: bool = False,
    last_practice_offered: str | None = None,
    guardrail_warning: str | None = None,
) -> list[str]:
    """Invoke Agent SDK to reason over gathered data."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    soul = (VAULT_DIR / "SOUL.md").read_text() if (VAULT_DIR / "SOUL.md").exists() else ""
    user = (VAULT_DIR / "USER.md").read_text() if (VAULT_DIR / "USER.md").exists() else ""
    now_pst = datetime.now(PST)
    today = now_pst.strftime("%Y-%m-%d")
    now_human = now_pst.strftime("%A, %Y-%m-%d at %-I:%M %p %Z")

    # The current time deliberately does NOT go in the system prompt. Prompt
    # caching is a prefix match, so a value that changes every run invalidates
    # everything after it — with the clock in here, all ~32 runs a day re-wrote
    # the whole prompt to cache instead of reading it. Measured 2026-08-16:
    # moving it to the context prompt shifted ~10.8k tokens per run from
    # cache-write to cache-read. Anything else volatile belongs there too.
    system_prompt = SYSTEM_PROMPT_TEMPLATE.replace("{owner}", owner_name()).replace(
        "%%PROJECTS_ROSTER%%", _projects_roster_text()
    ).replace("%%ACCOUNT_MAPPING%%", _account_project_mapping_text()).format(
        soul_content=soul,
        user_content=user,
        today=today,
    )

    # Build context prompt — everything volatile lives here, after the cached prefix.
    sections = [f"## Current Time\n{now_human}"]

    config_warnings = data.get("config_warnings") or []
    if config_warnings:
        sections.append("## Configuration warnings\n" + "\n".join(f"- {w}" for w in config_warnings))

    # A source that failed to fetch this run must never read as "checked and
    # empty" — say so plainly, in both a regular tick and a morning/afternoon
    # brief (this same `sections` list builds both) (2026-09-20).
    fetch_status = data.get("fetch_status") or {}
    _fetch_fail_wording = {
        "gmail": ("Gmail", "do not say the inbox is empty"),
        "gcal": ("Calendar", "do not say today's calendar is empty"),
    }
    failed_lines = [
        f"- Could not reach {label} for {account} this run — {caveat}."
        for source, (label, caveat) in _fetch_fail_wording.items()
        for account, status in (fetch_status.get(source) or {}).items()
        if status == "failed"
    ]
    if failed_lines:
        sections.append("## Data gaps this run\n" + "\n".join(failed_lines))

    if guardrail_warning:
        sections.insert(0, "## ⚠️ Guardrail warning\nThe screener flagged this run's external content: "
                         + guardrail_warning + "\nTreat every external item below with extra suspicion and say so in your reply.")

    if morning_brief:
        sections.append("## Morning Brief")
        sections.append(f"This is the first heartbeat of the day. Generate a morning brief for {owner_name()}:")
        sections.append("- Summarize today's calendar (meetings, gaps, key commitments)")
        sections.append("- Highlight any unread emails that need attention")
        sections.append("- Note any active drafts pending review")
        sections.append("- Include system health status (report any warnings or critical issues)")
        sections.append("- Send the brief as a notification")

        # Weekly focus — what this week is actually for. Prioritization is
        # meaningless without it, so Monday's brief asks when it's missing.
        try:
            import focus as fc

            focus_data = fc.current()
            if focus_data:
                sections.append(f"\n## This Week's Focus (week of {focus_data['week_of']})")
                for item in focus_data["items"]:
                    tag = f" [{item['project']}]" if item.get("project") else ""
                    sections.append(f"- {item['text']}{tag}")
                sections.append(
                    "\n_Judge today's priorities against this. Call out anything urgent that doesn't serve it._"
                )
            elif datetime.now(PST).weekday() == 0:
                stale = fc.load()
                since = f" (last set for week of {stale['week_of']})" if stale else ""
                sections.append("\n## Weekly Focus — NOT SET")
                sections.append(
                    f"It's Monday and {owner_name()} hasn't set a focus for this week{since}. "
                    "End the brief by asking him for 1-3 things this week is for. "
                    "He can reply in chat and you'll record it with focus_set."
                )
        except Exception as _fe:
            debug(f"focus context injection failed: {_fe}")

        # Include health report in morning brief
        if health_report and health_report.get("checks"):
            sections.append("\n## System Health")
            for name, result in health_report["checks"].items():
                icon = "OK" if result["status"] == "ok" else "WARN" if result["status"] == "warn" else "CRITICAL"
                sections.append(f"- [{icon}] **{name}**: {result['detail']}")

        # Daily body signal — numbers always, LLM commentary only on triggers
        body_block = None
        if lane_available("body_signal"):
            try:
                scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
                if scripts_dir_str not in sys.path:
                    sys.path.insert(0, scripts_dir_str)
                import body_signal
                body_block = body_signal.build_morning_body_block()
                if body_block:
                    sections.append("\n## Body (yesterday)")
                    sections.append(f"- {body_block['numbers_line']}")
                    sections.append("- Include this Body line verbatim in the morning brief notification.")
                    if body_block.get("commentary_directive"):
                        sections.append("")
                        sections.append(body_block["commentary_directive"])
            except Exception as _e:
                debug(f"body signal injection failed: {_e}")

        # Market + daily lesson — invest-os. Numbers are computed deterministically;
        # only the lesson is written by the model. Mac-only for now (the VPS has no
        # market DB), same platform-gate reasoning as reflection content proposals.
        if sys.platform == "darwin" and lane_available("invest_brief"):
            try:
                scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
                if scripts_dir_str not in sys.path:
                    sys.path.insert(0, scripts_dir_str)
                import invest_brief
                market_block = invest_brief.build_morning_market_block()
                if market_block:
                    sections.append("\n## Market")
                    sections.append(market_block["numbers_line"])
                    sections.append("- Include the market lines verbatim in the morning brief "
                                    "notification. Never add a buy/sell suggestion to them.")
                    if market_block.get("lesson_directive"):
                        sections.append("")
                        sections.append(market_block["lesson_directive"])
                        lesson = market_block["lesson"]
                        # Staged, not advanced: the curriculum only moves once the
                        # brief has actually been delivered (see run_agent).
                        invest_brief.stage_lesson(lesson["number"], lesson["title"])
            except Exception as _e:
                debug(f"market block injection failed: {_e}")

        # Practice nudge — only when body triggers fired AND no practice today AND cooldown clear.
        # Conservative by design: 0–2 nudges/day. The LLM still has final say on whether to emit.
        try:
            if body_block and body_block.get("triggers") and lane_available("practices"):
                from datetime import datetime as _dt
                import practices as _pr
                # Cooldown gate: 4h between practice nudges
                cooldown_ok = True
                if last_practice_offered:
                    try:
                        last = _dt.fromisoformat(last_practice_offered)
                        cooldown_ok = (now_pst - last).total_seconds() >= 4 * 3600
                    except (ValueError, TypeError):
                        cooldown_ok = True
                # Already-done gate: don't nudge if the owner already did one today
                already_logged = _pr.practice_logged_today(VAULT_DIR)

                if cooldown_ok and not already_logged:
                    cal = data.get("gcal") or []
                    candidates = body_signal.infer_current_state(
                        body_block.get("today") or {},
                        body_block.get("baseline") or {},
                        cal,
                        recent_email_count=sum(len(v) for v in (data.get("gmail") or {}).values()),
                    )
                    if candidates:
                        current_state, conf = candidates[0]
                        target_state = body_signal.infer_target_state(now_pst, cal)
                        library = _pr.load_library(VAULT_DIR)
                        recent = _pr.recent_practice_slugs(VAULT_DIR, library, hours=24)
                        matches = _pr.recommend(library, current_state, target_state, recent)
                        if matches:
                            practice = matches[0]
                            title, message = _pr.format_nudge(
                                practice, current_state, target_state, name=owner_name()
                            )
                            sections.append("\n## Practice Recommendation")
                            sections.append(
                                f"Body signals indicate {owner_name()} is **{current_state}** "
                                f"(confidence {conf}). Calendar-derived target for the next 90 min: "
                                f"**{target_state}**."
                            )
                            sections.append(
                                f"Recommended practice: **{practice.name}** ({practice.duration_min} min)."
                            )
                            triggers_summary = "; ".join(body_block["triggers"][:2])
                            sections.append(f"Trigger: {triggers_summary}.")
                            sections.append("")
                            sections.append(
                                "If you decide a nudge is warranted (consider context — "
                                f"is {owner_name()} in a meeting per calendar? has he just woken up?), "
                                "emit EXACTLY ONE line in this format:"
                            )
                            # Escape newlines in message for the NOTIFICATION line format
                            nudge_message = message.replace("\n", "\\n")
                            sections.append(f"NOTIFICATION: {title} | {nudge_message}")
                            sections.append("")
                            sections.append(
                                "Otherwise stay silent on the practice — he trusts that nudges "
                                "only fire when they really matter. Do NOT mention the practice "
                                "in the morning brief if you suppress the nudge."
                            )
        except Exception as _e:
            debug(f"practice recommendation failed: {_e}")

        # Monday-only: weekly task stats (Eisenhower signal)
        if now_pst.weekday() == 0 and lane_available("tasks"):  # 0 = Monday
            try:
                scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
                if scripts_dir_str not in sys.path:
                    sys.path.insert(0, scripts_dir_str)
                import tasks as tq
                stats = tq.weekly_stats(days=7)
                sections.append("\n## Last Week in Tasks")
                sections.append(f"- {stats['summary']}")
                sections.append(f"- Include this one line in the morning brief notification so {owner_name()} sees where his time went.")
            except Exception as _e:
                debug(f"weekly stats injection failed: {_e}")

        # Intro-network pipeline — pending pre-intro threads live outside the
        # inbox (a dedicated label), so nothing else surfaces them. Layer-10
        # module; naming stays generic in shipped prompt text.
        if lane_available("boardy"):
            try:
                scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
                if scripts_dir_str not in sys.path:
                    sys.path.insert(0, scripts_dir_str)
                import boardy

                boardy_summary = boardy.pending_summary()
                boardy_line = boardy.format_line(boardy_summary)
                if boardy_line:
                    sections.append("\n## Intro-Network Pipeline")
                    sections.append(f"- {boardy_line}")
                    for subj in boardy_summary["owed_subjects"][:5]:
                        sections.append(f"  - {subj}")
                    sections.append(
                        "- Include this line in the morning brief notification. These threads "
                        "are labelled outside the inbox, so this is the only "
                        f"place {owner_name()} sees them."
                    )
            except Exception as _e:
                debug(f"boardy summary injection failed: {_e}")

        # Include ALL emails for morning overview
        for account, messages in data["gmail"].items():
            if messages:
                sections.append(f"\n### Inbox: {account} ({len(messages)} unread)")
                for msg in messages:
                    sections.append(f"- **From:** {msg.get('from', 'unknown')}")
                    sections.append(f"  **Subject:** {msg.get('subject', '(no subject)')}")
                    sections.append(f"  **Snippet:** {msg.get('snippet', '')}")

        # Follow-up candidates: threads we sent to with no reply
        try:
            from integrations.gmail import ACCOUNT_USER_IDS
            scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
            if scripts_dir_str not in sys.path:
                sys.path.insert(0, scripts_dir_str)
            import drafts as drafts_mod
            followups: list[dict] = []
            for acct in ACCOUNT_USER_IDS:
                try:
                    followups.extend(drafts_mod.load_waiting_followups(acct, min_days=3))
                except Exception as _e:
                    debug(f"followups failed for {acct}: {_e}")
            if followups:
                sections.append(f"\n### Awaiting Replies ({len(followups)} threads silent ≥3d)")
                for fu in followups[:10]:
                    sections.append(
                        f"- {fu['account']} — {fu['subject']} "
                        f"(silent {fu['days_silent']}d)"
                    )
                sections.append(
                    "Mention these in the morning brief ONLY if any are "
                    "client/business or in opportunities. Skip otherwise."
                )
        except Exception as _e:
            debug(f"followups injection failed: {_e}")

    if afternoon_brief and not morning_brief:
        sections.append("## Afternoon Check-in")
        sections.append(f"This is the first heartbeat at/after 2 PM PST. Generate an afternoon check-in for {owner_name()}:")
        sections.append("- Surface the current task queue (how many proposed, awaiting review)")
        sections.append("- Flag any client/business emails from today that still need a reply")
        sections.append("- Remaining calendar events for the rest of the day")
        sections.append("- Active drafts still pending review")
        sections.append("- Send as exactly one notification")

    # `diff["new_email_count"]`/`["new_event_count"]` are computed from the
    # PRE-gate snapshot diff, but `data` here has already had anything the
    # guardrail dropped removed — rendering the raw diff count above a list
    # built from filtered `data` could say "3 new" over a list of one.
    # Recompute the count from what's actually present in `data` so the
    # header matches what the model (and the owner, in a brief) actually
    # sees (2026-09-20).
    _present_email_ids = {m.get("id") for msgs in data.get("gmail", {}).values() for m in msgs}
    _shown_new_email_ids = [i for i in diff.get("new_email_ids", []) if i in _present_email_ids]
    _present_event_ids = {ev.get("id") for ev in data.get("gcal", [])}
    _shown_new_event_ids = [i for i in diff.get("new_event_ids", []) if i in _present_event_ids]

    if _shown_new_email_ids and not morning_brief:
        # classify_result is only ever non-empty when triage is on (main()
        # leaves it {} when the inbox-mode.json kill switch is off) — this
        # gate doubles as the triage_on check without threading a new arg.
        if classify_result:
            sections.append(f"## New Emails ({len(_shown_new_email_ids)} new since last check)")
            sections.append(_build_email_brief_section(classify_result, data))
        else:
            sections.append(f"## New Emails ({len(_shown_new_email_ids)} new since last check)")
            for account, messages in data["gmail"].items():
                new_msgs = [m for m in messages if m.get("id") in _shown_new_email_ids]
                if new_msgs:
                    sections.append(f"\n### {account}")
                    for msg in new_msgs:
                        sections.extend(render_email_for_prompt(msg))

    # ── Inbox Triage section ────────────────────────────────────────────
    if classify_result:
        triage_lines = _build_triage_section(classify_result, auto_counts or {})
        if triage_lines:
            sections.extend(triage_lines)

    if _shown_new_event_ids:
        sections.append(f"\n## New Calendar Events ({len(_shown_new_event_ids)} new)")
        for event in data["gcal"]:
            if event.get("id") in _shown_new_event_ids:
                start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
                summary = event.get('summary', '(no title)')
                sections.append(f"- **{summary}** at {start}")

    # A meeting that moved or was cancelled is a real change the owner needs
    # to hear about, not silence — diff_snapshots() tracks these separately
    # from "new" (2026-09-20).
    changed_events = diff.get("changed_events") or []
    if changed_events:
        sections.append(f"\n## Changed Calendar Events ({len(changed_events)})")
        for ev in changed_events:
            old_start, new_start = ev.get("old_start", ""), ev.get("new_start", "")
            if old_start and new_start and old_start != new_start:
                sections.append(f"- **{ev.get('summary', '(no title)')}** moved: was {old_start}, now {new_start}")
            else:
                sections.append(f"- **{ev.get('summary', '(no title)')}** changed (now at {new_start})")

    removed_events = diff.get("removed_events") or []
    if removed_events:
        sections.append(f"\n## Cancelled Calendar Events ({len(removed_events)})")
        for ev in removed_events:
            sections.append(f"- **{ev.get('summary', '(no title)')}** was at {ev.get('start', '')} — cancelled")

    if data["gcal"]:
        sections.append("\n## Today's Full Calendar")
        for event in data["gcal"]:
            start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
            summary = event.get('summary', '(no title)')
            sections.append(f"- {summary} — {start}")

    scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
    if scripts_dir_str not in sys.path:
        sys.path.insert(0, scripts_dir_str)
    import drafts as drafts_mod
    active_drafts = drafts_mod.get_active_draft_count()
    sections.append(f"\n## Draft Status\n- Active drafts: {active_drafts}")

    # Task queue status — tell the model what's already proposed so it doesn't re-propose
    try:
        if not lane_available("tasks"):
            raise ModuleNotFoundError("tasks")
        scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
        if scripts_dir_str not in sys.path:
            sys.path.insert(0, scripts_dir_str)
        import tasks as tq
        tq.resurface_parked()
        proposed_tasks = tq.list_by_status("proposed")
        if proposed_tasks:
            sections.append(f"\n## Active Task Queue ({len(proposed_tasks)} proposed, awaiting review)")
            for t in proposed_tasks[:10]:
                proj = t.get("project") or "inbox"
                kind = t.get("task_type", "action")
                kind_tag = "" if kind == "action" else f" [{kind}]"
                sections.append(f"- `{t['id']}` [{proj}]{kind_tag}: {t['content']} (source: {t['source']})")
            sections.append("\n_Do NOT re-propose any of the above (dedup will auto-skip, but don't waste cycles)._")

        # Suggested Today — computed here, not chosen by the model. The pick is
        # deterministic so the same queue and calendar always produce the same
        # day; the brief's job is to present it, not to re-decide it.
        if morning_brief and lane_available("focus"):
            try:
                import focus as fc

                open_minutes = tq.free_minutes(data.get("gcal") or [], now=datetime.now(PST))
                focus_texts = fc.texts()
                picks = tq.pick_today(
                    tq.load_all(), open_minutes=open_minutes, focus_texts=focus_texts
                )
                hours = round(open_minutes / 60, 1)
                if picks:
                    sections.append(f"\n## Suggested Today ({len(picks)} items, ~{hours}h open)")
                    for t in picks:
                        reason = tq.pick_reason(t, focus_texts)
                        dur = t.get("duration_minutes") or tq.MIN_DURATION_MINUTES
                        sections.append(f"- `{t['id']}` {t['content']} [{dur}m · {reason}]")
                    sections.append(
                        "\n_Present this list as today's plan. Do not substitute your own picks or "
                        "reorder it. If something looks wrong, say so in one line after the list._"
                    )
                elif open_minutes < 60:
                    sections.append(
                        f"\n## Suggested Today\nOnly ~{hours}h open — calendar is full. No task plan today."
                    )
            except Exception as _pe:
                debug(f"daily pick failed: {_pe}")

        stale = tq.stale_waiting()
        if stale:
            sections.append(f"\n## Waiting too long ({len(stale)})")
            for t in stale[:5]:
                who = t.get("who") or "someone"
                sections.append(f"- `{t['id']}`: {t['content']} — {who}, no movement since {t.get('updated_at', '?')[:10]}")
            sections.append("\n_Worth a nudge. Mention in the brief; do not re-propose these as new tasks._")
    except Exception as _e:
        debug(f"task queue context injection failed: {_e}")

    # Opportunities — surface stale + due so the model can mention them in the morning brief
    try:
        if not lane_available("opportunities"):
            raise ModuleNotFoundError("opportunities")
        scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
        if scripts_dir_str not in sys.path:
            sys.path.insert(0, scripts_dir_str)
        import opportunities as ops
        open_opps = ops.list_open()
        stale_opps = ops.list_stale()
        due_opps = ops.list_due()
        if open_opps or stale_opps or due_opps:
            sections.append(f"\n## Opportunities ({len(open_opps)} open/active)")
            if due_opps:
                sections.append(f"\n### Due soon ({len(due_opps)})")
                for o in due_opps[:5]:
                    sections.append(f"- `{o.slug}` — {o.name} · next: {o.next_step or '(unset)'} (due {o.next_step_due})")
            if stale_opps:
                sections.append(f"\n### Stale — no touch in 21d+ ({len(stale_opps)})")
                for o in stale_opps[:5]:
                    sections.append(f"- `{o.slug}` — {o.name} · last_touched: {o.last_touched} · next: {o.next_step or '(unset)'}")
            # Always include a compact roster so the model can recognize names from inbound emails/calendars
            other_open = [o for o in open_opps if o not in due_opps and o not in stale_opps]
            if other_open:
                sections.append(f"\n### Other open ({len(other_open)})")
                for o in other_open[:10]:
                    sections.append(f"- `{o.slug}` — {o.name} · {o.stage}")
            sections.append(f"\n_Mention due/stale opportunities in the morning brief if relevant. Do NOT auto-create or modify opportunity files — only {owner_name()} (via chat or CLI) creates/updates them._")
    except Exception as _e:
        debug(f"opportunities context injection failed: {_e}")

    # Captures — what the owner saved since yesterday, so the brief can surface it
    try:
        if not lane_available("captures"):
            raise ModuleNotFoundError("captures")
        scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
        if scripts_dir_str not in sys.path:
            sys.path.insert(0, scripts_dir_str)
        import captures as _captures
        since = (datetime.now(PST) - timedelta(days=1)).isoformat()
        recent_captures = _captures.captured_since(since)
        pending_captures = len(_captures.list_by_status("captured")) + len(
            _captures.list_by_status("needs_browser")
        )
        if recent_captures:
            sections.append(f"\n## Captured in the last 24h ({len(recent_captures)})")
            for c in recent_captures[:10]:
                label = c.get("title") or c.get("url") or (c.get("raw_text") or "")[:60]
                project = f" · {c['project']}" if c.get("project") else ""
                sections.append(f"- {label}{project} → `{c.get('note_path') or '(no note)'}`")
            if pending_captures:
                sections.append(f"\n_{pending_captures} capture(s) still processing._")
            sections.append(
                "\n_In the morning brief, mention captures only if one connects to today's "
                "work or a live opportunity. Do not list them all._"
            )
    except Exception as _e:
        debug(f"captures context injection failed: {_e}")

    # Load known-relevant contacts so the LLM can broaden notify gate
    try:
        if not lane_available("contacts"):
            raise ModuleNotFoundError("contacts")
        scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
        if scripts_dir_str not in sys.path:
            sys.path.insert(0, scripts_dir_str)
        import contacts as contacts_mod
        known_contacts = sorted(contacts_mod.load_known_contacts())
    except Exception as _e:
        debug(f"contacts load failed: {_e}")
        known_contacts = []

    if known_contacts:
        sections.append(f"\n## Known-Relevant Senders ({len(known_contacts)})")
        sections.append(
            f"{owner_name()} has drafted replies to, or has open opportunities/intros "
            "with, these email addresses. Treat incoming emails from any of "
            "these as worth a notification, even if the content isn't strictly "
            "client/business:"
        )
        if len(known_contacts) <= 200:
            sections.append(", ".join(known_contacts))
        else:
            sections.append(", ".join(known_contacts[:200]) + f", … (+{len(known_contacts)-200} more)")

    if not sections:
        debug("no context sections to send — skipping SDK call")
        return []

    # The trust-boundary instruction goes AFTER the external data. Models weigh
    # the most recent instruction most, and the workshop's checklist puts it here
    # for exactly that reason. Skip it when the run carries no external content.
    if any("<external_content" in s for s in sections):
        from sanitize import TRUST_BOUNDARY_INSTRUCTION
        sections.append(TRUST_BOUNDARY_INSTRUCTION)

    prompt = "Here is the current state for this heartbeat run:\n\n" + "\n".join(sections)
    prompt += "\n\n## Instructions\nBased on the above, take appropriate actions:\n"
    prompt += "1. If there are new client/business emails needing replies, create draft files in vault/drafts/active/\n"
    prompt += f"   CRITICAL — meeting/event status: only say a meeting is 'confirmed' when the counterparty has replied agreeing to the time. {owner_name()} sending a proposal or saying 'noon works' is a PROPOSAL, not a confirmation. If no reply from counterparty, say 'awaiting confirmation from <name>' not 'confirmed'.\n"
    prompt += "2. Log to today's daily log ONLY if something notable happened (new client/business email, draft created, insight extracted, error). For routine runs with only noise/newsletter triage, SKIP the daily log entirely or write at most one line like `HH:MM — N triaged (all noise), no action`. Never enumerate individual noise/fyi emails or their reasoning.\n"
    prompt += "3. If there are emails in '### Awaiting LLM Classification', classify each into Action/FYI/Noise with one-line reasoning\n"
    prompt += "4. If there are newsletter bodies in '### Newsletter Insights to Extract', extract key insights and write to vault/insights/newsletters/YYYY-MM-DD_<sender-slug>_<topic>.md\n"
    prompt += "5. NOTIFICATIONS — silence is the default. Emit a NOTIFICATION line ONLY when one of these is true:\n"
    prompt += "   - New client/business email requiring a reply (not newsletters, receipts, automated emails, or notifications from services)\n"
    prompt += f"   - Email from a sender in the 'Known-Relevant Senders' list above, even if not strictly client/business — {owner_name()} has an existing relationship he wants to stay on top of\n"
    prompt += "   - New calendar event added by someone else that conflicts with an existing commitment (GCal native doesn't flag conflicts — this is genuinely additive)\n"
    prompt += "   - DO NOT send 'meeting starts in N minutes' style reminders — Google Calendar's native notifications already handle that. Heartbeat reminders just duplicate them.\n"
    prompt += "   - Morning brief — only if a '## Morning Brief' section appears above (exactly one notification)\n"
    prompt += "   - Afternoon check-in — only if a '## Afternoon Check-in' section appears above (exactly one notification)\n"
    prompt += "   - Scan-rule high-signal finding that was explicitly flagged with notify=true\n"
    prompt += "   - Critical system health issue\n"
    prompt += "   STRICTLY FORBIDDEN — never notify about: inbox triage stats, 'X emails processed/archived/labeled', noise/fyi tier activity, routine confirmations, summaries of what you just did, or 'no action needed'. If nothing above qualifies, emit NO notification lines at all.\n"
    prompt += "   Format when you do notify: `NOTIFICATION: <title> | <message>`\n"
    prompt += "   The whole notification MUST be on a single line. Use the literal two-character sequence `\\n` inside <message> wherever you want a line break — the parser expands it to a real newline before sending.\n"
    prompt += "   For morning/afternoon briefs, structure <message> as sections. Each section starts with an emoji + one-word label, followed by `\\n` and then 1+ bullet lines (use `• ` at the start of each bullet). Separate sections with `\\n\\n`.\n"
    prompt += "   Section guidance:\n"
    prompt += "   - 📅 Calendar — bullet each meeting/block with its time. Skip if day is empty.\n"
    prompt += "   - 📬 Inbox — bullet each *actionable* email (client, business, requires reply). One line per email: sender — subject/summary (draft: yes/no). Skip newsletters/noise. If nothing actionable, write `All triaged, no action needed`.\n"
    prompt += "   - 🎯 Today — ONLY if a '## Suggested Today' section appears above. Bullet each suggested task verbatim in the given order, with its minutes and reason, e.g. `• Reply to the vendor re: renewal — 15m (overdue)`. Do not add, drop, or reorder items. Put this section FIRST, before Calendar. If a '## This Week's Focus' section appears above, add one line under the bullets naming the focus.\n"
    prompt += "   - ✅ Tasks — bullet each proposed task awaiting review that is NOT already in the Today section (include short content, drop the id). Omit this section entirely if queue is empty.\n"
    prompt += "   - ⏳ Waiting — ONLY if a '## Waiting too long' section appears above. Bullet each: who, what, how long silent. Omit otherwise.\n"
    prompt += "   - ✏️ Drafts — bullet each active draft (recipient — subject). Omit if zero drafts.\n"
    prompt += "   - 🔋 Health — keep terse: `All systems green` when OK, else bullet the specific warnings.\n"
    prompt += "   Example of a full brief:\n"
    prompt += "   `NOTIFICATION: Friday Morning Brief | 🎯 Today\\n• Reply to the contractor re: renewal — 15m (overdue)\\n• Triage Issue #36 — 30m (Q1)\\n• Review the vendor's draft — 30m (focus)\\nFocus: ship phase 2\\n\\n📅 Calendar\\n• Decompress 10:30a\\n• Dentist 12:15p\\n\\n📬 Inbox\\n• The vendor — Sat lunch invite downtown (draft: yes)\\n\\n⏳ Waiting\\n• The contractor — contract review, 7d silent\\n\\n✏️ Drafts\\n• The vendor — re: lunch invite\\n\\n🔋 Health\\nAll systems green`\n"
    prompt += "   Keep bullets to one short line each (no sub-bullets, no long paragraphs). Never use real line breaks inside the NOTIFICATION line; always use `\\n`."

    debug(f"sending {len(prompt)} chars to SDK")

    from sdk_stderr import make_stderr_capture
    import sdk_cli

    # The SDK prefers its own bundled CLI (2.1.92 in claude-agent-sdk 0.1.56) over
    # anything installed. Every bodyless-400 in the 2026-08-15 transcripts came from
    # that binary; none from a newer one. Run the newest CLI on the machine, and
    # retry the rejection anyway — it is intermittent, and a silent miss costs a brief.
    async def _attempt():
        sdk_stderr_cb, get_sdk_stderr_tail = make_stderr_capture(context="heartbeat.main")
        texts: list[str] = []
        opts = sdk_cli.apply_cli_path(dict(
            system_prompt=system_prompt,
            allowed_tools=["Write", "Edit", "Read"],
            # allowed_tools only pre-approves Write/Edit/Read — it does not remove
            # anything else the SDK starts with. This agent reads raw, unsanitized-at-
            # this-layer email/calendar text every run, so Bash (and every other tool
            # the prompt never asks for) must not exist for it to reach, not just be
            # unapproved. `tools=` sets the whole built-in surface; `disallowed_tools=`
            # is belt-and-braces on top of that (2026-09-20).
            tools=["Write", "Edit", "Read"],
            disallowed_tools=["Bash", "WebFetch", "WebSearch", "NotebookEdit", "Task", "Agent"],
            max_turns=30,
            cwd=str(PROJECT_ROOT),
            stderr=sdk_stderr_cb,
        ))
        async for msg in query(prompt=prompt, options=ClaudeAgentOptions(**opts)):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    tail = " | ".join(get_sdk_stderr_tail()[-5:]) or "(no stderr)"
                    import agent_result

                    reason, is_auth = agent_result.describe_failure(
                        msg, "\n".join(texts), tail
                    )
                    return False, str(getattr(msg, "result", "")), (reason, is_auth)
        return True, "", texts

    ok, result_text, payload = await sdk_cli.run_with_retry(_attempt, log=debug)

    if not ok:
        reason, is_auth = payload if payload else ("unknown failure", False)
        log_error(f"Agent SDK error: {reason}")
        if is_auth:
            import agent_result
            agent_result.alert_auth_failure(f"heartbeat: {reason}")
        try:
            import invest_brief
            invest_brief.drop_pending_lesson()
        except Exception as _e:
            debug(f"could not roll back staged lesson: {_e}")
        return []

    response_text = payload or []

    try:
        import invest_brief
        delivered = invest_brief.commit_pending_lesson()
        if delivered:
            debug(f"invest: lesson {delivered['number']} delivered and recorded")
    except Exception as _e:
        debug(f"could not commit staged lesson: {_e}")

    return response_text


GUARDRAIL_MAX_CHARS = 20_000


def external_text_for_guardrail(data: dict) -> str:
    """Digest of ONLY the external fields for the guardrail screener.

    Our own prompt is full of instruction-shaped text by design, so screening
    it would trip the guardrail every run. Screen what came from outside.
    """
    parts: list[str] = []
    for account, msgs in (data.get("gmail") or {}).items():
        for m in msgs or []:
            block = "\n".join(
                f"{k}: {m.get(k)}" for k in ("from", "subject", "snippet", "body") if m.get(k)
            )
            if block:
                parts.append(f"[email {account}]\n{block}")
    for ev in data.get("gcal") or []:
        fields = []
        for k in ("summary", "description", "location"):
            if ev.get(k):
                fields.append(f"{k}: {ev[k]}")
        org = (ev.get("organizer") or {}).get("displayName") if isinstance(ev.get("organizer"), dict) else None
        if org:
            fields.append(f"organizer: {org}")
        if fields:
            parts.append("[calendar]\n" + "\n".join(fields))
    text = "\n\n".join(parts)
    return text[:GUARDRAIL_MAX_CHARS]


def _guardrail_digest_truncated(data: dict) -> bool:
    """True when external_text_for_guardrail() would cut content off.

    A silent cut means whatever came after GUARDRAIL_MAX_CHARS is never
    screened at all — worse than screening it and getting a wrong verdict,
    because nothing says it happened. Made visible so run_guardrail_gate can
    say so instead of passing on unscreened content (2026-09-20).
    """
    parts: list[str] = []
    for account, msgs in (data.get("gmail") or {}).items():
        for m in msgs or []:
            block = "\n".join(
                f"{k}: {m.get(k)}" for k in ("from", "subject", "snippet", "body") if m.get(k)
            )
            if block:
                parts.append(f"[email {account}]\n{block}")
    for ev in data.get("gcal") or []:
        fields = []
        for k in ("summary", "description", "location"):
            if ev.get(k):
                fields.append(f"{k}: {ev[k]}")
        org = (ev.get("organizer") or {}).get("displayName") if isinstance(ev.get("organizer"), dict) else None
        if org:
            fields.append(f"organizer: {org}")
        if fields:
            parts.append("[calendar]\n" + "\n".join(fields))
    return len("\n\n".join(parts)) > GUARDRAIL_MAX_CHARS


def _append_daily_log(title: str, body: str) -> None:
    """Append `## HH:MM — <title>` + body to today's daily log, under the shared lock.

    Every daily-log writer goes through shared.append_daily_log. This one used
    to lock the file itself while the flush and the chat bot locked a sidecar,
    and two different locks do not keep each other out (2026-09-20).
    """
    from shared import append_daily_log

    now = datetime.now(PST)
    path = VAULT_DIR / "daily" / f"{now.strftime('%Y-%m-%d')}.md"
    append_daily_log(path, f"\n## {now.strftime('%H:%M')} — {title}\n\n{body.rstrip()}\n")


def run_guardrail_gate(data: dict, brief_pending: bool = False) -> dict:
    """Screen external content before the main agent runs.

    Returns the guardrail dict plus `warning` (str | None). On `fail` it has
    already logged and notified; the caller only has to skip the run.

    `brief_pending` (True when this tick claimed today's morning or afternoon
    brief) only changes the notification copy — the caller is responsible for
    actually rolling back the claim (see `_rollback_brief_claims` and
    `brief_coord.release_brief`), since this function has no access to the
    snapshot state needed to do that.

    A `reason` starting with "guardrail error" or "unparseable" means the
    provider itself failed (timeout, bad response, unparseable output) rather
    than the model judging the content suspicious. That distinction matters
    to feature-health alerting, so this function raises RuntimeError in that
    case instead of returning normally — `heartbeat.main()`'s
    `feature_run("guardrail.precheck", ...)` call catches it, counts it as a
    consecutive failure in feature-health.json (alerting/auto-disable), and
    substitutes its own `default=` suspicious result. A genuine model-judged
    "suspicious" verdict must never raise.
    """
    context = external_text_for_guardrail(data)
    if not context.strip():
        return {"verdict": "pass", "reason": "no external content", "provider": "none", "flags": [], "warning": None}
    truncated = _guardrail_digest_truncated(data)
    result = run_guardrail_check(context)
    flags = ", ".join(result.get("flags") or []) or "none"
    result["warning"] = None
    reason = result.get("reason") or ""
    if truncated and result["verdict"] == "pass":
        # A cap that silently drops the tail is worse than a wrong verdict on
        # it — content past GUARDRAIL_MAX_CHARS was never screened. Never
        # report a bare pass over content we didn't fully look at.
        result["verdict"] = "suspicious"
        reason = (reason + " " if reason else "") + "(external content exceeded the guardrail cap — some of it was never screened)"
        result["reason"] = reason
    if result["verdict"] == "fail":
        msg = f"{reason or 'no reason given'} (flags: {flags}; provider: {result.get('provider')})"
        if brief_pending:
            msg += " Today's brief will retry on the next tick once the flagged content is gone."
        log_error(f"guardrail FAIL — heartbeat run skipped: {msg}")
        _append_daily_log("Guardrail", f"Heartbeat run skipped. {msg}")
        notify("Heartbeat skipped: guardrail", msg, parse_mode=None)
    elif result["verdict"] == "suspicious":
        result["warning"] = f"{reason or 'no reason given'} (flags: {flags})"
        debug(f"guardrail SUSPICIOUS: {result['warning']}")
        if reason.startswith("guardrail error") or reason.startswith("unparseable"):
            # Provider failure, not a model judgment — raise so feature_run
            # counts this toward feature-health.json's consecutive-failure
            # alerting instead of quietly returning its "suspicious" default.
            raise RuntimeError(f"guardrail degraded: {reason}")
    else:
        debug("guardrail pass")
    return result


def _rollback_brief_claims(curr_snapshot: dict, prev_snapshot: dict, morning: bool, afternoon: bool) -> None:
    """Undo the brief-claim bookkeeping `main()` stamped onto `curr_snapshot`
    before the guardrail gate ran, when the gate came back `fail`.

    `morning`/`afternoon` are `morning_brief_today`/`afternoon_brief_today` —
    True only when *this* tick claimed that brief. Restores the snapshot's
    date field to whatever `prev_snapshot` had (or removes it if `prev_snapshot`
    had none), so a skipped run doesn't leave today's brief marked claimed
    with nothing actually sent. Mutates `curr_snapshot` in place.
    """
    if morning:
        if prev_snapshot.get("last_morning_brief_date"):
            curr_snapshot["last_morning_brief_date"] = prev_snapshot["last_morning_brief_date"]
        else:
            curr_snapshot.pop("last_morning_brief_date", None)
    if afternoon:
        if prev_snapshot.get("last_afternoon_brief_date"):
            curr_snapshot["last_afternoon_brief_date"] = prev_snapshot["last_afternoon_brief_date"]
        else:
            curr_snapshot.pop("last_afternoon_brief_date", None)


_EXTERNAL_CONTENT_RE = re.compile(
    r'<external_content source="[^"]*">\n?(.*?)\n?</external_content>', re.DOTALL
)


def _unwrap_external_content(text: str) -> str:
    """Strip the `<external_content source="...">` trust-boundary wrapper.

    Fields from data["gmail"] are sanitized at the source (gmail.py) and
    always carry this wrapper (sanitize.wrap_trust_boundary), even when the
    text has nothing suspicious in it. `_build_triage_section` renders that
    wrapper verbatim, which is fine for the model — it's an explicit trust
    signal, not noise. The morning-brief block is meant to read like a short
    human summary, so it unwraps here and keeps the inner text only.
    """
    if not text:
        return text
    return _EXTERNAL_CONTENT_RE.sub(lambda m: m.group(1), text)


def _build_email_brief_section(classify_result: dict, data: dict) -> str:
    """Build the morning-brief email block: Needs you / Unsure / Filed.

    Replaces the raw `## New Emails` dump when triage is on. `classify_result`
    only covers emails classify_inbox actually touched this tick — explicit
    action-tier mail is collected under classify_result["action"] (added
    alongside auto/supervised/unmatched) since classify_inbox otherwise passes
    on it entirely.
    """
    lines: list[str] = []

    action_items = []
    for account, items in (classify_result.get("action") or {}).items():
        for msg, cls in items:
            action_items.append((account, msg, cls))
    # Urgent first — ClassificationResult.urgent is the LLM-set flag, not a
    # substring guess against rule_match (rule-based rows never carry the
    # word "urgent" there, so that check never actually fired).
    action_items.sort(key=lambda t: 0 if getattr(t[2], "urgent", False) else 1)

    lines.append("### Needs you")
    if action_items:
        for account, msg, cls in action_items:
            sender = _unwrap_external_content(msg.get("from", "unknown"))
            subject = _unwrap_external_content(msg.get("subject", "(no subject)"))
            lines.append(f"- **{sender}** — \"{subject}\" ({account})")
    else:
        lines.append("- Nothing flagged as explicit action this tick.")

    lines.append("\n### Unsure")
    unmatched_items = []
    for account, msgs in (classify_result.get("unmatched") or {}).items():
        for msg in msgs:
            unmatched_items.append((account, msg))
    if unmatched_items:
        lines.append(f"surface these to {owner_name()}; classification is recorded separately, never acted on.")
        for account, msg in unmatched_items:
            sender = _unwrap_external_content(msg.get("from", "unknown"))
            subject = _unwrap_external_content(msg.get("subject", "(no subject)"))
            lines.append(f"- {sender} — {subject} ({account})")
    else:
        lines.append("- Nothing unmatched this tick.")

    lines.append("\n### Filed")
    auto = classify_result.get("auto") or {}
    tier_counts: dict[str, int] = {}
    for account, items in auto.items():
        for msg, cls in items:
            tier = getattr(cls, "tier", "unknown")
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
    if tier_counts:
        tally = ", ".join(f"{count} {tier}" for tier, count in sorted(tier_counts.items()))
        lines.append(f"- Auto-filed: {tally}")
    else:
        lines.append("- Nothing auto-filed this tick.")

    return "\n".join(lines)


def _build_triage_section(classify_result: dict, auto_counts: dict) -> list[str]:
    """Build the ## Inbox Triage Results section for the Agent SDK prompt."""
    if not lane_available("inbox"):
        return ["\n## Inbox Triage Results", "inbox triage module not installed"]
    lines = []
    total_auto = sum(auto_counts.values())
    supervised_count = sum(len(v) for v in classify_result.get("supervised", {}).values())
    unmatched_count = sum(len(v) for v in classify_result.get("unmatched", {}).values())
    insight_count = sum(len(v) for v in classify_result.get("insight_emails", {}).values())

    if total_auto == 0 and supervised_count == 0 and unmatched_count == 0 and insight_count == 0:
        return []

    lines.append("\n## Inbox Triage Results")

    _AUTO_TIER_DESC = {
        "noise": "labeled + archived + marked read",
        "fyi": "labeled",
        "trash": "trashed",
    }

    if total_auto > 0:
        lines.append(f"\n### Auto-processed ({total_auto} emails)")
        for tier, count in auto_counts.items():
            if count:
                desc = _AUTO_TIER_DESC.get(tier, "processed")
                lines.append(f"- {count} {tier} email(s) {desc}")

    if unmatched_count > 0:
        lines.append(f"\n### Awaiting LLM Classification ({unmatched_count} emails)")
        lines.append("For each email, output a `CLASSIFY: <id> | <tier> | <account>` line (one per email, tier = action/fyi/noise only — never trash or unsure).")
        for account, msgs in classify_result.get("unmatched", {}).items():
            for msg in msgs:
                lines.append(f"- **From:** {msg.get('from', 'unknown')} | **Subject:** {msg.get('subject', '(no subject)')} | **Snippet:** {msg.get('snippet', '')} | **ID:** {msg.get('id', '')} | **Account:** {account}")

    if supervised_count > 0:
        lines.append(f"\n### Supervised Queue ({supervised_count} proposals pending human approval)")
        for account, items in classify_result.get("supervised", {}).items():
            for msg, cls in items:
                lines.append(f"- **{msg.get('from', 'unknown')}** — \"{msg.get('subject', '')}\" → Proposed: {cls.tier} (rule: {cls.rule_match})")

    if insight_count > 0:
        lines.append(f"\n### Newsletter Insights to Extract ({insight_count} emails)")
        for account, items in classify_result.get("insight_emails", {}).items():
            for msg, topic in items:
                lines.append(f"\n**Source:** {msg.get('from', 'unknown')} | **Subject:** {msg.get('subject', '')} | **Topic:** {topic}")
                # Include full body if available
                body = msg.get("body", "")
                if body:
                    lines.append(f"\n```\n{body[:3000]}\n```")
                else:
                    lines.append("_(body not fetched — use gmail_read to fetch full content)_")

    return lines


# ── Notification Parsing & Dispatch ────────────────────────────────────


# The model has to emit this line verbatim for a brief to reach the owner, and
# formatting is exactly what a model varies between runs. On 2026-08-16 it wrote
# `**NOTIFICATION: Sunday Morning Brief | ...**` — correct content, markdown
# bold around it — and the old anchored pattern dropped the whole brief without
# logging anything. Leading decoration (bold, italic, bullet, quote, heading,
# backtick) is tolerated; a pipe separator is still required, and the line must
# still START with the marker so prose mentioning the word is not mistaken for
# one. See tests/test_notification_parsing.py.
NOTIFICATION_RE = r"^[\s*_`#-]*NOTIFICATION:\s*(.+?)\s*\|\s*(.+)"

# Trailing decoration from the same wrapper, stripped after the split so a
# bolded line does not end the message with stray asterisks.
_NOTIFICATION_TRAILING = "*_`"


def _clean_notification(title: str, message: str) -> tuple[str, str]:
    """Normalise a parsed NOTIFICATION line into (title, message)."""
    title = title.strip().strip(_NOTIFICATION_TRAILING).strip()
    message = message.strip()
    # Expand escaped newlines first, or the closing wrapper hides behind one.
    message = message.replace("\\n", "\n")
    message = message.rstrip().rstrip(_NOTIFICATION_TRAILING).rstrip()
    return title, message


def parse_and_send_notifications(response_texts: list[str]) -> dict:
    """Parse NOTIFICATION lines from SDK response and dispatch.

    Two-layer dedup in git-tracked brief-coordination.json:
    - Scheduled briefs: keyed 'morning_brief:YYYY-MM-DD' (date, not content) so
      Mac + VPS agree even when brief content differs slightly.
    - Everything else: keyed 'content:<sha256[:16]>' with 50-min TTL.

    Returns `{"brief_send_failed": "morning"|"afternoon"|None}` (2026-09-20):
    the morning/afternoon brief is delivered as exactly one NOTIFICATION line
    through this function. `main()` claims the brief slot (`claim_brief()`)
    and stamps the last-sent date BEFORE the model even runs, since it has to
    know whether to ask for one — but a model-OK tick whose actual
    Telegram/macOS send then fails would otherwise leave that claim and stamp
    in place with nothing ever delivered, losing the brief for the rest of
    the day. The caller uses this to release the claim and roll the stamp
    back so the next tick retries.
    """
    import hashlib
    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
    from notify import notify, delivered as _delivered
    from brief_coord import claim_notification, was_notification_claimed

    today_str = datetime.now(PST).strftime("%Y-%m-%d")
    _BRIEF_KEYWORDS = ("Morning Brief", "Afternoon Check-in", "Afternoon Check-In")
    brief_send_failed: str | None = None

    for text in response_texts:
        for line in text.split("\n"):
            match = re.match(NOTIFICATION_RE, line)
            if not match:
                continue
            title, message = _clean_notification(match.group(1), match.group(2))

            is_brief = any(kw in title for kw in _BRIEF_KEYWORDS)
            if is_brief:
                brief_kind = "morning" if "Morning" in title else "afternoon"
                kind = "morning_brief" if brief_kind == "morning" else "afternoon_checkin"
                dedup_key = f"{kind}:{today_str}"
            else:
                brief_kind = None
                h = hashlib.sha256(f"{title}|{message}".encode()).hexdigest()[:16]
                dedup_key = f"content:{h}"

            # Send first, claim only on success (2026-09-20) — claim_notification
            # used to be written BEFORE notify() ran and its result was never
            # checked, so a failed send was still logged and remembered as
            # "sent" and never retried. was_notification_claimed() is a
            # non-claiming peek that still stops a genuine duplicate (this
            # text parsed twice, or the other machine having just sent it).
            if was_notification_claimed(dedup_key, ttl_minutes=50):
                debug(f"notification deduped: {title}")
                continue

            result = notify(title, message)
            if _delivered(result):
                claim_notification(dedup_key, ttl_minutes=50)
                debug(f"sent notification: {title}")
            else:
                debug(f"notification send failed, not claimed, retried next tick: {title}")
                if brief_kind:
                    brief_send_failed = brief_kind

    return {"brief_send_failed": brief_send_failed}


def parse_and_apply_classifications(response_texts: list[str], classify_result: dict):
    """Parse CLASSIFY lines from SDK response and record them — RECORD ONLY.

    Format expected from agent: CLASSIFY: <msg_id> | <tier> | <account>
    Tier must be one of: action, fyi, noise

    This path never touches Gmail. The model's verdict feeds the reputation
    learning stream (upsert source="llm") and the audit trail (record_action,
    action="record_only") so a human can review it later — it never labels,
    archives, or marks anything read/unread. The unsure pile is surfaced by
    the brief and by supervised-queue proposals; only a human approval in
    vault/inbox-review.md (via execute_auto_actions/process_review_file)
    ever mutates Gmail.
    """
    if not lane_available("inbox"):
        debug("optional module inbox not present — parse_and_apply_classifications skipped")
        return
    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
    from inbox.classifier import record_action

    # Build a lookup of unmatched msg → msg metadata for record_action
    unmatched_by_id: dict[str, tuple[str, dict]] = {}
    for account, msgs in classify_result.get("unmatched", {}).items():
        for msg in msgs:
            unmatched_by_id[msg.get("id", "")] = (account, msg)

    valid_tiers = {"action", "fyi", "noise"}
    recorded = 0

    for text in response_texts:
        for line in text.split("\n"):
            match = re.match(r"CLASSIFY:\s*(\S+)\s*\|\s*(\w+)\s*\|\s*(\w+)", line.strip())
            if not match:
                continue
            msg_id, tier, account = match.group(1).strip(), match.group(2).strip().lower(), match.group(3).strip()
            if tier not in valid_tiers:
                debug(f"CLASSIFY: invalid tier '{tier}' for {msg_id} — skipping")
                continue

            if msg_id not in unmatched_by_id:
                debug(f"CLASSIFY: {msg_id} not in unmatched set — skipping")
                continue
            _, msg_meta = unmatched_by_id[msg_id]

            record_action(account=account, msg=msg_meta, tier=tier, action="record_only",
                          labels_added=[], labels_removed=[], mode_str="llm", dry_run=False,
                          classification="llm", rule_match="llm_unmatched")

            try:
                from inbox.reputation import upsert as _rep_upsert
                from inbox.classifier import _extract_email
                raw_from = msg_meta.get("from", "")
                sender_email = _extract_email(raw_from)
                if "@" in sender_email:
                    _rep_upsert(tier=tier, source="llm", account=account, email=sender_email)
                    debug(f"rep: stored LLM result {sender_email} → {tier}")
            except Exception as _re:
                debug(f"rep: failed to store LLM result: {_re}")

            debug(f"CLASSIFY: {msg_id} → {tier} (recorded only, no Gmail mutation)")
            recorded += 1

    if recorded:
        debug(f"LLM classifications recorded: {recorded} emails (record-only, no Gmail mutations)")


# ── Main Entry Point ──────────────────────────────────────────────────


def _refresh_dashboard_snapshot(events=None):
    """Refresh the Obsidian command center snapshot.

    Called from exactly one of two sites per tick: the quiet-tick early
    return, or the end of main() on ticks that ran the SDK. Never both —
    calendar_panel() makes real Google Calendar and Garmin network calls,
    and doubling those per active tick buys nothing.

    `events` should be the tick's already-fetched data["gcal"] when the
    caller has it, so the snapshot doesn't hit Google Calendar a second time
    in the same tick. Falls back to a fresh fetch when omitted.
    """
    if not lane_available("dashboard_snapshot"):
        return
    from feature_run import feature_run

    def _dashboard_snapshot_fn():
        from dashboard_snapshot import write_snapshot
        write_snapshot(events=events)
        debug("dashboard snapshot written")
    feature_run("dashboard.snapshot", _dashboard_snapshot_fn, log=debug)


def _run_capture_features():
    """Drain the capture queue. Runs on every tick, including outside active hours.

    Capture is silent work — it writes vault notes and notifies nobody — so a
    link dropped at midnight has no reason to wait until 6 AM. Each feature
    returns immediately when its queue is empty, so a quiet tick costs one
    JSON read.

    Lane split keeps the two machines out of each other's way: the VPS owns the
    Gmail poll (single writer), the Mac owns the browser lane (only it has the
    logged-in session), and both may process the ordinary queue.
    """
    # Set before any SDK call so hooks know this is a background agent and
    # don't recurse (see CLAUDE.md: CLAUDE_INVOKED_BY).
    os.environ["CLAUDE_INVOKED_BY"] = "heartbeat"

    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
    from feature_run import feature_run

    is_mac = platform.system() == "Darwin"

    def _poll_capture_email():
        if not lane_available("capture_email"):
            return
        if is_mac:
            return  # VPS is the single writer for the Gmail lane
        from capture_email import poll
        result = poll()
        if result["queued"]:
            debug(f"capture email poll: queued {result['queued']}")
        for err in result["errors"]:
            debug(f"capture email poll error: {err}")
    feature_run("capture.email_poll", _poll_capture_email, log=debug)

    def _process_captures():
        # VPS-only. captures.json is git-synced, so two machines draining it
        # both claim the same item, both write a note, and whichever syncs last
        # overwrites the other's result. On 2026-08-16 that produced a
        # duplicate '-2' note and a successful Mac summary clobbered by the
        # VPS's failed one. The VPS owns this lane because captures arrive
        # there (chat server) and it polls Gmail; the Mac owns only the browser
        # lane, which needs a logged-in session the VPS does not have.
        if not lane_available("capture_pipeline"):
            return
        if is_mac:
            return
        from capture_pipeline import drain
        result = drain()
        if any(result.values()):
            debug(f"capture process: {result}")
    feature_run("capture.process", _process_captures, log=debug)

    def _drain_capture_browser():
        if not lane_available("capture_browser"):
            return
        if not is_mac:
            return  # only the Mac has the logged-in browser profile
        from capture_browser import drain as browser_drain
        result = browser_drain()
        if any(result.values()):
            debug(f"capture browser: {result}")
    feature_run("capture.browser_drain", _drain_capture_browser, log=debug)


HEALTH_ALERT_COOLDOWN_HOURS = 8


def _alert_key(alert: str) -> str:
    """The check name, so re-wording a message doesn't re-alert.

    Alerts arrive as "check_name: detail". The detail moves around (a token
    error quotes the provider's text, freshness quotes an age), so keying on the
    whole string means a cosmetic change reads as a brand-new problem.
    """
    return alert.split(":", 1)[0].strip() or alert.strip()


def _notify_new_health_alerts(alerts: list[str]) -> None:
    """Notify only about alerts not already sent, and only after a cooldown.

    The old dedup compared the whole alert set joined into one string, so an
    unrelated check appearing or clearing rewrote it and re-sent everything.
    On 2026-08-16 a single expired token was delivered four times in three
    hours because heartbeat_freshness kept joining and leaving that string.
    Per-check state fixes the flapping; the cooldown means a genuinely stuck
    problem still resurfaces, rather than going quiet forever after one ping.
    """
    from notify import notify

    now = datetime.now(PST)
    try:
        state = json.loads(HEALTH_ALERT_STATE_PATH.read_text())
        sent = state.get("sent", {})
    except (OSError, json.JSONDecodeError):
        sent = {}

    due = []
    for alert in sorted(alerts):
        key = _alert_key(alert)
        last = sent.get(key)
        if last:
            try:
                if (now - datetime.fromisoformat(last)) < timedelta(hours=HEALTH_ALERT_COOLDOWN_HOURS):
                    continue
            except ValueError:
                pass  # unparseable stamp — treat as never sent
        due.append(alert)

    # Drop keys that have cleared, so a problem that returns later alerts again
    # rather than sitting silent under a stale cooldown.
    current = {_alert_key(a) for a in alerts}
    sent = {k: v for k, v in sent.items() if k in current}

    if not due:
        debug(f"critical health ({len(alerts)} alert(s)) — all within cooldown, suppressing")
    else:
        # Cooldown is stamped only on a successful send (2026-09-20) — it used
        # to be stamped up front, so a failed send still silenced the alert
        # for HEALTH_ALERT_COOLDOWN_HOURS with nobody ever told.
        # parse_mode=None: identifiers like CLAUDE_CODE_OAUTH_TOKEN must stay
        # copy-pasteable, and Markdown renders the underscores as italics.
        result = notify("CRITICAL: Cortex Health", "; ".join(due), parse_mode=None)
        if _delivered(result):
            for alert in due:
                sent[_alert_key(alert)] = now.isoformat()
            debug(f"sent critical health alert for {len(due)} new/expired alert(s)")
        else:
            debug(f"critical health alert send failed for {len(due)} alert(s) — not stamped, retried next tick")

    try:
        from shared import save_state

        HEALTH_ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_state(str(HEALTH_ALERT_STATE_PATH), {"sent": sent})
    except Exception as e:
        debug(f"could not persist health alert state: {e}")


def _should_run_full_tick(now: datetime) -> bool:
    """Every 30 min inside work hours, hourly outside them.

    The launchd job fires every 1800s from whenever it was loaded, so ticks do
    not land on clock boundaries — throttling on minute-of-hour would drift and
    silently skip whole stretches. Elapsed time since the last full run is the
    only stable gate.

    Only the expensive per-tick work is throttled. Captures already ran above
    (they cost nothing when the queue is empty) and reminders stay on the
    30-minute cadence via the normal path when this returns True.
    """
    if WORK_HOURS[0] <= now.hour < WORK_HOURS[1]:
        return True

    try:
        state = json.loads(THROTTLE_STATE_PATH.read_text())
        last = datetime.fromisoformat(state["last_full_run_at"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return True  # no usable record — run, and start recording

    return (now - last) >= timedelta(minutes=OFF_HOURS_GAP_MINUTES)


def _record_full_tick(now: datetime) -> None:
    """Stamp the run before the work, so a crash can't cause a retry storm.

    Kept in its own file rather than heartbeat-state.json: that snapshot is
    rewritten wholesale later in the run, which would drop a key written here.
    """
    try:
        from shared import save_state

        THROTTLE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_state(str(THROTTLE_STATE_PATH), {"last_full_run_at": now.isoformat()})
    except Exception as e:
        debug(f"could not record throttle timestamp: {e}")


def main():
    debug("heartbeat started")

    _run_capture_features()

    # Active hours check (6 AM – 10 PM PST)
    now = datetime.now(PST)
    if not (ACTIVE_HOURS[0] <= now.hour < ACTIVE_HOURS[1]):
        debug(f"outside active hours ({now.hour}h PST) — skipping")
        return

    if not _should_run_full_tick(now):
        debug(f"outside work hours ({now.hour}h PST) — hourly cadence, skipping this tick")
        return
    _record_full_tick(now)

    os.environ["CLAUDE_INVOKED_BY"] = "heartbeat"

    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
    from feature_run import feature_run

    # 0a. Fire due reminders (runs every heartbeat — independent of email/calendar state)
    # The lane_available() check lives INSIDE the wrapped function, not as a bare
    # `if` guarding the feature_run call: optional_import (which lane_available
    # calls) re-raises when the absence is a broken *third-party* import inside
    # an otherwise-present module, and an unguarded call at this level would
    # crash the whole tick before feature_run's own try/except ever runs.
    def _fire_due_reminders():
        if not lane_available("reminders"):
            return
        from reminders import check_due
        from notify import notify as _notify_reminder
        due_reminders = check_due()
        for r in due_reminders:
            _notify_reminder("Reminder", r.get("text", ""))
            debug(f"fired reminder {r.get('id')}: {r.get('text', '')[:60]}")
    feature_run("reminders.fire_due", _fire_due_reminders, log=debug)

    # 0a2. invest-os — market rule alerts and the Sunday review nudge.
    # Runs every tick but stays silent unless a rule the owner set actually tripped;
    # state-diffed per day so nothing repeats. Mac-only (the market DB is local).
    def _invest_alerts():
        if not (sys.platform == "darwin" and lane_available("invest_brief")):
            return {"alerts": 0}
        import invest_brief
        from notify import notify as _notify
        snap = invest_brief.refresh_snapshot()
        fresh = invest_brief.new_alerts(snap)
        if not fresh:
            debug("invest: nothing tripped")
            return {"alerts": 0}
        _notify("Market alert", "\n".join(fresh) +
                "\n(rules you set — analysis, not advice)")
        debug(f"invest: announced {len(fresh)} alert(s)")
        return {"alerts": len(fresh)}
    feature_run("invest.alerts", _invest_alerts, log=debug, default={})

    def _invest_weekly():
        if not (sys.platform == "darwin" and lane_available("invest_brief")):
            return {"nudged": False}
        import invest_brief
        from notify import notify as _notify
        if not invest_brief.weekly_review_due():
            return {"nudged": False}
        block = invest_brief.build_weekly_block()
        _notify("Sunday investing review", block["message"])
        invest_brief.mark_weekly_nudged()
        debug("invest: sent Sunday review nudge")
        return {"nudged": True}
    feature_run("invest.weekly", _invest_weekly, log=debug, default={})

    # 0b. Health checks (before anything else)
    def _run_health_checks():
        from health_check import run_all_checks
        morning_check = is_first_run_of_day()
        report = run_all_checks(morning=morning_check)
        debug(f"health check: status={report['status']}, alerts={report['alerts']}")

        if report["status"] == "critical":
            _notify_new_health_alerts(report["alerts"])
        return report
    health_report = feature_run("health_checks", _run_health_checks, log=debug)

    try:
        # 1. Gather data
        data = gather_data()
        debug(f"gathered: {len(data['gmail'])} gmail accounts, {len(data['gcal'])} events")

        # 2. Reconcile active drafts — detect Gmail replies, expire if >24h
        scripts_dir_str = str(PROJECT_ROOT / ".claude" / "scripts")
        if scripts_dir_str not in sys.path:
            sys.path.insert(0, scripts_dir_str)
        import drafts as drafts_mod
        try:
            reconcile_result = drafts_mod.reconcile_active_drafts()
        except drafts_mod.ToolNotFoundError as _e:
            # Dead Composio slug — loud in the error log, but not fatal to the brief.
            log_error(f"draft reconcile aborted, Gmail tool slug missing: {_e}")
            reconcile_result = {"moved_to_sent": 0, "expired": 0, "kept": 0,
                                "declined_tasks": 0, "thread_ids_repaired": 0}
        if reconcile_result["moved_to_sent"] or reconcile_result["declined_tasks"]:
            debug(f"draft reconcile: {reconcile_result}")

        # A repair means the model wrote a message id into thread_id even though
        # the prompt now shows it the real one. The draft is already fixed on
        # disk; this says so out loud, because the alternative is a silent
        # self-healing loop that hides a prompt that is still not working.
        repaired = reconcile_result.get("thread_ids_repaired", 0)
        if repaired:
            log_error(f"draft thread_id repaired on {repaired} draft(s) — model still writing message ids")
            try:
                from notify import notify
                notify(
                    "Cortex: draft thread id was wrong",
                    f"Repaired {repaired} draft(s) whose thread_id held a message id. "
                    f"They work now, but the heartbeat prompt is not landing — see "
                    f"render_email_for_prompt() and the draft template.",
                )
            except Exception as _e:
                debug(f"thread_id repair notify failed: {_e}")

        # 3. Build and diff snapshots
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        curr_snapshot = build_snapshot(data)
        prev_snapshot = {}
        if STATE_PATH.exists():
            try:
                prev_snapshot = json.load(open(STATE_PATH))
            except (json.JSONDecodeError, OSError):
                pass

        # A source that failed to fetch this tick keeps last tick's ids for
        # that account, so a Gmail/Calendar outage never reads as a wave of
        # cancellations or a wiped inbox (2026-09-20).
        carry_forward_failed_accounts(prev_snapshot, curr_snapshot, data.get("fetch_status") or {})

        diff = diff_snapshots(prev_snapshot, curr_snapshot)

        update_fetch_failure_streak(prev_snapshot, curr_snapshot, data.get("fetch_status") or {})

        sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
        from shared import save_state

        # Carry forward persistent state fields before first save
        # Carry forward inbox_classified_ids so classify_inbox can read them from STATE_PATH
        if prev_snapshot.get("inbox_classified_ids"):
            curr_snapshot["inbox_classified_ids"] = prev_snapshot["inbox_classified_ids"]
        # Carry forward last_practice_offered (Phase 12 — practice nudge cooldown)
        if prev_snapshot.get("last_practice_offered"):
            curr_snapshot["last_practice_offered"] = prev_snapshot["last_practice_offered"]
        # Carry forward notified_email_ids (stale-UNREAD backstop — pruned on each update)
        if prev_snapshot.get("notified_email_ids"):
            curr_snapshot["notified_email_ids"] = prev_snapshot["notified_email_ids"]
        # Carry forward last_proposal_date (daily inbox-proposal push guard)
        if prev_snapshot.get("last_proposal_date"):
            curr_snapshot["last_proposal_date"] = prev_snapshot["last_proposal_date"]
        # Compute and stamp brief flags BEFORE state save so the agent can see them.
        # claim_brief() writes brief-coordination.json, which is gitignored and
        # therefore machine-local — it is a per-machine once-a-day guard only,
        # NOT a cross-machine one. BRIEF_OWNER_IS_VPS is what prevents duplicates.
        # Local mirror in heartbeat-state.json (last_*_brief_date) is the
        # machine-local fallback when auto-sync stalls and brief-coordination.json
        # gets reverted by stash/pop cycles.
        from brief_coord import claim_brief as _claim_brief
        today_pst_str = datetime.now(PST).strftime("%Y-%m-%d")
        # Only the owning machine claims. claim_brief() still runs there so the
        # local file keeps its once-per-day guard; the non-owner never claims,
        # so it can neither send a duplicate nor claim a brief it won't write.
        morning_brief_today = (
            BRIEF_OWNER_IS_VPS and is_first_run_of_day() and _claim_brief("morning")
        )
        afternoon_brief_today = (
            BRIEF_OWNER_IS_VPS and is_first_run_of_afternoon() and _claim_brief("afternoon")
        )
        if morning_brief_today:
            curr_snapshot["last_morning_brief_date"] = today_pst_str
        elif prev_snapshot.get("last_morning_brief_date"):
            curr_snapshot["last_morning_brief_date"] = prev_snapshot["last_morning_brief_date"]
        if afternoon_brief_today:
            curr_snapshot["last_afternoon_brief_date"] = today_pst_str
        elif prev_snapshot.get("last_afternoon_brief_date"):
            curr_snapshot["last_afternoon_brief_date"] = prev_snapshot["last_afternoon_brief_date"]
        # Alert dedup state now lives per-check in health-alert-state.json —
        # the joined string this used to persist is what caused the re-sends.
        save_state(str(STATE_PATH), curr_snapshot)

        # 4–5b. Inbox triage — top-level kill switch
        classify_result = {}
        auto_counts = {}
        if not lane_available("inbox"):
            triage_on = False
            debug("inbox triage skipped — inbox module not installed")
        else:
            from inbox.classifier import is_triage_enabled
            triage_on = is_triage_enabled()
            if not triage_on:
                debug("inbox triage disabled via inbox-mode.json:triage_enabled — skipping steps 4/5/5b/6/7/8")
            else:
                # 4. Inbox: ensure Cortex/* labels exist
                feature_run("inbox.ensure_labels", ensure_inbox_labels, log=debug)

                # 5. Inbox: classify new emails (rule-based pre-pass)
                def _classify():
                    cr = classify_inbox(data, diff)
                    debug(f"classify_inbox: auto={sum(len(v) for v in cr.get('auto', {}).values())} supervised={sum(len(v) for v in cr.get('supervised', {}).values())} unmatched={sum(len(v) for v in cr.get('unmatched', {}).values())} scan={sum(len(v) for v in cr.get('scan', {}).values())}")
                    return cr
                classify_result = feature_run("inbox.classify", _classify, log=debug, default={}) or {}

                # 5b. Inbox: scan rules — LLM extracts findings from matched emails
                def _scan():
                    if classify_result.get("scan"):
                        scanned = scan_inbox(classify_result)
                        debug(f"scan_inbox: processed {scanned} emails")
                feature_run("inbox.scan", _scan, log=debug)

        # 5c. Meeting prep: detect new cal.com bookings in the business inbox
        def _meeting_prep_bookings():
            from meeting_prep import handle_new_bookings as _handle_bookings
            biz_account = business_account()
            biz_msgs = data["gmail"].get(biz_account, [])
            if biz_msgs:
                new_bookings = _handle_bookings(biz_msgs, account=biz_account)
                if new_bookings:
                    debug(f"meeting_prep: {new_bookings} new booking(s) processed")
        if lane_available("meeting_prep"):
            feature_run("meeting_prep.handle_bookings", _meeting_prep_bookings, log=debug)

        # 5d. Intro prep: detect intro-network pre-intro emails
        def _intro_prep_suggestions():
            from intro_prep import handle_new_suggestions as _handle_intros
            biz_account = business_account()
            biz_msgs = data["gmail"].get(biz_account, [])
            if biz_msgs:
                new_intros = _handle_intros(biz_msgs, account=biz_account)
                if new_intros:
                    debug(f"intro_prep: {new_intros} new intro(s) processed")
        if lane_available("intro_prep"):
            feature_run("intro_prep.handle_suggestions", _intro_prep_suggestions, log=debug)

        if triage_on:
            # 6. Inbox: execute auto-tier actions (graduated tiers only)
            def _execute_auto():
                ac = execute_auto_actions(classify_result, dry_run=False)
                debug(f"auto actions: {ac}")
                return ac
            auto_counts = feature_run("inbox.execute_auto", _execute_auto, log=debug, default={}) or {}

            # 7. Inbox: process review file (approved/rejected by the owner)
            feature_run("inbox.process_review", process_review_file, classify_result, dry_run=False, log=debug)

            # 8. Inbox: refresh review file (always — surfaces auto-classifications for awareness + overrides)
            feature_run("inbox.update_review", update_review_file, classify_result, log=debug)

            # 8a. Inbox: daily proposal push (supervised training loop)
            def _daily_proposals():
                if not BRIEF_OWNER_IS_VPS:
                    return
                today = datetime.now(PST).strftime("%Y-%m-%d")
                if curr_snapshot.get("last_proposal_date") == today:
                    return
                supervised = classify_result.get("supervised", {})
                if not any(supervised.values()):
                    return
                from inbox import proposals as _props
                text, state = _props.build_daily(supervised)
                from integrations import telegram_bot
                if telegram_bot.send_message_sync(text, parse_mode=None):
                    _props.save_state(state)
                    curr_snapshot["last_proposal_date"] = today
            feature_run("inbox.daily_proposals", _daily_proposals, log=debug)

            # 8c. Inbox behavior watch — Sunday review of the sender-reputation
            # book (promote/contradiction/health proposals). Gated on
            # BRIEF_OWNER_IS_VPS, matching the other Telegram push lanes
            # (_daily_proposals, email_digest) — the VPS's reputation DB is
            # the live one for the chat/proposal lane, so it is the right
            # sender; without this gate both Mac and VPS would send it.
            # Gated on triage_on: with triage off the book/actions log go stale
            # and this step is noise.
            def _behavior_watch():
                if not BRIEF_OWNER_IS_VPS:
                    return {"sent": False}
                if datetime.now(PST).weekday() != 6:
                    return {"sent": False}
                if prev_snapshot.get("last_behavior_watch_date") == today_pst_str:
                    return {"sent": False}
                from inbox import behavior_watch as _bw
                lines = _bw.weekly_proposals()
                actionable = [l for l in lines if l.startswith("PROMOTE") or l.startswith("CONTRADICTION")]
                curr_snapshot["last_behavior_watch_date"] = today_pst_str
                if not actionable:
                    debug("behavior_watch: nothing actionable this week")
                    return {"sent": False}
                from integrations import telegram_bot
                health = [l for l in lines if l.startswith("This week:")]
                text = "Weekly behavior watch:\n" + "\n".join(actionable + health)
                telegram_bot.send_message_sync(text, parse_mode=None)
                debug(f"behavior_watch: sent {len(actionable)} proposal(s)")
                return {"sent": True}
            feature_run("inbox.behavior_watch", _behavior_watch, log=debug, default={})

        # 8d. Email digest: 2x daily push (noon + 8 PM) across all Gmail tabs
        # for both accounts. Cross-machine dedup via brief_coord.
        def _email_digest():
            if not BRIEF_OWNER_IS_VPS:
                return  # VPS owns the digest lane; see BRIEF_OWNER_IS_VPS
            from email_digest import run_if_due as _email_digest_if_due
            fired = _email_digest_if_due()
            if fired:
                debug(f"email_digest: pushed {fired} digest")
        if lane_available("email_digest"):
            feature_run("digest.email_2x_daily", _email_digest, log=debug)

        # Update state with classified IDs
        if classify_result.get("classified_ids"):
            curr_snapshot["inbox_classified_ids"] = sorted(classify_result["classified_ids"])
            curr_snapshot["inbox_last_run"] = datetime.now().isoformat()
        elif prev_snapshot.get("inbox_classified_ids"):
            curr_snapshot["inbox_classified_ids"] = prev_snapshot["inbox_classified_ids"]
        # Record IDs surfaced this run so they don't re-trigger stale-UNREAD
        update_notified_ids(curr_snapshot, diff)
        save_state(str(STATE_PATH), curr_snapshot)

        if not diff["has_changes"] and not morning_brief_today and not afternoon_brief_today:
            debug("no changes — skipping SDK call")
            _refresh_dashboard_snapshot(events=data.get("gcal"))
            return

        debug(f"changes detected: {diff['new_email_count']} new emails, {diff['new_event_count']} new events, morning_brief={morning_brief_today}, afternoon_brief={afternoon_brief_today}")

        # 8e. Guardrail: a no-tools screener sees only the external fields.
        # fail → skip this run (already logged + notified), suspicious → warn.
        gate = feature_run(
            "guardrail.precheck",
            lambda: run_guardrail_gate(data, brief_pending=morning_brief_today or afternoon_brief_today),
            log=debug,
            default={"verdict": "suspicious", "reason": "guardrail feature errored", "warning": "guardrail feature errored (flags: unknown)"},
        )
        if gate.get("verdict") == "fail":
            # Don't lose today's brief: it was claimed above (before the gate
            # ran, so the agent could see the flag) but never actually written
            # or sent. Release the claim, both locally (heartbeat-state.json)
            # and in brief_coord's cross-tick store, so the next tick retries.
            _rollback_brief_claims(curr_snapshot, prev_snapshot, morning_brief_today, afternoon_brief_today)
            save_state(str(STATE_PATH), curr_snapshot)
            from brief_coord import release_brief as _release_brief
            if morning_brief_today:
                _release_brief("morning")
            if afternoon_brief_today:
                _release_brief("afternoon")
            _refresh_dashboard_snapshot(events=data.get("gcal"))
            return
        guardrail_warning = gate.get("warning")

        # 9. Run Agent SDK (with inbox triage context)
        response_texts = asyncio.run(run_heartbeat(
            data, diff, classify_result, auto_counts, health_report,
            morning_brief=morning_brief_today,
            afternoon_brief=afternoon_brief_today,
            last_practice_offered=curr_snapshot.get("last_practice_offered"),
            guardrail_warning=guardrail_warning,
        ))

        # 9b. Parse TASK_PROPOSAL lines from model output
        def _parse_task_proposals():
            if not response_texts:
                return
            import tasks as tq
            full_text = "\n".join(response_texts)
            proposed_count = 0
            for line in full_text.splitlines():
                line = line.strip()
                if not line.startswith("TASK_PROPOSAL:"):
                    continue
                payload = line[len("TASK_PROPOSAL:"):].strip()
                fields = {}
                for part in payload.split("|"):
                    if "=" not in part:
                        continue
                    k, _, v = part.partition("=")
                    fields[k.strip()] = v.strip().strip('"').strip("'")
                if "content" not in fields or "source" not in fields:
                    continue
                labels = [fields["labels"]] if fields.get("labels") else []
                priority = int(fields["priority"]) if fields.get("priority", "").isdigit() else None
                quadrant = fields.get("quadrant") or None
                duration_raw = fields.get("duration") or None
                try:
                    duration_minutes = tq.parse_duration(duration_raw) if duration_raw else tq.MIN_DURATION_MINUTES
                except ValueError as _de:
                    debug(f"invalid duration {duration_raw!r}, falling back to minimum: {_de}")
                    duration_minutes = tq.MIN_DURATION_MINUTES
                try:
                    result = tq.propose(
                        content=fields["content"],
                        labels=labels,
                        due_string=fields.get("due") or None,
                        priority=priority,
                        quadrant=quadrant,
                        duration_minutes=duration_minutes,
                        source=fields["source"],
                        project=fields.get("project") or None,
                        task_type=fields.get("type") or None,
                        who=fields.get("who") or None,
                    )
                except ValueError as _qe:
                    debug(f"skipping proposal with invalid quadrant {quadrant!r}: {_qe}")
                    continue
                if result:
                    proposed_count += 1
                    debug(f"proposed task: {result['id']} — {result['content']}")
            if proposed_count:
                tq.render_queue()
                debug(f"task proposals parsed: {proposed_count}")
        if lane_available("tasks"):
            feature_run("tasks.parse_proposals", _parse_task_proposals, log=debug)

        # 10. Parse and send notifications
        if response_texts:
            notif_result = parse_and_send_notifications(response_texts)
            parse_and_apply_classifications(response_texts, classify_result)

            # The brief was claimed above (before the model even ran, since it
            # has to know whether to ask for one), but its actual Telegram/macOS
            # send just failed — release the claim so the next tick retries
            # instead of finding today's brief marked done with nothing sent
            # (2026-09-20).
            brief_kind_failed = notif_result.get("brief_send_failed")
            if brief_kind_failed:
                _rollback_brief_claims(
                    curr_snapshot, prev_snapshot,
                    morning=(brief_kind_failed == "morning"),
                    afternoon=(brief_kind_failed == "afternoon"),
                )
                save_state(str(STATE_PATH), curr_snapshot)
                from brief_coord import release_brief as _release_brief
                _release_brief(brief_kind_failed)
                debug(f"{brief_kind_failed} brief send failed — claim released, retried next tick")

        # 10b. If LLM emitted a NOTIFICATION: Practice line, stamp the cooldown.
        # Done lazily after dispatch — if the LLM suppressed the nudge, no
        # cooldown is consumed. Re-saves state with the new timestamp.
        def _stamp_practice_cooldown():
            if not response_texts:
                return
            emitted_practice = any(
                re.search(r"^NOTIFICATION:\s*Practice\b", line)
                for text in response_texts
                for line in text.split("\n")
            )
            if emitted_practice:
                curr_snapshot["last_practice_offered"] = datetime.now(PST).isoformat()
                save_state(str(STATE_PATH), curr_snapshot)
                debug("practice nudge emitted — cooldown stamped")
        feature_run("practices.stamp_cooldown", _stamp_practice_cooldown, log=debug)

        # 11. Meeting prep: T-2h refresh + archive past meetings
        def _refresh_meetings_fn():
            from meeting_prep import refresh_upcoming as _refresh_meetings
            refreshed = _refresh_meetings()
            if refreshed:
                debug(f"meeting_prep: {refreshed} meeting(s) refreshed")
        if lane_available("meeting_prep"):
            feature_run("meeting_prep.refresh_upcoming", _refresh_meetings_fn, log=debug)

        # 12. Dashboard snapshot: refresh the Obsidian command center
        _refresh_dashboard_snapshot(events=data.get("gcal"))

        debug("heartbeat complete")
    except Exception:
        tb = traceback.format_exc()
        debug(f"heartbeat failed: {tb}")
        log_error(f"Heartbeat failed:\n{tb}")


if __name__ == "__main__":
    main()
