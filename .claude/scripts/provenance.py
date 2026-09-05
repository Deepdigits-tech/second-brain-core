"""OKF-style provenance and lifecycle fields for vault documents.

Adds three field families from the Open Knowledge Format v0.2 spec
(github.com/GoogleCloudPlatform/knowledge-catalog) to the memory and KB layers:

    generated:   {by: <actor>, at: <ISO 8601>}   how the content was produced
    verified:    [{by: <actor>, at: <ISO 8601>}] who confirmed it, and when
    status / stale_after                          lifecycle

Why it exists. Cortex writes to the vault from at least four actors — heartbeat,
reflection, the chat agent, and the owner directly — and until now the file recorded
none of that. An agent's guess and a human-confirmed fact were indistinguishable
on disk. The producer is already known at runtime via CLAUDE_INVOKED_BY; this is
where it finally reaches the file.

`stale_after` exists because file mtime lies in both directions: auto-sync
touches vault files every two minutes (rot looks fresh), while a stable
reference doc untouched for a year looks rotten. mtime remains the fallback for
documents that never declare an expiry.

Actor forms follow the OKF convention:
    human:<id>              a person
    process:<id>            an automated workflow
    <producer>/<version>    an agent

See vault/research/2026-08-14-okf-evaluation.md for the decision behind this.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

# Keys that lead the frontmatter block, in this order, for OKF readability.
# `type` is the only field OKF always requires.
_LEADING_KEYS = ("type", "title", "description")


def _default_human(env: dict | None = None) -> str:
    """The OKF actor for a human driving the session interactively.

    Derived from OWNER_NAME at call time (never a hardcoded name), so a
    fresh clone with no OWNER_NAME set still gets a valid, generic actor
    instead of someone else's identity baked into the source.
    """
    env = os.environ if env is None else env
    owner = (env.get("OWNER_NAME") or "").strip()
    if not owner:
        return "human:owner"
    slug = "-".join(owner.lower().split())
    return f"human:{slug}" if slug else "human:owner"


def current_actor(env: dict | None = None) -> str:
    """Resolve the OKF actor for whoever is writing right now.

    No CLAUDE_INVOKED_BY means a human is driving the session interactively.
    """
    env = os.environ if env is None else env
    invoked_by = (env.get("CLAUDE_INVOKED_BY") or "").strip()
    if not invoked_by:
        return _default_human(env)
    # Already an OKF actor (`human:x`, `process:x`, `producer/version`) — pass through.
    if ":" in invoked_by or "/" in invoked_by:
        return invoked_by
    return f"process:{invoked_by}"


def _iso(at: datetime | None) -> str:
    if at is None:
        at = datetime.now(timezone.utc)
    return at.isoformat()


def stamp_generated(meta: dict, actor: str | None = None, at: datetime | None = None) -> dict:
    """Record how this document was produced. Never touches `verified`."""
    meta = dict(meta)
    meta["generated"] = {"by": actor or current_actor(), "at": _iso(at)}
    return meta


def add_verification(meta: dict, actor: str | None = None, at: datetime | None = None) -> dict:
    """Append a confirmation. Appends — a prior check is not erased by a later one."""
    meta = dict(meta)
    existing = meta.get("verified")
    if isinstance(existing, dict):  # spec allows a single mapping
        entries = [existing]
    elif isinstance(existing, list):
        entries = list(existing)
    else:
        entries = []
    entries.append({"by": actor or current_actor(), "at": _iso(at)})
    meta["verified"] = entries
    return meta


def is_verified(meta: dict) -> bool:
    """True only if somebody actually confirmed this document."""
    entries = meta.get("verified")
    if isinstance(entries, dict):
        return bool(entries)
    return bool(entries) if isinstance(entries, list) else False


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None


def stale_reason(
    meta: dict,
    mtime: datetime,
    now: datetime | None = None,
    max_age_days: int = 90,
) -> str | None:
    """Why this document is stale, or None.

    Precedence: explicit `status: deprecated`, then `stale_after`, then mtime.
    A declared expiry wins over mtime in BOTH directions — that is the whole
    point, since mtime is noise on a repo that auto-syncs every two minutes.
    """
    now = now or datetime.now(timezone.utc)

    if str(meta.get("status", "")).strip().lower() == "deprecated":
        return "marked status: deprecated"

    expiry = _as_date(meta.get("stale_after")) if meta.get("stale_after") is not None else None
    if expiry is not None:
        if now.date() > expiry:
            return f"past declared stale_after ({expiry.isoformat()})"
        return None

    age_days = (now - mtime).days
    if age_days > max_age_days:
        return f"no expiry declared; not modified in {age_days} days"
    return None


def has_declared_lifecycle(meta: dict) -> bool:
    """True if the doc states its own lifecycle rather than leaving it to mtime.

    A malformed `stale_after` counts as undeclared — stale_reason fell back to
    mtime, so callers must not treat the result as a broken promise.
    """
    if str(meta.get("status", "")).strip().lower() == "deprecated":
        return True
    return _as_date(meta.get("stale_after")) is not None


def parse(text: str) -> tuple[dict, str]:
    """Split a markdown document into (frontmatter dict, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    try:
        meta = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    body = text[end + 4:].lstrip("\n")
    return meta, body


def render(meta: dict, body: str) -> str:
    """Write a document back out, `type` first, otherwise preserving key order.

    PyYAML for reading and writing rather than a hand-rolled writer, because
    `generated` and `verified` are nested structures where hand-rolling breaks.
    """
    ordered: dict = {}
    for key in _LEADING_KEYS:
        if key in meta:
            ordered[key] = meta[key]
    for key, value in meta.items():
        if key not in ordered:
            ordered[key] = value

    # Dumped one key at a time so key order is exact and leaf collections keep the
    # inline style the vault already uses (`tags: [aios, pricing]`,
    # `generated: {by: ..., at: ...}`). Dumping the whole mapping at once would
    # either collapse it entirely or reformat every existing list into a block,
    # producing a pointless diff across 154 KB files.
    chunks = []
    for key, value in ordered.items():
        chunk = yaml.safe_dump(
            {key: value}, sort_keys=False, default_flow_style=None,
            allow_unicode=True, width=1000,
        ).rstrip("\n")
        if chunk.startswith("{"):
            # Scalar-only pair collapsed to flow style; force block for the root.
            chunk = yaml.safe_dump(
                {key: value}, sort_keys=False, default_flow_style=False,
                allow_unicode=True, width=1000,
            ).rstrip("\n")
        chunks.append(chunk)

    return f"---\n{chr(10).join(chunks)}\n---\n\n{body.lstrip(chr(10))}"


def stamp_file(
    path: Path,
    actor: str | None = None,
    at: datetime | None = None,
    default_type: str | None = None,
    verified: bool = False,
) -> bool:
    """Stamp provenance onto a file in place. Returns True if the file changed.

    Idempotent: re-stamping identical data rewrites nothing, so this can run in
    a loop or a backfill without churning git history.
    """
    original = path.read_text()
    meta, body = parse(original)
    if default_type and "type" not in meta:
        meta["type"] = default_type

    meta = stamp_generated(meta, actor=actor, at=at)
    if verified:
        meta = add_verification(meta, actor=actor, at=at)

    updated = render(meta, body)
    if updated == original:
        return False
    path.write_text(updated)
    return True


def missing_provenance(root: Path) -> list[Path]:
    """Documents under root with no `generated` block — origin unknown."""
    out = []
    for path in sorted(Path(root).rglob("*.md")):
        if path.name in {"index.md", "log.md"}:
            continue
        try:
            meta, _ = parse(path.read_text())
        except (OSError, UnicodeDecodeError):
            continue
        if not meta.get("generated"):
            out.append(path)
    return out


def _main(argv: list[str] | None = None) -> int:
    """Stamp provenance at write time, or report what is unstamped.

    Deliberately no bulk backfill of historical files: stamping a document
    written in May as generated today, by whatever process happened to run the
    backfill, would be a fabricated record. Absence of `generated` honestly
    means "origin unknown". Only stamp at the moment of an actual write.
    """
    import argparse

    parser = argparse.ArgumentParser(description="OKF provenance stamping")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stamp", help="stamp a file you just wrote")
    s.add_argument("paths", nargs="+")
    s.add_argument("--actor", help="override actor (default: from CLAUDE_INVOKED_BY)")
    s.add_argument("--type", dest="default_type", help="type to set if the file has none")
    s.add_argument("--verified", action="store_true", help="also record a confirmation")

    c = sub.add_parser("check", help="list documents with no recorded origin")
    c.add_argument("root")

    args = parser.parse_args(argv)

    if args.cmd == "stamp":
        changed = 0
        for raw in args.paths:
            path = Path(raw)
            if not path.is_file():
                print(f"skip (not a file): {path}", file=sys.stderr)
                continue
            if stamp_file(
                path, actor=args.actor, default_type=args.default_type, verified=args.verified
            ):
                changed += 1
        print(f"stamped {changed} file(s) as {args.actor or current_actor()}")
        return 0

    unstamped = missing_provenance(Path(args.root))
    print(f"{len(unstamped)} document(s) with no recorded origin under {args.root}")
    for path in unstamped[:20]:
        print(f"  {path}")
    if len(unstamped) > 20:
        print(f"  ... and {len(unstamped) - 20} more")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
