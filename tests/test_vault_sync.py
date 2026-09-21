"""Guards on the 2-minute auto-sync cron: what it commits, and how it behaves
around locking, conflicts, and git operations it does not own.

Auto-sync ran `git add -A` and committed everything under a message like
"auto-sync: Mac 2026-08-15T04:52:28Z". On 2026-08-14 that swept a half-written
script into main mid-edit, committed 177 generated files before their ignore
rule existed, and buried a security fix under a message that never mentions it.
Source code now needs an explicit commit. Content and state still sync freely.

2026-09-20: the denylist became an allowlist (a forgotten new directory used
to auto-commit silently; now it is left for a human by construction), a
stash-pop conflict is resolved instead of discarded (`git checkout -- .` lost
240 lines of the owner's notes across 17 stashes), a single-run lock was
added, and the script now distinguishes a human-owned git operation
(merge/cherry-pick/revert/rebase) from one it started itself.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = REPO_ROOT / ".claude" / "scripts" / "vault_sync.sh"

# Isolation: these tests must never read or write the owner's real git config
# (global gitconfig, credential helpers, etc.) — every git subprocess here
# gets a null global/system config and its own throwaway user.name/email.
_ISOLATED_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is not available",
)


def _allowed_paths() -> list[str]:
    """Read the allowlist out of the script, so this test tracks the real source."""
    match = re.search(r'^AUTO_SYNC_ALLOW="([^"]*)"', SYNC_SCRIPT.read_text(), re.M)
    assert match, "AUTO_SYNC_ALLOW not found in vault_sync.sh"
    return match.group(1).split()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False, env=_ISOLATED_ENV,
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


_SYNC_STATE_GITIGNORE = "\n".join([
    ".claude/data/state/*.json",
    ".claude/data/state/sync-failures.txt",
    ".claude/data/state/sync-held-commit.txt",
    ".claude/data/state/sync-paused-branch.txt",
    ".claude/data/state/sync.lock/",
    ".claude/data/state/sync-started-rebase.txt",
    ".claude/data/state/sync-human-op-notified.txt",
    ".claude/data/state/sync-conflict-notified.txt",
    ".claude/data/state/sync-conflicts/",
    "",
])


def _write_sync_gitignore(repo: Path) -> None:
    """Mirror the real repo's .gitignore rules for the sync's own state, so a
    fixture repo exercises the same never-auto-committed behaviour as the
    real deployment (the mechanism relies on .gitignore, not on script logic
    alone)."""
    (repo / ".gitignore").write_text(_SYNC_STATE_GITIGNORE)


def _stage_like_autosync(repo: Path, allowed: list[str]) -> set[str]:
    """Reproduce the script's staging sequence: add -A, then keep only allowed paths."""
    _git(repo, "add", "-A")
    staged_all = [l for l in _git(repo, "diff", "--cached", "--name-only").splitlines() if l]
    for path in staged_all:
        if not any(path == prefix or path.startswith(prefix + "/") for prefix in allowed):
            _git(repo, "reset", "-q", "--", path)
    out = _git(repo, "diff", "--cached", "--name-only")
    return {line for line in out.splitlines() if line}


# ── Allowlist (2026-09-20) ────────────────────────────────────────────


def test_allowlist_covers_vault_state_and_claude_md():
    allowed = _allowed_paths()
    for expected in ("vault", "CLAUDE.md", ".claude/data"):
        assert expected in allowed, f"{expected} should be auto-synced content"


def test_code_and_config_paths_are_not_staged(repo):
    allowed = _allowed_paths()
    non_allowed_files = [
        ".claude/scripts/thing.py",
        ".claude/hooks/thing.py",
        ".claude/chat/tools.py",
        ".claude/skills/foo/SKILL.md",
        ".claude/commands/foo.md",
        ".claude/launchd/com.secondbrain.sync.plist",
        ".claude/systemd/secondbrain-sync.service",
        ".claude/settings.json",
        "tests/test_thing.py",
        "docs/notes.md",
        "PHASES.md",
        ".gitignore",
        ".gitattributes",
    ]
    for rel in non_allowed_files:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")

    staged = _stage_like_autosync(repo, allowed)

    assert staged == set(), f"non-allowlisted paths were staged: {staged}"


def test_content_and_state_still_sync(repo):
    """The allowlist must not block the thing auto-sync exists for."""
    allowed = _allowed_paths()
    for rel in (
        "vault/daily/2026-08-14.md",
        ".claude/data/tasks.json",
        "CLAUDE.md",
    ):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("content\n")

    staged = _stage_like_autosync(repo, allowed)

    assert staged == {
        "vault/daily/2026-08-14.md",
        ".claude/data/tasks.json",
        "CLAUDE.md",
    }


def test_mixed_change_commits_content_and_leaves_code(repo):
    allowed = _allowed_paths()
    (repo / "vault").mkdir()
    (repo / "vault" / "note.md").write_text("note\n")
    (repo / ".claude" / "scripts").mkdir(parents=True)
    (repo / ".claude" / "scripts" / "half_written.py").write_text("def broken(\n")

    staged = _stage_like_autosync(repo, allowed)

    assert staged == {"vault/note.md"}
    # the code change survives in the working tree — skipped, never discarded
    assert (repo / ".claude" / "scripts" / "half_written.py").exists()
    assert "half_written.py" in _git(repo, "status", "--porcelain", "-uall")


def test_skip_list_names_every_file_individually(repo):
    """The skip log is built from `git diff --cached --name-only`, which lists
    each file — unlike plain `git status --porcelain`, which collapses a
    wholly-untracked directory to one line and would under-report the count."""
    scripts = repo / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "a.py").write_text("a\n")
    (scripts / "b.py").write_text("b\n")

    _git(repo, "add", "-A")
    listed = [l for l in _git(repo, "diff", "--cached", "--name-only").splitlines() if l]
    assert len(listed) == 2


def test_deletions_in_content_still_propagate(repo):
    """`git add -A` semantics must survive: a deleted note has to sync as a deletion."""
    allowed = _allowed_paths()
    note = repo / "vault" / "gone.md"
    note.parent.mkdir()
    note.write_text("bye\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add note")

    note.unlink()
    staged = _stage_like_autosync(repo, allowed)

    assert staged == {"vault/gone.md"}
    assert "D" in _git(repo, "diff", "--cached", "--name-status")


def test_script_logs_when_it_skips_non_vault_files():
    source = SYNC_SCRIPT.read_text()
    assert "uncommitted (commit these yourself with a real message)" in source


def test_send_notification_loads_credentials_and_backs_every_alert():
    """Every alert path funnels through one send_notification() that loads
    credentials first — `integrations/telegram_bot.py` only calls load_env()
    under __main__, so an importing caller gets no credentials and notify()
    silently returns telegram=False. 66 consecutive sync failures once raised
    a desktop popup on a Mac nobody was sitting at, and never sent a message.
    """
    source = SYNC_SCRIPT.read_text()
    assert source.count("from notify import notify") == 1
    assert source.count("load_env()") >= 1
    # notify_sync_failure, notify_branch_pause, notify_push_held,
    # notify_human_op_pause, notify_conflict_saved
    assert source.count('send_notification "') >= 5


# ── Branch safety ────────────────────────────────────────────────────
#
# The sync pulled and pushed `main` no matter which branch was checked out.
# On a feature branch that means rebasing every unmerged commit onto main
# every 2 minutes. On 2026-08-16 that jammed on a conflict, failed 66 times
# in a row, stopped both machines syncing for a day, and stranded in-progress
# work in a stash the script never popped.


_ORIGINS: dict[Path, Path] = {}


def _notify_stub(repo: Path) -> Path:
    """A stub standing in for the real Telegram send, so tests never notify
    for real. vault_sync.sh calls `"$CORTEX_SYNC_NOTIFY_CMD" "$title" "$message"`
    when the env var is set (see send_notification())."""
    stub = repo.parent / f"notify-stub-{repo.name}.sh"
    log = _notify_log(repo)
    if not stub.exists():
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\t%s\\n" "$1" "$2" >> "{log}"\n'
        )
        stub.chmod(0o755)
    return stub


def _notify_log(repo: Path) -> Path:
    return repo.parent / f"notify-{repo.name}.log"


def _notify_lines(repo: Path) -> list[str]:
    log = _notify_log(repo)
    if not log.exists():
        return []
    return [l for l in log.read_text().splitlines() if l]


@pytest.fixture
def synced_repo(tmp_path):
    """A repo with a real `origin` remote and the sync script installed in it.

    The script derives its repo root from its own location, so it has to live
    inside the fixture repo to be runnable at all.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=_ISOLATED_ENV)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, env=_ISOLATED_ENV)

    _git(work, "config", "user.email", "developer@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", str(origin))

    scripts = work / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "vault_sync.sh").write_text(SYNC_SCRIPT.read_text())
    (scripts / "vault_sync.sh").chmod(0o755)
    (work / "vault" / "daily").mkdir(parents=True)
    (work / "vault" / "daily" / "seed.md").write_text("seed\nlast\n")
    _write_sync_gitignore(work)

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
    subprocess.run(["git", "clone", "-q", str(_ORIGINS[repo]), str(other)], check=True, env=_ISOLATED_ENV)
    _git(other, "config", "user.email", "developer@example.org")
    _git(other, "config", "user.name", "vps")
    (other / filename).write_text(body)
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", f"vps: {filename}")
    _git(other, "push", "-q", "origin", "main")


def _run_sync(repo: Path, overrides: dict | None = None):
    run_environ = dict(_ISOLATED_ENV)
    run_environ["CORTEX_SYNC_NOTIFY_CMD"] = str(_notify_stub(repo))
    if overrides:
        for k, v in overrides.items():
            run_environ[k] = v
    return subprocess.run(
        ["bash", str(repo / ".claude" / "scripts" / "vault_sync.sh")],
        cwd=repo, capture_output=True, text=True, env=run_environ,
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


# ── The sync pushes only its own commits (2026-09-18) ────────────────


def _hand_commit(repo: Path, name: str = "tool.py") -> None:
    p = repo / ".claude" / "scripts" / name
    p.write_text("print('hi')\n")
    _git(repo, "add", str(p))
    _git(repo, "commit", "-qm", "scripts: a change made by hand")


def _origin_subjects(repo: Path) -> str:
    return subprocess.run(["git", "--git-dir", str(_ORIGINS[repo]), "log", "--format=%s", "main"],
                          capture_output=True, text=True, check=True, env=_ISOLATED_ENV).stdout


def _held_file(repo: Path) -> Path:
    return repo / ".claude" / "data" / "state" / "sync-held-commit.txt"


def _owner_pushes(repo: Path) -> None:
    _git(repo, "push", "-q", "origin", "HEAD")


def test_a_hand_made_commit_is_not_pushed(synced_repo):
    _hand_commit(synced_repo)
    result = _run_sync(synced_repo)
    assert result.returncode == 0
    assert "a change made by hand" not in _origin_subjects(synced_repo)
    assert _held_file(synced_repo).exists()


def test_vault_content_is_still_saved_locally_while_held(synced_repo):
    _hand_commit(synced_repo)
    (synced_repo / "vault" / "note.md").write_text("written while held\n")
    _run_sync(synced_repo)
    assert "auto-sync:" in _git(synced_repo, "log", "--format=%s", "-1")
    assert "auto-sync:" not in _origin_subjects(synced_repo)   # it sits behind the held commit


def test_other_machines_commits_still_arrive_while_held(synced_repo):
    _hand_commit(synced_repo)
    _advance_origin_main(synced_repo, "vault-from-vps.md", "vps note\n")
    _run_sync(synced_repo)
    assert (synced_repo / "vault-from-vps.md").exists()
    assert "a change made by hand" not in _origin_subjects(synced_repo)


def test_a_hold_is_not_a_failure_and_is_announced_once(synced_repo):
    _hand_commit(synced_repo)
    _run_sync(synced_repo)
    first = _held_file(synced_repo).read_text()
    # origin moves, the held commit is rebased and gets a new hash — same hold.
    _advance_origin_main(synced_repo, "more.md", "x\n")
    _run_sync(synced_repo)
    assert _held_file(synced_repo).read_text() == first
    assert not (synced_repo / ".claude" / "data" / "state" / "sync-failures.txt").exists()


def test_sync_resumes_once_the_owner_pushes(synced_repo):
    _hand_commit(synced_repo)
    (synced_repo / "vault" / "note.md").write_text("queued\n")
    _run_sync(synced_repo)
    _owner_pushes(synced_repo)
    (synced_repo / "vault" / "later.md").write_text("after\n")
    _run_sync(synced_repo)
    assert _git(synced_repo, "rev-list", "--count", "origin/main..HEAD").strip() == "0"
    assert not _held_file(synced_repo).exists()


# ── Single-run lock (2026-09-20) ──────────────────────────────────────


def _lock_dir(repo: Path) -> Path:
    return repo / ".claude" / "data" / "state" / "sync.lock"


def test_concurrent_run_exits_immediately_without_touching_git(synced_repo):
    (synced_repo / "vault" / "note.md").write_text("should not sync\n")
    head_before = _git(synced_repo, "rev-parse", "HEAD").strip()

    lock = _lock_dir(synced_repo)
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))  # this test process: definitely alive

    result = _run_sync(synced_repo)

    assert result.returncode == 0
    assert _git(synced_repo, "rev-parse", "HEAD").strip() == head_before, "sync ran despite a held lock"
    assert lock.exists(), "a run that never acquired the lock must not remove it"


def test_stale_lock_with_dead_pid_is_replaced(synced_repo):
    (synced_repo / "vault" / "note.md").write_text("should sync\n")

    lock = _lock_dir(synced_repo)
    lock.mkdir(parents=True)
    (lock / "pid").write_text("999999")  # not a real pid on any normal test host

    result = _run_sync(synced_repo)

    assert result.returncode == 0
    assert "note.md" in _git(synced_repo, "log", "--name-only", "-1")
    assert not lock.exists(), "the lock must be released after a successful run"


def test_stale_lock_by_age_is_replaced(synced_repo):
    (synced_repo / "vault" / "note.md").write_text("should sync too\n")

    lock = _lock_dir(synced_repo)
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    old = time.time() - 700  # > LOCK_STALE_SECONDS (600)
    os.utime(lock, (old, old))

    result = _run_sync(synced_repo)

    assert result.returncode == 0
    assert "note.md" in _git(synced_repo, "log", "--name-only", "-1")


# ── Lock race: the empty-pid window (2026-09-21 fix) ──────────────────
#
# `mkdir` and `echo $$ > pid` are two steps. A second run landing in that
# window used to read an empty pid file, decide the lock was dead, steal it,
# and run concurrently with the first — whichever exited first then removed
# the other's lock via its exit trap. CORTEX_SYNC_LOCK_TEST_HOLD_SECONDS
# (test-only) sleeps inside that exact window so the race is reproducible.


def test_lock_race_second_run_does_not_steal_the_empty_pid_window(synced_repo):
    (synced_repo / "vault" / "note.md").write_text("from the real owner\n")
    env = dict(_ISOLATED_ENV)
    env["CORTEX_SYNC_NOTIFY_CMD"] = str(_notify_stub(synced_repo))
    env["CORTEX_SYNC_LOCK_TEST_HOLD_SECONDS"] = "2"
    p1 = subprocess.Popen(
        ["bash", str(synced_repo / ".claude" / "scripts" / "vault_sync.sh")],
        cwd=synced_repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        time.sleep(0.5)  # p1 has mkdir'd the lock and is now inside its artificial hold
        lock = _lock_dir(synced_repo)
        assert lock.exists(), "test setup: p1 should already hold the lock"

        result2 = _run_sync(synced_repo)  # p2: no delay, races the empty-pid window

        assert result2.returncode == 0
        assert lock.exists(), "a second run stole or removed the still-held lock"
        assert _git(synced_repo, "rev-list", "--count", "HEAD").strip() == "1", \
            "a second run must not have done any git work while the lock was held"
    finally:
        p1.wait(timeout=15)

    assert not lock.exists(), "the lock must be released once the true owner finishes"
    assert "note.md" in _git(synced_repo, "log", "--name-only", "-1")


def test_release_lock_only_removes_its_own_pid(synced_repo):
    """Defense in depth for the same race: even if some future bug lets a
    second run acquire while the first is still active, the first run's exit
    trap must not blow away a lock that no longer names its own pid."""
    source = SYNC_SCRIPT.read_text()
    assert "release_lock" in source
    assert "trap release_lock EXIT" in source
    fn = source.split("release_lock() {", 1)[1].split("\n}", 1)[0]
    assert '"$(cat "$LOCK_PID_FILE"' in fn and "$$" in fn, \
        "release_lock must compare the lock's recorded pid to its own before removing it"


# ── Human-owned git operations are never touched (2026-09-20) ────────


def test_human_merge_conflict_is_left_untouched_and_notified_once(synced_repo):
    _git(synced_repo, "checkout", "-qb", "other")
    (synced_repo / "vault" / "daily" / "seed.md").write_text("seed\nother branch\n")
    _git(synced_repo, "add", "-A")
    _git(synced_repo, "commit", "-qm", "other branch edit")
    _git(synced_repo, "checkout", "-q", "main")
    (synced_repo / "vault" / "daily" / "seed.md").write_text("seed\nmain branch\n")
    _git(synced_repo, "add", "-A")
    _git(synced_repo, "commit", "-qm", "main edit")

    subprocess.run(["git", "merge", "other"], cwd=synced_repo, capture_output=True, text=True, env=_ISOLATED_ENV)
    assert (synced_repo / ".git" / "MERGE_HEAD").exists(), "test setup did not produce a real conflict"

    result = _run_sync(synced_repo)

    assert result.returncode == 0
    assert (synced_repo / ".git" / "MERGE_HEAD").exists(), "sync touched a human-owned merge"
    assert "<<<<<<<" in (synced_repo / "vault" / "daily" / "seed.md").read_text()
    assert len(_notify_lines(synced_repo)) == 1

    # a second tick while still unresolved must not notify again
    _run_sync(synced_repo)
    assert len(_notify_lines(synced_repo)) == 1


def test_stale_rebase_marker_does_not_hijack_a_human_merge_conflict(synced_repo):
    """The bug: a marker left by a run killed between `git pull --rebase`
    returning and the `rm` that clears it used to be matched against ANY of
    rebase-merge/rebase-apply/MERGE_HEAD/CHERRY_PICK_HEAD/REVERT_HEAD just
    because it existed — not because it actually described the operation in
    progress. A later, unrelated, human-started merge conflict got read as
    "ours" and `git rebase --abort` + `git merge --abort` ran, destroying the
    human's in-progress resolution, with no notification. This script never
    starts a merge at all, so MERGE_HEAD must never consult the marker."""
    state = synced_repo / ".claude" / "data" / "state" / "sync-started-rebase.txt"
    state.parent.mkdir(parents=True, exist_ok=True)
    # A plausible-looking leftover — some real sha, just not one describing
    # the merge below (this script never rebases onto a merge in progress).
    state.write_text(_git(synced_repo, "rev-parse", "HEAD").strip() + "\n")

    _git(synced_repo, "checkout", "-qb", "other")
    (synced_repo / "vault" / "daily" / "seed.md").write_text("seed\nother branch\n")
    _git(synced_repo, "add", "-A")
    _git(synced_repo, "commit", "-qm", "other branch edit")
    _git(synced_repo, "checkout", "-q", "main")
    (synced_repo / "vault" / "daily" / "seed.md").write_text("seed\nmain branch\n")
    _git(synced_repo, "add", "-A")
    _git(synced_repo, "commit", "-qm", "main edit")

    subprocess.run(["git", "merge", "other"], cwd=synced_repo, capture_output=True, text=True, env=_ISOLATED_ENV)
    assert (synced_repo / ".git" / "MERGE_HEAD").exists(), "test setup did not produce a real conflict"

    result = _run_sync(synced_repo)

    assert result.returncode == 0
    assert (synced_repo / ".git" / "MERGE_HEAD").exists(), \
        "sync aborted a human merge because a stale rebase marker happened to exist"
    assert "<<<<<<<" in (synced_repo / "vault" / "daily" / "seed.md").read_text()
    assert len(_notify_lines(synced_repo)) == 1
    assert not state.exists(), "the stale marker must be cleared, not left to cause the same misread again"


# ── Detached-HEAD self-recovery (2026-09-21) ──────────────────────────
#
# A rebase leaves HEAD detached. Before this fix, this script's OWN
# `git pull --rebase` being killed mid-conflict left HEAD detached forever:
# the NEXT tick's branch check saw CURRENT_BRANCH="HEAD" != "main" and took
# the "not on main" branch-pause path — which never even looks at the rebase
# marker — so the machine stopped syncing for good and the one notice
# claimed the owner had checked out a branch called "HEAD", which he never
# did. The fix recovers a marker-matched rebase BEFORE the branch check runs.


def test_detached_head_self_recovery_when_marker_matches(synced_repo):
    """Simulates the script's own interrupted `git pull --rebase`: HEAD
    detached, mid-conflict, with the marker it writes (the pre-rebase HEAD
    sha) still present. The next run must recover onto main and go through
    the NORMAL pull-rebase path — not the branch-pause path, which used to
    trap it forever behind one misleading "checked out 'HEAD'" notice.

    The underlying local/remote conflict is still real and unresolved by
    construction, so the retried `git pull --rebase` legitimately conflicts
    again and this run still reports failure — but as an ordinary, escalating
    `record_failure`, never again as a silent, permanent branch pause."""
    (synced_repo / "vault" / "daily" / "seed.md").write_text("seed\nlast\nlocal side\n")
    _git(synced_repo, "add", "-A")
    _git(synced_repo, "commit", "-qm", "local edit")
    local_head = _git(synced_repo, "rev-parse", "HEAD").strip()

    _advance_origin_main(synced_repo, "vault/daily/seed.md", "seed\nlast\nremote side\n")

    # Reproduce exactly what the script's own `git pull --rebase origin main` does.
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=synced_repo, env=_ISOLATED_ENV)
    subprocess.run(["git", "rebase", "origin/main"], cwd=synced_repo,
                    capture_output=True, text=True, env=_ISOLATED_ENV)
    rebase_dir = synced_repo / ".git" / "rebase-merge"
    if not rebase_dir.exists():
        rebase_dir = synced_repo / ".git" / "rebase-apply"
    assert rebase_dir.exists(), "test setup did not produce a real conflicted rebase"
    assert _git(synced_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"

    state = synced_repo / ".claude" / "data" / "state" / "sync-started-rebase.txt"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(local_head + "\n")

    result = _run_sync(synced_repo)

    # The underlying conflict is real and persists, so this tick still fails —
    # that failure must come from the normal pull-rebase path, not a stuck
    # branch pause: HEAD is back on main and the rebase dir is gone either way.
    assert _git(synced_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert not (synced_repo / ".git" / "rebase-merge").exists()
    assert not (synced_repo / ".git" / "rebase-apply").exists()
    assert not (synced_repo / ".claude" / "data" / "state" / "sync-paused-branch.txt").exists(), \
        "must never be mistaken for (or recorded as) a branch pause"
    assert len(_notify_lines(synced_repo)) == 0, \
        "one real conflict must not yet cross the 5-consecutive-failures notify threshold"
    fail_state = synced_repo / ".claude" / "data" / "state" / "sync-failures.txt"
    assert fail_state.exists() and fail_state.read_text().strip() == "1", \
        "the retried failure must be counted by the ordinary failure path"

    # Once the underlying conflict is actually resolved (say, the owner
    # reverts the remote edit), the very next tick must succeed cleanly —
    # proving the machine is not permanently stuck the way it used to be.
    _git(synced_repo, "config", "user.email", "developer@example.com")
    _git(synced_repo, "config", "user.name", "t")
    other = synced_repo.parent / "resolver"
    subprocess.run(["git", "clone", "-q", str(_ORIGINS[synced_repo]), str(other)],
                    check=True, env=_ISOLATED_ENV)
    _git(other, "config", "user.email", "developer@example.org")
    _git(other, "config", "user.name", "resolver")
    (other / "vault" / "daily" / "seed.md").write_text("seed\nlast\nlocal side\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "converge on the local edit")
    _git(other, "push", "-q", "origin", "main")

    result2 = _run_sync(synced_repo)
    assert result2.returncode == 0, result2.stderr
    assert not fail_state.exists() or fail_state.read_text().strip() in ("", "0")


def test_detached_head_human_rebase_with_no_marker_is_left_untouched(synced_repo):
    (synced_repo / "vault" / "daily" / "seed.md").write_text("seed\nlast\nlocal side\n")
    _git(synced_repo, "add", "-A")
    _git(synced_repo, "commit", "-qm", "local edit")

    _advance_origin_main(synced_repo, "vault/daily/seed.md", "seed\nlast\nremote side\n")

    subprocess.run(["git", "fetch", "-q", "origin"], cwd=synced_repo, env=_ISOLATED_ENV)
    subprocess.run(["git", "rebase", "origin/main"], cwd=synced_repo,
                    capture_output=True, text=True, env=_ISOLATED_ENV)
    rebase_dir = synced_repo / ".git" / "rebase-merge"
    if not rebase_dir.exists():
        rebase_dir = synced_repo / ".git" / "rebase-apply"
    assert rebase_dir.exists()

    result = _run_sync(synced_repo)  # no marker at all — a human started this

    assert result.returncode == 0
    assert rebase_dir.exists(), "a human-owned rebase must never be aborted"
    assert _git(synced_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert len(_notify_lines(synced_repo)) == 1, "the branch-pause notice must still fire once"


def test_detached_head_human_rebase_with_mismatched_marker_is_left_untouched(synced_repo):
    (synced_repo / "vault" / "daily" / "seed.md").write_text("seed\nlast\nlocal side\n")
    _git(synced_repo, "add", "-A")
    _git(synced_repo, "commit", "-qm", "local edit")

    _advance_origin_main(synced_repo, "vault/daily/seed.md", "seed\nlast\nremote side\n")

    subprocess.run(["git", "fetch", "-q", "origin"], cwd=synced_repo, env=_ISOLATED_ENV)
    subprocess.run(["git", "rebase", "origin/main"], cwd=synced_repo,
                    capture_output=True, text=True, env=_ISOLATED_ENV)
    rebase_dir = synced_repo / ".git" / "rebase-merge"
    if not rebase_dir.exists():
        rebase_dir = synced_repo / ".git" / "rebase-apply"
    assert rebase_dir.exists()

    state = synced_repo / ".claude" / "data" / "state" / "sync-started-rebase.txt"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("0000000000000000000000000000000000000000\n")  # a leftover that does not match

    result = _run_sync(synced_repo)

    assert result.returncode == 0
    assert rebase_dir.exists(), "a mismatched marker must never authorise an abort"
    assert len(_notify_lines(synced_repo)) == 1


# ── Stash-pop conflicts are resolved, never discarded (2026-09-20) ───


def _uncommitted_edit(repo: Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def _stash_shas(repo: Path) -> list[str]:
    """Top of stack first, matching `git stash list` order."""
    return [l for l in _git(repo, "stash", "list", "--format=%H").splitlines() if l]


def _seed_foreign_stash(repo: Path, label: str, rel: str, content: str) -> str:
    """A stash standing in for one of the owner's own 63 pre-existing
    stashes, unrelated to whatever this run's own sync stashes."""
    _uncommitted_edit(repo, rel, content)
    _git(repo, "add", "-A")
    _git(repo, "stash", "push", "-m", label)
    return _git(repo, "rev-parse", "stash@{0}").strip()


def _seed_two_foreign_stashes(repo: Path) -> tuple[str, str]:
    """Returns (older, newer) shas. After seeding, `git stash list` order is
    [newer, older] — most recently pushed on top."""
    older = _seed_foreign_stash(repo, "foreign-1", "vault/foreign1.md", "foreign one\n")
    newer = _seed_foreign_stash(repo, "foreign-2", "vault/foreign2.md", "foreign two\n")
    return older, newer


def _merge_file_blocking_path(tmp_dir: Path) -> str:
    """A directory with a `git` shim that fails ONLY `git merge-file`, so a
    union-merge step in resolve_stash_conflicts can be forced to fail
    deterministically without touching real git internals. Every other git
    subcommand is passed straight through to the real binary."""
    real_git = shutil.which("git")
    assert real_git, "git must be on PATH to build the test shim"
    wrapper_dir = tmp_dir / "fake-git-bin"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "merge-file" ]; then exit 1; fi\n'
        f'exec "{real_git}" "$@"\n'
    )
    wrapper.chmod(0o755)
    return str(wrapper_dir)


@pytest.fixture
def two_clones(tmp_path):
    """Two independent working clones of the same origin — one plays the Mac,
    the other the VPS, the way the real deployment runs the same script on
    both machines against the same GitHub repo."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=_ISOLATED_ENV)

    mac = tmp_path / "mac"
    subprocess.run(["git", "init", "-q", "-b", "main", str(mac)], check=True, env=_ISOLATED_ENV)
    _git(mac, "config", "user.email", "mac@example.com")
    _git(mac, "config", "user.name", "mac")
    _git(mac, "remote", "add", "origin", str(origin))
    scripts = mac / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "vault_sync.sh").write_text(SYNC_SCRIPT.read_text())
    (scripts / "vault_sync.sh").chmod(0o755)
    (mac / "vault" / "daily").mkdir(parents=True)
    (mac / "vault" / "daily" / "log.md").write_text("seed\nlast\n")
    (mac / ".claude" / "data").mkdir(parents=True)
    (mac / ".claude" / "data" / "store.json").write_text("remote-unset\n")
    _write_sync_gitignore(mac)
    _git(mac, "add", "-A")
    _git(mac, "commit", "-qm", "seed")
    _git(mac, "push", "-q", "-u", "origin", "main")
    _ORIGINS[mac] = origin

    vps = tmp_path / "vps"
    subprocess.run(["git", "clone", "-q", str(origin), str(vps)], check=True, env=_ISOLATED_ENV)
    _git(vps, "config", "user.email", "vps@example.com")
    _git(vps, "config", "user.name", "vps")
    _ORIGINS[vps] = origin

    return mac, vps


def test_both_machines_appending_to_the_same_daily_log_keeps_both_lines(two_clones):
    mac, vps = two_clones

    _uncommitted_edit(mac, "vault/daily/log.md", "seed\nmac says hi\n")
    _run_sync(mac)  # mac commits and pushes its line

    _uncommitted_edit(vps, "vault/daily/log.md", "seed\nvps says hi\n")
    result = _run_sync(vps)  # vps pulls mac's commit, stashes, conflicts on pop, must union-merge

    assert result.returncode == 0, result.stderr
    merged = (vps / "vault" / "daily" / "log.md").read_text()
    assert "mac says hi" in merged
    assert "vps says hi" in merged
    assert _git(vps, "stash", "list").strip() == "", "stash must be dropped once the conflict is resolved"

    # and a second sync tick brings the merged result back to the mac clone
    _run_sync(mac)
    assert "vps says hi" in (mac / "vault" / "daily" / "log.md").read_text()


def test_json_store_conflict_keeps_remote_and_saves_local_aside(two_clones):
    mac, vps = two_clones

    _uncommitted_edit(mac, ".claude/data/store.json", "remote value\n")
    _run_sync(mac)

    _uncommitted_edit(vps, ".claude/data/store.json", "local value\n")
    result = _run_sync(vps)

    assert result.returncode == 0, result.stderr
    assert (vps / ".claude" / "data" / "store.json").read_text() == "remote value\n", \
        "the pulled (remote) version must stay in the working tree"
    assert _git(vps, "stash", "list").strip() == ""

    conflicts_root = vps / ".claude" / "data" / "state" / "sync-conflicts"
    assert conflicts_root.exists()
    saved = list(conflicts_root.rglob("store.json"))
    assert len(saved) == 1, "the local (stashed) version must be saved aside, not lost"
    assert saved[0].read_text() == "local value\n"

    # never inside the vault or otherwise auto-committed/synced
    assert "vault" not in str(saved[0].relative_to(conflicts_root))
    log_after = _git(vps, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
    assert "sync-conflicts" not in log_after

    assert len(_notify_lines(vps)) == 1, "exactly one notification for this conflict"


# ── OUR_STASH_SHA / top_stash_is_ours (2026-09-21 fix) ────────────────
#
# The owner's live repo holds 63 old stashes of his own. The script used to
# `git stash drop stash@{0}` (or record it as retained) whenever ANY stash
# existed on top of the stack after resolution — but if the phase-0 retry
# pop had already auto-dropped THIS run's own stash (nothing else was in
# it), stash@{0} was by then one of the owner's, and would have been
# deleted or misreported as the retained one. Now every drop/record goes
# through top_stash_is_ours(), gated on the sha captured right after this
# run's own `git stash push`.


def test_foreign_stashes_survive_an_untracked_union_collision(two_clones):
    """(a) The retry pop auto-drops our own stash (nothing else was in it) —
    the exact case that used to reach for "the top of the stack" and delete
    whichever of the owner's stashes happened to be sitting there."""
    mac, vps = two_clones
    foreign_older, foreign_newer = _seed_two_foreign_stashes(vps)

    _uncommitted_edit(mac, "vault/daily/2026-02-02.md", "mac's note\n")
    _run_sync(mac)

    _uncommitted_edit(vps, "vault/daily/2026-02-02.md", "vps's note\n")
    result = _run_sync(vps)

    assert result.returncode == 0, result.stderr
    merged = (vps / "vault" / "daily" / "2026-02-02.md").read_text()
    assert "mac's note" in merged and "vps's note" in merged

    remaining = _stash_shas(vps)
    assert remaining == [foreign_newer, foreign_older], \
        "the owner's pre-existing stashes must survive, unchanged, in order"


def test_foreign_stashes_survive_an_ordinary_tracked_conflict(two_clones):
    """(b) An ordinary tracked (non-collision) conflict — our own stash is
    explicitly `git stash drop`ped at the end of resolve_stash_conflicts."""
    mac, vps = two_clones
    foreign_older, foreign_newer = _seed_two_foreign_stashes(vps)

    _uncommitted_edit(mac, "vault/daily/log.md", "seed\nlast\nmac says hi\n")
    _run_sync(mac)

    _uncommitted_edit(vps, "vault/daily/log.md", "seed\nlast\nvps says hi\n")
    result = _run_sync(vps)

    assert result.returncode == 0, result.stderr
    merged = (vps / "vault" / "daily" / "log.md").read_text()
    assert "mac says hi" in merged and "vps says hi" in merged

    remaining = _stash_shas(vps)
    assert remaining == [foreign_newer, foreign_older]


def test_foreign_stashes_survive_a_plain_no_conflict_cycle(two_clones):
    """(c) A completely ordinary tick: our own stash pop succeeds with no
    conflict at all (git's own pop already drops it — nothing extra for the
    script to do), and the owner's stashes are never even touched."""
    mac, vps = two_clones
    foreign_older, foreign_newer = _seed_two_foreign_stashes(vps)

    _uncommitted_edit(mac, "vault/daily/other.md", "from mac\n")
    _run_sync(mac)

    _uncommitted_edit(vps, "vault/daily/vps-note.md", "from vps, no conflict\n")
    result = _run_sync(vps)

    assert result.returncode == 0, result.stderr
    assert (vps / "vault" / "daily" / "vps-note.md").read_text() == "from vps, no conflict\n"
    assert (vps / "vault" / "daily" / "other.md").exists()

    remaining = _stash_shas(vps)
    assert remaining == [foreign_newer, foreign_older]


def test_retained_stash_records_our_sha_never_a_foreign_one(two_clones, tmp_path):
    """A run that cannot resolve its own conflict must record ITS OWN stash's
    sha — never a pre-existing, unrelated one that happens to still be on
    top or nearby in the stack."""
    mac, vps = two_clones
    foreign_older, foreign_newer = _seed_two_foreign_stashes(vps)

    _uncommitted_edit(mac, "vault/daily/log.md", "seed\nlast\nmac edit\n")
    _run_sync(mac)

    _uncommitted_edit(vps, "vault/daily/log.md", "seed\nlast\nvps edit\n")  # a union-glob conflict

    wrapper_path = _merge_file_blocking_path(tmp_path)
    result = _run_sync(vps, overrides={"PATH": f"{wrapper_path}:{os.environ.get('PATH', '')}"})

    assert result.returncode == 1, "a resolution failure must fail the run"
    state = vps / ".claude" / "data" / "state" / "sync-retained-stash.txt"
    assert state.exists()
    recorded_sha = state.read_text().split()[0]
    assert recorded_sha not in (foreign_older, foreign_newer), \
        "must never record one of the owner's pre-existing stashes"

    remaining = _stash_shas(vps)
    assert foreign_older in remaining and foreign_newer in remaining
    assert recorded_sha in remaining
    assert len(remaining) == 3, "our own (retained) stash plus both foreign ones"


# ── Untracked-file collisions (2026-09-21 fix, item 1a) ───────────────
#
# `git stash push -u` stashes a local untracked file into the stash's 3rd
# parent. If the same path has since arrived from origin (the classic
# both-machines-create-today's-daily-log case), git refuses the WHOLE pop
# before touching anything — no conflict markers anywhere — which used to
# read as "resolved, nothing to do" and silently orphan the stash.


def test_untracked_collision_on_a_union_path_merges_both_sides(two_clones):
    mac, vps = two_clones

    _uncommitted_edit(mac, "vault/daily/2026-02-02.md", "mac's note\n")
    _run_sync(mac)  # mac's file is now tracked on origin

    _uncommitted_edit(vps, "vault/daily/2026-02-02.md", "vps's note\n")  # untracked on vps
    result = _run_sync(vps)

    assert result.returncode == 0, result.stderr
    merged = (vps / "vault" / "daily" / "2026-02-02.md").read_text()
    assert "mac's note" in merged
    assert "vps's note" in merged
    assert _git(vps, "stash", "list").strip() == "", "stash must be dropped once resolved"
    assert not (vps / ".claude" / "data" / "state" / "sync-retained-stash.txt").exists()
    assert len(_notify_lines(vps)) == 0, \
        "a clean union merge lost nothing — 'your local version was saved' would be false"


def test_untracked_collision_on_a_non_union_path_keeps_remote_and_saves_local(two_clones):
    mac, vps = two_clones

    _uncommitted_edit(mac, ".claude/data/fresh.json", "remote fresh\n")
    _run_sync(mac)

    _uncommitted_edit(vps, ".claude/data/fresh.json", "local fresh\n")
    result = _run_sync(vps)

    assert result.returncode == 0, result.stderr
    assert (vps / ".claude" / "data" / "fresh.json").read_text() == "remote fresh\n"
    assert _git(vps, "stash", "list").strip() == ""

    conflicts_root = vps / ".claude" / "data" / "state" / "sync-conflicts"
    saved = list(conflicts_root.rglob("fresh.json"))
    assert len(saved) == 1, "the local (previously untracked) version must be saved aside"
    assert saved[0].read_text() == "local fresh\n"
    assert len(_notify_lines(vps)) == 1


# ── Local delete vs. remote edit (2026-09-21 fix, item 1b) ────────────
#
# Missing stage 3 in a stash-pop conflict means the STASH side deleted the
# path. The old code did `git show ":3:$path" > "$dest"` directly — the `>`
# truncates/creates $dest before git show can fail, leaving a misleading
# zero-byte "saved copy" that reads as rescued work, on top of aborting and
# retaining the stash.


def test_local_delete_vs_remote_edit_keeps_remote_with_no_empty_copy(two_clones):
    mac, vps = two_clones

    _uncommitted_edit(mac, "vault/daily/log.md", "seed\nlast\nmac edited\n")
    _run_sync(mac)

    (vps / "vault" / "daily" / "log.md").unlink()  # local uncommitted deletion, no `git rm`
    result = _run_sync(vps)

    assert result.returncode == 0, result.stderr
    assert (vps / "vault" / "daily" / "log.md").read_text() == "seed\nlast\nmac edited\n", \
        "the local deletion must not be applied — the pulled/edited version wins"
    assert _git(vps, "stash", "list").strip() == "", "stash must be dropped once resolved"

    conflicts_root = vps / ".claude" / "data" / "state" / "sync-conflicts"
    saved = list(conflicts_root.rglob("log.md")) if conflicts_root.exists() else []
    assert saved == [], "a deletion has no content to save — no zero-byte placeholder copy"
    assert not (vps / ".claude" / "data" / "state" / "sync-retained-stash.txt").exists()
    assert len(_notify_lines(vps)) == 0, \
        "a local deletion left nothing to save aside — nothing to notify about either"


# ── A retained stash is never orphaned (2026-09-21 fix, item 1c) ──────
#
# A stash this script could not fully resolve used to rely on
# record_failure's 5-consecutive-failures counter — but the NEXT tick
# typically starts from a clean tree and succeeds, resetting that counter to
# zero before it could ever fire. check_retained_stash runs every tick,
# independent of anything else, and reminds every 24h until the stash (named
# by sha in sync-retained-stash.txt) is actually gone.


def test_retained_stash_is_announced_once_then_after_24h_then_goes_quiet(synced_repo):
    # Stand in for a stash a previous tick could not resolve — the mechanism
    # only cares whether the recorded sha is still in `git stash list`.
    (synced_repo / "vault" / "orphan.md").write_text("orphaned work\n")
    _git(synced_repo, "add", "-A")
    _git(synced_repo, "stash", "push", "-m", "orphan")
    sha = _git(synced_repo, "rev-parse", "stash@{0}").strip()

    state = synced_repo / ".claude" / "data" / "state" / "sync-retained-stash.txt"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(f"{sha} {int(time.time()) - 90000}\n")  # last notified >24h ago

    result = _run_sync(synced_repo)
    assert result.returncode == 0
    assert len(_notify_lines(synced_repo)) == 1, "a due reminder must fire"
    assert sha in "\t".join(_notify_lines(synced_repo))

    # immediately again: not due yet
    _run_sync(synced_repo)
    assert len(_notify_lines(synced_repo)) == 1, "must not remind again before 24h"

    # simulate 24h passing since the last reminder
    state.write_text(f"{sha} {int(time.time()) - 90000}\n")
    _run_sync(synced_repo)
    assert len(_notify_lines(synced_repo)) == 2, "must remind again after 24h"

    # the stash is gone — the reminder must stop and the state must clear
    # (`git stash drop` takes a stash ref like stash@{0}, not the bare sha)
    drop_out = _git(synced_repo, "stash", "drop", "stash@{0}")
    assert _git(synced_repo, "stash", "list").strip() == "", f"test setup: drop failed: {drop_out}"
    _run_sync(synced_repo)
    assert len(_notify_lines(synced_repo)) == 2, "must go quiet once the stash is gone"
    assert not (synced_repo / ".claude" / "data" / "state" / "sync-retained-stash.txt").exists()


def test_retained_reminder_survives_an_unrelated_successful_conflict(two_clones):
    """resolve_stash_conflicts used to unconditionally `rm -f
    RETAINED_STASH_STATE` on every successful resolution — wiping out a
    reminder for a DIFFERENT, still-stranded stash from an earlier incident
    just because this tick's own, unrelated conflict happened to resolve
    cleanly. The reminder must only ever be cleared by check_retained_stash,
    once the stash it actually names is gone."""
    mac, vps = two_clones

    # A stash left over from some earlier incident this tick knows nothing about.
    (vps / "vault" / "orphan.md").write_text("stuck from before\n")
    _git(vps, "add", "-A")
    _git(vps, "stash", "push", "-m", "orphan")
    orphan_sha = _git(vps, "rev-parse", "stash@{0}").strip()

    state = vps / ".claude" / "data" / "state" / "sync-retained-stash.txt"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(f"{orphan_sha} {int(time.time())}\n")  # just notified, not due again yet

    # An ordinary, successful, UNRELATED union conflict this tick.
    _uncommitted_edit(mac, "vault/daily/log.md", "seed\nlast\nmac edit\n")
    _run_sync(mac)
    _uncommitted_edit(vps, "vault/daily/log.md", "seed\nlast\nvps edit\n")
    result = _run_sync(vps)

    assert result.returncode == 0, result.stderr
    merged = (vps / "vault" / "daily" / "log.md").read_text()
    assert "mac edit" in merged and "vps edit" in merged

    assert state.exists(), "an unrelated successful resolution must not clear a different stash's reminder"
    assert state.read_text().split()[0] == orphan_sha

    # fast-forward the reminder clock — it must still fire, for the orphan stash
    state.write_text(f"{orphan_sha} {int(time.time()) - 90000}\n")
    result2 = _run_sync(vps)
    assert result2.returncode == 0
    assert len(_notify_lines(vps)) == 1
    assert orphan_sha in "\t".join(_notify_lines(vps))

    # only once the orphan stash is actually gone does the reminder clear
    drop_out = _git(vps, "stash", "drop", "stash@{0}")
    assert orphan_sha not in _stash_shas(vps), f"test setup: drop failed: {drop_out}"
    _run_sync(vps)
    assert not state.exists()
    assert not state.exists()


def test_conflict_copy_directory_is_gitignored(synced_repo):
    out = subprocess.run(
        ["git", "check-ignore", "-q", ".claude/data/state/sync-conflicts/x/y.json"],
        cwd=synced_repo,
    )
    assert out.returncode == 0, "sync-conflicts copies must never be tracked"
