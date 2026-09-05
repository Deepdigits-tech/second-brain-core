"""Cross-store resolution and dead-link categorisation for the link graph.

The vault's `[[wikilink]]` checker used to report links into Claude Code's
auto-memory folder (a second markdown store outside the vault) as dead, and
its headline dead-link count was dominated by vendored/template junk. This
adds a second resolution root (the auto-memory dir) and categorises whatever
is left genuinely unresolved.
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


@pytest.fixture
def automem(tmp_path, monkeypatch):
    automem_dir = tmp_path / "automem"
    automem_dir.mkdir()
    monkeypatch.setattr(memory_index, "auto_memory_dir", lambda: automem_dir)
    return automem_dir


def _index():
    memory_index.index_vault()


# --------------------------------------------------------------------------
# cross-store resolution
# --------------------------------------------------------------------------

def test_cross_store_link_resolves_and_is_not_dead(vault, automem):
    (automem / "feedback_writing_style.md").write_text("# style\n")
    vault.write("kb/gtm/a.md", "see [[feedback_writing_style]]\n")
    _index()
    assert memory_links.dead_links(path_prefix="vault/kb") == []
    xs = memory_links.cross_store_links(path_prefix="vault/kb")
    assert len(xs) == 1 and xs[0]["target"] == "feedback_writing_style" and xs[0]["store"] == "memory"


def test_memory_path_never_becomes_a_hub(vault, automem):
    (automem / "m.md").write_text("# m\n")
    vault.write("a.md", "[[m]]\n")
    vault.write("b.md", "[[m]]\n")
    _index()
    assert memory_links.hubs() == []


def test_auto_memory_dir_is_derived_from_repo_root(monkeypatch, tmp_path):
    monkeypatch.delenv("CORTEX_AUTO_MEMORY_DIR", raising=False)
    monkeypatch.setattr(memory_index, "REPO_ROOT", Path("/srv/x/repo"))
    assert memory_index.auto_memory_dir() == Path.home() / ".claude" / "projects" / "-srv-x-repo" / "memory"
    monkeypatch.setenv("CORTEX_AUTO_MEMORY_DIR", str(tmp_path))
    assert memory_index.auto_memory_dir() == tmp_path


# --------------------------------------------------------------------------
# dead-link categorisation
# --------------------------------------------------------------------------

def test_dead_links_are_categorised(vault, automem):
    vault.write("kb/aios/sources/raw/vendor.md", "[[Target]] and [[missing-thing]]\n")
    vault.write("investments/playbook/x.md", "[[../concepts/gold-and-real-yields]] [[Note Name]]\n")
    _index()
    by = memory_links.dead_links_by_category()
    assert {d["target"] for d in by["vendored"]} == {"Target", "missing-thing"}
    assert {d["target"] for d in by["template"]} == {"Note Name"}
    assert {d["target"] for d in by["real"]} == {"../concepts/gold-and-real-yields"}
    flat = memory_links.dead_links()
    assert all("category" in d for d in flat) and len(flat) == 4


def test_cli_dead_groups_real_first(vault, automem, capsys):
    vault.write("own.md", "[[nowhere]]\n")
    vault.write("kb/aios/sources/raw/v.md", "[[Target]]\n")
    _index()
    import sys as _sys
    _sys.argv = ["memory_links.py", "dead"]
    memory_links.main()
    out = capsys.readouterr().out
    assert out.index("real") < out.index("vendored")
    assert "nowhere" in out and "Target" in out
