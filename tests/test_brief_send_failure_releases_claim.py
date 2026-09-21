"""REL-09 (2026-09-20): a model-OK tick whose brief send fails must not lose
the brief claim for the day.

claim_brief()/last_*_brief_date are committed BEFORE the model even runs
(main() has to know whether to ask for a brief). The brief itself is
delivered as exactly one NOTIFICATION line, parsed and sent by
parse_and_send_notifications(). Before this fix, a model-OK tick whose
actual Telegram/macOS send then failed left the claim and the
last_morning_brief_date stamp in place with nothing ever delivered — the
brief was lost until the next calendar day.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import heartbeat  # noqa: E402
import brief_coord  # noqa: E402


def test_parse_and_send_notifications_reports_a_failed_morning_brief_send(tmp_path, monkeypatch):
    monkeypatch.setattr(brief_coord, "COORD_PATH", tmp_path / "brief-coordination.json")
    import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", lambda title, message: {"telegram": False, "macos": False})

    out = heartbeat.parse_and_send_notifications(
        ["NOTIFICATION: Morning Brief | Good morning. Here's your day."]
    )
    assert out == {"brief_send_failed": "morning"}


def test_parse_and_send_notifications_reports_a_failed_afternoon_brief_send(tmp_path, monkeypatch):
    monkeypatch.setattr(brief_coord, "COORD_PATH", tmp_path / "brief-coordination.json")
    import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", lambda title, message: {"telegram": False})

    out = heartbeat.parse_and_send_notifications(
        ["NOTIFICATION: Afternoon Check-in | Quick update."]
    )
    assert out == {"brief_send_failed": "afternoon"}


def test_parse_and_send_notifications_no_failure_reported_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(brief_coord, "COORD_PATH", tmp_path / "brief-coordination.json")
    import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", lambda title, message: {"telegram": True})

    out = heartbeat.parse_and_send_notifications(
        ["NOTIFICATION: Morning Brief | Good morning."]
    )
    assert out == {"brief_send_failed": None}


def test_parse_and_send_notifications_ordinary_notification_never_reports_brief_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(brief_coord, "COORD_PATH", tmp_path / "brief-coordination.json")
    import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", lambda title, message: {"telegram": False})

    out = heartbeat.parse_and_send_notifications(["NOTIFICATION: Reminder | do the thing"])
    assert out == {"brief_send_failed": None}


def test_release_brief_after_reported_failure_lets_next_tick_reclaim(tmp_path, monkeypatch):
    """The composed behaviour main() relies on: release_brief() undoes the
    claim taken earlier in the same tick, so the next tick's claim_brief()
    succeeds again."""
    monkeypatch.setattr(brief_coord, "COORD_PATH", tmp_path / "brief-coordination.json")
    assert brief_coord.claim_brief("morning") is True

    import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", lambda title, message: {"telegram": False})
    out = heartbeat.parse_and_send_notifications(
        ["NOTIFICATION: Morning Brief | Good morning."]
    )
    assert out["brief_send_failed"] == "morning"

    brief_coord.release_brief(out["brief_send_failed"])
    assert brief_coord.claim_brief("morning") is True
