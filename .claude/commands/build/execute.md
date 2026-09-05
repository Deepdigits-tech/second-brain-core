---
description: Execute a plan file task by task, with a test and a commit per task
---

# Execute: build from the plan, nothing else

Argument: the path to a plan written by `/build:plan`.

## Setup

1. Run `/build:prime` if this session has not.
2. Work on a branch, never on `main`: `git switch -c <short-name>`.
3. Read the plan in full. If a task references code that no longer matches the
   repo, stop and say which task and why. Do not improvise around it.

## Per task, in order

1. Write the failing test from the plan. Run it. It must fail for the stated
   reason. If it passes already, something is off; say so before continuing.
2. Make the smallest change that turns it green.
3. Run the whole suite: `.venv/bin/python3 -m pytest -q`. The pass count must not
   drop below the baseline from prime.
4. Commit with the message the plan gives. One task, one commit.
5. Tick the task in the plan file and move on.

## Rules

- Do not add anything the plan does not ask for, even if it looks easy.
- Do not touch `vault/SOUL.md`, `vault/USER.md`, `vault/MEMORY.md`, the credentials
  file, or anything the secrets hook blocks. If a task needs that, stop and ask.
- If a test is hard to write, that is information about the design. Say so; do
  not delete the test.
- Never claim a task is done without pasting the test output that proves it.

## When all tasks are done

Run `/build:review`, then report: branch name, commits, final test counts, and any
task you could not complete with the reason. Leave merging to the user.
