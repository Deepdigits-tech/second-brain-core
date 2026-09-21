"""_event_start_is_future must treat a tz-naive start as PST, not UTC.

GCal events always carry a UTC offset on their dateTime; a naive value here
is already a degraded one (something upstream dropped the offset). Treating
it as UTC silently shifts it by up to 8 hours (PST) / 7 (PDT), which can flip
"still upcoming" to "already past" right around the shift boundary — and
this function decides whether a calendar event that vanished from the fetch
window was cancelled (still-future start) or just rolled off normally
(already-past start).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import heartbeat  # noqa: E402
from shared import PST  # noqa: E402


def test_naive_datetime_close_to_midnight_reads_as_pst_not_utc():
    # 2026-09-20 23:30 PST is 2026-09-21 06:30 or 07:30 UTC (depending on
    # DST) — i.e. tomorrow in UTC. `now` is set to just after that PST
    # instant. Treated as UTC, the naive start would read as tomorrow
    # morning and still be "future"; treated as PST (correct), it's already
    # 5 minutes in the past.
    naive_start = "2026-09-20T23:30:00"
    now_pst = datetime(2026, 9, 20, 23, 35, tzinfo=PST)
    now_utc = now_pst.astimezone(timezone.utc)

    assert heartbeat._event_start_is_future(naive_start, now_utc) is False


def test_naive_datetime_still_ahead_in_pst_reads_as_future():
    naive_start = "2026-09-20T23:30:00"
    now_pst = datetime(2026, 9, 20, 20, 0, tzinfo=PST)  # 3.5h before, in PST
    now_utc = now_pst.astimezone(timezone.utc)

    assert heartbeat._event_start_is_future(naive_start, now_utc) is True


def test_offset_aware_datetime_is_unaffected():
    # A properly-formed GCal dateTime (with offset) must still work exactly
    # as before — this fix only changes the naive-value fallback.
    aware_start = "2026-09-20T23:30:00-07:00"
    now = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)  # 23:00 PDT — still before
    assert heartbeat._event_start_is_future(aware_start, now) is True

    now_later = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)  # well after
    assert heartbeat._event_start_is_future(aware_start, now_later) is False


def test_all_day_date_is_the_owners_pst_day_not_a_utc_day():
    # A bare "YYYY-MM-DD" is an all-day event's date, always the owner's
    # own day.
    today_pst_evening = datetime(2026, 9, 20, 23, 0, tzinfo=PST)
    now_utc = today_pst_evening.astimezone(timezone.utc)
    assert heartbeat._event_start_is_future("2026-09-20", now_utc) is True
    assert heartbeat._event_start_is_future("2026-09-19", now_utc) is False
