import plistlib, subprocess, sys
from pathlib import Path
import pytest
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".claude" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))
import install_units as iu
from test_no_personal_constants import DENYLIST

LAUNCHD = sorted((REPO_ROOT / ".claude" / "launchd").glob("*.plist"))
SYSTEMD = sorted(p for p in (REPO_ROOT / ".claude" / "systemd").iterdir() if p.suffix in (".service", ".timer"))

# hd-weather and reputation-audit are Cortex-only units (extract_core.EXCLUDE
# drops them from the shareable core) — present in the Cortex repo, absent
# from an extracted tree this same test file also ships into. Same
# skip-if-absent idiom as test_hd_weather_script_derives_repo_root below.
_CORTEX_ONLY_LAUNCHD_LABELS = {"com.cortexos.hd-weather", "com.cortexos.reputation-audit"}
_LAUNCHD_LABELS_PRESENT = {p.stem for p in LAUNCHD}
REPUTATION_AUDIT_PRESENT = "com.cortexos.reputation-audit" in _LAUNCHD_LABELS_PRESENT

def test_all_seven_launchd_templates_exist():
    base = {"com.cortexos.heartbeat", "com.cortexos.sync", "com.cortexos.indexer",
            "com.cortexos.reflection", "com.cortexos.hygiene"}
    # The base five ship everywhere; hd-weather/reputation-audit only on the
    # Cortex side. Nothing outside base+known-Cortex-only should ever appear.
    assert base <= _LAUNCHD_LABELS_PRESENT
    assert _LAUNCHD_LABELS_PRESENT <= (base | _CORTEX_ONLY_LAUNCHD_LABELS)

def test_all_seven_systemd_templates_exist():
    assert {p.name for p in SYSTEMD} == {"cortexos-chat.service", "cortexos-heartbeat.service", "cortexos-heartbeat.timer",
        "cortexos-reflection.service", "cortexos-reflection.timer", "cortexos-indexer.service", "cortexos-indexer.timer"}

@pytest.mark.parametrize("tmpl", LAUNCHD + SYSTEMD, ids=lambda p: p.name)
def test_templates_carry_no_personal_constants(tmpl):
    text = tmpl.read_text()
    for pattern in DENYLIST:
        assert not pattern.search(text), f"{tmpl.name} matches denylist pattern {pattern.pattern!r}"
    for literal in ("/Users/", "/home/"):
        assert literal not in text, f"{tmpl.name} contains {literal!r}"
    assert "{{REPO_ROOT}}" in text

@pytest.mark.parametrize("tmpl", LAUNCHD, ids=lambda p: p.name)
def test_rendered_plist_parses_and_resolves(tmpl, tmp_path):
    values = iu.machine_values("launchd", tmp_path / "repo")
    out = iu.render_template(tmpl.read_text(), values)
    assert "{{" not in out
    d = plistlib.loads(out.encode())
    assert d["Label"] == tmpl.stem
    assert d["WorkingDirectory"] == str(tmp_path / "repo")
    assert d["EnvironmentVariables"]["PATH"].endswith("/usr/bin:/bin")
    assert any(str(tmp_path / "repo") in a for a in d["ProgramArguments"])

def test_unknown_placeholder_is_an_error():
    with pytest.raises(iu.TemplateError):
        iu.render_template("<x>{{NOPE}}</x>", iu.machine_values("launchd", Path("/r")))

def test_systemd_render_resolves_user_and_paths(tmp_path):
    values = iu.machine_values("systemd", tmp_path / "repo", user="svc")
    for t in SYSTEMD:
        out = iu.render_template(t.read_text(), values)
        assert "{{" not in out
        if t.suffix == ".service":
            assert "User=svc" in out and f"WorkingDirectory={tmp_path / 'repo'}" in out

def test_install_launchd_writes_files_skips_disabled_and_removes_retired(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(iu.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    (tmp_path / "com.cortexos.reminder-6pm.plist").write_text("<plist/>")
    written = iu.install("launchd", repo_root=tmp_path / "repo", out_dir=tmp_path, dry_run=False)
    names = {p.name for p in tmp_path.glob("*.plist")}
    assert "com.cortexos.heartbeat.plist" in names
    assert "com.cortexos.reputation-audit.plist" not in names
    assert "com.cortexos.reminder-6pm.plist" not in names
    assert any("load" in c for c in calls)
    # reputation-audit is DISABLED_UNITS-skipped when its template exists
    # (Cortex); it doesn't ship at all in an extracted tree, so the expected
    # written count is "every template that exists, minus one if disabled".
    assert len(written) == len(LAUNCHD) - (1 if REPUTATION_AUDIT_PRESENT else 0)

def test_diff_reports_missing_then_same(tmp_path, monkeypatch):
    monkeypatch.setattr(iu, "launchd_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(iu.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
    report = iu.diff("launchd", repo_root=tmp_path / "repo")
    assert report["com.cortexos.heartbeat"] == "missing"
    if REPUTATION_AUDIT_PRESENT:
        assert report["com.cortexos.reputation-audit"] == "disabled"
    iu.install("launchd", repo_root=tmp_path / "repo", out_dir=tmp_path, dry_run=True)
    report = iu.diff("launchd", repo_root=tmp_path / "repo")
    assert report["com.cortexos.heartbeat"] == "same"

def test_hd_weather_script_derives_repo_root():
    path = REPO_ROOT / ".claude" / "scripts" / "hd_weather.sh"
    if not path.exists():
        pytest.skip("hd_weather.sh is Cortex-only (Human Design lane) and does not ship")
    text = path.read_text()
    assert "/Users/" not in text and 'dirname "$0"' in text

def test_path_keeps_unresolved_claude_dir(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    share_dir = tmp_path / "share"
    bin_dir.mkdir()
    share_dir.mkdir()
    real = share_dir / "real-claude"
    real.write_text("#!/bin/sh\n")
    link = bin_dir / "claude"
    link.symlink_to(real)
    monkeypatch.setattr(iu.shutil, "which", lambda name: str(link))
    values = iu.machine_values("launchd", tmp_path / "repo")
    assert values["PATH"].split(":")[0] == str(bin_dir)

def test_install_dry_run_does_not_delete_retired(tmp_path, monkeypatch):
    monkeypatch.setattr(iu.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
    retired_path = tmp_path / "com.cortexos.reminder-6pm.plist"
    retired_path.write_text("<plist/>")
    iu.install("launchd", repo_root=tmp_path / "repo", out_dir=tmp_path, dry_run=True)
    assert retired_path.exists()
    iu.install("launchd", repo_root=tmp_path / "repo", out_dir=tmp_path, dry_run=False)
    assert not retired_path.exists()

def test_diff_is_semantic_for_launchd(tmp_path, monkeypatch):
    monkeypatch.setattr(iu, "launchd_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(iu.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
    values = iu.machine_values("launchd", tmp_path / "repo")
    tmpl = next(p for p in LAUNCHD if p.stem == "com.cortexos.heartbeat")
    rendered = iu.render_template(tmpl.read_text(), values)
    d = plistlib.loads(rendered.encode())
    reordered = {}
    for key in reversed(list(d.keys())):
        reordered[key] = d[key]
    installed_path = tmp_path / "com.cortexos.heartbeat.plist"
    installed_path.write_bytes(plistlib.dumps(reordered))
    report = iu.diff("launchd", repo_root=tmp_path / "repo")
    assert report["com.cortexos.heartbeat"] == "same"

    d["StartInterval"] = 9999
    installed_path.write_bytes(plistlib.dumps(d))
    report = iu.diff("launchd", repo_root=tmp_path / "repo")
    assert report["com.cortexos.heartbeat"] == "differs"

def test_diff_is_semantic_for_systemd(tmp_path):
    values = iu.machine_values("systemd", tmp_path / "repo", user="svc")
    tmpl = next(p for p in SYSTEMD if p.name == "cortexos-heartbeat.timer")
    rendered = iu.render_template(tmpl.read_text(), values)

    with_extra = rendered + "\n\n# just a comment\n"
    assert iu._unit_lines(with_extra) == iu._unit_lines(rendered)

    changed = rendered.replace("Persistent=true", "Persistent=false")
    assert iu._unit_lines(changed) != iu._unit_lines(rendered)

def test_diff_reads_binary_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(iu, "launchd_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(iu.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
    values = iu.machine_values("launchd", tmp_path / "repo")
    tmpl = next(p for p in LAUNCHD if p.stem == "com.cortexos.heartbeat")
    rendered = iu.render_template(tmpl.read_text(), values)
    d = plistlib.loads(rendered.encode())
    installed_path = tmp_path / "com.cortexos.heartbeat.plist"
    with open(installed_path, "wb") as f:
        plistlib.dump(d, f, fmt=plistlib.FMT_BINARY)
    report = iu.diff("launchd", repo_root=tmp_path / "repo")
    assert report["com.cortexos.heartbeat"] == "same"

def test_unit_lines_drops_semicolon_comments():
    text = "Foo=bar\n; note\n  Baz=qux  \n"
    with_extra = "Foo=bar\n; note\nBaz=qux\n"
    assert iu._unit_lines(text) == iu._unit_lines(with_extra) == ["Foo=bar", "Baz=qux"]

def test_cli_render_user_flag_threads_through_to_rendered_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(iu, "REPO_ROOT", tmp_path / "repo")
    rc = iu.main(["render", "--platform", "systemd", "--out", str(tmp_path / "out"), "--user", "svc"])
    assert rc == 0
    service = next((tmp_path / "out").glob("*.service"))
    assert "User=svc" in service.read_text()

def test_install_dry_run_with_no_out_dir_never_touches_live_agents_dir(tmp_path, monkeypatch):
    live_dir = tmp_path / "live-agents"
    live_dir.mkdir()
    monkeypatch.setattr(iu, "launchd_agents_dir", lambda: live_dir)
    iu.install("launchd", repo_root=tmp_path / "repo", out_dir=None, dry_run=True)
    assert list(live_dir.iterdir()) == []
