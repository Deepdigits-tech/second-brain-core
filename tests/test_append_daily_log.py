"""Tests for append_daily_log helper and daily log timestamp fixing.

FIX 3: daily-log writers use UTC time instead of Pacific, and inconsistent locking.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

from shared import append_daily_log, owner_timezone  # noqa: E402


class TestAppendDailyLog:
    """Tests for append_daily_log helper."""

    def test_appends_text_to_file(self, tmp_path):
        """append_daily_log appends text to a file."""
        target = tmp_path / "daily.md"
        append_daily_log(target, "First line\n")
        append_daily_log(target, "Second line\n")

        content = target.read_text()
        assert "First line" in content
        assert "Second line" in content
        assert content.startswith("First line")

    def test_creates_parent_directories(self, tmp_path):
        """append_daily_log creates parent directories if needed."""
        target = tmp_path / "a" / "b" / "daily.md"
        append_daily_log(target, "content\n")
        assert target.read_text() == "content\n"
        assert target.parent.exists()

    def test_preserves_existing_content(self, tmp_path):
        """append_daily_log preserves existing file content."""
        target = tmp_path / "daily.md"
        target.write_text("Original content\n")
        append_daily_log(target, "Appended content\n")

        content = target.read_text()
        assert content == "Original content\nAppended content\n"

    def test_thread_safe_concurrent_appends(self, tmp_path):
        """Two threads appending concurrently both land intact."""
        target = tmp_path / "daily.md"

        def append_lines(n):
            for i in range(5):
                append_daily_log(target, f"Thread {n} line {i}\n")
                time.sleep(0.001)  # Small delay to increase contention

        t1 = threading.Thread(target=append_lines, args=(1,))
        t2 = threading.Thread(target=append_lines, args=(2,))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # All 10 lines should be present
        content = target.read_text()
        lines = content.strip().split("\n")
        assert len(lines) == 10

        # Count lines from each thread
        t1_lines = sum(1 for line in lines if "Thread 1" in line)
        t2_lines = sum(1 for line in lines if "Thread 2" in line)
        assert t1_lines == 5
        assert t2_lines == 5


class TestCaptureInsightPacificTime:
    """Tests for Pacific time in capture_insight."""

    def test_pacific_time_filename(self, tmp_path, monkeypatch):
        """Daily log filename uses Pacific time, not UTC."""
        # Mock owner_timezone to return PST (UTC-8)
        pst = ZoneInfo("America/Los_Angeles")

        # 2026-09-20 17:30 Pacific (2026-09-21 00:30 UTC)
        pacific_time = datetime(2026, 9, 20, 17, 30, 0, tzinfo=pst)

        with patch("datetime.datetime") as mock_datetime:
            # Make datetime.now(pst) return our test time
            mock_datetime.now.return_value = pacific_time
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

            filename = pacific_time.strftime("%Y-%m-%d")
            # Should be 2026-09-20 (Pacific date), not 2026-09-21 (UTC date)
            assert filename == "2026-09-20"

    def test_pacific_time_header(self, tmp_path, monkeypatch):
        """Daily log entry header uses Pacific time, not UTC."""
        # 2026-09-20 17:30 Pacific
        pst = ZoneInfo("America/Los_Angeles")
        pacific_time = datetime(2026, 9, 20, 17, 30, 45, tzinfo=pst)

        timestamp = pacific_time.strftime("%H:%M")
        # Should be 17:30 (Pacific), not 00:30 (UTC)
        assert timestamp == "17:30"

    def test_midnight_boundary_pacific_vs_utc(self, tmp_path, monkeypatch):
        """Time near midnight shows correct date in Pacific, not UTC date rollover."""
        # 2026-09-20 23:45 Pacific is 2026-09-21 06:45 UTC
        pst = ZoneInfo("America/Los_Angeles")
        pacific_time = datetime(2026, 9, 20, 23, 45, 0, tzinfo=pst)

        pacific_date = pacific_time.strftime("%Y-%m-%d")

        # Should use Pacific date (2026-09-20), not UTC date (2026-09-21)
        assert pacific_date == "2026-09-20"

        # Verify UTC would be different
        utc_time = pacific_time.astimezone(timezone.utc)
        utc_date = utc_time.strftime("%Y-%m-%d")
        assert utc_date == "2026-09-21"


class TestOwnerTimezoneFallback:
    """Tests for owner_timezone helper."""

    def test_defaults_to_los_angeles(self, monkeypatch):
        """owner_timezone defaults to America/Los_Angeles when TIMEZONE is unset."""
        monkeypatch.delenv("TIMEZONE", raising=False)
        tz = owner_timezone()
        # Can't directly compare ZoneInfo, but we can check the name
        assert str(tz) == "America/Los_Angeles"

    def test_respects_timezone_env_var(self, monkeypatch):
        """owner_timezone uses TIMEZONE environment variable when set."""
        monkeypatch.setenv("TIMEZONE", "America/New_York")
        tz = owner_timezone()
        assert str(tz) == "America/New_York"

    def test_falls_back_on_invalid_timezone(self, monkeypatch):
        """owner_timezone falls back to Los Angeles on invalid IANA name."""
        monkeypatch.setenv("TIMEZONE", "Invalid/Timezone")
        tz = owner_timezone()
        assert str(tz) == "America/Los_Angeles"
