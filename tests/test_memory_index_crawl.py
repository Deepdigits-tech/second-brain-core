"""The crawler indexes what git considers part of the vault: tracked files and
untracked-but-not-ignored files. A folder named `build` is not a reason to skip
the owner's own notes."""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import memory_index  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_vault(tmp_path, monkeypatch):
    """A real git repo with a vault/, a .gitignore, tracked + untracked + ignored notes."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    vault = tmp_path / "vault"
    (vault / "projects" / "X" / "build" / "spikes" / "vendor").mkdir(parents=True)
    (vault / "projects" / "X" / "build" / "ARCHITECTURE.md").write_text("# arch\nown note in a build folder\n")
    (vault / "projects" / "X" / "build" / "spikes" / "vendor" / "README.md").write_text("# vendored\n")
    (vault / "notes.md").write_text("# notes\nplain\n")
    (vault / "untracked.md").write_text("# untracked\nnot yet committed\n")
    (vault / "empty.md").write_text("   \n")
    (vault / "SOUL.md").write_text("# real soul\n")
    (vault / "projects" / "X" / "build" / "core" / "vault").mkdir(parents=True)
    (vault / "projects" / "X" / "build" / "core" / "vault" / "SOUL.md").write_text("# nested soul\n")
    (vault / "projects" / "X" / "build" / "MEMORY.template.md").write_text("# template\n")
    (vault / "projects" / "X" / "build" / "spikes" / "h").mkdir(parents=True)
    (vault / "projects" / "X" / "build" / "spikes" / "h" / "SOUL.md").write_text("# spike soul\n")
    (tmp_path / ".gitignore").write_text("vault/projects/X/build/spikes/vendor/\n")
    _git(
        tmp_path, "add", ".gitignore", "vault/notes.md",
        "vault/projects/X/build/ARCHITECTURE.md", "vault/empty.md", "vault/SOUL.md",
        "vault/projects/X/build/core/vault/SOUL.md",
        "vault/projects/X/build/MEMORY.template.md",
        "vault/projects/X/build/spikes/h/SOUL.md",
    )
    _git(tmp_path, "commit", "-q", "-m", "init")
    monkeypatch.setattr(memory_index, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(memory_index, "VAULT_DIR", vault)
    return tmp_path


def test_git_path_keeps_own_notes_under_build_and_drops_ignored(git_vault):
    files = dict(memory_index.crawl_vault())
    assert "vault/projects/X/build/ARCHITECTURE.md" in files
    assert "vault/untracked.md" in files
    assert "vault/notes.md" in files
    assert "vault/SOUL.md" in files
    assert "vault/projects/X/build/spikes/vendor/README.md" not in files
    assert "vault/empty.md" not in files
    assert "vault/projects/X/build/core/vault/SOUL.md" not in files
    assert "vault/projects/X/build/MEMORY.template.md" not in files
    assert "vault/projects/X/build/spikes/h/SOUL.md" not in files
    stats = memory_index.LAST_CRAWL_STATS
    assert stats["source"] == "git"
    assert stats["indexed"] == 4
    assert stats["skipped_ignored"] >= 1
    assert stats["skipped_empty"] == 1
    assert stats["skipped_nested"] == 3


def test_build_is_no_longer_an_excluded_segment():
    assert "build" not in memory_index.EXCLUDED_DIR_SEGMENTS


def test_walk_fallback_when_not_a_git_repo(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "build").mkdir(parents=True)
    (vault / "build" / "own.md").write_text("# own\n")
    (vault / "node_modules").mkdir()
    (vault / "node_modules" / "x.md").write_text("# vendored\n")
    (vault / "SOUL.md").write_text("# real soul\n")
    (vault / "build" / "core" / "vault").mkdir(parents=True)
    (vault / "build" / "core" / "vault" / "SOUL.md").write_text("# nested soul\n")
    (vault / "build" / "MEMORY.template.md").write_text("# template\n")
    (vault / "build" / "spikes" / "h").mkdir(parents=True)
    (vault / "build" / "spikes" / "h" / "SOUL.md").write_text("# spike soul\n")
    monkeypatch.setattr(memory_index, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(memory_index, "VAULT_DIR", vault)
    files = dict(memory_index.crawl_vault())
    assert "vault/build/own.md" in files
    assert "vault/node_modules/x.md" not in files
    assert "vault/SOUL.md" in files
    assert "vault/build/core/vault/SOUL.md" not in files
    assert "vault/build/MEMORY.template.md" not in files
    assert "vault/build/spikes/h/SOUL.md" not in files
    assert memory_index.LAST_CRAWL_STATS["source"] == "walk"
    assert memory_index.LAST_CRAWL_STATS["skipped_nested"] == 3


def test_excluded_paths_still_apply_on_git_path(git_vault):
    vault = git_vault / "vault"
    (vault / "inbox-review.md").write_text("# auto-rendered\n")
    _git(git_vault, "add", "vault/inbox-review.md")
    files = dict(memory_index.crawl_vault())
    assert "vault/inbox-review.md" not in files
    assert memory_index.LAST_CRAWL_STATS["skipped_excluded"] == 1
