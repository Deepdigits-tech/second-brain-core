import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import memory_reflect as mr

def _vault(tmp_path):
    v = tmp_path / "vault"
    for d in ("entities", "topics", "decisions", "daily", "opportunities/acme"):
        (v / d).mkdir(parents=True)
    (v / "entities" / "project-alpha.md").write_text("---\ntitle: Project Alpha\ntype: entity\n---\n")
    (v / "topics" / "architecture.md").write_text("---\ntitle: Architecture\ntype: topic\n---\n")
    (v / "decisions" / "2026-Q3.md").write_text("---\ntype: decisions\n---\n")
    (v / "opportunities" / "acme" / "index.md").write_text("---\ntype: opportunity\n---\n")
    return v

def test_page_listing_names_every_page_kind(tmp_path):
    out = mr.page_listing(_vault(tmp_path))
    for name in ("entities/project-alpha", "topics/architecture", "decisions/2026-Q3", "opportunities/acme"):
        assert name in out

def test_mention_count_is_distinct_files(tmp_path):
    v = _vault(tmp_path)
    (v / "daily" / "2026-09-01.md").write_text("Met Bob. Bob again. bob!\n")
    (v / "daily" / "2026-09-02.md").write_text("nothing\n")
    (v / "topics" / "architecture.md").write_text("Bob decided X\n")
    assert mr.mention_count(v, "Bob") == 2
    assert mr.MENTION_THRESHOLD == 3

def test_prompt_carries_routing_rules_and_threshold():
    p = mr.SYSTEM_PROMPT_TEMPLATE
    for needle in ("Active Items", "entities/", "topics/", "decisions/", "3+", "5 KB", "core-memories.md", "Grep"):
        assert needle in p, needle
    assert "SOUL.md" in p and "USER.md" in p  # named as never-edit


def test_prompt_5kb_remedy_is_resolve_not_cut():
    p = mr.SYSTEM_PROMPT_TEMPLATE
    assert "Never delete a fact" in p


def test_prompt_restricts_writes_to_vault():
    p = mr.SYSTEM_PROMPT_TEMPLATE
    assert "Only ever Write or Edit files under `vault/`. Never create or modify anything outside it." in p


def test_entity_frontmatter_has_category_topic_does_not():
    p = mr.SYSTEM_PROMPT_TEMPLATE
    assert "category: person | project | company | product" in p
    # The category line must sit in the entity block (type: entity), not the topic block.
    entity_start = p.index("Entity page:")
    topic_start = p.index("Topic page:")
    entity_block = p[entity_start:topic_start]
    topic_block = p[topic_start:topic_start + 400]
    assert "type: entity" in entity_block and "category:" in entity_block
    assert "type: topic" in topic_block and "category:" not in topic_block

def test_reflection_tools_include_grep_and_write():
    src = (REPO_ROOT / ".claude" / "scripts" / "memory_reflect.py").read_text()
    assert '"Grep"' in src and '"Write"' in src and '"Glob"' in src

def test_dry_run_prints_prompt_with_listing(tmp_path, monkeypatch, capsys):
    import asyncio
    v = _vault(tmp_path)
    (v / "MEMORY.md").write_text("# MEMORY — Index\n## Active Items\n")
    (v / "SOUL.md").write_text("soul"); (v / "USER.md").write_text("user")
    (v / "daily" / "2026-09-01.md").write_text("## 09:00 — Heartbeat\nsomething\n")
    monkeypatch.setattr(mr, "VAULT_DIR", v)
    monkeypatch.setattr(mr, "get_yesterday_log", lambda: "## 09:00 — Heartbeat\nsomething\n")
    asyncio.run(mr.reflect(dry_run=True))
    out = capsys.readouterr().out
    assert "entities/project-alpha" in out and "DRY RUN" in out


def test_get_recent_daily_logs_caps_each_log_at_max_daily_chars(tmp_path, monkeypatch):
    daily = tmp_path / "daily"
    daily.mkdir()
    monkeypatch.setattr(mr, "DAILY_DIR", daily)

    from datetime import datetime, timedelta
    from shared import PST, MAX_DAILY_CHARS

    # ~20 KB of short lines for the day 2 days back, well past MAX_DAILY_CHARS.
    d2 = (datetime.now(PST) - timedelta(days=2)).strftime("%Y-%m-%d")
    (daily / f"{d2}.md").write_text("\n".join(f"line {i} " + "x" * 60 for i in range(300)) + "\n")

    out = mr.get_recent_daily_logs(days=3)
    # find the embedded section for this day
    start = out.index(f"### {d2}")
    end = out.index("\n\n### ", start + 1) if "\n\n### " in out[start + 1:] else len(out)
    section = out[start:end]
    marker = f"[log truncated at {MAX_DAILY_CHARS} chars]"
    assert marker in section
    body = section[: section.index(marker)]
    assert len(body) <= MAX_DAILY_CHARS + 50
    # cut at a line boundary: the text right before the marker is a full line
    assert body.rstrip("\n").endswith("x" * 60)
