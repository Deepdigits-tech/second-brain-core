"""Tests for atomic_write_text.

Ported from the private Cortex repo's batch-2 reliability fixes. The source
also carries TestTaskStoreAtomicity / TestNeedsYouStoreAtomicity, which
exercise a tasks.py / needs_you.py that do not exist in this tree — this
target has no task queue or needs-you tray, so those cases are dropped.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

from shared import atomic_write_text  # noqa: E402


class TestAtomicWriteText:
    """Tests for atomic_write_text function."""

    def test_writes_file_successfully(self, tmp_path):
        """atomic_write_text creates and writes a file."""
        target = tmp_path / "test.txt"
        atomic_write_text(target, "hello world")
        assert target.read_text() == "hello world"

    def test_preserves_old_file_on_write_failure(self, tmp_path):
        """When write fails, old file remains unchanged."""
        target = tmp_path / "test.txt"
        target.write_text("original content")

        def failing_replace(src, dst):
            raise OSError("Simulated failure")

        with patch("os.replace", failing_replace):
            with pytest.raises(OSError):
                atomic_write_text(target, "new content")

        # Original file should be unchanged
        assert target.read_text() == "original content"

    def test_no_stray_temp_files_on_failure(self, tmp_path):
        """When write fails, temp file is cleaned up."""
        target = tmp_path / "test.txt"
        target.write_text("original")

        def failing_replace(src, dst):
            raise OSError("Simulated failure")

        with patch("os.replace", failing_replace):
            with pytest.raises(OSError):
                atomic_write_text(target, "new content")

        # No temp files should remain
        temp_files = list(tmp_path.glob("test.txt.*"))
        assert len(temp_files) == 0

    def test_creates_parent_directories(self, tmp_path):
        """atomic_write_text creates parent directories if needed."""
        target = tmp_path / "a" / "b" / "c" / "test.txt"
        atomic_write_text(target, "nested content")
        assert target.read_text() == "nested content"
        assert target.parent.exists()

    def test_format_is_preserved(self, tmp_path):
        """File format with newlines and indentation is preserved."""
        target = tmp_path / "test.json"
        content = json.dumps({"key": "value"}, indent=2)
        atomic_write_text(target, content)
        written = target.read_text()
        assert written == content
        assert json.loads(written) == {"key": "value"}
