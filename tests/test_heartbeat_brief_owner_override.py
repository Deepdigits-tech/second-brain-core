"""E (2026-09-20): CORTEX_BRIEF_OWNER overrides the platform rule that
decides which machine sends briefs and owns every Telegram push lane — same
idiom as memory_reflect.is_reflection_owner()'s CORTEX_REFLECTION_OWNER and
content_weekly's CORTEX_CONTENT_WEEKLY_OWNER. Lets the Mac take over when
the VPS heartbeat is down, with no code edit.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import heartbeat as hb  # noqa: E402


def test_unset_env_keeps_the_platform_rule(monkeypatch):
    monkeypatch.delenv("CORTEX_BRIEF_OWNER", raising=False)
    monkeypatch.setattr(hb, "BRIEF_OWNER_IS_VPS", True)
    assert hb.is_brief_owner() is True
    monkeypatch.setattr(hb, "BRIEF_OWNER_IS_VPS", False)
    assert hb.is_brief_owner() is False


def test_env_1_forces_ownership_on_regardless_of_platform(monkeypatch):
    monkeypatch.setenv("CORTEX_BRIEF_OWNER", "1")
    monkeypatch.setattr(hb, "BRIEF_OWNER_IS_VPS", False)  # e.g. running on the Mac
    assert hb.is_brief_owner() is True


def test_env_0_forces_ownership_off_regardless_of_platform(monkeypatch):
    monkeypatch.setenv("CORTEX_BRIEF_OWNER", "0")
    monkeypatch.setattr(hb, "BRIEF_OWNER_IS_VPS", True)  # e.g. running on the VPS
    assert hb.is_brief_owner() is False


def test_env_ignores_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("CORTEX_BRIEF_OWNER", " 1 ")
    monkeypatch.setattr(hb, "BRIEF_OWNER_IS_VPS", False)
    assert hb.is_brief_owner() is True


def test_any_other_value_is_treated_as_off(monkeypatch):
    monkeypatch.setenv("CORTEX_BRIEF_OWNER", "yes")
    monkeypatch.setattr(hb, "BRIEF_OWNER_IS_VPS", True)
    assert hb.is_brief_owner() is False


def test_platform_default_still_matches_documented_rule(monkeypatch):
    """BRIEF_OWNER_IS_VPS itself (the module-level constant computed at
    import time from sys.platform) is untouched — is_brief_owner() only adds
    an override layer on top of it."""
    monkeypatch.delenv("CORTEX_BRIEF_OWNER", raising=False)
    import sys as _sys
    assert hb.BRIEF_OWNER_IS_VPS == (_sys.platform != "darwin")


def test_no_bare_brief_owner_is_vps_reads_left_in_code():
    """Every former `if BRIEF_OWNER_IS_VPS` / `if not BRIEF_OWNER_IS_VPS`
    read must have become an `is_brief_owner()` call — a bare read of the
    constant anywhere except its own definition, comments and its own
    docstring would silently ignore CORTEX_BRIEF_OWNER for that one gate."""
    src = (SCRIPTS / "heartbeat.py").read_text()
    lines = src.splitlines()
    in_is_brief_owner_def = False
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("def is_brief_owner("):
            in_is_brief_owner_def = True
            continue
        if in_is_brief_owner_def:
            # Function ends at the next line starting a new top-level statement.
            if line and not line.startswith((" ", "\t")):
                in_is_brief_owner_def = False
            else:
                continue
        if stripped.startswith("#"):
            continue
        if "BRIEF_OWNER_IS_VPS = sys.platform" in line:
            continue
        code_part = line.split("#", 1)[0]
        assert "BRIEF_OWNER_IS_VPS" not in code_part, (
            f"line {i} reads BRIEF_OWNER_IS_VPS directly instead of calling "
            f"is_brief_owner(): {line!r}"
        )
