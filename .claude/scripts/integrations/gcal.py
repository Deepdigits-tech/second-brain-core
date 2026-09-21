"""Google Calendar integration via Composio SDK for Cortex OS."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from integrations.gmail import _execute, DEFAULT_ACCOUNT

from shared import PST, owner_emails

# Aliases that have googlecalendar connected on Composio. Source of truth for
# anything that needs to fan out across all of the owner's calendars (heartbeat,
# weekly reports). From shared.calendar_accounts() (CORTEX_CALENDAR_ACCOUNTS,
# default = the gmail account aliases) — not evaluated at import time, for the
# same reason gmail.ACCOUNT_USER_IDS isn't: the env may not be loaded yet.


def calendar_accounts() -> list[str]:
    from shared import calendar_accounts as _calendar_accounts
    return _calendar_accounts()


def __getattr__(name: str):
    if name == "CALENDAR_ACCOUNTS":
        return calendar_accounts()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Every string an invite sender controls. Cleaned here so heartbeat, chat and
# the weekly report all inherit it — the same choice gmail.py makes for mail.
_EVENT_TEXT_FIELDS = ["summary", "description", "location"]
_PERSON_TEXT_FIELDS = ["displayName", "email", "comment"]


def _sanitize_event(event: dict) -> dict:
    """Clean attacker-controlled text on a calendar event. Non-destructive.

    An invite needs no acceptance to appear on the calendar, so its text is
    external content in exactly the sense an email body is. Nested people
    fields are cleaned too — an organizer display name is free text.
    """
    try:
        parent_dir = str(Path(__file__).resolve().parent.parent)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from sanitize import sanitize_dict
    except ImportError:
        return event

    out = sanitize_dict(event, "gcal", _EVENT_TEXT_FIELDS)

    organizer = out.get("organizer")
    if isinstance(organizer, dict):
        out["organizer"] = sanitize_dict(organizer, "gcal", _PERSON_TEXT_FIELDS)

    attendees = out.get("attendees")
    if isinstance(attendees, list):
        out["attendees"] = [
            sanitize_dict(a, "gcal", _PERSON_TEXT_FIELDS) if isinstance(a, dict) else a
            for a in attendees
        ]
    return out


def _today_bounds() -> tuple[str, str]:
    """Return ISO timestamps for start/end of today in PST."""
    now = datetime.now(PST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _upcoming_bounds(days: int) -> tuple[str, str]:
    """Return ISO timestamps for now → N days from now in PST."""
    now = datetime.now(PST)
    end = now + timedelta(days=days)
    return now.isoformat(), end.isoformat()


def _week_bounds() -> tuple[str, str]:
    """Return ISO timestamps for the current week (Mon–Sun) in PST."""
    now = datetime.now(PST)
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _owner_response_status(event: dict) -> str | None:
    """The owner's own RSVP for an event, or None if not found/not invited.

    Read from the raw (pre-sanitize) attendee list — `_sanitize_event` wraps
    attendee `email` in `<external_content>` tags, which would break an exact
    match against `shared.owner_emails()`. `responseStatus` itself is a
    Google-controlled enum ("accepted"/"declined"/"tentative"/"needsAction"),
    not attacker-controlled free text, so it is safe to read and fingerprint
    unsanitized (used by build_snapshot's event fingerprint, 2026-09-20).
    """
    try:
        owners = {e.lower() for e in owner_emails()}
    except Exception:
        return None
    for a in event.get("attendees") or []:
        if isinstance(a, dict) and (a.get("email") or "").lower() in owners:
            return a.get("responseStatus")
    return None


def _fetch_events(
    time_min: str, time_max: str, account: str = DEFAULT_ACCOUNT, status_out: dict | None = None,
) -> list[dict]:
    """Fetch calendar events within a time range.

    `status_out`, when given, is stamped `{"ok": True|False}` so a caller can
    tell a Composio/Calendar outage apart from a genuinely empty result
    (2026-09-20) — `_execute` returning None already carries that distinction,
    this just surfaces it instead of collapsing both cases to [].
    """
    data = _execute("GOOGLECALENDAR_EVENTS_LIST", {
        "calendarId": "primary",
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": True,
        "orderBy": "startTime",
        "timeZone": "America/Los_Angeles",
    }, account=account)

    if data is None:
        if status_out is not None:
            status_out["ok"] = False
        return []
    if status_out is not None:
        status_out["ok"] = True
    if isinstance(data, dict):
        items = data.get("items", [])
        out = []
        for ev in items:
            if isinstance(ev, dict):
                # Stamp before sanitizing — see _owner_response_status docstring.
                ev["_owner_response_status"] = _owner_response_status(ev)
                ev = _sanitize_event(ev)
            out.append(ev)
        return out
    return []


def agenda_today(account: str = DEFAULT_ACCOUNT, status_out: dict | None = None) -> list[dict]:
    """Get today's calendar events for one account."""
    time_min, time_max = _today_bounds()
    return _fetch_events(time_min, time_max, account, status_out=status_out)


def agenda_today_all_accounts(status_out: dict | None = None) -> list[dict]:
    """Today's events merged across every account in CALENDAR_ACCOUNTS.

    Each event is tagged with `_account` so consumers (heartbeat brief,
    body_signal, weekly report) know which calendar surfaced it. Per-account
    failures are isolated — a broken connection on one calendar shouldn't
    blank out the others.

    `status_out`, when given, is filled in place with `{account: "ok"|"failed"}`
    (2026-09-20), same contract as gmail.triage_all_accounts — an exception
    here (previously a silent `continue`) now also counts as "failed" rather
    than reading identically to an empty calendar.
    """
    out: list[dict] = []
    for account in calendar_accounts():
        acct_status: dict = {}
        try:
            events = agenda_today(account=account, status_out=acct_status)
        except Exception:
            acct_status["ok"] = False
            events = []
        if status_out is not None:
            status_out[account] = "ok" if acct_status.get("ok", True) else "failed"
        for ev in events:
            ev["_account"] = account
            out.append(ev)
    return out


def agenda_upcoming(days: int = 3, account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """Get upcoming events for the next N days."""
    time_min, time_max = _upcoming_bounds(days)
    return _fetch_events(time_min, time_max, account)


def agenda_week(account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """Get this week's events (Mon–Sun)."""
    time_min, time_max = _week_bounds()
    return _fetch_events(time_min, time_max, account)


def list_calendars(account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """List all calendars for the account."""
    data = _execute("GOOGLECALENDAR_LIST_CALENDARS", {}, account=account)
    if data is None:
        return []
    items = data.get("items") or data.get("calendars", [])
    return items if isinstance(items, list) else []


def create_event(
    account: str,
    summary: str,
    description: str,
    start_iso: str,
    end_iso: str,
    *,
    calendar_id: str = "primary",
    timezone: str = "America/Los_Angeles",
) -> dict | None:
    """Create a calendar event. Returns the event dict on success, None on failure.

    start_iso / end_iso should be ISO 8601 strings with a UTC offset, e.g.
    '2026-05-10T14:00:00-07:00'.  The function derives duration from the two
    timestamps and translates them into Composio's flat arg schema.

    Note: Composio's GOOGLECALENDAR_CREATE_EVENT does not expose `visibility` or
    `transparency` params — events inherit the calendar's defaults (typically
    "default" / "opaque"). The shadow-event use case sets `create_meeting_room`
    to False to suppress auto-added Google Meet links.
    """
    start_dt = datetime.fromisoformat(start_iso)
    end_dt = datetime.fromisoformat(end_iso)
    duration_minutes = int((end_dt - start_dt).total_seconds() / 60)
    duration_hours, remaining_minutes = divmod(duration_minutes, 60)

    # Composio wants a naive wall-clock string in the target timezone. If the
    # caller passes an offset that doesn't match `timezone`, normalize first
    # so 21:00+00:00 with timezone="America/Los_Angeles" becomes "14:00:00".
    if start_dt.tzinfo is not None:
        start_dt = start_dt.astimezone(ZoneInfo(timezone))
    start_naive = start_dt.strftime("%Y-%m-%dT%H:%M:%S")

    args = {
        "calendarId": calendar_id,
        "start_datetime": start_naive,
        "timezone": timezone,
        "event_duration_hour": duration_hours,
        "event_duration_minutes": remaining_minutes,
        "summary": summary,
        "description": description,
        "create_meeting_room": False,
    }
    data = _execute("GOOGLECALENDAR_CREATE_EVENT", args, account=account)
    if data is None:
        return None
    # Composio nests the created event under `response_data` (same as update_event).
    # Unwrap so callers can read result["id"] directly.
    return data.get("response_data") if isinstance(data, dict) and "response_data" in data else data


def update_event(
    account: str,
    event_id: str,
    start_iso: str,
    *,
    calendar_id: str = "primary",
    timezone: str = "America/Los_Angeles",
    summary: str | None = None,
    description: str | None = None,
) -> dict | None:
    """Patch an existing event. Returns the updated event dict on success, None on failure.

    start_iso is required because Composio's GOOGLECALENDAR_UPDATE_EVENT is
    PUT-style — it requires start_datetime even for description-only patches.
    Pass the original start time to avoid corrupting the event. The wall-clock
    string is normalized to the target timezone (same logic as create_event).

    Note: Composio UPDATE schema quirks discovered 2026-05-06:
      - event_id is snake_case (unlike calendarId which stays camelCase)
      - start_datetime + timezone are both required to preserve event time correctly
      - description / summary are optional patch fields
    """
    start_dt = datetime.fromisoformat(start_iso)
    if start_dt.tzinfo is not None:
        start_dt = start_dt.astimezone(ZoneInfo(timezone))
    start_naive = start_dt.strftime("%Y-%m-%dT%H:%M:%S")

    args: dict = {
        "calendarId": calendar_id,
        "event_id": event_id,
        "start_datetime": start_naive,
        "timezone": timezone,
    }
    if summary is not None:
        args["summary"] = summary
    if description is not None:
        args["description"] = description
    data = _execute("GOOGLECALENDAR_UPDATE_EVENT", args, account=account)
    if data is None:
        return None
    # Composio nests the updated event under `response_data`; unwrap it so the
    # caller sees the same shape as create_event ({"id": ..., ...}).
    return data.get("response_data") if isinstance(data, dict) and "response_data" in data else data


def delete_event(account: str, event_id: str, *, calendar_id: str = "primary") -> bool:
    """Delete an event. Returns True on success, False on failure."""
    result = _execute(
        "GOOGLECALENDAR_DELETE_EVENT",
        {"calendarId": calendar_id, "eventId": event_id},
        account=account,
    )
    return result is not None


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from registry import load_env
    load_env()
    events = agenda_today()
    print(f"Today's events: {len(events)}")
    print(json.dumps(events[:1], indent=2) if events else "No events")
