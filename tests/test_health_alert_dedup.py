"""Health alert dedup: notify per check, not per joined string.

The old dedup compared every alert joined into one string, so an unrelated
check appearing or clearing rewrote it and re-sent everything. On 2026-08-16 a
single expired token arrived four times in three hours because
heartbeat_freshness kept joining and leaving that string. These pin the
per-check behaviour and the cooldown.
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

TOKEN = "claude_auth: Claude token expired — run `claude setup-token`"
FRESH = "heartbeat_freshness: last heartbeat 1.3h ago"


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """Isolate alert state and record notifications instead of sending them."""
    monkeypatch.setattr(heartbeat, "HEALTH_ALERT_STATE_PATH", tmp_path / "alerts.json")
    calls = []
    import notify as notify_mod

    monkeypatch.setattr(
        notify_mod, "notify",
        lambda title, msg, channels=None, parse_mode="Markdown": calls.append((msg, parse_mode)),
    )
    return calls


def messages(calls):
    return [m for m, _ in calls]


# ── the reported bug ──────────────────────────────────────────────────


def test_unrelated_check_flapping_does_not_resend(sent):
    """The exact 2026-08-16 spam: freshness joins, then clears."""
    heartbeat._notify_new_health_alerts([TOKEN])
    heartbeat._notify_new_health_alerts([TOKEN, FRESH])   # freshness joins
    heartbeat._notify_new_health_alerts([TOKEN])          # freshness clears

    # The token alert is sent once; only the genuinely new freshness alert follows.
    assert len(sent) == 2
    assert TOKEN in messages(sent)[0]
    assert messages(sent)[1] == FRESH


def test_repeated_identical_alert_is_sent_once(sent):
    for _ in range(4):
        heartbeat._notify_new_health_alerts([TOKEN])
    assert len(sent) == 1


def test_only_the_new_alert_is_included(sent):
    heartbeat._notify_new_health_alerts([TOKEN])
    heartbeat._notify_new_health_alerts([TOKEN, FRESH])
    assert TOKEN not in messages(sent)[1]


# ── cooldown ──────────────────────────────────────────────────────────


def test_stuck_problem_resurfaces_after_the_cooldown(sent, tmp_path):
    """Silence forever would hide a problem that was never fixed."""
    heartbeat._notify_new_health_alerts([TOKEN])
    stale = (datetime.now(heartbeat.PST) - timedelta(hours=9)).isoformat()
    (tmp_path / "alerts.json").write_text(json.dumps({"sent": {"claude_auth": stale}}))
    heartbeat._notify_new_health_alerts([TOKEN])
    assert len(sent) == 2


def test_within_cooldown_stays_quiet(sent, tmp_path):
    heartbeat._notify_new_health_alerts([TOKEN])
    recent = (datetime.now(heartbeat.PST) - timedelta(hours=1)).isoformat()
    (tmp_path / "alerts.json").write_text(json.dumps({"sent": {"claude_auth": recent}}))
    heartbeat._notify_new_health_alerts([TOKEN])
    assert len(sent) == 1


def test_cleared_alert_re_alerts_when_it_returns(sent):
    """A fixed-then-broken problem must ping again, not sit under a stale stamp."""
    heartbeat._notify_new_health_alerts([TOKEN])
    heartbeat._notify_new_health_alerts([])       # resolved
    heartbeat._notify_new_health_alerts([TOKEN])  # broke again
    assert len(sent) == 2


# ── keying and formatting ─────────────────────────────────────────────


def test_reworded_detail_does_not_re_alert(sent):
    """Keyed on the check name — the detail text moves around."""
    heartbeat._notify_new_health_alerts(["claude_auth: token expired at 5pm"])
    heartbeat._notify_new_health_alerts(["claude_auth: token expired at 6pm"])
    assert len(sent) == 1


def test_sent_without_markdown_so_identifiers_survive(sent):
    heartbeat._notify_new_health_alerts([TOKEN])
    assert sent[0][1] is None


# ── robustness ────────────────────────────────────────────────────────


def test_corrupt_state_still_notifies(sent, tmp_path):
    (tmp_path / "alerts.json").write_text("{not json")
    heartbeat._notify_new_health_alerts([TOKEN])
    assert len(sent) == 1


def test_unparseable_timestamp_treated_as_never_sent(sent, tmp_path):
    (tmp_path / "alerts.json").write_text(json.dumps({"sent": {"claude_auth": "nope"}}))
    heartbeat._notify_new_health_alerts([TOKEN])
    assert len(sent) == 1


def test_no_alerts_sends_nothing(sent):
    heartbeat._notify_new_health_alerts([])
    assert sent == []
