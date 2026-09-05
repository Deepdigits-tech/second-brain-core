"""Tests for the new memory-index-shape hygiene checks (batch 6, task 4).

MEMORY.md became a small index pointing at vault/entities|topics|decisions
pages. These checks replace the old line-count and bullet-length caps, which
failed every week once that restructure landed.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
import hygiene


def _findings(check, root, monkeypatch):
    monkeypatch.setattr(hygiene, "PROJECT_ROOT", root)
    monkeypatch.setattr(hygiene, "VAULT_DIR", root / "vault")
    monkeypatch.setattr(hygiene, "MEMORY_MD", root / "vault" / "MEMORY.md")
    out = []
    check(out)
    return out


def _write(root, rel, content=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _clean_vault(root, extra_index_lines=""):
    """A clean index + one linked page per category."""
    _write(root, "vault/entities/acme-person.md", "# Acme Person\n")
    _write(root, "vault/topics/gtm.md", "# GTM\n")
    _write(root, "vault/decisions/2026-08-01-thing.md", "# Decision\n")
    index = (
        "# MEMORY — Index\n\n"
        "## Active Items\n"
        "- (2026-08-25) Acme call Tue 2:30pm PT -> [[opportunities/acme]]\n\n"
        "## Entity Pages\n"
        "- [[entities/acme-person]] — GTM engineer\n\n"
        "## Topic Pages\n"
        "- [[topics/gtm]] — GTM playbooks\n\n"
        "## Decision Archives\n"
        "- [[decisions/2026-08-01-thing]] — decided X\n\n"
        "## Preferences\n"
        "- Plain wording\n\n"
        + extra_index_lines
        + "---\nfooter\n"
    )
    _write(root, "vault/opportunities/acme/index.md", "# Acme\n")
    _write(root, "vault/MEMORY.md", index)
    return index


# ── check_memory_index_size ─────────────────────────────────────────────

def test_memory_index_size_clean(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    out = _findings(hygiene.check_memory_index_size, tmp_path, monkeypatch)
    assert out == []


def test_memory_index_size_over_limit(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    # Pad the index past MEMORY_INDEX_MAX_BYTES (5_120).
    padding = "x" * 6000
    mem = tmp_path / "vault" / "MEMORY.md"
    mem.write_text(mem.read_text() + "\n<!-- " + padding + " -->\n")
    out = _findings(hygiene.check_memory_index_size, tmp_path, monkeypatch)
    assert len(out) == 1
    assert out[0].check == "memory_index_size"


# ── check_memory_index_links (dangling [[x]]) ───────────────────────────

def test_memory_index_links_clean(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    out = _findings(hygiene.check_memory_index_links, tmp_path, monkeypatch)
    assert out == []


def test_memory_index_links_dangling(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    mem = tmp_path / "vault" / "MEMORY.md"
    mem.write_text(mem.read_text() + "- [[entities/nobody]] — ghost\n")
    out = _findings(hygiene.check_memory_index_links, tmp_path, monkeypatch)
    assert len(out) == 1
    assert out[0].check == "memory_index_links"
    assert "entities/nobody" in out[0].issue


def test_memory_index_links_alias_and_heading_resolve(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    mem = tmp_path / "vault" / "MEMORY.md"
    mem.write_text(
        mem.read_text()
        + "- [[entities/acme-person|Alias]] — aliased link\n"
        + "- [[entities/acme-person#Heading]] — heading link\n"
    )
    out = _findings(hygiene.check_memory_index_links, tmp_path, monkeypatch)
    assert out == []


def test_memory_index_links_dangling_alias_is_flagged(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    mem = tmp_path / "vault" / "MEMORY.md"
    mem.write_text(mem.read_text() + "- [[entities/nope|Alias]] — dead aliased link\n")
    out = _findings(hygiene.check_memory_index_links, tmp_path, monkeypatch)
    assert len(out) == 1
    assert out[0].check == "memory_index_links"
    assert "entities/nope" in out[0].issue


def test_memory_index_links_fenced_dead_link_skipped(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    mem = tmp_path / "vault" / "MEMORY.md"
    mem.write_text(
        mem.read_text()
        + "```\n"
        + "- [[entities/nobody]] — example syntax, not a real link\n"
        + "```\n"
        + "- [[entities/alsonobody]] — this one is real and dead\n"
    )
    out = _findings(hygiene.check_memory_index_links, tmp_path, monkeypatch)
    assert len(out) == 1
    assert "entities/alsonobody" in out[0].issue
    assert "entities/nobody" not in out[0].issue


def test_broken_references_skips_backticked_placeholders(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    mem = tmp_path / "vault" / "MEMORY.md"
    # Same placeholder shape as the CLAUDE.md "Writing to the Vault" table
    # footer: angle-bracket placeholders inside backticks, not real paths.
    mem.write_text(
        mem.read_text()
        + "See `vault/entities/<name>.md`, `vault/topics/<name>.md`, "
        + "`vault/decisions/<name>.md` for page conventions.\n"
    )
    out = _findings(hygiene.check_broken_references_in_memory_md, tmp_path, monkeypatch)
    assert out == []


# ── check_pages_missing_from_index ──────────────────────────────────────

def test_pages_missing_from_index_clean(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    out = _findings(hygiene.check_pages_missing_from_index, tmp_path, monkeypatch)
    assert out == []


def test_pages_missing_from_index_orphan(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    _write(tmp_path, "vault/entities/orphan.md", "# Orphan\n")
    out = _findings(hygiene.check_pages_missing_from_index, tmp_path, monkeypatch)
    assert len(out) == 1
    assert out[0].check == "pages_missing_from_index"
    assert "entities/orphan" in out[0].path or "entities/orphan" in out[0].issue


def test_pages_missing_from_index_backup_exempt(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    _write(tmp_path, "vault/decisions/pre-migration-backup.md", "# Backup\n")
    out = _findings(hygiene.check_pages_missing_from_index, tmp_path, monkeypatch)
    assert out == []


# ── check_active_items_stale ────────────────────────────────────────────

def test_active_items_stale_clean(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    out = _findings(hygiene.check_active_items_stale, tmp_path, monkeypatch)
    assert out == []


def test_active_items_stale_iso_date(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    old = datetime.now() - timedelta(days=40)
    line = f"- ({old.strftime('%Y-%m-%d')}) Old thing -> [[opportunities/acme]]\n"
    mem = tmp_path / "vault" / "MEMORY.md"
    text = mem.read_text().replace(
        "## Active Items\n",
        "## Active Items\n" + line,
    )
    mem.write_text(text)
    out = _findings(hygiene.check_active_items_stale, tmp_path, monkeypatch)
    assert len(out) == 1
    assert out[0].check == "active_items_stale"


def test_active_items_stale_short_date(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    old = datetime.now() - timedelta(days=40)
    line = f"- ({old.strftime('%b')} {old.day}) Old thing -> [[opportunities/acme]]\n"
    mem = tmp_path / "vault" / "MEMORY.md"
    text = mem.read_text().replace(
        "## Active Items\n",
        "## Active Items\n" + line,
    )
    mem.write_text(text)
    out = _findings(hygiene.check_active_items_stale, tmp_path, monkeypatch)
    assert len(out) == 1
    assert out[0].check == "active_items_stale"


def test_parse_active_item_date_cross_year_past():
    # "(Dec 20)" read on Jan 5 of the following year is ~16 days old, not
    # ~350 days in the future.
    now = datetime(2027, 1, 5)
    parsed = hygiene._parse_active_item_date("- (Dec 20) Old thing", now=now)
    assert parsed == datetime(2026, 12, 20)
    assert (now - parsed).days == 16


def test_parse_active_item_date_cross_year_future_not_stale():
    # "(Sep 8)" read on Sep 2 of the same year is a few days in the future,
    # not a stale date from last year.
    now = datetime(2026, 9, 2)
    parsed = hygiene._parse_active_item_date("- (Sep 8) Upcoming thing", now=now)
    assert parsed == datetime(2026, 9, 8)
    assert parsed > now


# ── check_page_sizes ─────────────────────────────────────────────────────

def test_page_sizes_clean(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    out = _findings(hygiene.check_page_sizes, tmp_path, monkeypatch)
    assert out == []


def test_page_sizes_over_warn(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    _write(tmp_path, "vault/entities/big.md", "x" * 9000)
    _write_index_link(tmp_path, "entities/big")
    out = _findings(hygiene.check_page_sizes, tmp_path, monkeypatch)
    assert len(out) == 1
    assert out[0].check == "page_sizes"
    assert out[0].severity == "low"


def test_page_sizes_exempts_pre_migration_backup(tmp_path, monkeypatch):
    _clean_vault(tmp_path)
    _write(tmp_path, "vault/decisions/pre-migration-backup.md", "x" * 40_000)
    out = _findings(hygiene.check_page_sizes, tmp_path, monkeypatch)
    assert out == []


def _write_index_link(root, ref):
    mem = root / "vault" / "MEMORY.md"
    mem.write_text(mem.read_text() + f"- [[{ref}]] — big page\n")


# ── registry / removed checks ────────────────────────────────────────────

def test_old_checks_removed():
    assert not hasattr(hygiene, "check_memory_md_bullet_length")
    assert not hasattr(hygiene, "check_memory_md_total_lines")
    assert not hasattr(hygiene, "MEMORY_BULLET_MAX_CHARS")
    assert not hasattr(hygiene, "MEMORY_LINE_WARN")
    assert not hasattr(hygiene, "MEMORY_LINE_HARD")


def test_registry_contains_new_checks():
    names = {c.__name__ for c in hygiene._CHECKS}
    for expected in (
        "check_memory_index_size",
        "check_memory_index_links",
        "check_pages_missing_from_index",
        "check_active_items_stale",
        "check_page_sizes",
    ):
        assert expected in names
    assert "check_memory_md_bullet_length" not in names
    assert "check_memory_md_total_lines" not in names
    # check_broken_references_in_memory_md stays
    assert "check_broken_references_in_memory_md" in names
