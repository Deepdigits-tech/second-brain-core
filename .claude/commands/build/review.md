---
description: Review the current branch against its plan before it is merged
---

# Review: check the work, not the intent

Argument: optional plan path. Without it, review the diff from `main` to `HEAD`.

Be the reviewer who has to maintain this in a year. Kindness is precision.

## Do

1. `git diff main...HEAD --stat` then the full diff. Read every hunk.
2. Run the suite: `.venv/bin/python3 -m pytest -q`. Paste the last line.
3. If a plan is given, check each task: is it done as written, and is there a
   test that would fail if it were undone?
4. Look specifically for:
   - external text (email, chat, calendar) that reaches a prompt without passing
     through `sanitize.py`
   - a new file under `vault/` that automation writes to and reflection does not
     know about
   - a secret, an email address, a hostname, or a person's name in code or tests
   - a test that asserts nothing, or that mocks the very thing it claims to test
   - anything the plan did not ask for
5. Run `.venv/bin/python3 .claude/scripts/hygiene.py --dry-run` if the vault changed.

## Report, in this order

1. **Verdict**: merge, fix first, or do not merge.
2. **Blocking findings**: file and line, what is wrong, what would go wrong in
   production. Nothing else goes in this list.
3. **Should fix**: same format, lower stakes.
4. **Skipped on purpose**: anything you chose not to flag and why.

Do not fix anything yourself during review. Findings go back to `/build:execute`.
