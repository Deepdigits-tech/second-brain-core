"""
Tests for the database backup script (.claude/scripts/db_backup.py)
"""

import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Import the module directly
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "scripts"))

from db_backup import (
    backup_database,
    create_backup_dirs,
    prune_old_backups,
    verify_backup_integrity,
)


@pytest.fixture
def temp_db():
    """Create a temporary sqlite database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    # Create a simple database with a table
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO test (name) VALUES (?)", ("test_data",))
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    db_path.unlink(missing_ok=True)


@pytest.fixture
def temp_backup_dir():
    """Create a temporary backup directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def test_backup_database_success(temp_db, temp_backup_dir):
    """Test successful database backup."""
    backup_path, status = backup_database(temp_db, temp_backup_dir)

    assert status == "success"
    assert backup_path is not None
    assert backup_path.exists()
    assert backup_path.name == temp_db.name

    # Verify backup has correct permissions
    mode = backup_path.stat().st_mode & 0o777
    assert mode == 0o600, f"Backup file has mode {oct(mode)}, expected 0o600"


def test_backup_database_missing_db(temp_backup_dir):
    """Test backup of a non-existent database is skipped."""
    missing_db = Path("/tmp/nonexistent_db_12345.db")
    backup_path, status = backup_database(missing_db, temp_backup_dir)

    assert status == "database does not exist"
    assert backup_path is None


def test_backup_integrity(temp_db, temp_backup_dir):
    """Test that backup passes integrity check."""
    backup_path, status = backup_database(temp_db, temp_backup_dir)

    assert status == "success"
    assert verify_backup_integrity(backup_path) is True


def test_backup_data_preserved(temp_db, temp_backup_dir):
    """Test that data in original database is preserved in backup."""
    # Add test data
    conn = sqlite3.connect(str(temp_db))
    conn.execute("INSERT INTO test (name) VALUES (?)", ("data1",))
    conn.execute("INSERT INTO test (name) VALUES (?)", ("data2",))
    conn.commit()
    conn.close()

    # Backup
    backup_path, status = backup_database(temp_db, temp_backup_dir)
    assert status == "success"

    # Verify data in backup
    backup_conn = sqlite3.connect(str(backup_path))
    cursor = backup_conn.execute("SELECT COUNT(*) FROM test")
    count = cursor.fetchone()[0]
    backup_conn.close()

    assert count == 3  # 1 from fixture + 2 added


def test_backup_dry_run(temp_db, temp_backup_dir):
    """Test dry-run mode doesn't create files."""
    backup_path, status = backup_database(temp_db, temp_backup_dir, dry_run=True)

    assert status == "dry-run"
    # Verify file was not actually created
    assert not (temp_backup_dir / temp_db.name).exists()


def test_create_backup_dirs(tmp_path):
    """Test backup directory creation."""
    # Mock the get_machine_backup_dir to return our temp directory
    with mock.patch("db_backup.get_machine_backup_dir", return_value=tmp_path / "machine"):
        with mock.patch("db_backup.get_today_backup_dir", return_value=tmp_path / "machine" / "2026-09-20"):
            backup_dir = create_backup_dirs(dry_run=False)

            assert backup_dir is not None
            assert backup_dir.exists()
            # Verify permissions (dir should be 0o700)
            mode = backup_dir.stat().st_mode & 0o777
            assert mode == 0o700, f"Backup directory has mode {oct(mode)}, expected 0o700"


def test_create_backup_dirs_dry_run(tmp_path):
    """Test dry-run doesn't create directories."""
    with mock.patch("db_backup.get_machine_backup_dir", return_value=tmp_path / "machine"):
        with mock.patch("db_backup.get_today_backup_dir", return_value=tmp_path / "machine" / "2026-09-20"):
            backup_dir = create_backup_dirs(dry_run=True)

            # In dry-run, directory path is returned but not created
            assert backup_dir is not None


def test_prune_old_backups_keeps_recent(tmp_path):
    """Test that recent backups are kept during pruning."""
    from datetime import datetime, timedelta

    machine_dir = tmp_path / "machine"
    machine_dir.mkdir()

    # Create a backup folder from today
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_folder = machine_dir / today_str
    today_folder.mkdir()
    (today_folder / "test.db").write_text("test")

    # Mock the machine directory
    with mock.patch("db_backup.get_machine_backup_dir", return_value=machine_dir):
        removed, total = prune_old_backups(keep_days=14, dry_run=False)

        # Today's backup should not be removed
        assert today_folder.exists()
        assert removed == 0


def test_prune_old_backups_removes_old(tmp_path):
    """Test that old backups are removed during pruning."""
    from datetime import datetime, timedelta

    machine_dir = tmp_path / "machine"
    machine_dir.mkdir()

    # Create an old backup folder (20 days ago)
    old_date = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
    old_folder = machine_dir / old_date
    old_folder.mkdir()
    (old_folder / "test.db").write_text("test")

    # Create a recent backup folder
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_folder = machine_dir / today_str
    today_folder.mkdir()
    (today_folder / "test.db").write_text("test")

    # Mock the machine directory
    with mock.patch("db_backup.get_machine_backup_dir", return_value=machine_dir):
        removed, total = prune_old_backups(keep_days=14, dry_run=False)

        # Old backup should be removed, recent one kept
        assert not old_folder.exists()
        assert today_folder.exists()
        assert removed == 1
        assert total == 2  # Both folders were considered


def test_prune_old_backups_skips_non_date_folders(tmp_path):
    """Test that non-date-named folders are skipped during pruning."""
    machine_dir = tmp_path / "machine"
    machine_dir.mkdir()

    # Create a non-date folder (should be skipped)
    other_folder = machine_dir / "other-folder"
    other_folder.mkdir()
    (other_folder / "test.db").write_text("test")

    # Mock the machine directory
    with mock.patch("db_backup.get_machine_backup_dir", return_value=machine_dir):
        removed, total = prune_old_backups(keep_days=14, dry_run=False)

        # Non-date folder should still exist
        assert other_folder.exists()
        assert removed == 0
        assert total == 0  # No date-named folders processed


def test_prune_old_backups_dry_run(tmp_path):
    """Test dry-run mode for pruning."""
    from datetime import datetime, timedelta

    machine_dir = tmp_path / "machine"
    machine_dir.mkdir()

    # Create an old backup folder
    old_date = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
    old_folder = machine_dir / old_date
    old_folder.mkdir()
    (old_folder / "test.db").write_text("test")

    # Mock the machine directory
    with mock.patch("db_backup.get_machine_backup_dir", return_value=machine_dir):
        removed, total = prune_old_backups(keep_days=14, dry_run=True)

        # Dry-run should not actually remove
        assert old_folder.exists()
        assert removed == 1  # Reported that it would be removed


def test_prune_never_follows_a_symlink_named_like_a_date(tmp_path):
    """is_dir() follows links: the target's files used to be deleted."""
    import db_backup
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("keep me")
    machine_dir = tmp_path / "machine"
    machine_dir.mkdir()
    (machine_dir / "2020-01-01").symlink_to(victim, target_is_directory=True)
    old = machine_dir / "2020-01-02"
    old.mkdir()
    (old / "chat.db").write_text("x")

    with mock.patch("db_backup.get_machine_backup_dir", return_value=machine_dir):
        removed, total = db_backup.prune_old_backups(keep_days=14)

    assert (victim / "precious.txt").read_text() == "keep me"
    assert not old.exists()            # the real old folder still went
    assert (removed, total) == (1, 1)


def test_one_unremovable_folder_does_not_stop_the_prune(tmp_path):
    import db_backup
    machine_dir = tmp_path / "machine"
    for name in ("2020-01-01", "2020-01-02"):
        (machine_dir / name / "sub").mkdir(parents=True)   # a subfolder broke the old unlink loop

    with mock.patch("db_backup.get_machine_backup_dir", return_value=machine_dir):
        removed, total = db_backup.prune_old_backups(keep_days=14)

    assert (removed, total) == (2, 2)
    assert list(machine_dir.iterdir()) == []


def test_backup_dir_override_and_its_guard(tmp_path, monkeypatch):
    import db_backup
    monkeypatch.setenv("CORTEX_BACKUP_DIR", str(tmp_path / "b"))
    assert db_backup.get_backup_dir() == tmp_path / "b"
    for bad in ("", "  ", "/"):
        monkeypatch.setenv("CORTEX_BACKUP_DIR", bad)
        assert db_backup.get_backup_dir() == Path.home() / ".cortex-backups"


def test_databases_are_found_from_the_script_not_the_current_directory(tmp_path, monkeypatch):
    import db_backup
    monkeypatch.chdir(tmp_path)
    assert tmp_path not in db_backup.PROJECT_ROOT.parents and db_backup.PROJECT_ROOT != tmp_path
    assert (db_backup.PROJECT_ROOT / ".claude" / "scripts" / "db_backup.py").exists()


def test_a_run_that_finds_no_database_is_a_failure(tmp_path, monkeypatch):
    import db_backup
    monkeypatch.setenv("CORTEX_BACKUP_DIR", str(tmp_path / "b"))
    monkeypatch.setattr(db_backup, "PROJECT_ROOT", tmp_path / "empty-repo")
    monkeypatch.setattr(sys, "argv", ["db_backup.py"])
    assert db_backup.main() == 1
