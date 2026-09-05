"""I2: _behavior_watch (Sunday inbox behavior-watch Telegram push) must be
gated on BRIEF_OWNER_IS_VPS, same as the other Telegram push lanes
(_daily_proposals, email_digest) — without the gate both Mac and VPS would
send the Sunday message.

_behavior_watch is a closure defined inline inside heartbeat.main(), not an
importable top-level function, so this is a structural check on the source
rather than a call-through unit test.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_SRC = (REPO_ROOT / ".claude" / "scripts" / "heartbeat.py").read_text()


def _function_body(name: str) -> str:
    """Grab the source text of a `def <name>(...):` block up to the next
    top-level (4-space-indented) `def` at the same indentation."""
    match = re.search(rf"^( +)def {re.escape(name)}\(", HEARTBEAT_SRC, re.MULTILINE)
    assert match, f"{name} not found in heartbeat.py"
    indent = match.group(1)
    start = match.start()
    # next line at the same indentation starting with "def "
    next_def = re.search(rf"^{indent}def ", HEARTBEAT_SRC[match.end():], re.MULTILINE)
    end = match.end() + next_def.start() if next_def else len(HEARTBEAT_SRC)
    return HEARTBEAT_SRC[start:end]


def test_behavior_watch_gated_on_brief_owner_is_vps():
    body = _function_body("_behavior_watch")
    assert "BRIEF_OWNER_IS_VPS" in body, (
        "_behavior_watch has no BRIEF_OWNER_IS_VPS gate — both Mac and VPS "
        "would send the Sunday behavior-watch Telegram message"
    )
    # The gate must come before the Sunday weekday check / send, not after.
    gate_pos = body.index("BRIEF_OWNER_IS_VPS")
    send_pos = body.index("telegram_bot.send_message_sync")
    assert gate_pos < send_pos


def test_behavior_watch_comment_no_longer_claims_mac_only():
    body = _function_body("_behavior_watch")
    # The stale comment used to say "runs where invest.weekly runs (Mac), no
    # VPS gate needed" — that must be gone now that a VPS gate exists.
    assert "no VPS gate needed" not in body
