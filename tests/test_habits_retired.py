import re, sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import heartbeat

def test_heartbeat_no_longer_reads_habits():
    src = (REPO_ROOT / ".claude" / "scripts" / "heartbeat.py").read_text()
    assert "HABITS.md" not in src
    assert not re.search(r"\bhabits?\b", src, re.I), "habits lane should be gone from heartbeat.py"
    assert not hasattr(heartbeat, "check_habits_reset_needed")
    assert not hasattr(heartbeat, "is_nudge_time")

def test_system_prompt_has_no_habits_instruction():
    assert "habit" not in heartbeat.SYSTEM_PROMPT_TEMPLATE.lower()

def test_generator_has_no_habits_row():
    src = (REPO_ROOT / ".claude" / "scripts" / "generate_heartbeat_doc.py").read_text()
    assert "habit" not in src.lower()
