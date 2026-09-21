"""peer_watch: the cross-machine half of the failure watcher.

feature_health.py only ever reads THIS machine's own diary, so a dead VPS
heartbeat — which owns every brief and Telegram push lane — has no watcher.
This module has each machine write a tiny liveness stamp the OTHER machine
can read (via the existing 2-minute git sync, not SSH) and alerts when the
peer's stamp looks stale, its model keeps failing, or its brief is missing.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import peer_watch as pw  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)  # 07:00 PT


@pytest.fixture(autouse=True)
def _peer_is_configured(monkeypatch):
    """Every test in this file exercises the peer-watch mechanism itself, not
    the single-machine-default gate (which has its own dedicated tests
    below) — so CORTEX_PEER_MACHINE is set for all of them by default."""
    monkeypatch.setenv("CORTEX_PEER_MACHINE", "vps")


def _stamp(machine, tick, model_ok=None, brief_date=None):
    return {"machine": machine, "last_tick_utc": tick.isoformat(),
            "last_model_ok_utc": model_ok.isoformat() if model_ok else None,
            "last_brief_date": brief_date}


# ── should_write_stamp: write cadence is not "every tick" ───────────────


def test_no_prior_stamp_always_writes():
    assert pw.should_write_stamp(None, T0, None, None) is True


def test_routine_case_waits_for_the_interval():
    prev = _stamp("VPS", T0)
    just_under = T0 + timedelta(hours=pw.WRITE_MIN_INTERVAL_HOURS) - timedelta(minutes=1)
    at_interval = T0 + timedelta(hours=pw.WRITE_MIN_INTERVAL_HOURS)
    assert pw.should_write_stamp(prev, just_under, None, None) is False
    assert pw.should_write_stamp(prev, at_interval, None, None) is True


def test_model_never_ok_then_first_success_writes_immediately():
    """prev last_model_ok_utc=None ('failing', bootstrap case) -> a fresh
    success is a recovery and must be surfaced right away."""
    prev = _stamp("VPS", T0, model_ok=None)
    soon = T0 + timedelta(minutes=5)
    assert pw.should_write_stamp(prev, soon, T0.isoformat(), None) is True


def test_brief_date_change_writes_immediately():
    prev = _stamp("VPS", T0, brief_date="2026-09-19")
    soon = T0 + timedelta(minutes=5)
    assert pw.should_write_stamp(prev, soon, None, "2026-09-20") is True


def test_unreadable_prior_tick_writes():
    prev = {"machine": "VPS", "last_tick_utc": "not-a-date"}
    assert pw.should_write_stamp(prev, T0, None, None) is True


# ── item 2 (2026-09-20): a model that keeps succeeding must NOT force a
# write every tick — only a genuine ok<->failing state flip does.


def test_model_still_ok_every_tick_does_not_force_a_write():
    """The original bug: last_model_ok_utc gets a fresh timestamp on every
    successful model call, and comparing raw timestamps treated every one of
    those as 'changed' -> ~32 writes/day instead of the intended ~2h cadence."""
    prev = _stamp("VPS", T0, model_ok=T0 - timedelta(minutes=30))
    soon = T0 + timedelta(minutes=30)  # well under WRITE_MIN_INTERVAL_HOURS
    fresh_success = soon.isoformat()  # this tick's own fresh success
    assert pw.should_write_stamp(prev, soon, fresh_success, None) is False


def test_model_recovering_from_a_stale_failure_writes_immediately():
    """prev's last_model_ok_utc was already >= MODEL_STALE_HOURS old as of
    `now` (a real failing streak) and a fresh success just landed — that IS
    a state flip and must be surfaced right away, not held for up to 2h."""
    prev = _stamp("VPS", T0, model_ok=T0 - timedelta(hours=pw.MODEL_STALE_HOURS + 1))
    soon = T0 + timedelta(minutes=5)
    fresh_success = soon.isoformat()
    assert pw.should_write_stamp(prev, soon, fresh_success, None) is True


def test_model_going_stale_with_unchanged_timestamp_waits_for_the_interval():
    """Transition INTO failing: last_model_ok_utc simply stops changing (the
    model stops succeeding). Caught by the routine 2h write, not immediately
    — see MODEL_STALE_HOURS's own comment for why that lag is safe."""
    unchanged = (T0 - timedelta(hours=1)).isoformat()
    prev = _stamp("VPS", T0, model_ok=T0 - timedelta(hours=1))
    just_under = T0 + timedelta(hours=pw.WRITE_MIN_INTERVAL_HOURS) - timedelta(minutes=1)
    assert pw.should_write_stamp(prev, just_under, unchanged, None) is False


# ── write_own_stamp: files in, respecting the cadence ────────────────────


def test_write_own_stamp_creates_and_then_holds_off(tmp_path):
    d = tmp_path / "liveness"
    s1 = pw.write_own_stamp(T0, last_model_ok_utc=None, last_brief_date=None,
                             machine="VPS", liveness_dir=d)
    assert s1 is not None
    assert json.loads((d / "vps.json").read_text())["machine"] == "VPS"

    soon = T0 + timedelta(minutes=10)
    s2 = pw.write_own_stamp(soon, last_model_ok_utc=None, last_brief_date=None,
                             machine="VPS", liveness_dir=d)
    assert s2 is None  # nothing changed, interval not up — file untouched
    assert json.loads((d / "vps.json").read_text())["last_tick_utc"] == s1["last_tick_utc"]


def test_write_own_stamp_fires_early_on_a_real_change(tmp_path):
    d = tmp_path / "liveness"
    pw.write_own_stamp(T0, last_model_ok_utc=None, last_brief_date=None, machine="VPS", liveness_dir=d)
    soon = T0 + timedelta(minutes=10)
    s2 = pw.write_own_stamp(soon, last_model_ok_utc=T0.isoformat(), last_brief_date=None,
                             machine="VPS", liveness_dir=d)
    assert s2 is not None
    assert json.loads((d / "vps.json").read_text())["last_model_ok_utc"] == T0.isoformat()


def test_read_stamp_missing_or_corrupt_is_none(tmp_path):
    d = tmp_path / "liveness"
    assert pw.read_stamp("VPS", liveness_dir=d) is None
    d.mkdir()
    (d / "vps.json").write_text("{not json")
    assert pw.read_stamp("VPS", liveness_dir=d) is None


# ── evaluate: fresh / stale / model failing / brief missing / Mac asleep ──


def test_missing_stamp_with_no_first_watched_known_is_an_alarm():
    """A direct evaluate() call with no first_watched_utc (bypassing run()'s
    own bookkeeping) has no grace period to grant — this is the same as
    "we don't know when we started watching", so it alarms immediately."""
    p = pw.evaluate("VPS", None, T0, T0.astimezone(PT))
    assert p and p["kind"] == "missing" and p["severity"] == "alarm"


# ── item 1 (2026-09-20): deploy-day false alarm — a missing stamp is quiet
# until this machine has been watching for at least the peer's own threshold.


def test_missing_stamp_is_quiet_within_the_grace_period():
    first_watched = T0
    just_inside = T0 + timedelta(hours=pw.STALE_HOURS["VPS"]) - timedelta(minutes=1)
    assert pw.evaluate("VPS", None, just_inside, just_inside.astimezone(PT),
                       first_watched_utc=first_watched) is None


def test_missing_stamp_alarms_once_the_grace_period_elapses():
    first_watched = T0
    past_grace = T0 + timedelta(hours=pw.STALE_HOURS["VPS"])
    p = pw.evaluate("VPS", None, past_grace, past_grace.astimezone(PT), first_watched_utc=first_watched)
    assert p and p["kind"] == "missing" and p["severity"] == "alarm"


def test_missing_stamp_grace_period_uses_the_watched_machines_own_threshold():
    """Watching the Mac (96h threshold) must grant the Mac's own grace
    period, not the VPS's shorter one."""
    first_watched = T0
    within_mac_grace = T0 + timedelta(hours=pw.STALE_HOURS["Mac"] - 1)
    assert pw.evaluate("Mac", None, within_mac_grace, within_mac_grace.astimezone(PT),
                       first_watched_utc=first_watched) is None


def test_run_deploy_day_is_quiet_no_false_alarm_then_no_false_recovery(env):
    """Reproduces the probed defect exactly: first tick after a fresh
    deploy, neither machine has the other's stamp. Must send nothing on the
    first tick, and — since nothing was ever alerted — nothing on later
    ticks either, even once a real stamp does show up (no false
    'back to normal')."""
    liveness, state = env
    sent = []

    out1 = pw.run(now=T0, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
                  liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out1 == {"alerted": [], "reminded": [], "recovered": []}
    assert sent == []

    # Some time later (still within the grace period) the VPS's real stamp
    # finally lands via the git sync.
    later = T0 + timedelta(hours=1)
    liveness.mkdir(exist_ok=True)
    (liveness / "vps.json").write_text(json.dumps(_stamp("VPS", later - timedelta(minutes=5))))
    out2 = pw.run(now=later, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
                  liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out2 == {"alerted": [], "reminded": [], "recovered": []}
    assert sent == []


def test_run_still_alarms_a_genuinely_never_reporting_peer(env):
    """The grace period is not silence forever — once it elapses with
    still no stamp at all, that is a real problem."""
    liveness, state = env
    sent = []
    pw.run(now=T0, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
          liveness_dir=liveness, state_path=state, owner_tz=PT)

    past_grace = T0 + timedelta(hours=pw.STALE_HOURS["VPS"])
    out = pw.run(now=past_grace, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
                liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out["alerted"] == ["VPS"]
    assert sent and "never reported" in sent[-1]


def test_fresh_tick_is_fine():
    stamp = _stamp("VPS", T0 - timedelta(hours=1))
    assert pw.evaluate("VPS", stamp, T0, T0.astimezone(PT)) is None


def test_vps_stale_tick_is_an_alarm_at_5h():
    stale = _stamp("VPS", T0 - timedelta(hours=30))
    p = pw.evaluate("VPS", stale, T0, T0.astimezone(PT))
    assert p and p["kind"] == "stale_tick" and p["severity"] == "alarm"


def test_mac_asleep_overnight_is_not_an_alarm():
    """The Mac sleeps by design — 20h of silence (well under its threshold)
    must not read as a problem, and must be worded as information even once
    it does cross the threshold."""
    asleep_20h = _stamp("Mac", T0 - timedelta(hours=20))
    assert pw.evaluate("Mac", asleep_20h, T0, T0.astimezone(PT)) is None

    past_threshold = _stamp("Mac", T0 - timedelta(hours=pw.STALE_HOURS["Mac"]))
    p = pw.evaluate("Mac", past_threshold, T0, T0.astimezone(PT))
    assert p and p["kind"] == "stale_tick" and p["severity"] == "info"


def test_mac_closed_over_an_ordinary_weekend_is_not_an_alarm():
    """item 4: Friday 6pm close -> Monday 9am reopen is 63h of entirely
    ordinary silence. The old 36h threshold fired on this; 96h (~4 days)
    does not, and the wording reads as information, not a failure."""
    friday_6pm_pt = datetime(2026, 9, 18, 18, 0, tzinfo=PT)  # a Friday
    monday_9am_pt = datetime(2026, 9, 21, 9, 0, tzinfo=PT)   # the following Monday
    assert (monday_9am_pt - friday_6pm_pt) < timedelta(hours=pw.STALE_HOURS["Mac"])
    stamp = _stamp("Mac", friday_6pm_pt.astimezone(timezone.utc))
    assert pw.evaluate("Mac", stamp, monday_9am_pt.astimezone(timezone.utc), monday_9am_pt) is None


def test_mac_stale_message_names_the_day_and_says_fine_if_closed():
    friday_6pm_pt = datetime(2026, 9, 18, 18, 0, tzinfo=PT)
    now_pt = friday_6pm_pt + timedelta(hours=pw.STALE_HOURS["Mac"] + 1)
    stamp = _stamp("Mac", friday_6pm_pt.astimezone(timezone.utc))
    p = pw.evaluate("Mac", stamp, now_pt.astimezone(timezone.utc), now_pt)
    assert p and p["severity"] == "info"
    assert "friday" in p["detail"].lower()
    assert "fine if it's been closed" in p["detail"]


def test_vps_stale_tick_outside_active_hours_is_not_an_alarm():
    """heartbeat.ACTIVE_HOURS means NEITHER machine ticks overnight — a 5h
    stale VPS check evaluated at 2 AM PT would otherwise false-alarm every
    single night. The check only fires during the watched machine's own
    active hours (6 AM - 10 PM PT)."""
    two_am_pt = datetime(2026, 9, 20, 2, 0, tzinfo=PT)
    stale = _stamp("VPS", two_am_pt.astimezone(timezone.utc) - timedelta(hours=pw.STALE_HOURS["VPS"] + 3))
    assert pw.evaluate("VPS", stale, two_am_pt.astimezone(timezone.utc), two_am_pt) is None


def test_vps_stale_tick_inside_active_hours_still_alarms():
    seven_am_pt = datetime(2026, 9, 20, 7, 0, tzinfo=PT)
    stale = _stamp("VPS", seven_am_pt.astimezone(timezone.utc) - timedelta(hours=30))
    p = pw.evaluate("VPS", stale, seven_am_pt.astimezone(timezone.utc), seven_am_pt)
    assert p and p["kind"] == "stale_tick" and p["severity"] == "alarm"


def test_model_failing_while_ticking_is_the_auth_expired_case():
    now = T0
    stamp = _stamp("VPS", now - timedelta(minutes=5), model_ok=now - timedelta(hours=30))
    p = pw.evaluate("VPS", stamp, now, now.astimezone(PT))
    assert p and p["kind"] == "model_stale" and p["severity"] == "alarm"


def test_model_ok_recent_is_fine_even_if_never_updated_is_not_penalised():
    now = T0
    stamp = _stamp("VPS", now - timedelta(minutes=5), model_ok=None)
    assert pw.evaluate("VPS", stamp, now, now.astimezone(PT)) is None


def test_brief_missing_only_fires_after_930_pt_for_vps():
    before_930 = datetime(2026, 9, 20, 9, 0, tzinfo=PT)  # 09:00 PT
    stamp = _stamp("VPS", before_930.astimezone(timezone.utc) - timedelta(minutes=5), brief_date="2026-09-19")
    assert pw.evaluate("VPS", stamp, before_930.astimezone(timezone.utc), before_930) is None

    after_930 = datetime(2026, 9, 20, 9, 31, tzinfo=PT)
    stamp2 = _stamp("VPS", after_930.astimezone(timezone.utc) - timedelta(minutes=5), brief_date="2026-09-19")
    p = pw.evaluate("VPS", stamp2, after_930.astimezone(timezone.utc), after_930)
    assert p and p["kind"] == "brief_missing" and p["severity"] == "alarm"


def test_brief_present_today_is_fine():
    after_930 = datetime(2026, 9, 20, 9, 31, tzinfo=PT)
    stamp = _stamp("VPS", after_930.astimezone(timezone.utc) - timedelta(minutes=5), brief_date="2026-09-20")
    assert pw.evaluate("VPS", stamp, after_930.astimezone(timezone.utc), after_930) is None


# ── item 3 (2026-09-20): untrusted peer data — future-dated or tz-less ──


def test_future_dated_stamp_is_flagged_not_silently_trusted():
    """A future last_tick_utc would otherwise produce a negative age that
    never crosses any staleness threshold — silencing the watcher entirely
    instead of alerting."""
    future = T0 + timedelta(hours=1000)
    stamp = _stamp("VPS", future)
    p = pw.evaluate("VPS", stamp, T0, T0.astimezone(PT))
    assert p and p["kind"] == "clock_wrong" and p["severity"] == "info"
    assert "future" in p["detail"]


def test_slightly_future_stamp_within_tolerance_is_fine():
    """Clock skew of a few minutes between machines is normal, not a
    problem — only drift beyond CLOCK_SKEW_TOLERANCE_MINUTES is flagged."""
    barely_future = T0 + timedelta(minutes=pw.CLOCK_SKEW_TOLERANCE_MINUTES - 1)
    stamp = _stamp("VPS", barely_future)
    assert pw.evaluate("VPS", stamp, T0, T0.astimezone(PT)) is None


def test_timezone_less_tick_is_rejected_not_read_as_utc():
    """Before this fix, a tz-naive last_tick_utc was silently assumed to be
    UTC — a real clock/config problem on the peer would read as healthy."""
    stamp = {"machine": "VPS", "last_tick_utc": (T0 - timedelta(minutes=5)).replace(tzinfo=None).isoformat(),
            "last_model_ok_utc": None, "last_brief_date": None}
    p = pw.evaluate("VPS", stamp, T0, T0.astimezone(PT))
    assert p and p["kind"] == "clock_wrong" and p["severity"] == "info"


def test_clock_wrong_reminder_cadence_is_once_per_24h():
    """clock_wrong flows through the same check()/REMIND_HOURS machinery as
    every other problem kind."""
    problem = {"kind": "clock_wrong", "severity": "info", "detail": "clock looks wrong on VPS"}
    msgs, state = pw.check(problem, {}, now=T0, watched_machine="VPS")
    assert len(msgs) == 1
    msgs2, _ = pw.check(problem, state, now=T0 + timedelta(hours=1), watched_machine="VPS")
    assert msgs2 == []


def test_brief_missing_check_is_scoped_to_vps():
    """The Mac is not the default brief owner — a Mac stamp with no
    last_brief_date must never trip the brief-missing check."""
    after_930 = datetime(2026, 9, 20, 9, 31, tzinfo=PT)
    stamp = _stamp("Mac", after_930.astimezone(timezone.utc) - timedelta(minutes=5), brief_date=None)
    assert pw.evaluate("Mac", stamp, after_930.astimezone(timezone.utc), after_930) is None


# ── check: alert once, remind once a day, recover once ──────────────────


def test_check_silent_when_no_problem():
    msgs, state = pw.check(None, {}, now=T0, watched_machine="VPS")
    assert msgs == [] and state["alerted"] == {}


def test_check_alerts_once_with_takeover_hint():
    problem = {"kind": "stale_tick", "severity": "alarm", "detail": "VPS last ticked 6.0h ago"}
    msgs, state = pw.check(problem, {}, now=T0, watched_machine="VPS")
    assert len(msgs) == 1
    assert "VPS last ticked" in msgs[0]
    assert "CORTEX_BRIEF_OWNER" in msgs[0]
    assert "VPS" in state["alerted"]

    # Same problem next tick: nothing new.
    msgs2, state2 = pw.check(problem, state, now=T0 + timedelta(minutes=30), watched_machine="VPS")
    assert msgs2 == []


def test_check_info_severity_has_no_takeover_hint():
    problem = {"kind": "stale_tick", "severity": "info", "detail": "Mac last ticked 40.0h ago"}
    msgs, _ = pw.check(problem, {}, now=T0, watched_machine="Mac")
    assert len(msgs) == 1 and "ℹ️" in msgs[0] and "CORTEX_BRIEF_OWNER" not in msgs[0]


def test_check_hint_never_shown_for_a_mac_alarm():
    """item 5 corollary: even an ALARM-severity problem about the Mac (e.g.
    model_stale, which is not gated to info by watched_machine) must not
    suggest 'move things to the Mac' — that machine IS the one broken."""
    problem = {"kind": "model_stale", "severity": "alarm", "detail": "Mac's model keeps failing"}
    msgs, _ = pw.check(problem, {}, now=T0, watched_machine="Mac")
    assert len(msgs) == 1 and "CORTEX_BRIEF_OWNER" not in msgs[0]


def test_takeover_hint_says_what_it_does_and_does_not_move():
    problem = {"kind": "stale_tick", "severity": "alarm", "detail": "VPS stale"}
    msgs, _ = pw.check(problem, {}, now=T0, watched_machine="VPS")
    hint = msgs[0]
    assert "CORTEX_REFLECTION_OWNER" in hint
    assert "CORTEX_CONTENT_WEEKLY_OWNER" in hint
    assert "does NOT move" in hint


def test_check_reminds_once_per_24h():
    problem = {"kind": "stale_tick", "severity": "alarm", "detail": "VPS stale"}
    _, state = pw.check(problem, {}, now=T0, watched_machine="VPS")
    too_soon = T0 + timedelta(hours=pw.REMIND_HOURS - 1)
    msgs, _ = pw.check(problem, state, now=too_soon, watched_machine="VPS")
    assert msgs == []

    due = T0 + timedelta(hours=pw.REMIND_HOURS, minutes=1)
    msgs2, state2 = pw.check(problem, state, now=due, watched_machine="VPS")
    assert len(msgs2) == 1 and "Still" in msgs2[0]
    assert state2["alerted"]["VPS"]["at"] == due.isoformat()


def test_check_recovers_once_and_forgets():
    problem = {"kind": "stale_tick", "severity": "alarm", "detail": "VPS stale"}
    _, state = pw.check(problem, {}, now=T0, watched_machine="VPS")
    msgs, state2 = pw.check(None, state, now=T0 + timedelta(hours=1), watched_machine="VPS")
    assert len(msgs) == 1 and "back to normal" in msgs[0]
    assert state2["alerted"] == {}
    msgs3, _ = pw.check(None, state2, now=T0 + timedelta(hours=2), watched_machine="VPS")
    assert msgs3 == []


# ── run: files + notify in, respecting send-then-stamp ───────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    liveness = tmp_path / "liveness"
    state = tmp_path / "peer-watch.json"
    return liveness, state


def test_run_alerts_on_stale_peer_and_persists_state(env):
    liveness, state = env
    liveness.mkdir()
    stale = T0 - timedelta(hours=30)
    (liveness / "vps.json").write_text(json.dumps(_stamp("VPS", stale)))
    sent = []
    out = pw.run(now=T0, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
                 liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out["alerted"] == ["VPS"] and len(sent) == 1
    assert json.loads(state.read_text())["alerted"]["VPS"]["kind"] == "stale_tick"


def test_run_send_fails_leaves_state_untouched_for_retry(env):
    liveness, state = env
    liveness.mkdir()
    stale = T0 - timedelta(hours=30)
    (liveness / "vps.json").write_text(json.dumps(_stamp("VPS", stale)))
    out = pw.run(now=T0, notify_fn=lambda m: False, own_machine="Mac",
                 liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out["alerted"] == []
    assert not state.exists()


def test_run_is_quiet_with_no_peer_stamp_missing_directory(env):
    """A brand-new liveness dir (e.g. right after this feature ships) is
    within the grace period on the very first call (item 1) — quiet, not an
    immediate alarm. See test_run_deploy_day_is_quiet_* above for the full
    scenario and test_run_still_alarms_a_genuinely_never_reporting_peer for
    the eventual-alarm case. This just proves run() doesn't crash."""
    liveness, state = env
    sent = []
    out = pw.run(now=T0, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
                 liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out == {"alerted": [], "reminded": [], "recovered": []}
    assert sent == []


def test_run_never_raises_on_broken_state_file(env):
    liveness, state = env
    liveness.mkdir()
    (liveness / "vps.json").write_text(json.dumps(_stamp("VPS", T0 - timedelta(minutes=5))))
    state.write_text("{not json")
    out = pw.run(now=T0, notify_fn=lambda m: True, own_machine="Mac",
                 liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out == {"alerted": [], "reminded": [], "recovered": []}


def test_run_recovers_across_two_calls(env):
    liveness, state = env
    liveness.mkdir()
    stale = T0 - timedelta(hours=30)
    (liveness / "vps.json").write_text(json.dumps(_stamp("VPS", stale)))
    sent = []
    pw.run(now=T0, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
          liveness_dir=liveness, state_path=state, owner_tz=PT)

    fresh = T0 + timedelta(hours=1)
    (liveness / "vps.json").write_text(json.dumps(_stamp("VPS", fresh - timedelta(minutes=1))))
    out2 = pw.run(now=fresh, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
                  liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out2["recovered"] == ["VPS"]
    assert "back to normal" in sent[-1]


# ── wiring ─────────────────────────────────────────────────────────────


def test_heartbeat_runs_peer_watch_under_its_own_feature_gate():
    src = (REPO_ROOT / ".claude" / "scripts" / "heartbeat.py").read_text()
    assert 'feature_run("health.peer_watch"' in src
    # Placed before the nothing-changed early exit — same section as the
    # other always-on health lanes, not inside the big try: block below it.
    peer_pos = src.index('feature_run("health.peer_watch"')
    quiet_exit_pos = src.index('debug("no changes — skipping SDK call")')
    assert peer_pos < quiet_exit_pos


def test_liveness_dir_is_not_gitignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    for line in gitignore.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "liveness" not in line, f".gitignore line {line!r} would hide the liveness stamps"


# -- Overnight is not silence --------------------------------------------------


def _at(y, m, d, hh, mm=0):
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo
    pt = _dt(y, m, d, hh, mm, tzinfo=ZoneInfo("America/Los_Angeles"))
    return pt.astimezone(_tz.utc), pt


def test_first_tick_of_the_morning_does_not_alarm_about_last_nights_vps_stamp():
    last_night_utc, _ = _at(2026, 9, 21, 21, 45)
    now_utc, now_pt = _at(2026, 9, 22, 6, 5)
    stamp = {"last_tick_utc": last_night_utc.isoformat(), "last_model_ok_utc": last_night_utc.isoformat(),
             "last_brief_date": "2026-09-21"}
    assert pw.evaluate("VPS", stamp, now_utc, now_pt) is None


def test_vps_silent_for_five_waking_hours_is_an_alarm():
    last_utc, _ = _at(2026, 9, 21, 21, 0)          # 1 waking hour last night
    now_utc, now_pt = _at(2026, 9, 22, 10, 30)     # + 4.5 this morning
    stamp = {"last_tick_utc": last_utc.isoformat(), "last_model_ok_utc": last_utc.isoformat(),
             "last_brief_date": "2026-09-22"}
    out = pw.evaluate("VPS", stamp, now_utc, now_pt)
    assert out and out["kind"] == "stale_tick" and out["severity"] == "alarm"


def test_a_closed_mac_is_never_reported_as_ticking_with_a_failing_model():
    closed_utc, _ = _at(2026, 9, 21, 21, 30)
    now_utc, now_pt = _at(2026, 9, 22, 6, 30)
    stamp = {"last_tick_utc": closed_utc.isoformat(), "last_model_ok_utc": closed_utc.isoformat()}
    assert pw.evaluate("Mac", stamp, now_utc, now_pt) is None
    fri_utc, _ = _at(2026, 9, 18, 18, 0)             # and over a weekend
    mon_utc, mon_pt = _at(2026, 9, 21, 9, 0)
    stamp = {"last_tick_utc": fri_utc.isoformat(), "last_model_ok_utc": fri_utc.isoformat()}
    assert pw.evaluate("Mac", stamp, mon_utc, mon_pt) is None


def test_vps_ticking_with_a_model_that_has_failed_all_day_is_an_alarm():
    now_utc, now_pt = _at(2026, 9, 22, 15, 0)
    tick_utc, _ = _at(2026, 9, 22, 14, 0)
    model_utc, _ = _at(2026, 9, 22, 7, 0)
    stamp = {"last_tick_utc": tick_utc.isoformat(), "last_model_ok_utc": model_utc.isoformat(),
             "last_brief_date": "2026-09-22"}
    out = pw.evaluate("VPS", stamp, now_utc, now_pt)
    assert out and out["kind"] == "model_stale" and out["severity"] == "alarm"


def test_active_hours_between_counts_only_the_waking_window():
    _, a = _at(2026, 9, 21, 21, 0)
    _, b = _at(2026, 9, 22, 7, 0)
    assert pw._active_hours_between(a, b) == 2.0      # 21-22 + 06-07
    assert pw._active_hours_between(b, a) == 0.0


# ── Single-machine deployments stay silent by default ────────────────────
#
# This repo may be installed on a single machine. A peer that has never
# reported must never alarm unless CORTEX_PEER_MACHINE says a second
# machine actually exists — otherwise a lone install would eventually alarm
# about a peer that was never going to report, because it doesn't exist.


def test_peer_configured_is_false_when_var_is_unset(monkeypatch):
    monkeypatch.delenv("CORTEX_PEER_MACHINE", raising=False)
    assert pw.peer_configured() is False


def test_peer_configured_is_false_for_blank_or_whitespace(monkeypatch):
    monkeypatch.setenv("CORTEX_PEER_MACHINE", "")
    assert pw.peer_configured() is False
    monkeypatch.setenv("CORTEX_PEER_MACHINE", "   ")
    assert pw.peer_configured() is False


def test_peer_configured_is_true_for_any_non_blank_value(monkeypatch):
    monkeypatch.setenv("CORTEX_PEER_MACHINE", "vps")
    assert pw.peer_configured() is True
    monkeypatch.setenv("CORTEX_PEER_MACHINE", "1")
    assert pw.peer_configured() is True


def test_run_is_a_pure_noop_on_a_single_machine_install(monkeypatch, env):
    """Unconfigured: run() must not read or write anything, and must never
    alert, no matter how stale or absent the (nonexistent) peer's stamp is."""
    monkeypatch.delenv("CORTEX_PEER_MACHINE", raising=False)
    liveness, state = env
    sent = []
    out = pw.run(now=T0, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
                 liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out == {"alerted": [], "reminded": [], "recovered": []}
    assert sent == []
    assert not state.exists()


def test_run_stays_silent_across_many_ticks_when_unconfigured(monkeypatch, env):
    """The deviation this exists for: the grace period in evaluate() would
    normally expire and alarm about a peer that has never reported. On a
    single-machine install that must never happen, at any elapsed time."""
    monkeypatch.delenv("CORTEX_PEER_MACHINE", raising=False)
    liveness, state = env
    sent = []
    far_future = T0 + timedelta(days=365)
    out = pw.run(now=far_future, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
                 liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out == {"alerted": [], "reminded": [], "recovered": []}
    assert sent == []
    assert not state.exists()


def test_run_starts_working_once_a_peer_machine_is_configured(monkeypatch, env):
    """Flipping CORTEX_PEER_MACHINE on turns the watcher on immediately —
    no code change, matching the module's own documented contract."""
    liveness, state = env
    liveness.mkdir()
    stale = T0 - timedelta(hours=30)
    (liveness / "vps.json").write_text(json.dumps(_stamp("VPS", stale)))
    sent = []

    monkeypatch.delenv("CORTEX_PEER_MACHINE", raising=False)
    out_off = pw.run(now=T0, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
                     liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out_off == {"alerted": [], "reminded": [], "recovered": []}
    assert sent == []

    monkeypatch.setenv("CORTEX_PEER_MACHINE", "vps")
    out_on = pw.run(now=T0, notify_fn=lambda m: sent.append(m) or True, own_machine="Mac",
                    liveness_dir=liveness, state_path=state, owner_tz=PT)
    assert out_on["alerted"] == ["VPS"]
    assert sent


def test_write_own_stamp_is_not_gated_on_peer_configured(monkeypatch, tmp_path):
    """write_own_stamp() itself always behaves the same regardless of
    CORTEX_PEER_MACHINE — heartbeat.py's own call site is what decides
    whether to invoke it at all (see the module docstring)."""
    monkeypatch.delenv("CORTEX_PEER_MACHINE", raising=False)
    d = tmp_path / "liveness"
    s1 = pw.write_own_stamp(T0, last_model_ok_utc=None, last_brief_date=None,
                             machine="VPS", liveness_dir=d)
    assert s1 is not None
    assert json.loads((d / "vps.json").read_text())["machine"] == "VPS"


def test_heartbeat_gates_peer_watch_on_peer_configured():
    src = (REPO_ROOT / ".claude" / "scripts" / "heartbeat.py").read_text()
    idx = src.index('feature_run("health.peer_watch"')
    # The wrapped step function is defined just above the feature_run call;
    # it must check peer_configured() before doing anything else.
    body_start = src.rindex("def _peer_watch():", 0, idx)
    body = src[body_start:idx]
    assert "peer_configured()" in body
