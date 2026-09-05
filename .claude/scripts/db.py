#!/usr/bin/env python3
"""Database abstraction for Cortex OS memory search."""

import sqlite3
from pathlib import Path

import sqlite_vec

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = REPO_ROOT / ".claude" / "data" / "memory.db"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Get a SQLite connection with sqlite-vec loaded and tables initialized."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    _init_tables(db)
    return db


def _init_tables(db: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    db.execute("""CREATE TABLE IF NOT EXISTS chunks(
        id INTEGER PRIMARY KEY,
        text TEXT,
        source TEXT,
        heading TEXT,
        updated_at TEXT,
        hash TEXT UNIQUE,
        start_line INTEGER,
        end_line INTEGER
    )""")
    # An index built before line numbers existed keeps its rows; the columns are
    # added empty and fill in as those chunks are re-indexed. Nothing reads a
    # NULL range as a real location.
    columns = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}
    for column in ("start_line", "end_line"):
        if column not in columns:
            db.execute(f"ALTER TABLE chunks ADD COLUMN {column} INTEGER")
    # A note's header block and its [[links]] describe the FILE, not a chunk, so
    # they live in their own tables keyed by source path. That is what makes them
    # cheap: rebuilding either means re-reading the vault, seconds of work, with
    # no embedding. A chunker change by contrast forces the full 12-minute
    # reindex, so the two must not be entangled.
    #
    # (source, key, value) rather than (source, key): `tags: [gtm, kb-source]`
    # becomes two rows, so `--where tags=gtm` matches on either.
    db.execute("""CREATE TABLE IF NOT EXISTS file_meta(
        source TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (source, key, value)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_file_meta_kv ON file_meta(key, value)")

    # `target` is the link as written; `dst` is what it resolved to, or NULL when
    # nothing in the vault answers to it. Both are kept: the written form is what
    # a reader has to go and fix.
    db.execute("""CREATE TABLE IF NOT EXISTS links(
        src TEXT NOT NULL,
        target TEXT NOT NULL,
        dst TEXT,
        PRIMARY KEY (src, target)
    )""")
    # `store` says which root `dst` resolved against: "vault" (the default —
    # backfilled rows and same-run vault resolutions), "memory" (Claude Code's
    # auto-memory folder, a second markdown store outside the vault), or NULL
    # when `dst` itself is NULL (unresolved). Added the same way `chunks`
    # grew start_line/end_line: existing rows keep their value (NULL) and fill
    # in as they're re-indexed.
    columns = {row[1] for row in db.execute("PRAGMA table_info(links)")}
    if "store" not in columns:
        db.execute("ALTER TABLE links ADD COLUMN store TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst)")

    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[384])"
    )
    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content=chunks, content_rowid=id)"
    )
    db.commit()
