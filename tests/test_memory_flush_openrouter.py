"""Memory flush via OpenRouter, with the Agent SDK as fallback.

The flush is the second-heaviest LLM consumer in the system (34-58 real calls
on 2026-08-15/16, behind only the heartbeat) and it is a single-shot, no-tool
text call — the same shape capture already runs on OpenRouter.

The risk that makes these tests worth having: flush output is appended
VERBATIM to vault/daily/. A chattier model that prefixes "Here's what I
extracted:" would write that line into the log, and one that answers
"FLUSH_OK - nothing notable" instead of the bare sentinel would have its
refusal written in as content. normalize_flush_output is the guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import memory_flush as mf  # noqa: E402

BULLETS = "- Decided to move the flush to OpenRouter\n- Heartbeat brain is VPS-only"


class _Resp:
    def __init__(self, content):
        self.choices = [
            type("C", (), {"message": type("M", (), {"content": content})()})()
        ]


@pytest.fixture
def fake_openai(monkeypatch):
    """Stub the OpenAI client and capture what was sent."""
    sent: dict = {"_reply": BULLETS}

    class FakeCompletions:
        def create(self, **kw):
            sent.update(kw)
            if isinstance(sent["_reply"], Exception):
                raise sent["_reply"]
            return _Resp(sent["_reply"])

    class FakeClient:
        def __init__(self, **kw):
            sent["client_kwargs"] = kw
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return sent


# --- normalize_flush_output: the guard on what reaches the daily log ---------


@pytest.mark.parametrize(
    "raw",
    [
        "FLUSH_OK",
        "  FLUSH_OK  ",
        "flush_ok",
        "FLUSH_OK - nothing notable was discussed",
        "FLUSH_OK.",
        "```\nFLUSH_OK\n```",
    ],
)
def test_sentinel_variants_all_mean_nothing_to_write(raw):
    """A model that decorates the sentinel must not have its refusal logged."""
    assert mf.normalize_flush_output(raw) == "FLUSH_OK"


def test_preamble_before_bullets_is_dropped():
    raw = "Here's what I extracted from the conversation:\n\n" + BULLETS
    assert mf.normalize_flush_output(raw) == BULLETS


def test_code_fences_are_stripped():
    raw = "```markdown\n" + BULLETS + "\n```"
    assert mf.normalize_flush_output(raw) == BULLETS


def test_clean_bullets_pass_through_unchanged():
    assert mf.normalize_flush_output(BULLETS) == BULLETS


def test_asterisk_and_numbered_bullets_count_as_bullets():
    raw = "Summary:\n* first thing\n* second thing"
    assert mf.normalize_flush_output(raw) == "* first thing\n* second thing"

    raw2 = "Notes:\n1. first thing\n2. second thing"
    assert mf.normalize_flush_output(raw2) == "1. first thing\n2. second thing"


def test_prose_with_no_bullets_is_treated_as_nothing_to_write():
    """Better to write nothing than to append a chatty paragraph to the log."""
    assert mf.normalize_flush_output("The user and I talked about scheduling.") == "FLUSH_OK"


def test_empty_output_is_nothing_to_write():
    assert mf.normalize_flush_output("") == "FLUSH_OK"
    assert mf.normalize_flush_output("   \n  ") == "FLUSH_OK"


# --- the OpenRouter call ----------------------------------------------------


def test_uses_kimi_and_the_flush_system_prompt(fake_openai):
    text, err = mf._flush_via_openrouter("transcript here")

    assert err == ""
    assert text == BULLETS
    assert fake_openai["model"] == mf.OPENROUTER_MODEL
    assert fake_openai["client_kwargs"]["base_url"] == mf.OPENROUTER_BASE_URL
    assert fake_openai["messages"][0]["role"] == "system"
    assert fake_openai["messages"][0]["content"] == mf.system_prompt()
    assert "transcript here" in fake_openai["messages"][1]["content"]


def test_output_is_normalized_on_the_way_out(fake_openai):
    fake_openai["_reply"] = "Here's what I found:\n\n" + BULLETS
    text, err = mf._flush_via_openrouter("t")
    assert err == ""
    assert text == BULLETS


def test_missing_key_reports_reason_and_does_not_call(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    text, err = mf._flush_via_openrouter("t")
    assert text == ""
    assert "OPENROUTER_API_KEY" in err


def test_provider_error_reports_reason_rather_than_raising(fake_openai):
    fake_openai["_reply"] = RuntimeError("502 upstream")
    text, err = mf._flush_via_openrouter("t")
    assert text == ""
    assert "502 upstream" in err


def test_empty_provider_reply_is_an_error_not_a_silent_flush_ok(fake_openai):
    """An outage returning "" must fall through to the SDK, not look like
    'nothing worth keeping' — that would silently drop a day of memory."""
    fake_openai["_reply"] = ""
    text, err = mf._flush_via_openrouter("t")
    assert text == ""
    assert err != ""


# --- wiring: the guard must actually sit on the write path ------------------


def test_openrouter_output_reaches_the_daily_log(tmp_path, monkeypatch, fake_openai):
    """A guard nobody calls is not a guard — pin that flush() uses this path."""
    import asyncio

    monkeypatch.setattr(mf, "DAILY_DIR", tmp_path)
    monkeypatch.setattr(mf, "parse_transcript", lambda p: "some transcript")
    monkeypatch.setattr(mf, "check_content_dedup", lambda h: False)
    monkeypatch.setattr(mf, "update_dedup", lambda *a, **k: None)
    fake_openai["_reply"] = "Here's what I extracted:\n\n" + BULLETS

    asyncio.run(mf.flush("ignored.jsonl", "sess-1"))

    written = next(tmp_path.glob("*.md")).read_text()
    assert BULLETS in written
    assert "Here's what I extracted" not in written, "preamble must never be logged"


def test_nothing_to_write_leaves_no_daily_entry(tmp_path, monkeypatch, fake_openai):
    import asyncio

    monkeypatch.setattr(mf, "DAILY_DIR", tmp_path)
    monkeypatch.setattr(mf, "parse_transcript", lambda p: "some transcript")
    monkeypatch.setattr(mf, "check_content_dedup", lambda h: False)
    monkeypatch.setattr(mf, "update_dedup", lambda *a, **k: None)
    fake_openai["_reply"] = "FLUSH_OK - nothing notable came up"

    asyncio.run(mf.flush("ignored.jsonl", "sess-2"))

    assert list(tmp_path.glob("*.md")) == [], "a decorated sentinel must write nothing"
