"""notify.delivered(): Telegram-authoritative send-success, not any().

Before 2026-09-20 every send-then-stamp check in heartbeat.py used
`any(result.values())` against `notify()`'s `{"macos": bool, "telegram":
bool}` result. On the Mac, `notify()` defaults to `channels=["macos",
"telegram"]`, and `notify_macos()` (osascript) succeeds almost regardless of
network state — so a dead Telegram bot token still made every "did it send"
check read as delivered, and every stamp (a reminder fired, a health-alert
cooldown started, a brief claimed as sent) silently went ahead with nothing
actually reaching the owner's phone.

Ported from the private Cortex repo's batch-2 reliability fixes. The source
also carries a reminder-lane regression test driven through the whole-heartbeat
harness (tests/heartbeat_harness.py) and reminders.py — this target has
neither, so that case is dropped; the heartbeat's own _notify_new_health_alerts
and parse_and_send_notifications call sites are covered directly in
tests/test_send_then_stamp_lanes.py instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import notify  # noqa: E402


def test_the_mac_bug_telegram_failed_macos_succeeded_is_not_delivered():
    # This is the exact shape a Mac notify() call returns when osascript
    # works but the Telegram bot token is dead — the bug this helper exists
    # to fix.
    assert notify.delivered({"macos": True, "telegram": False}) is False


def test_both_channels_succeeded_is_delivered():
    assert notify.delivered({"macos": True, "telegram": True}) is True


def test_telegram_succeeded_macos_failed_is_still_delivered():
    # Telegram is the channel every stamp actually promises — a local
    # notification failing (e.g. headless VPS with no notify_macos support)
    # must not block the stamp.
    assert notify.delivered({"macos": False, "telegram": True}) is True


def test_telegram_only_channel_failed_is_not_delivered():
    assert notify.delivered({"telegram": False}) is False


def test_telegram_only_channel_succeeded_is_delivered():
    assert notify.delivered({"telegram": True}) is True


def test_macos_only_call_falls_back_to_any():
    # No "telegram" key at all (channels=["macos"]) — nothing more specific
    # to check than whether the one channel attempted succeeded.
    assert notify.delivered({"macos": True}) is True
    assert notify.delivered({"macos": False}) is False


def test_empty_or_none_result_is_never_delivered():
    assert notify.delivered({}) is False
    assert notify.delivered(None) is False
