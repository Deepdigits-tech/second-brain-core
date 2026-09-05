#!/usr/bin/env python3
"""Composio connection setup for Cortex OS.

One-time (per account) interactive OAuth flow. Idempotent — skips already-
connected accounts. Reads COMPOSIO_API_KEY from .claude/.env.

Usage:
    .venv/bin/python3 .claude/scripts/composio_setup.py               # connect all
    .venv/bin/python3 .claude/scripts/composio_setup.py --test        # test calls only
    .venv/bin/python3 .claude/scripts/composio_setup.py --list        # show status
    .venv/bin/python3 .claude/scripts/composio_setup.py --urls        # print OAuth URLs
    .venv/bin/python3 .claude/scripts/composio_setup.py --auth-configs  # list auth configs

Account map (Rule 1: no personal account id lives in this file):
    Gmail account aliases and their Composio user_ids come from
    CORTEX_GMAIL_ACCOUNTS ("alias=user_id,alias=user_id" — same variable
    shared.gmail_accounts() reads). Calendar toolkit is added for whichever
    aliases are in CORTEX_CALENDAR_ACCOUNTS (default: all Gmail aliases).

    Per-account Composio connection ids (auth configs) are Composio-side
    state, not code. They come from CORTEX_COMPOSIO_CONNECTIONS
    ("alias=gmail_conn_id:gcal_conn_id,..."). Run --auth-configs to list
    what exists in the project before filling that variable in.

Why auth configs are pinned explicitly: `toolkits.authorize()` takes only
(user_id, toolkit) — with several auth configs registered for one toolkit it
picks one on its own, so you cannot tell which OAuth app a connection was made
through and reconnects are not reproducible. `connected_accounts.initiate()`
takes an explicit auth_config_id, so re-running this script always rebuilds the
same connection shape. An auth config is an OAuth *app*, not a mailbox binding —
several accounts can legitimately share one (e.g. one calendar auth config
shared across every Gmail alias).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = REPO_ROOT / ".claude" / ".env"

sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import shared  # noqa: E402

# user_id → {toolkit: auth_config_id}. Populated by _load_accounts() in main(),
# after .env is loaded, so CORTEX_GMAIL_ACCOUNTS / CORTEX_COMPOSIO_CONNECTIONS
# are visible. Left as an empty dict at import time so tests can monkeypatch it
# directly without needing any env vars set.
ACCOUNTS: dict[str, dict[str, str]] = {}


def _load_connections() -> dict[str, tuple[str, str]]:
    """alias -> (gmail_conn_id, gcal_conn_id), from CORTEX_COMPOSIO_CONNECTIONS.

    Format: "alias=gmail_id:gcal_id,alias2=gmail_id2:gcal_id2". Composio
    connection ids are account state, not code — this env var is the static
    fallback --urls/--connect need to pin a specific auth config per account
    instead of letting Composio pick one. A missing or malformed pair yields
    empty ids, handled the same way a never-configured account was before:
    _initiate() reports it and tells the operator where to set it.
    """
    raw = os.environ.get("CORTEX_COMPOSIO_CONNECTIONS", "").strip()
    out: dict[str, tuple[str, str]] = {}
    if not raw:
        return out
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        alias, _, ids = pair.partition("=")
        alias = alias.strip()
        gmail_id, _, cal_id = ids.strip().partition(":")
        out[alias] = (gmail_id.strip(), cal_id.strip())
    return out


def _load_accounts() -> dict[str, dict[str, str]]:
    """Build the user_id -> {toolkit: auth_config_id} map from configuration.

    Gmail accounts and their Composio user_ids come from
    shared.gmail_accounts(). Calendar toolkit is added only for aliases in
    shared.calendar_accounts(). Connection ids come from
    _load_connections() — an alias with no entry there gets an empty
    auth_config_id per toolkit, which _initiate() reports rather than
    silently skipping.
    """
    gmail = shared.gmail_accounts()
    cal_aliases = set(shared.calendar_accounts())
    connections = _load_connections()

    accounts: dict[str, dict[str, str]] = {}
    for alias, user_id in gmail.items():
        gmail_id, cal_id = connections.get(alias, ("", ""))
        toolkits: dict[str, str] = {"gmail": gmail_id}
        if alias in cal_aliases:
            toolkits["googlecalendar"] = cal_id
        accounts[user_id] = toolkits
    return accounts


def _account_domain(user_id: str) -> str | None:
    """The email domain for a user_id that looks like an email address, else None."""
    if "@" not in user_id:
        return None
    return user_id.split("@", 1)[1] or None


def _account_label(user_id: str) -> str:
    """Friendly label for OAuth instructions: the account's email domain if
    the user_id looks like an email address, else the raw user_id."""
    return _account_domain(user_id) or user_id


def _load_client():
    load_dotenv(ENV_PATH)
    api_key = os.environ.get("COMPOSIO_API_KEY")
    if not api_key:
        print(f"ERROR: COMPOSIO_API_KEY not set in {ENV_PATH}", file=sys.stderr)
        sys.exit(1)
    from composio import Composio
    return Composio(api_key=api_key)


def _initiate(composio, user_id: str, toolkit: str):
    """Start an OAuth flow pinned to this account's auth config.

    Returns the ConnectionRequest, or None if the config is missing/mismatched.
    """
    auth_config_id = ACCOUNTS[user_id][toolkit]
    if not auth_config_id:
        print(f"  ERROR: no auth_config_id set for {user_id}/{toolkit}", file=sys.stderr)
        print(
            "  Run --auth-configs to list available configs, then add "
            f"{user_id}/{toolkit} to CORTEX_COMPOSIO_CONNECTIONS.",
            file=sys.stderr,
        )
        return None

    # Catch a config id pasted against the wrong toolkit before burning an OAuth round-trip.
    try:
        cfg = composio.auth_configs.get(auth_config_id)
        cfg_toolkit = getattr(getattr(cfg, "toolkit", None), "slug", None)
        if cfg_toolkit and cfg_toolkit != toolkit:
            print(
                f"  ERROR: {auth_config_id} is a '{cfg_toolkit}' config, expected '{toolkit}'",
                file=sys.stderr,
            )
            return None
    except Exception as e:
        print(f"  WARN: could not verify auth config {auth_config_id}: {e}", file=sys.stderr)

    try:
        return composio.connected_accounts.initiate(user_id=user_id, auth_config_id=auth_config_id)
    except Exception as e:
        print(f"  ERROR: initiate failed: {e}", file=sys.stderr)
        return None


def cmd_auth_configs(composio) -> int:
    """List every auth config in the project, so ACCOUNTS can be filled in correctly."""
    try:
        resp = composio.auth_configs.list()
    except Exception as e:
        print(f"ERROR: could not list auth configs: {e}", file=sys.stderr)
        return 1

    items = getattr(resp, "items", None) or []
    in_use = {cfg for toolkits in ACCOUNTS.values() for cfg in toolkits.values()}
    print(f"Auth configs ({len(items)}):\n")
    for it in items:
        cid = getattr(it, "id", "?")
        toolkit = getattr(getattr(it, "toolkit", None), "slug", "?")
        marker = "  ← in use" if cid in in_use else ""
        print(f"  {cid:20s} {toolkit:16s} {getattr(it, 'name', '')}{marker}")

    missing = in_use - {getattr(it, "id", None) for it in items}
    if missing:
        print(f"\nWARN: referenced in ACCOUNTS but not found in project: {', '.join(sorted(missing))}")
        return 1
    return 0


ACTIVE = "ACTIVE"
MISSING = "MISSING"


def _attr(obj, name):
    """Read a field off an SDK model or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _connection_status(composio, user_id: str, toolkit: str) -> str:
    """Single source of truth for "is this account × toolkit usable?".

    Returns ACTIVE, MISSING, or "UNKNOWN: <reason>". Every command routes its
    skip decision through this, so --list and --urls cannot drift apart.

    Only ACTIVE means connected. The server-side `statuses` filter is re-checked
    locally on each returned item because Composio keeps every past connection
    attempt around — this account alone carries a pile of EXPIRED and INITIATED
    gmail records. If that filter ever stops applying, trusting the row count
    would make a dead record look live, --urls would skip the account, and
    Gmail calls would 404 with ActionExecute_ConnectedAccountNotFound.
    """
    try:
        resp = composio.connected_accounts.list(
            user_ids=[user_id],
            toolkit_slugs=[toolkit],
            statuses=[ACTIVE],
        )
    except Exception as e:
        return f"UNKNOWN: {e}"

    for item in getattr(resp, "items", None) or []:
        if str(_attr(item, "status") or "").upper() != ACTIVE:
            continue
        item_user = _attr(item, "user_id")
        if item_user is not None and item_user != user_id:
            continue
        item_toolkit = _attr(_attr(item, "toolkit"), "slug")
        if item_toolkit is not None and item_toolkit.lower() != toolkit.lower():
            continue
        return ACTIVE
    return MISSING


def _is_connected(composio, user_id: str, toolkit: str) -> bool:
    """True only for a verified ACTIVE connection."""
    return _connection_status(composio, user_id, toolkit) == ACTIVE


def cmd_list(composio) -> int:
    """Show connection status for all accounts × toolkits."""
    print(f"Connected account status:\n")
    width = max((len(uid) for uid in ACCOUNTS), default=22)
    all_ok = True
    for user_id, toolkits in ACCOUNTS.items():
        for toolkit in toolkits:
            status = _connection_status(composio, user_id, toolkit)
            if status == ACTIVE:
                label = "✓ ACTIVE"
            elif status == MISSING:
                label = "✗ MISSING"
                all_ok = False
            else:
                label = f"? {status}"
                all_ok = False
            print(f"  {user_id:<{width}s} {toolkit:18s} {label}")
    return 0 if all_ok else 1


def cmd_urls(composio) -> int:
    """Generate OAuth URLs for all missing connections and print them. No waiting.

    Never aborts partway and never skips a pair quietly: the operator treats this
    output as the complete to-do list, so an account dropped from it silently
    becomes an integration that 404s forever.
    """
    any_missing = False
    degraded = False
    failures: list[tuple[str, str, str]] = []
    for user_id, toolkits in ACCOUNTS.items():
        for toolkit in toolkits:
            print(f"\n── {user_id} / {toolkit} ──")
            status = _connection_status(composio, user_id, toolkit)
            if status == ACTIVE:
                print("  already connected, skipping.")
                continue
            if status != MISSING:
                # Unverifiable status. Offer the URL anyway — a link the operator
                # does not need costs nothing, a missing one costs a dead inbox.
                print(f"  WARN: could not verify status ({status}) — generating a URL anyway.")
                degraded = True

            print("  generating OAuth URL...")
            req = _initiate(composio, user_id, toolkit)
            redirect_url = getattr(req, "redirect_url", None) if req is not None else None
            if not redirect_url:
                reason = "initiate failed" if req is None else "no redirect_url in response"
                if req is not None:
                    print(f"  ERROR: {reason}", file=sys.stderr)
                    print(f"  raw response: {req}", file=sys.stderr)
                print(f"  ✗ NO URL GENERATED — {reason}")
                failures.append((user_id, toolkit, reason))
                continue

            print(f"\n  👉 OPEN IN BROWSER (sign in with {_account_label(user_id)} Google account):")
            print(f"  {redirect_url}")
            print(f"  connection: {getattr(req, 'id', '?')}  auth config: {ACCOUNTS[user_id][toolkit]}\n")
            any_missing = True

    if failures:
        print("\n✗ NOT SET UP — could not generate an OAuth URL for:")
        for user_id, toolkit, reason in failures:
            print(f"    {user_id} / {toolkit} — {reason}")
        print("  Fix the errors above and re-run --urls for these accounts.")

    if any_missing:
        print("\nComplete all OAuth flows above, then run:")
        print("  .venv/bin/python3 .claude/scripts/composio_setup.py --list")
        print("to verify all connections are ACTIVE.")
    elif not failures:
        print("\n✓ All accounts already connected.")

    if degraded:
        print("\nWARN: at least one status check failed — re-run --list to confirm the real state.")
    # Non-zero on anything unresolved, so a caller cannot read a broken run as clean.
    return 1 if (failures or degraded) else 0


def cmd_connect(composio) -> int:
    """Walk through OAuth connection for any missing account × toolkit."""
    for user_id, toolkits in ACCOUNTS.items():
        for toolkit in toolkits:
            print(f"\n── {user_id} / {toolkit} ──")
            status = _connection_status(composio, user_id, toolkit)
            if status == ACTIVE:
                print("  already connected, skipping.")
                continue
            if status != MISSING:
                print(f"  WARN: could not verify status ({status}) — connecting anyway.")

            print("  initiating OAuth flow...")
            req = _initiate(composio, user_id, toolkit)
            if req is None:
                return 1

            redirect_url = getattr(req, "redirect_url", None)
            if not redirect_url:
                print(f"  ERROR: no redirect_url in response", file=sys.stderr)
                print(f"  raw response: {req}", file=sys.stderr)
                return 1

            print(f"\n  👉 OPEN THIS URL IN YOUR BROWSER:\n  {redirect_url}\n")
            print(f"  Sign in with the {_account_label(user_id)} Google account and grant access.")
            print(f"  Waiting for connection (up to 5 min)...")

            try:
                req.wait_for_connection(timeout=300)
                print("  ✓ connected.")
            except Exception as e:
                print(f"  ERROR: connection wait failed: {e}", file=sys.stderr)
                return 1

    print("\n✓ All accounts connected.")
    return 0


def cmd_test(composio) -> int:
    """Run a smoke-test Gmail call for each connected account."""
    print("Running smoke tests:\n")
    all_ok = True
    for user_id, toolkits in ACCOUNTS.items():
        if "gmail" not in toolkits:
            continue
        print(f"── {user_id} / GMAIL_FETCH_EMAILS ──")
        try:
            result = composio.tools.execute(
                "GMAIL_FETCH_EMAILS",
                arguments={"user_id": "me", "q": "is:unread", "maxResults": 1},
                user_id=user_id,
                dangerously_skip_version_check=True,
            )
            successful = getattr(result, "successful", None)
            if successful is None and isinstance(result, dict):
                successful = result.get("successful")
            if successful:
                data = getattr(result, "data", None) or (result.get("data") if isinstance(result, dict) else {})
                n = len((data or {}).get("messages", []) or [])
                print(f"  ✓ returned {n} message(s)")
            else:
                err = getattr(result, "error", None) or (result.get("error") if isinstance(result, dict) else "unknown")
                print(f"  ✗ failed: {err}")
                all_ok = False
        except Exception as e:
            print(f"  ✗ exception: {e}")
            all_ok = False
    return 0 if all_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Composio setup for Cortex OS")
    parser.add_argument("--list", action="store_true", help="show connection status only")
    parser.add_argument("--test", action="store_true", help="run smoke test calls only")
    parser.add_argument("--urls", action="store_true", help="print OAuth URLs for missing connections (no waiting)")
    parser.add_argument("--auth-configs", action="store_true", help="list auth configs in the project")
    parser.add_argument("--account", help="limit to one user_id (e.g. work)")
    args = parser.parse_args()

    composio = _load_client()

    if not os.environ.get("CORTEX_COMPOSIO_CONNECTIONS", "").strip():
        print(
            "WARNING: CORTEX_COMPOSIO_CONNECTIONS is not set — every account/toolkit "
            "will report no auth_config_id. Run --auth-configs to list available "
            "configs, then set CORTEX_COMPOSIO_CONNECTIONS.",
            file=sys.stderr,
        )

    global ACCOUNTS
    ACCOUNTS = _load_accounts()

    if args.account:
        if args.account not in ACCOUNTS:
            print(f"ERROR: unknown account '{args.account}'. Known: {', '.join(ACCOUNTS)}", file=sys.stderr)
            return 1
        for user_id in list(ACCOUNTS):
            if user_id != args.account:
                del ACCOUNTS[user_id]

    if args.auth_configs:
        return cmd_auth_configs(composio)
    if args.list:
        return cmd_list(composio)
    if args.test:
        return cmd_test(composio)
    if args.urls:
        return cmd_urls(composio)
    # Default: connect missing accounts, then test
    rc = cmd_connect(composio)
    if rc != 0:
        return rc
    print()
    return cmd_test(composio)


if __name__ == "__main__":
    sys.exit(main())
