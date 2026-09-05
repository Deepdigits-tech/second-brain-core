"""Off-hours cadence: every 30 min inside work hours, hourly outside.

The launchd job fires every 1800s from load, so ticks drift off clock
boundaries. These pin the elapsed-time gate — a minute-of-hour check would
pass these on a lucky schedule and silently skip whole evenings on an unlucky
one.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import heartbeat  # noqa: E402


@pytest.fixture
def throttle(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "THROTTLE_STATE_PATH", tmp_path / "throttle.json")
    return tmp_path / "throttle.json"


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, tzinfo=heartbeat.PST)


def write_last_run(path: Path, when: datetime) -> None:
    path.write_text(json.dumps({"last_full_run_at": when.isoformat()}))


# ── inside work hours: never throttled ────────────────────────────────


@pytest.mark.parametrize("hour", [7, 9, 12, 17, 20])
def test_inside_work_hours_always_runs(throttle, hour):
    """Even one minute after the previous run — 30-min cadence is preserved."""
    write_last_run(throttle, at(hour, 0))
    assert heartbeat._should_run_full_tick(at(hour, 1)) is True


def test_work_hours_boundaries():
    """7am is in; 9pm is out (the window is half-open, like ACTIVE_HOURS)."""
    assert heartbeat.WORK_HOURS == (7, 21)


def test_seven_am_is_inside(throttle):
    write_last_run(throttle, at(7, 0))
    assert heartbeat._should_run_full_tick(at(7, 5)) is True


def test_nine_pm_is_outside(throttle):
    write_last_run(throttle, at(21, 0))
    assert heartbeat._should_run_full_tick(at(21, 30)) is False


# ── outside work hours: hourly ────────────────────────────────────────


def test_offhours_skips_the_half_hour_tick(throttle):
    write_last_run(throttle, at(22, 0))
    assert heartbeat._should_run_full_tick(at(22, 30)) is False


def test_offhours_runs_after_the_gap(throttle):
    write_last_run(throttle, at(22, 0))
    assert heartbeat._should_run_full_tick(at(22, 56)) is True


def test_offhours_gap_is_under_an_hour(throttle):
    """55 min, not 60: a tick landing at 59m30s must not defer a whole hour."""
    assert heartbeat.OFF_HOURS_GAP_MINUTES < 60
    write_last_run(throttle, at(6, 0))
    assert heartbeat._should_run_full_tick(at(6, 0) + timedelta(minutes=59)) is True


def test_drifted_ticks_still_run_hourly(throttle):
    """The real failure mode: ticks that never land near the top of the hour.

    A minute-of-hour gate would skip every one of these. Simulate launchd
    firing at :17 and :47 and confirm roughly one run per hour survives.
    """
    ran = []
    tick = at(22, 17)
    for _ in range(6):  # 3 hours of 30-min ticks, all off-boundary
        if heartbeat._should_run_full_tick(tick):
            ran.append(tick)
            write_last_run(throttle, tick)
        tick += timedelta(minutes=30)
    assert len(ran) == 3, f"expected ~1 run/hour, got {len(ran)}: {ran}"


# ── missing / corrupt state must not wedge the heartbeat ──────────────


def test_missing_state_runs(throttle):
    assert heartbeat._should_run_full_tick(at(23, 0)) is True


def test_corrupt_state_runs(throttle):
    throttle.write_text("{not json")
    assert heartbeat._should_run_full_tick(at(23, 0)) is True


def test_state_missing_key_runs(throttle):
    throttle.write_text(json.dumps({"something_else": 1}))
    assert heartbeat._should_run_full_tick(at(23, 0)) is True


def test_unparseable_timestamp_runs(throttle):
    throttle.write_text(json.dumps({"last_full_run_at": "not-a-date"}))
    assert heartbeat._should_run_full_tick(at(23, 0)) is True


# ── recording ─────────────────────────────────────────────────────────


def test_record_writes_a_readable_timestamp(throttle):
    heartbeat._record_full_tick(at(22, 30))
    assert heartbeat._should_run_full_tick(at(22, 45)) is False
    assert heartbeat._should_run_full_tick(at(23, 30)) is True


def test_record_failure_does_not_raise(tmp_path, monkeypatch):
    """A bad state path must not take the heartbeat down with it."""
    monkeypatch.setattr(
        heartbeat, "THROTTLE_STATE_PATH", tmp_path / "nope" / "\0bad" / "throttle.json"
    )
    heartbeat._record_full_tick(at(22, 0))  # must not raise
