"""Background memory flush — analyze transcript and write daily log entries.

Spawned by PreCompact/SessionEnd hooks. Uses Claude Agent SDK with pure
reasoning (no tools) to extract decisions, lessons, and facts from the
conversation transcript.

Dedup model:
  - Session-keyed (cheap, pre-LLM): same session_id flushing twice within
    SESSION_DEDUP_SECONDS gets short-circuited before the LLM runs.
  - Content-keyed (post-LLM): if a different concurrent session produced
    the same extracted content within CONTENT_DEDUP_SECONDS, skip writing
    the duplicate. Catches the "5 sessions all end at 13:08" case from
    the memory audit (Finding #3).
  - State pruning: entries older than 7 days are dropped on every run.

Usage: .venv/bin/python3 .claude/scripts/memory_flush.py <temp_transcript_path> <session_id>
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from shared import owner_name

# Resolve project root (this script lives at .claude/scripts/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / ".claude" / "data"
FLUSH_STATE_PATH = DATA_DIR / "flush_state.json"
ERROR_LOG_PATH = DATA_DIR / "flush_errors.log"
DAILY_DIR = PROJECT_ROOT / "vault" / "daily"

SESSION_DEDUP_SECONDS = 60     # same session won't flush twice within 60s
CONTENT_DEDUP_SECONDS = 600    # same content (across sessions) won't write twice within 10min
PRUNE_AFTER_DAYS = 7           # entries older than this dropped on every run
MAX_MESSAGES = 100

SYSTEM_PROMPT_TEMPLATE = (
    "You are a memory curator for {owner}'s AI second brain. "
    "Given a conversation transcript, extract ONLY decisions made, lessons learned, "
    "facts worth remembering, and important context. "
    "Output as markdown bullet points. Each bullet should be self-contained and "
    "understandable without the original conversation. "
    "If nothing important was discussed, output exactly 'FLUSH_OK'. "
    "Do not include greetings, small talk, or routine actions."
)


def system_prompt() -> str:
    """Rendered at call time: OWNER_NAME is only in the env after load_env()."""
    return SYSTEM_PROMPT_TEMPLATE.replace("{owner}", owner_name())


DEBUG_LOG = "/tmp/cortex_hook_debug.log"


def debug(msg: str):
    with open(DEBUG_LOG, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] memory_flush: {msg}\n")


def log_error(msg: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ERROR_LOG_PATH, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def _load_state() -> dict:
    """Load state. Auto-migrates old flat {session_id: ts} format."""
    if not FLUSH_STATE_PATH.exists():
        return {"sessions": {}, "content_hashes": {}}
    try:
        raw = json.load(open(FLUSH_STATE_PATH))
    except (json.JSONDecodeError, OSError):
        return {"sessions": {}, "content_hashes": {}}
    # Migrate old format: flat dict of {session_id: timestamp}
    if "sessions" not in raw and "content_hashes" not in raw:
        return {"sessions": dict(raw), "content_hashes": {}}
    return {
        "sessions": raw.get("sessions", {}),
        "content_hashes": raw.get("content_hashes", {}),
    }


def _save_state(state: dict):
    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
    from shared import save_state
    save_state(str(FLUSH_STATE_PATH), state)


def _prune_state(state: dict) -> dict:
    """Drop session and content-hash entries older than PRUNE_AFTER_DAYS."""
    cutoff = datetime.now().timestamp() - (PRUNE_AFTER_DAYS * 86400)
    state["sessions"] = {k: v for k, v in state["sessions"].items() if v >= cutoff}
    state["content_hashes"] = {k: v for k, v in state["content_hashes"].items() if v >= cutoff}
    return state


def check_session_dedup(session_id: str) -> bool:
    """Return True if this session was flushed less than SESSION_DEDUP_SECONDS ago."""
    state = _load_state()
    last_flush = state["sessions"].get(session_id, 0)
    return (datetime.now().timestamp() - last_flush) < SESSION_DEDUP_SECONDS


def check_content_dedup(content_hash: str) -> bool:
    """Return True if this exact content was written less than CONTENT_DEDUP_SECONDS ago."""
    state = _load_state()
    last_write = state["content_hashes"].get(content_hash, 0)
    return (datetime.now().timestamp() - last_write) < CONTENT_DEDUP_SECONDS


def update_dedup(session_id: str, content_hash: str | None = None):
    """Mark session + (if content provided) content as flushed now. Prunes old entries."""
    state = _load_state()
    state = _prune_state(state)
    now = datetime.now().timestamp()
    state["sessions"][session_id] = now
    if content_hash:
        state["content_hashes"][content_hash] = now
    _save_state(state)


def hash_content(text: str) -> str:
    """SHA-256 of normalized content for dedup."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def parse_transcript(transcript_path: str) -> str:
    """Parse JSONL transcript into readable text. Cap at last MAX_MESSAGES."""
    messages = []
    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Claude Code transcripts nest under a "message" key
            msg = obj.get("message", obj)
            role = msg.get("role", "") or obj.get("type", "")
            if role not in ("user", "assistant"):
                continue

            content = msg.get("content", "")
            if isinstance(content, list):
                # Extract text blocks from content array
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)

            if not content or not content.strip():
                continue

            messages.append(f"{role.upper()}: {content.strip()}")

    # Take last MAX_MESSAGES
    return "\n\n".join(messages[-MAX_MESSAGES:])


# Flush output is appended VERBATIM to vault/daily/, which is append-only and
# feeds reflection into MEMORY.md. Anything that leaks through here is durable.
# Single-shot, no-tool call — same shape as capture, so it belongs on OpenRouter
# rather than dragging Claude Code's tooling in. SDK stays as the fallback.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_FLUSH_MODEL", "moonshotai/kimi-k3")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_TIMEOUT = 120

_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")

NOTHING_TO_WRITE = "FLUSH_OK"


def _strip_fences(text: str) -> str:
    """Drop a wrapping ``` block, keeping its contents."""
    lines = [ln for ln in text.strip().splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines).strip()


def normalize_flush_output(raw: str) -> str:
    """Reduce a model reply to bullets, or the nothing-to-write sentinel.

    The guard between a chatty model and the permanent record. Three failure
    modes it exists to stop, all of which write real text into the daily log:
    a decorated sentinel ("FLUSH_OK - nothing notable") logged as content, a
    preamble ("Here's what I extracted:") logged above the bullets, and a
    prose paragraph with no bullets logged wholesale.

    Anything that is not clearly a bullet list is treated as nothing to write.
    Losing one flush is recoverable; a polluted daily log is not, because
    reflection promotes from it and the file is append-only.
    """
    text = _strip_fences(raw or "")
    if not text:
        return NOTHING_TO_WRITE
    if text.lower().lstrip().startswith(NOTHING_TO_WRITE.lower()):
        return NOTHING_TO_WRITE

    lines = text.splitlines()
    first = next((i for i, ln in enumerate(lines) if _BULLET_RE.match(ln)), None)
    if first is None:
        return NOTHING_TO_WRITE

    # Trailing sign-offs ("Let me know if…") sit after the last bullet, so cut
    # back to it. An indented line is a bullet's continuation, not prose.
    last = max(
        i for i, ln in enumerate(lines)
        if _BULLET_RE.match(ln) or (ln.strip() and ln[:1].isspace())
    )
    return "\n".join(lines[first:last + 1]).strip()


def _flush_via_openrouter(transcript_text: str) -> tuple[str, str]:
    """Returns (normalized_output, error). Empty output is an error, not a
    sentinel: a provider returning "" must fall through to the SDK rather than
    look like 'nothing worth keeping' and silently drop a day of memory."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return "", "no OPENROUTER_API_KEY"
    try:
        from openai import OpenAI

        client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key, timeout=OPENROUTER_TIMEOUT)
        resp = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[
                {"role": "system", "content": system_prompt()},
                {"role": "user", "content": f"Here is the conversation transcript:\n\n{transcript_text}"},
            ],
            max_tokens=1500,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        return "", f"OpenRouter call failed: {str(exc)[:160]}"

    if not raw:
        return "", "OpenRouter returned an empty reply"
    return normalize_flush_output(raw), ""


async def _write_flush(result: str, session_id: str) -> None:
    """Append normalized flush output to today's daily log.

    Shared by the OpenRouter and SDK paths so dedup and locking cannot drift
    between them — the daily log is append-only, so a divergence here is
    permanent.
    """
    # Content-keyed dedup: catches concurrent sessions producing identical
    # extracted output (the "5 sessions all end at 13:08" failure mode).
    content_hash = hash_content(result)
    if check_content_dedup(content_hash):
        debug(f"content dedup skip — same flush content was written within last {CONTENT_DEDUP_SECONDS}s")
        update_dedup(session_id, content_hash)
        return

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    daily_path = DAILY_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.md"
    timestamp = datetime.now().strftime("%H:%M")
    entry = f"\n## {timestamp} — Memory Flush\n\n{result}\n"

    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
    from shared import file_lock

    with file_lock(str(daily_path)):
        with open(daily_path, "a") as f:
            f.write(entry)

    update_dedup(session_id, content_hash)


async def flush(transcript_path: str, session_id: str):
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    transcript_text = parse_transcript(transcript_path)
    if not transcript_text.strip():
        return

    prompt = f"Here is the conversation transcript:\n\n{transcript_text}"

    os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

    # OpenRouter first; the SDK below is the fallback so a missing key or a
    # provider outage degrades to the old path rather than losing the flush.
    or_text, or_error = _flush_via_openrouter(transcript_text)
    if or_text == NOTHING_TO_WRITE:
        # A successful call that found nothing. Not a failure, so do NOT fall
        # through to the SDK — that would pay twice to be told the same thing,
        # and writing the sentinel itself is the leak the guard exists to stop.
        debug("FLUSH_OK — nothing to write")
        update_dedup(session_id)
        return
    if or_text:
        await _write_flush(or_text, session_id)
        return
    debug(f"OpenRouter flush unavailable ({or_error}) — falling back to the SDK")

    from sdk_stderr import make_stderr_capture
    import sdk_cli
    sdk_stderr_cb, get_sdk_stderr_tail = make_stderr_capture(context="memory_flush")

    response_text = []
    async for msg in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            system_prompt=system_prompt(),
            allowed_tools=[],
            max_turns=1,
            stderr=sdk_stderr_cb,
            **sdk_cli.cli_kwargs(),
        ),
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    response_text.append(block.text)
        elif isinstance(msg, ResultMessage):
            if msg.is_error:
                tail = " | ".join(get_sdk_stderr_tail()[-5:]) or "(no stderr)"
                import agent_result

                reason, is_auth = agent_result.describe_failure(
                    msg, "\n".join(response_text), tail
                )
                log_error(f"Agent SDK error: {reason}")
                if is_auth:
                    agent_result.alert_auth_failure(f"memory flush: {reason}")
                return

    result = normalize_flush_output("\n".join(response_text))
    debug(f"SDK response (normalized): {repr(result[:200])}")
    if result == NOTHING_TO_WRITE:
        debug("FLUSH_OK — nothing to write")
        update_dedup(session_id)  # mark session done so it won't re-flush
        return

    await _write_flush(result, session_id)


def main():
    debug(f"started argv={sys.argv}")

    # Hooks spawn this detached with a bare environment, so the Agent SDK's
    # CLI auth token has to come from .claude/.env (same as heartbeat.py).
    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "scripts"))
    from integrations.registry import load_env

    load_env()

    if len(sys.argv) < 3:
        log_error("Usage: memory_flush.py <transcript_path> <session_id>")
        sys.exit(1)

    transcript_path = sys.argv[1]
    session_id = sys.argv[2]

    if not os.path.exists(transcript_path):
        debug(f"transcript not found: {transcript_path}")
        log_error(f"Transcript not found: {transcript_path}")
        sys.exit(1)

    if check_session_dedup(session_id):
        debug(f"dedup skip — session {session_id} flushed within last {SESSION_DEDUP_SECONDS}s")
        return

    # Debug: print parsed transcript
    try:
        parsed = parse_transcript(transcript_path)
        debug(f"parsed transcript ({len(parsed)} chars): {repr(parsed[:300])}")
    except Exception as e:
        debug(f"parse error: {e}")

    debug(f"calling SDK for session {session_id}")
    try:
        asyncio.run(flush(transcript_path, session_id))
        debug("flush complete")
    except Exception:
        tb = traceback.format_exc()
        debug(f"flush failed: {tb}")
        log_error(f"Flush failed:\n{tb}")
    finally:
        # Clean up temp transcript
        try:
            os.unlink(transcript_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
