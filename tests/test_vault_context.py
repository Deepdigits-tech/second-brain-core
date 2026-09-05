"""One vault-context builder (finding 7.2).

session-start-context.py and chat/context.py implemented the same thing twice,
each with its own MAX_DAILY_LINES. The workshop hook also injects the date, so
the agent always knows what day it is; ours did not.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / ".claude"))

from shared import build_vault_context  # noqa: E402


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "daily").mkdir(parents=True)
    (v / "SOUL.md").write_text("# SOUL\nbe kind\n")
    (v / "USER.md").write_text("# USER\nname: someone\n")
    (v / "MEMORY.md").write_text("# MEMORY\n- fact one\n")
    for d, n in (("2026-09-01", 3), ("2026-08-31", 80), ("2026-08-30", 2), ("2026-08-29", 1)):
        (v / "daily" / f"{d}.md").write_text("\n".join(f"line {i} of {d}" for i in range(n)) + "\n")
    return v


def test_sections_in_order_with_date_first(tmp_path):
    out = build_vault_context(_vault(tmp_path))
    idx = [out.index(s) for s in ("=== TODAY ===", "=== SOUL ===", "=== USER ===", "=== MEMORY ===",
                                    "=== DAILY LOG: 2026-09-01 ===", "=== DAILY LOG: 2026-08-31 ===",
                                    "=== DAILY LOG: 2026-08-30 ===")]
    assert idx == sorted(idx)
    assert "2026-08-29" not in out  # only the three newest


def test_daily_logs_are_truncated_to_the_top(tmp_path):
    out = build_vault_context(_vault(tmp_path), max_daily_lines=50)
    assert "line 49 of 2026-08-31" in out
    assert "line 50 of 2026-08-31" not in out


def test_date_line_can_be_omitted(tmp_path):
    assert "=== TODAY ===" not in build_vault_context(_vault(tmp_path), include_date=False)


def test_bootstrap_loads_first_after_date_when_present(tmp_path):
    v = _vault(tmp_path)
    (v / "BOOTSTRAP.md").write_text("# BOOTSTRAP\nwelcome, let's set you up\n")
    out = build_vault_context(v)
    assert "=== BOOTSTRAP ===" in out
    assert "welcome, let's set you up" in out
    idx = [out.index(s) for s in ("=== TODAY ===", "=== BOOTSTRAP ===", "=== SOUL ===")]
    assert idx == sorted(idx)


def test_bootstrap_absent_when_file_missing(tmp_path):
    out = build_vault_context(_vault(tmp_path))
    assert "=== BOOTSTRAP ===" not in out


def test_missing_core_file_is_skipped_not_fatal(tmp_path):
    v = _vault(tmp_path)
    (v / "USER.md").unlink()
    out = build_vault_context(v)
    assert "=== USER ===" not in out and "=== SOUL ===" in out


def test_session_start_hook_prints_exactly_the_builder_output(tmp_path):
    v = _vault(tmp_path)
    hook = REPO_ROOT / ".claude" / "hooks" / "session-start-context.py"
    r = subprocess.run([sys.executable, str(hook)], input=json.dumps({"cwd": str(tmp_path)}),
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout.strip() == build_vault_context(v).strip()


def test_chat_context_embeds_the_builder_output(tmp_path):
    from chat.context import build_system_prompt
    v = _vault(tmp_path)
    prompt = build_system_prompt(vault_path=v, platform="telegram")
    assert build_vault_context(v) in prompt
    assert "=== TODAY ===" in prompt


def test_no_second_copy_of_the_daily_glob():
    for rel in (".claude/hooks/session-start-context.py", ".claude/chat/context.py"):
        src = (REPO_ROOT / rel).read_text()
        assert "????-??-??.md" not in src, f"{rel} still crawls daily logs itself"
        assert "MAX_DAILY_LINES" not in src, f"{rel} still defines its own line budget"


def test_daily_log_with_invalid_utf8_bytes_is_not_fatal(tmp_path):
    v = _vault(tmp_path)
    (v / "daily" / "2026-09-01.md").write_bytes(b"line \xff\xfe bad\n")
    out = build_vault_context(v)
    assert "=== DAILY LOG: 2026-09-01 ===" in out


def test_unreadable_core_file_is_skipped_not_fatal(tmp_path):
    import os

    v = _vault(tmp_path)
    path = v / "USER.md"
    path.chmod(0)
    try:
        if os.geteuid() == 0:
            import pytest as _pytest
            _pytest.skip("root ignores file permissions; chmod 0 does not block reads")
        out = build_vault_context(v)
        assert "=== USER ===" not in out
        assert "=== SOUL ===" in out
    finally:
        path.chmod(0o644)


def test_core_memories_loaded_after_memory_when_present(tmp_path):
    v = _vault(tmp_path)
    (v / "core-memories.md").write_text("# Core Memories\n- never do X\n")
    out = build_vault_context(v)
    assert out.index("=== MEMORY ===") < out.index("=== CORE MEMORIES ===") < out.index("=== DAILY LOG: 2026-09-01 ===")
    assert "never do X" in out


def test_pages_are_never_loaded(tmp_path):
    v = _vault(tmp_path)
    (v / "entities").mkdir(); (v / "entities" / "acme.md").write_text("SECRET-PAGE-TEXT\n")
    (v / "topics").mkdir(); (v / "topics" / "arch.md").write_text("SECRET-TOPIC-TEXT\n")
    out = build_vault_context(v)
    assert "SECRET-PAGE-TEXT" not in out and "SECRET-TOPIC-TEXT" not in out


def test_context_is_capped_at_budget_on_a_line_boundary(tmp_path):
    v = _vault(tmp_path)
    (v / "daily" / "2026-09-01.md").write_text("\n".join(f"log line {i} " + "x" * 80 for i in range(400)) + "\n")
    out = build_vault_context(v, max_chars=3000)
    assert len(out) <= 3000
    assert not out.endswith("x")  # cut at a line boundary, never mid-line
    assert "=== SOUL ===" in out and "=== MEMORY ===" in out  # core files survive; logs are what gets cut
    assert out.rstrip().endswith("[context truncated at 3000 chars]")


def test_no_truncation_marker_when_under_budget(tmp_path):
    out = build_vault_context(_vault(tmp_path))
    assert "[context truncated" not in out


def test_default_budget_is_twenty_thousand():
    import shared
    assert shared.MAX_CONTEXT_CHARS == 20_000


def test_a_long_daily_log_is_cut_to_max_daily_chars_at_a_line_boundary(tmp_path):
    v = _vault(tmp_path)
    # ~20 KB of short lines, well past MAX_DAILY_LINES=50 and MAX_DAILY_CHARS=3500.
    (v / "daily" / "2026-09-01.md").write_text(
        "\n".join(f"line {i} " + "x" * 60 for i in range(300)) + "\n"
    )
    out = build_vault_context(v)
    start = out.index("=== DAILY LOG: 2026-09-01 ===")
    end = out.index("=== DAILY LOG: 2026-08-31 ===")
    section = out[start:end]
    body = section[len("=== DAILY LOG: 2026-09-01 ===\n"):]
    assert len(body) <= 3_500
    assert not body.endswith("x")  # cut at a line boundary, never mid-line


def test_three_daily_logs_fit_head_intact_under_the_context_budget():
    import shared
    assert shared.MAX_DAILY_CHARS == 3_500


def test_chat_preamble_tells_the_agent_memory_md_is_an_index():
    chat_dir = REPO_ROOT / ".claude" / "chat"
    if str(chat_dir) not in sys.path:
        sys.path.insert(0, str(chat_dir))
    import context as chat_context
    for platform in ("slack", "telegram"):
        preamble = chat_context.CHAT_PREAMBLES[platform]
        assert "MEMORY.md above is an INDEX" in preamble
        assert "vault/entities/<name>.md" in preamble


def test_truncation_hard_cuts_when_no_newline_before_the_limit(tmp_path):
    v = _vault(tmp_path)
    # One giant line with no newlines anywhere near the truncation point —
    # rfind("\n", 0, hard_limit) returns -1, so the cut must fall back to
    # a hard cut at hard_limit instead of slicing off the last character.
    (v / "SOUL.md").write_text("x" * 50_000)
    out = build_vault_context(v, max_chars=1_000)
    assert len(out) <= 1_000
    assert "[context truncated at 1000 chars]" in out
