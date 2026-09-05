"""Session lifetime for the chat bot.

Written after a real failure. On 2026-08-29 the owner asked the bot a question.
It answered correctly from the vault, then closed with
"the +7% move this week, driven by Treasury buyback news + dollar weakness".
That number is nowhere in the vault. It came from web searches the bot ran on
2026-08-19 and 2026-08-21 — inside the same conversation, which had been open
since 2026-08-16 across 142 messages. Ten-day-old market commentary, restated
as "this week".

That is worse than a fabrication, because it looks sourced. Fabrication has
nothing behind it and can be caught; this had a real search behind it and only
the date was wrong.

Three faults made one conversation immortal:

1. Every Telegram DM resolves to the same thread_key, so every message resumed
   the same session. CLAUDE.md claimed a fresh message started a new one.
2. save_session used INSERT OR REPLACE, resetting created_at on every message.
   The row said the session was minutes old; the transcript said 13 days.
3. cleanup_old_sessions ran once at startup and keyed off last_active, so for
   anyone who messages daily it could never fire.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude"))

from chat.session_store import SessionStore  # noqa: E402

KEY = "telegram:431916622:dm"


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "chat.db")


def age(store, key, *, created_hours_ago=0.0, active_hours_ago=0.0):
    """Backdate a stored session, the way real elapsed time would."""
    now = datetime.now(timezone.utc)
    store.db.execute(
        "UPDATE sessions SET created_at = ?, last_active = ? WHERE thread_key = ?",
        (
            (now - timedelta(hours=created_hours_ago)).isoformat(),
            (now - timedelta(hours=active_hours_ago)).isoformat(),
            key,
        ),
    )
    store.db.commit()


# --------------------------------------------------------------------------
# created_at must survive the conversation it describes
# --------------------------------------------------------------------------

def test_saving_the_same_session_again_does_not_reset_its_age(store):
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=100, active_hours_ago=100)

    store.save_session(KEY, "sess-1", "telegram")  # another message arrives

    row = store.list_sessions()[0]
    created = datetime.fromisoformat(row["created_at"])
    hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    assert hours > 90, "created_at was reset — the session's real age is lost"


def test_a_genuinely_new_session_starts_its_clock_now(store):
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=100, active_hours_ago=100)

    store.save_session(KEY, "sess-2", "telegram")  # different session id

    row = store.list_sessions()[0]
    created = datetime.fromisoformat(row["created_at"])
    hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    assert hours < 1, "a new session id must start a new clock"


def test_last_active_still_moves_on_every_message(store):
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=100, active_hours_ago=100)

    store.update_last_active(KEY)

    row = store.list_sessions()[0]
    seen = datetime.fromisoformat(row["last_active"])
    assert (datetime.now(timezone.utc) - seen).total_seconds() < 60


# --------------------------------------------------------------------------
# rotation: 8 hours idle, or 24 hours old
# --------------------------------------------------------------------------

def test_a_fresh_session_is_reused(store):
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=2, active_hours_ago=0.5)
    assert store.get_session(KEY) == "sess-1"


def test_an_idle_session_is_retired(store):
    """Eight hours without a word is a night's sleep. Start clean."""
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=9, active_hours_ago=9)
    assert store.get_session(KEY) is None


def test_a_long_running_session_is_retired_even_while_in_use(store):
    """The 08-16 conversation never went idle for long. Age alone must end it."""
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=30, active_hours_ago=0.2)
    assert store.get_session(KEY) is None


def test_the_boundary_is_not_off_by_an_hour(store):
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=23, active_hours_ago=7.5)
    assert store.get_session(KEY) == "sess-1", "just inside both limits"


def test_a_retired_session_is_removed_not_just_hidden(store):
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=30, active_hours_ago=30)
    store.get_session(KEY)
    assert store.list_sessions() == [], "a retired row must not linger"


def test_the_next_message_after_rotation_opens_a_clean_session(store):
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=30, active_hours_ago=30)
    assert store.get_session(KEY) is None

    store.save_session(KEY, "sess-2", "telegram")
    assert store.get_session(KEY) == "sess-2"


def test_rotation_limits_are_configurable_per_store(store):
    """So a Slack thread can be given different limits without a code change."""
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=2, active_hours_ago=1)

    tight = SessionStore(store.db_path, max_idle_hours=0.5, max_age_hours=1)
    assert tight.get_session(KEY) is None


# --------------------------------------------------------------------------
# what must not change
# --------------------------------------------------------------------------

def test_message_context_survives_rotation(store):
    """Replying to an old bot message must still carry that message as context.

    Rotation drops the conversation, not the quoted text — otherwise a reply to
    this morning's brief would arrive with nothing attached.
    """
    store.save_message_context("m1", "telegram", "c1", "Friday Morning Brief ...")
    store.save_session(KEY, "sess-1", "telegram")
    age(store, KEY, created_hours_ago=30, active_hours_ago=30)
    store.get_session(KEY)

    assert store.get_message_context("m1", "telegram", "c1") == "Friday Morning Brief ..."


def test_unknown_thread_is_simply_absent(store):
    assert store.get_session("telegram:999:dm") is None


# --------------------------------------------------------------------------
# cleanup_old_sessions prunes message tables too
# --------------------------------------------------------------------------

def test_cleanup_prunes_old_message_context_and_map(store):
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    store.save_message_mapping("m-old", "telegram", "c", "telegram:c:dm")
    store.save_message_context("m-old", "telegram", "c", "old text")
    store.db.execute("UPDATE message_context SET saved_at = ? WHERE message_id = 'm-old'", (old,))
    store.save_message_mapping("m-new", "telegram", "c", "telegram:c:dm")
    store.save_message_context("m-new", "telegram", "c", "new text")
    store.db.commit()
    store.cleanup_old_sessions(days=7)
    assert store.get_message_context("m-old", "telegram", "c") is None
    assert store.get_thread_key_for_message("m-old", "telegram", "c") is None
    assert store.get_message_context("m-new", "telegram", "c") == "new text"
    assert store.get_thread_key_for_message("m-new", "telegram", "c") == "telegram:c:dm"


def test_cleanup_return_value_still_counts_sessions_only(store):
    store.save_message_context("m", "telegram", "c", "x")
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    store.db.execute("UPDATE message_context SET saved_at = ? WHERE message_id = 'm'", (old,))
    store.db.commit()
    assert store.cleanup_old_sessions(days=7) == 0


def test_map_rows_without_context_are_pruned_by_missing_partner(store):
    """message_map has no timestamp; a row whose context partner is gone is pruned with it."""
    store.save_message_mapping("orphan", "slack", "c", "slack:c:t")
    store.db.commit()
    store.cleanup_old_sessions(days=7)
    assert store.get_thread_key_for_message("orphan", "slack", "c") is None
