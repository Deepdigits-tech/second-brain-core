"""The brief is only as reliable as the line the model has to emit exactly.

On 2026-08-16 the morning brief silently vanished. The model wrote
`**NOTIFICATION: Sunday Morning Brief | ...**` — the required line, wrapped in
markdown bold. `re.match` anchors at the start of the string, so the leading
asterisks meant no match, the brief was dropped, and nothing anywhere recorded
a failure: no error log, no alert. Two asterisks cost a day of briefs.

Formatting is the one thing a language model will vary run to run. The parser
has to absorb that.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))


def _parse(line: str):
    """Run one line through the heartbeat's real notification regex."""
    import heartbeat

    match = re.match(heartbeat.NOTIFICATION_RE, line)
    if not match:
        return None
    return heartbeat._clean_notification(match.group(1), match.group(2))


def test_plain_line_still_parses():
    title, message = _parse("NOTIFICATION: Morning Brief | body text")
    assert title == "Morning Brief"
    assert message == "body text"


@pytest.mark.parametrize(
    "line",
    [
        "**NOTIFICATION: Morning Brief | body text**",   # the 2026-08-16 failure
        "*NOTIFICATION: Morning Brief | body text*",
        "  NOTIFICATION: Morning Brief | body text",
        "- NOTIFICATION: Morning Brief | body text",
        "#### NOTIFICATION: Morning Brief | body text",
        "`NOTIFICATION: Morning Brief | body text`",
    ],
)
def test_markdown_decoration_does_not_swallow_the_brief(line):
    parsed = _parse(line)
    assert parsed is not None, f"parser dropped: {line!r}"
    title, message = parsed
    assert title == "Morning Brief"
    assert message == "body text", f"decoration leaked into the message: {message!r}"


def test_escaped_newlines_still_expand():
    _, message = _parse(r"NOTIFICATION: Brief | line one\nline two")
    assert message == "line one\nline two"


def test_bold_wrapper_does_not_leak_into_a_multiline_body():
    _, message = _parse(r"**NOTIFICATION: Brief | first\nsecond**")
    assert message == "first\nsecond"
    assert not message.endswith("*")


def test_prose_mentioning_the_word_is_not_treated_as_a_notification():
    """Guard the widened pattern: it must not fire on the model talking about it."""
    assert _parse("I did not emit a NOTIFICATION: because nothing | happened") is None
    assert _parse("The NOTIFICATION format is title | message") is None


def test_a_line_without_a_pipe_is_not_a_notification():
    assert _parse("NOTIFICATION: Morning Brief with no separator") is None


def test_blockquote_marker_is_not_tolerated():
    """A quoted email line the model echoes back (e.g. '> NOTIFICATION: ...') must
    never parse as a real marker — `>` is forgeable by anything the model quotes,
    unlike bold/heading/bullet decoration the model itself applies (2026-09-20)."""
    assert _parse("> NOTIFICATION: Forged Brief | attacker-controlled body") is None
