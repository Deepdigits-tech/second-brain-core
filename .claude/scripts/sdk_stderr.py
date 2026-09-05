"""Capture Claude Agent SDK subprocess stderr to a shared log file.

The SDK swallows subprocess stderr by default — when the bundled `claude` CLI
fails, the only signal back to the caller is `is_error=True` plus a vague
`subtype` (sometimes contradictorily set to `"success"`). This module gives
each Agent SDK invocation a callback that the SDK pipes its subprocess stderr
into, so failures leave a paper trail.

Usage:

    from sdk_stderr import make_stderr_capture
    stderr_cb, get_stderr_tail = make_stderr_capture(context="heartbeat.main")
    options = ClaudeAgentOptions(..., stderr=stderr_cb)
    async for msg in query(prompt=..., options=options):
        if isinstance(msg, ResultMessage) and msg.is_error:
            tail = get_stderr_tail()
            log_error(f"SDK error: {msg.subtype}; stderr tail: {tail}")

All call sites write to one shared log (.claude/data/sdk_stderr.log) tagged
by context. Easier to grep across components than per-context files.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG_PATH = PROJECT_ROOT / ".claude" / "data" / "sdk_stderr.log"


def make_stderr_capture(
    *,
    context: str,
    log_path: Path | None = None,
    max_recent: int = 30,
) -> tuple[Callable[[str], None], Callable[[], list[str]]]:
    """Returns (stderr_callback, get_recent_lines).

    stderr_callback: pass to ClaudeAgentOptions(stderr=...).
    get_recent_lines(): returns the last N stderr lines (for error reporting).
    """
    path = log_path or DEFAULT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    recent: deque[str] = deque(maxlen=max_recent)
    lock = threading.Lock()

    def callback(line: str) -> None:
        with lock:
            recent.append(line)
            try:
                ts = datetime.now(timezone.utc).isoformat()
                with open(path, "a") as f:
                    f.write(f"[{ts}] [{context}] {line}\n")
            except OSError:
                pass  # never fail the SDK call because logging fails

    def get_recent() -> list[str]:
        with lock:
            return list(recent)

    return callback, get_recent
