#!/usr/bin/env python3
"""Index vault markdown files for Cortex OS memory search."""

import bisect
import hashlib
import os
import re
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import get_connection
from embeddings import embed_texts

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VAULT_DIR = REPO_ROOT / "vault"

# Files inside vault/ that are auto-rendered from other state (operational, not knowledge).
# Indexing them dilutes search relevance — see vault/research/2026-05-01-memory-system-audit.md
# Findings #1 and #6. Paths are relative to REPO_ROOT.
EXCLUDED_PATHS = {
    "vault/inbox-review.md",   # auto-rendered from inbox classifier (~64KB, 1483 chunks → 57% of index)
    "vault/tasks/queue.md",    # auto-rendered from tasks.json
    "vault/tasks/archive.md",  # same — archived task view
}

# Directory segments that are vendored/generated artifacts, kept as a safety net for
# the walk fallback (no git repo available). The primary crawl (vault_markdown_files)
# asks git instead: `vault/projects/AI Native OS/build/spikes/s1b-hermes/hermes-src`
# (2026-08-14, 1,427 files / 37,013 chunks, 82% of a 44,971-chunk index) is excluded
# because it is gitignored, not because it sits under a folder named `build` — a real
# note the owner wrote in a `build` folder is still knowledge. Gitignore is the rule now.
EXCLUDED_DIR_SEGMENTS = {
    "node_modules",
    "__pycache__",
    ".testrun",
}


_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
_WIKILINK = re.compile(r"(?<!\[)\[\[([^\[\]]+?)\]\]")


def parse_frontmatter(text: str) -> dict[str, list[str]]:
    """Read a note's YAML header block into {key: [values]}.

    Values are always a list, because `tags: [gtm, kb-source]` has to make both
    `tags=gtm` and `tags=kb-source` match; a scalar is simply a list of one.
    Everything is stringified — filters are compared as text, and a date that
    parses to a date object would stop matching the string the user typed.

    A malformed block yields {} rather than raising. One bad note must never
    cost the whole crawl, and the note's *text* is still indexed either way.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}

    try:
        import yaml
        loaded = yaml.safe_load(match.group(1))
    except Exception:
        return {}

    if not isinstance(loaded, dict):
        return {}

    out: dict[str, list[str]] = {}
    for key, value in loaded.items():
        if value is None or value == "":
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        values = [str(v).strip() for v in items if v is not None and str(v).strip()]
        if values:
            out[str(key)] = values
    return out


def parse_links(text: str) -> list[str]:
    """Return the `[[wikilink]]` targets in a note, as written.

    `[[note|shown]]` and `[[note#heading]]` both point at `note` — the display
    text and the anchor are for the reader, not the graph.
    """
    targets = []
    for raw in _WIKILINK.findall(text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            targets.append(target)
    return targets


def auto_memory_dir() -> Path:
    """Claude Code's per-project auto-memory folder — a second markdown store
    outside the vault, derived at call time (never hardcoded) so it tracks
    whatever REPO_ROOT is set to at the moment of the call, including in
    tests that monkeypatch REPO_ROOT.

    Overridable with CORTEX_AUTO_MEMORY_DIR for testing or a nonstandard
    checkout location.
    """
    override = os.environ.get("CORTEX_AUTO_MEMORY_DIR")
    if override:
        return Path(override)
    slug = "-" + str(REPO_ROOT.resolve()).strip("/").replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / "memory"


def resolve_link(
    target: str, by_path: dict, by_stem: dict, memory_by_stem: dict | None = None
) -> tuple[str | None, str | None]:
    """Map a link target onto a real vault path or an auto-memory file.

    Obsidian links are written both ways — a bare note name and a relative
    path — and with or without the `.md`. All four have to land on the file.

    Returns (dst, store): store is "vault" when it resolves against the
    vault crawl, "memory" when it only resolves against the auto-memory
    folder (a wikilink to `[[feedback_writing_style]]` naming a real
    Claude Code memory note, not a broken link), or (None, None) when
    nothing answers to it.
    """
    cleaned = target.strip().lstrip("./")
    for candidate in (cleaned, f"{cleaned}.md", cleaned.removesuffix(".md")):
        if candidate in by_path:
            return by_path[candidate], "vault"
    stem = Path(cleaned).name.removesuffix(".md").lower()
    if stem in by_stem:
        return by_stem[stem], "vault"
    if memory_by_stem and stem in memory_by_stem:
        return memory_by_stem[stem], "memory"
    return None, None


def file_metadata(source: str, db) -> dict[str, list[str]]:
    """Read back one file's stored header fields."""
    out: dict[str, list[str]] = {}
    for key, value in db.execute(
        "SELECT key, value FROM file_meta WHERE source = ? ORDER BY key, value",
        (source,),
    ):
        out.setdefault(key, []).append(value)
    return out


def index_file_facts(db, files: list[tuple[str, str]]) -> None:
    """Rebuild the header-field and link tables from a fresh crawl.

    Whole-table replacement rather than a diff. These are per-file facts for a
    few hundred notes — the rebuild is milliseconds, and a diff would be a
    second place for the index to drift out of step with the vault, which is
    the bug that put 220 dead chunks in the search index.
    """
    known = {rel: rel for rel, _ in files}
    # Also index by the path relative to vault/ and by bare filename, since
    # links are written both ways.
    by_path = dict(known)
    for rel in known:
        without_vault = rel.split("vault/", 1)[-1]
        by_path.setdefault(without_vault, rel)
        by_path.setdefault(without_vault.removesuffix(".md"), rel)
    by_stem = {}
    for rel in known:
        by_stem.setdefault(Path(rel).stem.lower(), rel)

    memory_dir = auto_memory_dir()
    memory_by_stem = (
        {p.stem.lower(): str(p) for p in memory_dir.glob("*.md")}
        if memory_dir.is_dir()
        else {}
    )

    db.execute("DELETE FROM file_meta")
    db.execute("DELETE FROM links")

    for rel_path, content in files:
        for key, values in parse_frontmatter(content).items():
            for value in values:
                db.execute(
                    "INSERT OR IGNORE INTO file_meta (source, key, value) VALUES (?, ?, ?)",
                    (rel_path, key, value),
                )
        for target in parse_links(content):
            dst, store = resolve_link(target, by_path, by_stem, memory_by_stem)
            db.execute(
                "INSERT OR IGNORE INTO links (src, target, dst, store) VALUES (?, ?, ?, ?)",
                (rel_path, target, dst, store),
            )
    db.commit()


def chunk_fingerprint(source: str, text: str) -> str:
    """Identity of a chunk: its text AND the file it came from.

    Hashing the text alone made two identical chunks in different files collide,
    and `hash` is UNIQUE, so only the first file's row survived — carrying that
    first file's path. Move a file and the text is unchanged, so the row is
    still 'current' and prune correctly keeps it, but `source` now points at a
    path that no longer exists. That is exactly what the draft lifecycle does:
    on 2026-08-25, 80 chunks still cited `vault/drafts/active/...` for drafts
    that had long since moved to `vault/drafts/expired/`.

    Including the path also means genuinely duplicated text is indexed once per
    file, so each copy is findable and each cites where it actually lives.
    """
    return hashlib.sha256(f"{source}\0{text}".encode()).hexdigest()[:16]


def chunk_markdown(text: str, source: str, max_tokens: int = 400, overlap: int = 50) -> list[dict]:
    """Split markdown into chunks by headings, then paragraphs.

    Returns list of dicts: {text, source, heading, hash, start_line, end_line}

    Chunks are addressed by offset into the document rather than assembled from
    split pieces, which buys two things. Every chunk's text is a verbatim slice
    of the file, so it can be found again on disk — the old chunker rebuilt
    overlap text as `previous[-N:] + "\\n\\n" + para`, inventing a blank line
    the file never had. And each chunk knows the line range it came from, so a
    search result can be a pointer the reader resolves itself instead of a
    payload carried through the whole conversation.
    """
    # 1-indexed line number for any offset, via the offset each line starts at.
    line_starts = [0] + [m.end() for m in re.finditer(r"\n", text)]

    def line_at(pos: int) -> int:
        return bisect.bisect_right(line_starts, pos)

    chunks: list[dict] = []

    def emit(start: int, end: int, heading: str) -> None:
        """Record text[start:end], trimmed to its non-blank extent."""
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start >= end:
            return
        body = text[start:end]
        chunks.append({
            "text": body,
            "source": source,
            "heading": heading,
            "hash": chunk_fingerprint(source, body),
            "start_line": line_at(start),
            "end_line": line_at(end - 1),
        })

    # Start after the YAML header block. It is metadata, not prose: it was being
    # chunked and embedded like body text, so every note with frontmatter
    # produced a junk result whose excerpt was raw YAML. Its fields are captured
    # properly in `file_meta`, which is also what makes them filterable.
    #
    # Keyed off the delimiters, not off whether the YAML parses — a malformed
    # header is still a header, and still not prose. Offsets are into the full
    # document throughout, so line numbers keep pointing at the real file.
    header = _FRONTMATTER.match(text)
    body_start = header.end() if header else 0

    # Section boundaries are the h1-h3 headings; anything before the first one
    # is its own leading section.
    heading_starts = [m.start() for m in re.finditer(r"^#{1,3}\s", text, flags=re.MULTILINE)
                      if m.start() >= body_start]
    bounds = sorted({body_start, *heading_starts, len(text)})

    overlap_chars = overlap * 4
    max_chars = max_tokens * 4

    for sec_start, sec_end in zip(bounds, bounds[1:]):
        if not text[sec_start:sec_end].strip():
            continue

        first_line = text[sec_start:sec_end].split("\n", 1)[0]
        heading = first_line.strip("# ").strip() if first_line.startswith("#") else ""

        if sec_end - sec_start <= max_chars:
            emit(sec_start, sec_end, heading)
            continue

        # Paragraph spans within the section, as absolute offsets.
        paragraphs: list[tuple[int, int]] = []
        cursor = sec_start
        for gap in re.finditer(r"\n\n+", text[sec_start:sec_end]):
            paragraphs.append((cursor, sec_start + gap.start()))
            cursor = sec_start + gap.end()
        paragraphs.append((cursor, sec_end))

        run_start = run_end = None
        for para_start, para_end in paragraphs:
            if run_start is None:
                run_start, run_end = para_start, para_end
            elif para_end - run_start > max_chars:
                emit(run_start, run_end, heading)
                # Start the next run inside the one just emitted, so a sentence
                # straddling the boundary is searchable from both sides.
                run_start = max(sec_start, run_end - overlap_chars)
                run_end = para_end
            else:
                run_end = para_end
        if run_start is not None:
            emit(run_start, run_end, heading)

    return chunks


LAST_CRAWL_STATS: dict = {}


def vault_markdown_files() -> tuple[list[Path], str]:
    """Candidate .md files under vault/, and how they were found.

    Ask git first: tracked files plus untracked files that are not ignored.
    That is the owner's definition of "part of the vault" — a vendored clone
    that .gitignore already excludes stays out, and a note that merely sits
    in a folder called `build` stays in. Fall back to a directory walk when
    git is unavailable (tests, a bare export).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z", "--", str(VAULT_DIR.relative_to(REPO_ROOT))],
            capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError, ValueError):
        return sorted(VAULT_DIR.rglob("*.md")), "walk"
    rels = [r for r in proc.stdout.decode("utf-8", "replace").split("\0") if r.endswith(".md")]
    return sorted((REPO_ROOT / r) for r in rels if (REPO_ROOT / r).is_file()), "git"


_CORE_VAULT_FILES = {"SOUL.md", "MEMORY.md", "USER.md", "HABITS.md", "HEARTBEAT.md"}


def _is_nested_vault_skeleton(md_file: Path) -> bool:
    """True when a candidate file is a nested product-vault skeleton, not the
    owner's real vault content.

    A product build (e.g. `vault/projects/AI Native OS/build/`) can carry its
    own scaffold `vault/`, complete with `SOUL.md`/`MEMORY.md` twins and
    `.template.md` stand-ins. Those are near-duplicates of the real
    `vault/SOUL.md`, `vault/MEMORY.md`, etc. and pollute search results if
    indexed alongside them.
    """
    parts = md_file.relative_to(REPO_ROOT).parts
    # parts[0] is expected to be "vault" itself — look for another "vault"
    # directory nested deeper than that.
    if "vault" in parts[1:]:
        return True
    if md_file.name.endswith(".template.md"):
        return True
    if md_file.name in _CORE_VAULT_FILES:
        # Real core files live directly under the top-level vault/ — i.e.
        # parts == ("vault", "<filename>"). Anything nested deeper is a
        # skeleton copy.
        if parts[:-1] != ("vault",):
            return True
    return False


def crawl_vault() -> list[tuple[str, str]]:
    """Crawl vault/ for .md files (skip EXCLUDED_PATHS and, on the walk path,
    EXCLUDED_DIR_SEGMENTS). Returns list of (relative_path, content) and records
    LAST_CRAWL_STATS so a silent loss becomes a visible count."""
    candidates, source = vault_markdown_files()
    stats = {"considered": len(candidates), "indexed": 0, "skipped_excluded": 0,
             "skipped_ignored": 0, "skipped_empty": 0, "skipped_nested": 0, "source": source}
    if source == "git":
        walked = {p for p in VAULT_DIR.rglob("*.md")}
        stats["skipped_ignored"] = len(walked - set(candidates))
    files = []
    for md_file in candidates:
        rel_path = str(md_file.relative_to(REPO_ROOT))
        if rel_path in EXCLUDED_PATHS:
            stats["skipped_excluded"] += 1
            continue
        if source == "walk" and EXCLUDED_DIR_SEGMENTS.intersection(md_file.relative_to(REPO_ROOT).parts):
            stats["skipped_ignored"] += 1
            continue
        if _is_nested_vault_skeleton(md_file):
            stats["skipped_nested"] += 1
            continue
        content = md_file.read_text(encoding="utf-8")
        if not content.strip():
            stats["skipped_empty"] += 1
            continue
        files.append((rel_path, content))
    stats["indexed"] = len(files)
    LAST_CRAWL_STATS.clear()
    LAST_CRAWL_STATS.update(stats)
    return files


def serialize_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def prune_removed_chunks(db, current_hashes: set[str]) -> int:
    """Drop indexed chunks that no longer exist anywhere in the vault.

    The incremental run only ever INSERTed, so a deleted note — or the previous
    wording of an edited paragraph — stayed searchable forever. On 2026-08-25
    the live index carried 220 such chunks from 40 files that were gone from
    disk, and the only thing that ever cleared them was a full reindex.

    `chunks_fts` is an external-content FTS5 table, so its rows cannot be
    removed with a plain DELETE; the 'delete' command needs the original text
    handed back to it to unpick the terms it indexed.

    Callers must only reach this with a crawl that actually produced content.
    An empty crawl is far more likely a broken path than a genuinely emptied
    vault, and pruning against it would erase the whole index.
    """
    rows = db.execute("SELECT id, hash, text FROM chunks").fetchall()
    stale = [(row_id, text) for row_id, chunk_hash, text in rows
             if chunk_hash not in current_hashes]

    for row_id, text in stale:
        db.execute(
            "INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', ?, ?)",
            (row_id, text),
        )
        db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (row_id,))
        db.execute("DELETE FROM chunks WHERE id = ?", (row_id,))

    if stale:
        db.commit()
    return len(stale)


def index_vault(incremental: bool = False) -> None:
    """Full or incremental indexing of the vault."""
    db = get_connection()
    files = crawl_vault()
    stats = LAST_CRAWL_STATS
    print(
        f"crawl ({stats['source']}): {stats['indexed']} indexed, "
        f"{stats['skipped_ignored']} ignored by git/segments, "
        f"{stats['skipped_excluded']} excluded, {stats['skipped_empty']} empty, "
        f"{stats['skipped_nested']} nested vault skeletons",
        file=sys.stderr,
    )

    all_chunks = []
    for rel_path, content in files:
        all_chunks.extend(chunk_markdown(content, rel_path))

    if not all_chunks:
        print("No content to index — index left untouched.", file=sys.stderr)
        db.close()
        return

    # Header fields and links cost no embedding, so they are refreshed on every
    # run — full or incremental — and stay in step with the vault for free.
    index_file_facts(db, files)

    current_hashes = {c["hash"] for c in all_chunks}

    if incremental:
        existing_hashes = {
            row[0] for row in db.execute("SELECT hash FROM chunks").fetchall()
        }
        removed = prune_removed_chunks(db, current_hashes)
        if removed:
            print(f"Pruned {removed} chunks no longer in the vault.", file=sys.stderr)
        new_chunks = [c for c in all_chunks if c["hash"] not in existing_hashes]
    else:
        db.execute("DELETE FROM chunks")
        db.execute("DELETE FROM vec_chunks")
        db.execute("DELETE FROM chunks_fts")
        db.commit()
        new_chunks = all_chunks

    if not new_chunks:
        print("No new or changed chunks to index.", file=sys.stderr)
        db.close()
        return

    print(f"Indexing {len(new_chunks)} chunks from {len(files)} files...", file=sys.stderr)

    now = datetime.now(timezone.utc).isoformat()
    # Batch the embed-and-write loop so VPS (low RAM) doesn't OOM. Embedding a
    # full vault of ~930 chunks at once peaked >2GB on VPS. 100 per batch keeps
    # the working set bounded and commits checkpoint progress.
    BATCH_SIZE = 100
    for batch_start in range(0, len(new_chunks), BATCH_SIZE):
        batch = new_chunks[batch_start:batch_start + BATCH_SIZE]
        texts = [c["text"] for c in batch]
        vectors = embed_texts(texts)

        for chunk, vec in zip(batch, vectors):
            cursor = db.execute(
                "INSERT OR IGNORE INTO chunks "
                "(text, source, heading, updated_at, hash, start_line, end_line) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chunk["text"], chunk["source"], chunk["heading"], now, chunk["hash"],
                 chunk["start_line"], chunk["end_line"])
            )
            if cursor.lastrowid and cursor.rowcount > 0:
                row_id = cursor.lastrowid
                db.execute(
                    "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                    (row_id, serialize_vec(vec))
                )
                db.execute(
                    "INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)",
                    (row_id, chunk["text"])
                )
        db.commit()
        print(f"  ...batch {batch_start // BATCH_SIZE + 1} ({batch_start + len(batch)}/{len(new_chunks)})", file=sys.stderr)

    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"Done. Total chunks in index: {total}", file=sys.stderr)
    db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Index vault markdown files for search")
    parser.add_argument("--incremental", action="store_true",
                        help="Only index new/changed chunks (skip existing)")
    args = parser.parse_args()
    index_vault(incremental=args.incremental)
