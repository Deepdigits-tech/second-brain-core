"""The prompt's 'N new since last check' header must match what's actually listed.

`diff["new_email_count"]`/`["new_event_count"]` are computed from the
PRE-gate snapshot diff — every new id this tick, including any that the
guardrail then quarantined or deferred out of `data`. Before 2026-09-20 the
header used that raw count while the list underneath it was built by
filtering `data` (already gate-filtered) against `diff["new_email_ids"]`, so
a tick with one new email quarantined out of three could render "## New
Emails (3 new since last check)" over a list of two.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT / ".claude" / "scripts", REPO_ROOT / ".claude" / "chat"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import sanitize  # noqa: E402


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


def _msg(mid):
    body = sanitize.sanitize("hello", "gmail")
    return {"id": mid, "from": sanitize.sanitize("p@example.com", "gmail"),
            "subject": sanitize.sanitize("hi", "gmail"), "snippet": body, "body": body,
            "date": "2026-09-20"}


def test_new_email_header_count_matches_filtered_list(monkeypatch, tmp_path):
    import heartbeat

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _fake_sdk(captured := {}))
    monkeypatch.setattr(heartbeat, "VAULT_DIR", tmp_path)
    (tmp_path / "SOUL.md").write_text("soul")
    (tmp_path / "USER.md").write_text("user")
    (tmp_path / "daily").mkdir()

    import sdk_cli
    async def _run(fn, log=None):
        return await fn()
    monkeypatch.setattr(sdk_cli, "run_with_retry", _run)

    # 3 new ids in the diff, but only 2 are present in `data` — the third was
    # dropped by the guardrail gate before run_heartbeat ever saw it.
    data = {
        "gmail": {"personal": [_msg("m1"), _msg("m2")]},  # m3 quarantined/deferred, absent
        "gcal": [],
    }
    diff = {
        "has_changes": True, "new_email_count": 3, "new_event_count": 0,
        "new_email_ids": ["m1", "m2", "m3"], "stale_email_ids": [],
        "changed_event_ids": [], "removed_event_ids": [], "changed_events": [], "removed_events": [],
    }

    asyncio.run(heartbeat.run_heartbeat(data, diff))
    prompt = captured["prompt"]

    assert "## New Emails (2 new since last check)" in prompt
    assert "## New Emails (3 new since last check)" not in prompt


def test_new_event_header_count_matches_filtered_list(monkeypatch, tmp_path):
    import heartbeat

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _fake_sdk(captured := {}))
    monkeypatch.setattr(heartbeat, "VAULT_DIR", tmp_path)
    (tmp_path / "SOUL.md").write_text("soul")
    (tmp_path / "USER.md").write_text("user")
    (tmp_path / "daily").mkdir()

    import sdk_cli
    async def _run(fn, log=None):
        return await fn()
    monkeypatch.setattr(sdk_cli, "run_with_retry", _run)

    event = {"id": "e1", "summary": "Standup", "start": {"dateTime": "2026-09-20T09:00:00-07:00"}}
    data = {"gmail": {}, "gcal": [event]}  # e2 quarantined/deferred, absent from data
    diff = {
        "has_changes": True, "new_email_count": 0, "new_event_count": 2,
        "new_email_ids": [], "stale_email_ids": [],
        "new_event_ids": ["e1", "e2"],
        "changed_event_ids": [], "removed_event_ids": [], "changed_events": [], "removed_events": [],
    }

    asyncio.run(heartbeat.run_heartbeat(data, diff))
    prompt = captured["prompt"]

    assert "## New Calendar Events (1 new)" in prompt
    assert "## New Calendar Events (2 new)" not in prompt
