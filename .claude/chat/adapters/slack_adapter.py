"""Slack Socket Mode adapter for the chat interface."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Callable, Coroutine, Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from .base import ChatAdapter, IncomingMessage, sender_allowed

logger = logging.getLogger(__name__)


class SlackAdapter(ChatAdapter):
    def __init__(
        self,
        bot_token: str,
        app_token: str,
        owner_user_id: str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        self.app = App(token=bot_token)
        self.app_token = app_token
        self.owner_user_id = owner_user_id
        self.loop = loop
        self.handler: SocketModeHandler | None = None

        # Resolve bot's own user ID for self-filtering
        try:
            auth = self.app.client.auth_test()
            self.bot_user_id = auth["user_id"]
        except Exception as e:
            logger.warning(f"Could not resolve bot user ID: {e}")
            self.bot_user_id = None

    async def start(self, message_callback: Callable[[IncomingMessage], Coroutine[Any, Any, None]]) -> None:
        """Start Socket Mode listener. Runs blocking in a thread."""

        def _handle_event(event: dict, say) -> None:
            logger.info(f"Slack event: user={event.get('user')}, subtype={event.get('subtype')}, text_len={len(event.get('text', ''))} chars")  # 2026-09-20: log only metadata, never message text
            # Filter bot messages and edits
            if event.get("subtype") is not None:
                return
            if "bot_id" in event:
                return
            if event.get("user") == self.bot_user_id:
                return
            # Owner restriction
            if not sender_allowed(event.get("user"), self.owner_user_id):
                logger.debug(f"Ignoring message from non-owner: {event.get('user')}")
                return

            text = event.get("text", "").strip()
            if not text:
                return

            channel = event["channel"]
            ts = event["ts"]
            thread_ts = event.get("thread_ts")
            # Thread key: root thread_ts or message ts if it's a root message
            thread_root = thread_ts or ts
            thread_key = f"slack:{channel}:{thread_root}"

            msg = IncomingMessage(
                text=text,
                user_id=event["user"],
                thread_key=thread_key,
                platform="slack",
                channel_id=channel,
                message_id=ts,
                thread_id=thread_root,
                is_dm=event.get("channel_type") == "im",
            )

            if self.loop:
                asyncio.run_coroutine_threadsafe(message_callback(msg), self.loop)
            else:
                asyncio.run(message_callback(msg))

        # Track processed messages to deduplicate (message + app_mention fire for @mentions in channels)
        seen_messages: set[str] = set()

        def _dedup_handler(event: dict, say) -> None:
            msg_id = event.get("ts", "")
            if msg_id in seen_messages:
                return
            seen_messages.add(msg_id)
            # Cap set size to prevent memory leak
            if len(seen_messages) > 1000:
                seen_messages.clear()
            _handle_event(event, say)

        self.app.event("message")(_dedup_handler)
        self.app.event("app_mention")(_dedup_handler)

        self.handler = SocketModeHandler(self.app, self.app_token)
        # connect() is non-blocking — returns immediately
        self.handler.connect()
        logger.info("Slack adapter connected via Socket Mode")

    async def send_reply(self, channel_id: str, text: str, thread_id: str | None = None, reply_to_message_id: str | None = None) -> list[str]:
        """Send a reply, splitting long messages at paragraph boundaries."""
        chunks = _split_message(text)
        sent_ids: list[str] = []
        for chunk in chunks:
            try:
                kwargs = {"channel": channel_id, "text": chunk}
                if thread_id:
                    kwargs["thread_ts"] = thread_id
                resp = self.app.client.chat_postMessage(**kwargs)
                sent_ids.append(resp["ts"])
            except SlackApiError as e:
                logger.error(f"Slack send failed: {e.response['error']}")
        return sent_ids

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        try:
            self.app.client.reactions_add(channel=channel_id, timestamp=message_id, name=emoji)
        except SlackApiError:
            pass  # Reaction may already exist

    async def remove_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        try:
            self.app.client.reactions_remove(channel=channel_id, timestamp=message_id, name=emoji)
        except SlackApiError:
            pass  # Reaction may not exist

    async def stop(self) -> None:
        if self.handler:
            self.handler.close()
            logger.info("Slack adapter disconnected")


def _split_message(text: str, max_length: int = 4000) -> list[str]:
    """Split long messages at paragraph boundaries."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break

        # Try to split at paragraph boundary
        cut = text.rfind("\n\n", 0, max_length)
        if cut == -1:
            # Fall back to newline
            cut = text.rfind("\n", 0, max_length)
        if cut == -1:
            # Hard cut
            cut = max_length

        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()

    return chunks
