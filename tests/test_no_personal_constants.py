"""Rule 1 of docs/FIX-PLAN.md (Cortex): nothing personal in the generic core.

This is the extracted-repo replacement for Cortex's own
tests/test_no_personal_constants.py — the extractor's ship gate excludes
that file on purpose (it holds Cortex's own denylist) and every clone starts
from this empty one instead.

This is a ratchet, not a gate by itself: add your own name, email, host, IP
or account string to DENYLIST below as you personalize this codebase, and
this test (plus `extract_core.gate()`, if you ever fork this repo again)
will start catching it. The test fails if:

  1. a file NOT in KNOWN_OFFENDERS contains a denylisted string (a leak), or
  2. a file IN KNOWN_OFFENDERS no longer contains one (it was cleaned —
     remove it from the set so the ratchet only ever tightens).

Add your own entries, for example:

    DENYLIST: list[re.Pattern[str]] = [re.compile(p) for p in (
        r"you@yourdomain\\.com",
        r"/Users/yourname",
        r"\\bYourFirstName\\b",
    )]
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Empty by default. Add your own strings — see the module docstring.
DENYLIST: list[re.Pattern[str]] = []

# Layers 1-9. Everything here is part of the generic core.
GENERIC_FILES: list[Path] = sorted(
    list((REPO_ROOT / ".claude" / "hooks").glob("*.py"))
    + list((REPO_ROOT / ".claude" / "chat").rglob("*.py"))
    + [REPO_ROOT / ".claude" / "scripts" / name for name in (
        "heartbeat.py", "memory_flush.py", "memory_reflect.py", "memory_index.py",
        "memory_search.py", "memory_links.py", "embeddings.py", "db.py",
        "sanitize.py", "shared.py", "notify.py", "feature_run.py", "brief_coord.py",
        "hygiene.py", "health_check.py", "sdk_cli.py", "sdk_stderr.py",
        "generate_heartbeat_doc.py", "drafts.py", "query.py", "vault_sync.sh",
        "guardrail.py",
    )]
    + [REPO_ROOT / ".claude" / "scripts" / "integrations" / name for name in (
        "gmail.py", "gcal.py", "registry.py", "telegram_bot.py", "slack_bot.py",
    )]
)

KNOWN_OFFENDERS: set[str] = set()


def _hits(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return [p.pattern for p in DENYLIST if p.search(text)]


@pytest.mark.parametrize("path", GENERIC_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_personal_constants_in_generic_core(path: Path) -> None:
    rel = str(path.relative_to(REPO_ROOT))
    hits = _hits(path)
    if rel in KNOWN_OFFENDERS:
        assert hits, f"{rel} is in KNOWN_OFFENDERS but no longer matches — remove it from the set"
    else:
        assert not hits, f"{rel} contains a denylisted string: {hits}"


def test_known_offenders_are_real_files() -> None:
    real = {str(p.relative_to(REPO_ROOT)) for p in GENERIC_FILES}
    stale = KNOWN_OFFENDERS - real
    assert not stale, f"KNOWN_OFFENDERS names files not in GENERIC_FILES: {stale}"
