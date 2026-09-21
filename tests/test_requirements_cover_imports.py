"""
Test that all third-party imports in chat adapters and integrations are declared in requirements.txt
"""

import re
from pathlib import Path


# Mapping of Python import name to pip package name
IMPORT_TO_PACKAGE = {
    "slack_bolt": "slack-bolt",
    "slack_sdk": "slack-sdk",
    "telegram": "python-telegram-bot",
    "requests": "requests",
    "pyyaml": "PyYAML",
    "yaml": "PyYAML",
    "fastembed": "fastembed",
    "sqlite_vec": "sqlite-vec",
    "dotenv": "python-dotenv",
    "composio": "composio",
    "openai": "openai",
}

# Stdlib modules to exclude
STDLIB_MODULES = {
    "os", "sys", "json", "time", "asyncio", "collections", "logging",
    "re", "typing", "pathlib", "datetime", "urllib", "fcntl", "signal",
    "shutil", "sqlite3", "subprocess", "zoneinfo", "threading", "dataclasses",
    "abc", "contextlib", "functools", "itertools", "operator", "__future__",
}

# Local/project modules to exclude
LOCAL_MODULES = {
    "_flush_common", "integrations", "sanitize", "shared", "captures",
    "registry", "base",
}


def extract_imports_from_file(filepath):
    """Extract imports from a Python file, skipping docstrings."""
    file_imports = set()
    content = filepath.read_text()

    # Skip lines in docstrings
    in_docstring = False
    docstring_char = None

    for line in content.split("\n"):
        # Check for docstring markers (simplified)
        for triple in ('"""', "'''"):
            if triple in line:
                if in_docstring and docstring_char == triple:
                    in_docstring = False
                elif not in_docstring:
                    in_docstring = True
                    docstring_char = triple

        if in_docstring:
            continue

        line = line.strip()
        # Skip comment-only lines and empty lines
        if not line or line.startswith("#"):
            continue

        if line.startswith("import ") or line.startswith("from "):
            match = re.match(r"(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_.]*)", line)
            if match:
                module = match.group(1).split(".")[0]
                if module not in STDLIB_MODULES and module not in LOCAL_MODULES:
                    file_imports.add(module)

    return file_imports


def get_third_party_imports():
    """Extract third-party imports from chat adapters and integrations."""
    imports = set()

    # Check chat adapters
    adapter_dir = Path(".claude/chat/adapters")
    if adapter_dir.exists():
        for py_file in adapter_dir.glob("*.py"):
            imports.update(extract_imports_from_file(py_file))

    # Check integrations
    integrations_dir = Path(".claude/scripts/integrations")
    if integrations_dir.exists():
        for py_file in integrations_dir.glob("*.py"):
            imports.update(extract_imports_from_file(py_file))

    return imports


def read_requirements_file(filepath):
    """Parse a requirements.txt file and extract package names."""
    packages = set()
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                # Skip comments and empty lines
                if not line or line.startswith("#"):
                    continue
                # Extract package name (before ==, >=, <=, etc.)
                match = re.match(r"([a-zA-Z0-9_-]+)", line)
                if match:
                    packages.add(match.group(1).lower())
    except FileNotFoundError:
        pass
    return packages


def test_chat_and_integrations_imports_in_requirements():
    """Verify all third-party imports in chat adapters and integrations are in requirements.txt"""
    imports = get_third_party_imports()

    if not imports:
        # No third-party imports found, test passes
        return

    # Read requirements.txt
    req_packages = read_requirements_file("requirements.txt")

    # Map imports to package names
    missing_packages = []
    for imp in sorted(imports):
        package_name = IMPORT_TO_PACKAGE.get(imp, imp)
        # Normalize for comparison
        normalized_package = package_name.lower().replace("_", "-")

        if normalized_package not in req_packages:
            missing_packages.append((imp, package_name))

    assert not missing_packages, (
        f"The following third-party imports are used but not declared in requirements.txt:\n"
        + "\n".join(f"  {imp} → {pkg}" for imp, pkg in missing_packages)
    )


def test_requirements_file_exists():
    """Verify requirements.txt exists at the repo root."""
    assert Path("requirements.txt").exists(), "requirements.txt does not exist at repo root"


def test_requirements_has_slack_bolt():
    """Verify slack_bolt is declared in requirements.txt (needed by slack_adapter)"""
    req_packages = read_requirements_file("requirements.txt")
    assert "slack-bolt" in req_packages or "slack_bolt" in req_packages, (
        "slack_bolt (pip: slack-bolt) is imported in .claude/chat/adapters/slack_adapter.py "
        "but not declared in requirements.txt"
    )


def test_claude_agent_sdk_pinned():
    """Verify claude-agent-sdk is pinned to 0.1.56 in requirements.txt."""
    filepath = "requirements.txt"
    if not Path(filepath).exists():
        return

    with open(filepath) as f:
        content = f.read()
        match = re.search(r"claude-agent-sdk==([0-9.]+)", content)
        if match:
            version = match.group(1)
            assert version == "0.1.56", (
                f"claude-agent-sdk in {filepath} is pinned to {version}, "
                "but should be 0.1.56 (0.2.x+ changes the options API)"
            )
