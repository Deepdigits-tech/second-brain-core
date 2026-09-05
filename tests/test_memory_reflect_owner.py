"""Reflection runs on exactly one machine (finding 6.2).

Both machines ran memory_reflect.py at 08:00 and both rewrote vault/MEMORY.md,
a git-tracked file synced every two minutes. On 2026-08-31 the Mac's run spent
all 25 turns resolving a merge conflict its twin had just created, then died —
the 29th error_max_turns failure on record. The coordination file is per-machine,
so ownership is a platform gate, the same one the briefs use.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import memory_reflect as mr  # noqa: E402


def test_owner_is_the_vps_by_default(monkeypatch):
    monkeypatch.delenv("CORTEX_REFLECTION_OWNER", raising=False)
    monkeypatch.setattr(mr, "REFLECTION_OWNER_IS_VPS", True)
    assert mr.is_reflection_owner() is True
    monkeypatch.setattr(mr, "REFLECTION_OWNER_IS_VPS", False)
    assert mr.is_reflection_owner() is False


def test_env_override_forces_ownership(monkeypatch):
    monkeypatch.setattr(mr, "REFLECTION_OWNER_IS_VPS", False)
    monkeypatch.setenv("CORTEX_REFLECTION_OWNER", "1")
    assert mr.is_reflection_owner() is True
    monkeypatch.setenv("CORTEX_REFLECTION_OWNER", "0")
    assert mr.is_reflection_owner() is False


def test_non_owner_never_reaches_the_model_call(monkeypatch):
    """The non-owner may read the log now (--test needs to work everywhere), but
    it must never reach the model call."""
    monkeypatch.setattr(mr, "REFLECTION_OWNER_IS_VPS", False)
    monkeypatch.delenv("CORTEX_REFLECTION_OWNER", raising=False)
    monkeypatch.setattr(mr, "get_yesterday_log", lambda: "## 09:00 — Heartbeat\n- something happened\n")

    def _no_query(*a, **k):
        raise AssertionError("query() must not run on the non-owner")

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "query", _no_query)
    import asyncio
    asyncio.run(mr.reflect())  # must not raise — query() is never called


def test_dry_run_works_on_the_non_owner(monkeypatch, capsys):
    """--test must work on any machine, not just the reflection owner."""
    monkeypatch.setattr(mr, "REFLECTION_OWNER_IS_VPS", False)
    monkeypatch.delenv("CORTEX_REFLECTION_OWNER", raising=False)
    monkeypatch.setattr(mr, "get_yesterday_log", lambda: "## 09:00 — Heartbeat\n- something happened\n")
    import asyncio
    asyncio.run(mr.reflect(dry_run=True))
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "something happened" in out


def test_dry_run_builds_the_prompt_and_calls_no_model(monkeypatch, capsys):
    monkeypatch.setattr(mr, "REFLECTION_OWNER_IS_VPS", True)
    monkeypatch.delenv("CORTEX_REFLECTION_OWNER", raising=False)
    monkeypatch.setattr(mr, "get_yesterday_log", lambda: "## 09:00 — Heartbeat\n- something happened\n")
    called = []

    def _no_query(*a, **k):
        called.append(1)
        raise AssertionError("query() must not run in dry-run")

    # reflect() imports query lazily from claude_agent_sdk; patch the module attribute it binds
    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "query", _no_query)
    import asyncio
    asyncio.run(mr.reflect(dry_run=True))
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "something happened" in out
    assert called == []


def test_content_proposals_follow_the_owner_not_the_platform():
    source = (REPO_ROOT / ".claude" / "scripts" / "memory_reflect.py").read_text()
    assert 'platform.system() == "Darwin"' not in source, (
        "content proposals used to be Mac-only to avoid double-proposing; with one "
        "owner the gate is ownership, not platform"
    )


def test_cli_exposes_test_flag():
    source = (REPO_ROOT / ".claude" / "scripts" / "memory_reflect.py").read_text()
    assert '"--test"' in source and "dry_run" in source
