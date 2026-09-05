"""Tests for integration availability checks (.claude/scripts/integrations/registry.py).

The Composio-backed checks verify a real ACTIVE connection instead of trusting the
presence of an API key. The critical invariant is that an unreachable Composio API
must not report the integration as unavailable — heartbeat gates email gathering on
this flag, so failing closed would silently stop all inbox monitoring.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

from integrations import registry  # noqa: E402


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key")
    monkeypatch.delenv("CORTEX_VERIFY_CONNECTIONS", raising=False)
    # Account aliases are env-driven (Rule 1) — fake ids for these tests only.
    monkeypatch.setenv(
        "CORTEX_GMAIL_ACCOUNTS",
        "personal=owner_personal,work=owner_work,"
        "team=example@example.org,vendor=example-vendor",
    )
    import shared
    shared.gmail_accounts.cache_clear()
    registry._ACTIVE_CACHE.clear()
    yield
    shared.gmail_accounts.cache_clear()
    registry._ACTIVE_CACHE.clear()


def _stub_active(monkeypatch, value):
    monkeypatch.setattr(registry, "_active_user_ids", lambda toolkit: value)


def test_missing_api_key_fails_closed(monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    result = registry.check_integration("gmail")
    assert result["available"] is False
    assert "COMPOSIO_API_KEY" in result["reason"]


def test_lookup_failure_fails_open(api_key, monkeypatch):
    """An unreachable Composio API must stay available — heartbeat depends on it."""
    _stub_active(monkeypatch, None)
    result = registry.check_integration("gmail")
    assert result["available"] is True
    assert "connection check failed" in result["reason"]


def test_no_active_connections_reports_unavailable(api_key, monkeypatch):
    _stub_active(monkeypatch, set())
    result = registry.check_integration("gmail")
    assert result["available"] is False
    assert "no ACTIVE gmail connection" in result["reason"]


def test_partial_connection_available_and_names_missing(api_key, monkeypatch):
    _stub_active(monkeypatch, {"owner_work", "example-vendor"})
    result = registry.check_integration("gmail")
    assert result["available"] is True
    assert result["accounts"] == {
        "personal": False,
        "work": True,
        "team": False,
        "vendor": True,
    }
    assert "missing: personal, team" in result["reason"]


def test_all_connected(api_key, monkeypatch):
    _stub_active(monkeypatch, set(registry._account_user_ids().values()))
    result = registry.check_integration("gcal")
    assert result["available"] is True
    assert all(result["accounts"].values())
    assert "4/4" in result["reason"]


def test_verify_false_skips_network(api_key, monkeypatch):
    def boom(toolkit):
        raise AssertionError("network lookup should not run when verify=False")

    monkeypatch.setattr(registry, "_active_user_ids", boom)
    result = registry.check_integration("gmail", verify=False)
    assert result["available"] is True
    assert "unverified" in result["reason"]


def test_env_var_disables_verification(api_key, monkeypatch):
    def boom(toolkit):
        raise AssertionError("network lookup should not run when disabled by env")

    monkeypatch.setattr(registry, "_active_user_ids", boom)
    monkeypatch.setenv("CORTEX_VERIFY_CONNECTIONS", "0")
    assert registry.check_integration("gmail")["available"] is True


def test_active_lookup_is_cached(api_key, monkeypatch):
    calls = []

    class _Item:
        user_id = "owner_work"
        status = "ACTIVE"
        toolkit = SimpleNamespace(slug="gmail")

    class _Resp:
        items = [_Item()]

    class _Client:
        def __init__(self, **kwargs):
            calls.append(1)
            self.connected_accounts = self

        def list(self, **kwargs):
            return _Resp()

    monkeypatch.setitem(sys.modules, "composio", type(sys)("composio"))
    sys.modules["composio"].Composio = _Client

    assert registry._active_user_ids("gmail") == {"owner_work"}
    assert registry._active_user_ids("gmail") == {"owner_work"}
    assert len(calls) == 1, "second call should hit the cache"


def test_non_composio_checks_unaffected(api_key, monkeypatch):
    if "smartlead" not in registry._optional_lanes_present():
        pytest.skip("smartlead is a Cortex-only lane; not present in this tree")
    monkeypatch.setenv("SMARTLEAD_API_KEY", "t")
    assert registry.check_integration("smartlead")["available"] is True
    monkeypatch.delenv("SMARTLEAD_API_KEY")
    assert registry.check_integration("smartlead")["available"] is False


def test_unknown_integration(api_key):
    assert registry.check_integration("nope")["available"] is False


def test_check_all_covers_every_integration(api_key, monkeypatch):
    _stub_active(monkeypatch, set())
    result = registry.check_all()
    # smartlead/garmin are Cortex-only optional lanes — present here, absent
    # from an extracted tree that doesn't ship integrations/smartlead.py or
    # integrations/garmin.py.
    expected = {"gmail", "gcal", "telegram", "slack"} | registry._optional_lanes_present()
    assert set(result) == expected


def test_optional_lanes_absent_when_module_blocked(api_key, monkeypatch):
    """garmin/smartlead register only when their module actually imports —
    they're optional dependencies of this repo, not the shareable core."""
    monkeypatch.setenv("CORTEX_DISABLE_MODULES", "integrations.garmin,integrations.smartlead")
    import shared
    shared.clear_config_caches()
    _stub_active(monkeypatch, set())
    try:
        result = registry.check_all()
        assert set(result) == {"gmail", "gcal", "telegram", "slack"}
        assert registry.check_integration("garmin")["available"] is False
        assert registry.check_integration("smartlead")["available"] is False
    finally:
        shared.clear_config_caches()


# --- ACTIVE-status hardening -----------------------------------------------
#
# _active_user_ids passes statuses=["ACTIVE"] to Composio, but Composio retains
# every past connection attempt (EXPIRED, INITIATED, FAILED). If that server-side
# filter ever stops applying, trusting the response wholesale would make a dead
# record look like a live connection. Same defect class as composio_setup.py.


def _install_fake_composio(monkeypatch, items):
    """Point `from composio import Composio` at a client returning `items`."""

    class _Resp:
        def __init__(self, items):
            self.items = items

    class _Client:
        def __init__(self, **kwargs):
            self.connected_accounts = self

        def list(self, **kwargs):
            return _Resp(items)

    monkeypatch.setitem(sys.modules, "composio", type(sys)("composio"))
    sys.modules["composio"].Composio = _Client


def _rec(status, user_id, toolkit="gmail"):
    return SimpleNamespace(
        status=status, user_id=user_id, toolkit=SimpleNamespace(slug=toolkit)
    )


@pytest.mark.parametrize("stale", ["INITIATED", "INITIALIZING", "EXPIRED", "FAILED", "INACTIVE"])
def test_non_active_records_are_not_counted(api_key, monkeypatch, stale):
    _install_fake_composio(
        monkeypatch,
        [_rec(stale, "owner_personal"), _rec("ACTIVE", "owner_work")],
    )
    assert registry._active_user_ids("gmail") == {"owner_work"}


def test_other_toolkit_records_are_not_counted(api_key, monkeypatch):
    _install_fake_composio(
        monkeypatch,
        [_rec("ACTIVE", "owner_personal", toolkit="googlecalendar")],
    )
    assert registry._active_user_ids("gmail") == set()


def test_unreadable_status_is_unverifiable_not_empty(api_key, monkeypatch):
    """Fail open, never closed.

    If the SDK renames `status`, reporting an empty set would make the registry
    say "no ACTIVE gmail connection" and heartbeat would stop gathering email
    entirely. Unverifiable must look like a failed lookup instead.
    """
    _install_fake_composio(
        monkeypatch, [SimpleNamespace(user_id="owner_personal")]  # no status field
    )
    assert registry._active_user_ids("gmail") is None

    registry._ACTIVE_CACHE.clear()
    monkeypatch.setattr(registry, "_active_user_ids", lambda toolkit: None)
    assert registry.check_integration("gmail")["available"] is True


def test_readable_active_survives_alongside_unreadable(api_key, monkeypatch):
    _install_fake_composio(
        monkeypatch,
        [SimpleNamespace(user_id="owner_personal"), _rec("ACTIVE", "owner_work")],
    )
    assert registry._active_user_ids("gmail") == {"owner_work"}


def test_active_records_are_counted(api_key, monkeypatch):
    _install_fake_composio(
        monkeypatch,
        [_rec("ACTIVE", "owner_personal"), _rec("active", "owner_work")],
    )
    assert registry._active_user_ids("gmail") == {"owner_personal", "owner_work"}
