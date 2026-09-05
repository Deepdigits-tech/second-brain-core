#!/usr/bin/env python3
"""Notification module for Cortex OS — macOS + Telegram."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def notify_macos(title: str, message: str) -> bool:
    """Send a macOS notification via osascript. Returns True on success."""
    escaped_msg = message.replace('"', '\\"').replace("'", "'\\''")
    escaped_title = title.replace('"', '\\"').replace("'", "'\\''")
    script = f'display notification "{escaped_msg}" with title "{escaped_title}"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def notify_telegram(message: str, parse_mode: str | None = "Markdown") -> bool:
    """Send a Telegram message. Returns True on success.

    Saves the sent message ID to chat.db message_map so that Telegram replies
    to heartbeat notifications thread back to the DM session correctly.

    `parse_mode=None` sends the text verbatim. Use it for anything containing
    identifiers: Markdown eats the underscores in CLAUDE_CODE_OAUTH_TOKEN and
    renders it as italics, which is exactly the kind of string an alert needs
    to be copy-pasteable.
    """
    try:
        sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
        from integrations.telegram_bot import send_message_sync
        result = send_message_sync(text=message, parse_mode=parse_mode)
        if result is None:
            return False

        # Save message ID → DM thread so replies resolve the session,
        # and store the notification text so replies can inject it as context.
        try:
            import os
            chat_id = str(result.get("chat_id", os.environ.get("TELEGRAM_CHAT_ID", "")))
            message_id = str(result.get("message_id", ""))
            if chat_id and message_id:
                sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "chat"))
                from session_store import SessionStore
                store = SessionStore()
                thread_key = f"telegram:{chat_id}:dm"
                store.save_message_mapping(message_id, "telegram", chat_id, thread_key)
                store.save_message_context(message_id, "telegram", chat_id, message)
        except Exception as e:
            print(f"Warning: could not save Telegram message mapping: {e}", file=sys.stderr)

        return True
    except Exception as e:
        print(f"Warning: Telegram notification failed: {e}", file=sys.stderr)
        return False


def notify(
    title: str,
    message: str,
    channels: list[str] | None = None,
    parse_mode: str | None = "Markdown",
) -> dict[str, bool]:
    """Send notification to configured channels. Returns {channel: success}.

    Pass `parse_mode=None` for messages containing identifiers — Markdown
    italicises anything between underscores, which mangles env var names in
    health alerts and makes them non-copy-pasteable.
    """
    if channels is None:
        channels = ["macos", "telegram"]

    results = {}
    formatted = f"{title}\n{message}" if title else message

    if "macos" in channels:
        results["macos"] = notify_macos(title, message)
    if "telegram" in channels:
        results["telegram"] = notify_telegram(formatted, parse_mode=parse_mode)

    return results
