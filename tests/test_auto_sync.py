import io
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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
        self.synced = []
        self.patch(
            auto_sync,
            "run_updated_copy",
            lambda repository, home: self.synced.append(repository) or 0,
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
        self.assertEqual((code, self.synced), (1, []))
        self.assertEqual(len(self.notices), 1)
        self.assertIn("cannot fast-forward", output)


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
        self.assertEqual(code, 1)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Problem: Unmanaged skill: /h/.claude/skills/stray", output)

    def test_sync_errors_notify(self):
        self.error = ValueError("catalog.json has an unsupported or incomplete schema")
        code, output = self.sync()
        self.assertEqual(code, 1)
        self.assertEqual(
            self.notices,
            ["Skill sync failed: catalog.json has an unsupported or incomplete schema"],
        )
        self.assertIn("Error: catalog.json", output)


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


class AgentTests(Isolated):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.patch(
            auto_sync,
            "launchctl",
            lambda *arguments: (
                self.calls.append(arguments) or subprocess.CompletedProcess(arguments, 0, "", "")
            ),
        )
        self.patch(auto_sync, "interpreter", lambda: "/opt/homebrew/bin/python3")
        self.agent = self.home / "Library/LaunchAgents" / f"{auto_sync.LABEL}.plist"
        self.domain = f"gui/{os.getuid()}"

    def test_install_previews_by_default(self):
        code, output = captured(auto_sync.install, REPOSITORY, self.home, apply=False)
        self.assertEqual((code, self.calls), (0, []))
        self.assertFalse(self.agent.exists())
        self.assertIn("--apply", output)

    def test_install_runs_now_at_login_and_every_six_hours(self):
        code, _ = captured(auto_sync.install, REPOSITORY, self.home, apply=True)
        self.assertEqual(code, 0)
        definition = plistlib.loads(self.agent.read_bytes())
        self.assertEqual(
            definition["ProgramArguments"],
            [
                "/opt/homebrew/bin/python3",
                str(REPOSITORY / "scripts/auto_sync.py"),
                "run",
                "--home",
                str(self.home),
            ],
        )
        self.assertEqual((definition["RunAtLoad"], definition["StartInterval"]), (True, 21600))
        log = str(self.home / ".local/state/personal-skills/auto-sync.log")
        self.assertEqual(
            (definition["StandardOutPath"], definition["StandardErrorPath"]), (log, log)
        )
        self.assertTrue((self.home / ".local/state/personal-skills").is_dir())
        self.assertEqual(self.calls[-1], ("bootstrap", self.domain, str(self.agent)))

    def test_failed_bootstrap_is_reported(self):
        self.patch(
            auto_sync,
            "launchctl",
            lambda *arguments: subprocess.CompletedProcess(arguments, 5, "", "Input/output error"),
        )
        with self.assertRaisesRegex(ValueError, "Input/output error"):
            captured(auto_sync.install, REPOSITORY, self.home, apply=True)

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
