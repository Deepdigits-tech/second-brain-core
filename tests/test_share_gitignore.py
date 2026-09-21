"""
Test that the root .gitignore allows tracking of vault notes and doesn't hide
indexable folders.
"""

import subprocess
import tempfile
from pathlib import Path


def test_vault_daily_not_ignored():
    """Test that vault/daily/YYYY-MM-DD.md is NOT ignored by the root .gitignore."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        # Copy the root .gitignore to the test repo
        gitignore_src = Path(__file__).parent.parent / ".gitignore"
        gitignore_dst = repo_root / ".gitignore"
        gitignore_dst.write_text(gitignore_src.read_text())

        # Create a test git repo
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )

        # Add .gitignore to repo
        subprocess.run(
            ["git", "add", ".gitignore"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add gitignore"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )

        # Create a daily note
        daily_dir = repo_root / "vault" / "daily"
        daily_dir.mkdir(parents=True)
        daily_file = daily_dir / "2026-01-01.md"
        daily_file.write_text("# 2026-01-01\n\nTest daily note\n")

        # Check if it's ignored (git check-ignore returns 0 if ignored, 1 if not)
        result = subprocess.run(
            ["git", "check-ignore", str(daily_file)],
            cwd=repo_root,
            capture_output=True,
        )

        assert result.returncode == 1, (
            f"vault/daily/2026-01-01.md is ignored by .gitignore, "
            f"but should be tracked. git check-ignore returned {result.returncode}"
        )


def test_vault_research_not_ignored():
    """Test that vault/research/ files are NOT ignored by the root .gitignore."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        # Copy the root .gitignore to the test repo
        gitignore_src = Path(__file__).parent.parent / ".gitignore"
        gitignore_dst = repo_root / ".gitignore"
        gitignore_dst.write_text(gitignore_src.read_text())

        # Create a test git repo
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )

        # Add .gitignore to repo
        subprocess.run(
            ["git", "add", ".gitignore"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add gitignore"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )

        # Create a research note
        research_dir = repo_root / "vault" / "research"
        research_dir.mkdir(parents=True)
        research_file = research_dir / "2026-01-01-topic.md"
        research_file.write_text("# Research on Topic\n\nTest research note\n")

        # Check if it's ignored
        result = subprocess.run(
            ["git", "check-ignore", str(research_file)],
            cwd=repo_root,
            capture_output=True,
        )

        assert result.returncode == 1, (
            f"vault/research/2026-01-01-topic.md is ignored by .gitignore, "
            f"but should be tracked. git check-ignore returned {result.returncode}"
        )


def test_machine_local_state_is_ignored():
    """Test that machine-local state files (.claude/data/) ARE still ignored."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        # Copy the root .gitignore to the test repo
        gitignore_src = Path(__file__).parent.parent / ".gitignore"
        gitignore_dst = repo_root / ".gitignore"
        gitignore_dst.write_text(gitignore_src.read_text())

        # Create a test git repo
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )

        # Add .gitignore to repo
        subprocess.run(
            ["git", "add", ".gitignore"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add gitignore"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )

        # Create machine-local state files
        state_dir = repo_root / ".claude" / "data" / "state"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "heartbeat-state.json"
        state_file.write_text('{"test": "state"}')

        # Check if it's ignored (should be)
        result = subprocess.run(
            ["git", "check-ignore", str(state_file)],
            cwd=repo_root,
            capture_output=True,
        )

        assert result.returncode == 0, (
            f".claude/data/state/heartbeat-state.json should be ignored by .gitignore "
            f"(machine-local state), but it's not. git check-ignore returned {result.returncode}"
        )


def test_share_gitignore_comment_is_clear():
    """Test that the comment in the root .gitignore is present and clear."""
    gitignore_path = Path(__file__).parent.parent / ".gitignore"
    content = gitignore_path.read_text()

    # Check that the comment explains the vault is a template
    assert "template" in content.lower() or "skeleton" in content.lower(), (
        "the root .gitignore should have a comment explaining that vault/ is a template"
    )

    # Check that vault/daily/ and vault/research/ are not ignored
    assert "vault/daily/" not in content, (
        "vault/daily/ should not be in .gitignore (notes should be tracked)"
    )
    assert "vault/research/" not in content, (
        "vault/research/ should not be in .gitignore (research should be tracked)"
    )
