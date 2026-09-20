#!/usr/bin/env python3
"""Cortex OS Chat Server — Slack interface to the Second Brain via Agent SDK."""

from __future__ import annotations

import asyncio
import argparse
import fcntl
import logging
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Add scripts to path for integration imports
sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
# Add .claude to path for chat package imports
sys.path.insert(0, str(PROJECT_ROOT / ".claude"))

from shared import PST, lane_available  # noqa: E402 — needs the scripts path inserted above

THINKING_EMOJI = "hourglass_flowing_sand"
MAX_RESPONSE_LENGTH = 4000
AGENT_SDK_TIMEOUT = 300  # seconds — prevent indefinite hangs (300s for research tasks)
PID_FILE = PROJECT_ROOT / ".claude" / "data" / "chat-server.pid"

# Single-emoji ack responses → routed as Telegram reactions on the owner's message
# instead of new chat messages. Must match the set in chat/context.py system prompt.
ALLOWED_REACTION_EMOJIS = frozenset({"👍", "👌", "🫡", "❤️", "🔥", "🤔", "👀"})

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)

# Credentials that leak through third-party loggers. httpx logs every request at
# INFO with the full URL, and the Telegram bot token lives in the URL path — so
# every 10s poll wrote the token to the journal in plaintext.
_SECRET_PATTERNS = (
    re.compile(r"/bot\d+:[A-Za-z0-9_-]{20,}"),      # Telegram: /bot<id>:<token>
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack bot/user/app tokens
    re.compile(r"\bxapp-[A-Za-z0-9-]{10,}"),        # Slack app-level (Socket Mode)
)


class RedactSecretsFilter(logging.Filter):
    """Mask credentials in any log record, whatever library emitted it.

    Lowering httpx to WARNING removes the routine leak, but an httpx *error*
    still carries the same URL — so redaction is the guarantee, not the log
    level. Runs on the handler, so it covers every logger in the process.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = message
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(lambda m: m.group(0)[:6] + "<REDACTED>", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(RedactSecretsFilter())

# Per-request HTTP logging is pure noise here (one line per 10s Telegram poll)
# and is where the token leaked from. Warnings and errors still come through.
for _noisy in ("httpx", "httpcore", "telegram.ext.Updater"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# Tools the chat bot may never use. It runs with permission_mode
# "bypassPermissions" — there is no human at the keyboard to approve anything —
# so whatever it can reach, it can do unattended, on a message from Telegram.
#
# Chosen from what it actually does. Across 13 days and 142 messages it made 20
# shell calls and **zero** Write/Edit/NotebookEdit calls, so denying these three
# costs nothing that is in use. Shell stays: the bot answers market questions by
# running the invest scripts and pulling quotes, and removing it would silently
# downgrade those answers to whatever the vault last recorded.
#
# The shell hole is closed at the hook instead. `block-secrets.py` does fire for
# this process (a `\benv\b` block appears in the chat transcripts), so it is a
# real boundary, not a Claude-Code-only one.
#
# Verified 2026-08-29 that a deny here binds even under bypassPermissions — the
# model reports the tool as unavailable rather than using it. Do not assume that
# for a new SDK version; it is one short probe to re-check.
DISALLOWED_TOOLS = ("Write", "Edit", "NotebookEdit")


async def handle_message(incoming, adapter, session_store, cortex_tools, platform: str):
    """Process an incoming message through the Agent SDK."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )
    from chat.adapters.base import IncomingMessage

    logger.info(f"Message from {incoming.user_id} in {incoming.thread_key} ({len(incoming.text)} chars)")  # 2026-09-20: log only metadata, never message text

    # Inbox-approval fast path: a reply matching today's proposal grammar is
    # handled deterministically and never reaches the Agent SDK session.
    if incoming.platform == "telegram" and lane_available("inbox"):
        try:
            from inbox import proposals as _props
            state = _props.load_state()
            parsed = _props.parse_reply(incoming.text, state) if state else None
            if parsed:
                # Clear state BEFORE executing: a reply consumes the day's
                # proposal exactly once. apply_reply can partially fail
                # (one Gmail call errors mid-loop) — clearing first means a
                # retried "ok" can't re-run already-applied items (double
                # upserts/counters/log rows). A partial failure is then
                # visible only in the confirmation counts and inbox-actions
                # log, not silently re-runnable.
                _props.clear_state()
                counts = _props.apply_reply(parsed)
                await adapter.send_reply(
                    incoming.channel_id,
                    f"Done — {len(parsed['approved'])} approved, "
                    f"{len(parsed['overruled'])} overruled. {counts}",
                    incoming.thread_id,
                    reply_to_message_id=incoming.message_id)
                return
        except Exception:
            logger.exception("inbox proposal reply handling failed")

    # Add thinking indicator
    try:
        await adapter.add_reaction(incoming.channel_id, incoming.message_id, THINKING_EMOJI)
    except Exception:
        pass

    try:
        # Look up existing session
        existing_session = session_store.get_session(incoming.thread_key)

        # Capture SDK subprocess stderr — without this, errors like
        # "is_error=True, subtype=success" arrive with no diagnostic context.
        import sys
        sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
        from sdk_stderr import make_stderr_capture
        sdk_stderr_cb, get_sdk_stderr_tail = make_stderr_capture(
            context=f"chat.{incoming.platform}:{incoming.thread_key}",
        )

        # Built per message on purpose. It was built once at server start and
        # a 46-hour-old process was answering from a 46-hour-old MEMORY.md
        # while its memory_search tool read the current one. A few file reads
        # per message is nothing next to the model call.
        from chat.context import build_system_prompt
        from shared import owner_name
        system_prompt = build_system_prompt(platform=platform)

        options_kwargs = {
            "system_prompt": system_prompt,
            "mcp_servers": {"cortex": cortex_tools},
            "max_turns": 25,
            "cwd": str(PROJECT_ROOT),
            "permission_mode": "bypassPermissions",
            "disallowed_tools": list(DISALLOWED_TOOLS),
            "env": {"CLAUDE_INVOKED_BY": "chat_server"},
            "stderr": sdk_stderr_cb,
        }

        if existing_session:
            options_kwargs["resume"] = existing_session
            logger.info(f"Resuming session {existing_session}")
        else:
            logger.info("Starting new session")

        import sdk_cli
        options = ClaudeAgentOptions(**sdk_cli.apply_cli_path(options_kwargs))

        response_parts = []
        result_session_id = None

        # Build the user prompt: append current time and any notification
        # context the user is replying to.
        # The system prompt is rebuilt per message above; the clock stays here
        # so it never lands in the cached prefix.
        now_pst = datetime.now(PST)
        now_human = now_pst.strftime("%A, %Y-%m-%d at %-I:%M %p %Z")
        prompt_parts = [f"[Current time: {now_human}]"]
        if getattr(incoming, "reply_context", None):
            prompt_parts.append(
                f"[{owner_name()} is replying to this earlier message from you — use it as context:\n"
                f"{incoming.reply_context}\n]"
            )
        prompt_parts.append(incoming.text)
        user_prompt = "\n\n".join(prompt_parts)

        async def _stream_response():
            nonlocal result_session_id
            async for msg in query(prompt=user_prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            response_parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    result_session_id = msg.session_id
                    if msg.is_error:
                        stderr_tail = get_sdk_stderr_tail()
                        tail_str = " | ".join(stderr_tail[-5:]) if stderr_tail else "(no stderr captured)"
                        logger.error(f"Agent SDK error: {msg.subtype}; stderr tail: {tail_str}")
                        response_parts.append(f"Error: {msg.subtype}")
                        # Drop the session so the next message starts fresh.
                        # A poisoned session keeps producing nonsense replies (e.g. "No response requested")
                        # on every subsequent message until cleared. Lose conversation continuity once,
                        # rather than for every future message in this thread.
                        session_store.delete_session(incoming.thread_key)

        try:
            await asyncio.wait_for(_stream_response(), timeout=AGENT_SDK_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(f"Agent SDK timed out after {AGENT_SDK_TIMEOUT}s for {incoming.thread_key}")
            response_parts.append(f"Response timed out after {AGENT_SDK_TIMEOUT}s. Try again.")

        # Save/update session
        if result_session_id:
            session_store.save_session(incoming.thread_key, result_session_id, incoming.platform)
        elif existing_session:
            session_store.update_last_active(incoming.thread_key)

        # Build response
        response_text = "\n".join(response_parts).strip()
        if not response_text:
            response_text = "I processed your message but had nothing to say."

        # Remove thinking indicator
        try:
            await adapter.remove_reaction(incoming.channel_id, incoming.message_id, THINKING_EMOJI)
        except Exception:
            pass

        # Single-emoji ack shortcut (Telegram only) — react on the owner's message
        # instead of sending a new bot message. Slack adapter inherits the
        # base-class False and skips this branch.
        stripped = response_text.strip()
        if (
            incoming.platform == "telegram"
            and stripped in ALLOWED_REACTION_EMOJIS
            and incoming.message_id
        ):
            ok = await adapter.react_to_message(
                incoming.channel_id, str(incoming.message_id), stripped
            )
            if ok:
                logger.info(f"Reacted with {stripped} on {incoming.thread_key}")
                # No bot message → nothing to map for reply-threading.
                # Next user message starts a fresh session (existing non-reply behavior).
                return
            # Reaction rejected — fall through to send_reply with the raw emoji.

        # Send response (reply visually to the user's message)
        sent_ids = await adapter.send_reply(incoming.channel_id, response_text, incoming.thread_id, reply_to_message_id=incoming.message_id)

        # Map sent bot message IDs back to this thread_key so replies resolve correctly,
        # and store the bot's reply text so user replies can reference it.
        for sid in sent_ids:
            session_store.save_message_mapping(sid, incoming.platform, incoming.channel_id, incoming.thread_key)
            session_store.save_message_context(sid, incoming.platform, incoming.channel_id, response_text)

    except Exception:
        logger.exception("Error handling message")
        # Remove thinking indicator on error
        try:
            await adapter.remove_reaction(incoming.channel_id, incoming.message_id, THINKING_EMOJI)
        except Exception:
            pass
        # Notify user
        try:
            await adapter.send_reply(
                incoming.channel_id,
                "Something went wrong. Check server logs.",
                incoming.thread_id,
            )
        except Exception:
            pass


def acquire_pid_lock():
    """Acquire exclusive PID lock or exit if another instance is running."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = open(PID_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            existing_pid = PID_FILE.read_text().strip()
        except Exception:
            existing_pid = "unknown"
        logger.error(f"Chat server already running (PID {existing_pid}). Exiting.")
        sys.exit(1)
    fd.write(str(os.getpid()))
    fd.flush()
    return fd  # Must keep reference — lock released when fd is GC'd/closed


def main():
    parser = argparse.ArgumentParser(description="Cortex OS Chat Server")
    parser.add_argument("--dry-run", action="store_true", help="Validate config without connecting")
    args = parser.parse_args()

    # Load env
    from integrations.registry import load_env
    load_env()

    # Prevent duplicate instances
    _pid_lock = acquire_pid_lock()  # noqa: F841 — must keep reference to hold lock
    logger.info(f"PID lock acquired (PID {os.getpid()})")

    # Recursion guard
    os.environ["CLAUDE_INVOKED_BY"] = "chat_server"

    # Detect available platforms
    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_app_token = os.environ.get("SLACK_APP_TOKEN")
    slack_owner = os.environ.get("SLACK_OWNER_USER_ID")
    has_slack = bool(slack_bot_token and slack_app_token)

    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_OWNER_USER_ID")
    telegram_owner_user_id = os.environ.get("TELEGRAM_OWNER_USER_ID")
    # Optional second Telegram listener: a dedicated capture group. Unset means
    # the whole capture path stays dormant.
    telegram_capture_chat_id = os.environ.get("TELEGRAM_CAPTURE_CHAT_ID")
    has_telegram = bool(telegram_bot_token)

    if not has_slack and not has_telegram:
        logger.error("No chat platform configured. Set SLACK_BOT_TOKEN+SLACK_APP_TOKEN and/or TELEGRAM_BOT_TOKEN in .claude/.env")
        sys.exit(1)

    # Initialize shared components
    from chat.session_store import SessionStore
    from chat.tools import create_cortex_tools

    session_store = SessionStore()
    expired = session_store.cleanup_old_sessions(days=7)
    if expired:
        logger.info(f"Cleaned up {expired} session(s) older than 7 days")
    cortex_tools = create_cortex_tools(PROJECT_ROOT)

    # Build per-platform config
    # (name, adapter_cls, kwargs, platform)
    adapters_config: list[tuple] = []

    if has_slack:
        if not slack_owner:
            logger.error("SLACK_OWNER_USER_ID is required — Slack adapter not started (allowlist fails closed)")
        else:
            from chat.adapters.slack_adapter import SlackAdapter
            logger.info(f"Slack configured — owner: {slack_owner}")
            adapters_config.append(("slack", SlackAdapter, {"bot_token": slack_bot_token, "app_token": slack_app_token, "owner_user_id": slack_owner}, "slack"))

    if has_telegram and not telegram_chat_id:
        logger.error("TELEGRAM_CHAT_ID is required — Telegram adapter not started (allowlist fails closed)")
        has_telegram = False

    if has_telegram:
        from chat.adapters.telegram_adapter import TelegramAdapter
        logger.info(f"Telegram configured — chat: {telegram_chat_id}")
        def _resolve_thread(message_id: str, platform: str, channel_id: str) -> str | None:
            return session_store.get_thread_key_for_message(message_id, platform, channel_id)

        def _resolve_context(message_id: str, platform: str, channel_id: str) -> str | None:
            return session_store.get_message_context(message_id, platform, channel_id)

        # The capture group rides on the SAME adapter. Telegram permits one
        # getUpdates poller per bot token, so a second adapter on this token
        # would fight the first and make both unreliable.
        if telegram_capture_chat_id == telegram_chat_id and telegram_capture_chat_id:
            logger.error(
                "TELEGRAM_CAPTURE_CHAT_ID equals TELEGRAM_CHAT_ID — the capture "
                "group must be a separate chat. Capture disabled."
            )
            telegram_capture_chat_id = None
        if telegram_capture_chat_id:
            logger.info(f"Telegram capture group configured — chat {telegram_capture_chat_id}")

        adapters_config.append(("telegram", TelegramAdapter, {"bot_token": telegram_bot_token, "allowed_chat_id": telegram_chat_id, "owner_user_id": telegram_owner_user_id, "capture_chat_id": telegram_capture_chat_id, "thread_resolver": _resolve_thread, "context_resolver": _resolve_context}, "telegram"))

    if args.dry_run:
        logger.info(f"Dry run complete — {len(adapters_config)} adapter(s) configured: {', '.join(c[0] for c in adapters_config)}")
        return

    # Create event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Instantiate adapters
    active_adapters = []
    for name, adapter_cls, kwargs, platform in adapters_config:
        adapter = adapter_cls(**kwargs, loop=loop)
        active_adapters.append((name, adapter, platform))

    # Message callback factory
    def make_callback(adapter, platform):
        async def on_message(incoming):
            # Capture-group traffic is queued and acknowledged; it never runs
            # the agent, so capture stays instant and costs nothing.
            if getattr(incoming, "is_capture", False):
                if not lane_available("captures"):
                    logger.debug("optional module captures not present — capture handler skipped")
                    return
                try:
                    from chat.capture_handler import handle_capture
                except ModuleNotFoundError as e:
                    if e.name in ("captures", "capture_handler", "chat.capture_handler"):
                        logger.debug(f"optional module {e.name} not present — capture handler skipped")
                        return
                    raise

                await handle_capture(incoming, adapter)
                return
            await handle_message(incoming, adapter, session_store, cortex_tools, platform)
        return on_message

    # Signal handlers for graceful shutdown
    def shutdown(sig, frame):
        logger.info(f"Received {signal.Signals(sig).name}, shutting down...")
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start all adapters
    logger.info(f"Starting Cortex OS Chat Server with {len(active_adapters)} adapter(s)...")

    async def start_all():
        for name, adapter, platform in active_adapters:
            callback = make_callback(adapter, platform)
            await adapter.start(callback)
            logger.info(f"{name.capitalize()} adapter started")

    loop.run_until_complete(start_all())

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        async def stop_all():
            for name, adapter, _ in active_adapters:
                await adapter.stop()
        loop.run_until_complete(stop_all())
        loop.close()
        logger.info("Chat server stopped")


if __name__ == "__main__":
    main()
