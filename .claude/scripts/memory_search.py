#!/usr/bin/env python3
"""Hybrid search over the Cortex OS vault."""

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import get_connection
from embeddings import embed_query


# sqlite-vec rejects a knn query asking for more neighbours than this.
MAX_KNN = 4096

# How much of a chunk a result carries, as a hint for judging relevance.
EXCERPT_CHARS = 200


def normalize(rows: list[tuple], lower_is_better: bool = True) -> dict[int, float]:
    """Spread one engine's raw scores across 0..1 so the two can be blended.

    Both halves must use the SAME shape or the weights are a fiction. They did
    not: vector rows were scored `1 - dist/max_dist`, which on a tight cluster
    of distances (0.90 to 0.98, say) compresses everything into the low
    hundredths, while keyword rows were scored `rank/min_rank`, handing the best
    match a flat 1.0 however weak it was. A 0.3-weighted keyword hit at 1.0 then
    beat a 0.7-weighted vector hit at 0.06, so the keyword half won every
    multi-word query outright — "what did we decide about gold" returned an
    article about a gold-coloured watermark above the actual gold thesis.

    Min-max over the returned set puts the best row of each engine at 1.0 and
    the worst at 0.0, so `vector_weight` and `keyword_weight` mean what they say.
    """
    if not rows:
        return {}
    values = [value for _, value in rows]
    best, worst = (min(values), max(values)) if lower_is_better else (max(values), min(values))
    span = best - worst
    if not span:
        # One row, or a dead heat: nothing to rank, so neither wins on this half.
        return {rowid: 1.0 for rowid, _ in rows}
    return {rowid: (value - worst) / span for rowid, value in rows}


def parse_where(conditions: list[str] | None) -> list[tuple[str, str, str]]:
    """Turn `["type=kb-topic", "status!=processed"]` into (key, op, value)."""
    parsed = []
    for raw in conditions or []:
        if "!=" in raw:
            key, _, value = raw.partition("!=")
            op = "!="
        elif "=" in raw:
            key, _, value = raw.partition("=")
            op = "="
        else:
            raise ValueError(
                f"filter {raw!r} is not a condition — write it as key=value or key!=value"
            )
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise ValueError(
                f"filter {raw!r} is not a condition — write it as key=value or key!=value"
            )
        parsed.append((key, op, value))
    return parsed


def sources_matching(db, conditions: list[tuple[str, str, str]]) -> set[str] | None:
    """Which files satisfy every condition. None means "no filter asked for".

    `=` needs the field present with that value. `!=` deliberately KEEPS a file
    that has no such field at all: "not processed" has to include the notes
    nobody ever gave a status, or the filter quietly hides most of the vault.
    """
    if not conditions:
        return None

    allowed: set[str] | None = None
    for key, op, value in conditions:
        if op == "=":
            rows = db.execute(
                "SELECT source FROM file_meta WHERE key = ? AND value = ?", (key, value)
            ).fetchall()
            matching = {r[0] for r in rows}
        else:
            excluded = {
                r[0] for r in db.execute(
                    "SELECT source FROM file_meta WHERE key = ? AND value = ?", (key, value)
                )
            }
            everything = {r[0] for r in db.execute("SELECT DISTINCT source FROM chunks")}
            matching = everything - excluded
        allowed = matching if allowed is None else (allowed & matching)
    return allowed or set()


def excerpt_of(text: str) -> str:
    """A one-line taste of a chunk, for deciding whether to open it."""
    flat = " ".join(text.split())
    if len(flat) <= EXCERPT_CHARS:
        return flat
    return flat[:EXCERPT_CHARS - 1].rstrip() + "…"


def serialize_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def keyword_candidates(db, query_text: str, limit: int) -> list[tuple]:
    """Ask FTS5 for matches, honouring query operators where the syntax allows.

    The query used to be wrapped in double quotes unconditionally, which turned
    every search into an exact-phrase match: `revenue OR earnings` hunted for
    that literal string, so on any multi-word query the keyword half of the
    hybrid contributed nothing and the blend was silently vector-only.

    Raw syntax gives bare words their AND join and makes OR / NOT / NEAR work.
    It also makes hyphens, stray quotes and bare operators hard syntax errors —
    `acme-corp` parses as a column reference. For those the quoted phrase is the
    right reading of what was typed, so it stands as the fallback.
    """
    quoted = '"' + query_text.replace('"', '""') + '"'
    for candidate in (query_text, quoted):
        try:
            return db.execute(
                "SELECT rowid, rank FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT ?",
                (candidate, limit),
            ).fetchall()
        except Exception:
            continue
    return []


def hybrid_search(
    query_text: str,
    limit: int = 10,
    path_prefix: str | None = None,
    vector_weight: float = 0.7,
    keyword_weight: float = 0.3,
    include_text: bool = False,
    where: list[str] | None = None,
    mode: str = "hybrid",
) -> list[dict]:
    """Run hybrid vector + keyword search.

    Returns list of dicts:
    {source, heading, start_line, end_line, lines, excerpt, score, vector_pool_truncated}

    A result is a POINTER, not a payload. It says where the answer lives and
    gives enough of it to judge relevance; whoever wants the rest opens the
    file at those lines. Results used to carry the whole chunk, so a ten-row
    search spent thousands of tokens on text that was mostly not read.

    `include_text=True` adds the full chunk back, for a caller that genuinely
    needs it and cannot read the file itself.

    `mode` selects which halves run: "hybrid" (both), "keyword" (word match only),
    or "semantic" (vector match only). Unknown mode raises ValueError.
    """
    if mode not in ("hybrid", "keyword", "semantic"):
        raise ValueError(f"mode must be hybrid, keyword or semantic; got {mode!r}")

    # Parse before touching the database so a typo'd filter fails immediately
    # rather than after an embedding call.
    conditions = parse_where(where)

    db = get_connection()
    allowed_sources = sources_matching(db, conditions)

    # Compute total once so truncated flag is well-defined on both branches.
    scoped = bool(path_prefix or allowed_sources is not None)
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or limit

    if scoped:
        sem_limit = min(total, MAX_KNN)
    else:
        sem_limit = limit * 3

    # Determine if the semantic pool was truncated by the hard ceiling.
    # Keyword mode never runs a vector pool, so it cannot be truncated.
    truncated = bool(scoped and total > MAX_KNN) and mode != "keyword"

    # Only embed the query if we're doing semantic search. Keyword mode has no use for it.
    query_vec = None if mode == "keyword" else serialize_vec(embed_query(query_text))

    # A scoped search has to consider far more than the top few. The prefix may
    # match nothing in a short pool, and the search then reports "no results"
    # for a folder that plainly has them. sqlite-vec scans linearly regardless,
    # so a wide pool costs a few thousand rows, not a rebuild — but its knn
    # implementation refuses a k above MAX_KNN, so that is the ceiling.
    semantic_rows = [] if query_vec is None else db.execute(
        "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (query_vec, sem_limit),
    ).fetchall()

    keyword_rows = [] if mode == "semantic" else keyword_candidates(db, query_text, sem_limit)

    sem_scores = normalize(semantic_rows, lower_is_better=True)
    kw_scores = normalize(keyword_rows, lower_is_better=True)

    all_rowids = set(sem_scores.keys()) | set(kw_scores.keys())
    combined = []
    for rowid in all_rowids:
        score = (
            sem_scores.get(rowid, 0.0) * vector_weight
            + kw_scores.get(rowid, 0.0) * keyword_weight
        )
        combined.append((rowid, score))

    combined.sort(key=lambda x: x[1], reverse=True)

    # Filter before truncating. The prefix used to be applied to an already
    # cut list, so a scoped search whose best hits lived elsewhere returned
    # nothing at all rather than the best rows inside the scope.
    results = []
    for rowid, score in combined:
        if len(results) >= limit:
            break
        row = db.execute(
            "SELECT text, source, heading, start_line, end_line FROM chunks WHERE id = ?",
            (rowid,),
        ).fetchone()
        if row is None:
            continue
        text, source, heading, start_line, end_line = row
        if path_prefix and not source.startswith(path_prefix):
            continue
        if allowed_sources is not None and source not in allowed_sources:
            continue

        # Why this row is here. A vector-only row means the query's words do
        # not appear in it at all — it surfaced on meaning alone, which is worth
        # knowing before trusting it.
        evidence = []
        if rowid in kw_scores:
            evidence.append("match")
        if rowid in sem_scores:
            evidence.append("vector")

        hit = {
            "source": source,
            "heading": heading,
            "via": "+".join(evidence),
            "start_line": start_line,
            "end_line": end_line,
            # None until a chunk indexed before line numbers existed is redone.
            "lines": f"{start_line}-{end_line}" if start_line and end_line else None,
            "excerpt": excerpt_of(text),
            "score": round(score, 4),
            "vector_pool_truncated": truncated,
        }
        if include_text:
            hit["text"] = text
        results.append(hit)

    db.close()
    return results


def main():
    parser = argparse.ArgumentParser(description="Search the Cortex OS vault")
    parser.add_argument("query", help="Search query text")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--path-prefix", "-p", help="Filter results to files under this path prefix")
    parser.add_argument("--mode", choices=["hybrid", "keyword", "semantic"], default="hybrid",
                        help="diagnostic: run one half of the hybrid on its own")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")
    parser.add_argument("--full", action="store_true",
                        help="Include the whole matched chunk, not just a pointer and excerpt")
    parser.add_argument("--where", "-w", action="append", metavar="KEY=VALUE",
                        help="Filter on the note's frontmatter, e.g. -w type=kb-topic "
                             "-w status!=processed. Repeatable; all conditions must hold.")
    args = parser.parse_args()

    try:
        results = hybrid_search(
            args.query,
            limit=args.limit,
            path_prefix=args.path_prefix,
            include_text=args.full,
            where=args.where,
            mode=args.mode,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(2)

    if args.as_json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No results found.", file=sys.stderr)
        return

    # Print truncation notice if any result has the flag.
    if any(r.get("vector_pool_truncated") for r in results):
        print(" [vector pool truncated at MAX_KNN]")

    for i, r in enumerate(results, 1):
        # `path:line` is the form editors and terminals turn into a jump.
        where = f"{r['source']}:{r['start_line']}" if r["start_line"] else r["source"]
        span = f" ({r['lines']})" if r["lines"] else ""
        heading = f" — {r['heading']}" if r["heading"] else ""
        print(f"\n{i}. {where}{span}{heading}   [{r['score']}]")
        print(f"   {r['excerpt']}")
        if args.full:
            print()
            print(r["text"])


if __name__ == "__main__":
    main()
