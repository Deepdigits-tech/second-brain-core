"""Guards on .claude/settings.json hook wiring.

Every hook used to be invoked as `$CLAUDE_PROJECT_DIR/.venv/bin/python3`. `.venv/`
is gitignored, so a git worktree never has one — the interpreter was missing, the
hook could not execute, and every guardrail (credential file reads, rm -rf, force
push, SOUL.md protection) silently did nothing for the whole session.

Every hook script is stdlib-only, so the venv was never actually required.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"


def _hook_commands() -> list[tuple[str, str]]:
    data = json.loads(SETTINGS.read_text())
    out = []
    for event, groups in data.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                if hook.get("type") == "command":
                    out.append((event, hook["command"]))
    return out


def test_settings_json_is_valid():
    assert _hook_commands(), "no command hooks found — settings.json wiring is broken"


def test_no_hook_hard_requires_the_gitignored_venv():
    """The worktree hole. A hook must not die just because .venv is absent."""
    offenders = [
        (event, cmd)
        for event, cmd in _hook_commands()
        if ".venv/bin/python3" in cmd and "command -v python3" not in cmd
    ]
    assert not offenders, (
        "hook(s) depend on .venv with no fallback; they will silently not run "
        f"in any git worktree: {offenders}"
    )


def test_referenced_hook_scripts_exist():
    for event, cmd in _hook_commands():
        for match in re.findall(r"/\.claude/hooks/([\w.-]+\.py)", cmd):
            assert (HOOKS_DIR / match).exists(), f"{event} references missing hook {match}"


@pytest.mark.parametrize(
    "script",
    sorted(p.name for p in HOOKS_DIR.glob("*.py")),
)
def test_hook_scripts_run_on_a_bare_interpreter(script):
    """Stdlib-only, so the fallback interpreter is safe.

    Imports are executed with the system python; a third-party import would fail
    here, which is exactly the signal that a fallback is no longer safe.
    """
    result = subprocess.run(
        ["/usr/bin/python3", "-c", f"import ast,pathlib;ast.parse(pathlib.Path('{HOOKS_DIR / script}').read_text())"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{script} does not parse: {result.stderr}"

    source = (HOOKS_DIR / script).read_text()
    third_party = re.findall(r"^\s*(?:import|from)\s+(\w+)", source, re.M)
    allowed = {
        "__future__", "json", "os", "re", "sys", "glob", "shutil", "subprocess",
        "pathlib", "datetime", "typing", "fcntl", "time", "hashlib", "argparse",
        "collections", "textwrap", "traceback", "tempfile", "urllib", "socket",
        "_flush_common", "shared",
    }
    unexpected = sorted(set(third_party) - allowed)
    assert not unexpected, (
        f"{script} imports {unexpected}, which the system-python fallback may not "
        "have — either vendor it or drop the fallback for this hook"
    )


def test_no_hook_script_hard_codes_the_venv_interpreter():
    """Finding 2.1: the hook had a fallback, the process it spawned did not."""
    offenders = []
    for script in HOOKS_DIR.glob("*.py"):
        text = script.read_text()
        if ".venv" in text and "bin" in text and "python3" in text and "shutil.which" not in text:
            offenders.append(script.name)
    assert not offenders, f"hook script(s) build a .venv interpreter path with no fallback: {offenders}"


def test_soul_hook_matcher_covers_every_edit_tool():
    """Batch-2 task 8: MultiEdit and NotebookEdit bypassed the SOUL guard before."""
    data = json.loads(SETTINGS.read_text())
    groups = data["hooks"]["PreToolUse"]
    soul = [g for g in groups if any("block-soul-edit.py" in h.get("command", "") for h in g.get("hooks", []))]
    assert soul, "no PreToolUse group wires block-soul-edit.py"
    matcher = soul[0].get("matcher", "")
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert tool in matcher.split("|"), f"SOUL matcher missing {tool}: {matcher}"
