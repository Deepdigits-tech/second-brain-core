"""The owner's name comes from config, not from source (Rule 1).

The heartbeat prompt said the owner's name 27 times; the reflection, flush and
chat prompts too. The shareable repo is these files with the name removed, so
the name has to be a value, not a literal.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / ".claude"))

NAME = re.compile(r"\bHam" + r"ed\b")  # assembled: this test file must not trip the ratchet


def test_owner_name_reads_env_with_a_generic_default(monkeypatch):
    from shared import owner_name
    monkeypatch.delenv("OWNER_NAME", raising=False)
    assert owner_name() == "the user"
    monkeypatch.setenv("OWNER_NAME", "  Ada ")
    assert owner_name() == "Ada"


def test_heartbeat_system_prompt_renders_the_owner(monkeypatch):
    monkeypatch.setenv("OWNER_NAME", "Ada")
    import heartbeat
    from shared import owner_name
    rendered = heartbeat.SYSTEM_PROMPT_TEMPLATE.replace("{owner}", owner_name())
    assert "Ada" in rendered and "{owner}" not in rendered
    assert not NAME.search(heartbeat.SYSTEM_PROMPT_TEMPLATE)


def test_reflection_and_flush_prompts_render_the_owner(monkeypatch):
    monkeypatch.setenv("OWNER_NAME", "Ada")
    import memory_reflect, memory_flush
    assert "Ada" in memory_reflect.system_prompt() and not NAME.search(memory_reflect.SYSTEM_PROMPT_TEMPLATE)
    assert "Ada" in memory_flush.system_prompt() and not NAME.search(memory_flush.SYSTEM_PROMPT_TEMPLATE)


def test_owner_is_read_at_call_time_not_import_time(monkeypatch):
    """The bug this catches: OWNER_NAME read once at import time, before
    registry.load_env() has populated the environment in production, so every
    render silently fell back to "the user" regardless of the configured name.
    """
    monkeypatch.delenv("OWNER_NAME", raising=False)
    import heartbeat, memory_reflect
    from shared import owner_name

    monkeypatch.setenv("OWNER_NAME", "Zed")
    assert "Zed" in memory_reflect.system_prompt()
    assert "Zed" in heartbeat.SYSTEM_PROMPT_TEMPLATE.replace("{owner}", owner_name())


def test_chat_preambles_render_the_owner_and_keep_literal_braces(monkeypatch, tmp_path):
    monkeypatch.setenv("OWNER_NAME", "Ada")
    import importlib
    from chat import context
    importlib.reload(context)
    from shared import lane_available
    (tmp_path / "vault" / "daily").mkdir(parents=True)
    prompt = context.build_system_prompt(vault_path=tmp_path / "vault", platform="telegram")
    assert "Ada's AI second brain" in prompt
    assert "{owner}" not in prompt
    # The state-vocabulary example (literal `{word, word}` braces that must
    # survive rendering — the preamble is built with str.replace("{owner}", ...),
    # never str.format(), which would choke on them) only ships in the
    # practices/StateShift guidance, spliced in when that lane is available.
    # In the extracted shareable tree the practices lane is optional and this
    # checkout may not have it — only require the braces when the lane is on.
    if lane_available("practices"):
        assert "{anxious, tired" in prompt, "state-vocabulary braces must survive rendering"
    assert not NAME.search(prompt)


@pytest.mark.parametrize("rel", [
    ".claude/scripts/heartbeat.py", ".claude/scripts/memory_reflect.py",
    ".claude/scripts/memory_flush.py", ".claude/chat/context.py", ".claude/chat/server.py",
])
def test_no_owner_literal_left_in_source(rel):
    assert not NAME.search((REPO_ROOT / rel).read_text()), f"{rel} still names the owner"
