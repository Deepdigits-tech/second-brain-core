"""Tests for OKF-style provenance and lifecycle fields.

Two behaviours matter most:

1. An agent-written claim and a human-confirmed fact must be distinguishable on
   disk. Before this module they looked identical.
2. `stale_after` must beat file mtime. Auto-sync touches vault files every two
   minutes, so mtime reports rotten files as fresh and stable reference docs as
   rotten.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import provenance  # noqa: E402

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


# --- actor resolution -------------------------------------------------------


def test_no_invoker_means_a_human_is_driving():
    assert provenance.current_actor(env={"OWNER_NAME": "Someone"}) == "human:someone"


def test_no_owner_name_falls_back_to_generic_actor():
    """A fresh clone with no OWNER_NAME set must not bake anyone's identity in."""
    assert provenance.current_actor(env={}) == "human:owner"


@pytest.mark.parametrize(
    "invoked_by,expected",
    [
        ("heartbeat", "process:heartbeat"),
        ("reflection", "process:reflection"),
        ("memory_flush", "process:memory_flush"),
        ("hygiene", "process:hygiene"),
    ],
)
def test_background_workers_become_process_actors(invoked_by, expected):
    assert provenance.current_actor(env={"CLAUDE_INVOKED_BY": invoked_by}) == expected


@pytest.mark.parametrize("already", ["human:someone", "process:reflection", "claude/opus-5"])
def test_already_okf_shaped_actor_passes_through(already):
    assert provenance.current_actor(env={"CLAUDE_INVOKED_BY": already}) == already


# --- generated / verified ---------------------------------------------------


def test_stamp_generated_records_who_and_when():
    meta = provenance.stamp_generated({"type": "kb-topic"}, actor="process:reflection", at=NOW)

    assert meta["generated"] == {"by": "process:reflection", "at": "2026-08-14T12:00:00+00:00"}
    assert meta["type"] == "kb-topic", "existing keys must survive"


def test_unverified_by_default():
    assert provenance.is_verified({"type": "kb-topic"}) is False
    assert provenance.is_verified({"type": "kb-topic", "verified": []}) is False


def test_verification_is_appended_not_replaced():
    meta = provenance.add_verification({"type": "x"}, actor="human:someone", at=NOW)
    later = NOW + timedelta(days=1)
    meta = provenance.add_verification(meta, actor="human:someone", at=later)

    assert len(meta["verified"]) == 2
    assert provenance.is_verified(meta) is True
    assert meta["verified"][0]["at"] == "2026-08-14T12:00:00+00:00"


def test_generating_does_not_wipe_prior_verification():
    meta = provenance.add_verification({"type": "x"}, actor="human:someone", at=NOW)
    meta = provenance.stamp_generated(meta, actor="process:reflection", at=NOW)

    assert provenance.is_verified(meta) is True


# --- staleness: the mtime fix ----------------------------------------------


def test_explicit_expiry_beats_a_recently_touched_file():
    """Auto-sync touches files constantly. Fresh mtime must not hide real rot."""
    meta = {"type": "kb-topic", "stale_after": "2026-01-01"}

    reason = provenance.stale_reason(meta, mtime=NOW, now=NOW, max_age_days=90)

    assert reason and "stale_after" in reason


def test_explicit_expiry_protects_an_old_but_valid_file():
    """A stable reference untouched for a year is not rot."""
    meta = {"type": "kb-topic", "stale_after": "2027-01-01"}
    ancient = NOW - timedelta(days=400)

    assert provenance.stale_reason(meta, mtime=ancient, now=NOW, max_age_days=90) is None


def test_falls_back_to_mtime_when_no_expiry_declared():
    ancient = NOW - timedelta(days=400)

    reason = provenance.stale_reason({"type": "x"}, mtime=ancient, now=NOW, max_age_days=90)

    assert reason and "400 days" in reason


def test_recent_file_without_expiry_is_fine():
    recent = NOW - timedelta(days=3)
    assert provenance.stale_reason({"type": "x"}, mtime=recent, now=NOW, max_age_days=90) is None


def test_deprecated_status_is_always_stale():
    meta = {"type": "x", "status": "deprecated", "stale_after": "2027-01-01"}
    assert provenance.stale_reason(meta, mtime=NOW, now=NOW, max_age_days=90)


def test_malformed_expiry_falls_back_instead_of_crashing():
    ancient = NOW - timedelta(days=400)
    meta = {"type": "x", "stale_after": "not-a-date"}

    reason = provenance.stale_reason(meta, mtime=ancient, now=NOW, max_age_days=90)

    assert reason and "400 days" in reason


# --- round-tripping ---------------------------------------------------------


DOC = """---
type: kb-topic
topic: pricing
tags: [aios, pricing]
---

# Pricing

Body text stays put.
"""


def test_parse_and_render_preserve_body_and_unknown_keys():
    meta, body = provenance.parse(DOC)
    assert meta["topic"] == "pricing"
    assert meta["tags"] == ["aios", "pricing"]

    out = provenance.render(meta, body)
    meta2, body2 = provenance.parse(out)

    assert meta2 == meta
    assert body2.strip() == body.strip()
    assert "Body text stays put." in out


def test_render_keeps_type_first_for_okf_readability():
    out = provenance.render({"topic": "p", "type": "kb-topic"}, "x")
    first_key_line = [l for l in out.splitlines() if l and not l.startswith("---")][0]
    assert first_key_line.startswith("type:")


def test_stamp_file_is_idempotent(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(DOC)

    provenance.stamp_file(p, actor="process:reflection", at=NOW)
    once = p.read_text()
    provenance.stamp_file(p, actor="process:reflection", at=NOW)

    assert p.read_text() == once, "re-stamping identical data must not churn the file"
    assert "generated:" in once


def test_file_without_frontmatter_gets_one(tmp_path):
    p = tmp_path / "bare.md"
    p.write_text("# Just a heading\n")

    provenance.stamp_file(p, actor="human:someone", at=NOW, default_type="Note")

    meta, body = provenance.parse(p.read_text())
    assert meta["type"] == "Note"
    assert "Just a heading" in body


# --- hygiene integration ----------------------------------------------------
#
# hygiene.py had no staleness check over vault knowledge at all — check_stale_data_files
# only scans .claude/data/. This is net-new coverage, not a replacement.

import hygiene  # noqa: E402


def _kb_doc(tmp_path, name, front, mtime_days_ago=0):
    p = tmp_path / name
    p.write_text(f"---\n{front}\n---\n\n# Doc\n")
    if mtime_days_ago:
        import os
        ts = (datetime.now(timezone.utc) - timedelta(days=mtime_days_ago)).timestamp()
        os.utime(p, (ts, ts))
    return p


def test_hygiene_flags_doc_past_declared_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(hygiene, "_knowledge_dirs", lambda: [tmp_path])
    _kb_doc(tmp_path, "rotten.md", "type: kb-topic\nstale_after: 2020-01-01")

    findings = []
    hygiene.check_stale_knowledge_docs(findings)

    assert len(findings) == 1
    assert findings[0].check == "stale_knowledge_docs"
    assert "stale_after" in findings[0].issue


def test_hygiene_does_not_flag_old_file_with_future_expiry(tmp_path, monkeypatch):
    """The mtime fix: a stable doc untouched for a year is not rot."""
    monkeypatch.setattr(hygiene, "_knowledge_dirs", lambda: [tmp_path])
    _kb_doc(tmp_path, "stable.md", "type: kb-topic\nstale_after: 2099-01-01", mtime_days_ago=400)

    findings = []
    hygiene.check_stale_knowledge_docs(findings)

    assert findings == []


def test_hygiene_falls_back_to_mtime_without_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(hygiene, "_knowledge_dirs", lambda: [tmp_path])
    _kb_doc(tmp_path, "old.md", "type: kb-topic", mtime_days_ago=400)

    findings = []
    hygiene.check_stale_knowledge_docs(findings)

    assert len(findings) == 1
    assert "not modified" in findings[0].issue


def test_hygiene_declared_expiry_outranks_mtime_in_severity(tmp_path, monkeypatch):
    """A broken promise is worse than an unannotated old file."""
    monkeypatch.setattr(hygiene, "_knowledge_dirs", lambda: [tmp_path])
    _kb_doc(tmp_path, "promised.md", "type: kb-topic\nstale_after: 2020-01-01")
    _kb_doc(tmp_path, "just-old.md", "type: kb-topic", mtime_days_ago=400)

    findings = []
    hygiene.check_stale_knowledge_docs(findings)

    by_name = {f.path.split("/")[-1]: f for f in findings}
    assert by_name["promised.md"].severity == "medium"
    assert by_name["just-old.md"].severity == "low"


def test_hygiene_check_is_registered():
    assert hygiene.check_stale_knowledge_docs in hygiene.run_all_checks.__globals__["_CHECKS"]


# --- CLI --------------------------------------------------------------------


def test_cli_stamps_a_file(tmp_path, capsys):
    p = tmp_path / "doc.md"
    p.write_text(DOC)

    rc = provenance._main(["stamp", str(p), "--actor", "human:someone"])

    assert rc == 0
    meta, _ = provenance.parse(p.read_text())
    assert meta["generated"]["by"] == "human:someone"


def test_cli_check_reports_unstamped(tmp_path, capsys):
    (tmp_path / "a.md").write_text(DOC)
    (tmp_path / "index.md").write_text("# index\n")

    provenance._main(["check", str(tmp_path)])
    out = capsys.readouterr().out

    assert "1 document(s) with no recorded origin" in out, "index.md is reserved, must be skipped"


def test_missing_provenance_ignores_stamped_files(tmp_path):
    p = tmp_path / "a.md"
    p.write_text(DOC)
    provenance.stamp_file(p, actor="human:someone", at=NOW)

    assert provenance.missing_provenance(tmp_path) == []


# --- root allowlist ---------------------------------------------------------
#
# PHASES.md is named in CLAUDE.md as the canonical append-only build history, and
# skills-lock.json is a lockfile. Both belong at the root. A finding that repeats
# every week and is always wrong trains you to ignore the whole report.


@pytest.mark.parametrize("name", ["PHASES.md", "skills-lock.json"])
def test_canonical_root_files_are_not_flagged(name):
    findings = []
    hygiene.check_files_at_root(findings)

    assert name not in {f.path for f in findings}


def test_genuinely_stray_root_file_is_still_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(hygiene, "PROJECT_ROOT", tmp_path)
    (tmp_path / "random.png").write_bytes(b"x")
    (tmp_path / "PHASES.md").write_text("# hist")

    findings = []
    hygiene.check_files_at_root(findings)

    assert {f.path for f in findings} == {"random.png"}
