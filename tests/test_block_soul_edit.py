import json, os, subprocess
from pathlib import Path
import pytest
HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "block-soul-edit.py"

def _env(agent):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if agent is not None:
        env["CLAUDE_INVOKED_BY"] = agent
    return env

def run(agent, tool, path):
    payload = {"tool_name": tool, "tool_input": {"file_path": path}}
    return subprocess.run(["/usr/bin/python3", str(HOOK)], input=json.dumps(payload), text=True, capture_output=True, env=_env(agent))

AGENTS = ["heartbeat", "reflection", "memory_flush", "chat_server", "weekly_body_report", "kb_score"]

@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("name", ["SOUL.md", "USER.md"])
def test_no_agent_may_write_soul_or_user(agent, name):
    r = run(agent, "Write", f"/x/vault/{name}")
    assert r.returncode == 2 and name in r.stderr

@pytest.mark.parametrize("agent", [a for a in AGENTS if a != "reflection"])
def test_only_reflection_may_write_memory(agent):
    assert run(agent, "Edit", "/x/vault/MEMORY.md").returncode == 2

def test_reflection_may_write_memory():
    assert run("reflection", "Edit", "/x/vault/MEMORY.md").returncode == 0

def test_interactive_session_is_never_blocked():
    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        assert run(None, "Write", f"/x/vault/{name}").returncode == 0

@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_every_edit_tool_is_covered(tool):
    assert run("heartbeat", tool, "/x/vault/SOUL.md").returncode == 2

def test_other_files_pass():
    assert run("heartbeat", "Write", "/x/vault/daily/2026-09-02.md").returncode == 0

def test_nested_skeleton_soul_is_not_the_guarded_file():
    """Only the vault's own core files are guarded, not a template deep in a project folder."""
    assert run("heartbeat", "Write", "/x/vault/projects/P/SOUL.md").returncode == 0
    assert run("heartbeat", "Write", "/x/vault/SOUL.md").returncode == 2

def test_bare_filename_with_no_path_is_guarded():
    """A guarded filename passed with no path separators at all (no "vault"
    segment to check against) must still be blocked, not slip through.
    """
    assert run("heartbeat", "Write", "SOUL.md").returncode == 2


@pytest.mark.parametrize("agent", AGENTS)
def test_no_agent_may_write_core_memories(agent):
    r = run(agent, "Write", "/x/vault/core-memories.md")
    assert r.returncode == 2 and "core-memories.md" in r.stderr


def test_interactive_session_may_write_core_memories():
    assert run(None, "Write", "/x/vault/core-memories.md").returncode == 0


def test_notebook_path_key_is_read():
    payload = {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/x/vault/USER.md"}}
    r = subprocess.run(["/usr/bin/python3", str(HOOK)], input=json.dumps(payload), text=True, capture_output=True, env=_env("chat_server"))
    assert r.returncode == 2
