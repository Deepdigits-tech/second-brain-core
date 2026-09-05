"""Tests for new Gmail primitives: thread/sent/stale queries."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".claude" / "scripts"))
from integrations import gmail  # noqa: E402


def test_query_messages_passes_query_to_composio():
    with patch.object(gmail, "_execute") as exec_mock:
        exec_mock.return_value = {"messages": [
            {"messageId": "m1", "threadId": "t1", "sender": "sender@example.com", "subject": "x", "messageTimestamp": "2026-05-12T10:00:00Z"},
        ]}
        result = gmail.query_messages("in:sent after:1234", account="work-account", max_results=5)

    exec_mock.assert_called_once()
    _, args = exec_mock.call_args.args[0], exec_mock.call_args.args[1]
    assert args["query"] == "in:sent after:1234"
    assert args["maxResults"] == 5
    assert len(result) == 1
    assert result[0]["id"] == "m1"


def test_fetch_thread_returns_message_list():
    with patch.object(gmail, "_execute") as exec_mock:
        exec_mock.return_value = {
            "messages": [
                {"messageId": "m1", "threadId": "t1", "sender": "sender@example.com", "messageTimestamp": "2026-05-12T10:00:00Z"},
                {"messageId": "m2", "threadId": "t1", "sender": "owner@example.com", "messageTimestamp": "2026-05-12T11:00:00Z"},
            ],
        }
        thread = gmail.fetch_thread("t1", account="work-account")

    exec_mock.assert_called_once()
    tool_slug = exec_mock.call_args.args[0]
    args = exec_mock.call_args.args[1]
    assert tool_slug == "GMAIL_FETCH_MESSAGE_BY_THREAD_ID"
    assert args["thread_id"] == "t1"
    assert len(thread) == 2
    assert thread[0]["id"] == "m1"


def test_fetch_thread_follows_pagination():
    pages = [
        {"messages": [{"messageId": "m1", "threadId": "t1"}], "nextPageToken": "p2"},
        {"messages": [{"messageId": "m2", "threadId": "t1"}]},
    ]
    with patch.object(gmail, "_execute", side_effect=pages) as exec_mock:
        thread = gmail.fetch_thread("t1")

    assert [m["id"] for m in thread] == ["m1", "m2"]
    assert exec_mock.call_args_list[1].args[1]["page_token"] == "p2"


def test_execute_raises_on_removed_tool_slug():
    """A slug Composio has deleted must not read as 'no data'.

    This is the GMAIL_FETCH_THREAD regression: _execute swallowed the 404 into a
    stderr warning and returned None, so fetch_thread returned [].
    """
    class FakeNotFound(Exception):
        status_code = 404
        body = {"error": {"message": "Tool GMAIL_GONE not found",
                          "code": 2401, "slug": "Tool_ToolNotFound"}}

    client = MagicMock()
    client.tools.execute.side_effect = FakeNotFound()
    with patch.object(gmail, "_client", return_value=client):
        with pytest.raises(gmail.ToolNotFoundError) as exc:
            gmail._execute("GMAIL_GONE", {})

    assert "GMAIL_GONE" in str(exc.value)


def test_execute_still_returns_none_on_ordinary_failure():
    """Transient errors keep the old contract — only dead slugs raise."""
    client = MagicMock()
    client.tools.execute.side_effect = RuntimeError("connection reset")
    with patch.object(gmail, "_client", return_value=client):
        assert gmail._execute("GMAIL_FETCH_EMAILS", {}) is None


def test_fetch_thread_propagates_tool_not_found():
    with patch.object(gmail, "_execute", side_effect=gmail.ToolNotFoundError("gone")):
        with pytest.raises(gmail.ToolNotFoundError):
            gmail.fetch_thread("t1")


def test_find_stale_unread_filters_to_old_unread():
    with patch.object(gmail, "query_messages") as q_mock:
        q_mock.return_value = [
            {"id": "old1", "from": "sender@example.com", "subject": "important", "labels": ["INBOX", "UNREAD"]},
        ]
        result = gmail.find_stale_unread(account="work-account", hours=4, exclude_ids={"already_notified"})

    query = q_mock.call_args.args[0]
    assert "is:unread" in query
    assert "in:inbox" in query
    assert "older_than:4h" in query
    assert q_mock.call_args.kwargs.get("max_results") == 15
    assert result == [{"id": "old1", "from": "sender@example.com", "subject": "important", "labels": ["INBOX", "UNREAD"]}]


def test_find_stale_unread_skips_excluded():
    with patch.object(gmail, "query_messages") as q_mock:
        q_mock.return_value = [
            {"id": "seen", "from": "sender@example.com", "subject": "x", "labels": ["INBOX", "UNREAD"]},
            {"id": "fresh", "from": "contact@example.com", "subject": "y", "labels": ["INBOX", "UNREAD"]},
        ]
        result = gmail.find_stale_unread(account="work-account", hours=4, exclude_ids={"seen"})

    assert [m["id"] for m in result] == ["fresh"]


def test_add_thread_label_calls_correct_composio_tool():
    with patch.object(gmail, "_execute") as exec_mock:
        exec_mock.return_value = {}
        ok = gmail.add_thread_label("t1", "Label_42", account="work-account")

    assert ok is True
    tool_slug = exec_mock.call_args.args[0]
    args = exec_mock.call_args.args[1]
    assert tool_slug == "GMAIL_MODIFY_THREAD_LABELS"
    assert args["thread_id"] == "t1"
    assert "Label_42" in args["add_label_ids"]
