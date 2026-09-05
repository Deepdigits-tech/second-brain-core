---
description: Load the project into context before planning or building anything
---

# Prime: learn the codebase before touching it

Run this at the start of every session that will change code. A fresh session
knows nothing; this command gives it the map before it makes a move.

## Steps

1. Read `CLAUDE.md` in full. It is the current state of the system, the commands,
   and the conventions. Treat it as authoritative over anything you infer from code.
2. Read `README.md`, then list the tree: `git ls-files | head -200`.
3. Read these files end to end. They carry most of the design:
   - `.claude/scripts/shared.py` (config, vault paths, session context builder)
   - `.claude/scripts/heartbeat.py` (the scheduled loop)
   - `.claude/scripts/sanitize.py` and `.claude/scripts/guardrail.py` (the trust boundary)
   - `.claude/hooks/block-secrets.py` (what you are not allowed to touch)
4. Run the test suite once so you know the baseline: `.venv/bin/python3 -m pytest -q`.
   Note the pass count. A later run must not go below it.
5. Check `git status` and `git log --oneline -10` so you know what is in flight.

## Report back, in this shape

- **What this system is** in two sentences.
- **The three rules you must not break** (pick them from CLAUDE.md's security
  boundaries and the vault writing rules).
- **Baseline**: tests passed / skipped, current branch, dirty files if any.
- **Open questions** you could not answer from the files. Do not guess at them.

Do not change anything during prime. Reading only.
