"""REL-04 (2026-09-20): "Gmail is down" must not look the same as "no mail".

Before this fix `gmail.triage`/`query_messages` returned `[]` whether
Composio failed or the inbox was genuinely empty (`_execute` returning None
collapsed into the same `[]` as zero messages), `gcal.agenda_today_all_accounts`
swallowed a per-account exception with a bare `except: continue`, and
`heartbeat.gather_data()` did the same — so an outage silently overwrote the
last-good snapshot with an empty one and the brief could claim "nothing needs
a reply" during a real outage.

These tests exercise the fetch-status plumbing end to end: the integration
layer's `status_out` parameter, `gather_data()`'s `data["fetch_status"]`, the
carry-forward that keeps a failed account's ids/fingerprints unchanged, the
consecutive-failure streak, and the prompt line that tells the model not to
trust silence from a failed source.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import heartbeat  # noqa: E402
from integrations import gmail, gcal  # noqa: E402


# ── Integration layer: status_out distinguishes failure from empty ────────


def test_gmail_triage_marks_failure_when_execute_returns_none(monkeypatch):
    monkeypatch.setattr(gmail, "_execute", lambda *a, **k: None)
    status: dict = {}
    result = gmail.triage(account="personal", status_out=status)
    assert result == []
    assert status["ok"] is False


def test_gmail_triage_marks_ok_on_a_genuinely_empty_inbox(monkeypatch):
    monkeypatch.setattr(gmail, "_execute", lambda *a, **k: {"messages": []})
    status: dict = {}
    result = gmail.triage(account="personal", status_out=status)
    assert result == []
    assert status["ok"] is True


def test_triage_all_accounts_isolates_one_failed_account(monkeypatch):
    monkeypatch.setattr(gmail, "account_user_ids", lambda: {"personal": "p", "deepdigits": "d"})

    def fake_execute(tool, args, account="personal"):
        if account == "personal":
            return None  # outage
        return {"messages": [{"messageId": "m1"}]}

    monkeypatch.setattr(gmail, "_execute", fake_execute)
    status: dict = {}
    out = gmail.triage_all_accounts(status_out=status)
    assert status == {"personal": "failed", "deepdigits": "ok"}
    assert out["personal"] == []
    assert len(out["deepdigits"]) == 1  # the other account's results are untouched


def test_gcal_fetch_events_marks_failure_and_ok(monkeypatch):
    monkeypatch.setattr(gcal, "_execute", lambda *a, **k: None)
    status: dict = {}
    assert gcal._fetch_events("t0", "t1", status_out=status) == []
    assert status["ok"] is False

    monkeypatch.setattr(gcal, "_execute", lambda *a, **k: {"items": []})
    status2: dict = {}
    assert gcal._fetch_events("t0", "t1", status_out=status2) == []
    assert status2["ok"] is True


def test_agenda_today_all_accounts_isolates_one_failed_account(monkeypatch):
    monkeypatch.setenv("CORTEX_CALENDAR_ACCOUNTS", "personal,deepdigits")
    import shared
    shared.calendar_accounts.cache_clear()

    def fake_fetch(time_min, time_max, account="personal", status_out=None):
        if account == "personal":
            if status_out is not None:
                status_out["ok"] = False
            return []
        if status_out is not None:
            status_out["ok"] = True
        return [{"id": "e1", "summary": "s", "start": {"dateTime": "2026-09-21T09:00:00-07:00"}}]

    monkeypatch.setattr(gcal, "_fetch_events", fake_fetch)
    status: dict = {}
    events = gcal.agenda_today_all_accounts(status_out=status)
    shared.calendar_accounts.cache_clear()
    assert status == {"personal": "failed", "deepdigits": "ok"}
    assert len(events) == 1
    assert events[0]["_account"] == "deepdigits"


def test_agenda_today_all_accounts_records_failed_on_exception(monkeypatch):
    monkeypatch.setenv("CORTEX_CALENDAR_ACCOUNTS", "personal")
    import shared
    shared.calendar_accounts.cache_clear()

    def boom(*a, **k):
        raise RuntimeError("composio down")

    monkeypatch.setattr(gcal, "agenda_today", boom)
    status: dict = {}
    events = gcal.agenda_today_all_accounts(status_out=status)
    shared.calendar_accounts.cache_clear()
    assert events == []
    assert status == {"personal": "failed"}


# ── heartbeat.gather_data() populates data["fetch_status"] ────────────────


def test_gather_data_records_failed_gmail_account(monkeypatch, tmp_path):
    monkeypatch.setattr(heartbeat, "gmail_accounts", lambda: {"personal": "p"})
    monkeypatch.setattr(heartbeat, "calendar_accounts", lambda: [])
    monkeypatch.setattr(heartbeat, "STATE_PATH", tmp_path / "heartbeat-state.json")
    monkeypatch.setattr(heartbeat, "DAILY_DIR", tmp_path)
    monkeypatch.setattr(heartbeat, "VAULT_DIR", tmp_path)

    import integrations.registry as registry
    monkeypatch.setattr(registry, "load_env", lambda: None)
    monkeypatch.setattr(registry, "check_all", lambda: {
        "gmail": {"available": True}, "gcal": {"available": False},
    })

    def fake_triage_all(max_results=20, status_out=None):
        if status_out is not None:
            status_out["personal"] = "failed"
        return {}
    monkeypatch.setattr(gmail, "triage_all_accounts", fake_triage_all)
    monkeypatch.setattr(gmail, "find_stale_unread", lambda **k: [])
    monkeypatch.setattr(gmail, "account_user_ids", lambda: {"personal": "p"})

    data = heartbeat.gather_data()
    assert data["fetch_status"]["gmail"] == {"personal": "failed"}


def test_gather_data_exception_marks_every_configured_account_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(heartbeat, "gmail_accounts", lambda: {"personal": "p", "deepdigits": "d"})
    monkeypatch.setattr(heartbeat, "calendar_accounts", lambda: [])
    monkeypatch.setattr(heartbeat, "STATE_PATH", tmp_path / "heartbeat-state.json")
    monkeypatch.setattr(heartbeat, "DAILY_DIR", tmp_path)
    monkeypatch.setattr(heartbeat, "VAULT_DIR", tmp_path)

    import integrations.registry as registry
    monkeypatch.setattr(registry, "load_env", lambda: None)
    monkeypatch.setattr(registry, "check_all", lambda: {
        "gmail": {"available": True}, "gcal": {"available": False},
    })

    def boom(*a, **k):
        raise RuntimeError("composio down")
    monkeypatch.setattr(gmail, "triage_all_accounts", boom)
    monkeypatch.setattr(gmail, "find_stale_unread", lambda **k: [])
    monkeypatch.setattr(gmail, "account_user_ids", lambda: {"personal": "p", "deepdigits": "d"})

    data = heartbeat.gather_data()
    assert data["fetch_status"]["gmail"] == {"personal": "failed", "deepdigits": "failed"}


# ── carry_forward_failed_accounts: failed account keeps its old entries ───


def test_failed_gmail_account_ids_carried_forward_unchanged():
    prev = heartbeat.build_snapshot({
        "gmail": {"personal": [{"id": "m1"}, {"id": "m2"}]}, "gcal": [], "stale_gmail": {},
    })
    curr = heartbeat.build_snapshot({"gmail": {}, "gcal": [], "stale_gmail": {}})  # fetch failed, nothing came back
    fetch_status = {"gmail": {"personal": "failed"}, "gcal": {}}
    heartbeat.carry_forward_failed_accounts(prev, curr, fetch_status)

    assert set(curr["gmail_ids"]) == {"m1", "m2"}
    assert "personal:m1" in curr["gmail_keys"]
    assert "personal:m2" in curr["gmail_keys"]

    diff = heartbeat.diff_snapshots(prev, curr)
    assert diff["has_changes"] is False
    assert diff["new_email_ids"] == []


def test_one_failed_account_does_not_erase_another_accounts_results():
    prev = heartbeat.build_snapshot({
        "gmail": {"personal": [{"id": "m1"}], "deepdigits": [{"id": "d1"}]}, "gcal": [], "stale_gmail": {},
    })
    # personal failed (nothing fetched); deepdigits succeeded with a genuinely new message
    curr = heartbeat.build_snapshot({
        "gmail": {"deepdigits": [{"id": "d1"}, {"id": "d2"}]}, "gcal": [], "stale_gmail": {},
    })
    fetch_status = {"gmail": {"personal": "failed", "deepdigits": "ok"}, "gcal": {}}
    heartbeat.carry_forward_failed_accounts(prev, curr, fetch_status)
    diff = heartbeat.diff_snapshots(prev, curr)

    assert "m1" in curr["gmail_ids"]  # personal carried forward
    assert "d2" in diff["new_email_ids"]  # deepdigits' real new mail still detected
    assert diff["has_changes"] is True


def test_recovery_tick_after_outage_does_not_mark_old_mail_new():
    baseline = heartbeat.build_snapshot({"gmail": {"personal": [{"id": "m1"}]}, "gcal": [], "stale_gmail": {}})

    # Outage tick: fetch failed, carry m1 forward.
    outage = heartbeat.build_snapshot({"gmail": {}, "gcal": [], "stale_gmail": {}})
    heartbeat.carry_forward_failed_accounts(baseline, outage, {"gmail": {"personal": "failed"}, "gcal": {}})

    # Recovery tick: same inbox as before the outage, fetch succeeds.
    recovered = heartbeat.build_snapshot({"gmail": {"personal": [{"id": "m1"}]}, "gcal": [], "stale_gmail": {}})
    diff = heartbeat.diff_snapshots(outage, recovered)
    assert diff["new_email_ids"] == []
    assert diff["has_changes"] is False


def test_genuinely_empty_inbox_ok_stays_ok_and_empty():
    prev = heartbeat.build_snapshot({"gmail": {"personal": [{"id": "m1"}]}, "gcal": [], "stale_gmail": {}})
    curr = heartbeat.build_snapshot({"gmail": {"personal": []}, "gcal": [], "stale_gmail": {}})
    heartbeat.carry_forward_failed_accounts(prev, curr, {"gmail": {"personal": "ok"}, "gcal": {}})
    assert curr["gmail_ids"] == []  # a real drain to zero is not carried forward


def test_failed_calendar_account_events_carried_forward():
    events = [{"id": "e1", "_account": "personal", "summary": "s",
               "start": {"dateTime": "2026-09-21T09:00:00-07:00"}, "status": "confirmed"}]
    prev = heartbeat.build_snapshot({"gmail": {}, "gcal": events, "stale_gmail": {}})
    curr = heartbeat.build_snapshot({"gmail": {}, "gcal": [], "stale_gmail": {}})
    heartbeat.carry_forward_failed_accounts(prev, curr, {"gmail": {}, "gcal": {"personal": "failed"}})
    diff = heartbeat.diff_snapshots(prev, curr)
    assert diff["removed_event_ids"] == []
    assert diff["has_changes"] is False


# ── consecutive-failure streak ─────────────────────────────────────────


def test_failure_streak_increments_and_resets():
    prev = {}
    curr = {}
    heartbeat.update_fetch_failure_streak(prev, curr, {"gmail": {"personal": "failed"}, "gcal": {}})
    assert curr["fetch_failure_streak"]["gmail"]["personal"] == 1

    prev2 = curr
    curr2 = {}
    heartbeat.update_fetch_failure_streak(prev2, curr2, {"gmail": {"personal": "failed"}, "gcal": {}})
    assert curr2["fetch_failure_streak"]["gmail"]["personal"] == 2

    prev3 = curr2
    curr3 = {}
    heartbeat.update_fetch_failure_streak(prev3, curr3, {"gmail": {"personal": "ok"}, "gcal": {}})
    assert curr3["fetch_failure_streak"]["gmail"]["personal"] == 0


def test_failure_streak_left_alone_when_account_not_in_fetch_status():
    prev = {"fetch_failure_streak": {"gmail": {"personal": 3}, "gcal": {}}}
    curr = {}
    heartbeat.update_fetch_failure_streak(prev, curr, {"gmail": {}, "gcal": {}})
    assert curr["fetch_failure_streak"]["gmail"]["personal"] == 3


def test_fetch_failure_streak_is_declared_sticky():
    assert "fetch_failure_streak" in heartbeat.STICKY_STATE_KEYS


# ── prompt carries the "could not reach" line ──────────────────────────


def _fake_sdk(captured: dict):
    mod = types.ModuleType("claude_agent_sdk")

    class _Opts:
        def __init__(self, **kw):
            self.kw = kw

    class AssistantMessage: ...
    class ResultMessage: ...
    class TextBlock: ...

    async def query(prompt, options):
        captured["prompt"] = prompt
        if False:
            yield None

    mod.ClaudeAgentOptions = _Opts
    mod.AssistantMessage = AssistantMessage
    mod.ResultMessage = ResultMessage
    mod.TextBlock = TextBlock
    mod.query = query
    return mod


@pytest.fixture
def heartbeat_prompt(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _fake_sdk(captured))
    monkeypatch.setattr(heartbeat, "VAULT_DIR", tmp_path)
    (tmp_path / "SOUL.md").write_text("soul")
    (tmp_path / "USER.md").write_text("user")
    (tmp_path / "daily").mkdir()
    import sdk_cli

    async def _run(fn, log=None):
        return await fn()
    monkeypatch.setattr(sdk_cli, "run_with_retry", _run)

    def build(data: dict, diff: dict):
        asyncio.run(heartbeat.run_heartbeat(data, diff))
        return captured
    return build


def test_prompt_carries_could_not_reach_gmail_line(heartbeat_prompt):
    data = {"gmail": {}, "gcal": [], "fetch_status": {"gmail": {"personal": "failed"}, "gcal": {}}}
    diff = {"has_changes": False, "new_email_count": 0, "new_event_count": 0}
    out = heartbeat_prompt(data, diff)
    assert "Could not reach Gmail for personal this run" in out["prompt"]
    assert "do not say the inbox is empty" in out["prompt"]


def test_prompt_carries_could_not_reach_calendar_line(heartbeat_prompt):
    data = {"gmail": {}, "gcal": [], "fetch_status": {"gmail": {}, "gcal": {"personal": "failed"}}}
    diff = {"has_changes": False, "new_email_count": 0, "new_event_count": 0}
    out = heartbeat_prompt(data, diff)
    assert "Could not reach Calendar for personal this run" in out["prompt"]


def test_prompt_has_no_data_gap_line_when_everything_ok(heartbeat_prompt):
    data = {"gmail": {}, "gcal": [], "fetch_status": {"gmail": {"personal": "ok"}, "gcal": {"personal": "ok"}}}
    diff = {"has_changes": False, "new_email_count": 0, "new_event_count": 0}
    out = heartbeat_prompt(data, diff)
    assert "Data gaps this run" not in out["prompt"]


def test_morning_brief_prompt_also_carries_the_line(monkeypatch, tmp_path):
    """The brief path shares run_heartbeat()'s sections list — a brief must
    never claim 'nothing needs a reply' during an outage."""
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _fake_sdk(captured))
    monkeypatch.setattr(heartbeat, "VAULT_DIR", tmp_path)
    (tmp_path / "SOUL.md").write_text("soul")
    (tmp_path / "USER.md").write_text("user")
    (tmp_path / "daily").mkdir()
    import sdk_cli

    async def _run(fn, log=None):
        return await fn()
    monkeypatch.setattr(sdk_cli, "run_with_retry", _run)

    data = {"gmail": {}, "gcal": [], "fetch_status": {"gmail": {"personal": "failed"}, "gcal": {}}}
    diff = {"has_changes": True, "new_email_count": 0, "new_event_count": 0}
    asyncio.run(heartbeat.run_heartbeat(data, diff, morning_brief=True))
    assert "Could not reach Gmail for personal this run" in captured["prompt"]
