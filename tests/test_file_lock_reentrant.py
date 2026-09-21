"""shared.file_lock: the docstring claims reentrancy; prove it, don't just say it.

Before 2026-09-20 `file_lock` opened a FRESH file descriptor and called
`fcntl.flock(..., LOCK_EX)` on every entry regardless of who already held it.
`flock` locks an open file DESCRIPTION, not a thread or a process, so a
second `open()` + `flock()` on the same path from the SAME thread is asking
the OS for a lock a different fd already holds — it blocks forever. Nothing
in this codebase nested `file_lock` yet, but the docstring invited it, and a
future caller (e.g. a function that takes the lock calling another that
takes it again for the same path) would hang the whole tick with no
exception, no timeout, no log line — just a dead process.

These tests run the nested acquire on a background thread with a bounded
join() timeout, so a regression FAILS FAST (assertion) instead of hanging
the test suite forever.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import shared  # noqa: E402


def test_nested_acquire_same_thread_does_not_deadlock(tmp_path):
    path = str(tmp_path / "store.json")
    entered_inner = []

    def _work():
        with shared.file_lock(path):
            with shared.file_lock(path):  # nested — must be a no-op, not a re-lock
                entered_inner.append(1)

    t = threading.Thread(target=_work)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "nested file_lock() acquire deadlocked"
    assert entered_inner == [1]


def test_triple_nested_acquire_releases_only_at_the_outermost_exit(tmp_path):
    path = str(tmp_path / "store.json")
    order = []

    def _work():
        with shared.file_lock(path):
            order.append("outer-in")
            with shared.file_lock(path):
                order.append("mid-in")
                with shared.file_lock(path):
                    order.append("inner-in")
                order.append("mid-out")
            order.append("outer-out")

    t = threading.Thread(target=_work)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert order == ["outer-in", "mid-in", "inner-in", "mid-out", "outer-out"]


def test_a_different_thread_still_genuinely_blocks(tmp_path):
    """The reentrancy is per-thread — a different thread must still wait for
    the real flock, or this "fix" would have quietly removed the lock's whole
    purpose (serializing concurrent writers)."""
    path = str(tmp_path / "store.json")
    holder_has_lock = threading.Event()
    release_holder = threading.Event()
    second_thread_acquired_at = []

    def _holder():
        with shared.file_lock(path):
            holder_has_lock.set()
            release_holder.wait(timeout=5)

    def _waiter():
        holder_has_lock.wait(timeout=5)
        with shared.file_lock(path):
            second_thread_acquired_at.append(release_holder.is_set())

    t1 = threading.Thread(target=_holder)
    t2 = threading.Thread(target=_waiter)
    t1.start()
    t1.join(timeout=0.2)  # let it grab the lock and block on release_holder
    t2.start()
    # Give the waiter a moment — it must NOT have acquired yet.
    t2.join(timeout=0.3)
    assert t2.is_alive(), "a different thread acquired the lock while the holder still had it"
    release_holder.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()
    # The waiter only got in AFTER the holder released.
    assert second_thread_acquired_at == [True]


def test_depth_counter_cleared_on_exception_so_a_later_call_is_not_stuck_reentrant(tmp_path):
    path = str(tmp_path / "store.json")

    with pytest.raises(RuntimeError):
        with shared.file_lock(path):
            raise RuntimeError("boom")

    # A fresh, non-nested acquire afterward must take the REAL lock again
    # (prove it by checking a different thread still blocks on it).
    holder_has_lock = threading.Event()
    release_holder = threading.Event()

    def _holder():
        with shared.file_lock(path):
            holder_has_lock.set()
            release_holder.wait(timeout=5)

    t1 = threading.Thread(target=_holder)
    t1.start()
    holder_has_lock.wait(timeout=5)

    acquired = threading.Event()

    def _waiter():
        with shared.file_lock(path):
            acquired.set()

    t2 = threading.Thread(target=_waiter)
    t2.start()
    t2.join(timeout=0.3)
    assert not acquired.is_set(), "depth counter leaked across an exception — lock treated as already held"
    release_holder.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert acquired.is_set()
