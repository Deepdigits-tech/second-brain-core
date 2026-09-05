#!/bin/bash
# Cortex OS vault sync — runs every 2 minutes via launchd (Mac) / cron (VPS)
set -uo pipefail

# Derive repo root from this script's location (.claude/scripts/vault_sync.sh → repo root)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG="$REPO_DIR/.git/sync.log"  # per checkout, never tracked

# Paths auto-sync will NOT commit. Source code needs an explicit commit with a
# real message; everything else (vault, .agents, skills, tracked state) syncs
# automatically. Changes here are left in the working tree and logged, never
# discarded. See tests/test_vault_sync.py.
AUTO_SYNC_EXCLUDE=".claude/scripts .claude/hooks tests"
STATE_DIR="$REPO_DIR/.claude/data/state"
FAIL_STATE="$STATE_DIR/sync-failures.txt"
NOTIFY_THRESHOLD=5  # consecutive failures before alerting

mkdir -p "$STATE_DIR"
cd "$REPO_DIR"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $1" >> "$LOG"
}

# Fire a Telegram + macOS notification when sync has been failing repeatedly.
# Runs only once per failure-escalation boundary (threshold, then every 15th).
notify_sync_failure() {
    local failures="$1"
    local detail="$2"
    if [ -x "$REPO_DIR/.venv/bin/python3" ]; then
        "$REPO_DIR/.venv/bin/python3" - <<PYEOF 2>>"$LOG" || true
import sys
sys.path.insert(0, "$REPO_DIR/.claude/scripts")
try:
    # telegram_bot only loads .claude/.env under __main__, so an importing
    # caller gets no credentials and notify() silently returns telegram=False.
    # That is why 66 consecutive sync failures raised a desktop popup and
    # never reached the owner's phone.
    from integrations.registry import load_env
    load_env()
    from notify import notify
    notify(
        "Cortex sync stuck",
        f"vault_sync.sh has failed $failures consecutive times. Latest: $detail. Run: cd $REPO_DIR && git status"
    )
except Exception as e:
    print(f"notify failed: {e}", file=sys.stderr)
PYEOF
    fi
}

# Tell the owner once when sync goes quiet because of a branch checkout. A log line
# in /tmp is not a notification: the 2026-08-16 outage ran for a day before
# anyone noticed the two machines had stopped talking.
notify_branch_pause() {
    local branch="$1"
    if [ -x "$REPO_DIR/.venv/bin/python3" ]; then
        "$REPO_DIR/.venv/bin/python3" - <<PYEOF 2>>"$LOG" || true
import sys
sys.path.insert(0, "$REPO_DIR/.claude/scripts")
try:
    # telegram_bot only loads .claude/.env under __main__, so an importing
    # caller gets no credentials and notify() silently returns telegram=False.
    # That is why 66 consecutive sync failures raised a desktop popup and
    # never reached the owner's phone.
    from integrations.registry import load_env
    load_env()
    from notify import notify
    notify(
        "Cortex sync paused",
        "Checked out '$branch', so vault sync between Mac and VPS is paused. "
        "Content written on either machine will not reach the other until you "
        "are back on main."
    )
except Exception as e:
    print(f"notify failed: {e}", file=sys.stderr)
PYEOF
    fi
}

record_failure() {
    local detail="$1"
    local count=0
    if [ -f "$FAIL_STATE" ]; then
        count=$(cat "$FAIL_STATE" 2>/dev/null || echo 0)
    fi
    count=$((count + 1))
    echo "$count" > "$FAIL_STATE"
    log "ERROR ($count consecutive): $detail"
    # Alert at threshold, and every 15 failures thereafter (every ~30 min)
    if [ "$count" -eq "$NOTIFY_THRESHOLD" ] || { [ "$count" -gt "$NOTIFY_THRESHOLD" ] && [ $((count % 15)) -eq 0 ]; }; then
        notify_sync_failure "$count" "$detail"
    fi
}

clear_failures() {
    if [ -f "$FAIL_STATE" ] && [ "$(cat "$FAIL_STATE" 2>/dev/null || echo 0)" -gt 0 ]; then
        log "sync recovered after previous failures"
        rm -f "$FAIL_STATE"
    fi
}

log "sync starting"

# This script only ever syncs main, and every step below assumes it. Running it
# from a feature branch rebased every unmerged commit onto main on each tick:
# on 2026-08-16 that jammed on one conflict, failed 66 times in a row, halted
# sync on both machines for a day, and left in-progress work in a stash it
# never popped. Branch work is a human decision — pause, say so, touch nothing.
#
# The check comes first on purpose. The stuck-rebase recovery below would
# happily abort a rebase the owner is running deliberately.
SYNC_BRANCH="main"
BRANCH_STATE="$STATE_DIR/sync-paused-branch.txt"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"

if [ "$CURRENT_BRANCH" != "$SYNC_BRANCH" ]; then
    log "not on main (HEAD is '$CURRENT_BRANCH') — sync paused, nothing touched"
    if [ "$(cat "$BRANCH_STATE" 2>/dev/null || echo '')" != "$CURRENT_BRANCH" ]; then
        echo "$CURRENT_BRANCH" > "$BRANCH_STATE"
        notify_branch_pause "$CURRENT_BRANCH"
    fi
    exit 0
fi
rm -f "$BRANCH_STATE"

# Capture pre-pull HEAD so we can tell later which files the pull introduced.
# Used to auto-restart the always-on chat service when its code changes (VPS only).
PRE_PULL_HEAD="$(git rev-parse HEAD 2>/dev/null || echo '')"

# Self-recovery: if the working tree is in an incomplete merge/rebase or has
# unmerged paths, abort cleanly so subsequent sync attempts can proceed.
if [ -d ".git/rebase-merge" ] || [ -d ".git/rebase-apply" ]; then
    log "WARN: detected stuck rebase, aborting"
    git rebase --abort 2>>"$LOG" || true
fi
if [ -f ".git/MERGE_HEAD" ]; then
    log "WARN: detected stuck merge, aborting"
    git merge --abort 2>>"$LOG" || true
fi
if git ls-files -u --error-unmatch . >/dev/null 2>&1; then
    # ls-files -u succeeded → there are unmerged paths
    log "WARN: detected unmerged paths, resetting to HEAD"
    git reset --merge 2>>"$LOG" || true
fi

# Fetch
git fetch origin 2>>"$LOG" || {
    record_failure "git fetch failed"
    exit 1
}

# Always stash with --include-untracked. The OLD failure mode was:
# - vault/daily/2026-04-26.md exists locally as untracked (heartbeat just created it)
# - origin has the same path tracked (other machine pushed it via auto-sync)
# - git pull refuses to overwrite the untracked file → "would be overwritten by merge"
# Without -u, plain `git stash` skips untracked files, so they keep colliding.
# With -u, the file goes into the stash and is restored after pull.
# The merge=union driver in .gitattributes auto-resolves daily-log content overlap.
#
# Gate the later `stash pop` on whether a stash entry was actually created,
# not on `git status` being dirty. Some "dirty" states are unstashable —
# e.g. a worktree inside .claude/worktrees/ renders as a submodule with
# "modified content" and `git stash push -u` silently reports "No local
# changes to save" and exits 0. Without this gate the pop runs against an
# empty stash list, errors, and the failure counter ticks forever.
STASHED=0
if [ -n "$(git status --porcelain)" ]; then
    stash_count_before="$(git stash list | wc -l | tr -d ' ')"
    git stash push --include-untracked -m "auto-sync $(date -u +%Y-%m-%dT%H:%M:%SZ)" 2>>"$LOG" || {
        record_failure "git stash push -u failed (dirty working tree with unmerged files?)"
        exit 1
    }
    stash_count_after="$(git stash list | wc -l | tr -d ' ')"
    if [ "$stash_count_after" -gt "$stash_count_before" ]; then
        STASHED=1
    fi
fi

git pull --rebase origin main 2>>"$LOG" || {
    git rebase --abort 2>>"$LOG" || true
    if [ "$STASHED" = "1" ]; then
        git stash pop 2>>"$LOG" || true
    fi
    record_failure "git pull --rebase failed"
    exit 1
}

if [ "$STASHED" = "1" ]; then
    git stash pop 2>>"$LOG" || {
        log "ERROR: stash pop had conflicts (merge=union should have handled daily logs)"
        # Don't blindly drop — leave stash for inspection. Reset the working
        # tree so the next sync can run cleanly. Stash is recoverable via
        # `git stash list`.
        git checkout -- . 2>>"$LOG" || true
        record_failure "stash pop had conflicts (stash retained — inspect with 'git stash list')"
        exit 1
    }
    # Commit anything that came back from the stash (vault content, daily logs, etc.)
    # so it propagates to the other machine on the next pull.
    if [ -n "$(git status --porcelain)" ]; then
        git add -A 2>>"$LOG"
        # Source code does not belong in a 2-minute "auto-sync: Mac <timestamp>"
        # commit. On 2026-08-14 this swept up a half-written script mid-edit, and
        # buried a security fix (hook interpreter fallback) under a message that
        # says nothing about it — unfindable later.
        #
        # A denylist, not an allowlist, on purpose: forgetting to list a new
        # CONTENT directory would silently stop syncing it between machines,
        # which loses data. Forgetting a new CODE directory just means it gets
        # auto-committed, which is only untidy.
        for excluded in $AUTO_SYNC_EXCLUDE; do
            git reset -q -- "$excluded" 2>>"$LOG" || true
        done
        # -uall: without it git collapses a wholly-untracked directory to a single
        # "?? .claude/scripts/" line, so the count would under-report.
        skipped="$(git status --porcelain -uall -- $AUTO_SYNC_EXCLUDE 2>/dev/null | wc -l | tr -d ' ')"
        if [ "$skipped" != "0" ]; then
            log "left $skipped code file(s) uncommitted (commit these yourself with a real message): $AUTO_SYNC_EXCLUDE"
        fi
        if [ "$(uname)" = "Darwin" ]; then
            SYNC_SOURCE="Mac"
        else
            SYNC_SOURCE="VPS"
        fi
        if ! git commit -m "auto-sync: ${SYNC_SOURCE} $(date -u +%Y-%m-%dT%H:%M:%SZ)" 2>>"$LOG"; then
            # This was `|| true`, which swallowed every commit failure. On 2026-08-02
            # user.email went unset; git refused to commit, the script still logged
            # "sync complete" and exited 0, so record_failure never fired and no alert
            # went out. Hours of vault changes sat staged and unpushed, silently.
            #
            # "nothing to commit" is genuinely benign (everything staged was ignored),
            # so distinguish it from a real error rather than alerting on both.
            if git diff --cached --quiet; then
                log "nothing to commit after add -A (benign)"
            else
                record_failure "git commit failed with changes staged — check: git config user.email"
                exit 1
            fi
        fi
    fi
fi

# Push if ahead of origin
if [ "$(git rev-list --count origin/main..HEAD)" -gt 0 ]; then
    git push origin main 2>>"$LOG" || {
        record_failure "git push failed"
        exit 1
    }
fi

clear_failures

# Auto-restart the chat service on VPS when its code was pulled in.
# The always-on Python process holds stale module state after git pull;
# systemd timers (heartbeat, reflection) spawn fresh processes each run,
# so only the chat server needs this. Sudoers rule:
#   /etc/sudoers.d/secondbrain-chat-restart permits the service user to restart it.
if [ "$(uname)" = "Linux" ] && [ -n "$PRE_PULL_HEAD" ]; then
    POST_PULL_HEAD="$(git rev-parse HEAD 2>/dev/null || echo '')"
    if [ -n "$POST_PULL_HEAD" ] && [ "$PRE_PULL_HEAD" != "$POST_PULL_HEAD" ]; then
        CHANGED="$(git diff --name-only "$PRE_PULL_HEAD" "$POST_PULL_HEAD" 2>/dev/null || echo '')"
        if echo "$CHANGED" | grep -qE '^\.claude/(chat/|scripts/tasks\.py|scripts/integrations/)'; then
            log "chat code changed, restarting secondbrain-chat"
            sudo -n /usr/bin/systemctl restart secondbrain-chat 2>>"$LOG" || log "WARN: chat restart failed"
        fi
    fi
fi

log "sync complete"
