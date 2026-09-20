#!/usr/bin/env python3
"""PreToolUse hook — guard the vault's four core files against background agents.

The guard is by FILE, with an allow-list of agents per file. Before 2026-09-02 it
was the other way round (two agents, one file), so the chat server — a long-running
agent with vault write tools, reachable from Telegram — could rewrite USER.md.

  SOUL.md            nobody
  USER.md            nobody
  MEMORY.md          reflection only (its job is to rewrite it)
  core-memories.md   nobody

An interactive session (CLAUDE_INVOKED_BY unset) is never blocked: the owner is
driving. Exit code 2 blocks the tool call. Stdlib only — this runs on the
fallback interpreter inside worktrees.

2026-09-20: this hook only ever looked at Edit/Write/MultiEdit/NotebookEdit and
exited 0 for Bash, so an automated agent with a Bash tool (the chat bot, via
Telegram) could reach the same four files through `cp`, `mv`, `dd`, `tee -a`,
`sed -i`, a python one-liner, or plain `>`/`>>` redirection — none of which
block-secrets.py's vault-write patterns are scoped to catch, since those exist
to stop deletion/truncation of the vault generally, not to enforce the
per-file agent allow-list above. Bash is now handled the same way: for an
automated caller, a command that both names one of the four files (as a bare
basename — `vault/SOUL.md` and a bare `SOUL.md` both match; over-blocking a
same-named file elsewhere is accepted, see check_bash_command()) and contains
any write-capable construct is blocked, subject to the same per-file allow
list above (so reflection's `>> vault/MEMORY.md` still passes). A second,
name-independent check blocks any inline interpreter one-liner (`python3 -c
...`, a heredoc, etc.) that also contains a file-write primitive, because a
path built by string concatenation or otherwise obfuscated cannot be caught
by name-matching at all — see the module docstring on check_bash_command for
what this still cannot catch.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import PurePosixPath

GUARDED = {
    "SOUL.md": set(),
    "USER.md": set(),
    "MEMORY.md": {"reflection"},
    "core-memories.md": set(),
}
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

_NAMES = tuple(GUARDED.keys())
# A guarded basename, bounded by anything that isn't a word/dot/hyphen char on
# either side — matches both `vault/SOUL.md` and a bare `SOUL.md` (no path
# separator at all), which is the accepted over-block for automated callers:
# porting block-secrets.py's full cd-resolution here isn't worth the
# duplication for a hook whose only live caller today is the chat bot, and a
# same-named file outside the vault costs nothing to also refuse.
_NAME_RE = re.compile(r'(?<![\w.-])(' + "|".join(re.escape(n) for n in _NAMES) + r')(?![\w.-])')

# Anything that can put new bytes on disk. Deliberately broad (bare `cp`,
# `mv`, `dd`, ... anywhere in the command) — this only ever fires alongside a
# protected-name match, and over-blocking an automated agent is cheap; an
# interactive session is never subject to this hook at all.
_HARMLESS_REDIRECT_RE = re.compile(r'\d*>>?\s*/dev/null(?=\s|$|[;&|)])|\d*>&\d+')

_WRITE_CONSTRUCT_RE = re.compile(
    r'>'
    r'|\btee\b'
    r'|\bcp\b'
    r'|\bmv\b'
    r'|\bdd\b'
    r'|\binstall\b'
    r'|\brsync\b'
    r'|\bln\b'
    r'|\b(?:sed|perl)\s+(?:-[a-zA-Z]*i|-pi|--in-place)\b'
    r'|\btruncate\b'
    r'|\bgit\s+(?:checkout|restore|apply)\b'
    r'|\b(?:python3?|perl|ruby|node|php)\b[^\n|;&]*\s-[a-zA-Z]*[ce](?:\s|$)'
    r'|\b(?:python3?|perl|ruby|node|php)\b[^\n|;&]*<<'
    r'|\bsh\s+-c\b'
    r'|\bbash\s+-c\b'
)

# Rule 2: a path built by string concatenation (`'vau' + 'lt/' + 'SOUL.md'`,
# chr-code assembly, etc.) can defeat _NAME_RE entirely — it isn't a
# pattern-matching problem, it's an information problem, so this check
# doesn't try to find the target at all. Instead it blocks the SHAPE of the
# attack outright for automated callers: an interpreter run inline (a `-c`/
# `-e` flag, or a heredoc feeding a shell) combined with any primitive that
# writes a file. This is unconditional — it does not know the target is
# MEMORY.md, so it cannot apply reflection's exemption; reflection has no
# Bash tool today, so this never actually fires for it.
_INTERPRETER_INLINE_RE = re.compile(
    r'\b(?:python3?|perl|ruby|node|php)\b[^\n|;&]*\s-[a-zA-Z]*[ce](?:\s|$)'
    r'|<<-?\s*[\'"]?\w+[\'"]?'
)
_WRITE_PRIMITIVE_RE = re.compile(
    r"open\([^\n]*?,\s*['\"][xwa][\w+]?['\"]"
    r"|\.write_text\("
    r"|\.write_bytes\("
    r"|\bshutil\."
    r"|\bos\.rename\("
    r"|\bos\.replace\("
    r"|Path\([^)\n]*\)\s*\.\s*write"
    r"|\bos\.open\("
    r"|\.open\([^)\n]*['\"][xwa]"
    r"|writeFile"
    r"|appendFile"
)


def guarded_name(path):
    """The core-file name when `path` is `<...>/vault/<NAME>`, else None.

    A SOUL.md sitting directly under a project folder is not the vault's file;
    one directly under any `vault/` directory is treated as guarded, which also
    covers nested product-vault skeletons — blocking a background write there
    costs nothing.
    """
    parts = PurePosixPath(path).parts
    if len(parts) >= 2 and parts[-2] == "vault" and parts[-1] in GUARDED:
        return parts[-1]
    # A bare filename with no path separators at all (e.g. an agent invoked
    # with just "SOUL.md" as the target) has no "vault" segment to check
    # against — treat it as guarded too rather than letting it slip through.
    if len(parts) == 1 and parts[0] in GUARDED:
        return parts[0]
    return None


def check_bash_command(invoked_by: str, tool_input: dict) -> str | None:
    """Block an automated caller's Bash command from writing a guarded file.

    Two independent checks:
      1. The command names a guarded basename (see _NAME_RE) AND contains a
         write-capable construct (see _WRITE_CONSTRUCT_RE) AND the caller
         isn't on that file's allow list.
      2. The command runs an interpreter inline (a `-c`/`-e` one-liner or a
         heredoc) AND contains a file-write primitive — blocked outright,
         whatever the target, because an obfuscated path (string
         concatenation, `chr()` assembly, base64, ...) never appears as the
         literal basename check 1 needs.

    What this still cannot catch: a write staged in two separate Bash calls
    (build a script file in one call with a name that dodges check 1's
    literal-basename scan, run it — not `-c`/`-e` — in the next), or any
    write that goes through neither a shell redirect/verb nor an interpreter
    this hook recognizes (e.g. a compiled binary, or a language not listed in
    _WRITE_CONSTRUCT_RE/_INTERPRETER_INLINE_RE). Defense in depth, not a
    sandbox.
    """
    cmd = tool_input.get("command") or ""
    if not cmd:
        return None

    names = set(_NAME_RE.findall(cmd))
    # A bare interpreter is not a write: the chat bot runs every lookup as
    # a python script under .claude/scripts, and a search FOR "MEMORY.md" must
    # not read as a write TO it. Only an INLINE program (`-c`, `-e`, heredoc)
    # counts as a write construct. Discarding output (`2>/dev/null`, `2>&1`)
    # is not a write either — but only when /dev/null is the whole target.
    shell = _HARMLESS_REDIRECT_RE.sub(' ', cmd)
    if names and _WRITE_CONSTRUCT_RE.search(shell):
        for name in sorted(names):
            if invoked_by not in GUARDED.get(name, set()):
                return (
                    f"{invoked_by} may not write vault/{name} via Bash. "
                    "Write suggestions to the daily log instead."
                )

    if _INTERPRETER_INLINE_RE.search(cmd) and _WRITE_PRIMITIVE_RE.search(cmd):
        return (
            f"{invoked_by} may not run an inline interpreter one-liner that writes "
            "files via Bash — it could target a protected vault file by an "
            "obfuscated path. Write suggestions to the daily log instead."
        )

    return None


def main():
    invoked_by = os.environ.get("CLAUDE_INVOKED_BY", "")
    if not invoked_by:
        sys.exit(0)
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}

    if tool_name in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        name = guarded_name(path)
        if name is not None and invoked_by not in GUARDED[name]:
            print(f"BLOCKED: {invoked_by} may not edit vault/{name}. Write suggestions to the daily log instead.", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)

    if tool_name == "Bash":
        reason = check_bash_command(invoked_by, tool_input)
        if reason:
            print(f"BLOCKED: {reason}", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
