"""Chat allowlists must fail closed.

Security checklist 6.1 [CRITICAL]: an empty or missing allowlist blocks
everyone, not everyone-is-allowed. Before this change, an unset
SLACK_OWNER_USER_ID or TELEGRAM_CHAT_ID meant the adapter answered anybody
who found the bot — with a bypassPermissions agent holding Bash behind it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude"))

from chat.adapters.base import sender_allowed  # noqa: E402


@pytest.mark.parametrize("allowed", [None, "", "   "])
def test_unset_allowlist_blocks_everyone(allowed):
    assert sender_allowed("12345", allowed) is False
    assert sender_allowed("", allowed) is False
    assert sender_allowed(None, allowed) is False


def test_matching_sender_is_allowed():
    assert sender_allowed("12345", "12345") is True


def test_non_matching_sender_is_blocked():
    assert sender_allowed("99999", "12345") is False


def test_comparison_is_exact_after_strip():
    assert sender_allowed("12345", " 12345 ") is True
    assert sender_allowed(" 12345", "12345") is True
    assert sender_allowed("1234", "12345") is False


def test_telegram_adapter_uses_the_shared_check():
    source = (REPO_ROOT / ".claude" / "chat" / "adapters" / "telegram_adapter.py").read_text()
    assert "sender_allowed(" in source
    assert "self.allowed_chat_id and incoming_chat_id != self.allowed_chat_id" not in source


def test_slack_adapter_uses_the_shared_check():
    source = (REPO_ROOT / ".claude" / "chat" / "adapters" / "slack_adapter.py").read_text()
    assert "sender_allowed(" in source
    assert 'self.owner_user_id and event.get("user") != self.owner_user_id' not in source


def test_server_refuses_an_adapter_with_no_allowlist():
    source = (REPO_ROOT / ".claude" / "chat" / "server.py").read_text()
    assert "or 'disabled'" not in source, "the log line that normalised fail-open is gone"
    assert "SLACK_OWNER_USER_ID is required" in source
    assert "TELEGRAM_CHAT_ID is required" in source
