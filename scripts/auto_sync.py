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
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cc_switch_skills
import sync_skills

ROOT = Path(__file__).resolve().parents[1]
LABEL = "io.github.niebloomj.skills-sync"
INTERVAL_SECONDS = 6 * 60 * 60
GIT_TIMEOUT_SECONDS = 120
NOTIFIED = 3
BLOCKED_NOTICE = timedelta(hours=24)
BOOTSTRAP_ATTEMPTS = 5
FIRST_RUN_SECONDS = 180
WRAPPER = """"$1" "$2" run --home "$3"
code=$?
case $code in
  0|3) ;;
  *) osascript -e 'on run argv' \\
       -e 'display notification (item 1 of argv) with title "Personal skills"' -e 'end run' \\
       "The skill sync could not start (exit $code). See $4." ;;
esac
exit $code
"""


class GitError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z} {message}", flush=True)


def log_path(home: Path) -> Path:
    return home / sync_skills.STATE / "auto-sync.log"


def state_path(home: Path) -> Path:
    return home / sync_skills.STATE / "auto-sync.json"


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


def update_checkout(repository: Path) -> tuple[bool, str | None]:
    branch = git(repository, "branch", "--show-current")
    if branch != "main":
        reason = f"the checkout is on {branch or 'a detached HEAD'}, not main"
        log(f"Skipped: {reason}.")
        return False, reason
    if git(repository, "status", "--porcelain", "--untracked-files=no"):
        reason = "the checkout has uncommitted changes"
        log(f"Skipped: {reason}.")
        return False, reason
    before = git(repository, "rev-parse", "HEAD")
    try:
        git(repository, "fetch", "--quiet", "origin", "main")
    except GitError as error:
        log(f"Could not fetch origin/main, so syncing the current checkout: {error}")
        return True, "origin/main could not be fetched"
    try:
        git(repository, "merge", "--ff-only", "--quiet", "origin/main")
    except GitError as error:
        raise GitError(f"main cannot fast-forward to origin/main: {error}") from error
    after = git(repository, "rev-parse", "HEAD")
    log(f"Updated main {before[:12]}..{after[:12]}." if after != before else "main is current.")
    return True, None


def moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def read_state(home: Path) -> dict[str, datetime]:
    path = state_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        log(f"Starting over from an unreadable {path.name}.")
        return {}
    times = {key: moment(raw.get(key)) for key in ("lastSynced", "lastNotice")}
    return {key: value for key, value in times.items() if value is not None}


def write_state(home: Path, state: dict[str, datetime]) -> None:
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    text = json.dumps({key: value.isoformat() for key, value in state.items()}, sort_keys=True)
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, path)


def note_progress(home: Path, blocked: str | None, now: datetime) -> None:
    state = read_state(home)
    since = state.get("lastSynced")
    if blocked is None or since is None:
        state = {"lastSynced": now}
    elif now - since >= BLOCKED_NOTICE:
        noticed = state.get("lastNotice")
        if noticed is None or now - noticed >= BLOCKED_NOTICE:
            notify(f"Skills haven't synced since {since.astimezone():%b %d}: {blocked}.")
            state["lastNotice"] = now
    write_state(home, state)


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
        return NOTIFIED
    return 0


def run(repository: Path, home: Path, *, pull: bool = True) -> int:
    try:
        if not pull:
            return sync(repository, home)
        proceed, blocked = update_checkout(repository)
        code = run_updated_copy(repository, home) if proceed else 0
        if code not in (0, NOTIFIED):
            log(f"Error: the sync exited {code} without reporting why.")
            notify(f"Skill sync failed. See {log_path(home)}.")
        if code == 0:
            note_progress(home, blocked, datetime.now(UTC))
        return 0 if code == 0 else NOTIFIED
    except (GitError, OSError, ValueError, KeyError) as error:
        log(f"Error: {error}")
        notify(f"Skill sync failed: {error}")
        return NOTIFIED
    except Exception as error:
        log(f"Error: {error!r}\n{traceback.format_exc()}")
        notify(f"Skill sync crashed: {error}")
        return NOTIFIED


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
        "ProgramArguments": [
            "/bin/sh",
            "-c",
            WRAPPER,
            "auto-sync",
            interpreter(),
            script,
            str(home),
            output,
        ],
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
    for _ in range(BOOTSTRAP_ATTEMPTS):
        result = launchctl("bootstrap", domain, str(path))
        if not result.returncode:
            break
        time.sleep(1)
    else:
        raise ValueError(f"launchctl bootstrap failed: {result.stderr.strip()}")
    print(f"Installed. Waiting for the first run; log: {output}")
    code = first_exit_code(domain)
    if code is None:
        print("The first run is still going. Check the log in a few minutes.")
    elif code:
        print(f"The first run failed with exit code {code}. See the log.")
        print("If it says 'Operation not permitted', allow python3 to use the Documents")
        print("folder in System Settings > Privacy & Security > Files and Folders.")
        return 1
    else:
        print("The first run finished. The log shows what it did.")
    return 0


def first_exit_code(domain: str) -> int | None:
    deadline = time.monotonic() + FIRST_RUN_SECONDS
    while time.monotonic() < deadline:
        status = launchctl("print", f"{domain}/{LABEL}").stdout
        finished = re.search(r"last exit code = (\d+)", status)
        if finished and "state = running" not in status:
            return int(finished[1])
        time.sleep(2)
    return None


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
