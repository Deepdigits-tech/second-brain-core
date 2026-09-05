"""SQLite-backed session store mapping platform thread keys to Agent SDK session IDs."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "chat.db"


# How long a conversation may live before the next message starts a clean one.
#
# A chat session is resumed context, and resumed context is quietly authoritative:
# the model treats what it said last week as something it knows. On 2026-08-29 the
# bot answered a question about gold correctly from the vault, then added "+7% this
# week, driven by Treasury buyback news" — market commentary it had web-searched on
# 08-19 and 08-21, inside the same conversation, restated eight days later as
# current. Nothing was fabricated; only the date was wrong, which is why it read as
# sourced.
#
# Eight idle hours is a night's sleep — a real conversational boundary. The 24-hour
# cap covers the case that actually happened: a conversation used every few hours,
# every day, that therefore never went idle long enough to lapse.
DEFAULT_MAX_IDLE_HOURS = 8
DEFAULT_MAX_AGE_HOURS = 24


class SessionStore:
    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        max_idle_hours: float = DEFAULT_MAX_IDLE_HOURS,
        max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    ):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.max_idle_hours = max_idle_hours
        self.max_age_hours = max_age_hours
        self.db = sqlite3.connect(str(db_path))
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            thread_key TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS message_map (
            message_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_key TEXT NOT NULL,
            PRIMARY KEY (message_id, platform, channel_id)
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS message_context (
            message_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            content TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            PRIMARY KEY (message_id, platform, channel_id)
        )""")
        self.db.commit()

    def get_session(self, thread_key: str) -> str | None:
        """Return the live session for this thread, retiring it if it is stale.

        Rotation is checked here, on the read, rather than on a timer. The
        previous cleanup ran once at server start and keyed off last_active, so
        for anyone who messages daily it could never fire — the conversation
        that prompted this had been open 13 days.
        """
        row = self.db.execute(
            "SELECT session_id, created_at, last_active FROM sessions WHERE thread_key = ?",
            (thread_key,),
        ).fetchone()
        if not row:
            return None

        session_id, created_at, last_active = row
        now = datetime.now(timezone.utc)

        def hours_since(stamp: str) -> float:
            try:
                then = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                # An unreadable timestamp must not pin a session open forever.
                return float("inf")
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            return (now - then).total_seconds() / 3600

        idle = hours_since(last_active)
        age = hours_since(created_at)
        if idle >= self.max_idle_hours or age >= self.max_age_hours:
            reason = "idle" if idle >= self.max_idle_hours else "age"
            logger.info(
                "Rotating session for %s (%s: idle %.1fh, age %.1fh) — next message starts fresh",
                thread_key, reason, idle, age,
            )
            self.delete_session(thread_key)
            return None

        return session_id

    def save_session(self, thread_key: str, session_id: str, platform: str) -> None:
        """Record the session for this thread, preserving its true start time.

        created_at used to be overwritten on every message, so the column always
        read "just now" however old the conversation was — the row said minutes,
        the transcript said 13 days. Age is the thing rotation is judged on, so
        it has to survive the messages that make up the conversation.
        """
        now = datetime.now(timezone.utc).isoformat()
        existing = self.db.execute(
            "SELECT session_id, created_at FROM sessions WHERE thread_key = ?",
            (thread_key,),
        ).fetchone()
        created_at = existing[1] if existing and existing[0] == session_id else now

        self.db.execute(
            "INSERT OR REPLACE INTO sessions (thread_key, session_id, platform, created_at, last_active) VALUES (?, ?, ?, ?, ?)",
            (thread_key, session_id, platform, created_at, now),
        )
        self.db.commit()

    def update_last_active(self, thread_key: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE sessions SET last_active = ? WHERE thread_key = ?",
            (now, thread_key),
        )
        self.db.commit()

    def list_sessions(self, platform: str | None = None, limit: int = 50) -> list[dict]:
        if platform:
            rows = self.db.execute(
                "SELECT thread_key, session_id, platform, created_at, last_active FROM sessions WHERE platform = ? ORDER BY last_active DESC LIMIT ?",
                (platform, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT thread_key, session_id, platform, created_at, last_active FROM sessions ORDER BY last_active DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"thread_key": r[0], "session_id": r[1], "platform": r[2], "created_at": r[3], "last_active": r[4]}
            for r in rows
        ]

    def save_message_mapping(self, message_id: str, platform: str, channel_id: str, thread_key: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO message_map (message_id, platform, channel_id, thread_key) VALUES (?, ?, ?, ?)",
            (message_id, platform, channel_id, thread_key),
        )
        self.db.commit()

    def get_thread_key_for_message(self, message_id: str, platform: str, channel_id: str) -> str | None:
        row = self.db.execute(
            "SELECT thread_key FROM message_map WHERE message_id = ? AND platform = ? AND channel_id = ?",
            (message_id, platform, channel_id),
        ).fetchone()
        return row[0] if row else None

    def delete_session(self, thread_key: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE thread_key = ?", (thread_key,))
        self.db.commit()

    def cleanup_old_sessions(self, days: int = 7) -> int:
        """Retire sessions idle past `days`, and prune the two message tables on the
        same horizon. `message_context` carries `saved_at`; `message_map` does not,
        so a map row is pruned when its context partner is gone — the two are always
        written together (server.py) and a reply to a week-old message cannot resume
        a session anyway (rotation retired it after 24h). Returns sessions deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.db.execute("DELETE FROM sessions WHERE last_active < ?", (cutoff,))
        deleted = cur.rowcount
        self.db.execute("DELETE FROM message_context WHERE saved_at < ?", (cutoff,))
        self.db.execute(
            "DELETE FROM message_map WHERE NOT EXISTS ("
            " SELECT 1 FROM message_context c WHERE c.message_id = message_map.message_id"
            " AND c.platform = message_map.platform AND c.channel_id = message_map.channel_id)"
        )
        self.db.commit()
        return deleted

    def save_message_context(self, message_id: str, platform: str, channel_id: str, content: str) -> None:
        """Store the content of a bot-sent message so replies can include it as context."""
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT OR REPLACE INTO message_context (message_id, platform, channel_id, content, saved_at) VALUES (?, ?, ?, ?, ?)",
            (message_id, platform, channel_id, content, now),
        )
        self.db.commit()

    def get_message_context(self, message_id: str, platform: str, channel_id: str) -> str | None:
        row = self.db.execute(
            "SELECT content FROM message_context WHERE message_id = ? AND platform = ? AND channel_id = ?",
            (message_id, platform, channel_id),
        ).fetchone()
        return row[0] if row else None
