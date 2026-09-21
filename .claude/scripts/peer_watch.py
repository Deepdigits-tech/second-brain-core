#!/usr/bin/env python3
"""Cross-machine dead-man's switch.

THE PROBLEM this closes: the VPS owns every Telegram push lane and the
morning/afternoon briefs (`heartbeat.is_brief_owner()`). If the VPS heartbeat
dies, hangs, or loses auth, all of that goes quiet — and nothing notices,
because `feature_health.py` runs INSIDE the heartbeat it would need to watch,
and the only outside check is the weekly audit. This module is the other
half: each machine writes a tiny "I am alive" stamp the OTHER machine can
read, and alerts when the peer's stamp looks stale.

Why not just read `auto-sync: <machine> ...` commit history as the liveness
signal — the obvious first idea: a machine with nothing to commit writes NO
commit at all. A quiet vault can leave hours between auto-sync commits purely
because nothing changed to commit, which is routine, not an outage. "Time
since last auto-sync commit" would false-alarm constantly. An explicit
per-tick stamp, written on its own cadence independent of vault activity, is
the only signal that means what it says.

**Single-machine deployments (this repo's own default): this whole watcher
stays silent.** Set `CORTEX_PEER_MACHINE` (to anything non-empty — its value
is not otherwise read) only once a genuine second machine exists and writes
its own stamp. Unset, `run()` is a no-op on the very first call: no state is
written, no liveness stamp arrives from anywhere to be missing, and nothing
ever alerts about a peer that was never going to report because it does not
exist. See master.env.example and CLAUDE.md.

Liveness stamp: `.claude/data/liveness/<machine>.json`, lowercase machine
name ("mac.json" / "vps.json"). It is GIT-TRACKED on purpose — that is the
whole mechanism: the existing 2-minute vault_sync.sh carries it to the other
machine, no SSH involved. (`.claude/data/` is inside vault_sync.sh's
AUTO_SYNC_ALLOW list, so this ships with everything else already syncing —
confirm this holds if that allowlist is ever narrowed; see CLAUDE.md.)

Written by `write_own_stamp()`, called from heartbeat.py's `health.peer_watch`
lane on EVERY tick — but the file is only actually rewritten when it is due
(see `should_write_stamp`): rewriting on every 30-min tick would be pure
noise through the git sync. Immediate exceptions and what is deferred to the
routine interval are documented on `should_write_stamp` itself.

Deploy-day false alarm: on the very first tick after two machines are wired
up, NEITHER machine has the other's stamp yet (it has to be committed,
pushed, pulled — and a laptop may be asleep for days). Alerting immediately
on a missing stamp produces wrong messages (alarm + "back to normal" on both
machines). `run()` records `first_watched_utc` per peer in its own
machine-local state and stays quiet about a missing stamp until this machine
has been watching for at least that peer's own stale threshold.

Alert state is machine-local (`.claude/data/state/peer-watch.json`,
gitignored) — same per-machine bookkeeping shape as feature_health.py's own
alert state, because each machine forms its own opinion about whether its
peer looks dead.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LIVENESS_DIR = PROJECT_ROOT / ".claude" / "data" / "liveness"
STATE_PATH = PROJECT_ROOT / ".claude" / "data" / "state" / "peer-watch.json"

MACHINE = "Mac" if sys.platform == "darwin" else "VPS"
PEER_OF = {"Mac": "VPS", "VPS": "Mac"}

# ── Constants ──────────────────────────────────────────────────────────

WRITE_MIN_INTERVAL_HOURS = 2  # write at most this often when nothing else changed

# "That machine's active hours" threshold for a stale last_tick_utc. The VPS
# never sleeps, so 5h of silence during its own always-on hours is a real
# problem. A laptop sleeps AND gets physically closed for a weekend — Friday
# 6pm to Monday 9am is 63h of ordinary, expected silence — so its threshold
# is set well past that (96h ≈ 4 days) and its severity is always "info",
# never an alarm: going quiet is normal for it, not a failure.
STALE_HOURS = {"VPS": 5, "Mac": 96}

# Ticking, but the model keeps failing (the auth-expired case). Must stay
# comfortably above the write-cadence lag: `should_write_stamp` only forces
# an immediate write on a RECOVERY (failing -> ok); the transition INTO
# failing, with last_model_ok_utc unchanged (the model just stops
# succeeding), is caught by the routine WRITE_MIN_INTERVAL_HOURS write
# instead — so a stale last_model_ok_utc can be up to ~2h behind reality by
# the time it reaches the peer. 6h leaves a 4h margin over that 2h worst-case
# lag before this would even evaluate as stale, so the threshold still holds.
MODEL_STALE_HOURS = 6

BRIEF_MISSING_HOUR_PT = 9  # 9:30 AM PT
BRIEF_MISSING_MINUTE_PT = 30
REMIND_HOURS = 24

# Mirrors heartbeat.ACTIVE_HOURS (6 AM - 10 PM PT) — heartbeat.py's own
# active-hours gate means NEITHER machine ticks overnight, VPS included. A
# 5h staleness threshold evaluated at, say, 2 AM would false-alarm every
# single night (last tick before the 10 PM cutoff would already be several
# hours old by then) — the VPS stale-tick check is therefore only evaluated
# during the watched machine's own active hours, same design as
# heartbeat.ACTIVE_HOURS itself. The laptop's much longer threshold already
# spans a full off-hours window several times over, so it is not gated the
# same way.
ACTIVE_HOURS = (6, 22)
FRESH_TICK_HOURS = 3

# A peer's stamp is untrusted input. A clock more than this far in the
# future would otherwise silence every age-based check forever (a negative
# age never exceeds a positive threshold) — flagged instead of trusted.
CLOCK_SKEW_TOLERANCE_MINUTES = 10


# ── Single-machine gate ──────────────────────────────────────────────────


def peer_configured() -> bool:
    """A second machine only exists if it is explicitly configured.

    Unset (the single-machine default) means this watcher is fully off: no
    peer stamp will ever arrive from a machine that doesn't exist, so `run()`
    must never reach the point of starting a "waiting for the peer's first
    stamp" grace-period clock that would eventually expire and alarm.
    """
    return bool(os.environ.get("CORTEX_PEER_MACHINE", "").strip())


# ── Parsing ────────────────────────────────────────────────────────────


def _parse(ts: str | None) -> datetime | None:
    """Lenient parse for OUR OWN files (this machine's own state / stamp,
    always written with a UTC offset by this module) — a naive timestamp
    here is a bug in our own code, not untrusted input, so it's fine to
    assume UTC. Do not use this on data read from the PEER's stamp; see
    `_parse_strict`."""
    if not ts:
        return None
    try:
        v = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v


def _parse_strict(ts: str | None) -> datetime | None:
    """Parse for data read from the PEER's stamp — untrusted input. A
    timezone-less timestamp is REJECTED, not silently assumed to be UTC: a
    peer with a misconfigured clock or a stale/hand-edited stamp could
    otherwise read as perfectly healthy."""
    if not ts:
        return None
    try:
        v = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if v.tzinfo is None:
        return None
    return v


def _stamp_path(machine: str, liveness_dir: Path | None = None) -> Path:
    return (liveness_dir or LIVENESS_DIR) / f"{machine.lower()}.json"


def read_stamp(machine: str, *, liveness_dir: Path | None = None) -> dict | None:
    path = _stamp_path(machine, liveness_dir)
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


# ── Write cadence (own stamp) ────────────────────────────────────────────


def should_write_stamp(prev: dict | None, now: datetime, last_model_ok_utc: str | None,
                        last_brief_date: str | None) -> bool:
    """Pure: is it time to rewrite our own liveness stamp?

    Always due with no prior stamp. Otherwise due immediately when:
      (a) `last_brief_date` changed — a brief finally landing should be
          visible right away, not held for up to 2h.
      (b) the model RECOVERS — the previously-written `last_model_ok_utc`
          was already stale (>= MODEL_STALE_HOURS old, as of `now`) and the
          new value is a genuinely fresher timestamp. This is a real state
          flip (failing -> ok), not just "the timestamp changed" — a model
          that keeps succeeding produces a fresh `last_model_ok_utc` on
          EVERY tick, and treating every one of those as "due" was the
          original bug here: many more commits/day/machine through the git
          sync than intended.
    Otherwise due once WRITE_MIN_INTERVAL_HOURS has passed since the last
    write — the routine case, driven by nothing but the clock. A transition
    INTO failing (the model stops succeeding, so `last_model_ok_utc` simply
    stops changing) is caught by this routine interval, not immediately —
    see MODEL_STALE_HOURS's comment for why that lag is still safe.
    """
    if not prev:
        return True
    if prev.get("last_brief_date") != last_brief_date:
        return True

    prev_model_ts = _parse(prev.get("last_model_ok_utc"))
    new_model_ts = _parse(last_model_ok_utc)
    prev_was_failing = prev_model_ts is None or (now - prev_model_ts) >= timedelta(hours=MODEL_STALE_HOURS)
    recovered = prev_was_failing and new_model_ts is not None and new_model_ts != prev_model_ts
    if recovered:
        return True

    last_written = _parse(prev.get("last_tick_utc"))
    if last_written is None:
        return True
    return now - last_written >= timedelta(hours=WRITE_MIN_INTERVAL_HOURS)


def write_own_stamp(now: datetime, *, last_model_ok_utc: str | None, last_brief_date: str | None,
                     machine: str | None = None, liveness_dir: Path | None = None) -> dict | None:
    """Write this machine's liveness stamp if due. Returns the stamp written,
    or None when it wasn't due this tick. Never raises — a write failure here
    must not break the heartbeat tick; the caller (heartbeat.py) additionally
    wraps this in feature_run, which would count a raised exception toward
    feature_health's failure streak, so this stays defensive on its own.

    Does NOT check `peer_configured()` — on a single-machine install this
    still writes a small, harmless local file nobody reads yet; the caller
    in heartbeat.py is what decides whether to invoke this at all. Calling
    it directly (e.g. in tests) always behaves the same way regardless of
    CORTEX_PEER_MACHINE.
    """
    machine = machine or MACHINE
    path = _stamp_path(machine, liveness_dir)
    prev = read_stamp(machine, liveness_dir=liveness_dir)
    if not should_write_stamp(prev, now, last_model_ok_utc, last_brief_date):
        return None
    stamp = {
        "machine": machine,
        "last_tick_utc": now.astimezone(timezone.utc).isoformat(),
        "last_model_ok_utc": last_model_ok_utc,
        "last_brief_date": last_brief_date,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(stamp, indent=2, sort_keys=True))
        tmp.replace(path)
    except OSError:
        return None
    return stamp


# ── Evaluating the peer's stamp ───────────────────────────────────────────


def _in_active_hours(now_pt: datetime) -> bool:
    return ACTIVE_HOURS[0] <= now_pt.hour < ACTIVE_HOURS[1]


def _active_hours_between(start_pt: datetime, end_pt: datetime) -> float:
    """Hours between two moments that fall INSIDE the heartbeat's active window.

    Neither machine ticks from 10 PM to 6 AM. Measured by the wall clock, every
    stamp is 8+ hours old at the first tick of the morning, and a 5-hour rule
    would raise an alarm at 6 AM every day until the peer's first stamp of the
    day arrived. Only waking hours count as silence.
    """
    if end_pt <= start_pt:
        return 0.0
    total = 0.0
    day = start_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end_pt:
        lo = max(start_pt, day.replace(hour=ACTIVE_HOURS[0]))
        hi = min(end_pt, day.replace(hour=ACTIVE_HOURS[1]))
        if hi > lo:
            total += (hi - lo).total_seconds() / 3600
        day += timedelta(days=1)
    return total


def _past_930_pt(now_pt: datetime) -> bool:
    return (now_pt.hour, now_pt.minute) >= (BRIEF_MISSING_HOUR_PT, BRIEF_MISSING_MINUTE_PT)


def _severity_for(watched_machine: str) -> str:
    return "info" if watched_machine == "Mac" else "alarm"


def evaluate(watched_machine: str, stamp: dict | None, now_utc: datetime, now_pt: datetime,
             *, first_watched_utc: datetime | None = None) -> dict | None:
    """Pure: does the watched machine's stamp look like a problem right now?

    Returns None when everything looks fine, else {"kind", "severity",
    "detail"}. `severity` is "alarm" or "info" — the laptop's own silence is
    always information, never an alarm (it sleeps and gets closed by
    design). `first_watched_utc` is when THIS machine started watching
    `watched_machine` (see `run()`) — a missing stamp is quiet until that
    has been at least `watched_machine`'s own stale threshold, so the first
    tick after two machines are first wired together (neither has the
    other's stamp yet) does not read as "has never reported".
    """
    threshold_hours = STALE_HOURS.get(watched_machine, 5)

    if stamp is None:
        if first_watched_utc is not None and (now_utc - first_watched_utc) < timedelta(hours=threshold_hours):
            return None
        return {"kind": "missing", "severity": _severity_for(watched_machine),
                "detail": f"{watched_machine} has never reported a liveness stamp"}

    tick = _parse_strict(stamp.get("last_tick_utc"))
    if tick is None:
        # Unreadable AND timezone-less are rejected the same way — both mean
        # the data can't be trusted, not that the peer is down.
        return {"kind": "clock_wrong", "severity": "info",
                "detail": f"{watched_machine}'s liveness stamp has an unreadable or "
                          f"timezone-less last_tick_utc"}

    skew = tick - now_utc
    if skew > timedelta(minutes=CLOCK_SKEW_TOLERANCE_MINUTES):
        return {"kind": "clock_wrong", "severity": "info",
                "detail": f"clock looks wrong on {watched_machine} (its last_tick_utc is "
                          f"{skew.total_seconds() / 60:.0f} min in the future)"}

    age = now_utc - tick
    # Waking hours the peer has been silent for. The VPS rule uses these; the
    # laptop's 96h rule is long enough to stay on the wall clock.
    silent_h = _active_hours_between(tick.astimezone(now_pt.tzinfo), now_pt)
    if watched_machine == "VPS":
        stale = silent_h >= threshold_hours and _in_active_hours(now_pt)
    else:
        stale = age >= timedelta(hours=threshold_hours)
    if stale:
        severity = _severity_for(watched_machine)
        if watched_machine == "Mac":
            local_tick = tick.astimezone(now_pt.tzinfo)
            detail = (f"Mac hasn't ticked since {local_tick.strftime('%A %-I:%M%p').lower()} "
                      f"— fine if it's been closed")
        else:
            detail = (f"{watched_machine} last ticked {age.total_seconds() / 3600:.1f}h ago "
                      f"(threshold {threshold_hours}h)")
        return {"kind": "stale_tick", "severity": severity, "detail": detail}

    # Brief-missing is scoped to the VPS on purpose: it is the default brief
    # owner (is_brief_owner()). A machine that never claims a brief would
    # otherwise always read last_brief_date as stale and false-alarm forever.
    # Under a CORTEX_BRIEF_OWNER override this check can be stale in either
    # direction — a known simplification, see CLAUDE.md.
    if watched_machine == "VPS" and _past_930_pt(now_pt):
        today = now_pt.strftime("%Y-%m-%d")
        if stamp.get("last_brief_date") != today:
            return {"kind": "brief_missing", "severity": "alarm",
                    "detail": f"VPS has not sent today's ({today}) brief yet"}

    # "Ticking but the model is failing" only means something while the peer IS
    # ticking. A closed laptop has an old tick AND an old model call; without
    # this gate the VPS reported "Mac is ticking (last tick 9.0h ago)..." every
    # morning. Stamps are written about every 2 hours, so "ticking" = a stamp
    # under FRESH_TICK_HOURS of waking time old.
    model_ok = _parse_strict(stamp.get("last_model_ok_utc"))
    if (model_ok is not None and silent_h < FRESH_TICK_HOURS
            and (model_ok - now_utc) <= timedelta(minutes=CLOCK_SKEW_TOLERANCE_MINUTES)):
        model_age = now_utc - model_ok
        model_silent_h = _active_hours_between(model_ok.astimezone(now_pt.tzinfo), now_pt)
        if model_silent_h >= MODEL_STALE_HOURS:
            return {"kind": "model_stale", "severity": _severity_for(watched_machine),
                    "detail": f"{watched_machine} is ticking (last tick {age.total_seconds() / 3600:.1f}h "
                              f"ago) but its last successful model call was "
                              f"{model_age.total_seconds() / 3600:.1f}h ago"}

    return None


# What CORTEX_BRIEF_OWNER actually moves, precisely — it must not be read as
# "moves everything". It does not move reflection (CORTEX_REFLECTION_OWNER),
# content weekly (CORTEX_CONTENT_WEEKLY_OWNER), or other platform-gated
# lanes; each of those has its own gate (some with their own override, some
# not).
_TAKEOVER_HINT = (
    " To move briefs and the Telegram push lanes to the Mac: set "
    "CORTEX_BRIEF_OWNER=1 in .claude/.env on the Mac and CORTEX_BRIEF_OWNER=0 "
    "on the VPS. This does NOT move reflection (CORTEX_REFLECTION_OWNER), "
    "content weekly (CORTEX_CONTENT_WEEKLY_OWNER), or other platform-gated "
    "lanes — those need their own override, or stay off. Setting both "
    "machines to 1 duplicates every brief and push lane."
)


def _message(problem: dict, watched_machine: str, *, reminder: bool) -> str:
    icon = "ℹ️" if problem.get("severity") == "info" else "⚠️"
    lead = "Still: " if reminder else ""
    msg = f"{icon} {lead}{problem['detail']}."
    # The takeover hint only makes sense when the machine having the problem
    # is the VPS — telling a broken Mac to "move things to the Mac" is not
    # an actionable fix.
    if problem.get("severity") == "alarm" and watched_machine == "VPS":
        msg += _TAKEOVER_HINT
    return msg


def check(problem: dict | None, state: dict, *, now: datetime, watched_machine: str) -> tuple[list[str], dict]:
    """Pure: given today's evaluation and what was already said, return the
    lines to send and the new state. Nothing here touches disk or network —
    mirrors feature_health.check()'s shape."""
    alerted = dict((state or {}).get("alerted") or {})
    msgs: list[str] = []
    prior = alerted.get(watched_machine)

    if problem is not None:
        if prior is None:
            msgs.append(_message(problem, watched_machine, reminder=False))
            alerted[watched_machine] = {"at": now.isoformat(), "kind": problem["kind"]}
        else:
            last = _parse(prior.get("at"))
            if last is None or now - last >= timedelta(hours=REMIND_HOURS):
                msgs.append(_message(problem, watched_machine, reminder=True))
                alerted[watched_machine] = {"at": now.isoformat(), "kind": problem["kind"]}
    elif prior is not None:
        msgs.append(f"✅ {watched_machine} is back to normal.")
        del alerted[watched_machine]

    return msgs, {"alerted": alerted}


# ── Files and Telegram ────────────────────────────────────────────────────


def _load_state(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _default_notify(message: str) -> bool:
    from notify import notify, delivered
    # Telegram channel only — this is a dead-man's-switch alert about the
    # machine that owns the push lanes; a macOS-only notification on a Mac
    # that is itself asleep or unattended is not a delivered alert.
    result = notify("", message, channels=["telegram"], parse_mode=None)
    return delivered(result)


def run(*, now: datetime | None = None, notify_fn: Callable[[str], bool] | None = None,
        own_machine: str | None = None, liveness_dir: Path | None = None,
        state_path: Path | None = None, owner_tz=None, dry_run: bool = False) -> dict:
    """Read the peer's stamp, speak once per new problem or recovery,
    remember what was said. A message that fails to send is not remembered,
    so it is retried next tick. Returns {"alerted": [...], "reminded": [...],
    "recovered": [...]} — same shape as feature_health.run().

    A no-op with no state touched at all when `peer_configured()` is false
    (the single-machine default) — see the module docstring.
    """
    empty = {"alerted": [], "reminded": [], "recovered": []}
    if not peer_configured():
        return empty

    now = now or datetime.now(timezone.utc)
    own_machine = own_machine or MACHINE
    watched = PEER_OF[own_machine]
    notify_fn = notify_fn or _default_notify
    spath = state_path or STATE_PATH

    try:
        if owner_tz is None:
            from shared import PST as owner_tz  # noqa: N814
        now_pt = now.astimezone(owner_tz)
        stamp = read_stamp(watched, liveness_dir=liveness_dir)
        state = _load_state(spath)

        # first_watched_utc: the clock a "missing stamp" grace period is
        # measured against. Recorded the first time THIS machine ever asks
        # about `watched` — not reset by anything else, so a genuinely-
        # missing peer eventually does alert once the grace period (that
        # peer's own stale threshold) elapses.
        first_watched = dict(state.get("first_watched_utc") or {})
        first_watched_is_new = watched not in first_watched
        if first_watched_is_new:
            first_watched[watched] = now.isoformat()
        first_watched_dt = _parse(first_watched.get(watched))

        problem = evaluate(watched, stamp, now, now_pt, first_watched_utc=first_watched_dt)
        msgs, new_state = check(problem, state, now=now, watched_machine=watched)
        new_state["first_watched_utc"] = first_watched
    except Exception:
        return empty

    if not msgs:
        # Persist first_watched_utc even with nothing to say — otherwise the
        # "waiting for the peer's first stamp" clock would reset every tick
        # and the grace period would never actually start.
        if first_watched_is_new and not dry_run:
            try:
                _save_state(spath, new_state)
            except OSError:
                pass
        return empty

    out = {"alerted": [], "reminded": [], "recovered": []}
    sent_all = True
    for m in msgs:
        if dry_run:
            print(m)
            ok = True
        else:
            try:
                ok = bool(notify_fn(m))
            except Exception:
                ok = False
        if not ok:
            sent_all = False
            continue
        bucket = ("recovered" if m.startswith("✅")
                  else ("reminded" if m.startswith(("⚠️ Still", "ℹ️ Still")) else "alerted"))
        out[bucket].append(watched)

    if sent_all and not dry_run:
        try:
            _save_state(spath, new_state)
        except OSError:
            pass
    return out


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv[1:]
    result = run(dry_run=dry)
    print(json.dumps(result))
