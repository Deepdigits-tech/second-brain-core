"""Draft lifecycle module for Cortex OS.

Owns the active/expired/sent transitions for vault/drafts/*.md files.
Drafts carry a thread_id so reconcile_active_drafts() can check the Gmail
Sent folder for replies and move active → sent automatically.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

try:
    from integrations.gmail import ToolNotFoundError
except ImportError:  # pragma: no cover - integrations package not on path
    class ToolNotFoundError(RuntimeError):  # type: ignore[no-redef]
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
DRAFTS_ACTIVE = VAULT_DIR / "drafts" / "active"
DRAFTS_EXPIRED = VAULT_DIR / "drafts" / "expired"
DRAFTS_SENT = VAULT_DIR / "drafts" / "sent"

EXPIRE_AFTER_SECONDS = 24 * 3600


def load_draft(path: Path) -> tuple[dict, str]:
    """Parse a draft file into (frontmatter dict, body string)."""
    text = path.read_text()
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 5 :]
    if yaml is None:
        return {}, body
    meta = yaml.safe_load(fm_text) or {}
    return meta, body


def save_draft(path: Path, meta: dict, body: str) -> None:
    """Write a draft file with the given frontmatter and body."""
    fm = "---\n"
    for key, value in meta.items():
        if isinstance(value, str) and (":" in value or value.startswith(" ")):
            fm += f'{key}: "{value}"\n'
        else:
            fm += f"{key}: {value}\n"
    fm += "---\n\n"
    path.write_text(fm + body.lstrip())


def iter_active_drafts() -> Iterator[Path]:
    """Yield every active draft path."""
    if not DRAFTS_ACTIVE.exists():
        return
    for p in sorted(DRAFTS_ACTIVE.glob("*.md")):
        yield p


def get_active_draft_count() -> int:
    """Count active drafts for heartbeat context."""
    return sum(1 for _ in iter_active_drafts())



def _owner_emails() -> set[str]:
    """The owner's own addresses, from shared.owner_emails() (CORTEX_OWNER_EMAILS).

    Used to tell the owner's own sent messages apart from a correspondent's
    reply. Not a module-level constant — the env may not be loaded yet at
    import time.
    """
    from shared import owner_emails
    return set(owner_emails())


def _fetch_thread(thread_id: str, account: str):
    """Indirection so tests can patch easily."""
    from integrations.gmail import fetch_thread
    return fetch_thread(thread_id, account=account)


def _read_message(message_id: str, account: str, metadata_only: bool = True):
    """Internal seam: read one message (indirection so tests can patch it)."""
    from integrations.gmail import read_message
    return read_message(message_id, account=account, metadata_only=metadata_only)


def _resolve_thread_id(thread_id: str, source_id: str, account: str) -> str:
    """Return a thread id that Gmail will actually accept, or "" if none does.

    `thread_id` is written by the model from the heartbeat's draft template, and
    until 2026-08-29 the prompt asked for "the inbound email's threadId" while
    only ever SHOWING the model the message id. It therefore wrote the message
    id, which Gmail answers with 404 NOT_FOUND — every 30 minutes, per draft,
    until the draft expired.

    The log noise was the small half. The silent half: a wrong thread id means
    a reply is never seen, so the draft expires instead of moving to sent/, and
    the drafted-vs-sent gap that `brain skills` learns from is lost.

    A message id is the one wrong value we can repair deterministically: read it
    as a message and take the real threadId off it. Called only after a fetch
    has already come back empty, so a correct draft pays nothing.
    """
    for candidate in (thread_id, source_id):
        if not candidate:
            continue
        try:
            msg = _read_message(candidate, account)
        except Exception as e:
            print(f"[drafts] thread id repair failed for {candidate} ({account}): {e}",
                  file=sys.stderr)
            continue
        real = (msg or {}).get("threadId") or ""
        if real and real != thread_id:
            return real
    return ""


def _apply_waiting_label(thread_id: str, account: str) -> bool:
    """Label the thread Cortex/Waiting. Returns True on success."""
    try:
        from integrations.gmail import get_or_create_label, add_thread_label
        label_id = get_or_create_label("Cortex/Waiting", account=account)
        if not label_id:
            return False
        return add_thread_label(thread_id, label_id, account=account)
    except Exception as e:
        print(f"[drafts] apply_waiting_label failed for thread {thread_id} ({account}): {e}", file=sys.stderr)
        return False


def _decline_matching_tasks(source_id: str) -> int:
    """Auto-decline any proposed task whose source ends with :<source_id>.

    Returns count of declined tasks.
    """
    from shared import lane_available
    if not lane_available("tasks"):
        return 0
    import tasks as tq
    declined = 0
    for t in tq.list_by_status("proposed"):
        src = t.get("source", "") or ""
        if src.endswith(f":{source_id}"):
            tq.decline(t["id"])
            declined += 1
    return declined


def _owner_replied_after(thread_messages: list[dict], created: datetime, owner_emails: set[str]) -> dict | None:
    """Return the first message in the thread that is FROM the owner and AFTER `created`.

    None if no reply found.
    """
    created_utc = created.astimezone(timezone.utc) if created.tzinfo else created.replace(tzinfo=timezone.utc)
    for msg in thread_messages:
        from_field = (msg.get("from") or "").lower()
        for email in owner_emails:
            if email in from_field:
                date_str = msg.get("date", "")
                if not date_str:
                    continue
                try:
                    msg_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if msg_dt > created_utc:
                    return msg
    return None


SENT_AS_HEADING = "## Sent As"


def move_to_sent(path: Path, sent_msg: dict) -> Path:
    """Move a draft from active/ → sent/, update frontmatter status and sent_at.

    Also records what the owner ACTUALLY sent, under `## Sent As`.

    Until 2026-08-27 this kept only `sent_msg_id` and threw the message away.
    That made the most valuable signal Cortex produces unreadable after the
    fact: the gap between what was drafted and what a human was willing to
    send. `brain skills` reads that gap to propose how the drafting
    instructions should change. Without the text there is nothing to compare.
    """
    DRAFTS_SENT.mkdir(parents=True, exist_ok=True)
    meta, body = load_draft(path)
    meta["status"] = "sent"
    meta["sent_at"] = sent_msg.get("date", datetime.now(timezone.utc).isoformat())
    meta["sent_msg_id"] = sent_msg.get("id", "")

    actual = (sent_msg.get("body") or sent_msg.get("snippet") or "").strip()
    if actual and SENT_AS_HEADING not in body:
        truncated = " (snippet only)" if not sent_msg.get("body") else ""
        body = f"{body.rstrip()}\n\n{SENT_AS_HEADING}{truncated}\n{actual}\n"

    dest = DRAFTS_SENT / path.name
    save_draft(dest, meta, body)
    path.unlink()
    return dest


def move_to_expired(path: Path) -> Path:
    """Move a draft from active/ → expired/ (no reply within 24h)."""
    DRAFTS_EXPIRED.mkdir(parents=True, exist_ok=True)
    dest = DRAFTS_EXPIRED / path.name
    path.rename(dest)
    return dest


def _query_waiting_threads(account: str) -> list[dict]:
    """Internal: list threads with label Cortex/Waiting (latest message each)."""
    # max_results=50 caps the per-morning N+1 fetch_thread call cost (1 query + N thread fetches)
    from integrations.gmail import query_messages
    return query_messages("label:Cortex/Waiting", account=account, max_results=50)


def _thread_last_inbound_age_days(thread_id: str, account: str, owner_emails: set[str]) -> int:
    """Days since the last NOT-FROM-OWNER message on the thread.
    If the thread's only messages are outbound, returns the age of the owner's last sent.
    """
    msgs = _fetch_thread(thread_id, account)
    if not msgs:
        return -1
    now_utc = datetime.now(timezone.utc)
    most_recent: datetime | None = None
    # Gmail/Composio returns thread messages chronological (oldest→newest); reversed() walks newest-first
    for msg in reversed(msgs):
        from_field = (msg.get("from") or "").lower()
        date_str = msg.get("date", "")
        if not date_str:
            continue
        try:
            msg_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if any(email in from_field for email in owner_emails):
            if most_recent is None:
                most_recent = msg_dt
            continue
        most_recent = msg_dt
        break
    if most_recent is None:
        return -1
    delta = (now_utc - most_recent).total_seconds() / 86400
    return int(delta)


def load_waiting_followups(account: str, min_days: int = 3, owner_emails: set[str] | None = None) -> list[dict]:
    """Return Cortex/Waiting threads where the last inbound message is older than min_days.

    Each entry: {thread_id, subject, latest_message_date, days_silent, account}.
    """
    owner_emails = owner_emails or _owner_emails()
    threads = _query_waiting_threads(account)
    out = []
    seen_threads: set[str] = set()
    for msg in threads:
        thread_id = msg.get("threadId") or msg.get("id")
        if not thread_id or thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)
        age = _thread_last_inbound_age_days(thread_id, account, owner_emails)
        if age >= min_days:
            out.append({
                "thread_id": thread_id,
                "account": account,
                "subject": msg.get("subject", ""),
                "latest_message_date": msg.get("date", ""),
                "days_silent": age,
            })
    return out


def reconcile_active_drafts(owner_emails: set[str] | None = None) -> dict:
    """For each active draft: check Gmail for a sent reply, move to sent/ if found.
    Otherwise if older than EXPIRE_AFTER_SECONDS, move to expired/.

    Returns: {moved_to_sent, expired, kept, declined_tasks}
    """
    owner_emails = owner_emails or _owner_emails()
    now = datetime.now(timezone.utc)
    # thread_ids_repaired counts drafts whose stored thread_id was not a thread
    # id at all and had to be recovered from the message. Non-zero means the
    # model is still writing the wrong field despite the prompt showing it —
    # the repair keeps the draft working, but the count is how we find out.
    result = {"moved_to_sent": 0, "expired": 0, "kept": 0, "declined_tasks": 0,
              "thread_ids_repaired": 0}

    for path in iter_active_drafts():
        try:
            meta, _body = load_draft(path)
        except Exception:
            continue

        thread_id = meta.get("thread_id") or meta.get("threadId") or ""
        source_id = meta.get("source_id", "")
        account = meta.get("account", "personal")
        created_raw = meta.get("created", "")

        try:
            created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            try:
                created = datetime.strptime(path.name[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                result["kept"] += 1
                continue

        reply_msg = None
        if thread_id:
            try:
                thread = _fetch_thread(thread_id, account)
                if not thread:
                    # Empty means "no such thread", not "no messages" — a real
                    # thread always contains at least the message we drafted
                    # against. Most often the stored id is a message id.
                    repaired = _resolve_thread_id(thread_id, source_id, account)
                    if repaired:
                        thread = _fetch_thread(repaired, account)
                        if thread:
                            meta["thread_id"] = repaired
                            save_draft(path, meta, _body)
                            thread_id = repaired
                            result["thread_ids_repaired"] += 1
                            print(
                                f"[drafts] repaired thread_id in {path.name}: "
                                f"{meta.get('source_id', '?')} was stored as a thread id, "
                                f"real thread is {repaired}",
                                file=sys.stderr,
                            )
                reply_msg = _owner_replied_after(thread, created, owner_emails)
            except ToolNotFoundError:
                # A dead Gmail slug would otherwise file every replied-to draft
                # as expired. Let it out so the caller logs it.
                raise
            except Exception as e:
                # Say what went wrong. This catch is deliberately broad — a
                # flaky Gmail call must not stall the whole reconcile — but it
                # swallows programming errors just as happily. A NameError in
                # the logging right above it silently disabled reply detection
                # and took four probes to find, because the only symptom was a
                # draft quietly expiring.
                print(f"[drafts] reply check failed for {path.name}: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                reply_msg = None

        if reply_msg is not None:
            move_to_sent(path, reply_msg)
            result["moved_to_sent"] += 1
            if thread_id:
                _apply_waiting_label(thread_id, account)
            if source_id:
                result["declined_tasks"] += _decline_matching_tasks(source_id)
            continue

        created_utc = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        age = (now - created_utc).total_seconds()
        if age > EXPIRE_AFTER_SECONDS:
            move_to_expired(path)
            result["expired"] += 1
        else:
            result["kept"] += 1

    return result
