import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import update_skills

OLD, NEW = "a" * 40, "b" * 40


def skill_text(name, description="Write things.", extra="", body="Body.\n"):
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}".encode()


class LatestRefTests(unittest.TestCase):
    def requests(self, commits):
        seen = []

        def respond(url):
            seen.append(url)
            if url.endswith("/repos/example/docs"):
                return {"default_branch": "main"}
            return commits

        return seen, respond

    def test_waiting_period_selects_the_newest_commit_old_enough(self):
        seen, respond = self.requests([{"sha": NEW}])
        with patch.object(update_skills, "json_request", side_effect=respond):
            ref = update_skills.latest_ref(
                "example/docs", 7, now=datetime(2026, 11, 23, 12, tzinfo=UTC)
            )
        self.assertEqual(ref, NEW)
        self.assertEqual(
            seen[1],
            "https://api.github.com/repos/example/docs/commits"
            "?sha=main&until=2026-11-16T12%3A00%3A00Z&per_page=1",
        )

    def test_no_commit_old_enough_means_no_update(self):
        seen, respond = self.requests([])
        with patch.object(update_skills, "json_request", side_effect=respond):
            self.assertIsNone(update_skills.latest_ref("example/docs", 7))


class UpstreamUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = {
            "schemaVersion": 1,
            "skills": {
                "docs": {"source": "example/docs", "upstreamPath": "skills/docs"},
                "tools": {"source": "example/tools", "upstreamPath": "skills/tools"},
            },
            "sources": {
                source: {
                    "license": "MIT",
                    "licenseFile": f"licenses/{source.replace('/', '-')}-LICENSE",
                    "licensePath": "LICENSE",
                    "ref": OLD,
                }
                for source in ("example/docs", "example/tools")
            },
        }
        self.upstream = {}
        self.licenses = {}
        for name in ("docs", "tools"):
            folder = self.root / "skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_bytes(skill_text(name))
            self.upstream[name] = {"SKILL.md": skill_text(name)}
        for source, details in self.catalog["sources"].items():
            path = self.root / details["licenseFile"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"MIT License.\n")
            self.licenses[source] = b"MIT License.\n"
        (self.root / "catalog.json").write_text(update_skills.catalog_text(self.catalog))
        self.executables = {"docs": set(), "tools": set()}
        self.relation = "ahead"
        for target, value in (
            ("ROOT", self.root),
            ("CATALOG_PATH", self.root / "catalog.json"),
        ):
            patcher = patch.object(update_skills, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, side_effect in (
            ("latest_ref", lambda repository, *arguments, **keywords: NEW),
            ("pin_relation", lambda repository, current, candidate: self.relation),
            ("fetch_skill", self.fetch),
            ("read_source", lambda repository, ref, path: self.licenses[repository]),
        ):
            patcher = patch.object(update_skills, target, side_effect=side_effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fetch(self, repository, ref, source_path, name, tree_cache, exclude=None):
        tree_cache[(repository, ref)] = {
            "tree": [
                {
                    "path": f"{source_path}/{relative}",
                    "type": "blob",
                    "mode": "100755" if relative in self.executables[name] else "100644",
                }
                for relative in self.upstream[name]
            ]
        }
        return dict(self.upstream[name])

    def update(self, only):
        return update_skills.update_catalog(update_skills.load_catalog(), only=only)

    def pins(self):
        catalog = json.loads((self.root / "catalog.json").read_text())
        return {source: details["ref"] for source, details in catalog["sources"].items()}

    def test_markdown_only_update_merges_automatically(self):
        self.upstream["docs"]["SKILL.md"] = skill_text("docs", "Write better things.")
        report = self.update("clean")
        self.assertEqual(report["clean"], {"example/docs": {"ref": NEW, "skills": ["docs"]}})
        self.assertEqual(report["review"], {})
        self.assertEqual(report["written"], ["example/docs"])
        self.assertEqual(self.pins(), {"example/docs": NEW, "example/tools": OLD})
        self.assertIn(b"Write better things.", (self.root / "skills/docs/SKILL.md").read_bytes())

    def test_script_changes_wait_for_review(self):
        self.upstream["tools"]["scripts/run.sh"] = b"#!/bin/sh\necho hi\n"
        self.executables["tools"] = {"scripts/run.sh"}
        report = self.update("clean")
        self.assertEqual(report["written"], [])
        self.assertEqual(
            report["review"]["example/tools"]["reasons"],
            ["skills/tools/scripts/run.sh: adds or changes a script or executable file"],
        )
        self.assertEqual(self.pins(), {"example/docs": OLD, "example/tools": OLD})
        self.assertFalse((self.root / "skills/tools/scripts").exists())
        report = self.update("review")
        self.assertEqual(report["written"], ["example/tools"])
        self.assertEqual(self.pins(), {"example/docs": OLD, "example/tools": NEW})
        self.assertTrue((self.root / "skills/tools/scripts/run.sh").stat().st_mode & 0o100)

    def test_license_changes_wait_for_review(self):
        self.licenses["example/docs"] = b"Apache License.\n"
        report = self.update("clean")
        self.assertEqual(report["written"], [])
        self.assertEqual(
            report["review"]["example/docs"]["reasons"], ["example/docs: license text changed"]
        )

    def held_for(self, *reasons):
        report = self.update("clean")
        self.assertEqual(report["written"], [])
        self.assertEqual(report["review"]["example/docs"]["reasons"], list(reasons))

    def test_frontmatter_settings_wait_for_review(self):
        for extra, setting in (
            ("user-invocable: false\n", "user-invocable"),
            ("allowed-tools: Bash\n", "allowed-tools"),
            ("hooks:\n  Stop:\n    - hooks:\n        - type: command\n", "hooks"),
            ('"hooks": {}\n', "hooks"),
        ):
            with self.subTest(setting=extra):
                self.upstream["docs"]["SKILL.md"] = skill_text("docs", extra=extra)
                self.held_for(f"skills/docs/SKILL.md: changes frontmatter settings: {setting}")

    def test_manual_invocation_waits_for_review_on_both_agents(self):
        self.upstream["docs"]["SKILL.md"] = skill_text(
            "docs", extra="disable-model-invocation: true\n"
        )
        self.held_for(
            "skills/docs/SKILL.md: changes frontmatter settings: disable-model-invocation",
            "skills/docs/agents/openai.yaml: changes Codex settings: policy",
        )

    def test_descriptive_frontmatter_changes_merge_automatically(self):
        self.upstream["docs"]["SKILL.md"] = skill_text(
            "docs", "Write better things.", extra="license: MIT\nmetadata:\n  version: 2\n"
        )
        report = self.update("clean")
        self.assertEqual(report["review"], {})
        self.assertEqual(report["written"], ["example/docs"])

    def test_shell_commands_wait_for_review(self):
        for body in ("Status: !`git status`\n", "````!\ngit status\n````\n"):
            with self.subTest(body=body):
                self.upstream["docs"]["SKILL.md"] = skill_text("docs", body=body)
                self.held_for(
                    "skills/docs/SKILL.md: adds or changes shell commands that run"
                    " when the skill loads"
                )

    def test_codex_settings_wait_for_review(self):
        self.upstream["docs"]["agents/openai.yaml"] = (
            b"policy:\n  allow_implicit_invocation: true\n"
        )
        self.held_for("skills/docs/agents/openai.yaml: changes Codex settings: policy")

    def test_codex_display_text_merges_automatically(self):
        self.upstream["docs"]["agents/openai.yaml"] = b'interface:\n  display_name: "Docs"\n'
        report = self.update("clean")
        self.assertEqual(report["review"], {})
        self.assertEqual(report["written"], ["example/docs"])

    def test_hidden_files_wait_for_review(self):
        self.upstream["docs"][".claude-plugin/plugin.json"] = b'{"name": "docs"}\n'
        self.held_for(
            "skills/docs/.claude-plugin/plugin.json: adds or changes a hidden file,"
            " which agents can load as configuration"
        )

    def test_pins_never_move_backwards(self):
        self.upstream["docs"]["SKILL.md"] = skill_text("docs", "Older text.")
        self.relation = "behind"
        report = self.update(None)
        self.assertEqual(report, {"clean": {}, "review": {}, "written": []})
        self.assertEqual(self.pins(), {"example/docs": OLD, "example/tools": OLD})

    def test_rewritten_upstream_history_waits_for_review(self):
        self.upstream["docs"]["SKILL.md"] = skill_text("docs", "Rewritten.")
        self.relation = "diverged"
        report = self.update("clean")
        self.assertEqual(report["written"], [])
        self.assertIn(
            "example/docs: upstream history no longer contains the pinned commit",
            report["review"]["example/docs"]["reasons"],
        )

    def test_report_file_is_written_for_the_workflow(self):
        self.upstream["docs"]["SKILL.md"] = skill_text("docs", "Write better things.")
        report = self.root / "report.json"
        arguments = ["update_skills.py", "--min-age-days", "7", "--only", "clean"]
        with patch.object(sys, "argv", [*arguments, "--report", str(report)]):
            self.assertEqual(update_skills.main(), 0)
        self.assertEqual(
            json.loads(report.read_text()),
            {
                "clean": {"example/docs": {"ref": NEW, "skills": ["docs"]}},
                "review": {},
                "written": ["example/docs"],
            },
        )


if __name__ == "__main__":
    unittest.main()
