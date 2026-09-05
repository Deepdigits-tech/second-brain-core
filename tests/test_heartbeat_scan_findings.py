"""Scan findings correctly unwrap the external_content trust boundary wrapper."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import heartbeat  # noqa: E402


def test_scan_findings_unwrap_the_trust_wrapper(tmp_path, monkeypatch):
    """The sender/subject fields should have external_content wrapper removed."""
    # Create output path for the scan findings
    output_file = tmp_path / "scan-findings.md"

    msg = {
        "from": '<external_content source="gmail">\nJane <jane@example.com>\n</external_content>',
        "subject": '<external_content source="gmail">\nWeekly roundup\n</external_content>',
        "body": "hello",
        "id": "m1",
    }

    findings = []  # Empty findings list for this test
    rule = {"extract": "jobs", "name": "test_rule"}

    heartbeat._append_scan_findings(output_file, msg, findings, rule)

    written = output_file.read_text()
    assert "Jane <jane@example.com>" in written and "Weekly roundup" in written
    assert "external_content" not in written


def test_dead_sanitize_helper_is_gone():
    """_sanitize_text function should be removed."""
    assert not hasattr(heartbeat, "_sanitize_text")
