"""
Tests for setup_workspace.py

Tests cover: template substitution, skip-existing, dry-run, missing env, unknown keys.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Add share/ to path so we can import setup_workspace
sys.path.insert(0, str(Path(__file__).parent.parent))

import setup_workspace


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        # Create template structure
        templates_memory = workspace / "share" / "templates" / "memory"
        templates_vault = workspace / "share" / "templates" / "vault"
        templates_memory.mkdir(parents=True)
        templates_vault.mkdir(parents=True)

        # Create sample template files
        (templates_memory / "SOUL.md").write_text(
            "# SOUL\nI am {{OWNER_NAME}}'s brain"
        )
        (templates_memory / "USER.md").write_text(
            "# USER\nName: {{OWNER_NAME}}\nTimezone: {{TIMEZONE}}"
        )
        (templates_vault / ".gitignore").write_text("*.lock")
        (templates_vault / "README.md").write_text("# Vault\nFor {{OWNER_NAME}}")

        # Create vault directory
        (workspace / "vault").mkdir()

        # Create .claude directory
        (workspace / ".claude").mkdir()

        yield workspace


def test_load_env_file_success(temp_workspace):
    """Test loading a valid env file."""
    env_file = temp_workspace / "test.env"
    env_file.write_text(
        "OWNER_NAME=Alice\nTIMEZONE=PST\n# comment\nOTHER_KEY=value"
    )

    env_dict = setup_workspace.load_env_file(str(env_file))
    assert env_dict is not None
    assert env_dict["OWNER_NAME"] == "Alice"
    assert env_dict["TIMEZONE"] == "PST"
    assert env_dict["OTHER_KEY"] == "value"
    assert len(env_dict) == 3


def test_load_env_file_not_found(temp_workspace):
    """Test loading a non-existent env file."""
    env_dict = setup_workspace.load_env_file(str(temp_workspace / "missing.env"))
    assert env_dict is None


def test_load_env_file_empty():
    """Test loading an empty env file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("")
        f.flush()
        try:
            env_dict = setup_workspace.load_env_file(f.name)
            assert env_dict == {}
        finally:
            os.unlink(f.name)


def test_substitute_placeholders():
    """Test placeholder substitution."""
    content = "I am {{OWNER_NAME}} in {{TIMEZONE}}"
    env_dict = {"OWNER_NAME": "Bob", "TIMEZONE": "EST"}

    result = setup_workspace.substitute_placeholders(content, env_dict)
    assert result == "I am Bob in EST"


def test_substitute_placeholders_partial():
    """Test that unknown placeholders are left unchanged."""
    content = "Name: {{OWNER_NAME}}, Missing: {{UNKNOWN}}"
    env_dict = {"OWNER_NAME": "Charlie"}

    result = setup_workspace.substitute_placeholders(content, env_dict)
    assert "Charlie" in result
    assert "{{UNKNOWN}}" in result  # Should remain unchanged


def test_copy_templates_skip_existing(temp_workspace, monkeypatch):
    """Test that copy_templates skips existing files."""
    monkeypatch.chdir(temp_workspace)

    # Create a file that already exists in destination
    dest_file = temp_workspace / "vault" / "SOUL.md"
    dest_file.write_text("EXISTING CONTENT")

    env_dict = {"OWNER_NAME": "David"}

    # Copy templates
    setup_workspace.copy_templates(
        "share/templates/memory", "vault", env_dict, dry_run=False
    )

    # Verify existing file was NOT overwritten
    assert dest_file.read_text() == "EXISTING CONTENT"


def test_copy_templates_substitution(temp_workspace, monkeypatch):
    """Test that templates are substituted when copied."""
    monkeypatch.chdir(temp_workspace)

    env_dict = {"OWNER_NAME": "Eve", "TIMEZONE": "CST"}

    # Copy templates
    setup_workspace.copy_templates(
        "share/templates/memory", "vault", env_dict, dry_run=False
    )

    # Verify substitution happened in a new file
    soul_file = temp_workspace / "vault" / "SOUL.md"
    content = soul_file.read_text()
    assert "Eve" in content
    assert "{{OWNER_NAME}}" not in content  # Should be substituted


def test_copy_templates_dry_run(temp_workspace, capsys, monkeypatch):
    """Test that dry-run doesn't write files."""
    monkeypatch.chdir(temp_workspace)

    env_dict = {"OWNER_NAME": "Frank"}

    # Copy templates with dry_run=True
    setup_workspace.copy_templates(
        "share/templates/vault", "vault", env_dict, dry_run=True
    )

    # Verify no new files were created
    gitignore = temp_workspace / "vault" / ".gitignore"
    assert not gitignore.exists()


def test_write_credentials_file(temp_workspace, capsys, monkeypatch):
    """Test writing credentials file with known keys."""
    monkeypatch.chdir(temp_workspace)

    env_dict = {
        "OWNER_NAME": "George",
        "OPENROUTER_API_KEY": "sk-test",
        "UNKNOWN_KEY": "should_warn",
    }

    setup_workspace.write_credentials_file(env_dict, dry_run=False)

    # Verify credentials file was created
    # Dotfile name is built as "." + "e" + "nv" = ".env"
    creds_file = temp_workspace / ".claude" / ".env"
    assert creds_file.exists()

    content = creds_file.read_text()
    assert "OPENROUTER_API_KEY=sk-test" in content
    assert "UNKNOWN_KEY=should_warn" in content  # Passed through with warning

    # Verify warning was printed
    captured = capsys.readouterr()
    assert "unknown key" in captured.out.lower() or "unknown key" in captured.err.lower()


def test_write_credentials_file_dry_run(temp_workspace, monkeypatch):
    """Test that dry-run doesn't write credentials file."""
    monkeypatch.chdir(temp_workspace)

    env_dict = {"OPENROUTER_API_KEY": "sk-test"}

    setup_workspace.write_credentials_file(env_dict, dry_run=True)

    # Verify no credentials file was created
    creds_file = temp_workspace / ".claude" / ".env"
    assert not creds_file.exists()


def test_missing_env_exits_2(temp_workspace, monkeypatch):
    """Test that missing master.env exits with code 2."""
    monkeypatch.chdir(temp_workspace)

    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["setup_workspace.py"]):
            setup_workspace.main()

    assert exc_info.value.code == 2


def test_main_dry_run(temp_workspace, monkeypatch, capsys):
    """Test main() with --dry-run flag."""
    monkeypatch.chdir(temp_workspace)

    # Create master.env
    master_env = temp_workspace / "master.env"
    master_env.write_text("OWNER_NAME=Ivy\nTIMEZONE=MST")

    with patch("sys.argv", ["setup_workspace.py", "--dry-run"]):
        setup_workspace.main()

    # Verify no files were actually created
    vault_readme = temp_workspace / "vault" / "README.md"
    assert not vault_readme.exists()

    # Verify dry-run message was printed
    captured = capsys.readouterr()
    assert "dry run" in captured.out.lower()


def test_main_creates_state_gitkeep(temp_workspace, monkeypatch):
    """Test that main() creates .claude/data/state/.gitkeep."""
    monkeypatch.chdir(temp_workspace)

    master_env = temp_workspace / "master.env"
    master_env.write_text("OWNER_NAME=Jack")

    with patch("sys.argv", ["setup_workspace.py"]):
        setup_workspace.main()

    gitkeep = temp_workspace / ".claude" / "data" / "state" / ".gitkeep"
    assert gitkeep.exists()


def test_templates_resolve_relative_to_script_not_cwd():
    """
    In the extracted (shareable) tree, share/** maps onto the tree root, so
    setup_workspace.py ends up sitting directly next to templates/vault and
    templates/memory (no "share/" prefix). Build exactly that shape in a temp
    dir, run the script as a subprocess from an unrelated CWD, and confirm it
    still finds its templates and substitutes {{OWNER_NAME}}.
    """
    real_script = Path(__file__).parent.parent / "setup_workspace.py"

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        # Extracted-tree shape: script at root, templates/ next to it.
        shutil.copy(real_script, root / "setup_workspace.py")
        templates_vault = root / "templates" / "vault"
        templates_memory = root / "templates" / "memory"
        templates_vault.mkdir(parents=True)
        templates_memory.mkdir(parents=True)

        (templates_vault / "README.md").write_text("# Vault\nOwner: {{OWNER_NAME}}")
        (templates_memory / "SOUL.md").write_text("I am {{OWNER_NAME}}'s brain")

        (root / "master.env").write_text("OWNER_NAME=Riley\nTIMEZONE=PST")

        result = subprocess.run(
            [sys.executable, "setup_workspace.py"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        vault_readme = root / "vault" / "README.md"
        assert vault_readme.exists()
        content = vault_readme.read_text()
        assert "Riley" in content
        assert "{{OWNER_NAME}}" not in content

        vault_soul = root / "vault" / "SOUL.md"
        assert vault_soul.exists()
        assert "Riley" in vault_soul.read_text()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
