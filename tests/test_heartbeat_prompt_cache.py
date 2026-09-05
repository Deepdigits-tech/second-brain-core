"""Guard the heartbeat's system prompt against cache invalidators.

Prompt caching is a prefix match: a value that changes between runs
invalidates everything after it. The heartbeat fires ~32 times a day, so a
clock in the system prompt meant every run re-wrote the whole prompt to cache
instead of reading it. Measured 2026-08-16: removing it shifted ~10.8k tokens
per run from cache-write to cache-read.

These tests fail if a volatile value drifts back into the system prompt.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import heartbeat  # noqa: E402

TEMPLATE = heartbeat.SYSTEM_PROMPT_TEMPLATE


def test_template_has_no_clock_placeholder():
    """A time-of-day value changes every run and voids the whole cached prefix."""
    assert "{now_human}" not in TEMPLATE
    assert "{now}" not in TEMPLATE
    assert "{timestamp}" not in TEMPLATE


def test_template_placeholders_are_all_stable():
    """Every remaining placeholder must be stable for at least a cache lifetime.

    `today` changes once a day (one rebuild, not 32); soul and user come from
    vault files that change rarely. `owner` is rendered via `.replace()` before
    `.format()` runs (Task 6), so by the time `.format` sees the template it is
    already gone — it never reaches the cached prefix as a live placeholder.
    Anything else is a new invalidator and should go in the context prompt instead.
    """
    placeholders = set(re.findall(r"\{(\w+)\}", TEMPLATE))
    assert placeholders <= {"soul_content", "user_content", "today", "owner"}, (
        f"Unexpected placeholder(s) in the system prompt: "
        f"{placeholders - {'soul_content', 'user_content', 'today', 'owner'}}. "
        "Volatile values belong in the context prompt, after the cached prefix."
    )


def test_template_renders_without_a_time_argument():
    """The call site must not need to pass a clock in."""
    rendered = TEMPLATE.replace("{owner}", heartbeat.owner_name()).format(soul_content="S", user_content="U", today="2026-08-16")
    assert "Cortex OS heartbeat agent" in rendered
    assert "S" in rendered and "U" in rendered


def test_no_time_of_day_string_survives_rendering():
    """Catch a literal clock baked in rather than interpolated."""
    rendered = TEMPLATE.replace("{owner}", heartbeat.owner_name()).format(soul_content="", user_content="", today="2026-08-16")
    assert not re.search(r"\d{1,2}:\d{2}\s*(AM|PM)", rendered, re.I)


def test_current_time_moved_to_the_context_prompt():
    """The model still needs the time — it just belongs after the cached prefix."""
    source = (REPO_ROOT / ".claude" / "scripts" / "heartbeat.py").read_text()
    assert 'sections = [f"## Current Time\\n{now_human}"]' in source, (
        "The clock should seed the context prompt; the model still needs it."
    )
