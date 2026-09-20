"""Telegram sender check (TELEGRAM_OWNER_USER_ID).

The chat id says WHERE a message was posted; it does not say WHO posted it.
In the capture group that meant anyone in the group could enqueue captures.
These tests call the gate itself. The first version of this check rejected
every message when the variable was unset, and tests that only read the
source text passed — which would have silenced the bot on deploy.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude"))

from chat.adapters.telegram_adapter import TelegramAdapter  # noqa: E402

OWNER = "111"
STRANGER = "999"
GROUP = "-500"


def _adapter(**kw):
    return TelegramAdapter(bot_token="x", capture_chat_id=GROUP, **kw)


def test_unset_owner_id_changes_nothing():
    a = _adapter(allowed_chat_id=OWNER)
    assert a._gate(OWNER, OWNER) == (True, False)      # owner's DM
    assert a._gate(STRANGER, STRANGER) == (False, False)  # someone else's DM
    assert a._gate(STRANGER, GROUP) == (True, True)    # old behaviour: group id is enough


def test_owner_id_alone_still_opens_the_dm():
    """The VPS sets only TELEGRAM_OWNER_USER_ID; in a DM chat id == user id."""
    a = _adapter(owner_user_id=OWNER)
    assert a._gate(OWNER, OWNER) == (True, False)


def test_owner_id_set_rejects_a_stranger_in_the_capture_group():
    a = _adapter(allowed_chat_id=OWNER, owner_user_id=OWNER)
    assert a._gate(STRANGER, GROUP) == (False, True)
    assert a._gate(OWNER, GROUP) == (True, True)


def test_owner_id_set_rejects_a_stranger_in_an_allowed_chat():
    """If TELEGRAM_CHAT_ID is ever pointed at a group, membership is not enough."""
    a = _adapter(allowed_chat_id="-700", owner_user_id=OWNER)
    assert a._gate(STRANGER, "-700") == (False, False)
    assert a._gate(OWNER, "-700") == (True, False)


def test_nothing_configured_fails_closed():
    a = TelegramAdapter(bot_token="x")
    assert a._gate(OWNER, OWNER) == (False, False)


def test_example_env_lists_the_variable():
    assert "TELEGRAM_OWNER_USER_ID" in (REPO_ROOT / "master.env.example").read_text()
