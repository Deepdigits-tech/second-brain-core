#!/usr/bin/env python3
"""Pick the Claude Code CLI the Agent SDK should run, and retry transient failures.

Why this exists (2026-08-15): every automated Cortex run — heartbeat, reflection,
capture, meeting prep — was intermittently returning
`API Error: 400 status code (no body)` with `input_tokens: 0`, meaning the request
was rejected before the model ever saw it. Session transcripts for the day showed
all 80 failures came from CLI **2.1.92**, the copy bundled inside
`claude-agent-sdk` 0.1.56, which `_find_cli()` prefers over anything installed.
Zero failures came from any newer CLI. The machine had 2.1.233 installed and
unused.

Two defences, because the failure is intermittent and version correlation is
strong evidence rather than proof:

1. `best_cli_path()` — run the newest CLI on the machine instead of the stale
   bundled one.
2. `run_with_retry()` — retry an API-level rejection a couple of times with
   backoff, so one bad response cannot silently cost the owner a morning brief.

A failure that survives the retries is raised/reported, never swallowed.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Candidates in the order we would like to use them; the newest version wins.
_CANDIDATES = [
    Path.home() / ".local/bin/claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
    Path.home() / ".claude/local/claude",
    Path.home() / ".npm-global/bin/claude",
]

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _version_of(path: Path) -> tuple[int, int, int] | None:
    try:
        out = subprocess.run([str(path), "--version"], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION_RE.search(out.stdout or "")
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


@functools.lru_cache(maxsize=1)
def best_cli_path() -> str | None:
    """Newest installed CLI, or None to let the SDK choose.

    Override with CORTEX_CLAUDE_CLI when a specific binary must be used.
    """
    override = os.environ.get("CORTEX_CLAUDE_CLI")
    if override and Path(override).is_file():
        return override

    seen: dict[str, tuple[int, int, int]] = {}
    candidates = list(_CANDIDATES)
    which = shutil.which("claude")
    if which:
        candidates.insert(0, Path(which))

    for c in candidates:
        try:
            if not c.is_file():
                continue
            real = str(c.resolve())
        except OSError:
            continue
        if real in seen:
            continue
        v = _version_of(c)
        if v:
            seen[str(c)] = v

    if not seen:
        return None
    return max(seen.items(), key=lambda kv: kv[1])[0]


def cli_kwargs() -> dict:
    """`**sdk_cli.cli_kwargs()` for inline ClaudeAgentOptions(...) call sites."""
    path = best_cli_path()
    return {"cli_path": path} if path else {}


def apply_cli_path(kwargs: dict) -> dict:
    """Add cli_path to ClaudeAgentOptions kwargs unless one was set explicitly."""
    if kwargs.get("cli_path"):
        return kwargs
    path = best_cli_path()
    if path:
        kwargs["cli_path"] = path
    return kwargs


def is_transient_api_failure(result_text: str | None) -> bool:
    """Is this the bodyless rejection worth retrying?

    Narrow on purpose: a 400 carrying a real body means our request was wrong and
    retrying it will fail identically. Overload/rate limits are also transient.
    """
    if not result_text:
        return False
    t = result_text.strip()
    return bool(
        re.search(r"API Error:\s*(400|429|500|502|503|529)\s+status code \(no body\)", t)
        or re.search(r"API Error:\s*(429|500|502|503|529)\b", t)
    )


async def run_with_retry(call, attempts: int = 3, base_delay: float = 5.0, log=None):
    """Run an async SDK call, retrying only transient API rejections.

    `call` must be an async callable returning (ok: bool, result_text: str, payload).
    Returns the last (ok, result_text, payload). Never swallows a failure — the
    caller still sees ok=False and logs it.
    """
    last = (False, "", None)
    for attempt in range(1, attempts + 1):
        ok, text, payload = await call()
        last = (ok, text, payload)
        if ok or not is_transient_api_failure(text):
            return last
        if attempt < attempts:
            delay = base_delay * attempt
            if log:
                log(f"transient API failure ({text.strip()[:60]}) — "
                    f"retry {attempt}/{attempts - 1} in {delay:.0f}s")
            await asyncio.sleep(delay)
    return last


if __name__ == "__main__":
    p = best_cli_path()
    print(f"selected CLI: {p}")
    if p:
        print(subprocess.run([p, "--version"], capture_output=True, text=True).stdout.strip())
