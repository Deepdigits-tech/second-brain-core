# HEARTBEAT — Proactive Checklist

_This file defines what to check during heartbeat runs. Configurable via the heartbeat script._

## Active Hours

- **Start:** 6 AM {{TIMEZONE}}
- **End:** 10 PM {{TIMEZONE}}
- Outside these hours: runs silently unless urgent

## Quick Checks (Every Heartbeat)

- [ ] Any urgent emails in the last 2 hours?
- [ ] Any calendar events in the next 4 hours?
- [ ] Any overdue tasks or reminders?

## Proactive Suggestions

After reviewing the data above, think about:
- [ ] What should be prioritized right now given the calendar and deadlines?
- [ ] Is there anything coming up that needs prep?
- [ ] Are there any overdue items that need action?

## Anomaly Detection

Flag anything unusual:
- [ ] Spike in unread email count
- [ ] Cancelled or moved meetings
- [ ] Tasks overdue by more than a week

## When to Notify

**Notify immediately:**
- Urgent email from important contacts
- Calendar event starting in < 2 hours with no prep
- Overdue tasks

**Batch for next interaction:**
- Non-urgent emails
- Tasks due in > 48 hours
- Interesting but not urgent information

**Stay silent:**
- Nothing new since last check
- Outside active hours unless truly urgent
- Everything is on track

## Configured Integrations

Mark which integrations you plan to use:

- [ ] Gmail
- [ ] Google Calendar
- [ ] Telegram
- [ ] Slack
- [ ] Other: {{OTHER_INTEGRATIONS}}

---

_Update this file to customize what gets checked. Add integrations to the list above as you configure them._
