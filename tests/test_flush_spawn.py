"""The flush spawn must survive a worktree with no .venv (finding 2.1).

The hooks themselves gained an interpreter fallback in August; the process
they spawn did not. In a worktree, Popen raised FileNotFoundError and the
session's memory was never written. Both hooks were byte-for-byte the same
logic (finding 2.4); now there is one module and two entry points.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / ".claude" / "hooks"

spec = importlib.util.spec_from_file_location("_flush_common", HOOKS / "_flush_common.py")
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)


def _fake_venv(root: Path) -> Path:
    py = root / ".venv" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    return py


def test_prefers_the_project_venv_when_present(tmp_path):
    py = _fake_venv(tmp_path)
    assert fc.resolve_interpreter(str(tmp_path)) == str(py)


def test_falls_back_to_system_python_without_a_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(fc.shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None)
    assert fc.resolve_interpreter(str(tmp_path)) == "/usr/bin/python3"


def test_returns_none_when_nothing_is_available(tmp_path, monkeypatch):
    monkeypatch.setattr(fc.shutil, "which", lambda name: None)
    assert fc.resolve_interpreter(str(tmp_path)) is None


def test_spawn_uses_the_resolved_interpreter_and_detaches(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    py = _fake_venv(tmp_path)
    (tmp_path / ".claude" / "scripts").mkdir(parents=True)
    (tmp_path / ".claude" / "scripts" / "memory_flush.py").write_text("")
    popen_calls = []

    class _FakePopen:
        def __init__(self, argv, **kw):
            popen_calls.append((argv, kw))

    monkeypatch.setattr(fc.subprocess, "Popen", _FakePopen)
    monkeypatch.delenv("CLAUDE_INVOKED_BY", raising=False)
    ok = fc.spawn_flush(
        {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(tmp_path)},
        hook_name="test-flush",
    )
    assert ok is True
    argv, kw = popen_calls[0]
    assert argv[0] == str(py)
    assert argv[1].endswith("memory_flush.py")
    assert argv[2] == f"/tmp/cortex_flush_s1.jsonl"
    assert kw["start_new_session"] is True
    assert kw["env"]["CLAUDE_INVOKED_BY"] == "memory_flush"
    assert Path(argv[2]).exists()


def test_spawn_is_skipped_under_the_recursion_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_INVOKED_BY", "heartbeat")
    assert fc.spawn_flush({"session_id": "s", "transcript_path": "/nope", "cwd": str(tmp_path)}, hook_name="x") is False


def test_spawn_is_skipped_when_no_interpreter(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.delenv("CLAUDE_INVOKED_BY", raising=False)
    monkeypatch.setattr(fc.shutil, "which", lambda name: None)
    assert fc.spawn_flush({"session_id": "s2", "transcript_path": str(transcript), "cwd": str(tmp_path)}, hook_name="x") is False


@pytest.mark.parametrize("hook", ["pre-compact-flush.py", "session-end-flush.py"])
def test_entry_points_are_thin_wrappers(hook):
    source = (HOOKS / hook).read_text()
    assert "from _flush_common import spawn_flush" in source
    assert "subprocess.Popen" not in source, f"{hook} still spawns on its own"
    assert '".venv"' not in source and ".venv/bin/python3" not in source
