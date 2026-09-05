"""The chat bot's memory must not be frozen at server start (finding 7.1).

On 2026-09-01 the VPS chat service had been up 46 hours; MEMORY.md had gained
20 lines and lost 7 in that window and two days of daily logs had been written.
The running bot had seen none of it, while holding a memory_search tool that
read the current vault — two vintages of the same vault in one reply.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude"))


def test_handle_message_takes_a_platform_not_a_prebuilt_prompt():
    from chat import server
    params = list(inspect.signature(server.handle_message).parameters)
    assert params[-1] == "platform"
    assert "system_prompt" not in params


def test_prompt_is_built_inside_handle_message():
    src = (REPO_ROOT / ".claude" / "chat" / "server.py").read_text()
    head, _, body = src.partition("async def handle_message(")
    assert "build_system_prompt(" in body.split("\ndef ", 1)[0], "handle_message must call the builder itself"


def test_startup_no_longer_caches_a_prompt():
    src = (REPO_ROOT / ".claude" / "chat" / "server.py").read_text()
    assert "system_prompt_slack = build_system_prompt(" not in src
    assert "system_prompt_telegram = build_system_prompt(" not in src
