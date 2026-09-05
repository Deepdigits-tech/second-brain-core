"""Tests for Agent SDK failure reporting.

The SDK returns is_error=True with subtype="success" when the CLI is not
authenticated. All three call sites logged only the subtype, so a hard auth
failure wrote the literal word "success" into an error log — 448 times in
heartbeat_errors.log while the morning brief, task proposals and memory
promotion silently produced nothing for weeks.

The real signal is in the assistant text: "Not logged in · Please run /login".
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import agent_result  # noqa: E402

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


def _result(subtype="success", stop_reason=None, errors=None):
    return SimpleNamespace(
        subtype=subtype, is_error=True, stop_reason=stop_reason, errors=errors
    )


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Not logged in · Please run /login",
        "Please run /login to continue",
        "401 OAuth access token is invalid",
        "Invalid API key · Please run /login",
    ],
)
def test_auth_failures_are_recognised(text):
    reason, is_auth = agent_result.describe_failure(_result(), text, "(no stderr)")

    assert is_auth is True
    assert "not authenticated" in reason.lower()


def test_auth_reason_names_the_fix_not_the_subtype():
    reason, _ = agent_result.describe_failure(
        _result(subtype="success"), "Not logged in · Please run /login", "(no stderr)"
    )

    assert "claude setup-token" in reason
    assert reason.strip() != "success", "the old message was literally this"


def test_subtype_alone_is_never_the_whole_message():
    """The bug: 'Agent SDK error: success' told nobody anything."""
    reason, is_auth = agent_result.describe_failure(
        _result(subtype="success"), "some unrelated model output", "(no stderr)"
    )

    assert is_auth is False
    assert reason != "success"
    assert "some unrelated model output" in reason, "assistant text carries the real signal"


def test_max_turns_is_reported_plainly():
    reason, is_auth = agent_result.describe_failure(
        _result(subtype="error_max_turns"), "half an answer", "(no stderr)"
    )

    assert is_auth is False
    assert "max_turns" in reason


def test_errors_list_is_included_when_present():
    reason, _ = agent_result.describe_failure(
        _result(errors=["rate limited", "retry"]), "", "(no stderr)"
    )

    assert "rate limited" in reason


def test_stop_reason_and_stderr_survive():
    reason, _ = agent_result.describe_failure(
        _result(stop_reason="stop_sequence"), "", "boom | crash"
    )

    assert "stop_sequence" in reason and "boom" in reason


def test_empty_assistant_text_does_not_crash():
    reason, is_auth = agent_result.describe_failure(_result(), "", "(no stderr)")

    assert isinstance(reason, str) and reason
    assert is_auth is False


def test_long_assistant_text_is_trimmed():
    reason, _ = agent_result.describe_failure(_result(), "x" * 5000, "(no stderr)")

    assert len(reason) < 1200, "an error log line must stay readable"


# --- alert de-duplication ---------------------------------------------------


def test_auth_alert_fires_once_then_suppresses(tmp_path, monkeypatch):
    """Heartbeat runs every 30 min. 448 failures must not mean 448 pings."""
    sent = []
    monkeypatch.setattr(agent_result, "_send", lambda title, body: sent.append(title))
    state = tmp_path / "auth-alert.json"

    first = agent_result.alert_auth_failure("reason", now=NOW, state_path=state)
    second = agent_result.alert_auth_failure("reason", now=NOW + timedelta(hours=2), state_path=state)

    assert first is True and second is False
    assert len(sent) == 1


def test_auth_alert_fires_again_the_next_day(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(agent_result, "_send", lambda title, body: sent.append(title))
    state = tmp_path / "auth-alert.json"

    agent_result.alert_auth_failure("reason", now=NOW, state_path=state)
    again = agent_result.alert_auth_failure("reason", now=NOW + timedelta(days=1), state_path=state)

    assert again is True
    assert len(sent) == 2


def test_corrupt_alert_state_does_not_suppress_the_alert(tmp_path, monkeypatch):
    """Failing to read the de-dup file must not mean failing to warn."""
    sent = []
    monkeypatch.setattr(agent_result, "_send", lambda title, body: sent.append(title))
    state = tmp_path / "auth-alert.json"
    state.write_text("{not json")

    assert agent_result.alert_auth_failure("reason", now=NOW, state_path=state) is True


def test_alert_never_raises(tmp_path, monkeypatch):
    """A failed notification must not mask the original failure."""
    def boom(*a, **kw):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(agent_result, "_send", boom)

    assert agent_result.alert_auth_failure(
        "reason", now=NOW, state_path=tmp_path / "s.json"
    ) is False


def test_state_file_records_the_date(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_result, "_send", lambda title, body: None)
    state = tmp_path / "auth-alert.json"

    agent_result.alert_auth_failure("reason", now=NOW, state_path=state)

    assert json.loads(state.read_text())["last_alert_date"] == "2026-08-15"


# --- all three call sites must use it ---------------------------------------


@pytest.mark.parametrize(
    "script", ["memory_reflect.py", "heartbeat.py", "memory_flush.py"]
)
def test_call_sites_no_longer_log_bare_subtype(script):
    """The exact line that produced 'Agent SDK error: success' 448 times."""
    source = (REPO_ROOT / ".claude" / "scripts" / script).read_text()

    assert "Agent SDK error: {msg.subtype}" not in source, "bare subtype is back"
    assert "agent_result.describe_failure" in source, f"{script} must use the helper"
    assert "alert_auth_failure" in source, f"{script} must alert on auth failure"
