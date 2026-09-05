"""Tests for the draft lifecycle module."""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import drafts  # noqa: E402


def _make_vault(tmp_path: Path) -> Path:
    (tmp_path / "drafts" / "active").mkdir(parents=True)
    (tmp_path / "drafts" / "expired").mkdir(parents=True)
    (tmp_path / "drafts" / "sent").mkdir(parents=True)
    return tmp_path


def test_load_draft_round_trip(tmp_path: Path):
    vault = _make_vault(tmp_path)
    path = vault / "drafts" / "active" / "2026-05-12_email_test.md"
    path.write_text(
        '---\n'
        'type: email\n'
        'source_id: abc123\n'
        'thread_id: thread_xyz\n'
        'account: work-account\n'
        'recipient: test@example.com\n'
        'subject: "Re: Hello"\n'
        'created: 2026-05-12T06:00:00-07:00\n'
        'status: active\n'
        '---\n\n'
        '## Original Message\nHello there.\n\n'
        '## Draft Reply\nHi back.\n'
    )

    meta, body = drafts.load_draft(path)

    assert meta["thread_id"] == "thread_xyz"
    assert meta["recipient"] == "test@example.com"
    assert meta["status"] == "active"
    assert "Draft Reply" in body


def test_iter_active_drafts_sorted(tmp_path: Path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    for name in ("2026-05-12_email_b.md", "2026-05-12_email_a.md", "2026-05-11_email_c.md"):
        (vault / "drafts" / "active" / name).write_text("---\n---\n")

    paths = list(drafts.iter_active_drafts())
    names = [p.name for p in paths]
    assert names == ["2026-05-11_email_c.md", "2026-05-12_email_a.md", "2026-05-12_email_b.md"]



def test_save_draft_round_trips_through_load_draft(tmp_path: Path):
    path = tmp_path / "x.md"
    meta = {
        "type": "email",
        "source_id": "abc",
        "thread_id": "thr1",
        "recipient": "recipient@example.com",
        "subject": "Re: Hi",
        "status": "active",
    }
    body = "## Original Message\nHello.\n\n## Draft Reply\nHi back.\n"
    drafts.save_draft(path, meta, body)

    loaded_meta, loaded_body = drafts.load_draft(path)
    assert loaded_meta["thread_id"] == "thr1"
    assert loaded_meta["recipient"] == "recipient@example.com"
    assert "Draft Reply" in loaded_body


def _make_active_draft(vault: Path, source_id: str, thread_id: str, created: datetime, recipient: str = "recipient@example.com") -> Path:
    name = created.strftime("%Y-%m-%d") + "_email_test.md"
    path = vault / "drafts" / "active" / name
    path.write_text(
        f"---\n"
        f"type: email\n"
        f"source_id: {source_id}\n"
        f"thread_id: {thread_id}\n"
        f"account: work-account\n"
        f"recipient: {recipient}\n"
        f'subject: "Re: x"\n'
        f"created: {created.isoformat()}\n"
        f"status: active\n"
        f"---\n\n## Draft Reply\ntext\n"
    )
    return path


def test_reconcile_moves_draft_to_sent_when_reply_exists(tmp_path: Path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(hours=2)
    path = _make_active_draft(vault, "src_1", "thread_1", created)

    sent_msg = {
        "id": "sent_msg",
        "threadId": "thread_1",
        "from": "owner@example.com",
        "date": (created + timedelta(minutes=30)).isoformat(),
    }

    with patch("drafts._fetch_thread") as fetch_mock, \
         patch("drafts._decline_matching_tasks") as decline_mock:
        fetch_mock.return_value = [sent_msg]
        decline_mock.return_value = 1
        result = drafts.reconcile_active_drafts(owner_emails={"owner@example.com"})

    assert result["moved_to_sent"] == 1
    assert result["expired"] == 0
    assert result["declined_tasks"] == 1
    assert not path.exists()
    sent_path = vault / "drafts" / "sent" / path.name
    assert sent_path.exists()
    meta, _ = drafts.load_draft(sent_path)
    assert meta["status"] == "sent"
    assert "sent_at" in meta
    decline_mock.assert_called_once_with("src_1")


def test_reconcile_expires_draft_with_no_reply_after_24h(tmp_path: Path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(hours=30)
    path = _make_active_draft(vault, "src_2", "thread_2", created)

    with patch("drafts._fetch_thread", return_value=[]):
        result = drafts.reconcile_active_drafts()

    assert result["expired"] == 1
    assert not path.exists()
    assert (vault / "drafts" / "expired" / path.name).exists()


def test_reconcile_keeps_recent_draft_with_no_reply(tmp_path: Path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(hours=3)
    path = _make_active_draft(vault, "src_3", "thread_3", created)

    with patch("drafts._fetch_thread", return_value=[]):
        result = drafts.reconcile_active_drafts()

    assert result["kept"] == 1
    assert path.exists()


def test_reconcile_surfaces_dead_gmail_slug(tmp_path: Path, monkeypatch):
    """A removed Composio slug must not masquerade as 'no reply on the thread'.

    Swallowing it here files every replied-to draft as expired instead of sent.
    """
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(hours=30)
    path = _make_active_draft(vault, "src_dead", "thread_dead", created)

    with patch("drafts._fetch_thread", side_effect=drafts.ToolNotFoundError("gone")):
        with pytest.raises(drafts.ToolNotFoundError):
            drafts.reconcile_active_drafts()

    assert path.exists(), "draft must not be expired on a tooling failure"


def test_reconcile_applies_cortex_waiting_label(tmp_path: Path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(hours=2)
    _make_active_draft(vault, "src_w", "thread_w", created)
    sent_msg = {"id": "m", "threadId": "thread_w", "from": "owner@example.com",
                "date": (created + timedelta(minutes=10)).isoformat()}

    with patch("drafts._fetch_thread", return_value=[sent_msg]), \
         patch("drafts._apply_waiting_label") as label_mock, \
         patch("drafts._decline_matching_tasks", return_value=0):
        drafts.reconcile_active_drafts(owner_emails={"owner@example.com"})

    label_mock.assert_called_once_with("thread_w", "work-account")


def test_load_waiting_followups_filters_to_unanswered(monkeypatch):
    sample_threads = [
        # Thread waiting for >3d with no inbound after owner's last message
        {"id": "thr1", "from": "owner@example.com", "subject": "Re: meet", "date": "2026-05-08T10:00:00Z"},
        # Thread waiting but inbound reply received yesterday → not stale
        {"id": "thr2", "from": "sender@example.com", "subject": "Re: meet", "date": "2026-05-11T10:00:00Z"},
    ]
    with patch("drafts._query_waiting_threads") as q_mock, \
         patch("drafts._thread_last_inbound_age_days") as age_mock:
        q_mock.return_value = sample_threads
        # thr1: 4 days since last inbound. thr2: 1 day since last inbound.
        age_mock.side_effect = lambda thread_id, account, owner_emails: 4 if thread_id == "thr1" else 1

        result = drafts.load_waiting_followups(account="work-account", min_days=3)

    assert len(result) == 1
    assert result[0]["thread_id"] == "thr1"


# ---------------------------------------------------------------------------
# thread_id is model-supplied and was frequently a MESSAGE id
#
# The heartbeat prompt showed the model only `**ID:** <message id>` and then
# asked it to fill `thread_id` "from the inbound email's threadId" — a value it
# was never given. So it wrote the message id. Verified 2026-08-29 against the
# a real mailbox: draft thread_id t-abc123 404s as a thread but reads
# fine as a message, whose real threadId is t-def456.
#
# The visible symptom was a 404 in the heartbeat log every 30 minutes. The real
# cost was silent: a reply was never detected, so the draft expired instead of
# being filed to sent/ — losing the drafted-vs-sent gap that `brain skills`
# reads.
# ---------------------------------------------------------------------------

def test_reconcile_repairs_a_thread_id_that_is_really_a_message_id(tmp_path: Path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(hours=2)
    # thread_id here is actually the message id — what the model wrote.
    path = _make_active_draft(vault, "src_1", "msg_id_not_a_thread", created)

    reply = {
        "id": "sent_msg",
        "threadId": "real_thread",
        "from": "owner@example.com",
        "date": (created + timedelta(minutes=30)).isoformat(),
    }

    def fake_fetch(thread_id, account):
        return [reply] if thread_id == "real_thread" else []

    def fake_read(message_id, account, metadata_only=False):
        if message_id == "msg_id_not_a_thread":
            return {"id": message_id, "threadId": "real_thread"}
        return {}

    with patch("drafts._fetch_thread", side_effect=fake_fetch), \
         patch("drafts._read_message", side_effect=fake_read), \
         patch("drafts._decline_matching_tasks", return_value=0):
        result = drafts.reconcile_active_drafts(owner_emails={"owner@example.com"})

    assert result["moved_to_sent"] == 1, "the reply must be found via the repaired thread id"
    assert result["expired"] == 0


def test_repaired_thread_id_is_written_back_to_the_draft(tmp_path: Path, monkeypatch):
    """Repair once, not every 30 minutes forever."""
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(minutes=10)  # too fresh to expire
    path = _make_active_draft(vault, "src_1", "msg_id_not_a_thread", created)

    # The real thread exists and holds the inbound message the owner has not yet
    # replied to — so the draft stays active, but the id must still be fixed.
    inbound = {"id": "inbound", "threadId": "real_thread",
               "from": "correspondent@example.com", "date": created.isoformat()}

    def fake_fetch(thread_id, account):
        return [inbound] if thread_id == "real_thread" else []

    with patch("drafts._fetch_thread", side_effect=fake_fetch), \
         patch("drafts._read_message", return_value={"threadId": "real_thread"}):
        result = drafts.reconcile_active_drafts(owner_emails={"owner@example.com"})

    assert result["kept"] == 1, "no reply yet — the draft stays active"
    meta, _ = drafts.load_draft(path)
    assert meta["thread_id"] == "real_thread", "but the broken id is repaired on disk"


def test_a_working_thread_id_costs_no_extra_lookup(tmp_path: Path, monkeypatch):
    """The happy path must not pay an API call for a problem it does not have."""
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(hours=2)
    _make_active_draft(vault, "src_1", "thread_1", created)

    reply = {
        "id": "sent_msg", "threadId": "thread_1",
        "from": "owner@example.com",
        "date": (created + timedelta(minutes=30)).isoformat(),
    }

    with patch("drafts._fetch_thread", return_value=[reply]), \
         patch("drafts._read_message") as read_mock, \
         patch("drafts._decline_matching_tasks", return_value=0):
        drafts.reconcile_active_drafts(owner_emails={"owner@example.com"})

    read_mock.assert_not_called()


def test_unrepairable_draft_is_left_alone_not_crashed(tmp_path: Path, monkeypatch):
    """Genuinely deleted mail: both lookups fail. Behave exactly as before."""
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(hours=30)  # past expiry
    _make_active_draft(vault, "src_1", "gone", created)

    with patch("drafts._fetch_thread", return_value=[]), \
         patch("drafts._read_message", return_value={}):
        result = drafts.reconcile_active_drafts(owner_emails={"owner@example.com"})

    assert result["expired"] == 1
    assert result["moved_to_sent"] == 0


def test_repair_is_counted_so_it_cannot_heal_silently(tmp_path: Path, monkeypatch):
    """Self-healing that says nothing hides a prompt that is still broken.

    The repair keeps the draft working, so without a counter the only evidence
    that the model is STILL writing message ids would be a stderr line nobody
    reads. The heartbeat notifies on this count.
    """
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(minutes=10)
    _make_active_draft(vault, "src_1", "msg_id_not_a_thread", created)

    inbound = {"id": "inbound", "threadId": "real_thread",
               "from": "correspondent@example.com", "date": created.isoformat()}

    def fake_fetch(thread_id, account):
        return [inbound] if thread_id == "real_thread" else []

    with patch("drafts._fetch_thread", side_effect=fake_fetch), \
         patch("drafts._read_message", return_value={"threadId": "real_thread"}):
        result = drafts.reconcile_active_drafts(owner_emails={"owner@example.com"})

    assert result["thread_ids_repaired"] == 1


def test_a_healthy_draft_reports_no_repairs(tmp_path: Path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(minutes=10)
    _make_active_draft(vault, "src_1", "thread_1", created)

    inbound = {"id": "inbound", "threadId": "thread_1",
               "from": "correspondent@example.com", "date": created.isoformat()}

    with patch("drafts._fetch_thread", return_value=[inbound]), \
         patch("drafts._read_message") as read_mock:
        result = drafts.reconcile_active_drafts(owner_emails={"owner@example.com"})

    assert result["thread_ids_repaired"] == 0
    read_mock.assert_not_called()


def test_a_failed_reply_check_is_reported_not_swallowed(tmp_path: Path, monkeypatch, capsys):
    """The broad catch must not hide programming errors.

    A NameError in a log line inside this block silently turned reply detection
    off: the draft just expired, which looks identical to "no reply came".
    """
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(drafts, "VAULT_DIR", vault)
    monkeypatch.setattr(drafts, "DRAFTS_ACTIVE", vault / "drafts" / "active")
    monkeypatch.setattr(drafts, "DRAFTS_EXPIRED", vault / "drafts" / "expired")
    monkeypatch.setattr(drafts, "DRAFTS_SENT", vault / "drafts" / "sent")

    created = datetime.now(timezone.utc) - timedelta(minutes=10)
    _make_active_draft(vault, "src_1", "thread_1", created)

    with patch("drafts._fetch_thread", side_effect=RuntimeError("gmail is down")):
        result = drafts.reconcile_active_drafts(owner_emails={"owner@example.com"})

    err = capsys.readouterr().err
    assert "reply check failed" in err
    assert "RuntimeError" in err and "gmail is down" in err
    assert result["kept"] == 1, "the draft survives — one bad call must not expire it"


def test_module_can_log_without_a_function_level_import():
    """`sys` must be imported at module level.

    It used to be imported inside one function, so every other `file=sys.stderr`
    in this module raised NameError the moment it was reached.
    """
    assert hasattr(drafts, "sys"), "drafts.py must import sys at module scope"
