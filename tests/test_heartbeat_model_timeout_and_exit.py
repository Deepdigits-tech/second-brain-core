"""Structural checks: the main SDK call has an overall timeout, a failed
outer exception exits non-zero, and the model call + notification dispatch
are wrapped in feature_run under their own names so feature_health.py can
see them fail.

These are source-level checks rather than full end-to-end heartbeat.main()
runs — this repo has no heartbeat_harness fixture (the source's harness
stubs a much larger set of inbox/tasks/priorities machinery that does not
exist here), so a full functional replay of a timed-out tick is out of
scope. What is checked here is exactly what a reader auditing the fix would
look for: the timeout constant is real and used, the two new lanes are
wrapped, and the exit-code wiring is the narrow `is False` check the ticket
requires (not general falsiness, which would fail every ordinary quiet
tick).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / ".claude" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import heartbeat as hb  # noqa: E402

HEARTBEAT_SRC = (SCRIPTS / "heartbeat.py").read_text()


def test_heartbeat_model_timeout_constant_is_generous_but_under_the_tick_interval():
    # 900s (15 min) is comfortably under the 30-min tick interval, and well
    # above a normal model call (seconds to low minutes).
    assert hb.HEARTBEAT_MODEL_TIMEOUT_S == 900


def test_model_call_is_wrapped_in_asyncio_wait_for_with_the_timeout_constant():
    assert "asyncio.wait_for(" in HEARTBEAT_SRC
    assert "timeout=HEARTBEAT_MODEL_TIMEOUT_S" in HEARTBEAT_SRC


def test_timeout_mechanism_actually_cancels_a_hung_coroutine():
    """A narrow functional check of the exact mechanism main() relies on —
    not a full heartbeat tick, but proof asyncio.wait_for(..., timeout=X)
    does what the wrapper around run_heartbeat() assumes it does."""

    async def _hangs():
        await asyncio.sleep(10)
        return ["should never be reached"]

    async def _run():
        try:
            return await asyncio.wait_for(_hangs(), timeout=0.02)
        except asyncio.TimeoutError:
            return []

    assert asyncio.run(_run()) == []


def test_model_call_runs_under_its_own_feature_run_name():
    assert 'feature_run(\n            "heartbeat.model_call"' in HEARTBEAT_SRC \
        or '"heartbeat.model_call"' in HEARTBEAT_SRC
    assert "def _run_model_call_step" in HEARTBEAT_SRC


def test_notify_dispatch_runs_under_its_own_feature_run_name():
    assert '"heartbeat.notify_dispatch"' in HEARTBEAT_SRC
    assert "def _run_notify_dispatch_step" in HEARTBEAT_SRC


def test_main_returns_true_at_the_end_of_a_successful_tick():
    src = HEARTBEAT_SRC
    idx = src.index('debug("heartbeat complete")')
    tail = src[idx:idx + 120]
    assert "return True" in tail


def test_outer_except_returns_false_not_none():
    idx = HEARTBEAT_SRC.rindex("except Exception:")
    tail = HEARTBEAT_SRC[idx:idx + 800]
    assert "return False" in tail


def test_dunder_main_exit_wiring_checks_is_false_not_falsiness():
    """A bare `is False` comparison, not truthiness, or every ordinary quiet
    tick (bare `return` -> None, which is falsy) would exit 1 too."""
    assert "sys.exit(1 if main() is False else 0)" in HEARTBEAT_SRC


def test_no_restart_directive_on_the_oneshot_unit():
    """A non-zero exit must not trigger a restart loop — Restart= must be
    absent from the systemd unit (a oneshot with Restart= would retry-loop a
    real, repeatable failure instead of waiting for the next scheduled tick)."""
    unit = (SCRIPTS.parent / "systemd" / "secondbrain-heartbeat.service").read_text()
    directives = [line.split("=", 1)[0].strip() for line in unit.splitlines()
                  if "=" in line and not line.strip().startswith("#")]
    assert "Restart" not in directives


def test_systemd_unit_has_a_start_timeout():
    unit = (SCRIPTS.parent / "systemd" / "secondbrain-heartbeat.service").read_text()
    assert "TimeoutStartSec=" in unit


def test_systemd_unit_timeout_is_1500_seconds():
    unit = (SCRIPTS.parent / "systemd" / "secondbrain-heartbeat.service").read_text()
    assert "TimeoutStartSec=1500" in unit
