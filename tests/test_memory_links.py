"""The `[[wikilink]]` graph over the vault.

Built on request, against the evidence: only 46 of 680 notes contain any link,
142 in total, and the apparent hubs are mostly template placeholders —
`[[Department/Person]]`, `[[Target]]`, `[[Note Name]]` — living in prose inside
vendored third-party docs under `kb/aios/sources/raw/`. So every query here
takes a path scope, and the caller decides whose links to look at rather than
this module hardcoding a guess about which notes are "real".

A link is dead when nothing in the vault answers to it. That is a fact worth
reporting, not a judgement — an attachment or an intentionally-future note is
also "dead" by this definition, which is why the report is a list to read, not
an error.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import db as db_mod  # noqa: E402
import memory_index  # noqa: E402
import memory_links  # noqa: E402

EMBEDDING_DIM = 384


@pytest.fixture
def vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    db_path = tmp_path / "memory.db"

    def connect(_path=None):
        return db_mod.get_connection(db_path)

    monkeypatch.setattr(memory_index, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(memory_index, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(memory_index, "get_connection", connect)
    monkeypatch.setattr(memory_index, "embed_texts",
                        lambda texts: [[0.0] * EMBEDDING_DIM for _ in texts])
    monkeypatch.setattr(memory_links, "get_connection", connect)

    def write(rel: str, body: str) -> Path:
        path = vault_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    return type("Vault", (), {"dir": vault_dir, "write": staticmethod(write)})


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def test_finds_a_plain_link():
    assert memory_index.parse_links("see [[gold-silver]] for more") == ["gold-silver"]


def test_strips_the_display_half_of_a_piped_link():
    assert memory_index.parse_links("see [[gold-silver|the thesis]]") == ["gold-silver"]


def test_strips_a_heading_anchor():
    assert memory_index.parse_links("see [[gold-silver#Entry rule]]") == ["gold-silver"]


def test_keeps_a_relative_path_target():
    assert memory_index.parse_links("[[../concepts/gold-and-real-yields]]") == \
        ["../concepts/gold-and-real-yields"]


def test_ignores_a_single_bracketed_markdown_link():
    assert memory_index.parse_links("[a link](http://example.com)") == []


def test_finds_several_links_in_order():
    assert memory_index.parse_links("[[a]] then [[b]] and [[a]] again") == ["a", "b", "a"]


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

def test_a_link_resolves_to_the_note_it_names(vault):
    vault.write("investments/gold-silver.md", "# Gold\n\nthesis\n")
    vault.write("daily/today.md", "see [[gold-silver]]\n")
    memory_index.index_vault()

    links = memory_links.all_links()
    resolved = {(l["src"], l["dst"]) for l in links}
    assert ("vault/daily/today.md", "vault/investments/gold-silver.md") in resolved


def test_a_link_to_nothing_is_recorded_as_dead(vault):
    vault.write("daily/today.md", "see [[does-not-exist]]\n")
    memory_index.index_vault()

    dead = memory_links.dead_links()
    assert [(d["src"], d["target"]) for d in dead] == \
        [("vault/daily/today.md", "does-not-exist")]


def test_resolution_ignores_the_md_extension_either_way(vault):
    vault.write("notes/thing.md", "# Thing\n")
    vault.write("daily/today.md", "[[thing.md]] and [[thing]]\n")
    memory_index.index_vault()

    assert memory_links.dead_links() == []


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------

def test_hubs_rank_notes_by_how_many_point_at_them(vault):
    vault.write("hub.md", "# Hub\n")
    vault.write("quiet.md", "# Quiet\n")
    for i in range(3):
        vault.write(f"n{i}.md", "see [[hub]]\n")
    vault.write("one.md", "see [[quiet]]\n")
    memory_index.index_vault()

    ranked = memory_links.hubs()
    assert ranked[0]["dst"] == "vault/hub.md"
    assert ranked[0]["inbound"] == 3


def test_backlinks_list_who_points_at_a_note(vault):
    vault.write("target.md", "# Target\n")
    vault.write("a.md", "see [[target]]\n")
    vault.write("b.md", "also [[target]]\n")
    memory_index.index_vault()

    assert sorted(memory_links.backlinks("vault/target.md")) == ["vault/a.md", "vault/b.md"]


def test_a_note_linked_twice_from_one_file_counts_once_as_a_hub(vault):
    vault.write("hub.md", "# Hub\n")
    vault.write("a.md", "[[hub]] and again [[hub]]\n")
    memory_index.index_vault()

    assert memory_links.hubs()[0]["inbound"] == 1


# --------------------------------------------------------------------------
# scoping — the reason this module takes a path at all
# --------------------------------------------------------------------------

def test_dead_links_can_be_scoped_to_your_own_notes(vault):
    """Vendored source material's broken links are the author's, not Owner's."""
    vault.write("kb/sources/raw/vendor.md", "template [[Note Name]]\n")
    vault.write("daily/today.md", "mine [[also-missing]]\n")
    memory_index.index_vault()

    assert len(memory_links.dead_links()) == 2

    scoped = memory_links.dead_links(path_prefix="vault/daily")
    assert [d["target"] for d in scoped] == ["also-missing"]


def test_hubs_can_be_scoped_too(vault):
    vault.write("kb/hub.md", "# KB hub\n")
    vault.write("kb/a.md", "[[hub]]\n")
    vault.write("daily/mine.md", "# Mine\n")
    vault.write("daily/b.md", "[[mine]]\n")
    memory_index.index_vault()

    scoped = memory_links.hubs(path_prefix="vault/daily")
    assert [h["dst"] for h in scoped] == ["vault/daily/mine.md"]


# --------------------------------------------------------------------------
# upkeep
# --------------------------------------------------------------------------

def test_links_from_a_deleted_note_are_forgotten(vault):
    vault.write("target.md", "# Target\n")
    vault.write("gone.md", "[[target]]\n")
    memory_index.index_vault()
    assert memory_links.backlinks("vault/target.md") == ["vault/gone.md"]

    (vault.dir / "gone.md").unlink()
    memory_index.index_vault(incremental=True)
    assert memory_links.backlinks("vault/target.md") == []


def test_editing_a_note_replaces_its_links(vault):
    vault.write("a.md", "# A\n")
    vault.write("b.md", "# B\n")
    vault.write("src.md", "[[a]]\n")
    memory_index.index_vault()
    assert memory_links.backlinks("vault/a.md") == ["vault/src.md"]

    vault.write("src.md", "[[b]]\n")
    memory_index.index_vault(incremental=True)
    assert memory_links.backlinks("vault/a.md") == []
    assert memory_links.backlinks("vault/b.md") == ["vault/src.md"]


def test_an_empty_vault_reports_nothing_rather_than_failing(vault):
    assert memory_links.hubs() == []
    assert memory_links.dead_links() == []
    assert memory_links.backlinks("vault/nothing.md") == []
