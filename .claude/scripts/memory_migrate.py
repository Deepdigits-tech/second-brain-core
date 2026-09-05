"""One-time migration of vault/MEMORY.md from a flat state+log file to a
small index backed by entity/topic/decision pages.

Usage:
    memory_migrate.py backup [--force] [--vault PATH]
    memory_migrate.py rollback [--dry-run] [--vault PATH]
    memory_migrate.py verify [--vault PATH]

--vault PATH points every operation at a scratch copy of the vault instead
of the repo's own vault/ — for rehearsing backup/rollback/verify without
touching real data.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VAULT_DIR = REPO_ROOT / "vault"

MEMORY_INDEX_MAX_BYTES = 5_120
MEMORY_INDEX_DIRS = ("entities", "topics", "decisions")

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[\[([^\]|#]+)")


def _memory_md(vault_dir: Path | None = None) -> Path:
    return (vault_dir if vault_dir is not None else VAULT_DIR) / "MEMORY.md"


def _backup_path(vault_dir: Path | None = None) -> Path:
    return (vault_dir if vault_dir is not None else VAULT_DIR) / "decisions" / "pre-migration-backup.md"


def index_size_ok(text: str) -> bool:
    """The index must fit under MEMORY_INDEX_MAX_BYTES."""
    return len(text.encode("utf-8")) <= MEMORY_INDEX_MAX_BYTES


def subjects_of(text: str) -> list[str]:
    """The bold subject of every `## Current State` bullet, plus the bold
    lead of every `## Log` bullet. Order preserved, no dedup — the caller
    only checks membership.
    """
    subjects: list[str] = []
    section = None  # "Current State" | "Log" | None
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip()
            if heading in ("Current State", "Log"):
                section = heading
            else:
                # A different H2 (e.g. a stray section) ends the run.
                section = None
            continue
        if section is None:
            continue
        if not stripped.startswith("- "):
            continue
        m = _BOLD_RE.search(stripped)
        if m:
            subjects.append(m.group(1).strip())
    return subjects


LESSONS_PAGES = ("lessons-gtm", "lessons-engineering", "lessons-working-with-agents")


def lessons_learned_bullets(text: str) -> list[str]:
    """Every bullet line under the backup's ### Lessons Learned subsection."""
    lines = text.splitlines()
    bullets: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("### "):
            in_section = stripped == "### Lessons Learned"
            continue
        if stripped.startswith("## "):
            in_section = False
            continue
        if in_section and stripped.startswith("- "):
            bullets.append(stripped)
    return bullets


def backup(force: bool = False, vault_dir: Path | None = None) -> Path:
    """Copy MEMORY.md to decisions/pre-migration-backup.md under vault_dir.

    Refuses to overwrite an existing backup unless force=True. Also creates
    the entities/topics/decisions folders and an empty core-memories.md if
    they don't already exist.
    """
    vault_dir = vault_dir if vault_dir is not None else VAULT_DIR
    src = _memory_md(vault_dir)
    dst = _backup_path(vault_dir)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists() and not force:
        print(f"backup already exists at {dst} — pass --force to overwrite", file=sys.stderr)
        sys.exit(1)

    if not src.exists():
        print(f"nothing to back up: {src} does not exist", file=sys.stderr)
        sys.exit(1)

    dst.write_text(src.read_text())

    for sub in MEMORY_INDEX_DIRS:
        (vault_dir / sub).mkdir(parents=True, exist_ok=True)

    core = vault_dir / "core-memories.md"
    if not core.exists():
        core.write_text(
            "# Core Memories\n\n"
            "_User-initiated only. Cortex never writes here on its own — this "
            "page holds what the owner explicitly asks to be remembered "
            "permanently, added by hand or by direct request in a session. "
            "Empty until he adds something._\n"
        )

    print(f"backed up {src} -> {dst}")
    return dst


def rollback(dry_run: bool = False, vault_dir: Path | None = None) -> str:
    """Restore MEMORY.md from decisions/pre-migration-backup.md. Pages stay
    on disk — they are inert without the index — rollback only touches
    MEMORY.md.
    """
    vault_dir = vault_dir if vault_dir is not None else VAULT_DIR
    src = _backup_path(vault_dir)
    dst = _memory_md(vault_dir)

    if not src.exists():
        msg = f"no backup found at {src}"
        print(msg, file=sys.stderr)
        if dry_run:
            return msg
        sys.exit(1)

    if dry_run:
        msg = f"would restore {dst} from {src}"
        print(msg)
        return msg

    dst.write_text(src.read_text())
    msg = f"restored {dst} from {src}"
    print(msg)
    return msg


def verify(vault_dir: Path | None = None) -> int:
    """Accounting check: every subject from the backup must appear
    somewhere in the new index or pages. Exits 1 on any unaccounted
    subject; prints a report either way.
    """
    vault_dir = vault_dir if vault_dir is not None else VAULT_DIR
    backup_path = _backup_path(vault_dir)
    if not backup_path.exists():
        print(f"no backup at {backup_path} — run `backup` first", file=sys.stderr)
        return 1

    index_path = _memory_md(vault_dir)
    index_text = index_path.read_text() if index_path.exists() else ""

    if not index_size_ok(index_text):
        size = len(index_text.encode("utf-8"))
        print(f"FAIL: index is {size} bytes, over the {MEMORY_INDEX_MAX_BYTES} limit")
        return 1

    # every [[link]] resolves
    broken_links = []
    for name in _LINK_RE.findall(index_text):
        name = name.strip()
        cand1 = vault_dir / f"{name}.md"
        cand2 = vault_dir / name / "index.md"
        if not cand1.exists() and not cand2.exists():
            broken_links.append(name)
    if broken_links:
        print(f"FAIL: broken links in index: {broken_links}")
        return 1

    # every page is linked from the index
    linked_targets = {n.strip() for n in _LINK_RE.findall(index_text)}
    orphan_pages = []
    for sub in MEMORY_INDEX_DIRS:
        d = vault_dir / sub
        if not d.exists():
            continue
        for page in sorted(d.glob("*.md")):
            if page.name == "pre-migration-backup.md":
                continue
            target = f"{sub}/{page.stem}"
            if target not in linked_targets:
                orphan_pages.append(target)
    if orphan_pages:
        print(f"FAIL: pages not linked from the index: {orphan_pages}")
        return 1

    # every subject from the backup is accounted for
    subjects = subjects_of(backup_path.read_text())
    corpus = index_text
    for folder in ("entities", "topics", "decisions", "opportunities"):
        d = vault_dir / folder
        if not d.exists():
            continue
        for page in d.rglob("*.md"):
            corpus += "\n" + page.read_text()

    missing = [s for s in subjects if s.lower() not in corpus.lower()]
    if missing:
        print(f"FAIL: subjects dropped by the migration: {missing}")
        return 1

    # every ### Lessons Learned bullet from the backup landed on one of the
    # theme-split lessons pages
    lessons_bullets = lessons_learned_bullets(backup_path.read_text())
    lessons_corpus = ""
    for name in LESSONS_PAGES:
        page = vault_dir / "topics" / f"{name}.md"
        if page.exists():
            lessons_corpus += page.read_text()
    missing_lessons = [b for b in lessons_bullets if b not in lessons_corpus]
    if missing_lessons:
        print(f"FAIL: lessons dropped by the theme split: {missing_lessons}")
        return 1

    print(
        f"OK: index {len(index_text.encode('utf-8'))} bytes, {len(subjects)} subjects "
        f"accounted for, {len(lessons_bullets)} lessons accounted for, no broken links, "
        "no orphan pages"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup")
    p_backup.add_argument("--force", action="store_true")
    p_backup.add_argument("--vault", type=Path, default=VAULT_DIR)

    p_rollback = sub.add_parser("rollback")
    p_rollback.add_argument("--dry-run", action="store_true")
    p_rollback.add_argument("--vault", type=Path, default=VAULT_DIR)

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--vault", type=Path, default=VAULT_DIR)

    args = parser.parse_args()

    if args.command == "backup":
        backup(force=args.force, vault_dir=args.vault)
    elif args.command == "rollback":
        rollback(dry_run=args.dry_run, vault_dir=args.vault)
    elif args.command == "verify":
        sys.exit(verify(vault_dir=args.vault))


if __name__ == "__main__":
    main()
