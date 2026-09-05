"""Telegram bot integration for Cortex OS."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import telegram

_bot = None


def _get_bot() -> telegram.Bot:
    """Lazy-init Telegram Bot with token from env."""
    global _bot
    if _bot is None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
        _bot = telegram.Bot(token=token)
    return _bot


async def set_reaction(chat_id: str, message_id: str, emoji: str) -> bool:
    """Replace the emoji reaction on a message. Returns False on any rejection.

    Telegram only accepts emoji from its own fixed set, and it changes without
    notice — a rejected emoji must degrade to "no reaction changed", never to a
    raised exception, because this is cosmetic feedback on work that already
    succeeded.
    """
    # Deliberately NOT the cached _get_bot(): its HTTP client binds to the event
    # loop that created it, and set_reaction_sync runs a fresh loop per call. A
    # drain reacting to several captures would then fail every call after the
    # first with "Event loop is closed" — silently, since this path swallows
    # errors. A per-call bot costs one TLS handshake and is always correct.
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Telegram reaction skipped: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return False
    try:
        bot = telegram.Bot(token=token)
        async with bot:
            await bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=int(message_id),
                reaction=[telegram.ReactionTypeEmoji(emoji=emoji)] if emoji else [],
            )
        return True
    except Exception as e:
        print(f"Telegram reaction {emoji!r} not set: {e}", file=sys.stderr)
        return False


def set_reaction_sync(chat_id: str, message_id: str, emoji: str) -> bool:
    """Blocking wrapper — the capture pipeline is synchronous."""
    try:
        return asyncio.run(set_reaction(chat_id, message_id, emoji))
    except Exception as e:
        print(f"Telegram reaction failed: {e}", file=sys.stderr)
        return False


async def send_message(text: str, chat_id: str | None = None, parse_mode: str = "Markdown") -> dict | None:
    """Send a message to a Telegram chat (async).

    Returns message dict on success, None on failure.
    """
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        print("Warning: TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return None

    def _packed(msg):
        return {
            "message_id": msg.message_id,
            "chat_id": msg.chat.id,
            "date": msg.date.isoformat() if msg.date else None,
            "text": msg.text,
        }

    try:
        bot = _get_bot()
        msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        return _packed(msg)
    except telegram.error.BadRequest as e:
        # Telegram rejects the whole message when Markdown doesn't parse — an
        # unmatched * or _ anywhere (a brief line like "P&L *estimate" is enough).
        # Losing a morning brief to punctuation is worse than losing its bold, so
        # retry once as plain text before giving up.
        if parse_mode and "parse" in str(e).lower():
            print(f"Warning: Telegram {parse_mode} parse failed ({e}) — "
                  "resending as plain text", file=sys.stderr)
            try:
                bot = _get_bot()
                msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
                return _packed(msg)
            except telegram.error.TelegramError as e2:
                print(f"Warning: Telegram plain-text resend failed: {e2}", file=sys.stderr)
                return None
        print(f"Warning: Telegram send failed: {e}", file=sys.stderr)
        return None
    except telegram.error.TelegramError as e:
        print(f"Warning: Telegram send failed: {e}", file=sys.stderr)
        return None


def send_message_sync(text: str, chat_id: str | None = None, parse_mode: str = "Markdown") -> dict | None:
    """Send a message to a Telegram chat (sync wrapper).

    Creates a fresh Bot instance per call to avoid 'Event loop is closed' errors
    when called after a prior asyncio.run() has torn down its loop (e.g. heartbeat).
    """
    global _bot
    _bot = None  # Force fresh bot on new event loop
    return asyncio.run(send_message(text, chat_id=chat_id, parse_mode=parse_mode))


async def get_updates(offset: int | None = None, limit: int = 10) -> list[dict]:
    """Fetch recent updates (messages sent to the bot)."""
    try:
        bot = _get_bot()
        updates = await bot.get_updates(offset=offset, limit=limit)
        return [
            {
                "update_id": u.update_id,
                "message": {
                    "message_id": u.message.message_id,
                    "text": u.message.text,
                    "from": u.message.from_user.username if u.message.from_user else None,
                    "date": u.message.date.isoformat() if u.message.date else None,
                } if u.message else None,
            }
            for u in updates
        ]
    except telegram.error.TelegramError as e:
        print(f"Warning: Telegram get_updates failed: {e}", file=sys.stderr)
        return []


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from registry import load_env
    load_env()
    result = send_message_sync("Cortex OS: smoke test")
    print(f"Telegram send: {'OK' if result else 'FAILED'}")
