"""SessionStart hook — inject vault context into the conversation.

Prints shared.build_vault_context() to stdout; Claude Code injects it. The
builder is shared with the chat server so the two can never drift apart.
"""

import json
import os
import sys


def main():
    hook_input = json.loads(sys.stdin.read())
    cwd = hook_input.get("cwd", "")
    if not cwd:
        sys.exit(0)

    # shared.py lives next to this hook's own tree, whatever directory the
    # session runs in; cwd is only used to find the vault.
    scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
    sys.path.insert(0, os.path.normpath(scripts_dir))
    from shared import build_vault_context

    print(build_vault_context(os.path.join(cwd, "vault")))


if __name__ == "__main__":
    main()
