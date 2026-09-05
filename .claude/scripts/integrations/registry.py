"""Integration availability checker for Cortex OS."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
ENV_PATH = REPO_ROOT / ".claude" / ".env"

# Composio toolkit slug per integration name
_COMPOSIO_TOOLKITS = {"gmail": "gmail", "gcal": "googlecalendar"}

# Cache of toolkit -> (fetched_at, set_of_user_ids_with_ACTIVE_connection).
# A None set means "the lookup failed" — distinct from "no accounts connected".
_ACTIVE_CACHE: dict[str, tuple[float, set[str] | None]] = {}
_ACTIVE_CACHE_TTL = 300.0


def load_env() -> None:
    """Load environment variables from .claude/.env.

    Also clears shared.py's lru_cache-decorated CORTEX_* accessors — several
    call sites read the environment before this has run (e.g. a module-level
    constant at import time), and even a lazy accessor stays stale across a
    later call to this because of the cache. Without this, a first load_env()
    call anywhere in the process permanently freezes those accessors' values.
    """
    load_dotenv(ENV_PATH)
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import shared
    shared.clear_config_caches()


# Lanes whose module is an optional dependency of this repo, not the
# shareable core — registered only when their module actually imports.
# Uses shared.optional_import so an extracted/stripped tree (or one with the
# module deliberately disabled via CORTEX_DISABLE_MODULES) reports them
# absent instead of raising ImportError deep inside a checker.
_OPTIONAL_LANE_MODULES = {
    "smartlead": "integrations.smartlead",
    "garmin": "integrations.garmin",
}


def _optional_lanes_present() -> set[str]:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import shared

    return {
        name
        for name, module_name in _OPTIONAL_LANE_MODULES.items()
        if shared.optional_import(module_name) is not None
    }


def check_integration(name: str, verify: bool = True) -> dict:
    """Check if a specific integration is available.

    Returns {"available": bool, "reason": str}, plus an "accounts" map for the
    Composio-backed integrations. With verify=True (default) the Composio checks
    query the API for ACTIVE connections instead of trusting the API key alone.
    """
    checkers = {
        "telegram": _check_telegram,
        "slack": _check_slack,
        "smartlead": _check_smartlead,
        "garmin": _check_garmin,
    }
    if name in _COMPOSIO_TOOLKITS:
        return _check_composio(name, verify=verify)
    if name in _OPTIONAL_LANE_MODULES and name not in _optional_lanes_present():
        return {"available": False, "reason": f"{_OPTIONAL_LANE_MODULES[name]} not present"}
    checker = checkers.get(name)
    if checker is None:
        return {"available": False, "reason": f"Unknown integration: {name}"}
    return checker()


def check_all(verify: bool = True) -> dict[str, dict]:
    """Check all integrations. Returns {name: {available, reason}}.

    Garmin and smartlead are included only when their module is actually
    importable — they are optional dependencies of this repo, not part of the
    shareable core.
    """
    names = ["gmail", "gcal", "telegram", "slack"]
    names.extend(sorted(_optional_lanes_present()))
    return {name: check_integration(name, verify=verify) for name in names}


def _account_user_ids() -> dict[str, str]:
    """Account alias -> Composio user_id. Single source of truth lives in gmail.py."""
    try:
        from integrations.gmail import ACCOUNT_USER_IDS

        return dict(ACCOUNT_USER_IDS)
    except Exception:
        return {}


def _field(obj, name):
    """Read a field off an SDK model or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _active_user_ids(toolkit: str) -> set[str] | None:
    """user_ids with an ACTIVE connection for toolkit, or None if unverifiable."""
    cached = _ACTIVE_CACHE.get(toolkit)
    if cached and (time.time() - cached[0]) < _ACTIVE_CACHE_TTL:
        return cached[1]

    result: set[str] | None
    try:
        from composio import Composio

        client = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
        resp = client.connected_accounts.list(toolkit_slugs=[toolkit], statuses=["ACTIVE"])

        # Re-check status and toolkit locally rather than trusting the server-side
        # filter. Composio keeps every past attempt, so a single EXPIRED or
        # INITIATED leftover slipping through would read as a live connection.
        result = set()
        unreadable = 0
        for item in getattr(resp, "items", None) or []:
            status = _field(item, "status")
            if status is None:
                unreadable += 1
                continue
            if str(status).upper() != "ACTIVE":
                continue
            item_toolkit = _field(_field(item, "toolkit"), "slug")
            if item_toolkit is not None and str(item_toolkit).lower() != toolkit.lower():
                continue
            uid = _field(item, "user_id")
            if uid:
                result.add(uid)

        if unreadable and not result:
            # Nothing had a readable status. That is "cannot verify", not "nothing
            # is connected" — reporting an empty set here would flip gmail to
            # unavailable and silently stop all inbox monitoring.
            result = None
    except Exception:
        # Network blip, SDK change, transient 5xx. Report "unknown", never "broken" —
        # heartbeat gates email gathering on this and must not go dark on a flaky call.
        result = None

    _ACTIVE_CACHE[toolkit] = (time.time(), result)
    return result


def _check_composio(name: str, verify: bool = True) -> dict:
    """Availability for a Composio-backed integration (gmail / gcal)."""
    toolkit = _COMPOSIO_TOOLKITS[name]
    if not os.environ.get("COMPOSIO_API_KEY"):
        return {"available": False, "reason": "COMPOSIO_API_KEY not set", "accounts": {}}
    if not verify or os.environ.get("CORTEX_VERIFY_CONNECTIONS") == "0":
        return {"available": True, "reason": "Composio API key configured (unverified)", "accounts": {}}

    active = _active_user_ids(toolkit)
    if active is None:
        return {
            "available": True,
            "reason": "Composio API key configured (connection check failed, assuming available)",
            "accounts": {},
        }

    accounts = {alias: (user_id in active) for alias, user_id in _account_user_ids().items()}
    live = sorted(a for a, ok in accounts.items() if ok)
    dead = sorted(a for a, ok in accounts.items() if not ok)

    if not live:
        return {
            "available": False,
            "reason": f"no ACTIVE {toolkit} connection (run composio_setup.py --urls)",
            "accounts": accounts,
        }
    reason = f"{len(live)}/{len(accounts)} accounts ACTIVE: {', '.join(live)}"
    if dead:
        reason += f" — missing: {', '.join(dead)}"
    return {"available": True, "reason": reason, "accounts": accounts}


def _check_telegram() -> dict:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return {"available": False, "reason": "TELEGRAM_BOT_TOKEN not set"}
    if not os.environ.get("TELEGRAM_CHAT_ID"):
        return {"available": False, "reason": "TELEGRAM_CHAT_ID not set"}
    return {"available": True, "reason": "Bot token and chat ID configured"}


def _check_slack() -> dict:
    if not os.environ.get("SLACK_BOT_TOKEN"):
        return {"available": False, "reason": "SLACK_BOT_TOKEN not set"}
    if not os.environ.get("SLACK_APP_TOKEN"):
        return {"available": False, "reason": "SLACK_APP_TOKEN not set"}
    return {"available": True, "reason": "Bot token and app token configured"}


def _check_smartlead() -> dict:
    if not (os.environ.get("SMARTLEAD_API_KEY") or os.environ.get("smartlead_api_key")):
        return {"available": False, "reason": "SMARTLEAD_API_KEY not set"}
    return {"available": True, "reason": "API key configured"}


def _check_garmin() -> dict:
    token_dir = REPO_ROOT / ".claude" / "data" / "auth" / "garmin"
    if token_dir.exists() and any(token_dir.iterdir()):
        return {"available": True, "reason": "garth tokens cached"}
    if os.environ.get("GARMIN_EMAIL") and os.environ.get("GARMIN_PASSWORD"):
        return {"available": True, "reason": "credentials configured (no cached tokens yet)"}
    return {"available": False, "reason": "GARMIN_EMAIL / GARMIN_PASSWORD not set and no cached tokens"}


if __name__ == "__main__":
    load_env()
    status = check_all()
    for name, info in status.items():
        icon = "✓" if info["available"] else "✗"
        print(f"  {icon} {name}: {info['reason']}")
        for alias, ok in sorted(info.get("accounts", {}).items()):
            print(f"      {'✓' if ok else '✗'} {alias}")
