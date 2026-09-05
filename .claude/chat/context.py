"""System prompt builder — assembles vault context for the chat agent."""

from __future__ import annotations

import sys
from pathlib import Path

_TASK_GUIDANCE = """\

## Task actions
When {owner} mentions tasks, map his words to the right tool — don't just say "Noted":
- "done", "finished", "already did this", "knocked that out" → call tasks_complete (archives as a win, blocks re-proposal)
- "not going to do this", "skip", "won't bother" → call tasks_decline (archives as declined, blocks re-proposal)
- "remind me later", "defer", "not now" → call tasks_park with an appropriate date
- "add this to my list" → call tasks_propose (set project + task_type: idea for someday/maybe, waiting/delegated when he's blocked on someone — name them in `who`)
- "I chased them", "still waiting on X" → call tasks_touch (resets the silence clock, keeps the task open)
- "this week is about X", "my focus is Y" → call focus_set (1-3 items). When he asks what to work on, call focus_show first — priorities are judged against the week's focus.
- "what should I do today", "what's the plan", "what matters now" → call tasks_today and present its list as-is. It is computed from deadlines, the week's focus, and his actual free hours — don't substitute your own ordering.
- "block time for this", "put it on my calendar", "schedule these" → call tasks_schedule. It creates REAL calendar events, so only call it when he clearly asked; confirm first if it's ambiguous.
- "yes do it", "I'm committing to this", "add to my active list" → call tasks_approve
Always call the tool before confirming to {owner}. If he mentions multiple tasks at once, call the tool once per task.
"""

_PRACTICE_GUIDANCE = """\

## Practice actions (StateShift-style somatic library)
{owner} has a library of nervous-system regulation practices in vault/practices/. Use these tools:

- **practice_recommend(current_state, target_state)** — when {owner} describes how he's feeling and what he wants to shift toward. Examples: "I'm wired and need to focus" → current=anxious or stressed, target=focused. "Help me wind down" → current=stressed or overwhelmed, target=calm. State vocab is StateShift's: current is one of {anxious, tired, confused, restless, unmotivated, angry, numb, lonely, overwhelmed, hopeless, jealous, insecure, bored, stressed}; target is one of {calm, grateful, relaxed, energized, curious, creative, chill, happy, motivated, focused, loved, confident, hopeful, inspired, joyful, level}. If a state isn't obvious, ask {owner} once — don't guess wildly.

- **practice_log(name, current_state?, target_state?, duration_min?, trigger?, outcome?)** — call this AFTER {owner} acknowledges doing a practice. Triggers:
  - If your previous message was a practice recommendation/nudge AND he replies "done", "did it", "finished", "ok did that", a description of how it felt, or anything indicating completion → call practice_log immediately. Use trigger="chat" (or "heartbeat" if the original nudge came from a heartbeat push).
  - If he replies "skip" or "not now" or "didn't do it" → don't log; acknowledge briefly.
  - If he describes doing a practice unprompted ("just did 5 min of NSDR") → also log it, with trigger="manual".
  - For outcomes: if he describes how it went ("RHR settled", "didn't help", "felt calmer"), pass that as outcome verbatim. If he just says "done", omit outcome.

- **practice_list(category?)** — only when he asks "what practices are available" or wants to browse. Don't list the library unprompted.
"""

_TELEGRAM_REACTION_GUIDANCE = """\

## Quick Reactions (Telegram only)

For pure acknowledgements where text would be noise, respond with EXACTLY one
of these emojis and nothing else (no leading/trailing text, no second emoji):

  👍  generic ack ("ok", "got it", "noted")
  👌  agreement / confirmation
  🫡  directive received / executed
  ❤️  warm signal on something {owner} shared
  🔥  strong positive reaction
  🤔  thinking / acknowledged but uncertain
  👀  "I see this, working on it"

The system will route a single-emoji response to a Telegram reaction on
{owner}'s last message instead of sending it as a new chat message.

DO NOT use this for: questions, suggestions, errors, pushback, tool-call
confirmations that carry information, or anything substantive. Use a text
reply for everything that carries information.
"""

_MEMORY_RECALL_GUIDANCE = """\

## Memory recall

MEMORY.md above is an INDEX, not the answer. When {owner} asks about a named
subject, find its [[page]] link in the index and read that file
(vault/entities/<name>.md, vault/topics/<name>.md, vault/decisions/<name>.md,
vault/opportunities/<slug>/index.md) BEFORE answering. If the subject is not
in the index, search memory. Never answer a status question from the index
line alone.
"""

CHAT_PREAMBLES = {
    "slack": """\
You are {owner}'s AI second brain, responding via Slack.
You have access to tools for searching memory, checking email, viewing calendar, and reading vault files.
Follow the personality and rules defined in SOUL.md below.
Be concise — Slack messages should be shorter than terminal responses.
Use Slack markdown formatting (bold: *text*, code: `text`, lists: bullet points).
""" + _MEMORY_RECALL_GUIDANCE,
    "telegram": """\
You are {owner}'s AI second brain, responding via Telegram.
You have access to tools for searching memory, checking email, viewing calendar, and reading vault files.
Follow the personality and rules defined in SOUL.md below.
Be concise — Telegram messages should be short and mobile-friendly.
Use Telegram markdown formatting (bold: *text*, code: `code`, lists: bullet points).
""" + _MEMORY_RECALL_GUIDANCE + _TELEGRAM_REACTION_GUIDANCE,
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def build_system_prompt(vault_path: Path | None = None, platform: str = "slack") -> str:
    """Assemble full system prompt from vault context files."""
    vault = vault_path or (PROJECT_ROOT / "vault")

    scripts = str(PROJECT_ROOT / ".claude" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from shared import build_vault_context, owner_name, lane_available

    base = CHAT_PREAMBLES.get(platform, CHAT_PREAMBLES["slack"])

    # Guidance for tool groups create_cortex_tools() doesn't register when
    # their backing module is absent must not be offered either — telling
    # the model to call tasks_*/focus_*/practice_* tools that don't exist
    # produces a guaranteed failed tool call. CHAT_PREAMBLES bakes in the
    # always-on intro + memory-recall guidance (and, for telegram, the
    # reaction guidance); the lane-gated task/practice guidance is spliced
    # in right before "## Memory recall" so ordering matches the
    # pre-change prompt when every lane is present.
    marker = "\n\n## Memory recall"
    intro, sep, rest = base.partition(marker)
    rest = sep + rest
    extra = ""
    if lane_available("tasks", "focus"):
        extra += _TASK_GUIDANCE
    if lane_available("practices"):
        extra += _PRACTICE_GUIDANCE
    preamble = intro + extra + rest

    preamble = preamble.replace("{owner}", owner_name())
    parts = [preamble]

    parts.append(build_vault_context(vault))

    from sanitize import TRUST_BOUNDARY_INSTRUCTION

    parts.append(TRUST_BOUNDARY_INSTRUCTION)
    return "\n\n".join(parts)
