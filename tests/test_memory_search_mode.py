"""Tests for search mode parameter and vector_pool_truncated flag."""

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


def _index():
    """Build the index using the same call as existing tests."""
    memory_index.index_vault()


def test_keyword_mode_only_returns_word_matches(vault):
    vault.write("a.md", "# A\nzebra crossing\n")
    vault.write("b.md", "# B\nnothing relevant\n")
    _index()
    hits = memory_search.hybrid_search("zebra", mode="keyword")
    assert [h["source"] for h in hits] == ["vault/a.md"]
    assert all(h["via"] == "match" for h in hits)


def test_semantic_mode_never_reports_a_keyword_match(vault):
    vault.write("a.md", "# A\nzebra crossing\n")
    _index()
    hits = memory_search.hybrid_search("zebra", mode="semantic")
    assert hits and all(h["via"] == "vector" for h in hits)


def test_unknown_mode_is_an_error(vault):
    with pytest.raises(ValueError):
        memory_search.hybrid_search("x", mode="fuzzy")


def test_truncation_flag_is_false_on_a_small_index(vault):
    vault.write("a.md", "# A\nzebra\n")
    _index()
    hits = memory_search.hybrid_search("zebra", path_prefix="vault/")
    assert hits and hits[0]["vector_pool_truncated"] is False


def test_truncation_flag_is_true_when_the_scope_hits_the_ceiling(vault, monkeypatch):
    vault.write("a.md", "# A\nzebra\n")
    vault.write("b.md", "# B\nzebra again\n")
    _index()
    monkeypatch.setattr(memory_search, "MAX_KNN", 1)
    hits = memory_search.hybrid_search("zebra", path_prefix="vault/")
    assert hits and hits[0]["vector_pool_truncated"] is True


def test_keyword_mode_is_never_truncated_even_at_the_ceiling(vault, monkeypatch):
    vault.write("a.md", "# A\nzebra\n")
    vault.write("b.md", "# B\nzebra again\n")
    _index()
    monkeypatch.setattr(memory_search, "MAX_KNN", 1)
    hits = memory_search.hybrid_search("zebra", path_prefix="vault/", mode="keyword")
    assert hits and all(h["vector_pool_truncated"] is False for h in hits)


def test_cli_accepts_mode(vault, capsys, monkeypatch):
    vault.write("a.md", "# A\nzebra\n")
    _index()
    monkeypatch_argv = ["memory_search.py", "zebra", "--mode", "keyword", "--json"]
    monkeypatch.setattr(sys, "argv", monkeypatch_argv)
    memory_search.main()
    out = capsys.readouterr().out
    assert '"via": "match"' in out


def test_keyword_mode_does_not_embed(vault, monkeypatch):
    vault.write("a.md", "# A\nzebra crossing\n")
    _index()

    # Monkeypatch embed_query to raise if called
    def fail_if_called(_text: str) -> list[float]:
        raise AssertionError("embed_query should not be called in keyword mode")

    monkeypatch.setattr(memory_search, "embed_query", fail_if_called)

    # This should succeed without calling embed_query
    hits = memory_search.hybrid_search("zebra", mode="keyword")
    assert hits and all(h["via"] == "match" for h in hits)
