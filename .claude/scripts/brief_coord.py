"""Cross-machine coordination for once-per-day briefs and notifications.

Why this file exists
--------------------
Heartbeat state (`.claude/data/state/heartbeat-state.json`) is gitignored — each
runner (Mac + VPS) keeps its own copy. That's fine for machine-local caches
(gmail_ids, gcal_ids, hashes), but it's a disaster for "first run of the day"
flags, which must be shared. Without sharing, both machines decide they're the
first runner and each fires a morning brief — user gets two.

This module persists the shared-coordination keys into a separate, git-tracked
file (`.claude/data/brief-coordination.json`), with fcntl file locking around
reads/writes so concurrent runners on the same machine can't race.

The git-sync cycle (every 2 min) means the two machines converge quickly. It's
not perfectly atomic across machines, but in practice briefs fire on scheduled
timers offset by machine wake-up patterns, so 2-min convergence is enough.

Additional defense: notification dispatch deduplicates on a *typed* key
(`morning_brief:YYYY-MM-DD`), not just content hash, so that even during a
sync-lag race the second machine's notification is dropped.
"""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
COORD_PATH = PROJECT_ROOT / ".claude" / "data" / "brief-coordination.json"
from shared import PST


def _today_pst() -> str:
    return datetime.now(PST).strftime("%Y-%m-%d")


def _load() -> dict:
    if not COORD_PATH.exists():
        return {}
    try:
        return json.loads(COORD_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    COORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = COORD_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(COORD_PATH)


def _with_lock(fn):
    """Run fn() under an exclusive file lock on COORD_PATH.lock."""
    COORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = str(COORD_PATH) + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def claim_brief(kind: str) -> bool:
    """Atomically claim today's brief of `kind` ('morning' or 'afternoon').

    Returns True if we just claimed it (caller should fire the brief).
    Returns False if it was already claimed today by any runner.

    SCOPE: this file is gitignored (.gitignore: "never sync"), so a claim is
    visible ONLY on the machine that made it. This is a per-machine
    once-a-day guard, not a cross-machine one. Duplicate sends are prevented
    by machine ownership instead — see heartbeat.BRIEF_OWNER_IS_VPS. Do not
    reintroduce a cross-machine assumption here without un-ignoring the file.
    """
    today = _today_pst()
    key = f"{kind}_brief_date"

    def _do():
        data = _load()
        if data.get(key) == today:
            return False
        data[key] = today
        data[f"{kind}_brief_at"] = datetime.now(PST).isoformat()
        _save(data)
        return True

    return _with_lock(_do)


def release_brief(kind: str) -> None:
    """Undo today's claim_brief(kind) — used when the run that claimed it was
    then aborted (e.g. a guardrail `fail` skips the run entirely) so the next
    tick can claim and actually send the brief instead of finding it already
    marked claimed with nothing sent.

    Only removes the claim if it's *today's* — a stale claim from a previous
    day (which claim_brief would already treat as expired) is left alone.
    """
    today = _today_pst()
    key = f"{kind}_brief_date"

    def _do():
        data = _load()
        if data.get(key) == today:
            data.pop(key, None)
            data.pop(f"{kind}_brief_at", None)
            _save(data)

    _with_lock(_do)


def was_brief_claimed(kind: str) -> bool:
    """Non-claiming read: has today's brief already been claimed?"""
    return _load().get(f"{kind}_brief_date") == _today_pst()


def was_notification_claimed(dedup_key: str, *, ttl_minutes: int = 50) -> bool:
    """Non-claiming read: has `dedup_key` already been sent within the TTL?

    Used to skip a re-send BEFORE calling notify() (2026-09-20) — the actual
    claim only happens after a successful send, via `claim_notification()`,
    so a failed send is retried next tick instead of being marked sent.
    """
    now = datetime.now(timezone.utc)
    ttl = timedelta(minutes=ttl_minutes)
    sent = _load().get("sent_notifications", {})
    ts = sent.get(dedup_key)
    if not ts:
        return False
    try:
        return (now - datetime.fromisoformat(ts)) < ttl
    except ValueError:
        return False


def claim_notification(dedup_key: str, *, ttl_minutes: int = 50) -> bool:
    """Atomically claim a notification send slot keyed by `dedup_key`.

    Use typed keys like 'morning_brief:2026-04-18' for brief-type dedup,
    or 'content:<hash>' for content-hash dedup. Returns True if caller
    should send; False if another runner already claimed within TTL.
    """
    now = datetime.now(timezone.utc)
    ttl = timedelta(minutes=ttl_minutes)

    def _do():
        data = _load()
        sent = data.get("sent_notifications", {})

        # Prune expired entries
        sent = {
            k: ts for k, ts in sent.items()
            if (now - datetime.fromisoformat(ts)) < ttl
        }

        if dedup_key in sent:
            data["sent_notifications"] = sent
            _save(data)
            return False

        sent[dedup_key] = now.isoformat()
        data["sent_notifications"] = sent
        _save(data)
        return True

    return _with_lock(_do)
