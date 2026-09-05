---
description: Turn a feature request into a task-by-task plan a fresh session can execute
---

# Plan: write the build plan before writing code

Argument: a one-paragraph description of the feature or fix, or a path to a spec.

You are writing for an engineer who has never seen this repo and has no memory of
this conversation. Everything they need must be in the plan file.

## Before writing

1. Run `/build:prime` if this session has not.
2. Find the code the change touches. Read it. Quote line ranges in the plan.
3. Find the tests that cover that code. If none exist, the first task is writing one.
4. Ask the user up to three questions only if different answers would produce
   materially different plans. Otherwise decide, and write the decision down.

## Plan file

Save to `docs/plans/YYYY-MM-DD-<short-name>.md` with this shape:

```
# <Feature> plan

Goal: one sentence.
Why: two or three sentences on what is wrong today.
Constraints: every rule from CLAUDE.md this change must respect, one per line.

## Task 1: <name>
Files: exact paths to create or change, with line ranges for edits.
Test first: the failing test, as real code.
Then: the smallest change that makes it pass, as real code or an exact description.
Verify: the exact command and the expected output.
Commit: the commit message.

## Task 2: ...
```

Rules for tasks:

- Each task ends in a passing test and a commit. If it cannot, split it.
- Never write "handle errors" or "add validation" without the actual code.
- Never write "same as Task N". Repeat the code; tasks may be read out of order.
- A task touches the vault, the identity files, or the credentials file only if
  the plan says so explicitly and the user has agreed.

## After writing

Re-read the request against the plan. Every sentence of the request must map to a
task. List anything that does not, and add a task or say why it is out of scope.
Then tell the user the file path and stop. Do not start building.
