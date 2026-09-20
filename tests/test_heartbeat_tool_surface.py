"""The heartbeat's main model call must not have Bash (or anything unrequested).

allowed_tools only pre-approves a tool inside whatever surface the SDK starts
with — it does not remove anything else. This agent reads raw email/calendar
text every run, so `tools=` must lock the whole built-in surface, not just
pre-approve a subset of it (2026-09-20 fix).
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / ".claude" / "scripts", ROOT / ".claude" / "chat"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _fake_sdk(captured: dict):
    """A stand-in claude_agent_sdk whose query() records the options and yields nothing.

    Same pattern as tests/test_trust_boundary_placement.py.
    """
    mod = types.ModuleType("claude_agent_sdk")

    class _Opts:
        def __init__(self, **kw):
            self.kw = kw

    class AssistantMessage: ...
    class ResultMessage: ...
    class TextBlock: ...

    async def query(prompt, options):
        captured["options"] = options.kw
        if False:
            yield None

    mod.ClaudeAgentOptions = _Opts
    mod.AssistantMessage = AssistantMessage
    mod.ResultMessage = ResultMessage
    mod.TextBlock = TextBlock
    mod.query = query
    return mod


@pytest.fixture
def heartbeat_options(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _fake_sdk(captured))
    import heartbeat
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


def _data_with_email():
    import sanitize
    body = sanitize.sanitize("hello there", "gmail")
    return {
        "gmail": {"personal": [{"id": "m1", "from": sanitize.sanitize("sender@example.com", "gmail"),
                                "subject": sanitize.sanitize("hi", "gmail"), "snippet": body, "body": body,
                                "date": "2026-09-02"}]},
        "gcal": [],
    }


def test_main_call_locks_tool_surface(heartbeat_options):
    out = heartbeat_options(_data_with_email(), {"has_changes": True, "new_email_count": 1,
                                                  "new_event_count": 0, "new_emails": [], "new_events": [],
                                                  "new_email_ids": {"m1"}})
    assert out["options"]["tools"] == ["Write", "Edit", "Read"]
    assert "Bash" not in out["options"]["tools"]
    assert "Bash" in out["options"]["disallowed_tools"]


def test_scan_llm_extraction_has_no_tool_surface(monkeypatch):
    """heartbeat._extract_with_llm_async is a separate no-tools call site."""
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _fake_sdk(captured))
    import heartbeat
    asyncio.run(heartbeat._extract_with_llm_async(
        {"extract": "items", "criteria": "anything"}, {"id": "m1"}, "body text"
    ))
    assert captured["options"]["tools"] == []
    assert captured["options"]["allowed_tools"] == []


def test_tools_is_a_real_field_on_the_installed_sdk():
    """The fake SDK above (`_Opts`) accepts any kwarg, so every test in this
    file would pass even if the REAL `ClaudeAgentOptions` rejected `tools=` —
    the fake can't catch a typo'd or removed field. Check the installed
    package directly. Skips cleanly (not xfail/error) when the SDK isn't
    importable, e.g. a minimal test environment without it installed.
    """
    import dataclasses

    sdk = pytest.importorskip("claude_agent_sdk")
    field_names = {f.name for f in dataclasses.fields(sdk.ClaudeAgentOptions)}
    assert "tools" in field_names
    assert "allowed_tools" in field_names
    assert "disallowed_tools" in field_names
