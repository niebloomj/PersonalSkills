import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import auto_sync
import cc_switch_skills
import sync_skills

REPOSITORY = Path(__file__).resolve().parents[1]
GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def captured(function, *arguments, **keywords):
    output = io.StringIO()
    with redirect_stdout(output):
        code = function(*arguments, **keywords)
    return code, output.getvalue()


class Isolated(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.notices = []
        self.patch(auto_sync, "notify", self.notices.append)

    def patch(self, target, attribute, replacement):
        patcher = patch.object(target, attribute, side_effect=replacement)
        patcher.start()
        self.addCleanup(patcher.stop)


class CheckoutTests(Isolated):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)
        origin = self.root / "origin.git"
        self.upstream, self.checkout = self.root / "upstream", self.root / "checkout"
        self.git(self.root, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
        self.git(self.root, "clone", "--quiet", str(origin), str(self.upstream))
        self.commit(self.upstream, "first")
        self.git(self.root, "clone", "--quiet", str(origin), str(self.checkout))
        self.synced, self.child_code = [], 0
        self.patch(
            auto_sync,
            "run_updated_copy",
            lambda repository, home: self.synced.append(repository) or self.child_code,
        )

    def git(self, directory, *arguments):
        return subprocess.run(
            ["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, directory, text, push=True, filename="notes.txt"):
        (directory / filename).write_text(text)
        self.git(directory, "add", filename)
        self.git(directory, "commit", "--quiet", "-m", text)
        if push:
            self.git(directory, "push", "--quiet", "origin", "HEAD:main")

    def head(self, directory):
        return self.git(directory, "rev-parse", "HEAD")

    def run_job(self):
        return captured(auto_sync.run, self.checkout, self.home)

    def test_clean_main_fast_forwards_before_syncing(self):
        self.commit(self.upstream, "second")
        code, output = self.run_job()
        self.assertEqual((code, self.synced, self.notices), (0, [self.checkout], []))
        self.assertEqual(self.head(self.checkout), self.head(self.upstream))
        self.assertIn("Updated main", output)

    def test_other_branches_are_left_alone(self):
        self.git(self.checkout, "switch", "--quiet", "-c", "feature")
        before = self.head(self.checkout)
        self.commit(self.upstream, "second")
        code, output = self.run_job()
        self.assertEqual((code, self.synced), (0, []))
        self.assertEqual(self.head(self.checkout), before)
        self.assertIn("on feature, not main", output)

    def test_uncommitted_changes_are_left_alone(self):
        (self.checkout / "notes.txt").write_text("editing")
        self.commit(self.upstream, "second")
        code, output = self.run_job()
        self.assertEqual((code, self.synced), (0, []))
        self.assertEqual((self.checkout / "notes.txt").read_text(), "editing")
        self.assertIn("uncommitted changes", output)

    def test_untracked_files_do_not_block_updates(self):
        (self.checkout / "scratch.txt").write_text("scratch")
        self.commit(self.upstream, "second")
        code, _ = self.run_job()
        self.assertEqual((code, self.synced), (0, [self.checkout]))
        self.assertEqual(self.head(self.checkout), self.head(self.upstream))

    def test_unreachable_origin_still_syncs_the_current_checkout(self):
        self.git(self.checkout, "remote", "set-url", "origin", str(self.root / "missing.git"))
        code, output = self.run_job()
        self.assertEqual((code, self.synced, self.notices), (0, [self.checkout], []))
        self.assertIn("Could not fetch origin/main", output)

    def test_diverged_main_stops_and_notifies(self):
        self.commit(self.upstream, "second")
        self.commit(self.checkout, "local", push=False, filename="local.txt")
        before = self.head(self.checkout)
        code, output = self.run_job()
        self.assertEqual(self.head(self.checkout), before)
        self.assertEqual((code, self.synced), (auto_sync.NOTIFIED, []))
        self.assertEqual(len(self.notices), 1)
        self.assertIn("cannot fast-forward", output)

    def test_a_failure_the_sync_did_not_report_is_reported(self):
        self.child_code = 1
        code, output = self.run_job()
        self.assertEqual(code, auto_sync.NOTIFIED)
        self.assertEqual(self.notices, [f"Skill sync failed. See {auto_sync.log_path(self.home)}."])
        self.assertIn("exited 1 without reporting why", output)

    def test_a_failure_the_sync_reported_is_not_reported_again(self):
        self.child_code = auto_sync.NOTIFIED
        code, _ = self.run_job()
        self.assertEqual((code, self.notices), (auto_sync.NOTIFIED, []))

    def test_a_skipped_run_records_why_syncing_is_blocked(self):
        self.git(self.checkout, "switch", "--quiet", "-c", "feature")
        blocked = []
        self.patch(auto_sync, "note_progress", lambda home, reason, now: blocked.append(reason))
        self.run_job()
        self.assertEqual(blocked, ["the checkout is on feature, not main"])


class BlockedNoticeTests(Isolated):
    START = datetime(2026, 11, 23, 12, tzinfo=UTC)

    def note(self, blocked, hours):
        auto_sync.note_progress(self.home, blocked, self.START + timedelta(hours=hours))

    def test_the_first_blocked_run_starts_the_clock_without_a_notice(self):
        self.note("the checkout is on feature, not main", 0)
        self.assertEqual(self.notices, [])
        state = json.loads(auto_sync.state_path(self.home).read_text())
        self.assertEqual(state, {"lastSynced": self.START.isoformat()})

    def test_a_day_without_syncing_notifies_once_a_day(self):
        self.note(None, 0)
        for hours, expected in ((23, 0), (25, 1), (30, 1), (49, 2)):
            self.note("the checkout is on feature, not main", hours)
            self.assertEqual(len(self.notices), expected, f"after {hours} hours")
        self.assertIn("the checkout is on feature, not main", self.notices[0])

    def test_an_unreadable_state_file_starts_the_clock_over(self):
        path = auto_sync.state_path(self.home)
        path.parent.mkdir(parents=True)
        for content in ("", "null", '{"lastSynced": "2026-11-20T12:00:00"}', '{"lastSynced": 5}'):
            with self.subTest(content=content):
                path.write_text(content)
                captured(self.note, "the checkout is on feature, not main", 30)
                state = json.loads(path.read_text())
                self.assertEqual(
                    state, {"lastSynced": (self.START + timedelta(hours=30)).isoformat()}
                )
        self.assertEqual(self.notices, [])
        self.assertEqual([item.name for item in path.parent.iterdir()], [path.name])

    def test_a_successful_sync_resets_the_clock(self):
        self.note(None, 0)
        self.note("origin/main could not be fetched", 25)
        self.note(None, 26)
        state = json.loads(auto_sync.state_path(self.home).read_text())
        self.assertEqual(state, {"lastSynced": (self.START + timedelta(hours=26)).isoformat()})
        self.note("origin/main could not be fetched", 49)
        self.assertEqual(len(self.notices), 1)


class UpdatedCopyTests(Isolated):
    def test_the_sync_runs_from_the_updated_checkout(self):
        calls = []
        with patch.object(
            auto_sync.subprocess,
            "run",
            side_effect=lambda command, check: (
                calls.append(command) or subprocess.CompletedProcess(command, 3)
            ),
        ):
            code = auto_sync.run_updated_copy(self.root / "checkout", self.home)
        self.assertEqual(code, 3)
        self.assertEqual(
            calls,
            [
                [
                    sys.executable,
                    str(self.root / "checkout/scripts/auto_sync.py"),
                    "run",
                    "--no-pull",
                    "--home",
                    str(self.home),
                ]
            ],
        )


class SyncTests(Isolated):
    def setUp(self):
        super().setUp()
        self.preview = {"actions": [], "backup": None, "ccSwitch": None}
        self.applied, self.config, self.running, self.error = [], {}, False, None
        self.report = {"problems": [], "warnings": ["Unmanaged dangling skill link: /kept"]}
        self.patch(sync_skills, "synchronize", self.synchronize)
        self.patch(sync_skills, "configuration", lambda home, *arguments: self.config)
        self.patch(sync_skills, "audit_report", lambda repository, home: self.report)
        self.patch(cc_switch_skills, "ensure_stopped", self.ensure_stopped)

    def synchronize(self, repository, home, apply=False):
        if self.error:
            raise self.error
        if not apply:
            return self.preview
        self.applied.append(repository)
        return {**self.preview, "backup": "/backups/1"}

    def ensure_stopped(self):
        if self.running:
            raise ValueError("CC Switch is running.")

    def sync(self):
        return captured(auto_sync.run, REPOSITORY, self.home, pull=False)

    def test_pending_changes_are_applied_then_audited(self):
        self.preview["actions"] = [
            {"kind": "link", "path": "/h/.claude/skills/new", "target": "/c"}
        ]
        code, output = self.sync()
        self.assertEqual((code, self.applied, self.notices), (0, [REPOSITORY], []))
        self.assertIn("link: /h/.claude/skills/new -> /c", output)
        self.assertIn("Backup: /backups/1", output)
        self.assertIn("Audit: 0 problem(s), 1 retained-link warning(s).", output)

    def test_nothing_is_applied_when_links_already_match(self):
        self.preview["actions"] = [{"kind": "skipped", "path": "/c", "reason": "unsupported"}]
        code, output = self.sync()
        self.assertEqual((code, self.applied, self.notices), (0, [], []))
        self.assertIn("already match", output)

    def test_cc_switch_metadata_changes_are_pending_changes(self):
        self.preview["ccSwitch"] = {"added": ["new-skill"], "updated": []}
        self.sync()
        self.assertEqual(self.applied, [REPOSITORY])

    def test_pending_changes_wait_while_cc_switch_runs(self):
        self.preview["actions"] = [{"kind": "link", "path": "/h/new", "target": "/c"}]
        self.config["ccSwitchManaged"] = True
        self.running = True
        code, output = self.sync()
        self.assertEqual((code, self.applied), (0, []))
        self.assertEqual(self.notices, ["Skill changes are waiting for CC Switch to quit."])
        self.assertIn("Waiting: CC Switch is running.", output)

    def test_a_running_cc_switch_matters_only_when_it_manages_skills(self):
        self.preview["actions"] = [{"kind": "link", "path": "/h/new", "target": "/c"}]
        self.running = True
        self.sync()
        self.assertEqual(self.applied, [REPOSITORY])

    def test_audit_problems_notify(self):
        self.report = {"problems": ["Unmanaged skill: /h/.claude/skills/stray"], "warnings": []}
        code, output = self.sync()
        self.assertEqual(code, auto_sync.NOTIFIED)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Problem: Unmanaged skill: /h/.claude/skills/stray", output)

    def test_sync_errors_notify(self):
        self.error = ValueError("catalog.json has an unsupported or incomplete schema")
        code, output = self.sync()
        self.assertEqual(code, auto_sync.NOTIFIED)
        self.assertEqual(
            self.notices,
            ["Skill sync failed: catalog.json has an unsupported or incomplete schema"],
        )
        self.assertIn("Error: catalog.json", output)

    def test_unexpected_errors_are_reported_with_a_traceback(self):
        self.error = RuntimeError("boom")
        code, output = self.sync()
        self.assertEqual((code, self.notices), (auto_sync.NOTIFIED, ["Skill sync crashed: boom"]))
        self.assertIn("Traceback", output)


class FreshHomeTests(Isolated):
    def test_the_real_catalog_links_into_a_fresh_home_and_audits_clean(self):
        code, output = captured(auto_sync.run, REPOSITORY, self.home, pull=False)
        self.assertEqual((code, self.notices), (0, []), output)
        catalog = sync_skills.load_skills(REPOSITORY)["skills"]
        for name in catalog:
            self.assertEqual(
                (self.home / ".agents/skills" / name).resolve(), REPOSITORY / "skills" / name
            )
        code, output = captured(auto_sync.run, REPOSITORY, self.home, pull=False)
        self.assertEqual(code, 0, output)
        self.assertIn("already match", output)


FINISHED = "state = not running\n\tlast exit code = 0\n"
RUNNING = "state = running\n\tlast exit code = (never exited)\n"


class AgentTests(Isolated):
    def setUp(self):
        super().setUp()
        self.calls, self.bootstrap_failures, self.statuses = [], 0, [FINISHED]
        self.patch(auto_sync, "launchctl", self.launchctl)
        self.patch(auto_sync, "interpreter", lambda: "/opt/homebrew/bin/python3")
        self.patch(auto_sync.time, "sleep", lambda seconds: None)
        self.agent = self.home / "Library/LaunchAgents" / f"{auto_sync.LABEL}.plist"
        self.domain = f"gui/{os.getuid()}"
        self.service = f"{self.domain}/{auto_sync.LABEL}"

    def launchctl(self, *arguments):
        self.calls.append(arguments)
        if arguments[0] == "bootstrap" and self.bootstrap_failures:
            self.bootstrap_failures -= 1
            return subprocess.CompletedProcess(arguments, 5, "", "5: Input/output error")
        if arguments[0] == "print":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return subprocess.CompletedProcess(arguments, 0, status, "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def test_install_previews_by_default(self):
        code, output = captured(auto_sync.install, REPOSITORY, self.home, apply=False)
        self.assertEqual((code, self.calls), (0, []))
        self.assertFalse(self.agent.exists())
        self.assertIn("--apply", output)

    def test_install_runs_now_at_login_and_every_six_hours(self):
        code, _ = captured(auto_sync.install, REPOSITORY, self.home, apply=True)
        self.assertEqual(code, 0)
        definition = plistlib.loads(self.agent.read_bytes())
        log = str(self.home / ".local/state/personal-skills/auto-sync.log")
        self.assertEqual(
            definition["ProgramArguments"],
            [
                "/bin/sh",
                "-c",
                auto_sync.WRAPPER,
                "auto-sync",
                "/opt/homebrew/bin/python3",
                str(REPOSITORY / "scripts/auto_sync.py"),
                str(self.home),
                log,
            ],
        )
        self.assertEqual((definition["RunAtLoad"], definition["StartInterval"]), (True, 21600))
        self.assertEqual(
            (definition["StandardOutPath"], definition["StandardErrorPath"]), (log, log)
        )
        self.assertTrue((self.home / ".local/state/personal-skills").is_dir())
        self.assertEqual(
            self.calls,
            [
                ("bootout", self.service),
                ("bootstrap", self.domain, str(self.agent)),
                ("print", self.service),
            ],
        )

    def test_bootstrap_is_retried_after_a_transient_failure(self):
        self.bootstrap_failures = 2
        code, _ = captured(auto_sync.install, REPOSITORY, self.home, apply=True)
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in self.calls].count("bootstrap"), 3)

    def test_failed_bootstrap_is_reported(self):
        self.bootstrap_failures = auto_sync.BOOTSTRAP_ATTEMPTS
        with self.assertRaisesRegex(ValueError, "Input/output error"):
            captured(auto_sync.install, REPOSITORY, self.home, apply=True)
        self.assertEqual(
            [call[0] for call in self.calls].count("bootstrap"), auto_sync.BOOTSTRAP_ATTEMPTS
        )

    def test_install_waits_for_the_first_run_to_finish(self):
        self.statuses = [RUNNING, RUNNING, FINISHED]
        code, output = captured(auto_sync.install, REPOSITORY, self.home, apply=True)
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in self.calls].count("print"), 3)
        self.assertIn("The first run finished", output)

    def test_a_failed_first_run_points_to_the_documents_permission(self):
        self.statuses = ["state = not running\n\tlast exit code = 2\n"]
        code, output = captured(auto_sync.install, REPOSITORY, self.home, apply=True)
        self.assertEqual(code, 1)
        self.assertIn("exit code 2", output)
        self.assertIn("Files and Folders", output)

    def test_an_earlier_exit_code_is_not_mistaken_for_the_first_run(self):
        self.statuses = [
            "state = running\n\tlast exit code = 0\n",
            "state = not running\n\tlast exit code = 2\n",
        ]
        code, _ = captured(auto_sync.install, REPOSITORY, self.home, apply=True)
        self.assertEqual(code, 1)

    def test_a_first_run_still_going_is_reported(self):
        self.statuses = [RUNNING]
        with patch.object(auto_sync, "FIRST_RUN_SECONDS", 0.05):
            code, output = captured(auto_sync.install, REPOSITORY, self.home, apply=True)
        self.assertEqual(code, 0)
        self.assertIn("still going", output)

    def test_uninstall_previews_by_default(self):
        captured(auto_sync.install, REPOSITORY, self.home, apply=True)
        self.calls.clear()
        code, output = captured(auto_sync.uninstall, self.home, apply=False)
        self.assertEqual((code, self.calls), (0, []))
        self.assertTrue(self.agent.exists())
        self.assertIn("--apply", output)

    def test_uninstall_unloads_and_removes_the_agent(self):
        captured(auto_sync.install, REPOSITORY, self.home, apply=True)
        self.calls.clear()
        code, _ = captured(auto_sync.uninstall, self.home, apply=True)
        self.assertEqual((code, self.calls), (0, [("bootout", f"{self.domain}/{auto_sync.LABEL}")]))
        self.assertFalse(self.agent.exists())


class WrapperTests(Isolated):
    def run_wrapper(self, exit_code):
        tools = self.root / "bin"
        tools.mkdir(exist_ok=True)
        python = tools / "python3"
        python.write_text(f'#!/bin/sh\necho "$@" > "{self.root}/arguments"\nexit {exit_code}\n')
        osascript = tools / "osascript"
        osascript.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{self.root}/notice"\n')
        for tool in (python, osascript):
            tool.chmod(0o755)
        (self.root / "notice").unlink(missing_ok=True)
        result = subprocess.run(
            ["/bin/sh", "-c", auto_sync.WRAPPER, "auto-sync", str(python), "job.py", "home", "log"],
            env={"PATH": f"{tools}:/usr/bin:/bin"},
            check=False,
        )
        notice = self.root / "notice"
        return result.returncode, notice.read_text() if notice.exists() else None

    def test_the_job_runs_with_its_arguments(self):
        self.run_wrapper(0)
        self.assertEqual((self.root / "arguments").read_text(), "job.py run --home home\n")

    def test_reported_outcomes_raise_no_extra_notice(self):
        for exit_code in (0, auto_sync.NOTIFIED):
            with self.subTest(exit_code=exit_code):
                self.assertEqual(self.run_wrapper(exit_code), (exit_code, None))

    def test_a_job_that_cannot_start_raises_a_notice(self):
        code, notice = self.run_wrapper(2)
        self.assertEqual(code, 2)
        self.assertIn("The skill sync could not start (exit 2). See log.", notice)


class InterpreterTests(Isolated):
    def test_the_stable_python3_alias_is_preferred_when_it_is_this_interpreter(self):
        alias = self.root / "python3"
        alias.symlink_to(sys.executable)
        with patch.object(auto_sync.shutil, "which", return_value=str(alias)):
            self.assertEqual(auto_sync.interpreter(), str(alias))

    def test_a_different_python3_on_path_is_not_used(self):
        other = self.root / "python3"
        other.write_text("#!/bin/sh\n")
        with patch.object(auto_sync.shutil, "which", return_value=str(other)):
            self.assertEqual(auto_sync.interpreter(), sys.executable)


if __name__ == "__main__":
    unittest.main()
