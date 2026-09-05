"""Filtering search by a note's header block.

65% of the vault (444 of 680 notes) carries YAML frontmatter with fields that
are genuinely worth filtering on — `type` (email, kb-source, kb-topic),
`status` (active, processed, complete), `account` (work, personal, team),
`tags`, `created`. Until now search could not see any of it: you could search
the words in a note but not ask for "only the active ones".

The metadata is per-FILE, not per-chunk, so it is stored in its own table and
rebuilt by walking the vault. That costs seconds and needs no re-embedding —
unlike a chunker change, which forces the full 12-minute reindex.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))

import db as db_mod  # noqa: E402
import memory_index  # noqa: E402
import memory_search  # noqa: E402

EMBEDDING_DIM = 384


def fake_embed_doc(text: str) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    vec[0] = 1.0 if "RANK_HIGH" in text else 0.0
    return vec


def fake_embed_query(_text: str) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    vec[0] = 1.0
    return vec


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
                        lambda texts: [fake_embed_doc(t) for t in texts])
    monkeypatch.setattr(memory_search, "get_connection", connect)
    monkeypatch.setattr(memory_search, "embed_query", fake_embed_query)

    def write(rel: str, body: str) -> Path:
        path = vault_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    return type("Vault", (), {"dir": vault_dir, "write": staticmethod(write)})


def note(fields: str, body: str = "a note about gold") -> str:
    return f"---\n{fields}\n---\n\n# Heading\n\n{body}\n"


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def test_parses_simple_fields():
    fm = memory_index.parse_frontmatter(note("type: email\nstatus: active\n"))
    assert fm["type"] == ["email"]
    assert fm["status"] == ["active"]


def test_a_list_field_becomes_several_values():
    """`tags: [gtm, kb-source]` must let you filter on either tag."""
    fm = memory_index.parse_frontmatter(note("tags: [gtm, kb-source]\n"))
    assert sorted(fm["tags"]) == ["gtm", "kb-source"]


def test_values_are_normalised_to_strings():
    fm = memory_index.parse_frontmatter(note("created: 2026-05-29\ncount: 3\n"))
    assert fm["created"] == ["2026-05-29"]
    assert fm["count"] == ["3"]


def test_a_note_with_no_header_block_has_no_metadata():
    assert memory_index.parse_frontmatter("# Just a heading\n\ntext\n") == {}


def test_malformed_frontmatter_is_skipped_not_fatal():
    """One bad note must never cost the whole crawl."""
    broken = "---\ntype: email\n  bad: [unclosed\n---\n\nbody\n"
    assert isinstance(memory_index.parse_frontmatter(broken), dict)


def test_a_dashed_rule_in_the_body_is_not_frontmatter():
    assert memory_index.parse_frontmatter("# Title\n\ntext\n\n---\n\nmore\n") == {}


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def test_indexing_records_each_note_s_fields(vault):
    vault.write("a.md", note("type: kb-topic\nstatus: active\n"))
    memory_index.index_vault()

    meta = memory_index.file_metadata("vault/a.md", db_mod.get_connection(vault.dir.parent / "memory.db"))
    assert meta["type"] == ["kb-topic"]
    assert meta["status"] == ["active"]


def test_metadata_follows_the_file_when_its_fields_change(vault):
    vault.write("a.md", note("status: active\n"))
    memory_index.index_vault()
    vault.write("a.md", note("status: complete\n"))
    memory_index.index_vault(incremental=True)

    conn = db_mod.get_connection(vault.dir.parent / "memory.db")
    assert memory_index.file_metadata("vault/a.md", conn)["status"] == ["complete"]


def test_metadata_is_dropped_when_the_file_is(vault):
    vault.write("a.md", note("status: active\n"))
    vault.write("b.md", note("status: active\n"))
    memory_index.index_vault()

    (vault.dir / "b.md").unlink()
    memory_index.index_vault(incremental=True)

    conn = db_mod.get_connection(vault.dir.parent / "memory.db")
    assert memory_index.file_metadata("vault/b.md", conn) == {}
    assert memory_index.file_metadata("vault/a.md", conn)["status"] == ["active"]


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------

def sources(hits):
    """Unique files behind a result list.

    A note yields more than one chunk today — the YAML header block is
    indexed as its own chunk alongside the body — so the same source can
    appear twice. These tests are about WHICH files match, not how many
    pieces each was cut into.
    """
    return sorted({h["source"] for h in hits})


def test_where_narrows_results_to_matching_notes(vault):
    vault.write("topic.md", note("type: kb-topic\n", "RANK_HIGH gold analysis"))
    vault.write("email.md", note("type: email\n", "RANK_HIGH gold email"))
    memory_index.index_vault()

    hits = memory_search.hybrid_search("gold", limit=10, where=["type=kb-topic"])
    assert sources(hits) == ["vault/topic.md"]


def test_several_conditions_all_have_to_hold(vault):
    vault.write("a.md", note("type: kb-topic\nstatus: active\n", "RANK_HIGH gold"))
    vault.write("b.md", note("type: kb-topic\nstatus: complete\n", "RANK_HIGH gold"))
    memory_index.index_vault()

    hits = memory_search.hybrid_search(
        "gold", limit=10, where=["type=kb-topic", "status=active"]
    )
    assert sources(hits) == ["vault/a.md"]


def test_not_equals_excludes(vault):
    vault.write("a.md", note("status: active\n", "RANK_HIGH gold"))
    vault.write("b.md", note("status: processed\n", "RANK_HIGH gold"))
    memory_index.index_vault()

    hits = memory_search.hybrid_search("gold", limit=10, where=["status!=processed"])
    assert sources(hits) == ["vault/a.md"]


def test_a_note_missing_the_field_is_excluded_by_equals(vault):
    vault.write("has.md", note("status: active\n", "RANK_HIGH gold"))
    vault.write("none.md", "# Plain\n\nRANK_HIGH gold with no header\n")
    memory_index.index_vault()

    hits = memory_search.hybrid_search("gold", limit=10, where=["status=active"])
    assert sources(hits) == ["vault/has.md"]


def test_a_note_missing_the_field_survives_not_equals(vault):
    """"not processed" has to include notes that were never given a status."""
    vault.write("none.md", "# Plain\n\nRANK_HIGH gold with no header\n")
    vault.write("proc.md", note("status: processed\n", "RANK_HIGH gold"))
    memory_index.index_vault()

    hits = memory_search.hybrid_search("gold", limit=10, where=["status!=processed"])
    assert sources(hits) == ["vault/none.md"]


def test_matching_any_one_value_of_a_list_field_is_enough(vault):
    vault.write("a.md", note("tags: [gtm, kb-source]\n", "RANK_HIGH gold"))
    memory_index.index_vault()

    assert memory_search.hybrid_search("gold", limit=5, where=["tags=gtm"])
    assert memory_search.hybrid_search("gold", limit=5, where=["tags=kb-source"])
    assert not memory_search.hybrid_search("gold", limit=5, where=["tags=nope"])


def test_where_filters_before_the_limit_is_applied(vault):
    """Same defect the path filter had: cut first, filter second, get nothing."""
    for i in range(10):
        vault.write(f"noise{i}.md", note("type: email\n", f"RANK_HIGH gold {i}"))
    vault.write("wanted.md", note("type: kb-topic\n", "RANK_LOW gold thesis"))
    memory_index.index_vault()

    hits = memory_search.hybrid_search("gold", limit=5, where=["type=kb-topic"])
    assert sources(hits) == ["vault/wanted.md"]


def test_where_combines_with_a_path_prefix(vault):
    vault.write("investments/a.md", note("status: active\n", "RANK_HIGH gold"))
    vault.write("investments/b.md", note("status: done\n", "RANK_HIGH gold"))
    vault.write("other/c.md", note("status: active\n", "RANK_HIGH gold"))
    memory_index.index_vault()

    hits = memory_search.hybrid_search(
        "gold", limit=10, path_prefix="vault/investments", where=["status=active"]
    )
    assert sources(hits) == ["vault/investments/a.md"]


def test_a_malformed_condition_is_rejected_clearly(vault):
    vault.write("a.md", note("status: active\n"))
    memory_index.index_vault()

    with pytest.raises(ValueError, match="key=value"):
        memory_search.hybrid_search("gold", limit=5, where=["nonsense"])


def test_no_where_filter_changes_nothing(vault):
    vault.write("a.md", note("status: active\n", "RANK_HIGH gold"))
    memory_index.index_vault()

    assert sources(memory_search.hybrid_search("gold", limit=5)) == ["vault/a.md"]
    assert sources(memory_search.hybrid_search("gold", limit=5, where=[])) == ["vault/a.md"]


# --------------------------------------------------------------------------
# the header block is metadata, not prose
#
# It was being chunked and embedded like body text, so every note with
# frontmatter produced a junk search result whose excerpt was raw YAML —
# `--- type: "tweet" title: ... ---`. Seen in a live result on 2026-08-29.
# The fields are captured properly in file_meta now, so indexing them as prose
# is duplication that only costs relevance.
# --------------------------------------------------------------------------

def test_the_header_block_is_not_indexed_as_text():
    doc = note("type: tweet\nstatus: processed\n", "the actual body text")
    chunks = memory_index.chunk_markdown(doc, "vault/a.md")

    assert chunks, "the body must still be indexed"
    for c in chunks:
        assert "type: tweet" not in c["text"], "raw YAML leaked into a chunk"
        assert "status: processed" not in c["text"]
    assert any("the actual body text" in c["text"] for c in chunks)


def test_line_numbers_still_point_at_the_real_file(tmp_path):
    """Skipping the header must not shift what the line numbers mean."""
    doc = note("type: tweet\n", "the actual body text")
    path = tmp_path / "a.md"
    path.write_text(doc, encoding="utf-8")
    lines = doc.splitlines()

    for c in memory_index.chunk_markdown(doc, "vault/a.md"):
        span = "\n".join(lines[c["start_line"] - 1:c["end_line"]])
        assert c["text"] in span, "line range does not contain the chunk"


def test_a_note_that_is_only_a_header_block_yields_nothing():
    doc = "---\ntype: email\nstatus: active\n---\n"
    assert memory_index.chunk_markdown(doc, "vault/a.md") == []


def test_a_note_with_no_header_block_is_untouched():
    doc = "# Title\n\nsome body\n"
    chunks = memory_index.chunk_markdown(doc, "vault/a.md")
    assert len(chunks) == 1
    assert chunks[0]["start_line"] == 1


def test_a_dashed_rule_in_the_body_is_still_indexed():
    """Only a block at the very top is frontmatter. A rule mid-note is content."""
    doc = "# Title\n\nfirst part\n\n---\n\nsecond part\n"
    text = " ".join(c["text"] for c in memory_index.chunk_markdown(doc, "vault/a.md"))
    assert "first part" in text and "second part" in text


def test_a_malformed_header_block_is_still_skipped():
    """It is delimiters that make it a header, not whether the YAML parses."""
    doc = "---\ntype: email\n  bad: [unclosed\n---\n\n# Body\n\nreal text\n"
    chunks = memory_index.chunk_markdown(doc, "vault/a.md")
    for c in chunks:
        assert "unclosed" not in c["text"]
    assert any("real text" in c["text"] for c in chunks)


def test_searching_no_longer_surfaces_a_yaml_only_result(vault):
    vault.write("a.md", note("type: tweet\nurl: https://x.com/i/status/123\n",
                             "RANK_HIGH gold commentary"))
    memory_index.index_vault()

    hits = memory_search.hybrid_search("gold", limit=10)
    assert hits
    for h in hits:
        assert not h["excerpt"].startswith("---"), f"YAML result: {h['excerpt'][:60]}"
