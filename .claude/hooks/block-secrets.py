#!/usr/bin/env python3
"""PreToolUse hook — block access to credentials, secrets, and dangerous commands.

Exit code 0 = allow, exit code 2 = block the tool call.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys


# --- Layer 1: Credential File Protection ---

BLOCKED_FILE_PATTERNS = [
    r'\.env$',
    r'\.env\.(?!example)',
    r'credentials\.json',
    r'credentials\.enc',
    r'client_secret\.json',
    r'token_cache\.json',
    r'encryption_key',
    r'google_token.*\.json',
    r'\.pem$',
    r'\.key$',
    r'id_rsa',
    r'id_ed25519',
    r'\.ssh/config',
    r'\.ssh/known_hosts',
    r'gws-[\w-]+',
    r'\.claude/data/auth/',
]

# --- Layer 3: Command Guardrails ---

BLOCKED_BASH_PATTERNS = [
    # Env exposure
    r'cat\s+.*\.env(?!\.example)\b',
    r'less\s+.*\.env(?!\.example)\b',
    r'more\s+.*\.env(?!\.example)\b',
    r'head\s+.*\.env(?!\.example)\b',
    r'tail\s+.*\.env(?!\.example)\b',
    r'\bprintenv\b',
    r'\becho\s+"?\$\{?\w*(KEY|TOKEN|SECRET|PASS|PASSWORD|CREDENTIAL)\w*',
    r'(?<!\.)\benv\b(?!\s*=)',     # bare 'env' command, not 'env VAR=x' or the tail of a dotfile like ".env.example"
    r'python.*os\.environ',
    r'export\s+.*TOKEN',
    r'curl.*[Aa]uthorization',
    r'curl.*Bearer',
    r'curl.*api[_-]?key=',
    # --- Credentials file by explicit path, whatever the verb -------------
    # Nine of eleven read-forms were only ever caught because the filename
    # ends in "env" and matched the bare-`env` rule above. That is an
    # accident of naming, not a rule. This is the rule.
    r'\.claude/\.env(?!\.example)\b',
    r'(^|[\s/])\.env(?!\.example)\b(?=.*\b(cat|less|more|head|tail|bat|type|sed|awk|grep|rg|xxd|od|hexdump|strings|cp|dd|source|python3?|node|ruby|perl|php)\b|\s*$)',
    # Globs that can expand to the credentials file
    # Also matches a quoted innocent path like ".claude/.gitignore" — accepted
    # over-block, one token wide.
    r'\.claude/\.\S*[\[\]?*\'"]\S*',
    r'(^|[\s/])\.(e\*|en\*|\?env|e\?\?|en\?|\[e\]nv)(\s|$)',
    # The process environment by another door
    r'/proc/[^/\s]+/environ',
    # find/locate for credentials
    r'\bfind\b[^|;&\n]*-i?name\s+[\'"]?\*?\.env\b',
    r'\blocate\b[^|;&\n]*\.env\b',
    # xargs re-reading a listed .claude directory (e.g. `ls .claude | xargs ... cat .claude/{}`)
    r'\bxargs\b[^|;&\n]*\bcat\b[^|;&\n]*\.claude/',
    # Symlinks to secrets
    r'\bln\b[^|;&\n]*-s[^|;&\n]*(\.env\b|credentials|token|\.pem\b|\.key\b|id_rsa|id_ed25519|\.ssh/)',
    # Process substitution reading secrets
    r'<\([^)]*(\.env\b|environ|credentials|token|id_rsa|id_ed25519)',
    # Hex/octal dumps and alternative viewers of secret files
    r'\b(xxd|od|hexdump|strings|bat|type|rg)\b[^|;&\n]*(\.env\b|credentials|\.pem\b|\.key\b|id_rsa|id_ed25519)',
    # Posting secrets outward
    r'\bcurl\b[^|;&\n]*(-d|--data|--data-binary|-F|--form|-T|--upload-file)\s*[\'"]?[^|;&\n]*(@[^\s]*\.env\b|\$\{?\w*(KEY|TOKEN|SECRET|PASS)\w*)',
    r'\bcurl\b[^|;&\n]*\?\S*\$\{?\w*(KEY|TOKEN|SECRET|PASS)\w*',
    r'\bwget\b[^|;&\n]*--post-(data|file)[^|;&\n]*(\.env\b|\$\{?\w*(KEY|TOKEN|SECRET))',
    # Other interpreters reading the environment
    r'\bphp\s+-r\b.*getenv',
    r'\b(python3?|perl|ruby)\b[^|;&\n]*<<[\'"]?\w+[\'"]?[\s\S]*?(os\.environ|%ENV|\bENV\b)',
    # Destructive commands
    r'rm\s+-rf\s+/',
    r'rm\s+-rf\s+~',
    r'rm\s+-rf\s+\.',
    # Deleting or emptying the vault or the system state, from any path shape.
    # The three patterns above only ever covered `rm -rf` against /, ~ and .,
    # so on 2026-08-17 the chat bot ran
    #     rm /path/to/cortexos/vault/drafts/active/*.md
    # and the hook allowed it. The owner had asked for that one, so nothing was
    # lost — but the bot runs unattended with permissions bypassed, and the
    # next such command need not be one he asked for. Deletion in the vault
    # goes through the draft lifecycle in Python, never the shell, so nothing
    # legitimate is caught here. Reading, appending (`>>`) and `mv` between
    # draft folders all stay allowed; see tests/test_block_secrets.py.
    #
    # `git rm` matches too, and that is deliberate (decided 2026-09-02, after
    # a subagent ran `git rm -r -q vault/daily-digest`). It is recoverable
    # from history, but the file leaves the working tree immediately and
    # auto-sync commits and pushes within two minutes, so from the vault's
    # point of view it is an rm with a paper trail. Recoverability is not
    # confirmation. The block IS the confirmation gate: a human runs it.
    #
    # Paths are matched as written AND after resolving relative arguments
    # against any `cd` earlier in the command and the tool's cwd — see
    # resolve_relative_paths(). `cd vault && git rm -r daily-digest` passed
    # this scan on 2026-09-02 because the text no longer said `vault/`.
    r'\brm\b[^|;&\n]*(vault|\.claude/data)/',
    # `>` truncates; `>>` appends and must stay allowed (the daily log is
    # append-only and the heartbeat writes it this way). Excluding `>` from the
    # run between the redirect and the path is what keeps `>>` out — a
    # lookbehind alone is not enough, since the class would otherwise swallow
    # the second `>` and match on the first.
    r'(?<!>)>[^|&;\n>]*(vault|\.claude/data)/\S+',
    # --- Write-mode access to the vault or system state from the shell -----
    # rm and truncating > were covered; every other verb that empties, moves
    # or rewrites a file was not. Deletion and rewrites go through Python in
    # the draft lifecycle, never the shell. mv INTO the vault, >> appends,
    # tee -a, cp FROM the vault, and reads all stay allowed.
    r'\bfind\b[^|;&\n]*(vault|\.claude/data)/?[^|;&\n]*-(delete|exec\s+(rm|shred|mv|truncate))\b',
    r'\bfind\b[^|;&\n]*-(delete|exec\s+(rm|shred|mv|truncate))\b[^|;&\n]*(vault|\.claude/data)/',
    r'\bmv\b(?![^|;&\n]*\s(vault|\.claude/data)/\S*\s*$)[^|;&\n]*\s(vault|\.claude/data)/\S+\s+(?!vault/|\.claude/data/)\S+',
    r'\btee\b(?!\s+-a)[^|;&\n]*\s(vault|\.claude/data)/',
    r'\b(sed|perl)\s+(-[a-zA-Z]*i|-pi|--in-place)[^|;&\n]*\s(vault|\.claude/data)/',
    r'\b(shred|truncate)\b[^|;&\n]*\s(vault|\.claude/data)/',
    r'\brsync\b[^|;&\n]*--delete[^|;&\n]*\s(vault|\.claude/data)/',
    r'\bcp\b[^|;&\n]*/dev/null\s+(vault|\.claude/data)/',
    r'(^|[;&|]\s*):\s*>\s*(vault|\.claude/data)/',
    # Python: the destructive call's OWN argument must be the protected path,
    # not merely a mention of "vault/" anywhere on the same line (e.g. inside
    # an unrelated print() string).
    r'\bopen\(\s*[\'"][^\'"\n]*(vault|\.claude/data)/[^\'"\n]*[\'"]\s*,\s*[\'"](w|w\+|a\+|r\+|wb)[\'"]',
    r'\b(os\.remove|os\.unlink|shutil\.rmtree)\(\s*[\'"][^\'"\n]*(vault|\.claude/data)/',
    r'\bPath\(\s*[\'"][^\'"\n]*(vault|\.claude/data)/[^\'"\n]*[\'"]\s*\)\s*\.(unlink|write_text|write_bytes)\(',
    # --- git: every way of landing on main ---------------------------------
    r'git\s+push\b[^|;&\n]*\s(-f|--force|--force-with-lease)\b',
    r'git\s+push\b[^|;&\n]*\s\+?(main|master)\b',
    r'git\s+push\b[^|;&\n]*:(refs/heads/)?(main|master)\b',
    r'git\s+push\s*($|[|;&])',
    r'git\s+push\s+(-u\s+)?origin\s*($|[|;&])',
    r'git\s+push\b[^|;&\n]*\sHEAD\s*($|[|;&])',
    r'git\s+reset\s+--hard',
    r'git\s+clean\s+-fd',
    # Discarding uncommitted vault edits. MEMORY.md is rewritten daily and the
    # sync window is two minutes; a checkout/restore/stash here throws away
    # work nothing else holds. list/show/pop/apply are reads or restores.
    r'git\s+(checkout|restore)\b[^|;&\n]*\s(--\s+)?[\'"]?(vault|\.claude/data)/',
    r'git\s+(checkout|restore)\b[^|;&\n]*\s(--\s+)?[\'"]?\.[\'"]?\s*($|[|;&])',
    r'git\s+stash\b(?!\s+(list|show|pop|apply|branch)\b)',
    r'curl.*api\.(stripe|paypal|braintree)',
    r'DROP\s+TABLE',
    r'DELETE\s+FROM',
    r'TRUNCATE\s+',
    # --- one-command environment dumps, more doors --------------------------
    r'\bnode\s+-[ep]\b[^|;&\n]*process\.env\b(?!\s*[.\[])',
    r'\b(export\s+-p|declare\s+-[xp]|typeset\s+-[xp])\b',
    r'(^|[;&|]\s*)set\s*($|[|;&>])',
    r'\bfrom\s+os\s+import\b[^\n]*\benviron\b',
    r'\bimport\s+os\s+as\s+\w+\b[^\n]*\.environ\b',
]


# --- Layer 4: Write/Edit content scan -------------------------------------
# The two-step attack: convince the agent to WRITE a script that dumps the
# environment now, and RUN it in a later turn where the bash scan sees only
# `python3 helper.py`. So the content of every Write and Edit is scanned.
#
# Deliberately narrow. Reading one named key — os.environ.get("X") — is how
# every Cortex module works and must stay writable. What is blocked is
# dumping the whole mapping, reading the credentials file, or shelling out
# to do either.
EXFILTRATION_CONTENT_PATTERNS = [
    (r'\b(print|repr|str|json\.dumps|pprint|pprint\.pprint)\(\s*(dict\()?\s*os\.environ\b', "dumps the whole mapping to output"),
    (r'\bfor\s+\w+(\s*,\s*\w+)?\s+in\s+os\.environ\b', "iterates the whole environment"),
    (r'\b(list|sorted|set|tuple)\(\s*os\.environ\b', "dumps the whole mapping"),
    (r'\bdict\(\s*os\.environ\s*\)', "dumps the whole mapping"),
    (r'\bos\.environ\.(items|keys|values)\(\)', "iterates the whole environment"),
    (r'\bprocess\.env\b(?!\s*[.\[])', "dumps the whole Node process environment object"),
    (r'\bdotenv_values\s*\(', "loads the whole credentials file as a dict"),
    (r'\bopen\s*\([^)\n]*\.claude/\.env(?!\.example)', "opens the credentials file"),
    (r'\.claude/\.env(?!\.example)[\'"]\s*\)\s*\.read(_text)?\s*\(', "reads the credentials file"),
    (r'\bPath\s*\([^)\n]*\.claude/\.env(?!\.example)[^)\n]*\)\s*\.read', "reads the credentials file"),
    (r'(^|\n)\s*(cat|less|more|head|tail|bat|xxd|od)\s+[^\n]*\.claude/\.env(?!\.example)', "shells out to read the credentials file"),
    (r'\bprintenv\b', "shells out to print the environment"),
    (r'\bcurl\b[^\n]*(-d|--data|-F)\s*[^\n]*(@-|@[^\s]*\.env\b|\$\{?\w*(KEY|TOKEN|SECRET))', "posts secrets outward"),
    (r'\brequests\.(post|put)\([^\n]*os\.environ\b', "posts the environment outward"),
    (r'\bpprint\.pp\(\s*(dict\()?\s*os\.environ\b', "dumps the whole environment mapping"),
    (r'\b(logging|log|logger)\.(debug|info|warning|error|critical|exception)\([^\n]*\bos\.environ\b(?!\s*(\.get\(|\[))', "logs the whole environment mapping"),
    (r'\b(json|yaml)\.(safe_)?dump\(\s*(dict\()?\s*os\.environ\b', "serialises the whole environment mapping to a file"),
]


def check_written_content(tool_name: str, tool_input: dict) -> str | None:
    """Scan the text a Write or Edit would put on disk.

    Only the NEW text is scanned: Edit's old_string is what is being removed,
    and blocking a cleanup because the thing being deleted was bad would be
    backwards.
    """
    if tool_name == "Write":
        text = tool_input.get("content") or ""
    elif tool_name == "Edit":
        text = tool_input.get("new_string") or ""
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        text = "\n".join(e.get("new_string", "") for e in edits if isinstance(e, dict))
    elif tool_name == "NotebookEdit":
        text = tool_input.get("new_source") or ""
    else:
        return None
    if not text:
        return None
    for pattern, reason in EXFILTRATION_CONTENT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            return f"Written content {reason} (matched '{pattern}')"
    return None


def check_file_access(tool_name: str, tool_input: dict) -> str | None:
    """Check if a file-access tool is targeting a sensitive path.

    Returns block reason string, or None if allowed.
    """
    if tool_name not in ("Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return None

    # Gather all path-like inputs
    paths_to_check = []
    for key in ("file_path", "path", "pattern", "notebook_path"):
        val = tool_input.get(key, "")
        if val:
            paths_to_check.append(val)
    # Grep glob parameter
    if tool_input.get("glob"):
        paths_to_check.append(tool_input["glob"])

    for path in paths_to_check:
        for pattern in BLOCKED_FILE_PATTERNS:
            if re.search(pattern, path, re.IGNORECASE):
                return f"Access to sensitive file matching '{pattern}'"

    return None


_SUBSHELL_RE = re.compile(r'\$\(((?:[^()]|\([^()]*\))*)\)|`([^`]*)`|<\(((?:[^()]|\([^()]*\))*)\)')
_MAX_SUBSHELL_DEPTH = 5


def _scan_one(cmd: str) -> str | None:
    """Run every pattern over a single command string. No recursion."""
    for pattern in BLOCKED_FILE_PATTERNS:
        if re.search(r'(cat|less|more|head|tail|vim|nano|code)\s+.*' + pattern, cmd, re.IGNORECASE):
            return f"Bash command would read sensitive file matching '{pattern}'"
    for pattern in BLOCKED_BASH_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return f"Blocked dangerous command matching '{pattern}'"
    return None


# --- Resolving relative paths against `cd` and the tool's cwd ---------------
# Every vault pattern above matches the literal text `vault/` or
# `.claude/data/`. On 2026-09-02 a subagent ran `git rm -r -q vault/daily-digest`
# (blocked), then `cd vault && git rm -r -q daily-digest` (allowed): same
# deletion, the word `vault/` just moved into an earlier segment. So the
# command is also scanned in a second, resolved form: a virtual cwd is
# tracked through every `cd`/`pushd` in the string (seeded from the hook
# payload's cwd), and each relative argument in the segments after it is
# rewritten to its path from the project root. `cd vault && rm daily-digest`
# becomes `rm vault/daily-digest`, which the existing patterns already catch.
#
# The virtual cwd is kept RELATIVE to the project root ("" = root, "vault" =
# inside the vault). None means "somewhere we cannot place" (home, `cd -`,
# `cd $X`, outside the repo): no rewriting happens there, and the raw scan
# still runs, so an unknown cwd never weakens what was caught before.

_PROTECTED_DIR_RE = re.compile(r'(?:^|/)((?:vault|\.claude/data)(?:/.*)?)$')
_SEGMENT_SPLIT_RE = re.compile(r'\s*(?:&&|\|\||;|\||\n)\s*')
_HEREDOC_RE = re.compile(r'<<-?\s*[\'"]?(\w+)[\'"]?')
# Wrappers whose second word is a subcommand or program name, not a path.
_KEEP_SECOND_TOKEN = {"git", "sudo", "command", "nohup", "xargs", "time", "exec", "builtin", "env", "nice", "doas"}


def _relative_to_root(path: str, project_root: str | None) -> str | None:
    """Absolute path -> path relative to the project root, or None if outside.

    Without a known root, fall back to the protected-dir suffix: a path ending
    in `.../vault/daily` is inside the vault whatever the repo is called.
    """
    path = os.path.normpath(path)
    if project_root:
        root = os.path.normpath(project_root)
        if path == root:
            return ""
        if path.startswith(root + "/"):
            return path[len(root) + 1:]
        return None
    m = _PROTECTED_DIR_RE.search(path)
    return m.group(1) if m else None


def _initial_cwd(cwd: str | None, project_root: str | None) -> str | None:
    """Seed the virtual cwd from the hook payload. Unknown -> root."""
    if not cwd:
        return ""
    if not cwd.startswith("/"):
        return ""
    rel = _relative_to_root(cwd, project_root)
    return rel


def _resolve_cd(cwd: str | None, target: str | None, project_root: str | None) -> str | None:
    """Where does `cd <target>` land, relative to the project root?"""
    if target is None:
        return None                      # bare `cd` -> home
    target = target.strip("'\"")
    if not target or target == "-" or target.startswith("~") or "$" in target or "`" in target:
        return None
    if target.startswith("/"):
        return _relative_to_root(target, project_root)
    if cwd is None:
        return None
    new = os.path.normpath(os.path.join(cwd, target))
    if new == "." :
        return ""
    if new == ".." or new.startswith("../"):
        return None
    return new


def _rewrite_token(tok: str, cwd: str) -> str:
    """Rewrite one argument to its root-relative path, or leave it alone."""
    if not tok or tok.startswith("-") or "=" in tok or "$" in tok or "`" in tok:
        return tok
    # Split a redirect prefix such as `>file`, `2>>file`, `<file` off the path.
    m = re.match(r'^([0-9&]*[<>]+&?)(.*)$', tok)
    prefix, body = (m.group(1), m.group(2)) if m else ("", tok)
    body = body.strip("'\"")
    if not body or body.startswith("/") or body.startswith("~"):
        return tok
    new = os.path.normpath(os.path.join(cwd, body))
    if new == ".." or new.startswith("../"):
        return tok
    return prefix + new


def _strip_heredoc_bodies(cmd: str) -> str:
    """Drop heredoc bodies: their lines are data, not commands with paths."""
    out: list[str] = []
    terminator: str | None = None
    for line in cmd.split("\n"):
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        out.append(line)
        m = _HEREDOC_RE.search(line)
        if m:
            terminator = m.group(1)
    return "\n".join(out)


def resolve_relative_paths(cmd: str, cwd: str | None = None, project_root: str | None = None) -> str:
    """Return the command with relative paths rewritten from the project root.

    `cwd` is the absolute directory the shell starts in (the hook payload's
    `cwd`); `project_root` is `$CLAUDE_PROJECT_DIR`. Segments run from a cwd
    we cannot place are returned unchanged.
    """
    vcwd = _initial_cwd(cwd, project_root)
    pieces: list[str] = []
    for seg in _SEGMENT_SPLIT_RE.split(_strip_heredoc_bodies(cmd)):
        tokens = seg.split()
        if not tokens:
            continue
        head = tokens[0].lstrip("({").strip()
        if head in ("cd", "pushd"):
            target = tokens[1].rstrip(")}") if len(tokens) > 1 else None
            vcwd = _resolve_cd(vcwd, target, project_root)
            pieces.append(seg)
            continue
        if not vcwd:                     # None (unplaceable) or "" (root): nothing to add
            pieces.append(seg)
            continue
        keep = 2 if head in _KEEP_SECOND_TOKEN else 1
        rewritten = [tokens[0]] + [
            t if i < keep else _rewrite_token(t.rstrip(")}"), vcwd)
            for i, t in enumerate(tokens[1:], start=1)
        ]
        pieces.append(" ".join(rewritten))
    return " ; ".join(pieces)


def check_bash_command(tool_input: dict, cwd: str | None = None, project_root: str | None = None) -> str | None:
    """Check if a Bash command would expose secrets or is destructive.

    Subshell contents — $(...), backticks, <(...) — are extracted and scanned
    with the same patterns, recursively. A banned command wrapped in an
    innocent-looking echo is the first bypass every naive matcher misses.

    The command is then scanned a second time with relative paths resolved
    against `cd` and the tool's cwd (see resolve_relative_paths), so a vault
    path is caught wherever the shell happens to be standing.

    Returns block reason string, or None if allowed.
    """
    cmd = tool_input.get("command", "")
    if not cmd:
        return None

    # Check subshell contents first: patterns are substring matches with no
    # boundary awareness of $(...) / `...` / <(...), so a raw dangerous
    # command sitting inside a subshell also matches the un-extracted full
    # string. Scanning subshells first lets us tag those hits "(inside
    # subshell)" before the top-level fallback below reports them untagged.
    pending = [cmd]
    for _ in range(_MAX_SUBSHELL_DEPTH):
        inner: list[str] = []
        for text in pending:
            for m in _SUBSHELL_RE.finditer(text):
                body = next(g for g in m.groups() if g is not None)
                if body.strip():
                    inner.append(body)
        if not inner:
            break
        for body in inner:
            reason = _scan_one(body)
            if reason:
                return f"{reason} (inside subshell)"
        pending = inner

    reason = _scan_one(cmd)
    if reason:
        return reason

    resolved = resolve_relative_paths(cmd, cwd, project_root)
    if resolved != cmd:
        reason = _scan_one(resolved)
        if reason:
            return f"{reason} (relative path resolved against cd/cwd)"
    return None


_REDACT_RE = re.compile(
    r'(\$\{?\w*(KEY|TOKEN|SECRET|PASS|PASSWORD|CREDENTIAL)\w*\}?'
    r'|\S*\.env(?!\.example)\b\S*'
    r'|\S*(credentials|id_rsa|id_ed25519)\S*'
    r'|(Bearer|Authorization:|api[_-]?key=|token=|secret=|password=)\s*\S+'
    r'|\b(sk|ghp|gho|xox[abp]|AIza|or)[-_][\w-]{8,}\b)',
    re.IGNORECASE,
)


def _audit(data: dict, tool_name: str, reason: str, tool_input: dict) -> None:
    """Append one line to .claude/data/hook-blocks.jsonl. Never raises.

    You cannot tune a defense you cannot see firing. The command is redacted
    and truncated — the log must never itself become the leak.
    """
    try:
        root = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
        if not root:
            return
        log_dir = os.path.join(root, ".claude", "data")
        if not os.path.isdir(log_dir):
            return
        raw = tool_input.get("command") or tool_input.get("file_path") or ""
        redacted = _REDACT_RE.sub("«redacted»", str(raw))[:200]
        row = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "tool": tool_name,
            "reason": reason[:300],
            "command": redacted,
        }
        with open(os.path.join(log_dir, "hook-blocks.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def main():
    data = json.loads(sys.stdin.read())
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    reason = check_file_access(tool_name, tool_input)
    if not reason and tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        reason = check_written_content(tool_name, tool_input)
    if not reason and tool_name == "Bash":
        reason = check_bash_command(
            tool_input,
            cwd=data.get("cwd"),
            project_root=os.environ.get("CLAUDE_PROJECT_DIR"),
        )

    if reason:
        _audit(data, tool_name, reason, tool_input)
        print(f"BLOCKED: {reason}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
