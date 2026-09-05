"""Slack integration for Cortex OS — send + receive via Socket Mode."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

_client = None


def _get_client() -> WebClient:
    """Lazy-init Slack WebClient with bot token from env."""
    global _client
    if _client is None:
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError("SLACK_BOT_TOKEN not set")
        _client = WebClient(token=token)
    return _client


def send_message(text: str, channel: str | None = None, thread_ts: str | None = None) -> dict | None:
    """Send a text message to a Slack channel.

    Returns API response data on success, None on failure.
    """
    channel = channel or os.environ.get("SLACK_CHANNEL_ID")
    if not channel:
        print("Warning: SLACK_CHANNEL_ID not set", file=sys.stderr)
        return None

    try:
        client = _get_client()
        kwargs = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        response = client.chat_postMessage(**kwargs)
        return response.data
    except SlackApiError as e:
        print(f"Warning: Slack send failed: {e.response['error']}", file=sys.stderr)
        return None


def send_blocks(blocks: list[dict], text: str, channel: str | None = None) -> dict | None:
    """Send a Block Kit message. `text` is the fallback for notifications."""
    channel = channel or os.environ.get("SLACK_CHANNEL_ID")
    if not channel:
        print("Warning: SLACK_CHANNEL_ID not set", file=sys.stderr)
        return None

    try:
        client = _get_client()
        response = client.chat_postMessage(channel=channel, blocks=blocks, text=text)
        return response.data
    except SlackApiError as e:
        print(f"Warning: Slack send_blocks failed: {e.response['error']}", file=sys.stderr)
        return None


def start_listener(message_handler: Callable | None = None) -> None:
    """Start Socket Mode listener (blocking).

    Registers message and app_mention event handlers.
    If message_handler is provided, it's called with event data for each message.
    """
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN required for listener")

    app = App(token=bot_token)

    @app.event("message")
    def handle_message(event, say):
        if message_handler:
            message_handler(event)

    @app.event("app_mention")
    def handle_mention(event, say):
        if message_handler:
            message_handler(event)

    handler = SocketModeHandler(app, app_token)
    handler.start()


def start_listener_background():
    """Non-blocking Socket Mode listener. Returns handler for cleanup."""
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN required for listener")

    app = App(token=bot_token)

    @app.event("message")
    def handle_message(event, say):
        pass

    @app.event("app_mention")
    def handle_mention(event, say):
        pass

    handler = SocketModeHandler(app, app_token)
    handler.connect()
    return handler


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from registry import load_env
    load_env()
    result = send_message("Cortex OS: smoke test")
    print(f"Slack send: {'OK' if result else 'FAILED'}")
