"""REL-03 (2026-09-20): changed and cancelled meetings must not be invisible.

Before this fix `build_snapshot()` stored bare event ids in one set and
`diff_snapshots()` was a plain set difference — an event moved from 09:00 to
11:00 (same id, same set membership) or cancelled (same id, still present with
status="cancelled") gave `has_changes = False`, and the same event id showing
up on two different calendar accounts collapsed into a single set entry.

`build_snapshot()` now fingerprints each event (start, end, status, location,
the owner's own RSVP) under an account-scoped key ("<account>:<id>"), and
`diff_snapshots()` reports new/changed/removed events separately. These tests
exercise the two functions directly — no Composio, no model.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import heartbeat  # noqa: E402


def _event(eid, account, start, end=None, status="confirmed", location="", summary="Sync",
           owner_response=None):
    ev = {
        "id": eid, "_account": account, "summary": summary,
        "start": {"dateTime": start}, "end": {"dateTime": end or start},
        "status": status, "location": location,
    }
    if owner_response is not None:
        ev["_owner_response_status"] = owner_response
    return ev


def _data(events):
    return {"gmail": {}, "gcal": events, "stale_gmail": {}}


def test_moved_event_is_changed_with_old_and_new_time():
    prev = heartbeat.build_snapshot(_data([
        _event("e1", "personal", "2026-09-21T09:00:00-07:00"),
    ]))
    curr = heartbeat.build_snapshot(_data([
        _event("e1", "personal", "2026-09-21T11:00:00-07:00"),
    ]))
    diff = heartbeat.diff_snapshots(prev, curr)
    assert diff["has_changes"] is True
    assert "e1" in diff["changed_event_ids"]
    assert "e1" not in diff["new_event_ids"]
    assert "e1" not in diff["removed_event_ids"]
    changed = [c for c in diff["changed_events"] if c["id"] == "e1"][0]
    assert changed["old_start"] == "2026-09-21T09:00:00-07:00"
    assert changed["new_start"] == "2026-09-21T11:00:00-07:00"


def test_cancelled_future_event_still_present_is_removed():
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    prev = heartbeat.build_snapshot(_data([_event("e2", "personal", future)]))
    curr = heartbeat.build_snapshot(_data([_event("e2", "personal", future, status="cancelled")]))
    diff = heartbeat.diff_snapshots(prev, curr)
    assert "e2" in diff["removed_event_ids"]
    assert diff["has_changes"] is True
    removed = [r for r in diff["removed_events"] if r["id"] == "e2"][0]
    assert removed["reason"] == "cancelled"


def test_cancelled_via_disappearance_when_start_still_future():
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    prev = heartbeat.build_snapshot(_data([_event("e3", "personal", future)]))
    curr = heartbeat.build_snapshot(_data([]))  # vanished from the fetch entirely
    diff = heartbeat.diff_snapshots(prev, curr)
    assert "e3" in diff["removed_event_ids"]
    removed = [r for r in diff["removed_events"] if r["id"] == "e3"][0]
    assert removed["reason"] == "disappeared"


def test_event_that_already_ended_and_dropped_out_of_window_is_not_removed():
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    prev = heartbeat.build_snapshot(_data([_event("e4", "personal", past)]))
    curr = heartbeat.build_snapshot(_data([]))  # today's window no longer includes it
    diff = heartbeat.diff_snapshots(prev, curr)
    assert "e4" not in diff["removed_event_ids"]
    assert diff["removed_events"] == []


def test_same_event_id_on_two_accounts_is_two_entries():
    curr = heartbeat.build_snapshot(_data([
        _event("shared1", "personal", "2026-09-21T09:00:00-07:00"),
        _event("shared1", "work", "2026-09-21T09:00:00-07:00"),
    ]))
    assert "personal:shared1" in curr["gcal_events"]
    assert "work:shared1" in curr["gcal_events"]
    assert len(curr["gcal_events"]) == 2
    assert curr["gcal_ids"] == ["shared1"]  # legacy bare-id list still collapses, by design


def test_old_shape_prev_snapshot_same_events_has_changes_false():
    start = "2026-09-21T09:00:00-07:00"
    curr = heartbeat.build_snapshot(_data([_event("e5", "personal", start)]))
    old_shape_prev = {"gmail_ids": [], "gcal_ids": ["e5"], "stale_gmail_ids": []}
    diff = heartbeat.diff_snapshots(old_shape_prev, curr)
    assert diff["has_changes"] is False
    assert diff["new_event_ids"] == []
    assert diff["changed_event_ids"] == []
    assert diff["removed_event_ids"] == []


def test_old_shape_prev_new_event_still_detected():
    start = "2026-09-21T09:00:00-07:00"
    curr = heartbeat.build_snapshot(_data([_event("e6", "personal", start)]))
    old_shape_prev = {"gmail_ids": [], "gcal_ids": [], "stale_gmail_ids": []}
    diff = heartbeat.diff_snapshots(old_shape_prev, curr)
    assert "e6" in diff["new_event_ids"]
    assert diff["has_changes"] is True


def test_repeated_identical_fetch_is_quiet():
    events = [_event("e7", "personal", "2026-09-21T09:00:00-07:00", location="Room 4")]
    snap1 = heartbeat.build_snapshot(_data(events))
    snap2 = heartbeat.build_snapshot(_data(events))
    diff = heartbeat.diff_snapshots(snap1, snap2)
    assert diff["has_changes"] is False
    assert diff["changed_event_ids"] == []
    assert diff["removed_event_ids"] == []
    assert diff["new_event_ids"] == []


def test_owner_response_status_change_is_a_changed_event():
    prev = heartbeat.build_snapshot(_data([
        _event("e8", "personal", "2026-09-21T09:00:00-07:00", owner_response="needsAction"),
    ]))
    curr = heartbeat.build_snapshot(_data([
        _event("e8", "personal", "2026-09-21T09:00:00-07:00", owner_response="declined"),
    ]))
    diff = heartbeat.diff_snapshots(prev, curr)
    assert "e8" in diff["changed_event_ids"]


def test_gmail_same_id_on_two_accounts_counts_as_new_on_each():
    """The bare-id set difference used to hide a second account's arrival of
    an id already seen (once) on a different account. Account-scoped
    gmail_keys fixes the dedup; new_email_ids stays bare for legacy readers."""
    def gmail_data(accounts_with_msg):
        return {"gmail": {a: [{"id": "dup1"}] for a in accounts_with_msg}, "gcal": [], "stale_gmail": {}}

    prev = heartbeat.build_snapshot(gmail_data(["personal"]))
    curr = heartbeat.build_snapshot(gmail_data(["personal", "work"]))
    diff = heartbeat.diff_snapshots(prev, curr)
    assert "dup1" in diff["new_email_ids"]
    assert "personal:dup1" in curr["gmail_keys"]
    assert "work:dup1" in curr["gmail_keys"]
