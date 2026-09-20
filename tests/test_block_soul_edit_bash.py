"""block-soul-edit.py's Bash coverage.

Before 2026-09-20 this hook only looked at Edit/Write/MultiEdit/NotebookEdit
and exited 0 for every Bash call, so an automated caller with a Bash tool (the
chat bot) could still rewrite vault/SOUL.md, USER.md, MEMORY.md or
core-memories.md through `cp`, `mv`, `dd`, `tee -a`, a python one-liner, or
plain `>`/`>>` redirection — none of which are this hook's job to catch via
Edit/Write.

Payloads are passed as Python dicts straight into check_bash_command(), the
way test_block_secrets.py already does for block-secrets.py — never as a
literal command string on this session's own Bash tool, since block-secrets.py
is LIVE for this session and would refuse the call.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".claude" / "hooks" / "block-soul-edit.py"

spec = importlib.util.spec_from_file_location("block_soul_edit", HOOK)
block_soul_edit = importlib.util.module_from_spec(spec)
sys.modules["block_soul_edit"] = block_soul_edit
spec.loader.exec_module(block_soul_edit)


def check(invoked_by: str, command: str) -> str | None:
    return block_soul_edit.check_bash_command(invoked_by, {"command": command})


# --------------------------------------------------------------------------
# every payload from the PROBLEM statement, now blocked for automated callers
# --------------------------------------------------------------------------

PAYLOADS = {
    "append_redirect": 'echo x >> vault/SOUL.md',
    "truncate_redirect": 'echo x > vault/SOUL.md',
    "cp_into": 'cp /tmp/x vault/SOUL.md',
    "mv_into": 'mv /tmp/x vault/SOUL.md',
    "dd_of": 'dd if=/tmp/x of=vault/SOUL.md',
    "tee_append": 'tee -a vault/USER.md </tmp/x',
    "python_open_append": """python3 -c "open('vault/SOUL.md','a').write('x')\"""",
    "python_open_write": """python3 -c "open('vault/SOUL.md','w').write('x')\"""",
    "pathlib_write_text": """python3 -c "from pathlib import Path; (Path('vault')/'SOUL.md').write_text('x')\"""",
    "string_concat_path": """python3 -c "open('vau'+'lt/SOUL.md','w').write('x')\"""",
}


@pytest.mark.parametrize("name", PAYLOADS)
@pytest.mark.parametrize("agent", ["chat_server", "heartbeat"])
def test_every_payload_is_now_blocked_for_automated_callers(agent, name):
    reason = check(agent, PAYLOADS[name])
    assert reason is not None, f"{name} should be blocked for {agent}: {PAYLOADS[name]!r}"


@pytest.mark.parametrize("name", PAYLOADS)
def test_every_payload_stays_allowed_for_interactive_sessions(name):
    """The Bash branch is only reached when CLAUDE_INVOKED_BY is set at all —
    main() exits 0 before check_bash_command runs for an interactive session.
    This test calls check_bash_command directly (it has no invoked_by gate of
    its own), so it instead verifies main()'s gate via the subprocess path."""
    payload = {"tool_name": "Bash", "tool_input": {"command": PAYLOADS[name]}}
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}  # no CLAUDE_INVOKED_BY
    r = subprocess.run(
        ["/usr/bin/python3", str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )
    assert r.returncode == 0, f"{name} should never be blocked interactively: {r.stderr}"


# --------------------------------------------------------------------------
# reflection's MEMORY.md exemption carries over to Bash, SOUL.md does not
# --------------------------------------------------------------------------

def test_reflection_may_append_memory_via_bash():
    assert check("reflection", "echo 'note' >> vault/MEMORY.md") is None


def test_reflection_may_not_append_soul_via_bash():
    assert check("reflection", "echo 'note' >> vault/SOUL.md") is not None


def test_reflection_may_not_write_core_memories_via_bash():
    assert check("reflection", "echo 'note' >> vault/core-memories.md") is not None


# --------------------------------------------------------------------------
# reads must stay allowed for every automated caller
# --------------------------------------------------------------------------

READ_COMMANDS = [
    "cat vault/SOUL.md",
    "grep -n Projector vault/USER.md",
    "head -20 vault/MEMORY.md",
    "wc -l vault/core-memories.md",
]


@pytest.mark.parametrize("command", READ_COMMANDS)
@pytest.mark.parametrize("agent", ["chat_server", "heartbeat"])
def test_reads_stay_allowed(agent, command):
    assert check(agent, command) is None, f"{command} should stay a read for {agent}"


# --------------------------------------------------------------------------
# legitimate chat-bot / CLI commands must stay allowed
# --------------------------------------------------------------------------

LEGITIMATE_COMMANDS = [
    ".venv/bin/python3 .claude/scripts/query.py tasks list",
    ".venv/bin/python3 .claude/scripts/query.py invest show",
    ".venv/bin/python3 .claude/scripts/invest/data.py --quotes GLD,SLV --history SPY --days 250",
    ".venv/bin/python3 .claude/scripts/query.py gcal today",
    ".venv/bin/python3 .claude/scripts/query.py --format text invest show",
]


@pytest.mark.parametrize("command", LEGITIMATE_COMMANDS)
@pytest.mark.parametrize("agent", ["chat_server", "heartbeat"])
def test_legitimate_commands_stay_allowed(agent, command):
    assert check(agent, command) is None, f"{command} should stay allowed for {agent}"


# --------------------------------------------------------------------------
# interpreter one-liner + write primitive, blocked regardless of a name match
# --------------------------------------------------------------------------

def test_interpreter_one_liner_with_write_primitive_blocked_even_without_a_name_match():
    """An obfuscated target (here: entirely absent from the command text, as a
    stand-in for base64/chr()-assembled paths) still gets caught because the
    SHAPE of the attack — inline interpreter + file-write primitive — is
    blocked outright for automated callers."""
    cmd = """python3 -c "open(__import__('base64').b64decode('dmF1bHQvU09VTC5tZA==').decode(),'w').write('x')\""""
    assert check("chat_server", cmd) is not None


def test_plain_interpreter_call_without_write_primitive_is_not_flagged_by_rule_two():
    assert check("chat_server", "python3 -c \"print('hi')\"") is None


def test_heredoc_with_write_primitive_is_blocked():
    cmd = "python3 <<'EOF'\nopen('vault/SOUL.md','w').write('x')\nEOF"
    assert check("chat_server", cmd) is not None


# --------------------------------------------------------------------------
# a lookup that merely NAMES a guarded file is not a write
# --------------------------------------------------------------------------

LOOKUPS = [
    ".venv/bin/python3 .claude/scripts/memory_search.py 'what is in MEMORY.md'",
    ".venv/bin/python3 .claude/scripts/memory_search.py 'SOUL.md rules' 2>/dev/null",
    ".venv/bin/python3 .claude/scripts/memory_search.py 'USER.md' 2>&1",
    "grep -n priorities vault/MEMORY.md 2>/dev/null",
]


@pytest.mark.parametrize("command", LOOKUPS)
def test_a_lookup_naming_a_guarded_file_is_allowed(command):
    assert check("chat_server", command) is None


def test_discarding_output_does_not_hide_a_real_write():
    assert check("chat_server", "cp /tmp/x vault/SOUL.md 2>/dev/null") is not None
    assert check("chat_server", "echo x 2>/dev/null > vault/SOUL.md") is not None


INLINE_WRITES = [
    """python3 -c "import pathlib; pathlib.Path('vault/SOUL.md').open('w').write('x')\"""",
    """node -e "require('fs').writeFileSync('vault/SOUL.md','x')\"""",
    """python3 -c "import os; os.write(os.open('vault/SOUL.md',os.O_WRONLY|os.O_CREAT),b'x')\"""",
    "echo x > /dev/null/../../vault/SOUL.md",
]


@pytest.mark.parametrize("command", INLINE_WRITES)
def test_inline_programs_and_fake_dev_null_are_still_writes(command):
    assert check("chat_server", command) is not None
