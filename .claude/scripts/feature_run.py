"""Per-feature execution helper for heartbeat sub-flows.

Wraps each named sub-flow with three things the bare try/except pattern misses:

1. Per-feature toggle (machine-local, .claude/data/state/feature-toggles.json).
   Lets you turn off practice/meeting-prep/digest without code edits.

2. Structured timing log (.claude/data/feature-timings.jsonl, append-only).
   One JSON line per invocation: {ts, feature, status, duration_ms, error?}.
   status: ok|error|disabled.
   Lets you answer "what's running, how often, how long, how often failing"
   over any time window — `jq` on the file.

3. Failure isolation. One feature's exception stays inside feature_run() and
   gets logged; never propagates to the heartbeat orchestrator.

Usage in heartbeat.py:

    from feature_run import feature_run
    classify_result = feature_run("inbox.classify", classify_inbox, data, diff,
                                  log=debug, default={}) or {}
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TOGGLE_PATH = PROJECT_ROOT / ".claude" / "data" / "state" / "feature-toggles.json"
TIMINGS_PATH = PROJECT_ROOT / ".claude" / "data" / "feature-timings.jsonl"


def is_enabled(feature: str, default: bool = True) -> bool:
    """Return whether a feature is enabled. Defaults to True if file or key missing."""
    if not TOGGLE_PATH.exists():
        return default
    try:
        with open(TOGGLE_PATH) as f:
            toggles = json.load(f)
    except (json.JSONDecodeError, OSError):
        return default
    val = toggles.get(feature)
    if val is None:
        return default
    return bool(val)


def feature_run(
    name: str,
    fn: Callable,
    *args,
    default_enabled: bool = True,
    log: Callable[[str], None] | None = None,
    default: Any = None,
    **kwargs,
) -> Any:
    """Run fn(*args, **kwargs) under a feature gate with timing + failure isolation.

    Returns fn's result on success, `default` on skip or error.
    """
    if not is_enabled(name, default=default_enabled):
        _write_timing({"ts": datetime.now(timezone.utc).isoformat(), "feature": name,
                       "status": "disabled", "duration_ms": 0})
        if log:
            log(f"feature {name}: skipped (disabled)")
        return default

    start = time.monotonic()
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "feature": name,
    }
    try:
        result = fn(*args, **kwargs)
        record["status"] = "ok"
        record["duration_ms"] = int((time.monotonic() - start) * 1000)
        _write_timing(record)
        if log:
            log(f"feature {name}: ok in {record['duration_ms']}ms")
        return result
    except Exception as e:
        record["status"] = "error"
        record["duration_ms"] = int((time.monotonic() - start) * 1000)
        record["error"] = type(e).__name__
        record["error_msg"] = str(e)[:200]
        _write_timing(record)
        if log:
            log(f"feature {name}: ERROR {type(e).__name__}: {e}")
        return default


def _write_timing(record: dict) -> None:
    try:
        TIMINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TIMINGS_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # never fail the heartbeat because the log won't write
