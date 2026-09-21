"""No function may use a name that is defined nowhere.

Python only finds this when the line runs. A NameError inside a try/except
can be swallowed silently and a background lane stops without a word. No
linter is installed on either machine, so this is the linter.

Rough on purpose: a name counts as defined if it is bound ANYWHERE in the
enclosing top-level function (or at module level, or is a builtin). That misses
use-before-assignment but never cries wolf on closures.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FOLDERS = (".claude/scripts", ".claude/scripts/integrations",
           ".claude/chat", ".claude/chat/adapters", ".claude/hooks")
FILES = sorted(p for d in FOLDERS if (REPO_ROOT / d).is_dir() for p in (REPO_ROOT / d).glob("*.py"))


def _bound(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, ast.MatchAs) and n.name:
            out.add(n.name)
    return out


def undefined_names(source: str) -> list[str]:
    tree = ast.parse(source)
    module = _bound(tree) | set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    tops = list(tree.body)
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        tops.extend(cls.body)
    found = []
    for func in tops:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope = module | _bound(func)
        for n in ast.walk(func):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in scope:
                found.append(f"line {n.lineno}: {n.id!r} in {func.name}()")
    return found


def test_the_check_catches_the_mistake_it_exists_for():
    assert undefined_names("def f():\n    return g(account)\n\ndef g(a):\n    return a\n") == [
        "line 2: 'account' in f()"]


def test_the_check_is_quiet_about_closures():
    assert undefined_names("def f():\n    x = 1\n    def inner():\n        return x\n    return inner\n") == []


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_undefined_names(path):
    assert undefined_names(path.read_text()) == []
