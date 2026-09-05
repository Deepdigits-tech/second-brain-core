#!/usr/bin/env python3
"""PreToolUse hook — guard the vault's three core files against background agents.

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
"""
import json
import os
import sys
from pathlib import PurePosixPath

GUARDED = {
    "SOUL.md": set(),
    "USER.md": set(),
    "MEMORY.md": {"reflection"},
    "core-memories.md": set(),
}
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


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


def main():
    invoked_by = os.environ.get("CLAUDE_INVOKED_BY", "")
    if not invoked_by:
        sys.exit(0)
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.exit(0)
    if data.get("tool_name", "") not in EDIT_TOOLS:
        sys.exit(0)
    tool_input = data.get("tool_input", {}) or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    name = guarded_name(path)
    if name is None:
        sys.exit(0)
    if invoked_by in GUARDED[name]:
        sys.exit(0)
    print(f"BLOCKED: {invoked_by} may not edit vault/{name}. Write suggestions to the daily log instead.", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
