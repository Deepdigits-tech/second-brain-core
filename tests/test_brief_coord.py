"""Tests for cross-machine brief coordination."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import brief_coord


@pytest.fixture(autouse=True)
def tmp_coord(tmp_path, monkeypatch):
    coord = tmp_path / "brief-coordination.json"
    monkeypatch.setattr(brief_coord, "COORD_PATH", coord)


def test_claim_brief_returns_true_first_time():
    assert brief_coord.claim_brief("morning") is True


def test_claim_brief_returns_false_second_time():
    brief_coord.claim_brief("morning")
    assert brief_coord.claim_brief("morning") is False


def test_claim_brief_morning_and_afternoon_independent():
    assert brief_coord.claim_brief("morning") is True
    assert brief_coord.claim_brief("afternoon") is True
    assert brief_coord.claim_brief("morning") is False
    assert brief_coord.claim_brief("afternoon") is False


def test_was_brief_claimed_reflects_claim():
    assert brief_coord.was_brief_claimed("morning") is False
    brief_coord.claim_brief("morning")
    assert brief_coord.was_brief_claimed("morning") is True


def test_claim_notification_returns_true_first_call():
    assert brief_coord.claim_notification("morning_brief:2026-04-18") is True


def test_claim_notification_returns_false_on_duplicate():
    brief_coord.claim_notification("morning_brief:2026-04-18")
    assert brief_coord.claim_notification("morning_brief:2026-04-18") is False


def test_claim_notification_different_keys_independent():
    assert brief_coord.claim_notification("morning_brief:2026-04-18") is True
    assert brief_coord.claim_notification("content:ab12cd34") is True


def test_claim_notification_expires_after_ttl(monkeypatch):
    """With ttl_minutes=0.001 (~0.06s), the slot should expire quickly."""
    brief_coord.claim_notification("key:x", ttl_minutes=0.001)
    time.sleep(0.1)
    assert brief_coord.claim_notification("key:x", ttl_minutes=0.001) is True
