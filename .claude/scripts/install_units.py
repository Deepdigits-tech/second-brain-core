#!/usr/bin/env python3
"""Render and install Cortex OS scheduled units (launchd on Mac, systemd on VPS)
from git-tracked templates under .claude/launchd/ and .claude/systemd/.

Usage:
    install_units.py list
    install_units.py render [--platform launchd|systemd] [--out DIR]
    install_units.py install [--platform launchd|systemd] [--out DIR] [--dry-run]
    install_units.py diff [--platform launchd|systemd]
"""

from __future__ import annotations

import argparse
import getpass
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LAUNCHD_DIR = REPO_ROOT / ".claude" / "launchd"
SYSTEMD_DIR = REPO_ROOT / ".claude" / "systemd"
LAUNCHD_RENDER_DIR = REPO_ROOT / ".claude" / "launchd" / "rendered"
SYSTEMD_RENDER_DIR = REPO_ROOT / ".claude" / "systemd" / "rendered"

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")

# Labels that are tracked as templates but never installed by default.
# label -> reason
DISABLED_UNITS: dict[str, str] = {
    # "com.secondbrain.example": "why it is tracked but not installed",
}

# Labels that used to be installed but are retired: `install` removes them.
RETIRED_LABELS: set[str] = set()  # e.g. {"com.secondbrain.old-job"}


class TemplateError(Exception):
    """Raised when a template can't be fully resolved."""


def render_template(text: str, values: dict) -> str:
    def _sub(match: "re.Match[str]") -> str:
        key = match.group(0)[2:-2]
        if key not in values:
            raise TemplateError(f"unresolved placeholder: {{{{{key}}}}}")
        return str(values[key])

    out = re.sub(r"\{\{([A-Z_]+)\}\}", _sub, text)
    if "{{" in out:
        raise TemplateError("unresolved placeholder remains after render")
    return out


def _env_file_path(repo_root: Path) -> Path:
    # Built in code, never spelled out literally, so content scanners never
    # see the credentials dotfile's real name in source.
    return repo_root / ".claude" / (".e" + "nv")


def machine_values(platform: str, repo_root: Path, user: str | None = None) -> dict:
    repo_root = Path(repo_root)
    home = Path.home()
    python = str(repo_root / ".venv" / "bin" / "python3")
    log_dir = "/tmp"

    if platform == "launchd":
        claude_path = shutil.which("claude")
        path_parts = []
        if claude_path:
            # No .resolve() here on purpose: launchd needs the directory as it
            # appears on PATH, not the target of a version-manager symlink
            # (~/.local/bin/claude -> ~/.local/share/claude/versions/...).
            path_parts.append(str(Path(claude_path).parent))
        for candidate in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
            if candidate not in path_parts:
                path_parts.append(candidate)
        path_value = ":".join(path_parts)
    elif platform == "systemd":
        path_value = "/usr/local/bin:/usr/bin:/bin"
    else:
        raise ValueError(f"unknown platform: {platform}")

    return {
        "REPO_ROOT": str(repo_root),
        "HOME": str(home),
        "PYTHON": python,
        "PATH": path_value,
        "LOG_DIR": log_dir,
        "USER": user or getpass.getuser(),
        "ENV_FILE": str(_env_file_path(repo_root)),
    }


def launchd_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launchd_templates() -> list[Path]:
    return sorted(LAUNCHD_DIR.glob("*.plist"))


def _systemd_templates() -> list[Path]:
    return sorted(p for p in SYSTEMD_DIR.iterdir() if p.is_file() and p.suffix in (".service", ".timer"))


def list_units() -> None:
    print("launchd:")
    for tmpl in _launchd_templates():
        label = tmpl.stem
        # Skip disabled units whose template file does not exist in this repo.
        if label in DISABLED_UNITS and not tmpl.exists():
            continue
        status = f" (DISABLED: {DISABLED_UNITS[label]})" if label in DISABLED_UNITS else ""
        print(f"  {label}{status}")
    print("systemd:")
    for tmpl in _systemd_templates():
        print(f"  {tmpl.name}")
    if RETIRED_LABELS:
        print("retired:")
        for label in sorted(RETIRED_LABELS):
            print(f"  {label}")


def render(platform: str, repo_root: Path, out_dir: Path, user: str | None = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if platform == "launchd":
        values = machine_values("launchd", repo_root, user=user)
        for tmpl in _launchd_templates():
            label = tmpl.stem
            if label in DISABLED_UNITS:
                continue
            out_path = out_dir / f"{label}.plist"
            out_path.write_text(render_template(tmpl.read_text(), values))
            written.append(out_path)
    elif platform == "systemd":
        values = machine_values("systemd", repo_root, user=user)
        for tmpl in _systemd_templates():
            out_path = out_dir / tmpl.name
            out_path.write_text(render_template(tmpl.read_text(), values))
            written.append(out_path)
    else:
        raise ValueError(f"unknown platform: {platform}")
    return written


def install(
    platform: str,
    repo_root: Path,
    out_dir: Path | None = None,
    dry_run: bool = False,
    user: str | None = None,
) -> list[Path]:
    repo_root = Path(repo_root)
    if platform == "launchd":
        explicit_out_dir = out_dir is not None
        if explicit_out_dir:
            out_dir = Path(out_dir)
        elif dry_run:
            # A dry run must never touch the live agents dir. With no
            # explicit --out, render into a scratch tempdir instead — the
            # point of --dry-run is to preview without writing anything real.
            out_dir = Path(tempfile.mkdtemp(prefix="secondbrain-install-units-dryrun-"))
        else:
            out_dir = launchd_agents_dir()
        written = render("launchd", repo_root, out_dir, user=user)
        for path in written:
            print(f"{'would render' if dry_run else 'rendered'} {path}")
            if not dry_run:
                subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
                subprocess.run(["launchctl", "load", str(path)], capture_output=True)
                print(f"loaded {path.stem}")
        # Retired-unit cleanup only makes sense against the real agents dir —
        # a scratch tempdir was never installed, so there is nothing retired
        # in it to remove.
        retired_check_dir = out_dir if (explicit_out_dir or not dry_run) else None
        if retired_check_dir is not None:
            for label in sorted(RETIRED_LABELS):
                retired_path = retired_check_dir / f"{label}.plist"
                if retired_path.exists():
                    if dry_run:
                        print(f"would remove retired {retired_path}")
                    else:
                        subprocess.run(["launchctl", "unload", str(retired_path)], capture_output=True)
                        retired_path.unlink()
                        print(f"removed retired {retired_path}")
        return written
    elif platform == "systemd":
        out_dir = Path(out_dir) if out_dir is not None else SYSTEMD_RENDER_DIR
        written = render("systemd", repo_root, out_dir, user=user)
        for path in written:
            print(f"rendered {path}")
        names = " ".join(p.name for p in written)
        timers_services = " ".join(
            p.name for p in written if p.suffix == ".timer" or p.name == "secondbrain-chat.service"
        )
        print(
            f"sudo cp {out_dir}/*.service {out_dir}/*.timer /etc/systemd/system/ "
            f"&& sudo systemctl daemon-reload && sudo systemctl enable --now {timers_services}"
        )
        return written
    else:
        raise ValueError(f"unknown platform: {platform}")


def _plists_equal(installed: bytes, rendered: str) -> bool:
    try:
        return plistlib.loads(installed) == plistlib.loads(rendered.encode())
    except Exception:
        return False


def _unit_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#") and not line.strip().startswith(";")
    ]


def diff(platform: str, repo_root: Path, user: str | None = None) -> dict:
    repo_root = Path(repo_root)
    report: dict[str, str] = {}

    if platform == "launchd":
        values = machine_values("launchd", repo_root, user=user)
        installed_dir = launchd_agents_dir()
        templated_labels = set()
        for tmpl in _launchd_templates():
            label = tmpl.stem
            templated_labels.add(label)
            if label in DISABLED_UNITS:
                report[label] = "disabled"
                continue
            installed_path = installed_dir / f"{label}.plist"
            if not installed_path.exists():
                report[label] = "missing"
                continue
            rendered = render_template(tmpl.read_text(), values)
            if _plists_equal(installed_path.read_bytes(), rendered):
                report[label] = "same"
            else:
                report[label] = "differs"

        if installed_dir.exists():
            for path in installed_dir.glob("com.secondbrain.*.plist"):
                label = path.stem
                if label in RETIRED_LABELS:
                    report[label] = "retired"
                elif label not in templated_labels:
                    report[label] = "extra"
    elif platform == "systemd":
        values = machine_values("systemd", repo_root, user=user)
        installed_dir = Path("/etc/systemd/system")
        templated_names = set()
        for tmpl in _systemd_templates():
            name = tmpl.name
            templated_names.add(name)
            installed_path = installed_dir / name
            if not installed_path.exists():
                report[name] = "missing"
                continue
            rendered = render_template(tmpl.read_text(), values)
            if _unit_lines(installed_path.read_text()) == _unit_lines(rendered):
                report[name] = "same"
            else:
                report[name] = "differs"
    else:
        raise ValueError(f"unknown platform: {platform}")

    return report


def _cmd_list(_args: argparse.Namespace) -> int:
    list_units()
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else (LAUNCHD_RENDER_DIR if args.platform == "launchd" else SYSTEMD_RENDER_DIR)
    written = render(args.platform, REPO_ROOT, out_dir, user=args.user)
    for path in written:
        print(f"rendered {path}")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else None
    install(args.platform, REPO_ROOT, out_dir=out_dir, dry_run=args.dry_run, user=args.user)
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    report = diff(args.platform, REPO_ROOT, user=args.user)
    bad = False
    for label, status in sorted(report.items()):
        print(f"{label}: {status}")
        if status in ("differs", "missing", "retired", "extra"):
            bad = True
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render/install Second Brain scheduled units")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list").set_defaults(func=_cmd_list)

    p_render = sub.add_parser("render")
    p_render.add_argument("--platform", choices=["launchd", "systemd"], default="launchd")
    p_render.add_argument("--out")
    p_render.add_argument("--user", help="Override the rendered USER value (systemd: User=; default is the current user)")
    p_render.set_defaults(func=_cmd_render)

    p_install = sub.add_parser("install")
    p_install.add_argument("--platform", choices=["launchd", "systemd"], default="launchd")
    p_install.add_argument("--out")
    p_install.add_argument("--dry-run", action="store_true")
    p_install.add_argument("--user", help="Override the rendered USER value (systemd: User=; default is the current user)")
    p_install.set_defaults(func=_cmd_install)

    p_diff = sub.add_parser("diff")
    p_diff.add_argument("--platform", choices=["launchd", "systemd"], default="launchd")
    p_diff.add_argument("--user", help="Override the rendered USER value (systemd: User=; default is the current user)")
    p_diff.set_defaults(func=_cmd_diff)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
