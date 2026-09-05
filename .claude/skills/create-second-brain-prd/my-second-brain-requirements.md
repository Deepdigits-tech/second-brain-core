# Second Brain Requirements

Fill this in, then run `/create-second-brain-prd ./my-second-brain-requirements.md`.
Short answers are fine. Blank answers are fine too; the skill will ask, or pick the
safe default and say so. Do not put passwords, keys, or account numbers in here.

## 1. About you

- Name the assistant should use for you:
- Timezone (IANA name, e.g. `Europe/Berlin`):
- What you do, in one or two sentences:
- The two or three things you are working on right now:
- How you like to be spoken to (blunt, warm, brief, detailed, no jokes, etc.):

## 2. Where your work lives

Tick what applies and add the account label you want the assistant to use.

- [ ] Gmail, accounts: (e.g. `personal`, `work`)
- [ ] Google Calendar, accounts:
- [ ] Telegram
- [ ] Slack, workspace:
- [ ] Other (name it, and whether it has an API):

Which one of these is the inbox that matters most?

## 3. What you want it to do for you

List up to five jobs, most valuable first. For each: what triggers it, what the
output looks like, and whether you must approve before anything leaves the machine.

1.
2.
3.
4.
5.

## 4. How proactive

Pick one and add any limits (hours, days, channels).

- [ ] Quiet: only answers when asked.
- [ ] Watchful: checks on a schedule, tells you only when something changed.
- [ ] Forward: checks on a schedule and drafts replies or next steps for approval.

Active hours (e.g. 6:00 to 22:00):
Where notifications should arrive (Telegram, Slack, desktop, none):

## 5. Lines it must never cross

Write the rules in your own words. Common ones: never send anything on my behalf,
never touch money, never delete files without asking, never edit its own
personality file, never read outside the vault. Add yours.

-
-
-

## 6. What it should remember

Which kinds of things are worth keeping for months, not days? Examples: people
and what they care about, project status, decisions and why, lessons, health,
money rules. Anything it should refuse to remember?

Keep:
Never keep:

## 7. Where it runs

- [ ] This laptop only
- [ ] This laptop plus an always-on server (name the OS)
- Is Docker acceptable? (yes / no / prefer not)
- Is a paid model API acceptable for cheap background calls, or Claude Code only?

## 8. Build order

Rank the integrations from section 2 in the order you want them wired. The first
one is what the very first working version will use; the rest come later.

1.
2.
3.
