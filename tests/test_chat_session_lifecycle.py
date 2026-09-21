"""Behavioural tests for chat/server.py's handle_message session lifecycle.

Rewritten 2026-09-20. The previous version of this file never called
handle_message — it re-enacted small pieces of its control flow by hand
(`if is_error: result_session_id = None`), so it would still pass even if
server.py's real code diverged from that description. These tests instead
drive the real `handle_message` (and therefore `_handle_message_locked`)
with a fake adapter, a real SessionStore on a temp SQLite file, and a faked
`claude_agent_sdk.query`.

Covers:
1. An error ResultMessage deletes the session and does not resurrect it.
2. A successful turn saves the session under the id the SDK returned.
3. A timeout deletes the session and tells the user actions may have run.
4. Two concurrent messages on the SAME thread never overlap inside the SDK
   call (the per-thread lock added 2026-09-20); two DIFFERENT threads do.
5. The per-thread lock dict does not grow without bound.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude"))
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

from chat.session_store import SessionStore  # noqa: E402
from chat.adapters.base import IncomingMessage  # noqa: E402
from chat import server as chat_server  # noqa: E402
import claude_agent_sdk as real_sdk  # noqa: E402
import sdk_cli  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAdapter:
    """Records everything handle_message sends, does nothing real."""

    def __init__(self):
        self.replies: list[tuple[str, str]] = []  # (channel_id, text)
        self.reactions_added: list[str] = []
        self.reactions_removed: list[str] = []

    async def add_reaction(self, channel_id, message_id, emoji):
        self.reactions_added.append(emoji)

    async def remove_reaction(self, channel_id, message_id, emoji):
        self.reactions_removed.append(emoji)

    async def react_to_message(self, channel_id, message_id, emoji):
        return False  # not exercised — tests use platform="slack"

    async def send_reply(self, channel_id, text, thread_id=None, reply_to_message_id=None):
        self.replies.append((channel_id, text))
        return [f"bot-msg-{len(self.replies)}"]


class ConcurrencyTracker:
    """Counts overlapping calls into the fake SDK's query()."""

    def __init__(self):
        self.current = 0
        self.max_concurrent = 0
        self._n = 0


def make_incoming(thread_key: str, text: str = "hello", platform: str = "slack") -> IncomingMessage:
    # platform="slack" deliberately: every deterministic fast-path handler in
    # handle_message (inbox approvals, needs-you numbers, met, idea, stop/continue,
    # yes N, pipeline replies) is gated on `incoming.platform == "telegram"`, so
    # slack messages always reach the Agent SDK path these tests exercise.
    return IncomingMessage(
        text=text,
        user_id="u1",
        thread_key=thread_key,
        platform=platform,
        channel_id="chan-1",
        message_id="msg-1",
        thread_id=None,
    )


def _text_message(text: str):
    return real_sdk.AssistantMessage(content=[real_sdk.TextBlock(text=text)], model="test-model")


def _result_message(session_id: str, is_error: bool = False, subtype: str = "success"):
    return real_sdk.ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
    )


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "chat.db")


@pytest.fixture(autouse=True)
def _stub_collaborators(monkeypatch):
    """Minimal stand-ins for everything handle_message needs besides the SDK.

    build_system_prompt reads real vault files (SOUL/USER/MEMORY.md) — stubbed
    for speed and to keep this test hermetic. sdk_cli.apply_cli_path shells out
    to `claude --version` to pick a CLI binary — stubbed to identity so no
    subprocess runs. Neither has anything to do with what's under test here.
    """
    import chat.context as chat_context
    monkeypatch.setattr(chat_context, "build_system_prompt", lambda platform="slack": "test system prompt")
    monkeypatch.setattr(sdk_cli, "apply_cli_path", lambda kw: kw)


def _install_fake_query(monkeypatch, handler):
    """Patch claude_agent_sdk.query (module attribute) with an async generator.

    handle_message does `from claude_agent_sdk import query` INSIDE the
    function body on every call, so patching the module attribute (rather
    than a locally-bound name) is picked up on the next call — same pattern
    as tests/test_memory_reflect_owner.py and tests/test_guardrail.py.
    """
    monkeypatch.setattr(real_sdk, "query", handler)


def run(coro):
    """Drive a coroutine to completion. No pytest-asyncio plugin is configured
    in this repo (tests/test_capture_handler.py uses the same pattern), so
    each test gets its own fresh event loop via asyncio.run()."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# (a) error result
# ---------------------------------------------------------------------------


def test_error_result_deletes_session_and_next_call_has_no_resume(store, monkeypatch):
    store.save_session("t_err", "sess-old", "slack")

    seen_resume = []

    async def fake_query(prompt, options):
        seen_resume.append(getattr(options, "resume", "MISSING"))
        yield _text_message("something went wrong")
        yield _result_message("sess-error", is_error=True, subtype="error_during_execution")

    _install_fake_query(monkeypatch, fake_query)

    adapter = FakeAdapter()
    incoming = make_incoming("t_err")
    run(chat_server.handle_message(incoming, adapter, store, cortex_tools={}, platform="slack"))

    assert store.get_session("t_err") is None
    assert seen_resume == ["sess-old"]  # the failed call itself resumed the old session

    # A second message on the same thread must not try to resume the poisoned session.
    run(chat_server.handle_message(incoming, adapter, store, cortex_tools={}, platform="slack"))
    assert seen_resume == ["sess-old", None]


# ---------------------------------------------------------------------------
# (b) success
# ---------------------------------------------------------------------------


def test_success_saves_session_under_returned_id(store, monkeypatch):
    async def fake_query(prompt, options):
        yield _text_message("here's your answer")
        yield _result_message("sess-new-123")

    _install_fake_query(monkeypatch, fake_query)

    adapter = FakeAdapter()
    incoming = make_incoming("t_ok")
    run(chat_server.handle_message(incoming, adapter, store, cortex_tools={}, platform="slack"))

    assert store.get_session("t_ok") == "sess-new-123"
    assert adapter.replies and "here's your answer" in adapter.replies[0][1]


# ---------------------------------------------------------------------------
# (c) timeout
# ---------------------------------------------------------------------------


def test_timeout_deletes_session_and_warns_user(store, monkeypatch):
    store.save_session("t_timeout", "sess-before-timeout", "slack")
    monkeypatch.setattr(chat_server, "AGENT_SDK_TIMEOUT", 0.05)

    async def fake_query(prompt, options):
        await asyncio.sleep(1)
        yield _text_message("too slow")  # never reached

    _install_fake_query(monkeypatch, fake_query)

    adapter = FakeAdapter()
    incoming = make_incoming("t_timeout")
    run(chat_server.handle_message(incoming, adapter, store, cortex_tools={}, platform="slack"))

    assert store.get_session("t_timeout") is None
    assert adapter.replies
    reply_text = adapter.replies[0][1]
    assert "timed out" in reply_text
    assert "already" in reply_text  # "actions may have already run"


# ---------------------------------------------------------------------------
# (d) concurrency: same thread serializes, different threads overlap
# ---------------------------------------------------------------------------


def test_same_thread_key_never_overlaps_in_the_sdk_call(store, monkeypatch):
    tracker = ConcurrencyTracker()

    async def fake_query(prompt, options):
        tracker.current += 1
        tracker.max_concurrent = max(tracker.max_concurrent, tracker.current)
        try:
            await asyncio.sleep(0.05)
            tracker._n += 1
            yield _text_message(f"reply {tracker._n}")
            yield _result_message(f"sess-{tracker._n}")
        finally:
            tracker.current -= 1

    _install_fake_query(monkeypatch, fake_query)

    adapter = FakeAdapter()
    incoming_a = make_incoming("t_same", text="first")
    incoming_b = make_incoming("t_same", text="second")

    async def _both():
        await asyncio.gather(
            chat_server.handle_message(incoming_a, adapter, store, cortex_tools={}, platform="slack"),
            chat_server.handle_message(incoming_b, adapter, store, cortex_tools={}, platform="slack"),
        )

    run(_both())

    assert tracker.max_concurrent == 1
    assert len(adapter.replies) == 2
    # The lock is released after this thread's messages are done.
    assert "t_same" not in chat_server._thread_locks
    assert "t_same" not in chat_server._thread_lock_refcounts


def test_different_thread_keys_run_concurrently(store, monkeypatch):
    tracker = ConcurrencyTracker()

    async def fake_query(prompt, options):
        tracker.current += 1
        tracker.max_concurrent = max(tracker.max_concurrent, tracker.current)
        try:
            await asyncio.sleep(0.05)
            tracker._n += 1
            yield _text_message(f"reply {tracker._n}")
            yield _result_message(f"sess-{tracker._n}")
        finally:
            tracker.current -= 1

    _install_fake_query(monkeypatch, fake_query)

    adapter = FakeAdapter()
    incoming_a = make_incoming("t_diff_a")
    incoming_b = make_incoming("t_diff_b")

    async def _both():
        await asyncio.gather(
            chat_server.handle_message(incoming_a, adapter, store, cortex_tools={}, platform="slack"),
            chat_server.handle_message(incoming_b, adapter, store, cortex_tools={}, platform="slack"),
        )

    run(_both())

    assert tracker.max_concurrent == 2


# ---------------------------------------------------------------------------
# (e) lock dict does not grow unboundedly
# ---------------------------------------------------------------------------


def test_lock_dict_does_not_grow_without_bound(store, monkeypatch):
    async def fake_query(prompt, options):
        yield _text_message("ok")
        yield _result_message("sess-x")

    _install_fake_query(monkeypatch, fake_query)

    adapter = FakeAdapter()

    async def _many():
        for i in range(50):
            incoming = make_incoming(f"t_many_{i}")
            await chat_server.handle_message(incoming, adapter, store, cortex_tools={}, platform="slack")

    run(_many())

    assert chat_server._thread_locks == {}
    assert chat_server._thread_lock_refcounts == {}
