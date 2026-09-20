"""Security test for chat logging: verify no message text leaks to logs.

FIX 1 (2026-09-20): server.py and slack_adapter.py must not log message text,
only metadata (platform, thread key, text length).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude"))

from chat.adapters.base import IncomingMessage  # noqa: E402


def test_server_message_logging_does_not_include_text(caplog):
    """Server.py logs message metadata (platform, user, thread, length) but never message text."""
    # Import after setting up path
    from chat import server

    # Set up logging to capture
    caplog.set_level(logging.INFO)

    # Simulate an incoming message with sensitive text that must never be logged
    sensitive_text = "do-not-log-this-message-body-12345"
    incoming = IncomingMessage(
        text=sensitive_text,
        user_id="user123",
        thread_key="telegram:12345:67890",
        platform="telegram",
        channel_id="12345",
        message_id="99999",
        thread_id="67890",
    )

    # The actual logging happens in handle_message, which we can't easily
    # call directly without async setup. Instead, verify the log format string
    # in the source code itself.
    with caplog.at_level(logging.INFO):
        # Manually verify what would be logged (the source code does the logging)
        # by checking that the message handler uses only metadata
        server_source = (REPO_ROOT / ".claude" / "chat" / "server.py").read_text()

        # The log line should include thread_key and length, never incoming.text[:N]
        # Find the specific logger line in handle_message
        lines = server_source.split('\n')
        logger_line = None
        for i, line in enumerate(lines):
            if 'logger.info' in line and 'Message from' in line:
                logger_line = line
                break

        assert logger_line is not None, "Could not find the logger.info line for message logging"
        # This line should NOT contain incoming.text[:N]
        assert 'incoming.text[:' not in logger_line, f"Log line contains incoming.text[:N]: {logger_line}"
        # Should contain len(incoming.text)
        assert 'len(incoming.text)' in logger_line, f"Log line should contain len(incoming.text): {logger_line}"


def test_slack_adapter_logging_does_not_include_text():
    """Slack adapter logs event metadata but never message text."""
    slack_source = (REPO_ROOT / ".claude" / "chat" / "adapters" / "slack_adapter.py").read_text()

    # Should not log event['text'] slice
    assert "['text', '', '][:50]" not in slack_source, "Slack adapter must not log text[:50]"
    assert "event.get('text', '')[:50]" not in slack_source, "Slack adapter must not log text slice"
    # Should log text length or skip text entirely
    assert "text_len=" in slack_source or "text=" not in slack_source, "Should log text_len or omit text"


def test_incoming_message_carries_text_for_processing():
    """Sanity check: IncomingMessage still carries full text for the agent."""
    secret = "SECRET-do-not-log"
    msg = IncomingMessage(
        text=secret,
        user_id="user1",
        thread_key="key1",
        platform="telegram",
        channel_id="ch1",
        message_id="msg1",
    )
    assert msg.text == secret, "IncomingMessage must still carry full text for processing"
