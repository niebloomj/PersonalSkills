#!/usr/bin/env python3
"""Keep this machine's skills in step with the catalog's main branch.

`run` is the unattended job: it fast-forwards a clean main checkout, applies any
pending link changes, and audits the result.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import cc_switch_skills
import sync_skills

ROOT = Path(__file__).resolve().parents[1]
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("run",))
    parser.add_argument(
        "--no-pull", action="store_true", help="sync the checkout without updating it"
    )
    parser.add_argument("--home", type=Path, default=Path.home(), help="agent home directory")
    arguments = parser.parse_args()
    return run(ROOT, arguments.home.expanduser().resolve(), pull=not arguments.no_pull)


if __name__ == "__main__":
    raise SystemExit(main())
