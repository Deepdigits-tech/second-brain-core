"""Abstract base class for chat platform adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Coroutine, Any


@dataclass
class IncomingMessage:
    """Platform-agnostic incoming message."""
    text: str
    user_id: str
    thread_key: str
    platform: str
    channel_id: str
    message_id: str
    thread_id: str | None = None
    is_dm: bool = True
    reply_context: str | None = None
    # True when the message came from the capture group rather than the main
    # chat — the server routes on this instead of running the agent.
    is_capture: bool = False
    # Set only for capture messages, which may carry a photo or be a forward
    # rather than plain text.
    photo_file_id: str | None = None
    document_name: str | None = None
    forward_from: str | None = None


class ChatAdapter(ABC):
    @abstractmethod
    async def start(self, message_callback: Callable[[IncomingMessage], Coroutine[Any, Any, None]]) -> None:
        """Start listening. Calls message_callback(IncomingMessage) for each message."""

    @abstractmethod
    async def send_reply(self, channel_id: str, text: str, thread_id: str | None = None, reply_to_message_id: str | None = None) -> list[str]:
        """Send a reply message. Returns list of sent message IDs."""

    @abstractmethod
    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """Add a reaction to a message (thinking indicator)."""

    @abstractmethod
    async def remove_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """Remove a reaction from a message."""

    async def react_to_message(self, channel_id: str, message_id: str, emoji: str) -> bool:
        """React with a single emoji on a user message in lieu of a text reply.

        Distinct from add_reaction (which is the transient "thinking" indicator).
        This is used when the agent's full response is one ack-tier emoji.
        Default returns False — adapters that don't support reactions inherit
        this and the server falls back to send_reply.
        """
        return False

    @abstractmethod
    async def stop(self) -> None:
        """Stop the listener."""


def sender_allowed(sender_id: str | None, allowed_id: str | None) -> bool:
    """Fail-closed allowlist check shared by every adapter.

    No configured allowlist means nobody is allowed. The bot behind these
    adapters runs with permissions bypassed and holds Bash; "answer anyone
    who finds me" is not an acceptable default for a misconfiguration.
    """
    if not allowed_id or not str(allowed_id).strip():
        return False
    if sender_id is None:
        return False
    return str(sender_id).strip() == str(allowed_id).strip()
