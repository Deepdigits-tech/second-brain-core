"""Tests for vault indexing and hybrid search.

Three defects this file pins down, all found 2026-08-25:

1. The 15-minute incremental run only ever ADDED. A note deleted from the vault,
   or a paragraph rewritten in place, left its old chunks searchable forever.
   The live index carried 220 such chunks (40 dead files) before the fix.
2. `--path-prefix` was applied AFTER the result list had been cut to `limit`, so
   a scoped search whose top hits lived elsewhere returned nothing at all rather
   than the best in-scope rows.
3. The FTS5 query was wrapped in double quotes, turning every search into an
   exact-phrase match. `revenue OR earnings` hunted for that literal string, so
   the keyword half of the hybrid contributed nothing on multi-word queries.
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
    """Deterministic stand-in for the real model, for note text.

    Dimension 0 is the only one that carries signal, and every query sits at
    1.0 (see `fake_embed_query`). So a note marked RANK_HIGH lands exactly on
    the query and RANK_LOW lands a full unit away, which makes semantic rank
    controllable straight from the fixture text, with no model download.
    """
    vec = [0.0] * EMBEDDING_DIM
    vec[0] = 1.0 if "RANK_HIGH" in text else 0.0
    return vec


def fake_embed_query(_text: str) -> list[float]:
    """Every query sits at the RANK_HIGH end of dimension 0."""
    vec = [0.0] * EMBEDDING_DIM
    vec[0] = 1.0
    return vec


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """An isolated vault + index, with the embedding model stubbed out."""
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

    def sources() -> set[str]:
        conn = connect()
        try:
            return {r[0] for r in conn.execute("SELECT DISTINCT source FROM chunks")}
        finally:
            conn.close()

    def counts() -> dict[str, int]:
        conn = connect()
        try:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("chunks", "vec_chunks", "chunks_fts")
            }
        finally:
            conn.close()

    def fts_hits(term: str) -> int:
        conn = connect()
        try:
            return len(conn.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (term,)
            ).fetchall())
        finally:
            conn.close()

    return type("Vault", (), {
        "dir": vault_dir, "db": db_path, "write": staticmethod(write),
        "sources": staticmethod(sources), "counts": staticmethod(counts),
        "fts_hits": staticmethod(fts_hits),
    })


# --------------------------------------------------------------------------
# 0. chunks must be locatable: a verbatim slice of a file, at known lines
# --------------------------------------------------------------------------

def read_lines(path: Path, start: int, end: int) -> str:
    """What a reader gets when it opens the file at the range we handed it."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[start - 1:end])


def test_every_chunk_is_a_verbatim_slice_of_its_file():
    """The old chunker rebuilt overlap text by slicing and rejoining with a
    literal blank line, so a chunk could contain a break the file never had.
    Nothing could then map a result back to a position on disk."""
    doc = "# Title\n\n" + "\n\n".join(f"Paragraph {i} " + "filler " * 60 for i in range(12))

    for chunk in memory_index.chunk_markdown(doc, "vault/big.md"):
        assert chunk["text"] in doc


def test_chunk_line_numbers_bracket_the_chunk_text(tmp_path):
    doc = "# Title\n\nFirst para.\n\nSecond para.\n\n## Later\n\nThird para.\n"
    path = tmp_path / "note.md"
    path.write_text(doc, encoding="utf-8")

    for chunk in memory_index.chunk_markdown(doc, "vault/note.md"):
        assert chunk["start_line"] >= 1
        assert chunk["end_line"] >= chunk["start_line"]
        assert chunk["text"] in read_lines(path, chunk["start_line"], chunk["end_line"])


def test_line_numbers_are_exact_for_a_late_section(tmp_path):
    doc = "# One\n\nalpha\n\n# Two\n\nbravo\n\n# Three\n\ncharlie\n"
    path = tmp_path / "note.md"
    path.write_text(doc, encoding="utf-8")

    chunks = memory_index.chunk_markdown(doc, "vault/note.md")
    charlie = next(c for c in chunks if "charlie" in c["text"])
    assert charlie["start_line"] == 9   # the '# Three' line
    assert charlie["end_line"] == 11    # the 'charlie' line
    assert "charlie" in read_lines(path, charlie["start_line"], charlie["end_line"])


def test_oversized_section_splits_with_overlapping_but_real_slices():
    doc = "# Title\n\n" + "\n\n".join(f"Paragraph {i} " + "filler " * 60 for i in range(12))
    chunks = memory_index.chunk_markdown(doc, "vault/big.md")

    assert len(chunks) > 1, "a section this size must split"
    for chunk in chunks:
        assert chunk["text"] in doc
    # Consecutive chunks overlap, so nothing falls between them.
    for earlier, later in zip(chunks, chunks[1:]):
        assert later["start_line"] <= earlier["end_line"] + 1


# --------------------------------------------------------------------------
# 1. the index must forget
# --------------------------------------------------------------------------

def test_incremental_run_drops_chunks_for_deleted_files(vault):
    vault.write("keep.md", "# Keep\n\nThe kept note about salmon.\n")
    vault.write("drop.md", "# Drop\n\nThe doomed note about halibut.\n")
    memory_index.index_vault()
    assert vault.sources() == {"vault/keep.md", "vault/drop.md"}

    (vault.dir / "drop.md").unlink()
    memory_index.index_vault(incremental=True)

    assert vault.sources() == {"vault/keep.md"}
    assert vault.fts_hits("halibut") == 0
    assert vault.fts_hits("salmon") == 1


def test_incremental_run_drops_superseded_chunks_when_a_file_is_edited(vault):
    vault.write("note.md", "# Note\n\nThe original claim about halibut.\n")
    memory_index.index_vault()

    vault.write("note.md", "# Note\n\nThe revised claim about salmon.\n")
    memory_index.index_vault(incremental=True)

    assert vault.fts_hits("halibut") == 0
    assert vault.fts_hits("salmon") == 1
    assert vault.counts()["chunks"] == 1


def test_prune_keeps_all_three_tables_in_step(vault):
    vault.write("a.md", "# A\n\nAlpha note RANK_HIGH.\n")
    vault.write("b.md", "# B\n\nBravo note RANK_HIGH.\n")
    memory_index.index_vault()
    assert vault.counts() == {"chunks": 2, "vec_chunks": 2, "chunks_fts": 2}

    (vault.dir / "b.md").unlink()
    memory_index.index_vault(incremental=True)
    assert vault.counts() == {"chunks": 1, "vec_chunks": 1, "chunks_fts": 1}


def test_moved_file_stops_citing_its_old_path(vault):
    """The draft lifecycle moves files with their text untouched.

    Hashing text alone made the moved copy collide with the original, so the
    index kept citing `active/` for drafts that had moved to `expired/`.
    """
    body = "# Draft\n\nThe body of a draft email to the contractor.\n"
    vault.write("drafts/active/note.md", body)
    memory_index.index_vault()
    assert vault.sources() == {"vault/drafts/active/note.md"}

    (vault.dir / "drafts" / "active" / "note.md").unlink()
    vault.write("drafts/expired/note.md", body)
    memory_index.index_vault(incremental=True)

    assert vault.sources() == {"vault/drafts/expired/note.md"}


def test_identical_text_in_two_files_is_indexed_once_per_file(vault):
    body = "# Shared\n\nA paragraph that appears in two places.\n"
    vault.write("one.md", body)
    vault.write("two.md", body)
    memory_index.index_vault()

    assert vault.sources() == {"vault/one.md", "vault/two.md"}
    assert vault.counts()["chunks"] == 2


def test_empty_crawl_never_wipes_a_good_index(vault):
    """A vault that reads as empty is far more likely a bug than a real deletion."""
    vault.write("a.md", "# A\n\nAlpha note.\n")
    memory_index.index_vault()

    for md in vault.dir.rglob("*.md"):
        md.unlink()
    memory_index.index_vault(incremental=True)

    assert vault.counts()["chunks"] == 1


# --------------------------------------------------------------------------
# 2. scoped search must filter before it truncates
# --------------------------------------------------------------------------

def test_path_prefix_filters_before_the_limit_is_applied(vault):
    for i in range(10):
        vault.write(f"other/n{i}.md", f"# Other {i}\n\nRANK_HIGH gold note {i}.\n")
    vault.write("investments/gold.md", "# Gold\n\nRANK_LOW gold thesis.\n")
    vault.write("investments/silver.md", "# Silver\n\nRANK_LOW gold hedge.\n")
    memory_index.index_vault()

    results = memory_search.hybrid_search(
        "gold", limit=10, path_prefix="vault/investments"
    )

    assert len(results) == 2
    assert all(r["source"].startswith("vault/investments") for r in results)


def test_scoped_search_stays_under_the_knn_ceiling(vault, monkeypatch):
    """sqlite-vec refuses a knn query wider than MAX_KNN.

    Widening the candidate pool to the whole index is right in principle and
    an outright error past that ceiling — the live index passed it at 7,363
    chunks, so a scoped search raised instead of returning rows.
    """
    monkeypatch.setattr(memory_search, "MAX_KNN", 4)
    for i in range(8):
        vault.write(f"investments/n{i}.md", f"# N{i}\n\nRANK_HIGH gold note {i}.\n")
    memory_index.index_vault()

    results = memory_search.hybrid_search("gold", limit=3, path_prefix="vault/investments")
    assert len(results) == 3


def test_path_prefix_still_respects_the_limit(vault):
    for i in range(6):
        vault.write(f"investments/n{i}.md", f"# N{i}\n\nRANK_HIGH gold note {i}.\n")
    memory_index.index_vault()

    results = memory_search.hybrid_search("gold", limit=3, path_prefix="vault/investments")
    assert len(results) == 3


# --------------------------------------------------------------------------
# 5. the two halves must be on one scale
# --------------------------------------------------------------------------

def test_normalize_puts_best_at_one_and_worst_at_zero():
    scores = memory_search.normalize([(1, 0.1), (2, 0.5), (3, 0.9)], lower_is_better=True)
    assert scores[1] == pytest.approx(1.0)
    assert scores[3] == pytest.approx(0.0)
    assert 0 < scores[2] < 1


def test_normalize_handles_a_dead_heat_without_dividing_by_zero():
    scores = memory_search.normalize([(1, 0.4), (2, 0.4)], lower_is_better=True)
    assert scores == {1: 1.0, 2: 1.0}


def test_normalize_of_nothing_is_nothing():
    assert memory_search.normalize([], lower_is_better=True) == {}


def test_keyword_half_cannot_outrank_the_vector_half_at_default_weights(vault):
    """A 0.3-weighted keyword win must not beat a 0.7-weighted vector win.

    It did: the halves were scaled differently, so the best keyword row scored
    a flat 1.0 while vector rows were squashed into the hundredths. Chatty
    queries AND-join their stopwords, so a note matching "what did we about"
    and nothing else took the top slot from the note that was actually on topic.
    """
    vault.write("onpoint.md", "# On point\n\nRANK_HIGH the gold thesis lives here.\n")
    vault.write("noise.md", "# Noise\n\nRANK_LOW what did we decide about gold plating.\n")
    memory_index.index_vault()

    results = memory_search.hybrid_search("what did we decide about gold", limit=2)
    assert results[0]["source"] == "vault/onpoint.md"


# --------------------------------------------------------------------------
# 4. results are pointers, not payloads
# --------------------------------------------------------------------------

def test_results_carry_a_pointer_and_an_excerpt_not_the_whole_chunk(vault):
    body = "# Gold\n\n" + "The gold thesis. " * 200
    vault.write("investments/gold.md", body)
    memory_index.index_vault()

    (hit,) = memory_search.hybrid_search("gold", limit=1)
    assert hit["source"] == "vault/investments/gold.md"
    assert hit["start_line"] >= 1
    assert hit["end_line"] >= hit["start_line"]
    assert hit["lines"] == f"{hit['start_line']}-{hit['end_line']}"
    assert "text" not in hit, "full chunk text must be opt-in"
    assert len(hit["excerpt"]) <= memory_search.EXCERPT_CHARS


def test_via_names_the_evidence_for_each_row(vault):
    vault.write("a.md", "# A\n\nA note about halibut.\n")
    memory_index.index_vault()

    (hit,) = memory_search.hybrid_search("halibut", limit=1)
    assert "match" in hit["via"], "the word is right there in the note"
    assert "vector" in hit["via"], "and it is the nearest chunk too"

    (miss,) = memory_search.hybrid_search("entirely unrelated wording", limit=1)
    assert miss["via"] == "vector", "no shared words — meaning alone put it here"


def test_full_text_is_available_on_request(vault):
    vault.write("investments/gold.md", "# Gold\n\nThe gold thesis in brief.\n")
    memory_index.index_vault()

    (hit,) = memory_search.hybrid_search("gold", limit=1, include_text=True)
    assert "The gold thesis in brief." in hit["text"]


def test_the_pointer_actually_locates_the_text_on_disk(vault):
    """The whole point: a reader opening those lines must find the excerpt."""
    vault.write("notes/a.md", "# One\n\nalpha filler\n\n# Two\n\nthe distinctive marker\n")
    memory_index.index_vault()

    hits = memory_search.hybrid_search("distinctive marker", limit=5, include_text=True)
    hit = next(h for h in hits if "distinctive marker" in h["text"])
    on_disk = read_lines(vault.dir.parent / hit["source"], hit["start_line"], hit["end_line"])
    assert "the distinctive marker" in on_disk


def test_excerpt_collapses_whitespace_so_a_pointer_stays_one_line(vault):
    vault.write("a.md", "# A\n\nfirst line\n\nsecond line\n\nthird line\n")
    memory_index.index_vault()

    (hit,) = memory_search.hybrid_search("first line", limit=1)
    assert "\n" not in hit["excerpt"]


# --------------------------------------------------------------------------
# 3. keyword search must behave like keyword search
#
# These run keyword-only (vector_weight=0). The semantic half would otherwise
# return every note in a tiny fixture vault and mask what FTS5 actually did.
# --------------------------------------------------------------------------

def keyword_only(query: str, **kw):
    """Rows the FTS5 half actually matched.

    The semantic half always contributes its nearest rows, which in a fixture
    this small is every note. `via` names each row's evidence, so filtering on
    it isolates the keyword engine exactly — where filtering on score cannot,
    since the worst row of a normalised set legitimately scores 0.
    """
    results = memory_search.hybrid_search(
        query, vector_weight=0.0, keyword_weight=1.0, **kw
    )
    return [r for r in results if "match" in r["via"]]


def test_or_operator_matches_either_term(vault):
    vault.write("a.md", "# A\n\nQuarterly revenue held flat.\n")
    vault.write("b.md", "# B\n\nAnnual earnings rose.\n")
    vault.write("c.md", "# C\n\nUnrelated note about gardening.\n")
    memory_index.index_vault()

    sources = {r["source"] for r in keyword_only("revenue OR earnings", limit=10)}
    assert sources == {"vault/a.md", "vault/b.md"}


def test_multi_word_query_matches_notes_lacking_the_exact_phrase(vault):
    vault.write("a.md", "# A\n\nRevenue was discussed, and separately earnings.\n")
    vault.write("b.md", "# B\n\nA note about gardening.\n")
    memory_index.index_vault()

    results = keyword_only("revenue earnings", limit=10)
    assert [r["source"] for r in results] == ["vault/a.md"]


def test_bare_words_still_join_with_and(vault):
    """`revenue earnings` means both words, not either — an AND join, not OR."""
    vault.write("both.md", "# Both\n\nRevenue and earnings together.\n")
    vault.write("one.md", "# One\n\nRevenue alone in this note.\n")
    memory_index.index_vault()

    results = keyword_only("revenue earnings", limit=10)
    assert [r["source"] for r in results] == ["vault/both.md"]


@pytest.mark.parametrize("query", ["acme-corp", 'un"balanced quote', "AND", "NOT", "*"])
def test_malformed_fts_queries_fall_back_instead_of_crashing(vault, query):
    """Hyphens and bare operators are FTS5 syntax errors, not user mistakes."""
    vault.write("a.md", "# A\n\nA note mentioning acme-corp and other things.\n")
    memory_index.index_vault()

    assert isinstance(keyword_only(query, limit=5), list)


def test_hyphenated_term_still_finds_its_note(vault):
    vault.write("a.md", "# A\n\nThe acme-corp campaign brief.\n")
    vault.write("b.md", "# B\n\nSomething else entirely.\n")
    memory_index.index_vault()

    results = keyword_only("acme-corp", limit=5)
    assert [r["source"] for r in results] == ["vault/a.md"]
