#!/usr/bin/env python3
"""Weekly hygiene scanner for Cortex OS.

Runs every Sunday at 8 AM PST via launchd.
Scans for known noise patterns and writes a ranked report.
NEVER auto-deletes — proposes for human review.

Usage:
    python3 .claude/scripts/hygiene.py              # generate report
    python3 .claude/scripts/hygiene.py --dry-run    # print findings, no file written
    python3 .claude/scripts/hygiene.py --no-notify  # skip Telegram ping
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shared import PST  # noqa: F401 (module-level constant, see shared.owner_timezone)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
DATA_DIR = PROJECT_ROOT / ".claude" / "data"
RESEARCH_DIR = VAULT_DIR / "research"
MEMORY_MD = VAULT_DIR / "MEMORY.md"
ERROR_LOG = DATA_DIR / "hygiene_errors.log"
DEBUG_LOG = "/tmp/cortex_hook_debug.log"

# Thresholds
LOG_SIZE_WARN_MB = 5
DRAFT_ACTIVE_MAX_HOURS = 24
DRAFT_EXPIRED_MAX_DAYS = 60
STALE_DATA_MAX_DAYS = 30
# Knowledge docs (KB + auto-memory). Only used when a doc declares no stale_after —
# see provenance.stale_reason. Deliberately generous: an undated reference doc going
# six months untouched is a nudge, not a defect.
KNOWLEDGE_MAX_DAYS = 180
def _knowledge_dirs() -> list[Path]:
    """KB plus Claude Code's per-project auto-memory folder — computed at call
    time via memory_index.auto_memory_dir(), never a hardcoded project slug."""
    import memory_index

    return [VAULT_DIR / "kb", memory_index.auto_memory_dir()]
# Post-restructure (2026-09), MEMORY.md is a small index pointing at pages under
# vault/entities/, vault/topics/, vault/decisions/ (and vault/opportunities/<slug>/index.md).
# The old per-bullet char cap and total-line cap failed every week once Current State bullets
# carried a date stamp plus real facts — a check that fails every week is a check nobody reads.
# These replace them: the index itself should stay small, its links should resolve, every
# page should be reachable from it, and Active Items should get archived once they go stale.
MEMORY_INDEX_MAX_BYTES = 5_120
PAGE_WARN_BYTES = 8_192
ACTIVE_ITEM_MAX_AGE_DAYS = 30
MEMORY_INDEX_DIRS = ("entities", "topics", "decisions")
MEMORY_INDEX_BACKUP_EXEMPT = "vault/decisions/pre-migration-backup.md"

# Directories that are empty on purpose: a .gitkeep there is the folder's
# existence, not drift. Anything else that holds only a .gitkeep is still a
# finding, so the headline number means "unexplained empties", not "all empties".
DELIBERATE_GITKEEP_DIRS = {
    ".claude/data", ".claude/data/state", ".claude/data/liveness",
    "vault/daily", "vault/drafts/active", "vault/drafts/expired", "vault/drafts/sent",
    "vault/meetings", "vault/opportunities", "vault/projects", "vault/research",
    "vault/investments", "vault/intros", "vault/insights", "vault/insights/newsletters",
    "vault/content/voice/samples", "vault/kb/aios/sources/raw",
}
# Trees hygiene must not walk: other sessions' worktrees, product-vault
# skeletons nested under a project, and spike test runs.
IGNORED_TREE_MARKERS = (".claude/worktrees/", "/.testrun/")


def _is_nested_vault(rel: str) -> bool:
    parts = rel.split("/")
    return "vault" in parts[1:]


# ── Data types ─────────────────────────────────────────────────────────


@dataclass
class Finding:
    check: str
    path: str
    issue: str
    severity: str  # "high" | "medium" | "low"
    suggested_action: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.path} — {self.issue}"


# ── Individual checks ──────────────────────────────────────────────────


def check_files_at_root(findings: list[Finding]):
    """Non-config files at repo root."""
    known_root_files = {
        ".env.example",
        ".gitignore",
        ".gitattributes",
        "CLAUDE.md",
        "README.md",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Makefile",
        # CLAUDE.md names PHASES.md as the canonical append-only build history,
        # so "move it to vault/sources/ or delete" was always the wrong advice.
        "PHASES.md",
        "skills-lock.json",
    }
    for path in PROJECT_ROOT.iterdir():
        if path.is_dir():
            continue
        if path.name.startswith("."):
            continue
        if path.name in known_root_files:
            continue
        size_kb = path.stat().st_size // 1024
        findings.append(Finding(
            check="files_at_repo_root",
            path=str(path.relative_to(PROJECT_ROOT)),
            issue=f"Unexpected file at repo root ({size_kb} KB)",
            severity="medium",
            suggested_action="Move to vault/sources/ or delete",
        ))


def check_auto_rendered_tracked_in_git(findings: list[Finding]):
    """Auto-rendered files that are tracked in git (should be gitignored)."""
    auto_rendered = [
        "vault/inbox-review.md",
        "vault/tasks/queue.md",
        "vault/tasks/archive.md",
        "vault/HEARTBEAT.md",
    ]
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch"] + auto_rendered,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        tracked = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ]
    except Exception:
        tracked = []

    for tracked_path in tracked:
        findings.append(Finding(
            check="auto_rendered_files_tracked_in_git",
            path=tracked_path,
            issue="Auto-rendered file still tracked in git",
            severity="high",
            suggested_action=f"Run: git rm --cached {tracked_path}  and add to .gitignore",
        ))


def check_stale_data_files(findings: list[Finding]):
    """Data files in .claude/data/ older than STALE_DATA_MAX_DAYS."""
    cutoff = datetime.now() - timedelta(days=STALE_DATA_MAX_DAYS)
    skip_suffixes = {".db", ".db-shm", ".db-wal", ".db-journal", ".lock", ".pid"}
    skip_names = {"flush_state.json", "feature-toggles.json", "inbox-mode.json",
                  "tasks.json", "brief-coordination.json"}

    for path in DATA_DIR.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in skip_suffixes:
            continue
        if path.name in skip_names:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if mtime < cutoff:
            age_days = (datetime.now() - mtime).days
            size_kb = path.stat().st_size // 1024
            findings.append(Finding(
                check="stale_data_files",
                path=str(path.relative_to(PROJECT_ROOT)),
                issue=f"Not modified in {age_days} days ({size_kb} KB)",
                severity="low",
                suggested_action="Review and delete if no longer needed",
            ))


def check_stale_knowledge_docs(findings: list[Finding]):
    """KB and auto-memory docs past their declared `stale_after`.

    Distinct from check_stale_data_files, which only scans .claude/data/ — vault
    knowledge had no staleness check at all before this.

    A declared expiry wins over file mtime in both directions. mtime is close to
    meaningless in this repo: auto-sync rewrites files every two minutes, so rot
    looks fresh, while a stable reference doc untouched for a year looks rotten.
    mtime stays as the fallback for docs that never declare an expiry.
    """
    import provenance

    now = datetime.now(timezone.utc)
    for root in _knowledge_dirs():
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            if path.name in {"index.md", "log.md"}:  # OKF reserved, no frontmatter expected
                continue
            try:
                meta, _ = provenance.parse(path.read_text())
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except (OSError, UnicodeDecodeError):
                continue

            reason = provenance.stale_reason(
                meta, mtime=mtime, now=now, max_age_days=KNOWLEDGE_MAX_DAYS
            )
            if not reason:
                continue

            # A broken explicit promise outranks a doc that simply never declared one.
            declared = provenance.has_declared_lifecycle(meta)
            try:
                rel = str(path.relative_to(PROJECT_ROOT))
            except ValueError:
                rel = str(path)
            findings.append(Finding(
                check="stale_knowledge_docs",
                path=rel,
                issue=reason,
                severity="medium" if declared else "low",
                suggested_action=(
                    "Review and refresh, then bump stale_after"
                    if declared
                    else "Confirm still accurate and add a stale_after date"
                ),
            ))


def check_log_files_over_limit(findings: list[Finding]):
    """Log files exceeding LOG_SIZE_WARN_MB."""
    limit_bytes = LOG_SIZE_WARN_MB * 1024 * 1024
    for path in DATA_DIR.rglob("*.log"):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > limit_bytes:
            size_mb = size / (1024 * 1024)
            findings.append(Finding(
                check="log_files_over_limit",
                path=str(path.relative_to(PROJECT_ROOT)),
                issue=f"{size_mb:.1f} MB — exceeds {LOG_SIZE_WARN_MB} MB limit",
                severity="high",
                suggested_action=f"Truncate: > {path} (backs up existing content first)",
            ))


def check_drafts_expired_too_old(findings: list[Finding]):
    """Draft files in drafts/expired/ older than DRAFT_EXPIRED_MAX_DAYS."""
    expired_dir = VAULT_DIR / "drafts" / "expired"
    if not expired_dir.exists():
        return
    cutoff = datetime.now() - timedelta(days=DRAFT_EXPIRED_MAX_DAYS)
    for path in expired_dir.glob("*.md"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if mtime < cutoff:
            age_days = (datetime.now() - mtime).days
            findings.append(Finding(
                check="drafts_expired_too_old",
                path=str(path.relative_to(PROJECT_ROOT)),
                issue=f"Expired draft is {age_days} days old (limit {DRAFT_EXPIRED_MAX_DAYS}d)",
                severity="low",
                suggested_action="Archive or delete — context is too old to matter",
            ))


def check_empty_dirs_with_gitkeep(findings: list[Finding]):
    """Directories that contain only .gitkeep (categorical drift)."""
    for path in PROJECT_ROOT.rglob(".gitkeep"):
        parent = path.parent
        contents = list(parent.iterdir())
        if len(contents) == 1:
            rel = str(parent.relative_to(PROJECT_ROOT))
            # Skip deliberate empty directories
            if rel in DELIBERATE_GITKEEP_DIRS:
                continue
            # Skip trees hygiene must not walk
            if any(marker in rel + "/" for marker in IGNORED_TREE_MARKERS):
                continue
            # Skip nested vaults
            if _is_nested_vault(rel):
                continue
            findings.append(Finding(
                check="empty_directories_with_gitkeep",
                path=rel,
                issue="Directory contains only .gitkeep — category may be unused",
                severity="low",
                suggested_action="Delete directory if category was never populated",
            ))


def check_broken_references_in_memory_md(findings: list[Finding]):
    """MEMORY.md mentions file paths that don't exist."""
    if not MEMORY_MD.exists():
        return
    content = MEMORY_MD.read_text()
    path_pattern = re.compile(r"`([^`]+\.(md|py|json|yaml|yml|txt|sh))`")
    for match in path_pattern.finditer(content):
        raw = match.group(1)
        if "<" in raw or ">" in raw:
            # Placeholder, e.g. `vault/entities/<name>.md` — not a real path.
            continue
        # Resolve relative to vault/ or project root
        candidates = [
            PROJECT_ROOT / raw,
            VAULT_DIR / raw,
            PROJECT_ROOT / raw.lstrip("/"),
        ]
        if raw.startswith("~/"):
            home = Path.home()
            candidates.append(home / raw[2:])
        if not any(c.exists() for c in candidates):
            findings.append(Finding(
                check="broken_references_in_memory_md",
                path="vault/MEMORY.md",
                issue=f"References `{raw}` which doesn't exist",
                severity="medium",
                suggested_action="Update or remove the stale file reference",
            ))


_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


def _resolve_wikilink(target: str) -> Path | None:
    """Resolve a [[name]] link to vault/<name>.md or vault/<name>/index.md."""
    as_md = VAULT_DIR / f"{target}.md"
    if as_md.exists():
        return as_md
    as_index = VAULT_DIR / target / "index.md"
    if as_index.exists():
        return as_index
    return None


def check_memory_index_size(findings: list[Finding]):
    """MEMORY.md (now an index) should stay small — real content lives in pages."""
    if not MEMORY_MD.exists():
        return
    size = MEMORY_MD.stat().st_size
    if size > MEMORY_INDEX_MAX_BYTES:
        findings.append(Finding(
            check="memory_index_size",
            path="vault/MEMORY.md",
            issue=f"Index is {size} bytes (limit {MEMORY_INDEX_MAX_BYTES}) — content belongs on a page",
            severity="medium",
            suggested_action="Move detail out to an entity/topic/decision page; keep the index a pointer",
        ))


def check_memory_index_links(findings: list[Finding]):
    """Every [[x]] in MEMORY.md must resolve to vault/x.md or vault/x/index.md.

    Links inside fenced code blocks (``` ... ```) are skipped — they're
    examples of the syntax, not real references the index depends on.
    """
    if not MEMORY_MD.exists():
        return
    content = MEMORY_MD.read_text()
    in_fence = False
    for i, line in enumerate(content.splitlines(), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in _WIKILINK_RE.finditer(line):
            target = match.group(1).strip()
            if _resolve_wikilink(target) is None:
                findings.append(Finding(
                    check="memory_index_links",
                    path=f"vault/MEMORY.md:{i}",
                    issue=f"[[{target}]] doesn't resolve to vault/{target}.md or vault/{target}/index.md",
                    severity="medium",
                    suggested_action="Fix the link target or remove the dead reference",
                ))


def check_pages_missing_from_index(findings: list[Finding]):
    """Every entities/topics/decisions page (except the migration backup) is linked from the index."""
    linked_targets: set[str] = set()
    if MEMORY_MD.exists():
        content = MEMORY_MD.read_text()
        for match in _WIKILINK_RE.finditer(content):
            linked_targets.add(match.group(1).strip())

    for sub in MEMORY_INDEX_DIRS:
        d = VAULT_DIR / sub
        if not d.exists():
            continue
        for path in sorted(d.glob("*.md")):
            rel = f"vault/{sub}/{path.name}"
            if rel == MEMORY_INDEX_BACKUP_EXEMPT:
                continue
            target = f"{sub}/{path.stem}"
            if target not in linked_targets:
                findings.append(Finding(
                    check="pages_missing_from_index",
                    path=rel,
                    issue=f"Not linked from vault/MEMORY.md as [[{target}]]",
                    severity="low",
                    suggested_action="Add a link from the index, or remove the orphaned page",
                ))


_ACTIVE_ITEM_DATE_RE = re.compile(
    r"^-\s*\((\d{4}-\d{2}-\d{2})\)"  # (YYYY-MM-DD)
    r"|^-\s*\(([A-Za-z]{3}) (\d{1,2})\)"  # (Mon D)
)


def _parse_active_item_date(line: str, now: datetime | None = None) -> datetime | None:
    if now is None:
        now = datetime.now()
    m = _ACTIVE_ITEM_DATE_RE.match(line.strip())
    if not m:
        return None
    if m.group(1):
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            return None
    month_abbr, day = m.group(2), m.group(3)
    try:
        candidate = datetime.strptime(f"{month_abbr} {day} {now.year}", "%b %d %Y")
    except ValueError:
        return None
    # A short date more than 30 days in the future almost certainly belongs to
    # last year (e.g. reading "Dec 20" in January).
    if candidate > now + timedelta(days=30):
        candidate = candidate.replace(year=now.year - 1)
    return candidate


def check_active_items_stale(findings: list[Finding]):
    """Active Items lines dated more than ACTIVE_ITEM_MAX_AGE_DAYS ago should be archived."""
    if not MEMORY_MD.exists():
        return
    content = MEMORY_MD.read_text()
    lines = content.splitlines()
    in_section = False
    cutoff = datetime.now() - timedelta(days=ACTIVE_ITEM_MAX_AGE_DAYS)
    for i, line in enumerate(lines, 1):
        if line.strip().startswith("## "):
            in_section = line.strip() == "## Active Items"
            continue
        if not in_section:
            continue
        date = _parse_active_item_date(line)
        if date is not None and date < cutoff:
            age_days = (datetime.now() - date).days
            findings.append(Finding(
                check="active_items_stale",
                path=f"vault/MEMORY.md:{i}",
                issue=f"Active item dated {age_days} days ago (limit {ACTIVE_ITEM_MAX_AGE_DAYS})",
                severity="medium",
                suggested_action="Resolve or archive the item; remove it from Active Items",
            ))


def check_page_sizes(findings: list[Finding]):
    """Entity/topic/decision pages over PAGE_WARN_BYTES may want splitting.

    The pre-migration backup is exempt on purpose: it's a verbatim archive
    of the old flat MEMORY.md, not a page anyone is expected to trim.
    """
    for sub in MEMORY_INDEX_DIRS:
        d = VAULT_DIR / sub
        if not d.exists():
            continue
        for path in sorted(d.glob("*.md")):
            rel = f"vault/{sub}/{path.name}"
            if rel == MEMORY_INDEX_BACKUP_EXEMPT:
                continue
            size = path.stat().st_size
            if size > PAGE_WARN_BYTES:
                findings.append(Finding(
                    check="page_sizes",
                    path=f"vault/{sub}/{path.name}",
                    issue=f"Page is {size} bytes (warn at {PAGE_WARN_BYTES})",
                    severity="low",
                    suggested_action="Consider splitting into smaller pages",
                ))


def check_duplicate_drafts(findings: list[Finding]):
    """Multiple active drafts with the same recipient within 7 days."""
    active_dir = VAULT_DIR / "drafts" / "active"
    if not active_dir.exists():
        return
    cutoff = datetime.now() - timedelta(days=7)
    recipient_files: dict[str, list[Path]] = {}
    for path in active_dir.glob("*.md"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        # Extract recipient slug from filename: YYYY-MM-DD_email_<recipient-slug>.md
        parts = path.stem.split("_")
        if len(parts) >= 3:
            recipient_slug = "_".join(parts[2:])
            recipient_files.setdefault(recipient_slug, []).append(path)

    for slug, paths in recipient_files.items():
        if len(paths) > 1:
            findings.append(Finding(
                check="duplicate_drafts",
                path=", ".join(str(p.relative_to(PROJECT_ROOT)) for p in paths),
                issue=f"Multiple active drafts for '{slug}' ({len(paths)} files)",
                severity="medium",
                suggested_action="Consolidate or delete stale draft copies",
            ))


def check_ds_store_files(findings: list[Finding]):
    """macOS .DS_Store files that shouldn't be present."""
    # Only check tracked files; untracked ones are just noise in the FS
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--cached", "-z"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        all_files = result.stdout.split("\0")
    except Exception:
        all_files = []

    for f in all_files:
        if f.endswith(".DS_Store"):
            if any(marker in f + "/" for marker in IGNORED_TREE_MARKERS):
                continue
            findings.append(Finding(
                check="ds_store_files",
                path=f,
                issue="macOS .DS_Store tracked in git or present on disk",
                severity="medium",
                suggested_action="Delete and ensure .DS_Store is in .gitignore",
            ))

    # Also check for untracked .DS_Store files on disk
    for path in PROJECT_ROOT.rglob(".DS_Store"):
        rel = str(path.relative_to(PROJECT_ROOT))
        if any(marker in rel + "/" for marker in IGNORED_TREE_MARKERS):
            continue
        if rel not in all_files:  # not already reported
            findings.append(Finding(
                check="ds_store_files",
                path=rel,
                issue="macOS .DS_Store on disk (untracked but present)",
                severity="low",
                suggested_action="Delete: rm " + str(path),
            ))


def check_pycache_tracked(findings: list[Finding]):
    """__pycache__ or .pytest_cache entries that snuck into git."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        tracked = result.stdout.splitlines()
    except Exception:
        tracked = []

    for f in tracked:
        if "__pycache__" in f or ".pytest_cache" in f or f.endswith(".pyc"):
            findings.append(Finding(
                check="pycache_tracked",
                path=f,
                issue="Build artifact tracked in git",
                severity="high",
                suggested_action=f"git rm --cached {f}  and add to .gitignore",
            ))


# ── Report rendering ───────────────────────────────────────────────────


def render_report(findings: list[Finding], date_str: str) -> str:
    high = [f for f in findings if f.severity == "high"]
    medium = [f for f in findings if f.severity == "medium"]
    low = [f for f in findings if f.severity == "low"]

    lines = [
        f"# Hygiene Report — {date_str}",
        "",
        f"Generated: {datetime.now(PST).strftime('%Y-%m-%d %H:%M PST')}  ",
        f"Total findings: {len(findings)} ({len(high)} high / {len(medium)} medium / {len(low)} low)",
        "",
    ]

    def render_section(label: str, items: list[Finding]):
        lines.append(f"## {label} ({len(items)})")
        lines.append("")
        if not items:
            lines.append("_None_")
            lines.append("")
            return
        for f in items:
            lines.append(f"- **[{f.check}]** `{f.path}`")
            lines.append(f"  - Issue: {f.issue}")
            lines.append(f"  - Action: {f.suggested_action}")
            lines.append("")

    render_section("High severity", high)
    render_section("Medium severity", medium)
    render_section("Low severity", low)

    if not findings:
        lines.append("_No findings. Repo is clean._")
        lines.append("")

    lines.append("---")
    lines.append("_This report is read-only — no files were deleted or modified._")
    lines.append("_Review each finding and take action manually, or ask the chat agent: \"apply hygiene findings\"._")

    return "\n".join(lines)


# ── Notification ───────────────────────────────────────────────────────


def send_telegram(report_path: Path, findings: list[Finding]):
    high_count = sum(1 for f in findings if f.severity == "high")
    med_count = sum(1 for f in findings if f.severity == "medium")
    low_count = sum(1 for f in findings if f.severity == "low")
    total = len(findings)

    if total == 0:
        msg = "Weekly hygiene: repo is clean. No findings."
    else:
        msg = (
            f"Weekly hygiene report ready: {report_path.name}\n"
            f"{total} findings: {high_count} high / {med_count} medium / {low_count} low"
        )

    try:
        subprocess.run(
            [
                str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
                str(PROJECT_ROOT / ".claude" / "scripts" / "query.py"),
                "telegram",
                "send",
                "--text",
                msg,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=30,
        )
    except Exception as e:
        _log_error(f"Telegram notify failed: {e}")


# ── Logging helpers ────────────────────────────────────────────────────


def _debug(msg: str):
    with open(DEBUG_LOG, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] hygiene: {msg}\n")


def _log_error(msg: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ERROR_LOG, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


# ── Main ───────────────────────────────────────────────────────────────


_CHECKS = [
    check_files_at_root,
    check_auto_rendered_tracked_in_git,
    check_stale_data_files,
    check_stale_knowledge_docs,
    check_log_files_over_limit,
    check_drafts_expired_too_old,
    check_empty_dirs_with_gitkeep,
    check_broken_references_in_memory_md,
    check_memory_index_size,
    check_memory_index_links,
    check_pages_missing_from_index,
    check_active_items_stale,
    check_page_sizes,
    check_duplicate_drafts,
    check_ds_store_files,
    check_pycache_tracked,
]


def run_all_checks() -> list[Finding]:
    findings: list[Finding] = []
    for check in _CHECKS:
        try:
            check(findings)
        except Exception:
            tb = traceback.format_exc()
            _log_error(f"Check {check.__name__} failed:\n{tb}")
    return findings


def main():
    parser = argparse.ArgumentParser(description="Cortex OS weekly hygiene scanner")
    parser.add_argument("--dry-run", action="store_true", help="Print findings, don't write report")
    parser.add_argument("--no-notify", action="store_true", help="Skip Telegram notification")
    args = parser.parse_args()

    _debug("hygiene scan started")

    findings = run_all_checks()

    date_str = datetime.now(PST).strftime("%Y-%m-%d")
    report_text = render_report(findings, date_str)

    if args.dry_run:
        print(report_text)
        _debug(f"dry-run complete: {len(findings)} findings")
        return

    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    report_path = RESEARCH_DIR / f"{date_str}-hygiene-report.md"
    report_path.write_text(report_text)

    _debug(f"report written: {report_path} ({len(findings)} findings)")

    if not args.no_notify:
        send_telegram(report_path, findings)

    print(f"Hygiene report: {report_path}")
    print(f"Findings: {len(findings)} total")


if __name__ == "__main__":
    main()
