"""Shared utilities for Cortex OS hooks and scripts."""

import contextlib
import fcntl
import functools
import glob
import importlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# zoneinfo is stdlib (3.9+) but needs system tzdata; a bare container without
# /usr/share/zoneinfo raises ZoneInfoNotFoundError at import.
# The one timezone definition. Eleven scripts used to hard-code UTC-7 and call
# it PST; that is PDT, and it is wrong for four months of the year. Read from
# TIMEZONE so an extracted/shared copy of this repo isn't pinned to one
# owner's timezone; falls back to America/Los_Angeles with a one-time note.
_TZ_WARNED = False


def _timezone_from_settings_file() -> str:
    """TIMEZONE straight from the settings file, for callers that run before
    load_env(). Module-level constants (PST below) are evaluated at import,
    which is before any script has loaded the file into the environment; without
    this, a configured timezone was silently ignored by every scheduled script.
    """
    try:
        path = Path(__file__).resolve().parent.parent / ".env"
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("TIMEZONE=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def owner_timezone() -> ZoneInfo:
    """Return the configured timezone (TIMEZONE setting), defaulting to Pacific.

    An unset TIMEZONE and an invalid one both fall back the same way — the
    difference is only which one-time warning gets printed. A bad IANA name
    (typo, wrong separator) must never crash import of this module; every
    script imports shared at module load time.
    """
    global _TZ_WARNED
    tz_name = os.environ.get("TIMEZONE", "").strip() or _timezone_from_settings_file()
    if not tz_name:
        if not _TZ_WARNED:
            print(
                "shared.owner_timezone: TIMEZONE not set, defaulting to America/Los_Angeles",
                file=sys.stderr,
            )
            _TZ_WARNED = True
        tz_name = "America/Los_Angeles"
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        if not _TZ_WARNED:
            print(
                f"shared.owner_timezone: TIMEZONE={tz_name!r} is not a valid IANA "
                "timezone, defaulting to America/Los_Angeles",
                file=sys.stderr,
            )
            _TZ_WARNED = True
        return ZoneInfo("America/Los_Angeles")


# Module constant kept for compatibility with existing call sites.
PST = owner_timezone()

# Daily-log budget shared by the session-start hook and the chat server. The
# TOP of each log is kept: the morning brief sits there and is the densest
# single summary of the day. See vault/research/2026-05-01-memory-system-audit.md #7.
MAX_DAILY_LINES = 50

# A per-cent-of-the-word-cap on top of MAX_DAILY_LINES: some daily-log lines
# are very long (a pasted transcript excerpt, a long URL), so 50 short lines
# and 50 long lines are very different sizes. Each log is cut to its first
# MAX_DAILY_LINES lines AND THEN to MAX_DAILY_CHARS at a line boundary, so
# three logs fit inside MAX_CONTEXT_CHARS with their heads intact instead of
# the third one being cut mid-sentence by the global cap.
MAX_DAILY_CHARS = 3_500

# Session-start budget (workshop memory PRD). The index is small by design; the
# daily logs are what gets cut when the budget is hit, newest first survives.
MAX_CONTEXT_CHARS = 20_000
CORE_FILES = (("SOUL", "SOUL.md"), ("USER", "USER.md"), ("MEMORY", "MEMORY.md"), ("CORE MEMORIES", "core-memories.md"))


# dict-as-ordered-set: insertion order is preserved (Python 3.7+), membership
# test is O(1), and we never care about the values.
_ABSENT_MODULES_LOGGED: dict[str, None] = {}


def optional_import(name: str) -> ModuleType | None:
    """Import `name`, returning None if it (or a submodule of it) is absent.

    Honours CORTEX_DISABLE_MODULES (comma-separated module names) as an
    explicit kill switch — useful both for the extracted/shareable copy and
    for operators who want to turn a lane off without uninstalling anything.

    Only a ModuleNotFoundError whose `.name` matches `name` or a dotted
    submodule of it is treated as "absent" — any other exception (a real bug
    inside the module) propagates, so a broken module never looks like a
    missing one.

    Logs `optional module <name> not present — lane skipped` to stderr once
    per process per name.
    """
    raw_disabled = os.environ.get("CORTEX_DISABLE_MODULES", "")
    disabled = {part.strip() for part in raw_disabled.split(",") if part.strip()}
    if name in disabled:
        _log_absent_once(name)
        return None
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        missing = e.name or ""
        if missing == name or missing.startswith(name + "."):
            _log_absent_once(name)
            return None
        raise


def _log_absent_once(name: str) -> None:
    if name in _ABSENT_MODULES_LOGGED:
        return
    _ABSENT_MODULES_LOGGED[name] = None
    if os.environ.get("CORTEX_DEBUG"):
        print(f"optional module {name} not present — lane skipped", file=sys.stderr)


def absent_modules() -> list[str]:
    """Names passed to optional_import that turned out absent this process,
    in the order first seen. `query.py status` prints these as one summary
    line instead of each one being a separate stderr line on every run."""
    return list(_ABSENT_MODULES_LOGGED)


def lane_available(*names: str) -> bool:
    """True only if every named module can be imported (or is already
    imported). Used to gate feature registration (tool lists, subcommand
    groups) without importing twice."""
    return all(optional_import(name) is not None for name in names)


def cut_at_line_boundary(text: str, max_chars: int) -> str:
    """Cut text to at most max_chars, at the last complete line — never
    mid-line. Falls back to a hard cut at max_chars when no newline exists
    before that point (e.g. one huge line).
    """
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    if cut == -1:
        cut = max_chars
    return text[:cut]


def _read_or_skip(path) -> str:
    """A missing OR unreadable file is skipped, never fatal — this runs per message."""
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


def build_vault_context(vault, max_daily_lines: int = MAX_DAILY_LINES, include_date: bool = True,
                        max_chars: int = MAX_CONTEXT_CHARS, max_daily_chars: int = MAX_DAILY_CHARS) -> str:
    """The context every session starts with. One definition, two callers.

    Order: today's date, BOOTSTRAP (if present — a fresh clone's onboarding
    interview, deleted once setup is done so it loads exactly once), SOUL,
    USER, MEMORY (the index), core-memories, then the three newest daily logs
    truncated to their first `max_daily_lines` lines, and then further to
    `max_daily_chars` at a line boundary — a per-log budget on top of the line
    cap, so one log full of long lines can't crowd out the other two.
    Entity/topic pages are NEVER loaded here — the index tells the agent what
    exists and it reads a page on demand. The whole output is capped at
    `max_chars`, cut at the last complete line, with a marker so a reader can
    tell a short context from a truncated one.
    """
    vault = Path(vault)
    output: list[str] = []

    if include_date:
        output.append(f"=== TODAY ===\n{datetime.now(PST).strftime('%A, %Y-%m-%d')}")

    bootstrap_content = _read_or_skip(vault / "BOOTSTRAP.md")
    if bootstrap_content:
        output.append(f"=== BOOTSTRAP ===\n{bootstrap_content}")

    for label, name in CORE_FILES:
        content = _read_or_skip(vault / name)
        if content:
            output.append(f"=== {label} ===\n{content}")

    daily_files = sorted(glob.glob(str(vault / "daily" / "????-??-??.md")), reverse=True)[:3]
    for daily_path in daily_files:
        p = Path(daily_path)
        lines = _read_or_skip(p).splitlines()[:max_daily_lines]
        if lines:
            body = "\n".join(lines)
            body = cut_at_line_boundary(body, max_daily_chars)
            output.append(f"=== DAILY LOG: {p.stem} ===\n" + body)

    text = "\n\n".join(output)
    if len(text) <= max_chars:
        return text
    marker = f"\n[context truncated at {max_chars} chars]"
    hard_limit = max_chars - len(marker)
    return cut_at_line_boundary(text, hard_limit).rstrip() + marker


@contextlib.contextmanager
def file_lock(path):
    """Exclusive file lock using fcntl.flock (macOS/Linux).
    # Windows would need msvcrt.locking instead.
    """
    lock_path = f"{path}.lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def with_retry(max_retries=3, backoff=1.0):
    """Decorator with exponential backoff."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_retries - 1:
                        time.sleep(backoff * (2 ** attempt))
            raise last_exc
        return wrapper
    return decorator


def owner_name() -> str:
    """The person this system serves, from OWNER_NAME in the env. Generic default.

    Rule 1 of the fix plan: no personal literal in a generic module. Every
    prompt says "{owner}" and renders it through this at build time.
    """
    return os.environ.get("OWNER_NAME", "").strip() or "the user"


GMAIL_ACCOUNTS_UNSET_WARNING = (
    "CORTEX_GMAIL_ACCOUNTS is not set — no Gmail accounts configured; "
    "the inbox lane is disabled"
)

_gmail_accounts_unset_warned = False


@functools.lru_cache(maxsize=1)
def gmail_accounts() -> dict[str, str]:
    """Alias -> Composio user_id, from CORTEX_GMAIL_ACCOUNTS ("alias=id,alias=id").

    Rule 1: no account alias or Composio user id lives in a generic module.
    Unset or empty -> {} (import must never crash on a missing env var), but
    prints one warning to stderr per process so a missing env var is loud,
    not a silent no-op. Cached per process; tests clear with
    gmail_accounts.cache_clear() after monkeypatch.setenv so a change is
    picked up.
    """
    raw = os.environ.get("CORTEX_GMAIL_ACCOUNTS", "").strip()
    if not raw:
        global _gmail_accounts_unset_warned
        if not _gmail_accounts_unset_warned:
            print(GMAIL_ACCOUNTS_UNSET_WARNING, file=sys.stderr)
            _gmail_accounts_unset_warned = True
        return {}
    accounts: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        alias, _, user_id = pair.partition("=")
        alias, user_id = alias.strip(), user_id.strip()
        if alias and user_id:
            accounts[alias] = user_id
    return accounts


@functools.lru_cache(maxsize=1)
def calendar_accounts() -> list[str]:
    """Calendar account aliases, from CORTEX_CALENDAR_ACCOUNTS ("alias,alias").

    Unset -> the gmail_accounts() aliases (calendars normally track mail
    accounts one-for-one). Explicit env, even empty, overrides that default.
    """
    raw = os.environ.get("CORTEX_CALENDAR_ACCOUNTS")
    if raw is None:
        return list(gmail_accounts())
    return [a.strip() for a in raw.split(",") if a.strip()]


def owner_emails() -> list[str]:
    """The owner's own email addresses, from CORTEX_OWNER_EMAILS (comma-separated).

    Used to tell the owner's own sent messages apart from a correspondent's.
    Unset -> [] (never fatal to a missing env var).
    """
    raw = os.environ.get("CORTEX_OWNER_EMAILS", "").strip()
    if not raw:
        return []
    return [e.strip() for e in raw.split(",") if e.strip()]


@functools.lru_cache(maxsize=1)
def primary_account() -> str:
    """The default Gmail/calendar account alias when a caller doesn't name one.

    CORTEX_PRIMARY_ACCOUNT, else the first alias in gmail_accounts(), else
    the generic fallback "personal" — matching gmail.DEFAULT_ACCOUNT.
    """
    explicit = os.environ.get("CORTEX_PRIMARY_ACCOUNT", "").strip()
    if explicit:
        return explicit
    accounts = gmail_accounts()
    if accounts:
        return next(iter(accounts))
    return "personal"


@functools.lru_cache(maxsize=1)
def business_account() -> str:
    """The inbox where bookings, intros and other business mail arrive.

    CORTEX_BUSINESS_ACCOUNT, else primary_account() — most single-business
    setups have one inbox that does both.
    """
    explicit = os.environ.get("CORTEX_BUSINESS_ACCOUNT", "").strip()
    return explicit or primary_account()


DEFAULT_PROJECTS = {
    "work": "client and business work",
    "personal": "personal",
    "inbox": "unsorted",
}


@functools.lru_cache(maxsize=1)
def projects() -> dict[str, str]:
    """Project slug -> one-line description, from CORTEX_PROJECTS.

    Rule 1: the project roster (client/business slugs and who owns them) is
    configuration, not a literal in a generic module. Format is
    "slug=description;slug=description" — semicolons, not commas, because a
    description may itself contain commas. Unset -> DEFAULT_PROJECTS, so a
    fresh checkout still has somewhere to file things.
    """
    raw = os.environ.get("CORTEX_PROJECTS", "").strip()
    if not raw:
        return dict(DEFAULT_PROJECTS)
    result: dict[str, str] = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        slug, _, desc = pair.partition("=")
        slug, desc = slug.strip(), desc.strip()
        if slug:
            result[slug] = desc
    return result or dict(DEFAULT_PROJECTS)


def project_slugs() -> list[str]:
    """Ordered list of configured project slugs — projects().keys()."""
    return list(projects())


def account_projects() -> dict[str, str]:
    """Gmail account alias -> default project slug, from CORTEX_ACCOUNT_PROJECTS.

    Optional. Format "alias=slug,alias=slug". Unset -> {}, in which case
    callers fall back to treating the alias itself as the project slug.
    """
    raw = os.environ.get("CORTEX_ACCOUNT_PROJECTS", "").strip()
    if not raw:
        return {}
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        alias, _, slug = pair.partition("=")
        alias, slug = alias.strip(), slug.strip()
        if alias and slug:
            result[alias] = slug
    return result


def vps_ssh_alias() -> str:
    """The SSH config alias for the VPS, from CORTEX_VPS_SSH_ALIAS.

    Unset -> "" (a personal default here would defeat the Rule 1 test).
    Callers that SSH to the VPS must treat an empty alias as "VPS checks
    skipped", never fall back to a hardcoded host.
    """
    return os.environ.get("CORTEX_VPS_SSH_ALIAS", "").strip()


def vps_repo_root() -> str:
    """Absolute path to the repository checkout on the VPS, from CORTEX_VPS_REPO_ROOT.

    Unset -> "".
    """
    return os.environ.get("CORTEX_VPS_REPO_ROOT", "").strip()


def vps_user() -> str:
    """The non-root service user the automation runs as on the VPS, from CORTEX_VPS_USER.

    Unset -> "".
    """
    return os.environ.get("CORTEX_VPS_USER", "").strip()


def clear_config_caches() -> None:
    """Clear every lru_cache-decorated CORTEX_* config helper in this module.

    A module-level constant computed at import time (e.g. `ACCOUNTS =
    _accounts()`) can capture an empty/stale value if it runs before
    integrations.registry.load_env() has loaded the dotenv file — and even a
    lazy call-time accessor stays stale across a later load_env() call
    because of @functools.lru_cache. registry.load_env() calls this right
    after loading so every cached accessor picks up what was just loaded.
    """
    for name in ("gmail_accounts", "calendar_accounts", "primary_account", "business_account", "projects"):
        fn = globals().get(name)
        cache_clear = getattr(fn, "cache_clear", None)
        if cache_clear:
            cache_clear()


def save_state(path, data):
    """Atomic JSON write — write to .tmp, then os.replace."""
    tmp_path = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


if __name__ == "__main__":
    # Smoke test
    import tempfile

    # Test file_lock
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
        test_path = tf.name
    with file_lock(test_path):
        pass
    os.unlink(test_path)
    os.unlink(f"{test_path}.lock")

    # Test save_state
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        state_path = tf.name
    save_state(state_path, {"test": True})
    loaded = json.load(open(state_path))
    assert loaded == {"test": True}
    os.unlink(state_path)

    print("shared.py: all smoke tests passed")
