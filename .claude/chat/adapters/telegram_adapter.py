"""Telegram long-polling adapter for the chat interface."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Coroutine, Any

from telegram import Update, ReactionTypeEmoji
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, MessageHandler, ContextTypes, filters

from .base import ChatAdapter, IncomingMessage, sender_allowed

logger = logging.getLogger(__name__)


class TelegramAdapter(ChatAdapter):
    def __init__(
        self,
        bot_token: str,
        allowed_chat_id: str | None = None,
        owner_user_id: str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        thread_resolver: Callable[[str, str, str], str | None] | None = None,
        context_resolver: Callable[[str, str, str], str | None] | None = None,
        capture_chat_id: str | None = None,
    ):
        self.bot_token = bot_token
        # Support both param names — VPS uses owner_user_id, local uses allowed_chat_id
        self.allowed_chat_id = allowed_chat_id or owner_user_id
        # The capture group is served by THIS adapter, not a second one.
        # Telegram allows exactly one getUpdates poller per bot token, so a
        # second TelegramAdapter on the same token makes the two fight and both
        # go unreliable. One poller, routed by chat id.
        self.capture_chat_id = capture_chat_id
        self.loop = loop
        self.app: Application | None = None
        self._bot_id: int | None = None
        self._thread_resolver = thread_resolver
        self._context_resolver = context_resolver

    async def start(self, message_callback: Callable[[IncomingMessage], Coroutine[Any, Any, None]]) -> None:
        """Start long-polling listener."""
        self.app = Application.builder().token(self.bot_token).build()

        # Resolve bot user ID
        bot_info = await self.app.bot.get_me()
        self._bot_id = bot_info.id
        logger.info(f"Telegram bot: @{bot_info.username} (id={self._bot_id})")

        async def _handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.message is None:
                return

            user = update.message.from_user
            if not user:
                return

            # Skip own messages
            if user.id == self._bot_id:
                return

            incoming_chat_id = str(update.message.chat_id)
            is_capture = bool(self.capture_chat_id and incoming_chat_id == self.capture_chat_id)

            if not is_capture and not sender_allowed(incoming_chat_id, self.allowed_chat_id):
                logger.debug(f"Ignoring message from chat {update.message.chat_id}")
                return

            # Outside the capture group only plain text is actionable; a photo
            # or a document has nothing for the agent to answer.
            if not is_capture and update.message.text is None:
                return

            photo_file_id: str | None = None
            document_name: str | None = None
            forward_from: str | None = None

            if is_capture:
                # A caption on a photo is the note that came with it.
                text = (update.message.text or update.message.caption or "").strip()
                if update.message.photo:
                    # photo is a list of sizes, ascending — the last is the largest.
                    photo_file_id = update.message.photo[-1].file_id
                if update.message.document:
                    document_name = update.message.document.file_name
                forward_from = _forward_attribution(update.message)
                # An empty forward or an unsupported attachment has nothing to save.
                if not text and not photo_file_id and not document_name:
                    return
            else:
                text = update.message.text.strip()
                if not text:
                    return

            chat_id = str(update.message.chat_id)
            message_id = str(update.message.message_id)
            user_id = str(user.id)

            # Reply-based threading: reply to a bot message continues the session
            # Look up which session the replied-to message belongs to via message_map
            reply = update.message.reply_to_message
            reply_context: str | None = None
            if reply and reply.from_user and reply.from_user.is_bot:
                reply_msg_id = str(reply.message_id)
                if self._thread_resolver:
                    resolved = self._thread_resolver(reply_msg_id, "telegram", chat_id)
                    thread_key = resolved if resolved else f"telegram:{chat_id}:dm"
                else:
                    thread_key = f"telegram:{chat_id}:dm"
                if self._context_resolver:
                    reply_context = self._context_resolver(reply_msg_id, "telegram", chat_id)
                # Fall back to the raw reply text if we didn't store context (e.g. older bot messages)
                if not reply_context and reply.text:
                    reply_context = reply.text
            else:
                thread_key = f"telegram:{chat_id}:dm"

            msg = IncomingMessage(
                text=text,
                user_id=user_id,
                thread_key=thread_key,
                platform="telegram",
                channel_id=chat_id,
                message_id=message_id,
                thread_id=None,
                is_dm=update.message.chat.type == "private",
                reply_context=reply_context,
                is_capture=is_capture,
                photo_file_id=photo_file_id,
                document_name=document_name,
                forward_from=forward_from,
            )

            # Fire-and-forget: don't block update processing while Agent SDK runs
            asyncio.create_task(_safe_callback(message_callback, msg))

        async def _safe_callback(callback, msg):
            try:
                await callback(msg)
            except Exception:
                logger.exception(f"Error in message callback for {msg.thread_key}")

        if self.capture_chat_id:
            # Attachments must reach the handler for the capture group; the
            # handler itself drops non-text coming from anywhere else.
            self.app.add_handler(MessageHandler(~filters.COMMAND, _handle_update))
        else:
            self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_update))

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message"],
        )
        logger.info("Telegram adapter connected via long-polling")

    async def send_reply(self, channel_id: str, text: str, thread_id: str | None = None, reply_to_message_id: str | None = None) -> list[str]:
        """Send a reply, splitting long messages at paragraph boundaries."""
        if not self.app:
            return []
        chunks = _split_message(text)
        sent_ids: list[str] = []
        for i, chunk in enumerate(chunks):
            try:
                # Only reply-to on the first chunk so the thread stays clean
                kwargs: dict[str, Any] = {
                    "chat_id": int(channel_id),
                    "text": chunk,
                    "parse_mode": None,
                }
                if i == 0 and reply_to_message_id:
                    kwargs["reply_to_message_id"] = int(reply_to_message_id)
                msg = await self.app.bot.send_message(**kwargs)
                sent_ids.append(str(msg.message_id))
            except TelegramError as e:
                logger.error(f"Telegram send failed: {e}")
        return sent_ids

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """No-op — used by the server as a Slack-style 'thinking' indicator
        with a Slack emoji name (e.g. 'hourglass_flowing_sand') that isn't
        valid in Telegram's allowed reaction set. See react_to_message for
        the actual emoji-reaction path on Telegram."""
        pass

    async def remove_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """No-op — counterpart to add_reaction (thinking indicator)."""
        pass

    async def react_to_message(self, channel_id: str, message_id: str, emoji: str) -> bool:
        """Set a single emoji reaction on a user message via Bot API setMessageReaction.

        Returns True on success, False if the emoji is rejected (not in
        Telegram's default allowed set) or the call otherwise errors —
        caller falls back to send_reply.
        """
        if not self.app:
            return False
        try:
            await self.app.bot.set_message_reaction(
                chat_id=int(channel_id),
                message_id=int(message_id),
                reaction=[ReactionTypeEmoji(emoji=emoji)] if emoji else [],
            )
            return True
        except BadRequest as e:
            logger.warning(f"Telegram reaction rejected for emoji={emoji!r}: {e}")
            return False
        except TelegramError as e:
            logger.error(f"Telegram set_message_reaction failed: {e}")
            return False

    async def download_file(self, file_id: str, dest_path: str) -> bool:
        """Download a Telegram file (photo/document) to dest_path.

        Returns False on any Telegram error — a capture whose image failed to
        download is still worth saving as a note.
        """
        if not self.app:
            return False
        try:
            file = await self.app.bot.get_file(file_id)
            await file.download_to_drive(dest_path)
            return True
        except TelegramError as e:
            logger.error(f"Telegram file download failed: {e}")
            return False

    async def stop(self) -> None:
        if self.app:
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            logger.info("Telegram adapter disconnected")


def _forward_attribution(message) -> str | None:
    """Who a forwarded message originally came from, or None if not a forward.

    Telegram models the origin as one of several types depending on whether the
    original sender is a user, a channel, or someone who hid their account.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None

    sender_user = getattr(origin, "sender_user", None)
    if sender_user:
        if sender_user.username:
            return f"@{sender_user.username}"
        name = f"{sender_user.first_name or ''} {sender_user.last_name or ''}".strip()
        return name or "unknown"

    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat:
        return chat.title or chat.username or str(chat.id)

    hidden = getattr(origin, "sender_user_name", None)
    return hidden or "unknown"


def _split_message(text: str, max_length: int = 4096) -> list[str]:
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
