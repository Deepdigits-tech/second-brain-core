"""memory_flush._write_flush must build the daily-log path/timestamp from PST.

The VPS runs in UTC. A naive `datetime.now()` there writes to tomorrow's (or
yesterday's) `vault/daily/YYYY-MM-DD.md` and stamps a UTC clock time in the
entry header — the owner's daily log is always his own day, same bug class
CLAUDE.md documents for heartbeat.py's own daily-log writes.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_flush  # noqa: E402
from shared import PST  # noqa: E402


class _FrozenDatetime(datetime):
    _frozen: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return cls._frozen.astimezone(tz)
        return cls._frozen


def test_daily_path_uses_pst_date_not_utc_date(tmp_path, monkeypatch):
    # 11:30 PM PST on 2026-09-20 is already 2026-09-21 in UTC.
    pst_instant = datetime(2026, 9, 20, 23, 30, tzinfo=PST)
    utc_instant = pst_instant.astimezone(timezone.utc)
    assert utc_instant.date().isoformat() == "2026-09-21"  # sanity: UTC has rolled to the next day

    _FrozenDatetime._frozen = utc_instant  # naive .now() on a UTC box returns this "naive UTC" reading
    monkeypatch.setattr(memory_flush, "datetime", _FrozenDatetime)
    monkeypatch.setattr(memory_flush, "DAILY_DIR", tmp_path / "daily")
    monkeypatch.setattr(memory_flush, "FLUSH_STATE_PATH", tmp_path / "flush_state.json")
    monkeypatch.setattr(memory_flush, "check_content_dedup", lambda h: False)
    monkeypatch.setattr(memory_flush, "update_dedup", lambda *a, **k: None)

    asyncio.run(memory_flush._write_flush("- did a thing", "session-1"))

    written = list((tmp_path / "daily").glob("*.md"))
    assert len(written) == 1
    # Must be the PST day (20th), not the UTC day (21st).
    assert written[0].name == "2026-09-20.md"
    assert "## 23:30 — Memory Flush" in written[0].read_text()
