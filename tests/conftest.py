"""Shared test guards.

Tests must never reach the outside world. On 2026-08-15 a run of the suite put a
real "Cortex: content intake FAILED — disk full" banner on the owner's Mac: a test
deliberately raised RuntimeError("disk full") to prove the handler does not
crash, and the handler faithfully did its job and alerted them. The disk had 24
GiB free.

A false alarm is worse than no alarm — it teaches you to dismiss the banner, and
the next one is real.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))


@pytest.fixture(scope="session", autouse=True)
def _isolate_auto_memory_dir(tmp_path_factory):
    """No test should glob the developer's real auto-memory folder.

    A test that reads `~/.claude/projects/.../memory/` without patching it
    would pick up whatever is actually on the machine running the suite,
    making the result depend on that machine's history. Point the env var
    at a fresh empty directory unless a test (or the environment) already
    set it explicitly — an explicit setting wins.
    """
    if "CORTEX_AUTO_MEMORY_DIR" not in os.environ:
        d = tmp_path_factory.mktemp("auto_memory")
        os.environ["CORTEX_AUTO_MEMORY_DIR"] = str(d)
    yield


@pytest.fixture(autouse=True)
def no_real_notifications(monkeypatch):
    """Fail loudly instead of paging the user.

    A test that legitimately exercises an alert path should monkeypatch the
    notifier itself and assert on what it captured; that patch is applied after
    this fixture and wins. Anything that reaches these stubs is leaking.
    """
    import notify

    def _leaked(*args, **kwargs):
        raise AssertionError(
            "a test tried to send a REAL notification. Patch the notifier in the "
            "test and assert on the captured call instead."
        )

    monkeypatch.setattr(notify, "notify", _leaked)
    monkeypatch.setattr(notify, "notify_macos", _leaked)
    monkeypatch.setattr(notify, "notify_telegram", _leaked)
    yield


import subprocess as _subprocess


def _vault_status() -> set[str]:
    r = _subprocess.run(["git", "status", "--porcelain", "--", "vault/"], cwd=REPO_ROOT,
                        capture_output=True, text=True)
    return set(r.stdout.splitlines()) if r.returncode == 0 else set()


def pytest_sessionstart(session):
    session.config._vault_before = _vault_status()


def pytest_sessionfinish(session, exitstatus):
    """A test that writes into the real vault is a bug, not a side effect.

    tests/test_capture_pipeline.py wrote a fake capture note into
    vault/kb/gtm/sources/ and a row into its INDEX.md on every run for two
    weeks (seven such files, committed and synced) before anyone noticed.

    A file that is already dirty before the session and gets dirtier during it
    shows the same porcelain line and is not caught; the guard exists for NEW
    paths.
    """
    before = getattr(session.config, "_vault_before", None)
    if before is None:
        return
    leaked = sorted(_vault_status() - before)
    if leaked:
        session.exitstatus = 1
        print("\nTESTS LEAKED INTO THE REAL VAULT:\n  " + "\n  ".join(leaked), file=sys.stderr)
