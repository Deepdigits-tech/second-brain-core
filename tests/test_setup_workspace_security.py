"""Security test for setup_workspace.py: verify credentials file permissions.

FIX 2 (2026-09-20): Credentials file must be created with 0o600 mode,
not world-readable (0o644).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_credentials_file_created_with_0o600_mode(tmp_path, monkeypatch):
    """Credentials file is created with restricted mode 0o600."""
    # Set up isolated environment
    monkeypatch.chdir(tmp_path)

    # Import after changing directory
    sys.path.insert(0, str(REPO_ROOT))
    import setup_workspace  # noqa: E402

    # Create test env dict
    env_dict = {
        "OWNER_NAME": "Test User",
        "TELEGRAM_BOT_TOKEN": "123:secret-token",
    }

    # Call write_credentials_file
    setup_workspace.write_credentials_file(env_dict, dry_run=False)

    # Check file exists
    credentials_path = Path(".claude") / ".env"
    assert credentials_path.exists(), f"Credentials file not created at {credentials_path}"

    # Check file mode is exactly 0o600
    mode = stat.S_IMODE(credentials_path.stat().st_mode)
    assert mode == 0o600, f"Credentials file mode is {oct(mode)}, expected 0o600"


def test_credentials_file_existing_loose_permissions_tightened(tmp_path, monkeypatch):
    """Existing credentials file with loose permissions is tightened to 0o600."""
    # Set up isolated environment
    monkeypatch.chdir(tmp_path)

    # Create directory
    Path(".claude").mkdir(exist_ok=True)

    # Create credentials file with loose permissions (0o644)
    credentials_path = Path(".claude") / ".env"
    credentials_path.write_text("OLD_KEY=old_value\n")
    os.chmod(str(credentials_path), 0o644)

    # Verify it starts with loose permissions
    mode_before = stat.S_IMODE(credentials_path.stat().st_mode)
    assert mode_before == 0o644, f"Setup: file created with {oct(mode_before)}"

    # Import and call write_credentials_file
    sys.path.insert(0, str(REPO_ROOT))
    import setup_workspace  # noqa: E402

    env_dict = {"OWNER_NAME": "Test", "NEW_KEY": "new_value"}
    setup_workspace.write_credentials_file(env_dict, dry_run=False)

    # Check file mode is now 0o600
    mode_after = stat.S_IMODE(credentials_path.stat().st_mode)
    assert mode_after == 0o600, f"After write, mode is {oct(mode_after)}, expected 0o600"


def test_env_example_and_script_agree_on_filename():
    """master.env.example exists; setup_workspace.py's default --env target
    (master.env, the file a user copies master.env.example to) matches it."""
    env_example = REPO_ROOT / "master.env.example"
    assert env_example.exists(), f"{env_example} not found"

    # Verify script's default --env value is the plain (non-.example) name
    setup_py = (REPO_ROOT / "setup_workspace.py").read_text()
    assert '"master.env"' in setup_py, "setup_workspace.py doesn't default to master.env"


def test_credentials_file_uses_os_open_not_builtin_open():
    """Verify setup_workspace.py uses os.open() with 0o600, not builtin open()."""
    setup_py = (REPO_ROOT / "setup_workspace.py").read_text()

    # Should use os.open with O_WRONLY | O_CREAT | O_TRUNC and 0o600
    assert "os.open(" in setup_py, "setup_workspace.py must use os.open()"
    assert "0o600" in setup_py, "setup_workspace.py must specify 0o600 mode"
    assert "os.fdopen(" in setup_py, "setup_workspace.py must use os.fdopen() after os.open()"
    assert "os.chmod(" in setup_py, "setup_workspace.py must call os.chmod() to tighten existing files"

    # Should NOT use builtin open for credentials
    # (this is a heuristic check; the exact pattern is in the fixed code)
    lines = setup_py.split("\n")
    creds_section_started = False
    for i, line in enumerate(lines):
        if "write_credentials_file" in line:
            creds_section_started = True
        if creds_section_started and "with open(credentials_path" in line:
            pytest.fail("setup_workspace.py still uses builtin open() for credentials file")
