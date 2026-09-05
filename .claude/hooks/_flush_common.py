"""Shared body of the two memory-flush hooks (PreCompact and SessionEnd).

Copies the transcript somewhere the original's deletion cannot reach, resolves
an interpreter that exists on THIS checkout, and spawns memory_flush.py fully
detached so the hook never blocks the session.

The interpreter fallback is the point. .venv/ is gitignored, so a git worktree
has none; the hooks themselves were given a fallback in August, but the
process they spawned still hard-coded the venv path and raised
FileNotFoundError — the session's memory was silently never written. Same
class of failure, one layer down.
"""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys

DEBUG_LOG = "/tmp/cortex_hook_debug.log"


def _debug(hook_name: str, msg: str) -> None:
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] {hook_name}: {msg}\n")
    except OSError:
        pass


def resolve_interpreter(cwd: str) -> str | None:
    """The project venv if it exists here, else the first python3 on PATH."""
    venv_py = os.path.join(cwd, ".venv", "bin", "python3")
    if os.access(venv_py, os.X_OK):
        return venv_py
    return shutil.which("python3")


def spawn_flush(hook_input: dict, hook_name: str) -> bool:
    """Spawn memory_flush.py for this session. Returns True if spawned."""
    _debug(hook_name, "hook fired")

    if os.environ.get("CLAUDE_INVOKED_BY"):
        _debug(hook_name, "recursion guard triggered — exiting")
        return False

    session_id = hook_input.get("session_id", "")
    transcript_path = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd", "") or os.getcwd()
    _debug(hook_name, f"session_id={session_id} transcript_path={transcript_path} cwd={cwd}")

    if not transcript_path or not os.path.exists(transcript_path):
        _debug(hook_name, f"no transcript — transcript_path={transcript_path!r}")
        return False

    interpreter = resolve_interpreter(cwd)
    if not interpreter:
        _debug(hook_name, "no python3 interpreter available — flush skipped")
        return False

    tmp_path = f"/tmp/cortex_flush_{session_id}.jsonl"
    shutil.copy2(transcript_path, tmp_path)

    flush_script = os.path.join(cwd, ".claude", "scripts", "memory_flush.py")
    _debug(hook_name, f"spawning: {interpreter} {flush_script} {tmp_path} {session_id}")
    subprocess.Popen(
        [interpreter, flush_script, tmp_path, session_id],
        env={**os.environ, "CLAUDE_INVOKED_BY": "memory_flush"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _debug(hook_name, "background process spawned — exiting hook")
    return True
