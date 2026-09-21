#!/bin/bash
# Second Brain vault sync — runs every 2 minutes via launchd (Mac) / cron (VPS)
set -uo pipefail

# Derive repo root from this script's location (.claude/scripts/vault_sync.sh → repo root)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG="$REPO_DIR/.git/sync.log"  # per checkout, never tracked

# Paths auto-sync WILL commit (an allowlist, not a denylist — see
# "Deployment" in CLAUDE.md for why). Everything not named here (scripts,
# hooks, chat code, skills, commands, unit templates, settings.json, docs/,
# root files, tests) is left uncommitted for a human with a real commit
# message. vault (memory content), CLAUDE.md (the one owner-edited root file
# auto-sync has always carried), and .claude/data (tracked JSON stores) are
# the things this cron exists to move between machines. See
# tests/test_vault_sync.py.
AUTO_SYNC_ALLOW="vault CLAUDE.md .claude/data"

# Paths whose content-overlap conflicts are safe to resolve by keeping BOTH
# sides (union of lines) rather than picking one. Mirrors vault/.gitattributes
# merge=union entries — kept as a literal list here because bash has no
# built-in .gitattributes parser and this set changes rarely.
UNION_GLOBS="vault/daily/*.md vault/MEMORY.md"

STATE_DIR="$REPO_DIR/.claude/data/state"
FAIL_STATE="$STATE_DIR/sync-failures.txt"
NOTIFY_THRESHOLD=5  # consecutive failures before alerting
LOCK_DIR="$STATE_DIR/sync.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
LOCK_STALE_SECONDS=600
REBASE_STARTED_STATE="$STATE_DIR/sync-started-rebase.txt"
HUMAN_OP_NOTIFIED="$STATE_DIR/sync-human-op-notified.txt"
RETAINED_STASH_STATE="$STATE_DIR/sync-retained-stash.txt"
RETAINED_STASH_REMIND_SECONDS=86400  # 24h

mkdir -p "$STATE_DIR"
cd "$REPO_DIR"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $1" >> "$LOG"
}

# True if $1 matches any of the space-separated glob patterns in $2..$N.
matches_any() {
    local path="$1"
    shift
    local glob
    for glob in "$@"; do
        case "$path" in
            $glob) return 0 ;;
        esac
    done
    return 1
}

# True if $1 is under (or equal to) any of the space-separated path prefixes
# in $2..$N — used for the AUTO_SYNC_ALLOW check.
under_any_prefix() {
    local path="$1"
    shift
    local prefix
    for prefix in "$@"; do
        case "$path" in
            "$prefix"|"$prefix"/*) return 0 ;;
        esac
    done
    return 1
}

# Single notification path. `CORTEX_SYNC_NOTIFY_CMD`, when set, is invoked as
# `"$CORTEX_SYNC_NOTIFY_CMD" "$title" "$message"` instead of the real Telegram
# send — tests point this at a stub that appends to a file so a test run never
# reaches a real phone. Loads credentials via load_env() first: an importing
# caller of telegram_bot gets none by default (only __main__ loads .claude/.env),
# so a notification without this can go out with no credentials and be
# silently dropped.
send_notification() {
    local title="$1"
    local message="$2"
    if [ -n "${CORTEX_SYNC_NOTIFY_CMD:-}" ]; then
        "$CORTEX_SYNC_NOTIFY_CMD" "$title" "$message" >>"$LOG" 2>&1 || true
        return
    fi
    if [ -x "$REPO_DIR/.venv/bin/python3" ]; then
        "$REPO_DIR/.venv/bin/python3" - "$REPO_DIR" "$title" "$message" <<'PYEOF' 2>>"$LOG" || true
import sys
repo, title, message = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, repo + "/.claude/scripts")
try:
    from integrations.registry import load_env
    load_env()
    from notify import notify
    notify(title, message, parse_mode=None)
except Exception as e:
    print(f"notify failed: {e}", file=sys.stderr)
PYEOF
    fi
}

notify_sync_failure() {
    local failures="$1"
    local detail="$2"
    send_notification "Vault sync stuck" \
        "vault_sync.sh has failed $failures consecutive times. Latest: $detail. Run: cd $REPO_DIR && git status"
}

# Tell the owner once when sync goes quiet because of a branch checkout. A log
# line nobody reads is not a notification — a stuck sync should be noticed the
# same day, not discovered a day later.
notify_branch_pause() {
    local branch="$1"
    send_notification "Vault sync paused" \
        "Checked out '$branch', so vault sync between your machines is paused. Content written on either machine will not reach the other until you are back on main."
}

# Tell the owner once when the push is being held for a hand-made commit.
notify_push_held() {
    local subject="$1"
    send_notification "Vault sync: a commit is waiting for your push" \
        "'${subject:0:120}' was committed on main but not by auto-sync, so nothing is being pushed from this machine until you push it. Notes from the other machine still arrive here. Run: cd $REPO_DIR && git push origin main"
}

# Tell the owner once when a human git operation (merge/cherry-pick/revert) is
# in progress and sync is standing down rather than aborting it.
notify_human_op_pause() {
    send_notification "Vault sync paused" \
        "A merge, cherry-pick or revert is in progress on this machine, so vault sync is paused until it finishes or you abort it. Nothing was touched."
}

# Tell the owner once when a stash-pop conflict set work aside instead of
# discarding it. Fires once per conflict incident — resolve_stash_conflicts
# runs once per sync tick, so this call site is itself the "once" boundary.
notify_conflict_saved() {
    local files="$1"
    local dir="$2"
    send_notification "Vault sync: work set aside during a conflict" \
        "A sync conflict kept the pulled version of: $files. Your local version was saved to $dir — nothing was discarded. Review and merge by hand if needed."
}

# A stash this script could not fully resolve is a data-loss risk sitting
# quietly on disk, not an ordinary failure: the NEXT tick typically starts
# from a clean tree (nothing left to stash) and succeeds, so record_failure's
# 5-consecutive-failures counter resets to zero and never fires — the exact
# silent-orphan bug this whole mechanism exists to end. So this notifies
# immediately (not gated on any failure count) and keeps reminding once every
# RETAINED_STASH_REMIND_SECONDS for as long as the stash is still there.
notify_retained_stash() {
    local sha="$1"
    send_notification "Vault sync: work is stuck in a stash" \
        "A stash-pop conflict on this machine could not be fully resolved, so the stash was kept rather than discarding anything ($sha). It needs a human look: cd $REPO_DIR && git stash show -p $sha"
}

record_retained_stash() {
    local sha="$1"
    echo "$sha $(date -u +%s)" > "$RETAINED_STASH_STATE"
    notify_retained_stash "$sha"
}

# Runs every tick, independent of whatever else this tick does, so a stash
# retained on a PREVIOUS tick (when the working tree is clean now and the
# rest of the run would otherwise succeed silently) still gets noticed.
check_retained_stash() {
    [ -f "$RETAINED_STASH_STATE" ] || return 0
    local sha last_notified now
    sha="$(awk '{print $1}' "$RETAINED_STASH_STATE" 2>/dev/null)"
    last_notified="$(awk '{print $2}' "$RETAINED_STASH_STATE" 2>/dev/null)"
    if [ -z "$sha" ]; then
        rm -f "$RETAINED_STASH_STATE"
        return 0
    fi
    if ! git stash list --format='%H' 2>/dev/null | grep -qx "$sha"; then
        log "previously retained stash $sha is gone — clearing the reminder"
        rm -f "$RETAINED_STASH_STATE"
        return 0
    fi
    now="$(date -u +%s)"
    last_notified="${last_notified:-0}"
    if [ $(( now - last_notified )) -ge "$RETAINED_STASH_REMIND_SECONDS" ]; then
        echo "$sha $now" > "$RETAINED_STASH_STATE"
        notify_retained_stash "$sha"
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

# ── Single-run lock ──────────────────────────────────────────────────
# `mkdir` is atomic on every filesystem we run on and portable across macOS
# and Linux (flock(1) does not exist on macOS). A stale lock — the previous
# run's PID no longer exists, or the lock is older than LOCK_STALE_SECONDS —
# is replaced rather than honoured forever, so a crashed run cannot wedge
# every future tick.
#
# `mkdir` and `echo $$ > pid` are two separate steps, so there is a real
# window between them where a second run sees the lock dir with an EMPTY pid
# file. Treating an empty pid file as "dead" (pid_alive=0) let that second
# run decide the lock was stale and steal it — both runs then proceeded
# concurrently, and whichever exited first removed the lock out from under
# the other. An empty/unreadable pid file now counts as ALIVE unless the
# lock is already older than LOCK_STALE_SECONDS (a run that has been sitting
# there that long without ever writing its pid is not "about to write it",
# it is actually stale).
#
# `CORTEX_SYNC_LOCK_TEST_HOLD_SECONDS` (test-only, default 0) sleeps inside
# that exact window so tests can reproduce the race deterministically.
acquire_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        sleep "${CORTEX_SYNC_LOCK_TEST_HOLD_SECONDS:-0}"
        echo $$ > "$LOCK_PID_FILE"
        return 0
    fi
    local lock_pid=""
    [ -f "$LOCK_PID_FILE" ] && lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || echo '')"
    local lock_age=0
    if [ -e "$LOCK_DIR" ]; then
        local now mtime
        now="$(date +%s)"
        mtime="$(stat -f %m "$LOCK_DIR" 2>/dev/null || stat -c %Y "$LOCK_DIR" 2>/dev/null || echo "$now")"
        lock_age=$((now - mtime))
    fi
    local pid_alive=1
    if [ -n "$lock_pid" ]; then
        if kill -0 "$lock_pid" 2>/dev/null; then
            pid_alive=1
        else
            pid_alive=0
        fi
    elif [ "$lock_age" -gt "$LOCK_STALE_SECONDS" ]; then
        pid_alive=0   # empty pid file AND old enough that "still writing it" no longer explains it
    fi
    if [ "$pid_alive" = "0" ] || [ "$lock_age" -gt "$LOCK_STALE_SECONDS" ]; then
        log "replacing stale lock (pid=$lock_pid alive=$pid_alive age=${lock_age}s)"
        rm -rf "$LOCK_DIR"
        if mkdir "$LOCK_DIR" 2>/dev/null; then
            sleep "${CORTEX_SYNC_LOCK_TEST_HOLD_SECONDS:-0}"
            echo $$ > "$LOCK_PID_FILE"
            return 0
        fi
    fi
    return 1
}

# Only remove the lock if it still belongs to THIS run. Without this check, a
# run that stole the lock during the empty-pid race (or any other future
# TOCTOU) could have its lock ripped away by the original holder's exit trap
# even after fixing the race above — belt and suspenders.
release_lock() {
    if [ -f "$LOCK_PID_FILE" ] && [ "$(cat "$LOCK_PID_FILE" 2>/dev/null || echo '')" = "$$" ]; then
        rm -rf "$LOCK_DIR"
    fi
}

if ! acquire_lock; then
    log "another sync run holds the lock — exiting"
    exit 0
fi
trap release_lock EXIT

log "sync starting"

# Check every tick, before anything else can short-circuit the run (branch
# pause, human git op, etc.) — a stash retained on a PREVIOUS tick must keep
# being noticed even on a tick that otherwise does nothing.
check_retained_stash

# This script only ever syncs main, and every step below assumes it. Running it
# from a feature branch would rebase every unmerged commit onto main on each
# tick, which can jam on one conflict, fail repeatedly, halt sync on both
# machines, and leave in-progress work in a stash it never pops. Branch work
# is a human decision — pause, say so, touch nothing.
#
# The check comes first on purpose. The human-git-op recovery below would
# happily abort a rebase or merge the owner is running deliberately.
SYNC_BRANCH="main"
BRANCH_STATE="$STATE_DIR/sync-paused-branch.txt"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"

# A rebase leaves HEAD detached, so a pull --rebase of OURS that was killed
# midway used to land in the branch pause below: the machine stopped syncing
# for good, and the one notice told the owner he had checked out a branch he
# never touched. Recover it here — and only when the marker this script wrote
# before its pull equals the rebase's own orig-head. A human's rebase has no
# such marker and still gets the pause, untouched.
if [ "$CURRENT_BRANCH" = "HEAD" ] && { [ -d ".git/rebase-merge" ] || [ -d ".git/rebase-apply" ]; }; then
    EARLY_REBASE_DIR=".git/rebase-merge"
    [ -d "$EARLY_REBASE_DIR" ] || EARLY_REBASE_DIR=".git/rebase-apply"
    EARLY_ORIG_HEAD="$(cat "$EARLY_REBASE_DIR/orig-head" 2>/dev/null || echo '')"
    EARLY_MARKER="$(cat "$STATE_DIR/sync-started-rebase.txt" 2>/dev/null || echo '')"
    if [ -n "$EARLY_MARKER" ] && [ "$EARLY_MARKER" = "$EARLY_ORIG_HEAD" ]; then
        log "WARN: recovering from a rebase this script started but did not finish (HEAD was detached)"
        git rebase --abort 2>>"$LOG" || true
        rm -f "$STATE_DIR/sync-started-rebase.txt"
        CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
    fi
fi

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

# A merge/cherry-pick/revert/rebase the OWNER started is a decision in progress,
# not something this script should ever touch. This script itself only ever
# starts a REBASE (`git pull --rebase` below) — it never runs `git merge`,
# `cherry-pick`, or `revert` — so MERGE_HEAD/CHERRY_PICK_HEAD/REVERT_HEAD are
# always human-owned and are never auto-aborted.
#
# A rebase is only "ours" if REBASE_STARTED_STATE (written with the HEAD sha
# just before `git pull --rebase`, cleared right after) matches the rebase's
# own recorded orig-head — not merely "a marker file happens to exist". A
# leftover marker from a run killed between the pull returning and the `rm`
# used to be trusted on its own: a LATER, unrelated, human-started rebase
# conflict would then be misread as "our own", `git rebase --abort`ed
# silently (destroying the human's in-progress resolution) with no
# notification. The marker is now also cleared whenever no git operation is
# in progress at all (a marker with nothing to match is stale by definition),
# which is the common case that used to leave it dangling.
if [ -d ".git/rebase-merge" ] || [ -d ".git/rebase-apply" ]; then
    REBASE_STATE_DIR=".git/rebase-merge"
    [ -d "$REBASE_STATE_DIR" ] || REBASE_STATE_DIR=".git/rebase-apply"
    REBASE_ORIG_HEAD="$(cat "$REBASE_STATE_DIR/orig-head" 2>/dev/null || echo '')"
    REBASE_MARKER="$(cat "$REBASE_STARTED_STATE" 2>/dev/null || echo '')"
    if [ -n "$REBASE_MARKER" ] && [ -n "$REBASE_ORIG_HEAD" ] && [ "$REBASE_MARKER" = "$REBASE_ORIG_HEAD" ]; then
        log "WARN: recovering from a rebase this script started but did not finish"
        git rebase --abort 2>>"$LOG" || true
        rm -f "$REBASE_STARTED_STATE"
    else
        log "human git operation in progress — sync paused, nothing touched"
        OP_KEY="$(git rev-parse HEAD 2>/dev/null || echo unknown)-$(ls .git | tr '\n' ,)"
        if [ "$(cat "$HUMAN_OP_NOTIFIED" 2>/dev/null || echo '')" != "$OP_KEY" ]; then
            echo "$OP_KEY" > "$HUMAN_OP_NOTIFIED"
            notify_human_op_pause
        fi
        exit 0
    fi
elif [ -f ".git/MERGE_HEAD" ] || [ -f ".git/CHERRY_PICK_HEAD" ] || [ -f ".git/REVERT_HEAD" ]; then
    rm -f "$REBASE_STARTED_STATE"   # never ours — stale by definition if it's still here
    log "human git operation in progress — sync paused, nothing touched"
    OP_KEY="$(git rev-parse HEAD 2>/dev/null || echo unknown)-$(ls .git | tr '\n' ,)"
    if [ "$(cat "$HUMAN_OP_NOTIFIED" 2>/dev/null || echo '')" != "$OP_KEY" ]; then
        echo "$OP_KEY" > "$HUMAN_OP_NOTIFIED"
        notify_human_op_pause
    fi
    exit 0
else
    rm -f "$HUMAN_OP_NOTIFIED"
    rm -f "$REBASE_STARTED_STATE"
fi

# Self-recovery for unmerged paths left by an earlier crashed run of THIS
# script (e.g. a killed conflict-resolution pass). The stash from that run is
# never dropped until resolution succeeds, so resetting the index here throws
# nothing away — the next stash pop attempt starts clean.
if git ls-files -u --error-unmatch . >/dev/null 2>&1; then
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
        # Remember WHICH stash is ours. The owner keeps old stashes of his own;
        # "the top of the stack" is ours only until something pops it, and
        # after that the top is one of his. Nothing below may drop or record a
        # stash without checking it against this sha first.
        OUR_STASH_SHA="$(git rev-parse -q --verify "stash@{0}" 2>/dev/null || echo '')"
    fi
fi

# True only while the top of the stash stack is the stash THIS run created.
top_stash_is_ours() {
    [ -n "${OUR_STASH_SHA:-}" ] && \
        [ "$(git rev-parse -q --verify "stash@{0}" 2>/dev/null || echo '')" = "$OUR_STASH_SHA" ]
}

echo "$(git rev-parse HEAD 2>/dev/null || echo '')" > "$REBASE_STARTED_STATE"
git pull --rebase origin main 2>>"$LOG" || {
    git rebase --abort 2>>"$LOG" || true
    rm -f "$REBASE_STARTED_STATE"
    if [ "$STASHED" = "1" ]; then
        git stash pop 2>>"$LOG" || true
    fi
    record_failure "git pull --rebase failed"
    exit 1
}
rm -f "$REBASE_STARTED_STATE"

# ── Stash-pop conflict resolution ────────────────────────────────────
# Never lose the set-aside work. Old behaviour on a conflict was
# `git checkout -- .` — discard the stash's changes and leave the stash
# behind uninspected, which can silently lose real notes.
#
# Two shapes of conflict, handled in two phases:
#
#   Phase 0 — untracked-file collision. `git stash push -u` stashes an
#   untracked file into the stash's 3rd parent commit. If the SAME path has
#   since arrived from origin (the classic case: both machines create
#   today's daily log), git refuses the ENTIRE pop before touching anything
#   — "untracked working tree files would be overwritten" — leaving no
#   conflict markers anywhere. Resolve it ourselves: move every colliding
#   untracked path out of the way, retry the pop (which can now restore
#   everything else, including non-colliding untracked files, the normal
#   way), then resolve each moved-aside path exactly like phase 1.
#
#   Phase 1 — ordinary tracked conflicts, from the original pop or the one
#   phase 0 retried. Per conflicted path: if the stash side (stage 3) is
#   simply ABSENT, the local side deleted the file — policy is "keep the
#   pulled version", so that deletion is never applied and nothing is saved
#   (there is no local content to save, only an absence, and writing an
#   empty file for it would look like rescued work that never existed).
#   Otherwise: a path covered by UNION_GLOBS is resolved by keeping BOTH
#   sides' lines (`git merge-file --union` against the index stages a failed
#   stash pop leaves: 1=base, 2=ours/pulled, 3=theirs/stashed); everything
#   else (including `.claude/data/*.json` tracked stores, which have no
#   merge strategy of their own — same handling as any other file, not a
#   special case) keeps the pulled version in the working tree and saves the
#   stashed (local) version to .claude/data/state/sync-conflicts/<ts>/<path>
#   — gitignored and out of the vault, so the saved copy is never itself
#   auto-committed, synced to the other machine, or picked up by
#   hygiene/indexer.
#
# The stash is dropped only once every conflicted/collided path has actually
# been processed. If nothing was recognisably resolved (an unrecognised pop
# failure) the stash is kept rather than guessed away. Any retained stash is
# recorded and notified immediately via record_retained_stash — never left
# for the 5-consecutive-failures counter, which the next clean tick resets
# to zero before it could ever fire.
resolve_stash_conflicts() {
    local ts
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    local conflict_dir="$STATE_DIR/sync-conflicts/$ts"
    local ok=1
    local merged_log="" saved_log="" deleted_log=""
    local handled_anything=0
    local hold_dir="" moved_paths=""

    # ── Phase 0: untracked-file collision ──
    if [ -z "$(git diff --name-only --diff-filter=U)" ] && git rev-parse -q --verify "stash@{0}^3" >/dev/null 2>&1; then
        hold_dir="$(mktemp -d)"
        while IFS= read -r -d '' upath; do
            [ -z "$upath" ] && continue
            if [ -e "$upath" ]; then
                mkdir -p "$(dirname "$hold_dir/$upath")"
                mv "$upath" "$hold_dir/$upath"
                moved_paths="$moved_paths $upath"
            fi
        done < <(git ls-tree -r -z --name-only "stash@{0}^3" 2>>"$LOG")

        if [ -n "$moved_paths" ]; then
            handled_anything=1
            if ! git stash pop 2>>"$LOG"; then
                if [ -n "$(git diff --name-only --diff-filter=U)" ]; then
                    # Isolating the untracked collision let the pop proceed
                    # far enough to surface an ORDINARY tracked conflict —
                    # that is phase 1's job below, not a reason to give up.
                    log "conflict: isolating the untracked collision surfaced ordinary conflict(s) too — resolving those next"
                else
                    log "ERROR: stash pop still failed after isolating untracked collision(s), with no ordinary conflict either — restoring them and giving up cleanly for this tick"
                    local p
                    for p in $moved_paths; do
                        mkdir -p "$(dirname "$p")"
                        mv "$hold_dir/$p" "$p" 2>>"$LOG" || true
                    done
                    rm -rf "$hold_dir"
                    local sha
                    sha=""; top_stash_is_ours && sha="$OUR_STASH_SHA"
                    record_failure "stash pop failed even after isolating untracked collisions — stash retained (inspect: git stash list)"
                    [ -n "$sha" ] && record_retained_stash "$sha"
                    return 1
                fi
            fi
        else
            rm -rf "$hold_dir"
            hold_dir=""
        fi
    fi

    # ── Phase 1: ordinary tracked conflicts (original, or surfaced by the phase-0 retry) ──
    local conflicted_files
    conflicted_files="$(git diff --name-only --diff-filter=U)"
    if [ -n "$conflicted_files" ]; then
        handled_anything=1
        local path
        while IFS= read -r path; do
            [ -z "$path" ] && continue
            if ! git cat-file -e ":3:$path" 2>/dev/null; then
                # Stash side deleted this path. Policy is "keep the pulled
                # version" — that deletion is never applied. There is no
                # local CONTENT to save (only an absence), so no copy is
                # written; writing an empty placeholder would misread as
                # rescued work that never existed.
                if git cat-file -e ":2:$path" 2>/dev/null; then
                    git show ":2:$path" > "$path" 2>>"$LOG" && git add -- "$path" 2>>"$LOG"
                else
                    rm -f "$path"
                    git rm -q -- "$path" 2>>"$LOG" || true
                fi
                deleted_log="$deleted_log $path"
                continue
            fi
            local theirs_f
            theirs_f="$(mktemp)"
            git show ":3:$path" > "$theirs_f" 2>>"$LOG"
            if matches_any "$path" $UNION_GLOBS && git cat-file -e ":2:$path" 2>/dev/null; then
                local base_f
                base_f="$(mktemp)"
                if git cat-file -e ":1:$path" 2>/dev/null; then
                    git show ":1:$path" > "$base_f" 2>>"$LOG"
                else
                    : > "$base_f"
                fi
                git show ":2:$path" > "$path" 2>>"$LOG"
                if git merge-file --union "$path" "$base_f" "$theirs_f" 2>>"$LOG"; then
                    git add -- "$path" 2>>"$LOG"
                    merged_log="$merged_log $path"
                else
                    log "ERROR: union merge failed for $path"
                    ok=0
                fi
                rm -f "$base_f"
            else
                mkdir -p "$conflict_dir/$(dirname "$path")"
                cp "$theirs_f" "$conflict_dir/$path"
                if git cat-file -e ":2:$path" 2>/dev/null; then
                    git show ":2:$path" > "$path" 2>>"$LOG" && git add -- "$path" 2>>"$LOG"
                else
                    rm -f "$path"
                    git rm -q -- "$path" 2>>"$LOG" || true
                fi
                saved_log="$saved_log $path"
            fi
            rm -f "$theirs_f"
        done <<< "$conflicted_files"
    fi

    if [ "$ok" != "1" ] || [ -n "$(git diff --name-only --diff-filter=U)" ]; then
        local sha
        sha=""; top_stash_is_ours && sha="$OUR_STASH_SHA"
        record_failure "stash pop conflict resolution failed or incomplete — stash retained, partial copies (if any) in $conflict_dir"
        [ -n "$sha" ] && record_retained_stash "$sha"
        return 1
    fi

    # ── Phase 2: resolve the untracked-collision paths moved aside in phase 0 ──
    #
    # $hold_dir/$path holds what phase 0 moved OUT of the way before the
    # retry pop — i.e. the PULLED/current content (that is what occupied the
    # path at collision time; the local content only ever lived inside the
    # stash's 3rd parent, never in the working tree). The retry pop then
    # checked the stash's untracked content — the LOCAL side — out onto
    # $path. So at this point: $path = local, $hold_dir/$path = pulled.
    if [ -n "$hold_dir" ]; then
        handled_anything=1
        local path
        for path in $moved_paths; do
            local pulled_file="$hold_dir/$path"
            [ -e "$pulled_file" ] || continue
            if matches_any "$path" $UNION_GLOBS; then
                local empty_base local_copy
                empty_base="$(mktemp)"
                : > "$empty_base"
                local_copy="$(mktemp)"
                if [ -e "$path" ]; then cp "$path" "$local_copy"; else : > "$local_copy"; fi
                cp "$pulled_file" "$path"
                if git merge-file --union "$path" "$empty_base" "$local_copy" 2>>"$LOG"; then
                    git add -- "$path" 2>>"$LOG"
                    merged_log="$merged_log $path"
                else
                    log "ERROR: union merge failed for previously-untracked $path — keeping the pulled version, saving local content aside instead"
                    mkdir -p "$conflict_dir/$(dirname "$path")"
                    cp "$local_copy" "$conflict_dir/$path"
                    cp "$pulled_file" "$path"
                    git add -- "$path" 2>>"$LOG"
                    saved_log="$saved_log $path"
                fi
                rm -f "$empty_base" "$local_copy"
            else
                mkdir -p "$conflict_dir/$(dirname "$path")"
                if [ -e "$path" ]; then
                    cp "$path" "$conflict_dir/$path"   # local content (from the retry pop), saved aside
                fi
                cp "$pulled_file" "$path"              # restore the pulled/current content into place
                git add -- "$path" 2>>"$LOG"
                saved_log="$saved_log $path"
            fi
        done
        rm -rf "$hold_dir"
    fi

    if [ "$handled_anything" = "0" ]; then
        # Pop failed for a reason we didn't recognise (not conflict markers,
        # not an untracked collision). Guessing "nothing to do, drop it" is
        # exactly the old bug — keep the stash and say so plainly.
        local sha
        sha=""; top_stash_is_ours && sha="$OUR_STASH_SHA"
        log "ERROR: stash pop failed for an unrecognised reason — stash retained, nothing touched"
        record_failure "stash pop failed for an unrecognised reason — stash retained (inspect: git stash list)"
        [ -n "$sha" ] && record_retained_stash "$sha"
        return 1
    fi

    # The phase-0 retry pop can already have dropped the stash itself (`stash
    # pop` = apply, then auto-drop on success) when the only thing it held
    # was the untracked collision handled there — nothing tracked was left
    # for it to conflict on. Only try to drop it ourselves if it is still there.
    # "Still there" means OUR stash is still on top — not "any stash exists":
    # with our own already gone, the top of the stack is one of the owner's.
    if top_stash_is_ours; then
        git stash drop "stash@{0}" 2>>"$LOG" || log "WARN: could not drop the stash after resolving its conflicts"
    fi
    # The retained-stash reminder is NOT cleared here. It may be about an
    # earlier, different stash that is still stranded; check_retained_stash
    # clears it at the top of the next run once that stash is really gone.

    if [ -n "$merged_log" ]; then
        log "conflict: union-merged (both sides kept):$merged_log"
    fi
    if [ -n "$deleted_log" ]; then
        log "conflict: local deletion not applied (kept the pulled version):$deleted_log"
    fi
    if [ -n "$saved_log" ]; then
        log "conflict: kept the pulled version, saved local version(s) to $conflict_dir:$saved_log"
    fi
    # Telegram only when a local version was actually set aside. A clean union
    # merge lost nothing and a local deletion left nothing to save; the message
    # says "your local version was saved to <dir>", which for those two pointed
    # the owner at an empty folder. They are in the log above.
    if [ -n "$saved_log" ]; then
        notify_conflict_saved "$saved_log" "$conflict_dir"
    fi
    return 0
}

if [ "$STASHED" = "1" ]; then
    if ! git stash pop 2>>"$LOG"; then
        log "WARN: stash pop had conflicts — resolving without discarding anything"
        if ! resolve_stash_conflicts; then
            exit 1
        fi
    fi

    # Commit anything that came back from the stash (vault content, daily logs, etc.)
    # so it propagates to the other machine on the next pull.
    if [ -n "$(git status --porcelain)" ]; then
        git add -A 2>>"$LOG"
        # Allowlist, not a denylist. A denylist silently auto-committed and
        # auto-pushed anything nobody had thought to add to it —
        # .claude/settings.json (hook wiring), unit templates, skills,
        # .gitignore, docs/, root files — under a timestamp-only "auto-sync:
        # Mac <ts>" message. An allowlist means a change outside it is left
        # for a human by construction, not by remembering to add it to a
        # growing exclude list.
        staged="$(git diff --cached --name-only)"
        skipped=0
        skipped_list=""
        while IFS= read -r f; do
            [ -z "$f" ] && continue
            if ! under_any_prefix "$f" $AUTO_SYNC_ALLOW; then
                git reset -q -- "$f" 2>>"$LOG" || true
                skipped=$((skipped + 1))
                skipped_list="$skipped_list $f"
            fi
        done <<< "$staged"
        if [ "$skipped" -gt 0 ]; then
            log "left $skipped file(s) outside {$AUTO_SYNC_ALLOW} uncommitted (commit these yourself with a real message):$skipped_list"
        fi
        if [ "$(uname)" = "Darwin" ]; then
            SYNC_SOURCE="Mac"
        else
            SYNC_SOURCE="VPS"
        fi
        if ! git commit -m "auto-sync: ${SYNC_SOURCE} $(date -u +%Y-%m-%dT%H:%M:%SZ)" 2>>"$LOG"; then
            # This was `|| true`, which swallowed every commit failure. If
            # user.email goes unset, git refuses to commit, the script still
            # logs "sync complete" and exits 0, so record_failure never fires
            # and no alert goes out. Changes sit staged and unpushed, silently.
            #
            # "nothing to commit" is genuinely benign (everything staged was
            # ignored), so distinguish it from a real error rather than
            # alerting on both.
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
#
# The sync pushes only its own commits. Pushing whatever was unpushed used to
# include code commits made on main by hand — a hook that blocks a push to
# main was real, and the sync walked around it every 2 minutes. A commit
# whose subject is not "auto-sync: ..." holds the push until the owner pushes
# it. Pulling still runs, and vault changes are still committed locally and
# queue up behind it. A hold is a decision waiting on a person, not a failure.
HELD_STATE="$STATE_DIR/sync-held-commit.txt"
if [ "$(git rev-list --count origin/main..HEAD)" -gt 0 ]; then
    # Oldest first. Keyed on author-time + subject, not the hash: every pull
    # rebases the held commit and changes its hash, and a hash key would
    # re-announce the same hold each time the other machine pushed.
    HELD_KEY="$(git log --reverse --format='%at %s' origin/main..HEAD | grep -v '^[0-9]* auto-sync: ' | head -1)"
    if [ -n "$HELD_KEY" ]; then
        HELD_SUBJECT="${HELD_KEY#* }"
        log "push held: '$HELD_SUBJECT' was not made by auto-sync — waiting for the owner's push"
        if [ "$(cat "$HELD_STATE" 2>/dev/null || echo '')" != "$HELD_KEY" ]; then
            echo "$HELD_KEY" > "$HELD_STATE"
            notify_push_held "$HELD_SUBJECT"
        fi
    else
        rm -f "$HELD_STATE"
        git push origin main 2>>"$LOG" || {
            record_failure "git push failed"
            exit 1
        }
    fi
else
    rm -f "$HELD_STATE"
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
        # Any script, not a hand-kept list: the bot imports whatever its tools
        # need and keeps it in memory. A hand-kept list of "the modules that
        # matter" silently misses the next new one.
        if echo "$CHANGED" | grep -qE '^\.claude/(chat/|scripts/.*\.py$)'; then
            log "chat code changed, restarting secondbrain-chat"
            sudo -n /usr/bin/systemctl restart secondbrain-chat 2>>"$LOG" || log "WARN: chat restart failed"
        fi
    fi
fi

log "sync complete"
