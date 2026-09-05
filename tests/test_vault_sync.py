"""Guards on what the 2-minute auto-sync cron is allowed to commit.

Auto-sync ran `git add -A` and committed everything under a message like
"auto-sync: Mac 2026-08-15T04:52:28Z". On 2026-08-14 that swept a half-written
script into main mid-edit, committed 177 generated files before their ignore
rule existed, and buried a security fix under a message that never mentions it.

Source code now needs an explicit commit. Content and state still sync freely.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = REPO_ROOT / ".claude" / "scripts" / "vault_sync.sh"


def _excluded_paths() -> list[str]:
    """Read the denylist out of the script, so this test tracks the real source."""
    match = re.search(r'^AUTO_SYNC_EXCLUDE="([^"]*)"', SYNC_SCRIPT.read_text(), re.M)
    assert match, "AUTO_SYNC_EXCLUDE not found in vault_sync.sh"
    return match.group(1).split()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "developer@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def _stage_like_autosync(repo: Path, excluded: list[str]) -> set[str]:
    """Reproduce the script's staging sequence: add -A, then reset the denylist."""
    _git(repo, "add", "-A")
    for path in excluded:
        _git(repo, "reset", "-q", "--", path)
    out = _git(repo, "diff", "--cached", "--name-only")
    return {line for line in out.splitlines() if line}


def test_denylist_covers_the_code_directories():
    excluded = _excluded_paths()
    for expected in (".claude/scripts", ".claude/hooks", "tests"):
        assert expected in excluded, f"{expected} must not be auto-committed"


def test_code_changes_are_not_staged(repo):
    excluded = _excluded_paths()
    for path in excluded:
        target = repo / path / "thing.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n")

    staged = _stage_like_autosync(repo, excluded)

    assert staged == set(), f"code was staged for auto-commit: {staged}"


def test_content_and_state_still_sync(repo):
    """The denylist must not block the thing auto-sync exists for."""
    excluded = _excluded_paths()
    for rel in ("vault/daily/2026-08-14.md", ".agents/plans/p.md", ".claude/data/tasks.json"):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("content\n")

    staged = _stage_like_autosync(repo, excluded)

    assert staged == {
        ".agents/plans/p.md",
        ".claude/data/tasks.json",
        "vault/daily/2026-08-14.md",
    }


def test_mixed_change_commits_content_and_leaves_code(repo):
    excluded = _excluded_paths()
    (repo / "vault").mkdir()
    (repo / "vault" / "note.md").write_text("note\n")
    (repo / ".claude" / "scripts").mkdir(parents=True)
    (repo / ".claude" / "scripts" / "half_written.py").write_text("def broken(\n")

    staged = _stage_like_autosync(repo, excluded)

    assert staged == {"vault/note.md"}
    # the code change survives in the working tree — skipped, never discarded
    assert (repo / ".claude" / "scripts" / "half_written.py").exists()
    # -uall, or git collapses the untracked dir to one "?? .claude/" line
    assert "half_written.py" in _git(repo, "status", "--porcelain", "-uall")


def test_skip_count_does_not_under_report_untracked_files(repo):
    """Plain --porcelain collapses an untracked dir to one line; the count must not."""
    scripts = repo / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "a.py").write_text("a\n")
    (scripts / "b.py").write_text("b\n")

    collapsed = _git(repo, "status", "--porcelain", "--", ".claude/scripts").splitlines()
    expanded = _git(repo, "status", "--porcelain", "-uall", "--", ".claude/scripts").splitlines()

    assert len(collapsed) == 1 and len(expanded) == 2, "git behaviour changed"
    assert "-uall" in SYNC_SCRIPT.read_text(), "skip counter must use -uall"


def test_deletions_in_content_still_propagate(repo):
    """`git add -A` semantics must survive: a deleted note has to sync as a deletion."""
    excluded = _excluded_paths()
    note = repo / "vault" / "gone.md"
    note.parent.mkdir()
    note.write_text("bye\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add note")

    note.unlink()
    staged = _stage_like_autosync(repo, excluded)

    assert staged == {"vault/gone.md"}
    assert "D" in _git(repo, "diff", "--cached", "--name-status")


def test_script_logs_when_it_skips_code():
    """A skip that is not reported is the failure mode this whole change is about."""
    source = SYNC_SCRIPT.read_text()
    assert "left $skipped code file(s) uncommitted" in source


# ── Branch safety ────────────────────────────────────────────────────
#
# The sync pulled and pushed `main` no matter which branch was checked out.
# On a feature branch that means rebasing every unmerged commit onto main
# every 2 minutes. On 2026-08-16 that jammed on a conflict, failed 66 times
# in a row, stopped both machines syncing for a day, and stranded in-progress
# work in a stash the script never popped.


_ORIGINS: dict[Path, Path] = {}


@pytest.fixture
def synced_repo(tmp_path):
    """A repo with a real `origin` remote and the sync script installed in it.

    The script derives its repo root from its own location, so it has to live
    inside the fixture repo to be runnable at all.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)

    _git(work, "config", "user.email", "developer@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", str(origin))

    scripts = work / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "vault_sync.sh").write_text(SYNC_SCRIPT.read_text())
    (scripts / "vault_sync.sh").chmod(0o755)
    (work / "vault").mkdir()
    (work / "vault" / "seed.md").write_text("seed\n")

    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "seed")
    _git(work, "push", "-q", "-u", "origin", "main")
    _ORIGINS[work] = origin
    return work


def _advance_origin_main(repo: Path, filename: str, body: str) -> None:
    """Land a commit on origin/main from elsewhere — i.e. what the VPS does.

    Without this the feature branch and main never diverge, a rebase is a
    no-op, and a test asserting 'history was not rewritten' passes against
    the very code it is meant to catch.
    """
    other = repo.parent / f"other-{filename}"
    subprocess.run(["git", "clone", "-q", str(_ORIGINS[repo]), str(other)], check=True)
    _git(other, "config", "user.email", "developer@example.org")
    _git(other, "config", "user.name", "vps")
    (other / filename).write_text(body)
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", f"vps: {filename}")
    _git(other, "push", "-q", "origin", "main")


def _run_sync(repo: Path):
    return subprocess.run(
        ["bash", str(repo / ".claude" / "scripts" / "vault_sync.sh")],
        cwd=repo, capture_output=True, text=True,
    )


def test_sync_still_works_on_main(synced_repo):
    """The normal path must keep working — content commits and pushes."""
    (synced_repo / "vault" / "note.md").write_text("from main\n")
    _run_sync(synced_repo)

    assert "note.md" in _git(synced_repo, "log", "--name-only", "-1")
    assert _git(synced_repo, "rev-list", "--count", "origin/main..HEAD").strip() == "0"


def test_main_still_receives_other_machines_commits(synced_repo):
    """Pulling is the whole point — the fix must not stop content flowing in."""
    _advance_origin_main(synced_repo, "vault-from-vps.md", "vps note\n")
    _run_sync(synced_repo)

    assert (synced_repo / "vault-from-vps.md").exists()


def test_feature_branch_is_not_rebased_onto_main(synced_repo):
    """The bug: unmerged commits replayed onto main on every 2-minute tick."""
    _git(synced_repo, "checkout", "-qb", "feature")
    (synced_repo / "code.py").write_text("x = 1\n")
    _git(synced_repo, "add", "-A")
    _git(synced_repo, "commit", "-qm", "feature work")
    head_before = _git(synced_repo, "rev-parse", "HEAD").strip()

    _advance_origin_main(synced_repo, "vps.md", "moved on\n")
    _run_sync(synced_repo)

    assert _git(synced_repo, "rev-parse", "HEAD").strip() == head_before, \
        "sync rewrote feature-branch history by rebasing it onto main"


def test_feature_branch_work_is_never_stashed(synced_repo):
    """A stash the script fails to pop is how in-progress work went missing."""
    _git(synced_repo, "checkout", "-qb", "feature")
    (synced_repo / "vault" / "wip.md").write_text("half-written\n")
    _advance_origin_main(synced_repo, "vps.md", "moved on\n")

    _run_sync(synced_repo)

    assert _git(synced_repo, "stash", "list").strip() == "", "work was left in a stash"
    assert (synced_repo / "vault" / "wip.md").read_text() == "half-written\n"


def test_feature_branch_does_not_push_to_main(synced_repo):
    """Pushing `main` from a branch pushes a stale ref — or the wrong work."""
    _git(synced_repo, "checkout", "-qb", "feature")
    (synced_repo / "vault" / "note.md").write_text("branch note\n")
    _git(synced_repo, "add", "-A")
    _git(synced_repo, "commit", "-qm", "branch note")

    _advance_origin_main(synced_repo, "vps.md", "moved on\n")
    # Read origin/main after the other machine's push, or "unchanged" would
    # mean "unchanged from a stale local ref" and the assert proves nothing.
    _git(synced_repo, "fetch", "-q", "origin")
    expected = _git(synced_repo, "rev-parse", "origin/main").strip()

    _run_sync(synced_repo)
    _git(synced_repo, "fetch", "-q", "origin")

    assert _git(synced_repo, "rev-parse", "origin/main").strip() == expected


def test_feature_branch_skip_is_not_counted_as_a_failure(synced_repo):
    """Skipping is normal on a branch — it must not trip the alert counter."""
    _git(synced_repo, "checkout", "-qb", "feature")
    _advance_origin_main(synced_repo, "vps.md", "moved on\n")

    result = _run_sync(synced_repo)

    assert result.returncode == 0
    fail_state = synced_repo / ".claude" / "data" / "state" / "sync-failures.txt"
    assert not fail_state.exists() or fail_state.read_text().strip() in ("", "0")


def test_feature_branch_skip_is_logged():
    """A sync that silently does nothing is indistinguishable from a broken one."""
    source = SYNC_SCRIPT.read_text()
    assert "not on main" in source


def test_alert_paths_load_credentials_before_notifying():
    """An alert that cannot reach Owner's phone is not an alert.

    `integrations/telegram_bot.py` only calls load_env() under __main__, so an
    importing caller gets no notification credentials and notify() returns
    telegram=False while still reporting macos=True. The sync's failure alert
    hit exactly that: 66 consecutive failures raised a desktop popup on a Mac
    nobody was sitting at, and never sent a message.
    """
    source = SYNC_SCRIPT.read_text()
    heredocs = source.count("from notify import notify")
    assert heredocs > 0, "no notification path found in the sync script"
    assert source.count("load_env()") >= heredocs, \
        "every notify path must load credentials first, or it fails silently"
