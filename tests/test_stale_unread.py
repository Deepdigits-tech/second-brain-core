"""Tests for stale-UNREAD backstop logic in heartbeat."""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import heartbeat  # noqa: E402


def test_prune_notified_ids_drops_entries_older_than_7d():
    now = datetime.now(timezone.utc)
    state = {
        "old": (now - timedelta(days=10)).isoformat(),
        "recent": (now - timedelta(days=2)).isoformat(),
        "today": now.isoformat(),
    }

    pruned = heartbeat.prune_notified_ids(state, max_age_days=7)

    assert "old" not in pruned
    assert "recent" in pruned
    assert "today" in pruned


def test_classify_inbox_includes_stale_unread_in_unmatched():
    pytest.importorskip("inbox.classifier", reason="inbox triage lane is Cortex-only and does not ship")
    data = {
        "gmail": {"work-account": [
            {"id": "stale_1", "from": "sender@example.com", "subject": "still here",
             "labels": ["INBOX", "UNREAD"]},
        ]},
        "gcal": [],
    }
    diff = {"new_email_ids": [], "stale_email_ids": ["stale_1"]}

    with patch("inbox.classifier.load_mode", return_value={"work-account": {}}), \
         patch("inbox.classifier._load_rules", return_value={}):
        result = heartbeat.classify_inbox(data, diff)

    assert "work-account" in result["unmatched"]
    assert any(m["id"] == "stale_1" for m in result["unmatched"]["work-account"])
