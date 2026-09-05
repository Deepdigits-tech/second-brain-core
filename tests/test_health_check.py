"""Tests for the VPS Composio health check.

Regression cover for a week-long silent failure: from 2026-08-08 the VPS
COMPOSIO_API_KEY was rejected with 401 and every Gmail/Calendar call failed, but
the check returned `warn` and heartbeat only notifies on `critical`. Nothing ever
reached Telegram.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import health_check  # noqa: E402


def _ssh_returning(monkeypatch, stdout="", stderr="", returncode=0):
    def fake(cmd, timeout=None):
        return subprocess.CompletedProcess(
            args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(health_check, "_ssh", fake)


ALL_ACTIVE = "\n".join(
    f"  acct{i} {tk:18s} ✓ ACTIVE" for i in range(4) for tk in ("gmail", "googlecalendar")
)

AUTH_401 = "\n".join(
    f"  acct{i} {tk} ? UNKNOWN: Error code: 401 - "
    "{'error': {'message': 'Invalid API key: ak_**XnSL', 'slug': 'APIKey_InvalidAPIKey'}}"
    for i in range(4)
    for tk in ("gmail", "googlecalendar")
)


def test_dead_api_key_is_critical(monkeypatch):
    """The exact week-long failure. Must be critical so heartbeat pings Telegram."""
    _ssh_returning(monkeypatch, stdout=AUTH_401, returncode=1)

    result = health_check.check_vps_composio_auth()

    assert result["status"] == "critical"
    assert "API key" in result["detail"]


def test_unverifiable_status_is_critical(monkeypatch):
    """UNKNOWN contains neither ACTIVE nor MISSING — must not read as healthy."""
    _ssh_returning(
        monkeypatch, stdout="  acct gmail ? UNKNOWN: connection reset", returncode=1
    )

    assert health_check.check_vps_composio_auth()["status"] == "critical"


def test_missing_connection_is_critical(monkeypatch):
    _ssh_returning(
        monkeypatch,
        stdout=f"  a gmail ✗ MISSING\n  a googlecalendar ✓ ACTIVE\n{ALL_ACTIVE}",
        returncode=1,
    )

    result = health_check.check_vps_composio_auth()

    assert result["status"] == "critical"
    assert "MISSING" in result["detail"]


def test_all_active_is_ok(monkeypatch):
    _ssh_returning(monkeypatch, stdout=f"Connected account status:\n\n{ALL_ACTIVE}", returncode=0)

    assert health_check.check_vps_composio_auth()["status"] == "ok"


def test_unrecognized_output_with_bad_exit_is_critical(monkeypatch):
    _ssh_returning(monkeypatch, stderr="ModuleNotFoundError: composio", returncode=1)

    assert health_check.check_vps_composio_auth()["status"] == "critical"


def test_ssh_timeout_is_warn_not_critical(monkeypatch):
    """A transient blip should not page. VPS-down is covered by the chat-server check."""

    def boom(cmd, timeout=None):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=45)

    monkeypatch.setattr(health_check, "_ssh", boom)

    assert health_check.check_vps_composio_auth()["status"] == "warn"


def test_check_does_not_truncate_output_before_parsing(monkeypatch):
    """`| head -6` used to hide both later rows and the real exit code."""
    seen = {}

    def fake(cmd, timeout=None):
        seen["cmd"] = cmd
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ALL_ACTIVE, stderr="")

    monkeypatch.setattr(health_check, "_ssh", fake)
    health_check.check_vps_composio_auth()

    assert "head -" not in seen["cmd"]
    assert seen["timeout"] and seen["timeout"] >= 30, "5.5s real runtime needs headroom"


def test_critical_check_reaches_alerts_and_overall_status(monkeypatch):
    """Wiring guard: critical must surface in run_all_checks so heartbeat notifies."""
    monkeypatch.setattr(health_check, "run_tier1_checks", lambda: {})
    monkeypatch.setattr(
        health_check,
        "run_tier2_checks",
        lambda: {"vps_composio_auth": health_check._critical("VPS Composio API key rejected")},
    )

    report = health_check.run_all_checks(morning=True)

    assert report["status"] == "critical"
    assert any("vps_composio_auth" in a for a in report["alerts"])


class TestVpsChecksSkipWhenAliasUnset:
    """Tier-2 VPS checks require all three of CORTEX_VPS_SSH_ALIAS,
    CORTEX_VPS_REPO_ROOT and CORTEX_VPS_USER — missing any one -> warn
    findings naming what's missing, never an ssh call.
    """

    def _no_ssh_allowed(self, monkeypatch):
        def fail(cmd, timeout=None):
            raise AssertionError(f"ssh must not run when VPS config is incomplete: {cmd}")

        monkeypatch.setattr(health_check, "_run", fail)

    def _set_all_vps_vars(self, monkeypatch):
        monkeypatch.setenv("CORTEX_VPS_SSH_ALIAS", "example-vps-alias")
        monkeypatch.setenv("CORTEX_VPS_REPO_ROOT", "/opt/example/repo")
        monkeypatch.setenv("CORTEX_VPS_USER", "svc-example")

    def test_tier2_warns_without_ssh_when_all_three_unset(self, monkeypatch):
        monkeypatch.setenv("CORTEX_VPS_SSH_ALIAS", "")
        monkeypatch.setenv("CORTEX_VPS_REPO_ROOT", "")
        monkeypatch.setenv("CORTEX_VPS_USER", "")
        monkeypatch.setattr(health_check, "IS_MAC", True)
        self._no_ssh_allowed(monkeypatch)
        monkeypatch.setattr(health_check, "check_local_launchd", lambda: health_check._ok("skipped"))
        monkeypatch.setattr(health_check, "check_reflection_freshness", lambda: health_check._ok("skipped"))

        checks = health_check.run_tier2_checks()

        vps_names = ("vps_chat_server", "vps_telegram_health", "vps_composio_auth", "vps_sync_cron")
        for name in vps_names:
            assert checks[name]["status"] == "warn"
            assert "CORTEX_VPS_SSH_ALIAS" in checks[name]["detail"]
            assert "CORTEX_VPS_REPO_ROOT" in checks[name]["detail"]
            assert "CORTEX_VPS_USER" in checks[name]["detail"]

    def test_tier2_warns_naming_only_the_missing_settings(self, monkeypatch):
        self._set_all_vps_vars(monkeypatch)
        monkeypatch.setenv("CORTEX_VPS_REPO_ROOT", "")  # only this one missing
        monkeypatch.setattr(health_check, "IS_MAC", True)
        self._no_ssh_allowed(monkeypatch)
        monkeypatch.setattr(health_check, "check_local_launchd", lambda: health_check._ok("skipped"))
        monkeypatch.setattr(health_check, "check_reflection_freshness", lambda: health_check._ok("skipped"))

        checks = health_check.run_tier2_checks()

        detail = checks["vps_chat_server"]["detail"]
        assert checks["vps_chat_server"]["status"] == "warn"
        assert "CORTEX_VPS_REPO_ROOT" in detail
        assert "CORTEX_VPS_SSH_ALIAS" not in detail
        assert "CORTEX_VPS_USER" not in detail

    def test_tier2_runs_ssh_checks_when_all_three_set(self, monkeypatch):
        self._set_all_vps_vars(monkeypatch)
        monkeypatch.setattr(health_check, "IS_MAC", True)

        def fake_ssh(cmd, timeout=None):
            return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="active", stderr="")

        monkeypatch.setattr(health_check, "_ssh", fake_ssh)
        monkeypatch.setattr(health_check, "check_local_launchd", lambda: health_check._ok("skipped"))
        monkeypatch.setattr(health_check, "check_reflection_freshness", lambda: health_check._ok("skipped"))

        checks = health_check.run_tier2_checks()

        assert checks["vps_chat_server"]["status"] == "ok"


class TestGmailAccountsConfiguredCheck:
    """Tier-1 check: CORTEX_GMAIL_ACCOUNTS unset -> warn, never a crash."""

    def test_warns_when_unset(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "")
        health_check.shared.gmail_accounts.cache_clear()
        result = health_check.check_gmail_accounts_configured()
        assert result["status"] == "warn"
        assert "CORTEX_GMAIL_ACCOUNTS" in result["detail"]

    def test_ok_when_set(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "personal=abc123")
        health_check.shared.gmail_accounts.cache_clear()
        result = health_check.check_gmail_accounts_configured()
        assert result["status"] == "ok"

    def test_included_in_tier1_checks(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "")
        health_check.shared.gmail_accounts.cache_clear()
        checks = health_check.run_tier1_checks()
        assert "gmail_accounts_configured" in checks
