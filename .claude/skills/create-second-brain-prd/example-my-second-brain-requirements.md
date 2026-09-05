# Second Brain Requirements (worked example)

A filled-in copy for a fictional person, so you can see the level of detail that
produces a good plan. Copy the shape, not the answers.

## 1. About you

- Name the assistant should use for you: Maya
- Timezone (IANA name): `America/Toronto`
- What you do: I run a two-person design studio and teach one evening course a week.
- Working on right now: a rebrand for a bakery chain, a course syllabus refresh,
  and finding a bookkeeper.
- How you like to be spoken to: brief, plain, no cheerleading. Tell me when I am
  wrong.

## 2. Where your work lives

- [x] Gmail, accounts: `personal`, `studio`
- [x] Google Calendar, accounts: `studio`
- [x] Telegram
- [ ] Slack
- [ ] Other

Inbox that matters most: `studio`

## 3. What you want it to do for you

1. Morning brief. Trigger: 7:00 on weekdays. Output: today's meetings, anything in
   `studio` mail from a client that needs a reply, and one line on what I said I
   would do yesterday. Approval: none, it is read-only.
2. Reply drafts for client mail. Trigger: a client email sits unanswered for 4
   hours. Output: a draft in my voice, saved as a draft, not sent. Approval: always.
3. Meeting prep. Trigger: 30 minutes before any meeting with an outside person.
   Output: who they are from my notes, last thing we discussed, open items.
4. Remember decisions. Trigger: I say "remember this" in Telegram. Output: a dated
   line on the right project page.
5. Weekly review nudge. Trigger: Sunday 17:00. Output: what moved this week, what
   is stale, three questions for me.

## 4. How proactive

- [x] Forward: checks on a schedule and drafts for approval.

Active hours: 7:00 to 21:00, Monday to Saturday. Silent on Sundays except the review.
Notifications: Telegram only.

## 5. Lines it must never cross

- Never send an email or message. Drafts only, always.
- Never touch anything to do with money or the bank.
- Never delete a file. Move it to an archive folder and tell me.
- Never change its own personality or my profile without me in the loop.
- Never read files outside the vault folder.

## 6. What it should remember

Keep: clients and their preferences, project status and deadlines, decisions and
the reason behind them, lessons from things that went wrong, people I meet and
where.
Never keep: anything about my health, anything from personal mail marked private,
passwords or account details even if I paste them.

## 7. Where it runs

- [x] This laptop plus an always-on server (Ubuntu)
- Docker: prefer not.
- Paid model API for cheap background calls: yes, with a monthly cap.

## 8. Build order

1. Gmail `studio`
2. Google Calendar `studio`
3. Telegram
4. Gmail `personal` (read-only, later)
