#!/usr/bin/env python3
"""
Nightly backup of sqlite3 databases that are not tracked in git.

Backs up:
- .claude/data/chat.db (chat session history)

memory.db is deliberately excluded — it is a derived search index, fully
rebuildable from the vault's own markdown files (see memory_index.py), so
backing it up buys nothing and only costs disk.

Each DB is backed up with integrity verification and automatic pruning of old backups.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Resolved from this file, never from the current directory: run by hand from
# anywhere else, a cwd-relative path found no databases, "skipped" all of them
# and exited 0 — a backup that backs up nothing and says it went fine.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATABASES = ("chat.db",)


def get_backup_dir():
    """~/.cortex-backups, or CORTEX_BACKUP_DIR when set to a real path.

    An empty value or "/" is ignored: the prune below removes folders inside
    this directory, and it must never be pointed at the filesystem root.
    """
    override = os.environ.get("CORTEX_BACKUP_DIR", "").strip()
    if override and Path(override).resolve() != Path("/"):
        return Path(override).expanduser()
    return Path.home() / ".cortex-backups"


def get_machine_backup_dir():
    """Get machine-specific backup directory."""
    import socket
    machine_name = socket.gethostname()
    return get_backup_dir() / machine_name


def get_today_backup_dir():
    """Get today's dated backup directory."""
    today = datetime.now().strftime("%Y-%m-%d")
    return get_machine_backup_dir() / today


def create_backup_dirs(dry_run=False):
    """Create backup directories with proper permissions."""
    backup_dir = get_machine_backup_dir()
    today_dir = get_today_backup_dir()

    if dry_run:
        print(f"[DRY-RUN] Would create {today_dir} with mode 0o700")
        return today_dir

    try:
        # Create base machine dir with mode 0o700
        backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Create today's dir with mode 0o700
        today_dir.mkdir(mode=0o700, exist_ok=True)
        return today_dir
    except Exception as e:
        print(f"ERROR: Failed to create backup directory: {e}", file=sys.stderr)
        return None


def verify_backup_integrity(backup_path):
    """Verify a backup sqlite DB can be opened and passes integrity check."""
    try:
        conn = sqlite3.connect(str(backup_path))
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check")
        result = cursor.fetchone()
        conn.close()
        return result and result[0] == "ok"
    except Exception as e:
        print(f"ERROR: Integrity check failed for {backup_path}: {e}", file=sys.stderr)
        return False


def backup_database(db_path, backup_dir, dry_run=False):
    """Back up a single database file using sqlite3 backup API."""
    if not db_path.exists():
        return None, "database does not exist"

    backup_path = backup_dir / db_path.name

    if dry_run:
        print(f"[DRY-RUN] Would backup {db_path} to {backup_path}")
        return backup_path, "dry-run"

    try:
        # Open source database
        src_conn = sqlite3.connect(str(db_path))
        # Open destination database
        dst_conn = sqlite3.connect(str(backup_path))

        # Perform backup (safe while DB is in use)
        with dst_conn:
            src_conn.backup(dst_conn)

        src_conn.close()
        dst_conn.close()

        # Set file permissions 0o600 (owner read/write only)
        backup_path.chmod(0o600)

        # Verify backup
        if not verify_backup_integrity(backup_path):
            return backup_path, "backup failed integrity check"

        return backup_path, "success"
    except Exception as e:
        print(f"ERROR: Failed to backup {db_path}: {e}", file=sys.stderr)
        return backup_path, f"error: {e}"


def prune_old_backups(keep_days=14, dry_run=False):
    """Remove backup folders older than keep_days, keeping only date-named folders."""
    machine_dir = get_machine_backup_dir()

    if not machine_dir.exists():
        return 0, 0

    cutoff_date = datetime.now() - timedelta(days=keep_days)
    removed_count = 0
    total_count = 0

    for folder in sorted(machine_dir.iterdir()):
        try:
            # is_dir() follows symlinks: a link named like a date would have its
            # TARGET's files deleted. A link is never one of our backups.
            if folder.is_symlink() or not folder.is_dir():
                continue

            # Only process date-named folders (YYYY-MM-DD)
            try:
                folder_date = datetime.strptime(folder.name, "%Y-%m-%d")
            except ValueError:
                # Not a date-named folder, skip
                continue

            total_count += 1

            if folder_date < cutoff_date:
                if dry_run:
                    print(f"[DRY-RUN] Would remove {folder}")
                else:
                    shutil.rmtree(folder)
                    print(f"Removed old backup: {folder.name}")

                removed_count += 1
        except Exception as e:
            # One bad folder must not stop the rest from being pruned.
            print(f"WARNING: could not prune {folder.name}: {e}", file=sys.stderr)

    return removed_count, total_count


def main():
    parser = argparse.ArgumentParser(
        description="Back up sqlite3 databases and prune old backups"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without making changes",
    )

    args = parser.parse_args()

    # Databases to back up
    databases = [PROJECT_ROOT / ".claude" / "data" / name for name in DATABASES]

    # Create backup directory
    backup_dir = create_backup_dirs(dry_run=args.dry_run)
    if backup_dir is None:
        return 1

    # Back up each database
    results = []
    any_failed = False

    for db_path in databases:
        backup_path, status = backup_database(db_path, backup_dir, dry_run=args.dry_run)
        results.append((db_path.name, status))

        if status not in ("success", "dry-run", "database does not exist"):
            any_failed = True

    # Prune old backups
    removed, total = prune_old_backups(dry_run=args.dry_run)

    # Print summary
    print(f"\nBackup Summary:")
    for db_name, status in results:
        if status == "database does not exist":
            print(f"  {db_name}: {status} (skipped)")
        else:
            print(f"  {db_name}: {status}")

    print(f"Pruning: removed {removed} of {total} old backup folders (kept last 14 days)")

    if any_failed:
        print("\nERROR: One or more backups failed", file=sys.stderr)
        return 1

    # A run that finds nothing to back up ran against the wrong place, and the
    # scheduler should see a failure rather than a silent no-op.
    if not args.dry_run and not any(status == "success" for _, status in results):
        print("\nERROR: no database was found to back up", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
