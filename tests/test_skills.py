"""Every skill in .claude/skills/ is mechanically valid: it has a SKILL.md whose
frontmatter parses, whose name matches its folder, whose repo-relative paths
exist, and whose shipped Python at least compiles."""
import py_compile
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS = REPO_ROOT / ".claude" / "skills"
SKILL_DIRS = sorted(p for p in SKILLS.iterdir() if p.is_dir())

_FM = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Paths a skill body may name: repo-relative under .claude/ or vault/, or its own scripts/.
_PATH_RE = re.compile(r"(?<![\w/.-])((?:\.claude|vault|scripts|references|assets)/[\w./-]+)")

# Documentation examples and templates that are not real file paths
KNOWN_EXAMPLES = {
    ".claude/.env",  # aios-lead-pipeline: gitignored credentials
    "vault/research/YYYY-MM-DD-cortex-audit.md",  # cortex-audit: template output path
    "vault/research/YYYY-MM-DD-",  # research: incomplete template example
    "vault/research/2026-04-19-ai-agent-orchestration-frameworks.md",  # research: example report
    "references/mcp-config.json",  # mcp-client: user-created config template
    ".claude/skills/mcp-client/references/mcp-config.json",  # mcp-client: user config
    "assets/font.ttf",  # skill-creator: example asset
    "assets/frontend-template/",  # skill-creator: example asset
    "assets/hello-world/",  # skill-creator: example asset
    "assets/logo.png",  # skill-creator: example asset
    "assets/slides.pptx",  # skill-creator: example asset
    "references/api_docs.md",  # skill-creator: example reference
    "references/finance.md",  # skill-creator: example reference
    "references/mnda.md",  # skill-creator: example reference
    "references/policies.md",  # skill-creator: example reference
    "references/schema.md",  # skill-creator: example reference
    "scripts/rotate_pdf.py",  # skill-creator: example script
}


def _frontmatter(text: str) -> dict:
    m = _FM.match(text)
    assert m, "no YAML frontmatter block"
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


@pytest.mark.parametrize("skill", SKILL_DIRS, ids=lambda p: p.name)
def test_skill_md_exists_and_parses(skill):
    md = skill / "SKILL.md"
    assert md.exists(), f"{skill.name}: SKILL.md missing (case matters)"
    fm = _frontmatter(md.read_text(encoding="utf-8"))
    assert fm.get("name"), f"{skill.name}: frontmatter has no name"
    assert fm.get("description"), f"{skill.name}: frontmatter has no description"


@pytest.mark.parametrize("skill", SKILL_DIRS, ids=lambda p: p.name)
def test_declared_name_matches_folder(skill):
    fm = _frontmatter((skill / "SKILL.md").read_text(encoding="utf-8"))
    assert fm["name"] == skill.name, f"{skill.name}: declares name {fm['name']!r}"


@pytest.mark.parametrize("skill", SKILL_DIRS, ids=lambda p: p.name)
def test_referenced_paths_exist(skill):
    body = (skill / "SKILL.md").read_text(encoding="utf-8")
    missing = []
    for ref in sorted(set(_PATH_RE.findall(body))):
        ref = ref.rstrip(".,;:)")
        if "*" in ref or "{" in ref or "<" in ref:
            continue  # a glob or a placeholder, not a path
        if ref in KNOWN_EXAMPLES:
            continue  # documented example, not a real reference
        candidates = [REPO_ROOT / ref, skill / ref]
        if not any(c.exists() for c in candidates):
            missing.append(ref)
    assert not missing, f"{skill.name}: paths named but absent: {missing}"


@pytest.mark.parametrize("skill", SKILL_DIRS, ids=lambda p: p.name)
def test_shipped_python_compiles(skill):
    scripts = sorted((skill / "scripts").glob("*.py")) if (skill / "scripts").exists() else []
    for s in scripts:
        py_compile.compile(str(s), doraise=True)


def test_no_cargo_skills_in_project_scope():
    assert not [p.name for p in SKILL_DIRS if p.name.startswith("cargo")], "cargo skills live at user level only (D4)"


def test_lock_file_has_no_cargo_entries():
    import json
    path = REPO_ROOT / "skills-lock.json"
    if not path.exists():
        pytest.skip("skills-lock.json is Cortex-only and does not ship")
    lock = json.loads(path.read_text())
    assert not [k for k in lock["skills"] if k.startswith("cargo")]
