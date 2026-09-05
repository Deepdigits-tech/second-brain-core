#!/usr/bin/env python3
"""Health checks for Cortex OS systems.

Two tiers:
  Tier 1 (every heartbeat, ~30 min): fast local checks, no SSH
  Tier 2 (morning brief only): deeper checks, SSH to VPS

Usage:
  python3 health_check.py              # tier 1 only
  python3 health_check.py --morning    # tier 1 + tier 2
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / ".claude" / "data"
STATE_DIR = DATA_DIR / "state"
HEARTBEAT_STATE = STATE_DIR / "heartbeat-state.json"
SYNC_LOG = PROJECT_ROOT / ".git" / "sync.log"
HEALTH_STATE = STATE_DIR / "health-state.json"

import shared
from shared import PST
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

EXPECTED_LAUNCHD_AGENTS = [
    "com.secondbrain.heartbeat",
    "com.secondbrain.sync",
    "com.secondbrain.indexer",
    "com.secondbrain.reflection",
]

SSH_TIMEOUT = 15  # seconds
# --list makes 8 Composio API calls over SSH; measured ~5.5s. A timeout here
# degrades to a silent warn, so give it real headroom.
COMPOSIO_CHECK_TIMEOUT = 45


def _ok(detail: str) -> dict:
    return {"status": "ok", "detail": detail}


def _warn(detail: str) -> dict:
    return {"status": "warn", "detail": detail}


def _critical(detail: str) -> dict:
    return {"status": "critical", "detail": detail}


def _first_unknown(output: str, limit: int = 120) -> str:
    """First UNKNOWN reason from a --list run, trimmed for a Telegram message."""
    for line in output.splitlines():
        if "UNKNOWN" in line:
            return line[line.index("UNKNOWN"):].strip()[:limit]
    return "unknown reason"


def _run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a command with timeout, capturing output."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ssh(cmd: str, timeout: int = SSH_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a command on VPS via SSH, using the alias from CORTEX_VPS_SSH_ALIAS.

    Callers must check shared.vps_ssh_alias() before reaching here — this
    function does not guard against an empty alias, so a caller that skips
    the check would ssh to a config entry named "".
    """
    return _run(["ssh", shared.vps_ssh_alias(), cmd], timeout=timeout)


# ── Tier 1: Fast local checks (every heartbeat) ──────────────────────


def check_local_duplicate() -> dict:
    """Check if chat/server.py is running locally (it shouldn't be — VPS runs it)."""
    if not IS_MAC:
        return _ok("skipped (not on Mac)")
    try:
        result = _run(["pgrep", "-f", "chat/server.py"], timeout=5)
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        if pids:
            return _critical(f"local chat server running: PID {', '.join(pids)}")
        return _ok("no local chat server")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _warn("could not check local processes")


SYNC_LOG = PROJECT_ROOT / ".git" / "sync.log"


def _last_consecutive_failure_count() -> int:
    """Read .git/sync.log and return the consecutive-failure count
    from the most recent line. Zero if the log doesn't exist, can't be read,
    or the most recent run succeeded."""
    if not SYNC_LOG.exists():
        return 0
    try:
        # Tail the last ~16KB — auto-sync logs are small per run, plenty of context
        with open(SYNC_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return 0

    last_failure_count = 0
    for line in tail.splitlines():
        # Reset on a successful sync — only consecutive failures are a problem
        if "sync complete" in line or "sync recovered" in line:
            last_failure_count = 0
            continue
        m = re.search(r"ERROR \((\d+) consecutive\)", line)
        if m:
            last_failure_count = int(m.group(1))
    return last_failure_count


def check_sync_drift() -> dict:
    """Health of the auto-sync between Mac and VPS.

    Two layers:
    1. Read .git/sync.log for consecutive-failure count. This is
       the *cause* — if sync isn't running, drift is just a symptom.
    2. Fall back to git rev-list drift if the log is unavailable.
    """
    failures = _last_consecutive_failure_count()
    if failures >= 5:
        return _critical(f"auto-sync failing: {failures} consecutive errors — pull is stuck")
    if failures >= 2:
        return _warn(f"auto-sync flaky: {failures} consecutive errors")

    try:
        _run(["git", "-C", str(PROJECT_ROOT), "fetch", "origin", "--quiet"], timeout=15)
        local = _run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], timeout=5)
        remote = _run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "origin/main"], timeout=5)

        local_sha = local.stdout.strip()
        remote_sha = remote.stdout.strip()

        if local_sha == remote_sha:
            return _ok("in sync with origin/main")

        # Count commits apart
        ahead = _run(["git", "-C", str(PROJECT_ROOT), "rev-list", "--count", "origin/main..HEAD"], timeout=5)
        behind = _run(["git", "-C", str(PROJECT_ROOT), "rev-list", "--count", "HEAD..origin/main"], timeout=5)
        ahead_n = int(ahead.stdout.strip() or "0")
        behind_n = int(behind.stdout.strip() or "0")

        detail = f"{ahead_n} ahead, {behind_n} behind origin/main"
        # Auto-sync commits every 2 min; overnight gaps produce ~60+ commits of
        # drift without any real problem. Only flag when truly stuck (>200 behind
        # = >6h gap) vs degraded (>50 = ~1.5h gap). The sync-log check above
        # catches the actual stuck-sync case earlier.
        if behind_n > 200:
            return _critical(detail)
        if behind_n > 50 or ahead_n > 10:
            return _warn(detail)
        return _ok(detail)
    except (subprocess.TimeoutExpired, Exception) as e:
        return _warn(f"sync drift check failed: {e}")


def _is_heartbeat_sleep_hours() -> bool:
    """Check if we're outside the heartbeat schedule (6 AM – 10 PM PST)."""
    now_pst = datetime.now(PST)
    return now_pst.hour < 6 or now_pst.hour >= 22


def check_heartbeat_freshness() -> dict:
    """Verify heartbeat state file was updated recently."""
    if not HEARTBEAT_STATE.exists():
        return _warn("heartbeat state file missing")
    try:
        state = json.loads(HEARTBEAT_STATE.read_text())
        ts = state.get("timestamp", "")
        if not ts:
            return _warn("heartbeat state has no timestamp")

        last_run = datetime.fromisoformat(ts)
        # Make timezone-aware if naive
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        hours_ago = (now - last_run).total_seconds() / 3600

        # During sleep hours (10 PM – 6 AM PST), don't flag stale heartbeat
        if _is_heartbeat_sleep_hours():
            return _ok(f"last heartbeat {hours_ago:.1f}h ago (outside schedule)")

        # Grace window: if we just crossed into active hours and the gap is
        # explained entirely by the overnight sleep window (~8.5h max), treat
        # as ok — the first morning run always sees a stale state.
        now_pst = datetime.now(PST)
        hours_since_active = now_pst.hour + now_pst.minute / 60  # hours past midnight
        overnight_gap = 22 - 6  # hours the scheduler sleeps (10 PM – 6 AM)
        if hours_since_active < 1.0 and hours_ago <= overnight_gap + 0.5:
            return _ok(f"last heartbeat {hours_ago:.1f}h ago (first morning run)")

        if hours_ago > 2:
            return _critical(f"last heartbeat {hours_ago:.1f}h ago")
        if hours_ago > 1:
            return _warn(f"last heartbeat {hours_ago:.1f}h ago")
        return _ok(f"last heartbeat {hours_ago:.1f}h ago")
    except (json.JSONDecodeError, ValueError) as e:
        return _warn(f"heartbeat state unreadable: {e}")


def check_sync_log_freshness() -> dict:
    """Verify sync log was modified recently (sync runs every 2 min).

    Only meaningful on the machine running the sync agent.
    On VPS, sync runs via cron and writes to a different log.
    """
    if not IS_MAC:
        return _ok("skipped (not on Mac — VPS uses cron)")
    if not SYNC_LOG.exists():
        return _warn("sync log missing (.git/sync.log)")
    try:
        mtime = SYNC_LOG.stat().st_mtime
        age_min = (time.time() - mtime) / 60

        if age_min > 10:
            return _critical(f"sync log stale ({age_min:.0f} min old)")
        if age_min > 5:
            return _warn(f"sync log {age_min:.0f} min old")
        return _ok(f"sync log {age_min:.0f} min old")
    except OSError as e:
        return _warn(f"could not stat sync log: {e}")


# ── Tier 2: Morning checks (SSH to VPS) ──────────────────────────────


def check_vps_chat_server() -> dict:
    """Check if VPS chat server systemd service is active."""
    try:
        result = _ssh("systemctl is-active secondbrain-chat")
        status = result.stdout.strip()
        if status == "active":
            return _ok("secondbrain-chat active")
        return _critical(f"secondbrain-chat status: {status}")
    except subprocess.TimeoutExpired:
        return _critical("SSH to VPS timed out")
    except Exception as e:
        return _warn(f"VPS check failed: {e}")


def check_vps_telegram_health() -> dict:
    """Scan recent VPS chat logs for Telegram 409 Conflict errors."""
    try:
        result = _ssh("journalctl -u secondbrain-chat --no-pager --since '30 min ago' 2>&1 | grep -c '409 Conflict' || true")
        count = int(result.stdout.strip() or "0")
        if count > 0:
            return _critical(f"{count} Telegram 409 Conflict errors in last 30 min")
        return _ok("no Telegram conflicts")
    except subprocess.TimeoutExpired:
        return _warn("SSH timed out checking Telegram health")
    except Exception as e:
        return _warn(f"Telegram health check failed: {e}")


def check_vps_composio_auth() -> dict:
    """Test if VPS can reach Composio and see ACTIVE connections.

    Anything short of "every connection ACTIVE" is CRITICAL, not a warn.
    Heartbeat only notifies on critical, so a warn here is silent — that is
    exactly how a rejected VPS API key went unnoticed from 2026-08-08 to
    2026-08-15 while every Gmail and Calendar call 401'd.

    The output is parsed whole. The old `| head -6` both hid later rows and
    masked the exit code, since head always exits 0.
    """
    try:
        result = _ssh(
            f"sudo -u {shared.vps_user()} bash -c 'cd {shared.vps_repo_root()} && "
            ".venv/bin/python3 .claude/scripts/composio_setup.py --list 2>&1'",
            timeout=COMPOSIO_CHECK_TIMEOUT,
        )
        output = result.stdout + result.stderr

        if "Invalid API key" in output or "APIKey_InvalidAPIKey" in output:
            return _critical(
                "VPS Composio API key rejected (401) — Gmail/Calendar blind. "
                "Update COMPOSIO_API_KEY in the VPS .claude/.env"
            )
        if "UNKNOWN" in output:
            return _critical(f"VPS Composio status unverifiable: {_first_unknown(output)}")
        if "MISSING" in output:
            missing = output.count("MISSING")
            return _critical(
                f"VPS Composio: {missing} connection(s) MISSING — re-run composio_setup.py --urls"
            )
        if "ACTIVE" in output:
            return _ok(f"Composio connections active ({output.count('ACTIVE')})")
        if result.returncode != 0:
            return _critical(f"VPS Composio check failed (exit {result.returncode})")
        return _warn("VPS Composio check produced no recognizable output")
    except subprocess.TimeoutExpired:
        # Transient blip. A genuinely unreachable VPS is caught by check_vps_chat_server.
        return _warn("SSH timed out checking Composio auth")
    except Exception as e:
        return _warn(f"Composio auth check failed: {e}")


def check_claude_auth() -> dict:
    """Catch an expired Claude token before a day of briefs is lost.

    CLAUDE_CODE_OAUTH_TOKEN is a subscription token: it lasts a year and never
    renews itself, so this failure is inevitable rather than exceptional. When
    it lands, data gathering keeps working and only the LLM steps die — the
    system looks alive while producing nothing, which is how 2026-08-15 lost a
    day of morning briefs before anyone noticed.

    Reads the errors already logged by feature_run rather than spending a token
    on a probe call: the heartbeat runs every 30 minutes, and a probe per tick
    would cost more than the checks it guards.
    """
    cutoff = (datetime.now(PST) - timedelta(hours=6)).isoformat()
    log = PROJECT_ROOT / ".claude" / "data" / "heartbeat_errors.log"

    # A missing or unreadable log is not an all-clear — fall through to the
    # capture queue, which is the signal that exists on a fresh machine.
    recent: list[str] = []
    if log.exists():
        try:
            recent = log.read_text(errors="ignore").splitlines()[-40:]
        except OSError:
            recent = []

    fresh = [ln for ln in recent if ln[1:26] > cutoff]

    for line in reversed(fresh):
        low = line.lower()
        if "oauth" in low and "expire" in low:
            return _critical(
                "Claude token expired — every LLM step is failing (briefs, "
                "reflection, capture summaries). Run `claude setup-token` and "
                "update CLAUDE_CODE_OAUTH_TOKEN in .claude/.env"
            )
        if "not logged in" in low:
            return _critical(
                "Claude CLI not logged in — every LLM step is failing. "
                "Run `claude setup-token` and update CLAUDE_CODE_OAUTH_TOKEN"
            )
        if "hit your limit" in low:
            return _warn(
                "Claude subscription usage limit reached — LLM steps paused "
                "until it resets"
            )
        if "authentication_error" in low or "401" in low:
            return _critical(f"Claude auth rejected: {line[-120:]}")

    # The capture queue is the faster signal: it drains every tick, records the
    # LLM's own error text per item, and runs outside active hours — so it sees
    # an expiry hours before the next brief would have.
    return _check_capture_auth_errors()


def _check_capture_auth_errors() -> dict:
    """Look for auth failures recorded against recent captures.

    Omitted (not warned) when the captures module isn't installed — this is a
    bonus signal on top of another check, not a dependency of the health run.
    """
    from shared import lane_available
    if not lane_available("captures"):
        return _ok("captures module not installed — auth-error scan skipped")
    try:
        sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
        import captures

        cutoff = (datetime.now(PST) - timedelta(hours=6)).isoformat()
        for item in captures.load_all():
            if item.get("updated_at", "") < cutoff:
                continue
            err = (item.get("error") or "").lower()
            if "oauth" in err and "expire" in err:
                return _critical(
                    "Claude token expired — captures are saving without summaries "
                    "and every other LLM step is failing too. Run `claude "
                    "setup-token` and update CLAUDE_CODE_OAUTH_TOKEN in .claude/.env"
                )
            if "not logged in" in err:
                return _critical(
                    "Claude CLI not logged in — captures are saving without "
                    "summaries. Run `claude setup-token`"
                )
    except Exception:
        # The capture queue is a bonus signal, not a dependency — a missing or
        # malformed queue must not take the health check down with it.
        pass

    return _ok("No Claude auth errors in the last 6h")


def check_vps_sync_cron() -> dict:
    """Verify VPS cron entry for vault sync exists."""
    try:
        result = _ssh(f"crontab -l -u {shared.vps_user()} 2>&1")
        if "vault_sync" in result.stdout:
            return _ok("VPS sync cron present")
        return _critical("VPS sync cron missing")
    except subprocess.TimeoutExpired:
        return _warn("SSH timed out checking VPS cron")
    except Exception as e:
        return _warn(f"VPS cron check failed: {e}")


def check_local_launchd() -> dict:
    """Verify all 4 launchd agents are loaded."""
    if not IS_MAC:
        return _ok("skipped (not on Mac)")
    try:
        result = _run(["launchctl", "list"], timeout=5)
        loaded = []
        missing = []
        for agent in EXPECTED_LAUNCHD_AGENTS:
            if agent in result.stdout:
                loaded.append(agent.replace("com.secondbrain.", ""))
            else:
                missing.append(agent.replace("com.secondbrain.", ""))

        if missing:
            return _critical(f"launchd agents missing: {', '.join(missing)}")
        return _ok(f"all {len(loaded)} agents loaded")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _warn("could not check launchd agents")


def check_reflection_freshness() -> dict:
    """Verify a daily log exists for yesterday (reflection should have written to it)."""
    yesterday = (datetime.now(PST) - timedelta(days=1)).strftime("%Y-%m-%d")
    daily_path = PROJECT_ROOT / "vault" / "daily" / f"{yesterday}.md"

    if not daily_path.exists():
        return _warn(f"no daily log for {yesterday}")

    content = daily_path.read_text()
    if len(content.strip()) < 50:
        return _warn(f"daily log for {yesterday} is nearly empty ({len(content)} chars)")
    return _ok(f"daily log for {yesterday} exists ({len(content)} chars)")


def check_gmail_accounts_configured() -> dict:
    """Warn when CORTEX_GMAIL_ACCOUNTS is unset — the inbox lane is disabled.

    Mirrors the CORTEX_VPS_SSH_ALIAS gate in run_tier2_checks: a missing
    generic-default env var degrades with a warn, never an exception.
    """
    if not shared.gmail_accounts():
        return _warn("CORTEX_GMAIL_ACCOUNTS not set")
    return _ok("CORTEX_GMAIL_ACCOUNTS configured")


# ── Runners ───────────────────────────────────────────────────────────


def run_tier1_checks() -> dict:
    """Fast checks — every heartbeat."""
    checks = {}
    for name, fn in [
        ("local_duplicate", check_local_duplicate),
        ("sync_drift", check_sync_drift),
        ("heartbeat_freshness", check_heartbeat_freshness),
        ("sync_log_freshness", check_sync_log_freshness),
        # Tier 1, not tier 2: an expired token silently kills every LLM step,
        # so waiting for the morning brief to notice costs a day of output —
        # and the morning brief is itself one of the casualties.
        ("claude_auth", check_claude_auth),
        ("gmail_accounts_configured", check_gmail_accounts_configured),
    ]:
        try:
            checks[name] = fn()
        except Exception as e:
            checks[name] = _warn(f"check crashed: {e}")
    return checks


def run_tier2_checks() -> dict:
    """Deep checks — morning brief only.

    SSH-based VPS checks are skipped when running on Linux (i.e. on the VPS
    itself — its own SSH alias to itself normally isn't configured), and
    skipped with a warn (never an ssh call) unless all three of
    CORTEX_VPS_SSH_ALIAS, CORTEX_VPS_REPO_ROOT and CORTEX_VPS_USER are set —
    those are the generic defaults, so a fresh checkout without full VPS
    config must degrade instead of erroring. Naming only the alias used to
    let a checkout with an alias but no repo root / user pass this gate and
    fail deeper in, in a check whose error is less obvious.
    """
    checks = {}
    vps_names = ("vps_chat_server", "vps_telegram_health", "vps_composio_auth", "vps_sync_cron")
    vps_checks = [
        ("vps_chat_server", check_vps_chat_server),
        ("vps_telegram_health", check_vps_telegram_health),
        ("vps_composio_auth", check_vps_composio_auth),
        ("vps_sync_cron", check_vps_sync_cron),
    ]
    local_checks = [
        ("local_launchd", check_local_launchd),
        ("reflection_freshness", check_reflection_freshness),
    ]
    missing = [
        var
        for var, val in (
            ("CORTEX_VPS_SSH_ALIAS", shared.vps_ssh_alias()),
            ("CORTEX_VPS_REPO_ROOT", shared.vps_repo_root()),
            ("CORTEX_VPS_USER", shared.vps_user()),
        )
        if not val
    ]
    if IS_MAC and missing:
        detail = f"{', '.join(missing)} not set — VPS checks skipped"
        for name in vps_names:
            checks[name] = _warn(detail)
        selected = local_checks
    else:
        # Only run SSH-based VPS checks from Mac, and only once all three
        # required VPS settings are configured.
        selected = (vps_checks if IS_MAC else []) + local_checks
    for name, fn in selected:
        try:
            checks[name] = fn()
        except Exception as e:
            checks[name] = _warn(f"check crashed: {e}")
    return checks


def run_all_checks(morning: bool = False) -> dict:
    """Run tier 1 always, tier 2 if morning=True.

    Returns:
        {
            "timestamp": ISO string,
            "status": "healthy" | "degraded" | "critical",
            "checks": {name: {"status": ..., "detail": ...}, ...},
            "alerts": [list of alert strings],
        }
    """
    checks = run_tier1_checks()
    if morning:
        checks.update(run_tier2_checks())

    # Determine overall status
    alerts = []
    has_critical = False
    has_warn = False

    for name, result in checks.items():
        if result["status"] == "critical":
            has_critical = True
            alerts.append(f"{name}: {result['detail']}")
        elif result["status"] == "warn":
            has_warn = True
            alerts.append(f"{name}: {result['detail']}")

    if has_critical:
        overall = "critical"
    elif has_warn:
        overall = "degraded"
    else:
        overall = "healthy"

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": overall,
        "checks": checks,
        "alerts": alerts,
    }

    # Persist health state
    try:
        sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
        from shared import save_state
        save_state(str(HEALTH_STATE), report)
    except Exception:
        pass

    return report


# ── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    morning = "--morning" in sys.argv or "--full" in sys.argv
    report = run_all_checks(morning=morning)
    print(json.dumps(report, indent=2))
