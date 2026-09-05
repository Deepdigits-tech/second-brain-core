"""Gmail integration via Composio SDK for Cortex OS."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ENV_PATH = REPO_ROOT / ".claude" / ".env"

DEFAULT_ACCOUNT = "personal"


def account_user_ids() -> dict[str, str]:
    """Alias -> Composio user_id, from shared.gmail_accounts() (env-backed).

    Not a module-level dict: CORTEX_GMAIL_ACCOUNTS may not be loaded yet when
    this module is first imported — registry.load_env() runs later at most
    call sites. Read fresh on every call instead of snapshotting an unloaded
    environment at import time. ACCOUNT_USER_IDS below is the same mapping,
    served lazily so `dict(ACCOUNT_USER_IDS)` / `.get()` / `for a in
    ACCOUNT_USER_IDS` keep working for every existing caller.
    """
    from shared import gmail_accounts
    return gmail_accounts()


def __getattr__(name: str):
    if name == "ACCOUNT_USER_IDS":
        return account_user_ids()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Label cache path (keyed by account → label_name → label_id)
_LABELS_CACHE_PATH = REPO_ROOT / ".claude" / "data" / "state" / "gmail-labels.json"
# In-memory label cache for the current process run
_label_id_cache: dict[str, dict[str, str]] = {}

_composio_client = None

# Every Composio slug this module calls. Kept in one list so audit.py can check
# they all still resolve — see verify_tool_slugs() and the ToolNotFoundError note.
TOOL_SLUGS = (
    "GMAIL_FETCH_EMAILS",
    "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
    "GMAIL_MODIFY_THREAD_LABELS",
    "GMAIL_CREATE_EMAIL_DRAFT",
    "GMAIL_LIST_LABELS",
    "GMAIL_CREATE_LABEL",
    "GMAIL_ADD_LABEL_TO_EMAIL",
    "GMAIL_BATCH_MODIFY_MESSAGES",
    "GMAIL_MOVE_TO_TRASH",
)

# Safety cap on thread pagination (Gmail tops out at 100 messages per thread).
MAX_THREAD_PAGES = 10


class ToolNotFoundError(RuntimeError):
    """A Composio slug this module depends on no longer exists upstream.

    Kept separate from the None return that means "the call gave us nothing":
    a removed slug is a code-level breakage no retry fixes, and folding it into
    "no data" is what hid GMAIL_FETCH_THREAD's removal for months. Same class of
    bug as the `query` vs `q` note in triage() — a wrong argument and a dead slug
    both look like an empty result unless something insists otherwise.
    """


def _is_tool_not_found(exc: Exception) -> bool:
    """True if `exc` is Composio's 404 for a slug that no longer exists."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and (err.get("slug") == "Tool_ToolNotFound" or err.get("code") == 2401):
            return True
    return getattr(exc, "status_code", None) == 404 and "Tool_ToolNotFound" in str(exc)


def _client():
    """Get or create the Composio client singleton."""
    global _composio_client
    if _composio_client is None:
        from dotenv import load_dotenv
        load_dotenv(_ENV_PATH)
        api_key = os.environ.get("COMPOSIO_API_KEY")
        if not api_key:
            raise RuntimeError(f"COMPOSIO_API_KEY not set in {_ENV_PATH}")
        from composio import Composio
        _composio_client = Composio(api_key=api_key)
    return _composio_client


def _execute(tool_slug: str, arguments: dict, account: str = DEFAULT_ACCOUNT) -> dict | None:
    """Execute a Composio tool.

    Returns data dict on success (possibly {} for 204 responses), None on failure.
    Raises ToolNotFoundError if the slug itself no longer exists upstream.
    """
    user_id = account_user_ids().get(account, account)
    try:
        result = _client().tools.execute(
            tool_slug,
            arguments=arguments,
            user_id=user_id,
            dangerously_skip_version_check=True,
        )
    except Exception as e:
        if _is_tool_not_found(e):
            raise ToolNotFoundError(
                f"Composio tool slug '{tool_slug}' no longer exists. Find its "
                f"replacement in the Gmail toolkit and update gmail.py "
                f"(TOOL_SLUGS). Upstream error: {e}"
            ) from e
        print(f"Warning: Composio execute {tool_slug} failed: {e}", file=sys.stderr)
        return None

    # Dict form: {'data': {...}, 'error': None, 'successful': True}
    if isinstance(result, dict):
        if not result.get("successful"):
            print(f"Warning: {tool_slug} not successful: {result.get('error')}", file=sys.stderr)
            return None
        return result.get("data") or {}

    # Object form
    if hasattr(result, "successful") and not result.successful:
        print(f"Warning: {tool_slug} not successful: {getattr(result, 'error', 'unknown')}", file=sys.stderr)
        return None
    return getattr(result, "data", {}) or {}


def _sanitize_email(data: dict) -> dict:
    """Sanitize external text fields in an email dict."""
    try:
        parent_dir = str(Path(__file__).resolve().parent.parent)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from sanitize import sanitize_dict
        return sanitize_dict(data, "gmail", ["subject", "from", "snippet", "body"])
    except ImportError:
        return data


def _normalize_message(msg: dict) -> dict:
    """Normalize a Composio message dict to the consistent internal format.

    Composio GMAIL_FETCH_EMAILS returns fields like:
        messageId, sender, subject, messageText, messageTimestamp, labelIds, threadId, to, preview
    We normalize to:
        id, threadId, snippet, labels, from, to, subject, date, [body]
    """
    preview = msg.get("preview") or {}
    snippet = preview.get("body", "") or msg.get("messageText", "")[:200]

    normalized = {
        "id": msg.get("messageId") or msg.get("id", ""),
        "threadId": msg.get("threadId", ""),
        "snippet": snippet,
        "labels": msg.get("labelIds", []),
        "from": msg.get("sender") or msg.get("from", ""),
        "to": msg.get("to", ""),
        "subject": msg.get("subject") or preview.get("subject", ""),
        "date": msg.get("messageTimestamp") or msg.get("date", ""),
    }
    if "messageText" in msg:
        normalized["body"] = msg["messageText"]

    return normalized


def triage(account: str = DEFAULT_ACCOUNT, max_results: int = 20, query: str | None = None) -> list[dict]:
    """List recent emails with metadata (subject, from, date, snippet).

    Returns list of message summaries in normalized format.
    """
    args: dict = {"user_id": "me", "maxResults": max_results}
    if query:
        # Composio's GMAIL_FETCH_EMAILS uses `query` (not `q` like the raw Gmail
        # API). Passing `q` is silently ignored — the search returns recent
        # inbox items unfiltered, which historically broke meeting prep's prior-
        # threads lookup. Discovered 2026-05-06.
        args["query"] = query

    data = _execute("GMAIL_FETCH_EMAILS", args, account=account)
    if data is None:
        return []

    messages = data.get("messages", [])
    return [_sanitize_email(_normalize_message(m)) for m in messages[:max_results]]


def query_messages(
    query: str,
    account: str = DEFAULT_ACCOUNT,
    max_results: int = 20,
) -> list[dict]:
    """Run a raw Gmail search (Gmail search syntax). Returns normalized messages."""
    data = _execute("GMAIL_FETCH_EMAILS", {
        "user_id": "me",
        "maxResults": max_results,
        "query": query,
    }, account=account)
    if data is None:
        return []
    return [_sanitize_email(_normalize_message(m)) for m in data.get("messages", [])[:max_results]]


def find_stale_unread(
    account: str = DEFAULT_ACCOUNT,
    hours: int = 4,
    exclude_ids: set[str] | None = None,
    max_results: int = 5,
) -> list[dict]:
    """Find UNREAD emails in INBOX older than `hours`, excluding already-notified IDs.

    Backstop for the diff-only processing model — catches emails that were
    seen in a prior heartbeat (not in new_email_ids) but never got a
    notification because the LLM judged them non-urgent at the time.
    """
    exclude_ids = exclude_ids or set()
    msgs = query_messages(
        f"is:unread in:inbox older_than:{hours}h",
        account=account,
        max_results=max_results * 3,  # over-fetch to survive exclusions
    )
    return [m for m in msgs if m.get("id") not in exclude_ids][:max_results]


def add_thread_label(
    thread_id: str,
    label_id: str,
    account: str = DEFAULT_ACCOUNT,
    dry_run: bool = False,
) -> bool:
    """Add a label to every message in a thread (e.g. Cortex/Waiting)."""
    if dry_run:
        print(f"[DRY-RUN] add_thread_label: thread={thread_id} label={label_id}", file=sys.stderr)
        return True
    data = _execute("GMAIL_MODIFY_THREAD_LABELS", {
        "user_id": "me",
        "thread_id": thread_id,
        "add_label_ids": [label_id],
        "remove_label_ids": [],
    }, account=account)
    return data is not None


def fetch_thread(thread_id: str, account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """Fetch all messages in a thread. Returns normalized messages, empty on failure.

    Slug history: this used GMAIL_FETCH_THREAD until Composio removed it — the
    call 404'd and every thread read as empty (verified 2026-08-15).
    GMAIL_FETCH_MESSAGE_BY_THREAD_ID is the replacement and returns the same
    {"messages": [...]} payload, so _normalize_message is unchanged.
    """
    if not thread_id:
        return []

    msgs: list[dict] = []
    page_token = ""
    for _ in range(MAX_THREAD_PAGES):
        args = {"user_id": "me", "thread_id": thread_id}
        if page_token:
            args["page_token"] = page_token
        data = _execute("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", args, account=account)
        if data is None:
            return []
        msgs.extend(data.get("messages") or [])
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break
    else:
        print(
            f"Warning: thread {thread_id} truncated at {MAX_THREAD_PAGES} pages",
            file=sys.stderr,
        )

    return [_sanitize_email(_normalize_message(m)) for m in msgs]


def verify_tool_slugs(account: str = DEFAULT_ACCOUNT) -> list[str]:
    """Return the slugs in TOOL_SLUGS that Composio no longer resolves.

    Transport and auth errors propagate rather than being reported as missing
    slugs — "could not check" and "the slug is gone" must stay distinguishable.
    """
    client = _client()
    user_id = account_user_ids().get(account, account)
    return [slug for slug in TOOL_SLUGS if not client.tools.get(user_id=user_id, tools=[slug])]


def read_message(message_id: str, account: str = DEFAULT_ACCOUNT, metadata_only: bool = False) -> dict:
    """Read a single email message.

    If metadata_only=True, fetches only headers and snippet (faster).
    Otherwise fetches full message content including body.
    """
    fmt = "metadata" if metadata_only else "full"
    data = _execute("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {
        "message_id": message_id,
        "format": fmt,
        "user_id": "me",
    }, account=account)

    if data is None:
        return {}

    # Response may be the message directly or nested under a key
    msg = data if ("messageId" in data or "id" in data or "sender" in data) else (data.get("message") or data)
    return _sanitize_email(_normalize_message(msg))


def create_draft(to: str, subject: str, body: str, account: str = DEFAULT_ACCOUNT, html: bool = False) -> dict:
    """Create a draft email.

    Returns draft data dict or empty dict on failure.
    """
    data = _execute("GMAIL_CREATE_EMAIL_DRAFT", {
        "recipient_email": to,
        "subject": subject,
        "body": body,
        "is_html": html,
        "user_id": "me",
    }, account=account)
    return data if data is not None else {}


def list_labels(account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """List all Gmail labels for the account."""
    data = _execute("GMAIL_LIST_LABELS", {"user_id": "me"}, account=account)
    if data is None:
        return []
    labels = data.get("labels", [])
    return labels if isinstance(labels, list) else []


def triage_all_accounts(max_results: int = 20) -> dict[str, list[dict]]:
    """Triage all configured accounts. Returns {account_alias: [messages]}."""
    return {account: triage(account=account, max_results=max_results) for account in account_user_ids()}


# ── Label Management ──────────────────────────────────────────────────


def _load_label_cache() -> dict[str, dict[str, str]]:
    """Load persisted label ID cache from disk."""
    if _LABELS_CACHE_PATH.exists():
        try:
            with open(_LABELS_CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_label_cache(cache: dict[str, dict[str, str]]):
    """Persist label ID cache to disk."""
    _LABELS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LABELS_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def get_or_create_label(name: str, account: str = DEFAULT_ACCOUNT) -> str | None:
    """Get label ID by name, creating if missing. Caches IDs in memory and on disk.

    Returns label ID string, or None on failure.
    """
    global _label_id_cache

    if account in _label_id_cache and name in _label_id_cache[account]:
        return _label_id_cache[account][name]

    if account not in _label_id_cache:
        disk_cache = _load_label_cache()
        if account in disk_cache:
            _label_id_cache[account] = disk_cache[account]
            if name in _label_id_cache[account]:
                return _label_id_cache[account][name]

    existing = list_labels(account=account)
    if account not in _label_id_cache:
        _label_id_cache[account] = {}
    for label in existing:
        lname = label.get("name", "")
        lid = label.get("id", "")
        _label_id_cache[account][lname] = lid

    if name in _label_id_cache.get(account, {}):
        _save_label_cache(_label_id_cache)
        return _label_id_cache[account][name]

    data = _execute("GMAIL_CREATE_LABEL", {
        "label_name": name,
        "user_id": "me",
        "label_list_visibility": "labelShow",
        "message_list_visibility": "show",
    }, account=account)
    if data is not None:
        lid = data.get("id", "")
        if lid:
            if account not in _label_id_cache:
                _label_id_cache[account] = {}
            _label_id_cache[account][name] = lid
            _save_label_cache(_label_id_cache)
            return lid

    print(f"Warning: could not create label '{name}' for account '{account}'", file=sys.stderr)
    return None


def ensure_labels(account: str = DEFAULT_ACCOUNT) -> dict[str, str]:
    """Idempotent setup: create Cortex/* labels if they don't exist.

    Returns: {label_name: label_id} for all Cortex labels.
    """
    cortex_labels = [
        "Cortex/Digest",
        "Cortex/Reference",
        "Cortex/Noise",
        "Cortex/Waiting",
        # Anything the owner labels Cortex/Capture is pulled into the capture queue
        # and the label is removed once queued.
        "Cortex/Capture",
    ]
    result = {}
    for name in cortex_labels:
        lid = get_or_create_label(name, account=account)
        if lid:
            result[name] = lid
    return result


def modify_labels(
    message_id: str,
    add_labels: list[str],
    remove_labels: list[str],
    account: str = DEFAULT_ACCOUNT,
    dry_run: bool = False,
) -> bool:
    """Modify labels on a single message.

    Args:
        message_id: Gmail message ID
        add_labels: Label IDs to add
        remove_labels: Label IDs to remove
        account: Account alias
        dry_run: If True, logs action but does not execute

    Returns:
        True on success, False on failure.
    Safety: refuses to remove INBOX from STARRED messages.
    """
    if dry_run:
        print(
            f"[DRY-RUN] modify_labels: msg={message_id} add={add_labels} remove={remove_labels} account={account}",
            file=sys.stderr,
        )
        return True

    if "INBOX" in remove_labels:
        msg_data = _execute("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {
            "message_id": message_id,
            "format": "metadata",
            "user_id": "me",
        }, account=account)
        if msg_data is not None:
            msg_labels = msg_data.get("labelIds", [])
            if "STARRED" in msg_labels:
                print(f"Warning: refusing to archive STARRED message {message_id}", file=sys.stderr)
                return False

    data = _execute("GMAIL_ADD_LABEL_TO_EMAIL", {
        "message_id": message_id,
        "add_label_ids": add_labels,
        "remove_label_ids": remove_labels,
        "user_id": "me",
    }, account=account)
    return data is not None


def batch_modify(
    message_ids: list[str],
    add_labels: list[str],
    remove_labels: list[str],
    account: str = DEFAULT_ACCOUNT,
    dry_run: bool = False,
) -> bool:
    """Bulk label change for multiple messages. Caps at 50 per call.

    Returns True if all batches succeeded.
    """
    if not message_ids:
        return True

    if dry_run:
        print(
            f"[DRY-RUN] batch_modify: {len(message_ids)} msgs add={add_labels} remove={remove_labels} account={account}",
            file=sys.stderr,
        )
        return True

    success = True
    for i in range(0, len(message_ids), 50):
        batch = message_ids[i:i + 50]
        data = _execute("GMAIL_BATCH_MODIFY_MESSAGES", {
            "messageIds": batch,
            "addLabelIds": add_labels,
            "removeLabelIds": remove_labels,
            "userId": "me",
        }, account=account)
        # batchModify returns 204 no content on success → data = {}
        # None means actual failure
        if data is None:
            print(f"Warning: batch_modify failed for batch {i // 50 + 1}", file=sys.stderr)
            success = False

    return success


def archive(
    message_id: str,
    account: str = DEFAULT_ACCOUNT,
    dry_run: bool = False,
) -> bool:
    """Archive a message by removing the INBOX label.

    Rate-limited: 100ms delay to avoid quota exhaustion.
    """
    time.sleep(0.1)
    return modify_labels(
        message_id,
        add_labels=[],
        remove_labels=["INBOX"],
        account=account,
        dry_run=dry_run,
    )


def mark_read(
    message_id: str,
    account: str = DEFAULT_ACCOUNT,
    dry_run: bool = False,
) -> bool:
    """Mark a message as read by removing the UNREAD label."""
    return modify_labels(
        message_id,
        add_labels=[],
        remove_labels=["UNREAD"],
        account=account,
        dry_run=dry_run,
    )


def trash_message(
    message_id: str,
    account: str = DEFAULT_ACCOUNT,
    dry_run: bool = False,
) -> bool:
    """Move message to trash. Only for confirmed spam — never automatic.

    Returns True on success.
    """
    if dry_run:
        print(f"[DRY-RUN] trash_message: msg={message_id} account={account}", file=sys.stderr)
        return True

    data = _execute("GMAIL_MOVE_TO_TRASH", {
        "message_id": message_id,
        "user_id": "me",
    }, account=account)
    return data is not None


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from registry import load_env
    load_env()
    results = triage(max_results=3)
    print(f"Gmail triage: {len(results)} messages")
    print(json.dumps(results[:1], indent=2) if results else "No messages")
