#!/usr/bin/env python3
"""Keep this Mac's skills in step with the catalog's main branch.

`run` is the unattended job: it fast-forwards a clean main checkout, applies any
pending link changes, and audits the result. `install` and `uninstall` manage the
LaunchAgent that calls `run` at login and every six hours; both preview unless
given --apply.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import cc_switch_skills
import sync_skills

ROOT = Path(__file__).resolve().parents[1]
LABEL = "io.github.niebloomj.skills-sync"
INTERVAL_SECONDS = 6 * 60 * 60
GIT_TIMEOUT_SECONDS = 120


class GitError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z} {message}", flush=True)


def log_path(home: Path) -> Path:
    return home / sync_skills.STATE / "auto-sync.log"


def notify(message: str) -> None:
    script = 'display notification (item 1 of argv) with title "Personal skills"'
    try:
        subprocess.run(
            ["osascript", "-e", "on run argv", "-e", script, "-e", "end run", message],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"Could not show a notification: {error}")


def git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as error:
        raise GitError(f"git {arguments[0]} timed out") from error
    if result.returncode:
        raise GitError(result.stderr.strip() or f"git {arguments[0]} exited {result.returncode}")
    return result.stdout.strip()


def update_checkout(repository: Path) -> bool:
    branch = git(repository, "branch", "--show-current")
    if branch != "main":
        log(f"Skipped: the checkout is on {branch or 'a detached HEAD'}, not main.")
        return False
    if git(repository, "status", "--porcelain", "--untracked-files=no"):
        log("Skipped: the checkout has uncommitted changes.")
        return False
    before = git(repository, "rev-parse", "HEAD")
    try:
        git(repository, "fetch", "--quiet", "origin", "main")
    except GitError as error:
        log(f"Could not fetch origin/main, so syncing the current checkout: {error}")
        return True
    try:
        git(repository, "merge", "--ff-only", "--quiet", "origin/main")
    except GitError as error:
        raise GitError(f"main cannot fast-forward to origin/main: {error}") from error
    after = git(repository, "rev-parse", "HEAD")
    log(f"Updated main {before[:12]}..{after[:12]}." if after != before else "main is current.")
    return True


def run_updated_copy(repository: Path, home: Path) -> int:
    script = repository / "scripts/auto_sync.py"
    command = [sys.executable, str(script), "run", "--no-pull", "--home", str(home)]
    return subprocess.run(command, check=False).returncode


def describe(action: dict[str, Any]) -> str:
    return (
        f"{action['kind']}: {action['path']}"
        + (f" -> {action['target']}" if "target" in action else "")
        + (f" ({action['reason']})" if "reason" in action else "")
    )


def sync(repository: Path, home: Path) -> int:
    preview = sync_skills.synchronize(repository, home)
    pending = [action for action in preview["actions"] if action["kind"] != "skipped"]
    metadata = preview["ccSwitch"] or {}
    if pending or any(metadata.get(key) for key in sync_skills.CC_SWITCH_CHANGES):
        if sync_skills.configuration(home).get("ccSwitchManaged"):
            try:
                cc_switch_skills.ensure_stopped()
            except ValueError as error:
                log(f"Waiting: {error}")
                notify("Skill changes are waiting for CC Switch to quit.")
                return 0
        result = sync_skills.synchronize(repository, home, apply=True)
        applied = [action for action in result["actions"] if action["kind"] != "skipped"]
        for action in applied:
            log(describe(action))
        if result["ccSwitch"] is not None:
            log("CC-Switch: " + json.dumps(result["ccSwitch"], sort_keys=True))
        backup = f" Backup: {result['backup']}" if result["backup"] else ""
        log(f"Applied {len(applied)} link change(s).{backup}")
    else:
        log("Links already match the catalog.")
    report = sync_skills.audit_report(repository, home)
    for problem in report["problems"]:
        log(f"Problem: {problem}")
    log(
        f"Audit: {len(report['problems'])} problem(s), "
        f"{len(report['warnings'])} retained-link warning(s)."
    )
    if report["problems"]:
        notify(f"The skill audit found {len(report['problems'])} problem(s). See {log_path(home)}.")
        return 1
    return 0


def run(repository: Path, home: Path, *, pull: bool = True) -> int:
    try:
        if pull:
            return run_updated_copy(repository, home) if update_checkout(repository) else 0
        return sync(repository, home)
    except (GitError, OSError, ValueError, KeyError) as error:
        log(f"Error: {error}")
        notify(f"Skill sync failed: {error}")
        return 1
    except Exception as error:
        notify(f"Skill sync crashed: {error}")
        raise


def agent_path(home: Path) -> Path:
    return home / "Library/LaunchAgents" / f"{LABEL}.plist"


def interpreter() -> str:
    alias = shutil.which("python3")
    if alias and Path(alias).resolve() == Path(sys.executable).resolve():
        return alias
    return sys.executable


def launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *arguments], capture_output=True, text=True, check=False)


def install(repository: Path, home: Path, *, apply: bool) -> int:
    path, domain, output = agent_path(home), f"gui/{os.getuid()}", str(log_path(home))
    script = str(repository / "scripts/auto_sync.py")
    definition = {
        "Label": LABEL,
        "ProgramArguments": [interpreter(), script, "run", "--home", str(home)],
        "ProcessType": "Background",
        "RunAtLoad": True,
        "StartInterval": INTERVAL_SECONDS,
        "StandardErrorPath": output,
        "StandardOutPath": output,
    }
    content = plistlib.dumps(definition)
    print(f"LaunchAgent {path}:\n{content.decode('utf-8')}")
    if not apply:
        print("Preview only. Rerun with --apply to install it; it then runs now, at login,")
        print("and every six hours.")
        return 0
    log_path(home).parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    launchctl("bootout", f"{domain}/{LABEL}")
    result = launchctl("bootstrap", domain, str(path))
    if result.returncode:
        raise ValueError(f"launchctl bootstrap failed: {result.stderr.strip()}")
    print(f"Installed. Log: {output}")
    return 0


def uninstall(home: Path, *, apply: bool) -> int:
    path, domain = agent_path(home), f"gui/{os.getuid()}"
    if not apply:
        print(f"Preview only. Rerun with --apply to unload {LABEL} and remove {path}.")
        return 0
    launchctl("bootout", f"{domain}/{LABEL}")
    path.unlink(missing_ok=True)
    print("Uninstalled.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("run", "install", "uninstall"))
    parser.add_argument(
        "--apply", action="store_true", help="install or uninstall instead of previewing"
    )
    parser.add_argument(
        "--no-pull", action="store_true", help="run: sync the checkout without updating it"
    )
    parser.add_argument("--home", type=Path, default=Path.home(), help="agent home directory")
    arguments = parser.parse_args()
    home = arguments.home.expanduser().resolve()
    if arguments.command == "run":
        if arguments.apply:
            parser.error("run always applies; --apply is for install and uninstall")
        return run(ROOT, home, pull=not arguments.no_pull)
    if arguments.no_pull:
        parser.error("--no-pull applies only to run")
    try:
        if arguments.command == "install":
            return install(ROOT, home, apply=arguments.apply)
        return uninstall(home, apply=arguments.apply)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
