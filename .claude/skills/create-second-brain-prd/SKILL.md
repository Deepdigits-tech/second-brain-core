---
name: create-second-brain-prd
description: Turn a filled-in requirements file into a phased build plan for a personal AI second brain on Claude Code. Use when the user has completed my-second-brain-requirements.md, or says "create my second brain PRD", "generate my PRD", "build plan for my second brain", or runs /create-second-brain-prd.
---

# Second Brain PRD generator

You read one requirements file and write one build plan. The plan must be
concrete enough that a coding agent with no memory of this conversation can
build each phase from it, and honest enough that the owner can see what they
are getting and what they are not.

## Parameters

- `$ARGUMENTS`: path to the filled-in requirements file. If missing, look for
  `my-second-brain-requirements.md` in the working directory. If that is missing
  too, tell the user where the blank template lives in this skill folder and stop.

## Workflow

1. Read the requirements file. Read `references/architecture-reference.md` from
   this skill folder. The reference is the source of truth for how each mechanism
   works; do not invent alternatives to it.
2. Check the answers. For every blank or vague section, decide whether the plan
   would differ materially depending on the answer. If yes, ask, up to three
   questions in one message. If no, choose the safe default below and write the
   choice into the plan's "Assumptions" section.
3. Write the plan to `second-brain-prd.md` next to the requirements file. Do not
   start building anything.
4. End with a five-line summary: what phase one delivers, the first integration,
   the proactivity level, the hard rules, and the open questions.

Safe defaults when an answer is blank: proactivity "watchful", active hours 7:00
to 21:00 in the given timezone, notifications to the first configured chat
platform, laptop-only infrastructure, no paid model API.

## The shipped code is the starting point

This skill lives inside a working system. The plan must build on it, not describe
it again for someone to rewrite:

- Phases 1 to 8 map onto code that already exists in this repository. For each,
  name the shipped file (for example `.claude/scripts/heartbeat.py`,
  `.claude/scripts/memory_reflect.py`, `.claude/hooks/block-secrets.py`) and say
  what to configure or extend in it. "Write a heartbeat" is wrong; "set these
  variables, add this lane inside the feature wrapper, keep the gather, diff,
  guardrail, agent, notify order" is right.
- New code is written only for a job in section 3 that no shipped file does. Say
  so explicitly in that phase: "new, because nothing shipped does X".
- Every phase's Test step includes running the shipped suite,
  `.venv/bin/python3 -m pytest -q`, and states that the pass count must not drop
  below what it was before the phase. A green suite is the proof the mechanism
  still works; a description of the mechanism is not.
- If a requirement would need changing a security file (the hooks, the sanitizer,
  the guardrail), the plan says which test in `tests/` covers it and adds a test
  for the change first.

## Plan shape

Write it with these sections, in this order. Every phase must be buildable and
testable on its own, and phase one must be usable by itself.

```
# <Name>'s Second Brain

## Assumptions
## Hard rules            (verbatim from section 5, plus the defaults from the reference)
## Phase 1: Vault and identity
## Phase 2: Session memory hooks
## Phase 3: Search
## Phase 4: First integration
## Phase 5: Heartbeat
## Phase 6: Reflection
## Phase 7: Chat
## Phase 8: Security hardening
## Phase 9: Second machine (only if section 7 asks for one)
## Later                  (everything from section 3 that did not fit above)
## Verification checklist
```

Each phase has exactly these headings:

- **Delivers**: one sentence the owner can check.
- **Reuses**: the shipped files this phase configures or extends, by path.
- **Files**: exact paths to create or change. New files are the exception and
  say why.
- **Build**: numbered steps. Name the functions, the env variables, and the
  file formats. Where the reference gives a mechanism (state diffing, the
  guardrail order, the index-plus-pages layout, the three-file page threshold,
  the append-only merge driver), restate it in the step rather than pointing
  at it; the builder will not have the reference open.
- **Test**: what to run and what output proves it works.
- **Out of scope**: what this phase deliberately does not do.

## Personalization rules

- Every job from section 3 lands in exactly one phase or in "Later". None vanish.
- Section 4 sets the heartbeat: "quiet" means no heartbeat at all, phase 5 becomes
  a manual command; "watchful" means gather, diff, notify; "forward" adds drafts
  and the draft lifecycle folders.
- Section 5 becomes the hook pattern list in phase 8 and the rules block in
  `SOUL.md` in phase 1. Copy the owner's wording; do not soften it.
- Section 6 "never keep" becomes a rule for reflection and for the session flush.
  Both must refuse to write those categories, and the plan must say how that is
  tested.
- Section 8 decides phase 4. The second and third integrations go in "Later"
  with a one-line note on what each adds.
- Timezone from section 1 goes into every schedule. Never write a time without it.
- The owner's name appears only as the value of an environment variable in the
  plan, never as a literal inside a code step.

## What not to do

- Do not produce a marketing document. No benefits, no vision paragraphs.
- Do not describe mechanisms that are not in the reference as if they exist.
- Do not collapse the guardrail into the main agent call to save a step.
- Do not propose sending anything on the owner's behalf, in any phase.
- Do not exceed roughly 400 lines. A plan nobody reads is not a plan.
