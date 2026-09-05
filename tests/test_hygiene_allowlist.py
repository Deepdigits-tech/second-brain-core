import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import hygiene

def _findings(check, root, monkeypatch):
    monkeypatch.setattr(hygiene, "PROJECT_ROOT", root)
    out = []
    check(out)
    return out

def test_deliberate_gitkeep_dirs_are_not_findings(tmp_path, monkeypatch):
    for d in ("vault/daily", "vault/drafts/active", "vault/drafts/sent", ".claude/data/state"):
        (tmp_path / d).mkdir(parents=True); (tmp_path / d / ".gitkeep").touch()
    assert _findings(hygiene.check_empty_dirs_with_gitkeep, tmp_path, monkeypatch) == []

def test_undeclared_gitkeep_dir_is_a_finding(tmp_path, monkeypatch):
    (tmp_path / "vault" / "mystery").mkdir(parents=True); (tmp_path / "vault" / "mystery" / ".gitkeep").touch()
    out = _findings(hygiene.check_empty_dirs_with_gitkeep, tmp_path, monkeypatch)
    assert [f.path for f in out] == ["vault/mystery"]

def test_worktrees_nested_vaults_and_testruns_are_ignored(tmp_path, monkeypatch):
    for d in (".claude/worktrees/x/vault/daily", "vault/projects/P/build/core/vault/notes", "vault/projects/P/build/t/.testrun/a/audit"):
        (tmp_path / d).mkdir(parents=True); (tmp_path / d / ".gitkeep").touch()
    assert _findings(hygiene.check_empty_dirs_with_gitkeep, tmp_path, monkeypatch) == []

def test_ds_store_under_ignored_tree_marker_is_skipped(tmp_path, monkeypatch):
    ignored_dir = tmp_path / ".claude" / "worktrees" / "x"
    ignored_dir.mkdir(parents=True)
    (ignored_dir / ".DS_Store").touch()
    out = _findings(hygiene.check_ds_store_files, tmp_path, monkeypatch)
    assert out == []

def test_ds_store_outside_ignored_tree_is_still_a_finding(tmp_path, monkeypatch):
    (tmp_path / ".DS_Store").touch()
    out = _findings(hygiene.check_ds_store_files, tmp_path, monkeypatch)
    assert [f.path for f in out] == [".DS_Store"]

def test_root_file_allowlist_unchanged_for_known_files(tmp_path, monkeypatch):
    for n in ("CLAUDE.md", "PHASES.md", "skills-lock.json", "README.md"):
        (tmp_path / n).write_text("x")
    (tmp_path / "stray.jpeg").write_bytes(b"x")
    out = _findings(hygiene.check_files_at_root, tmp_path, monkeypatch)
    assert [f.path for f in out] == ["stray.jpeg"]
