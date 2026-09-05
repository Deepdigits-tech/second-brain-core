"""Turn an Agent SDK failure into something a human can act on.

The SDK reports `is_error=True` alongside `subtype="success"` when the `claude`
CLI cannot authenticate — the process exited cleanly, the work did not happen.
All three call sites (heartbeat, reflection, memory flush) logged only the
subtype, so the error log filled with the literal word "success": 448 times in
heartbeat_errors.log while the morning brief, task proposals and memory
promotion quietly produced nothing for weeks.

The real signal never appears in `subtype`. It is in the assistant text:

    Not logged in · Please run /login

Data gathering does not need the LLM, so the system keeps looking alive.
See docs/MACHINE-SETUP.md §1 for the token fix this points people at.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ALERT_STATE = PROJECT_ROOT / ".claude" / "data" / "state" / "agent-auth-alert.json"

# Substrings that mean "the CLI could not authenticate", not "the model said something".
AUTH_MARKERS = (
    "not logged in",
    "/login",
    "oauth access token is invalid",
    "invalid api key",
    "authentication_error",
)

_MAX_TEXT = 600
_FIX_HINT = "run `claude setup-token` and put CLAUDE_CODE_OAUTH_TOKEN in .claude/.env (docs/MACHINE-SETUP.md §1)"


def describe_failure(msg, assistant_text: str, stderr_tail: str) -> tuple[str, bool]:
    """Build a log line for a failed ResultMessage.

    Returns (reason, is_auth_failure). `subtype` is included but never alone —
    it is the field that lied.
    """
    text = (assistant_text or "").strip()
    lowered = text.lower()
    is_auth = any(marker in lowered for marker in AUTH_MARKERS)

    parts = []
    if is_auth:
        parts.append(f"CLI not authenticated — LLM step did nothing. Fix: {_FIX_HINT}")

    subtype = getattr(msg, "subtype", None)
    parts.append(f"subtype={subtype!r}")

    stop_reason = getattr(msg, "stop_reason", None)
    if stop_reason:
        parts.append(f"stop_reason={stop_reason!r}")

    errors = getattr(msg, "errors", None)
    if errors:
        parts.append(f"errors={errors!r}")

    if text:
        trimmed = text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"
        parts.append(f"output={trimmed!r}")

    if stderr_tail and stderr_tail != "(no stderr)":
        parts.append(f"stderr={stderr_tail}")

    return "; ".join(parts), is_auth


def _send(title: str, body: str) -> None:
    from notify import notify

    notify(title, body)


def alert_auth_failure(
    reason: str,
    now: datetime | None = None,
    state_path: Path | None = None,
) -> bool:
    """Tell the operator the CLI is not authenticated. At most once a day.

    Heartbeat runs every 30 minutes; alerting on every failure would be 20 pings
    a day and would train the operator to ignore them. Once a day is enough to
    be impossible to miss and rare enough to still mean something.

    Never raises — a failed notification must not mask the original failure.
    """
    now = now or datetime.now(timezone.utc)
    path = Path(state_path) if state_path else DEFAULT_ALERT_STATE
    today = now.date().isoformat()

    try:
        # A corrupt or unreadable state file must not suppress the warning.
        last = ""
        if path.exists():
            try:
                last = json.loads(path.read_text()).get("last_alert_date", "")
            except (json.JSONDecodeError, OSError):
                last = ""
        if last == today:
            return False

        _send("Cortex: Claude CLI not authenticated", reason)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_alert_date": today}, indent=2) + "\n")
        return True
    except Exception:
        return False
