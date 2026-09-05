#!/usr/bin/env python3
"""Queries over the `[[wikilink]]` graph in the vault.

Built on request, against the evidence. A survey on 2026-08-29 found links in
only 46 of 680 notes — 142 in total — and the apparent hubs were mostly
template placeholders (`[[Department/Person]]`, `[[Target]]`, `[[Note Name]]`)
sitting in prose inside vendored third-party docs under
`vault/kb/aios/sources/raw/`. Strip those and roughly 40 real links remain.

That shaped the design rather than blocking it: every query takes a
`path_prefix`, so the caller decides whose links to look at. Hardcoding "ignore
vendored sources" would be this module guessing which of the owner's notes are
real, and the guess would rot the moment the vault reorganised.

A link is "dead" when nothing in the vault answers to it. That is a fact, not a
fault — a link to an attachment, or to a note deliberately not written yet, is
dead by the same test. The output is a list to read, never an error.

The graph is rebuilt by `memory_index.index_file_facts()` on every indexer run.
It costs no embedding, so it is always in step with the vault.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import get_connection

# A dead link's target is "vendored" when it was written in third-party source
# material we ingested, not authored ourselves — its broken links are the
# original author's problem. `sources/raw/` is where the YouTube/KB ingestion
# and vendored KB docs land (see memory_index.py, memory_search.py).
_VENDORED_MARK = "/sources/raw/"

# Obsidian's own placeholder convention — [[Target]], [[Note Name]],
# [[Department/Person]] — reads exactly like a real wikilink but was never
# meant to resolve. Title Case words (optionally slash-separated segments)
# match the pattern; a fixed set catches the couple of lowercase placeholders
# that don't fit the shape (`wikilinks`, `link`, `links` — found while
# reading real dead-link output, not guessed). Both were derived from a
# 2026-08-29 survey of the vault's actual dead links (see memory_links_test
# module docstring / task-3 brief), not invented in the abstract.
_TEMPLATE_RE = re.compile(
    r"^[A-Z][A-Za-z]*( [A-Z][A-Za-z]*)*(/[A-Z][A-Za-z]*)*$"
)
_TEMPLATE_WORDS = {"wikilinks", "link", "links"}

_CATEGORY_ORDER = ("real", "template", "vendored")


def categorise(src: str, target: str) -> str:
    """Classify a genuinely unresolved (dst IS NULL) dead link.

    `vendored` beats `template` — a placeholder inside vendored source
    material is still the vendor's problem, not a template the owner left in
    their own notes.
    """
    if _VENDORED_MARK in src:
        return "vendored"
    cleaned = target.strip().removesuffix(".md")
    if cleaned.lower() in _TEMPLATE_WORDS or _TEMPLATE_RE.match(cleaned):
        return "template"
    return "real"


def _scoped(column: str, path_prefix: str | None) -> tuple[str, list]:
    if not path_prefix:
        return "", []
    return f" AND {column} LIKE ?", [f"{path_prefix}%"]


def all_links(path_prefix: str | None = None) -> list[dict]:
    """Every link, resolved or not. `dst` is None when nothing answers to it."""
    db = get_connection()
    clause, params = _scoped("src", path_prefix)
    rows = db.execute(
        f"SELECT src, target, dst FROM links WHERE 1=1{clause} ORDER BY src, target",
        params,
    ).fetchall()
    db.close()
    return [{"src": r[0], "target": r[1], "dst": r[2]} for r in rows]


def dead_links(path_prefix: str | None = None) -> list[dict]:
    """Links whose target nothing answers to — neither the vault nor the
    auto-memory store. A cross-store link (resolved with store="memory") is
    NOT dead; see `cross_store_links`.

    Scope this to your own notes. Unscoped it is dominated by placeholders in
    ingested third-party source material, whose broken links are the original
    author's problem, not yours.
    """
    db = get_connection()
    clause, params = _scoped("src", path_prefix)
    rows = db.execute(
        f"SELECT src, target FROM links WHERE dst IS NULL{clause} ORDER BY src, target",
        params,
    ).fetchall()
    db.close()
    return [{"src": r[0], "target": r[1], "category": categorise(r[0], r[1])} for r in rows]


def dead_links_by_category(path_prefix: str | None = None) -> dict[str, list[dict]]:
    """`dead_links` grouped into real / template / vendored."""
    grouped: dict[str, list[dict]] = {cat: [] for cat in _CATEGORY_ORDER}
    for d in dead_links(path_prefix=path_prefix):
        grouped[d["category"]].append(d)
    return grouped


def cross_store_links(path_prefix: str | None = None) -> list[dict]:
    """Links that resolve into the auto-memory store rather than the vault.

    Not dead — the checker used to report these as broken because it only
    ever knew about one root.
    """
    db = get_connection()
    clause, params = _scoped("src", path_prefix)
    rows = db.execute(
        f"SELECT src, target, dst FROM links WHERE store = 'memory'{clause} ORDER BY src, target",
        params,
    ).fetchall()
    db.close()
    return [{"src": r[0], "target": r[1], "dst": r[2], "store": "memory"} for r in rows]


def hubs(path_prefix: str | None = None, limit: int = 20) -> list[dict]:
    """Notes ranked by how many OTHER notes point at them.

    Counted per source file, so a note linking the same target three times
    contributes one — otherwise a single enthusiastic page would invent a hub.
    """
    db = get_connection()
    clause, params = _scoped("dst", path_prefix)
    rows = db.execute(
        f"SELECT dst, COUNT(DISTINCT src) AS inbound FROM links "
        f"WHERE dst IS NOT NULL AND (store IS NULL OR store = 'vault'){clause} "
        f"GROUP BY dst ORDER BY inbound DESC, dst ASC LIMIT ?",
        params + [limit],
    ).fetchall()
    db.close()
    return [{"dst": r[0], "inbound": r[1]} for r in rows]


def backlinks(source: str) -> list[str]:
    """Which notes point at this one."""
    db = get_connection()
    rows = db.execute(
        "SELECT DISTINCT src FROM links WHERE dst = ? "
        "AND (store IS NULL OR store = 'vault') ORDER BY src",
        (source,),
    ).fetchall()
    db.close()
    return [r[0] for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the vault's [[wikilink]] graph")
    sub = parser.add_subparsers(dest="command", required=True)

    p_hubs = sub.add_parser("hubs", help="notes the most other notes point at")
    p_hubs.add_argument("--path-prefix", "-p", help="only count links into this path")
    p_hubs.add_argument("--limit", "-n", type=int, default=20)

    p_dead = sub.add_parser("dead", help="links whose target does not exist")
    p_dead.add_argument("--path-prefix", "-p",
                        help="only links written in this path — scope to your own "
                             "notes to skip ingested third-party sources")
    p_dead.add_argument("--flat", action="store_true",
                        help="old ungrouped list instead of real/template/vendored groups")

    p_back = sub.add_parser("backlinks", help="notes pointing at one note")
    p_back.add_argument("source", help="vault-relative path, e.g. vault/MEMORY.md")

    for p in (p_hubs, p_dead, p_back):
        p.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args()

    if args.command == "hubs":
        results = hubs(path_prefix=args.path_prefix, limit=args.limit)
        if args.as_json:
            print(json.dumps(results, indent=2))
        elif not results:
            print("No resolved links found.", file=sys.stderr)
        else:
            for r in results:
                print(f"{r['inbound']:>4}  {r['dst']}")

    elif args.command == "dead":
        if args.flat:
            results = dead_links(path_prefix=args.path_prefix)
            if args.as_json:
                print(json.dumps(results, indent=2))
            elif not results:
                print("No dead links.", file=sys.stderr)
            else:
                for r in results:
                    print(f"{r['src']}  ->  [[{r['target']}]]")
                sys.stdout.flush()
                print(f"\n{len(results)} dead link(s). A link to an attachment or to a "
                      f"note not written yet counts here too.", file=sys.stderr)
        else:
            grouped = dead_links_by_category(path_prefix=args.path_prefix)
            if args.as_json:
                print(json.dumps(grouped, indent=2))
            else:
                total = 0
                for cat in _CATEGORY_ORDER:
                    items = grouped[cat]
                    total += len(items)
                    if not items:
                        continue
                    print(f"== {cat} ({len(items)}) ==")
                    for r in items:
                        print(f"{r['src']}  ->  [[{r['target']}]]")
                if total == 0:
                    print("No dead links.", file=sys.stderr)
                sys.stdout.flush()
                cross = cross_store_links(path_prefix=args.path_prefix)
                print(f"\n{total} dead link(s). A link to an attachment or to a "
                      f"note not written yet counts here too.", file=sys.stderr)
                print("A Title-Case target such as [[Acme Corp]] is filed under "
                      "template even when it is a real note not yet written — "
                      "check that bucket too.", file=sys.stderr)
                print(f"cross-store ({len(cross)}) resolved in the auto-memory store",
                      file=sys.stderr)

    else:
        results = backlinks(args.source)
        if args.as_json:
            print(json.dumps(results, indent=2))
        elif not results:
            print(f"Nothing links to {args.source}.", file=sys.stderr)
        else:
            for src in results:
                print(src)


if __name__ == "__main__":
    main()
