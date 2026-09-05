"""Rule 1: account aliases, owner emails, and the business inbox come from
the environment, not from a literal in a generic module.

shared.gmail_accounts() / calendar_accounts() / owner_emails() /
primary_account() / business_account() are the only source of truth. Each is
cache-backed (functools.lru_cache) except owner_emails/calendar_accounts
which are cheap to recompute — see shared.py for the actual caching choice.
Tests clear caches with `.cache_clear()` where present, via monkeypatch env.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import shared  # noqa: E402


def _clear_caches():
    for name in ("gmail_accounts", "calendar_accounts", "primary_account", "business_account", "projects"):
        fn = getattr(shared, name, None)
        cache_clear = getattr(fn, "cache_clear", None)
        if cache_clear:
            cache_clear()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "CORTEX_GMAIL_ACCOUNTS",
        "CORTEX_CALENDAR_ACCOUNTS",
        "CORTEX_OWNER_EMAILS",
        "CORTEX_PRIMARY_ACCOUNT",
        "CORTEX_BUSINESS_ACCOUNT",
        "CORTEX_VPS_SSH_ALIAS",
        "CORTEX_VPS_REPO_ROOT",
        "CORTEX_VPS_USER",
        "CORTEX_PROJECTS",
        "CORTEX_ACCOUNT_PROJECTS",
    ):
        monkeypatch.delenv(var, raising=False)
    _clear_caches()
    yield
    _clear_caches()


class TestGmailAccounts:
    def test_unset_is_empty(self):
        assert shared.gmail_accounts() == {}

    def test_parses_alias_equals_id_pairs(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "personal=user_personal,work=user_work")
        assert shared.gmail_accounts() == {"personal": "user_personal", "work": "user_work"}

    def test_tolerates_spaces(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", " personal = user_personal , work = user_work ")
        assert shared.gmail_accounts() == {"personal": "user_personal", "work": "user_work"}

    def test_single_entry(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "personal=user_personal")
        assert shared.gmail_accounts() == {"personal": "user_personal"}

    def test_empty_string_is_empty(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "")
        assert shared.gmail_accounts() == {}

    def test_reflects_setenv_after_cache_clear(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "personal=user_personal")
        assert shared.gmail_accounts() == {"personal": "user_personal"}
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "work=user_work")
        shared.gmail_accounts.cache_clear()
        assert shared.gmail_accounts() == {"work": "user_work"}


class TestCalendarAccounts:
    def test_unset_defaults_to_gmail_aliases(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "personal=user_personal,work=user_work")
        assert shared.calendar_accounts() == ["personal", "work"]

    def test_explicit_value_used(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "personal=user_personal,work=user_work")
        monkeypatch.setenv("CORTEX_CALENDAR_ACCOUNTS", "personal,other")
        assert shared.calendar_accounts() == ["personal", "other"]

    def test_no_gmail_accounts_and_unset_is_empty(self):
        assert shared.calendar_accounts() == []


class TestOwnerEmails:
    def test_unset_is_empty_list(self):
        assert shared.owner_emails() == []

    def test_comma_separated(self, monkeypatch):
        monkeypatch.setenv("CORTEX_OWNER_EMAILS", "someone@example.com, someone@example.org")
        assert shared.owner_emails() == ["someone@example.com", "someone@example.org"]


class TestPrimaryAccount:
    def test_explicit_env_wins(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "personal=user_personal")
        monkeypatch.setenv("CORTEX_PRIMARY_ACCOUNT", "work")
        assert shared.primary_account() == "work"

    def test_falls_back_to_first_gmail_alias(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "personal=user_personal,work=user_work")
        assert shared.primary_account() == "personal"

    def test_falls_back_to_personal_when_nothing_set(self):
        assert shared.primary_account() == "personal"


class TestBusinessAccount:
    def test_explicit_env_wins(self, monkeypatch):
        monkeypatch.setenv("CORTEX_BUSINESS_ACCOUNT", "work")
        assert shared.business_account() == "work"

    def test_falls_back_to_primary_account(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "personal=user_personal,work=user_work")
        assert shared.business_account() == shared.primary_account() == "personal"


class TestVpsSshAlias:
    def test_unset_is_empty(self):
        assert shared.vps_ssh_alias() == ""

    def test_returns_env_value(self, monkeypatch):
        monkeypatch.setenv("CORTEX_VPS_SSH_ALIAS", "my-vps-alias")
        assert shared.vps_ssh_alias() == "my-vps-alias"


class TestVpsRepoRoot:
    def test_unset_is_empty(self):
        assert shared.vps_repo_root() == ""

    def test_returns_env_value(self, monkeypatch):
        monkeypatch.setenv("CORTEX_VPS_REPO_ROOT", "/opt/example/repo")
        assert shared.vps_repo_root() == "/opt/example/repo"


class TestVpsUser:
    def test_unset_is_empty(self):
        assert shared.vps_user() == ""

    def test_returns_env_value(self, monkeypatch):
        monkeypatch.setenv("CORTEX_VPS_USER", "svc-example")
        assert shared.vps_user() == "svc-example"


class TestGmailAccountsUnsetWarning:
    def test_warns_once_per_process_across_two_calls(self, monkeypatch, capsys):
        shared._gmail_accounts_unset_warned = False
        assert shared.gmail_accounts() == {}
        shared.gmail_accounts.cache_clear()
        assert shared.gmail_accounts() == {}
        captured = capsys.readouterr()
        assert captured.err.count(shared.GMAIL_ACCOUNTS_UNSET_WARNING) == 1

    def test_no_warning_when_set(self, monkeypatch, capsys):
        shared._gmail_accounts_unset_warned = False
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "personal=abc123")
        assert shared.gmail_accounts() == {"personal": "abc123"}
        captured = capsys.readouterr()
        assert shared.GMAIL_ACCOUNTS_UNSET_WARNING not in captured.err


class TestNoHardcodedAliasList:
    def test_tools_py_has_no_forbidden_alias_string(self):
        forbidden = "deep" + "digits"
        text = (REPO_ROOT / ".claude" / "chat" / "tools.py").read_text()
        assert forbidden not in text
        assert "example-alias-1" not in text
        assert "example-alias-2" not in text


class TestClearConfigCaches:
    def test_clear_config_caches_clears_all_cached_accessors(self, monkeypatch):
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "first=aaa")
        assert shared.gmail_accounts() == {"first": "aaa"}
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "second=bbb")
        # Still stale — cache not cleared yet.
        assert shared.gmail_accounts() == {"first": "aaa"}
        shared.clear_config_caches()
        assert shared.gmail_accounts() == {"second": "bbb"}

    def test_registry_load_env_clears_caches(self, monkeypatch, tmp_path):
        from integrations import registry

        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "first=aaa")
        assert shared.gmail_accounts() == {"first": "aaa"}

        empty_env = tmp_path / ".env"
        empty_env.write_text("")
        monkeypatch.setattr(registry, "ENV_PATH", empty_env)
        monkeypatch.setenv("CORTEX_GMAIL_ACCOUNTS", "second=bbb")

        registry.load_env()
        assert shared.gmail_accounts() == {"second": "bbb"}


class TestProjects:
    def test_unset_falls_back_to_default_roster(self):
        assert shared.projects() == shared.DEFAULT_PROJECTS
        assert shared.project_slugs() == list(shared.DEFAULT_PROJECTS)

    def test_parses_slug_equals_description_pairs(self, monkeypatch):
        monkeypatch.setenv("CORTEX_PROJECTS", "work=client stuff;personal=personal;inbox=unsorted")
        assert shared.projects() == {
            "work": "client stuff",
            "personal": "personal",
            "inbox": "unsorted",
        }
        assert shared.project_slugs() == ["work", "personal", "inbox"]

    def test_description_may_contain_commas(self, monkeypatch):
        monkeypatch.setenv("CORTEX_PROJECTS", "work=client, vendor, and business work;personal=personal")
        assert shared.projects()["work"] == "client, vendor, and business work"

    def test_tolerates_spaces(self, monkeypatch):
        monkeypatch.setenv("CORTEX_PROJECTS", " work = client work ; personal = personal ")
        assert shared.projects() == {"work": "client work", "personal": "personal"}

    def test_malformed_pairs_are_skipped(self, monkeypatch):
        monkeypatch.setenv("CORTEX_PROJECTS", "work=client work;not-a-pair;personal=personal")
        assert shared.projects() == {"work": "client work", "personal": "personal"}

    def test_blank_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CORTEX_PROJECTS", "   ")
        assert shared.projects() == shared.DEFAULT_PROJECTS

    def test_no_valid_pairs_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CORTEX_PROJECTS", "just garbage, no equals signs")
        assert shared.projects() == shared.DEFAULT_PROJECTS

    def test_cached_until_cleared(self, monkeypatch):
        monkeypatch.setenv("CORTEX_PROJECTS", "work=first")
        assert shared.projects() == {"work": "first"}
        monkeypatch.setenv("CORTEX_PROJECTS", "work=second")
        # Still stale — cache not cleared yet.
        assert shared.projects() == {"work": "first"}
        shared.clear_config_caches()
        assert shared.projects() == {"work": "second"}


class TestAccountProjects:
    def test_unset_is_empty(self):
        assert shared.account_projects() == {}

    def test_parses_alias_equals_slug_pairs(self, monkeypatch):
        monkeypatch.setenv("CORTEX_ACCOUNT_PROJECTS", "personal=personal,work=work")
        assert shared.account_projects() == {"personal": "personal", "work": "work"}

    def test_tolerates_spaces(self, monkeypatch):
        monkeypatch.setenv("CORTEX_ACCOUNT_PROJECTS", " personal = personal , work = work ")
        assert shared.account_projects() == {"personal": "personal", "work": "work"}

    def test_malformed_pairs_are_skipped(self, monkeypatch):
        monkeypatch.setenv("CORTEX_ACCOUNT_PROJECTS", "personal=personal,not-a-pair")
        assert shared.account_projects() == {"personal": "personal"}

    def test_not_cached_reflects_env_immediately(self, monkeypatch):
        monkeypatch.setenv("CORTEX_ACCOUNT_PROJECTS", "personal=personal")
        assert shared.account_projects() == {"personal": "personal"}
        monkeypatch.setenv("CORTEX_ACCOUNT_PROJECTS", "work=work")
        assert shared.account_projects() == {"work": "work"}
