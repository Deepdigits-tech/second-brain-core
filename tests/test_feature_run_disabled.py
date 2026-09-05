import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import feature_run as fr


def test_disabled_feature_writes_a_disabled_record(tmp_path, monkeypatch):
    toggles = tmp_path / "toggles.json"
    timings = tmp_path / "timings.jsonl"
    toggles.write_text(json.dumps({"inbox.scan": False}))
    monkeypatch.setattr(fr, "TOGGLE_PATH", toggles)
    monkeypatch.setattr(fr, "TIMINGS_PATH", timings)
    out = fr.feature_run("inbox.scan", lambda: 42, default="skipped")
    assert out == "skipped"
    rec = json.loads(timings.read_text().strip().splitlines()[-1])
    assert rec["feature"] == "inbox.scan" and rec["status"] == "disabled" and rec["duration_ms"] == 0


def test_enabled_feature_still_writes_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "TOGGLE_PATH", tmp_path / "none.json")
    monkeypatch.setattr(fr, "TIMINGS_PATH", tmp_path / "t.jsonl")
    assert fr.feature_run("x", lambda: 1) == 1
    assert json.loads((tmp_path / "t.jsonl").read_text())["status"] == "ok"
