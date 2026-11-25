import contextlib
import difflib
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import update_skills
from update_skills import (
    load_catalog,
    safe_relative_path,
    skill_files,
    validate_local,
    validate_skill,
)


class CatalogTests(unittest.TestCase):
    def test_committed_catalog_is_complete_and_valid(self):
        catalog = load_catalog()
        validate_local(catalog)
        self.assertEqual(
            set(catalog["skills"]),
            {path.name for path in (Path(__file__).resolve().parents[1] / "skills").iterdir()},
        )

    def test_committed_catalog_uses_the_tool_written_layout(self):
        text = update_skills.CATALOG_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            text,
            update_skills.catalog_text(json.loads(text)),
            "catalog.json is tool-written; run `python3 scripts/update_skills.py --vendor`",
        )

    def test_local_checks_skip_files_git_ignores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text("__pycache__/\n")
            folder = root / "skills/demo"
            (folder / "scripts/__pycache__").mkdir(parents=True)
            (folder / "SKILL.md").write_text("---\nname: demo\ndescription: Demo skill.\n---\n")
            cache = folder / "scripts/__pycache__/tool.cpython-312.pyc"
            cache.write_bytes(b"\0compiled")
            cache.chmod(0o755)
            with patch.object(update_skills, "ROOT", root):
                self.assertEqual(set(update_skills.current_files("demo")), {"SKILL.md"})
                self.assertEqual(update_skills.current_executables("demo"), set())

    def test_an_enclosing_repository_does_not_decide_what_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            subprocess.run(["git", "init", "-q", str(parent)], check=True)
            (parent / ".gitignore").write_text("*.md\n")
            root = parent / "copy"
            (root / "skills/demo").mkdir(parents=True)
            (root / "skills/demo/SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill.\n---\n"
            )
            with patch.object(update_skills, "ROOT", root):
                self.assertEqual(set(update_skills.current_files("demo")), {"SKILL.md"})

    def test_skills_that_read_untrusted_files_ask_before_running_shell_commands(self):
        skills = Path(__file__).resolve().parents[1] / "skills"
        for name in ("agentic-actions-auditor", "gha-security-review", "skill-scanner"):
            with self.subTest(name):
                skill = (skills / name / "SKILL.md").read_bytes()
                settings = update_skills.top_level_settings(update_skills.frontmatter(skill))
                self.assertNotIn("Bash", " ".join(settings.get("allowed-tools", [])))

    def test_upstream_update_changes_only_the_moved_pin(self):
        repository = Path(__file__).resolve().parents[1]
        new_ref = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            shutil.copy2(repository / "catalog.json", catalog_path)
            shutil.copytree(repository / "licenses", root / "licenses")
            before = catalog_path.read_text(encoding="utf-8")
            catalog = json.loads(before)
            sources = catalog["sources"]
            moved = sorted(sources)[0]
            moved_skills = sorted(
                name for name, entry in catalog["skills"].items() if entry.get("source") == moved
            )
            self.assertTrue(moved_skills)
            rendered = {
                name: {"SKILL.md": f"---\nname: {name}\ndescription: Updated text.\n---\n".encode()}
                for name in moved_skills
            }
            for name in moved_skills:
                (root / "skills" / name).mkdir(parents=True)
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin/marketplace.json").write_text("{}\n")
            licenses = {
                source: (root / details["licenseFile"]).read_bytes()
                for source, details in sources.items()
            }
            with (
                patch.object(update_skills, "ROOT", root),
                patch.object(update_skills, "CATALOG_PATH", catalog_path),
                patch.object(
                    update_skills,
                    "latest_ref",
                    side_effect=lambda source, *arguments, **keywords: (
                        new_ref if source == moved else sources[source]["ref"]
                    ),
                ),
                patch.object(
                    update_skills,
                    "pin_relation",
                    side_effect=lambda source, current, candidate: (
                        "identical" if current == candidate else "ahead"
                    ),
                ),
                patch.object(update_skills, "fetch_skill", return_value={}),
                patch.object(
                    update_skills,
                    "render_skill",
                    side_effect=lambda name, entry, files: rendered.get(
                        name, {"SKILL.md": b"unchanged"}
                    ),
                ),
                patch.object(
                    update_skills,
                    "current_files",
                    side_effect=lambda name: {
                        "SKILL.md": f"---\nname: {name}\ndescription: Old.\n---\n".encode()
                    },
                ),
                patch.object(
                    update_skills,
                    "read_source",
                    side_effect=lambda source, ref, path: licenses[source],
                ),
                patch.object(update_skills, "marketplace_text", return_value="{}\n"),
            ):
                report = update_skills.update_catalog(update_skills.load_catalog())
            after = catalog_path.read_text(encoding="utf-8")
            self.assertEqual(report["clean"], {moved: {"ref": new_ref, "skills": moved_skills}})
            self.assertEqual(report["written"], [moved])
            self.assertEqual(
                [
                    line
                    for line in difflib.ndiff(before.splitlines(), after.splitlines())
                    if line.startswith(("- ", "+ "))
                ],
                [f'-       "ref": "{sources[moved]["ref"]}"', f'+       "ref": "{new_ref}"'],
            )
            for name in moved_skills:
                self.assertEqual(
                    (root / "skills" / name / "SKILL.md").read_bytes(), rendered[name]["SKILL.md"]
                )

    def test_source_paths_reject_traversal_and_absolute_paths(self):
        for path in ("../outside", "/absolute/path", "skills/../../outside", "skills\\..\\outside"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_relative_path(path)

    def test_tree_selection_requires_root_skill_and_regular_files(self):
        tree = {
            "tree": [
                {"path": "repo/skills/example", "type": "tree", "mode": "040000"},
                {"path": "repo/skills/example/SKILL.md", "type": "blob", "mode": "100644"},
                {"path": "repo/skills/example/references", "type": "tree", "mode": "040000"},
                {
                    "path": "repo/skills/example/references/guide.md",
                    "type": "blob",
                    "mode": "100644",
                },
            ]
        }
        selected = skill_files(tree, "repo/skills/example")
        self.assertEqual(
            [item["path"] for item in selected],
            [
                "repo/skills/example/SKILL.md",
                "repo/skills/example/references/guide.md",
            ],
        )
        tree["tree"].append({"path": "repo/skills/example/link", "type": "blob", "mode": "120000"})
        with self.assertRaisesRegex(ValueError, "symlink"):
            skill_files(tree, "repo/skills/example")

    def test_skill_frontmatter_name_must_match_catalog_entry(self):
        content = b"---\nname: other\ndescription: Example skill.\n---\n"
        with self.assertRaisesRegex(ValueError, "name changed"):
            validate_skill("expected", {"SKILL.md": content})

    def test_empty_descriptions_are_rejected(self):
        for front in ('description: ""\n', "description: >\n", "description:\nlicense: MIT\n"):
            with self.subTest(front=front):
                files = {"SKILL.md": f"---\nname: demo\n{front}---\n\nBody.\n".encode()}
                with self.assertRaisesRegex(ValueError, "description is missing or empty"):
                    validate_skill("demo", files)

    def test_valid_quoted_skill_names_are_accepted_without_rewriting(self):
        for quote in ('"', "'"):
            content = (
                f"---\nname: {quote}review{quote}\ndescription: Review changes.\n---\n".encode()
            )
            with self.subTest(quote=quote):
                try:
                    validate_skill("review", {"SKILL.md": content})
                except ValueError as error:
                    self.fail(f"valid YAML quoting was rejected: {error}")

    def render(self, name, entry, files):
        renderer = getattr(update_skills, "render_skill", None)
        self.assertTrue(
            callable(renderer), "catalog imports need deterministic skill transformations"
        )
        return renderer(name, entry, files)

    def test_renamed_skill_preserves_upstream_body_and_supporting_files(self):
        files = {
            "SKILL.md": b"---\nname: code-review\ndescription: Review changes.\n---\n\nReview the diff.\n",
            "references/guide.md": b"Reference text.\n",
        }
        result = self.render("sentry-code-review", {"upstreamName": "code-review"}, files)
        self.assertEqual(
            result["SKILL.md"],
            files["SKILL.md"].replace(b"name: code-review\n", b"name: sentry-code-review\n"),
        )
        self.assertEqual(result["references/guide.md"], files["references/guide.md"])
        self.assertIn(b"name: code-review\n", files["SKILL.md"])

    def test_renamed_skill_is_also_distinct_in_codex_discovery(self):
        files = {
            "SKILL.md": b"---\nname: code-review\ndescription: Review changes.\n---\n",
            "agents/openai.yaml": b'interface:\n  display_name: "Code Review"\n  short_description: "Review standards and spec"\n',
        }
        result = self.render("standards-spec-review", {"upstreamName": "code-review"}, files)
        self.assertIn(b'display_name: "standards-spec-review"', result["agents/openai.yaml"])
        self.assertIn(
            b'short_description: "Review standards and spec"', result["agents/openai.yaml"]
        )

    def test_manual_skill_is_not_automatically_invoked(self):
        files = {"SKILL.md": b"---\nname: review\ndescription: Review changes.\n---\n\nBody.\n"}
        result = self.render("review", {"manual": True}, files)
        self.assertIn(b"disable-model-invocation: true\n---\n", result["SKILL.md"])
        self.assertEqual(result["SKILL.md"].count(b"disable-model-invocation:"), 1)

    def test_manual_override_replaces_existing_frontmatter_value(self):
        files = {
            "SKILL.md": b"---\nname: review\ndescription: Review changes.\ndisable-model-invocation: false\n---\n\nBody.\n"
        }
        result = self.render("review", {"manual": True}, files)
        self.assertNotIn(b"disable-model-invocation: false", result["SKILL.md"])
        self.assertEqual(result["SKILL.md"].count(b"disable-model-invocation:"), 1)

    def test_manual_policy_is_enforced_in_codex_too(self):
        files = {"SKILL.md": b"---\nname: review\ndescription: Review changes.\n---\n"}
        result = self.render("review", {"manual": True}, files)
        self.assertIn(b"allow_implicit_invocation: false", result.get("agents/openai.yaml", b""))

    def test_upstream_manual_policy_is_preserved_for_codex_discovery(self):
        files = {
            "SKILL.md": b"---\nname: review\ndescription: Review changes.\ndisable-model-invocation: true\n---\n"
        }
        result = self.render("review", {}, files)
        self.assertIn(b"allow_implicit_invocation: false", result.get("agents/openai.yaml", b""))

    def test_manual_policy_preserves_other_codex_metadata(self):
        files = {
            "SKILL.md": b"---\nname: review\ndescription: Review changes.\n---\n",
            "agents/openai.yaml": b'interface:\n  display_name: "Review"\npolicy:\n  allow_implicit_invocation: true\n  another_setting: keep\n',
        }
        result = self.render("review", {"manual": True}, files)
        self.assertIn(b"allow_implicit_invocation: false", result["agents/openai.yaml"])
        self.assertIn(b"another_setting: keep", result["agents/openai.yaml"])
        self.assertIn(b'display_name: "Review"', result["agents/openai.yaml"])

    def test_global_skill_references_do_not_require_an_uninstalled_plugin(self):
        files = {
            "SKILL.md": b"---\nname: planning\ndescription: Plan changes.\n---\n\nUse superpowers:writing-plans.\n",
            "references/guide.md": b"Read superpowers:test-driven-development.\n",
            "scripts/helper.sh": b"echo superpowers:unchanged\n",
        }
        result = self.render("planning", {"replaceMarkdown": {"superpowers:": ""}}, files)
        self.assertIn(b"Use writing-plans.", result["SKILL.md"])
        self.assertEqual(result["references/guide.md"], b"Read test-driven-development.\n")
        self.assertEqual(result["scripts/helper.sh"], files["scripts/helper.sh"])

    def test_unmodified_skill_is_byte_identical(self):
        files = {"SKILL.md": b"---\nname: review\ndescription: Review changes.\n---\n\nBody.\n"}
        self.assertEqual(self.render("review", {}, files), files)

    def test_root_level_upstream_skill_is_supported(self):
        tree = {"tree": [{"path": "SKILL.md", "type": "blob", "mode": "100644"}]}
        try:
            selected = skill_files(tree, ".")
        except ValueError as error:
            self.fail(f"root-level skills cannot be imported: {error}")
        self.assertEqual(selected, tree["tree"])

    def test_vendor_installs_new_skill_with_its_recorded_alias(self):
        vendor = getattr(update_skills, "vendor_catalog", None)
        self.assertTrue(callable(vendor), "new catalog entries need a pinned import path")
        files = {
            "SKILL.md": b"---\nname: code-review\ndescription: Review changes.\n---\n\nReview.\n"
        }
        catalog = {
            "skills": {
                "sentry-code-review": {
                    "source": "example/skills",
                    "upstreamPath": "skills/code-review",
                    "upstreamName": "code-review",
                }
            },
            "sources": {
                "example/skills": {
                    "ref": "a" * 40,
                    "licensePath": "LICENSE",
                    "licenseFile": "licenses/example-LICENSE",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(update_skills, "ROOT", root),
                patch.object(update_skills, "fetch_skill", return_value=files),
                patch.object(update_skills, "read_source", return_value=b"License notice.\n"),
            ):
                vendor(catalog)
            self.assertIn(
                b"name: sentry-code-review\n",
                (root / "skills/sentry-code-review/SKILL.md").read_bytes(),
            )
            self.assertEqual((root / "licenses/example-LICENSE").read_bytes(), b"License notice.\n")

    def test_vendor_rewrites_catalog_in_the_tool_layout(self):
        catalog = {
            "schemaVersion": 1,
            "skills": {"helper": {"upstreamPath": "skills/helper", "source": "example/skills"}},
            "sources": {
                "example/skills": {
                    "ref": "a" * 40,
                    "licensePath": "LICENSE",
                    "licenseFile": "licenses/example-LICENSE",
                }
            },
        }
        files = {"SKILL.md": b"---\nname: helper\ndescription: Run the helper.\n---\n"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            with (
                patch.object(update_skills, "ROOT", root),
                patch.object(update_skills, "fetch_skill", return_value=files),
                patch.object(update_skills, "read_source", return_value=b"License.\n"),
            ):
                update_skills.vendor_catalog(catalog)
            self.assertEqual(
                (root / "catalog.json").read_text(encoding="utf-8"),
                "{\n"
                '  "schemaVersion": 1,\n'
                '  "skills": {\n'
                '    "helper": {\n'
                '      "source": "example/skills",\n'
                '      "upstreamPath": "skills/helper"\n'
                "    }\n"
                "  },\n"
                '  "sources": {\n'
                '    "example/skills": {\n'
                '      "licenseFile": "licenses/example-LICENSE",\n'
                '      "licensePath": "LICENSE",\n'
                f'      "ref": "{"a" * 40}"\n'
                "    }\n"
                "  }\n"
                "}\n",
            )

    def test_vendor_refuses_to_overwrite_a_concurrent_catalog_edit(self):
        catalog = {
            "schemaVersion": 1,
            "skills": {"helper": {"source": "example/skills", "upstreamPath": "skills/helper"}},
            "sources": {
                "example/skills": {
                    "ref": "a" * 40,
                    "licensePath": "LICENSE",
                    "licenseFile": "licenses/example-LICENSE",
                }
            },
        }
        files = {"SKILL.md": b"---\nname: helper\ndescription: Run the helper.\n---\n"}
        edited = '{"edited": "while vendoring"}\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

            def fetch(*arguments, **keywords):
                catalog_path.write_text(edited, encoding="utf-8")
                return files

            with (
                patch.object(update_skills, "ROOT", root),
                patch.object(update_skills, "fetch_skill", side_effect=fetch),
                patch.object(update_skills, "read_source", return_value=b"License.\n"),
                self.assertRaisesRegex(ValueError, "catalog.json changed"),
            ):
                update_skills.vendor_catalog(catalog)
            self.assertEqual(catalog_path.read_text(encoding="utf-8"), edited)
            self.assertFalse((root / "skills/helper").exists())

    def archive_bytes(self, files):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path, data in files.items():
                info = tarfile.TarInfo(f"example-skills-abc1234/{path}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def served(self, data):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "archive.tar.gz"
        path.write_bytes(data)
        return ["cat", str(path)]

    def archive_fixture(self, recorded, archived):
        def blob(data):
            return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

        tree = {
            "tree": [
                {"path": path, "type": "blob", "mode": "100644", "sha": blob(data)}
                for path, data in recorded.items()
            ]
        }
        return tree, self.served(self.archive_bytes(archived))

    def fetch_from_archive(self, tree, command, read_source):
        with (
            patch.dict(update_skills.ARCHIVES, clear=True),
            patch.object(update_skills, "archive_command", return_value=command) as download,
            patch.object(update_skills, "read_source", side_effect=read_source),
            contextlib.redirect_stderr(io.StringIO()) as warnings,
        ):
            files = update_skills.fetch_skill(
                "example/skills",
                "a" * 40,
                "skills/docs",
                "docs",
                {("example/skills", "a" * 40): tree},
                archive_paths=("skills/docs/",),
            )
        return files, download.call_count, warnings.getvalue()

    def download(self, command, paths=("skills/docs/",)):
        with patch.object(update_skills, "archive_command", return_value=command):
            return update_skills.download_archive("example/skills", "a" * 40, paths)

    def test_skill_files_come_from_one_archive_download(self):
        files = {
            "skills/docs/SKILL.md": b"---\nname: docs\ndescription: Write docs.\n---\n",
            "skills/docs/guide.md": b"Guide.\n",
        }
        tree, command = self.archive_fixture(files, {**files, "README.md": b"Readme.\n"})
        fetched, downloads, warnings = self.fetch_from_archive(
            tree, command, AssertionError("a file was fetched on its own")
        )
        self.assertEqual(
            fetched, {path.removeprefix("skills/docs/"): data for path, data in files.items()}
        )
        self.assertEqual(downloads, 1)
        self.assertEqual(warnings, "")

    def test_archive_files_that_differ_from_the_recorded_blob_are_fetched_on_their_own(self):
        skill = b"---\nname: docs\ndescription: Write docs.\n---\n"
        recorded = {"skills/docs/SKILL.md": skill, "skills/docs/guide.md": b"Recorded guide.\n"}
        archived = {"skills/docs/SKILL.md": skill, "skills/docs/guide.md": b"Rewritten guide.\n"}
        tree, command = self.archive_fixture(recorded, archived)
        read = []

        def read_source(repository, ref, path):
            read.append(path)
            return recorded[path]

        fetched, _, warnings = self.fetch_from_archive(tree, command, read_source)
        self.assertEqual(fetched["guide.md"], b"Recorded guide.\n")
        self.assertEqual(read, ["skills/docs/guide.md"])
        self.assertEqual(
            warnings,
            "example/skills: skills/docs/guide.md does not match its blob in the archive; "
            "fetching it on its own.\n",
        )

    def test_files_missing_from_the_archive_are_reported_and_fetched_on_their_own(self):
        skill = b"---\nname: docs\ndescription: Write docs.\n---\n"
        recorded = {"skills/docs/SKILL.md": skill, "skills/docs/guide.md": b"Guide.\n"}
        tree, command = self.archive_fixture(recorded, {"skills/docs/SKILL.md": skill})
        fetched, _, warnings = self.fetch_from_archive(
            tree, command, lambda repository, ref, path: recorded[path]
        )
        self.assertEqual(fetched["guide.md"], b"Guide.\n")
        self.assertIn("skills/docs/guide.md is missing from the archive", warnings)

    def test_files_fetched_on_their_own_must_match_their_recorded_blob(self):
        skill = b"---\nname: docs\ndescription: Write docs.\n---\n"
        recorded = {"skills/docs/SKILL.md": skill, "skills/docs/guide.md": b"Recorded guide.\n"}
        tree, command = self.archive_fixture(recorded, {"skills/docs/SKILL.md": skill})
        with self.assertRaisesRegex(
            ValueError, "guide.md does not match the blob recorded at a{40}"
        ):
            self.fetch_from_archive(
                tree,
                command,
                lambda repository, ref, path: (
                    b"Served guide.\n" if path.endswith("guide.md") else skill
                ),
            )

    def test_a_failed_archive_download_falls_back_to_fetching_each_file(self):
        files = {
            "skills/docs/SKILL.md": b"---\nname: docs\ndescription: Write docs.\n---\n",
            "skills/docs/guide.md": b"Guide.\n",
        }
        tree, _ = self.archive_fixture(files, {})
        fetched, downloads, warnings = self.fetch_from_archive(
            tree,
            ["sh", "-c", "echo 'gh: Server Error (HTTP 502)' >&2; exit 1"],
            lambda repository, ref, path: files[path],
        )
        self.assertEqual(fetched["guide.md"], b"Guide.\n")
        self.assertEqual(downloads, 1)
        self.assertEqual(
            warnings,
            "Could not use the example/skills archive at aaaaaaaaaaaa "
            "(gh: Server Error (HTTP 502)); fetching its files one at a time.\n",
        )

    def test_a_corrupt_archive_falls_back_to_fetching_each_file(self):
        files = {
            "skills/docs/SKILL.md": b"---\nname: docs\ndescription: Write docs.\n---\n",
            "skills/docs/guide.md": b"Guide line.\n" * 9000,
        }
        tree, _ = self.archive_fixture(files, {})
        tar = gzip.decompress(self.archive_bytes(files))
        stored = bytearray(gzip.compress(tar, compresslevel=0))
        second_block = 15 + int.from_bytes(stored[11:13], "little")
        stored[second_block + 3] ^= 0xFF
        read = []

        def read_source(repository, ref, path):
            read.append(path)
            return files[path]

        fetched, _, warnings = self.fetch_from_archive(
            tree, self.served(bytes(stored)), read_source
        )
        self.assertEqual(fetched["guide.md"], files["skills/docs/guide.md"])
        self.assertEqual(sorted(read), sorted(files))
        self.assertIn("(invalid compressed data); fetching its files one at a time.", warnings)

    def test_archive_entries_that_are_not_regular_files_are_ignored(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo("example-skills-abc1234/skills/docs/SKILL.md")
            info.size = len(b"Skill.\n")
            archive.addfile(info, io.BytesIO(b"Skill.\n"))
            for name, kind in (
                ("link.md", tarfile.SYMTYPE),
                ("hardlink.md", tarfile.LNKTYPE),
                ("folder", tarfile.DIRTYPE),
                ("pipe", tarfile.FIFOTYPE),
            ):
                info = tarfile.TarInfo(f"example-skills-abc1234/skills/docs/{name}")
                info.type = kind
                info.linkname = "example-skills-abc1234/skills/docs/SKILL.md"
                archive.addfile(info)
        self.assertEqual(
            self.download(self.served(buffer.getvalue())), {"skills/docs/SKILL.md": b"Skill.\n"}
        )

    def test_an_empty_upstream_file_is_still_rejected(self):
        files = {
            "skills/docs/SKILL.md": b"---\nname: docs\ndescription: Write docs.\n---\n",
            "skills/docs/empty.md": b"",
        }
        tree, command = self.archive_fixture(files, files)
        with (
            patch.object(update_skills, "request", return_value=b""),
            self.assertRaisesRegex(
                ValueError, "empty, or oversized upstream file: example/skills/skills/docs/empty.md"
            ),
        ):
            self.fetch_from_archive(tree, command, update_skills.read_source)

    def test_license_notices_come_from_the_same_archive(self):
        notice = b"MIT License.\n"
        tree, _ = self.archive_fixture({"LICENSE": notice}, {})
        key = ("example/skills", "a" * 40, ("LICENSE",))
        with (
            patch.dict(update_skills.ARCHIVES, {key: {"LICENSE": notice}}),
            patch.object(update_skills, "read_source", side_effect=AssertionError("fetched alone")),
        ):
            self.assertEqual(
                update_skills.read_license(
                    "example/skills",
                    "a" * 40,
                    "LICENSE",
                    {("example/skills", "a" * 40): tree},
                    archive_paths=("LICENSE",),
                ),
                notice,
            )

    def test_archives_keep_only_the_skill_folders_and_license_the_catalog_uses(self):
        catalog = {
            "skills": {
                "docs": {"source": "example/skills", "upstreamPath": "skills/docs"},
                "humanizer": {"source": "example/root", "upstreamPath": "."},
                "tools": {"source": "example/skills", "upstreamPath": "skills/tools/"},
            },
            "sources": {
                "example/root": {"licensePath": "COPYING"},
                "example/skills": {"licensePath": "LICENSE"},
            },
        }
        paths = update_skills.catalog_paths(catalog, "example/skills")
        self.assertEqual(paths, ("skills/docs/", "skills/tools/", "LICENSE"))
        self.assertEqual(update_skills.catalog_paths(catalog, "example/root"), ("", "COPYING"))
        archive = {
            "skills/docs/SKILL.md": b"Docs.\n",
            "skills/other/SKILL.md": b"Other.\n",
            "LICENSE": b"MIT License.\n",
            "assets/video.mp4": b"Video.\n",
        }
        self.assertEqual(
            self.download(self.served(self.archive_bytes(archive)), paths),
            {"skills/docs/SKILL.md": b"Docs.\n", "LICENSE": b"MIT License.\n"},
        )

    def test_an_archive_download_stops_at_its_size_cap(self):
        archive = self.archive_bytes({"assets/noise.bin": os.urandom(50_000)})
        with (
            patch.object(update_skills, "MAX_ARCHIVE_BYTES", 20_000),
            self.assertRaisesRegex(ValueError, "download exceeds 20000 bytes"),
        ):
            self.download(self.served(archive))

    def test_files_kept_from_an_archive_are_capped(self):
        archive = self.archive_bytes(
            {f"skills/docs/part{index}.md": b"\0" * 40_000 for index in range(3)}
        )
        with (
            patch.object(update_skills, "MAX_ARCHIVE_BYTES", 100_000),
            self.assertRaisesRegex(ValueError, "files kept from the archive exceed"),
        ):
            self.download(self.served(archive))

    def test_only_tree_lookups_by_commit_are_cached(self):
        commands = []

        def run(arguments, **options):
            commands.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, b"{}", b"")

        with patch.object(update_skills.subprocess, "run", side_effect=run):
            for endpoint in (
                f"repos/example/skills/git/trees/{'a' * 40}?recursive=1",
                "repos/git/trees/commits/main",
                "repos/example/skills/git/trees/main?recursive=1",
            ):
                update_skills.request(f"{update_skills.API}/{endpoint}")
        self.assertEqual(["--cache" in command for command in commands], [True, False, False])

    def test_a_stalled_archive_download_times_out(self):
        started = time.monotonic()
        with (
            patch.object(update_skills, "ARCHIVE_TIMEOUT_SECONDS", 0.2),
            self.assertRaisesRegex(ValueError, "took longer than 0.2 seconds"),
        ):
            self.download(["sleep", "30"])
        self.assertLess(time.monotonic() - started, 5)

    def test_import_excludes_nonessential_gallery_before_downloading(self):
        tree = {
            "tree": [
                {"path": "skills/image/SKILL.md", "type": "blob", "mode": "100644"},
                {"path": "skills/image/gallery/demo.png", "type": "blob", "mode": "100644"},
            ]
        }
        content = b"---\nname: image\ndescription: Generate an image.\n---\n"

        def read_source(repository, ref, path):
            self.assertEqual(path, "skills/image/SKILL.md")
            return content

        with patch.object(update_skills, "read_source", side_effect=read_source):
            try:
                files = update_skills.fetch_skill(
                    "example/skills",
                    "a" * 40,
                    "skills/image",
                    "image",
                    {("example/skills", "a" * 40): tree},
                    exclude=["gallery/*"],
                    archive_paths=(),
                )
            except TypeError as error:
                self.fail(f"selective upstream imports are not supported: {error}")
        self.assertEqual(files, {"SKILL.md": content})

    def test_authored_skill_does_not_require_a_fake_upstream_commit(self):
        catalog = {"schemaVersion": 1, "sources": {}, "skills": {"my-workflow": {"local": True}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(catalog))
            with patch.object(update_skills, "CATALOG_PATH", path):
                try:
                    loaded = update_skills.load_catalog()
                except ValueError as error:
                    self.fail(f"authored skills are not supported: {error}")
        self.assertEqual(loaded["skills"], {"my-workflow": {"local": True}})

    def test_previous_names_must_be_unique_skill_id_lists(self):
        for aliases in ("code-review", ["code-review", "code-review"], ["../outside"], ["review"]):
            catalog = {
                "schemaVersion": 1,
                "sources": {},
                "skills": {"review": {"local": True, "previousNames": aliases}},
            }
            with self.subTest(aliases=aliases), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "catalog.json"
                path.write_text(json.dumps(catalog))
                with (
                    patch.object(update_skills, "CATALOG_PATH", path),
                    self.assertRaisesRegex(ValueError, "previousNames"),
                ):
                    update_skills.load_catalog()

    def test_vendor_never_replaces_an_authored_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "skills/my-workflow"
            folder.mkdir(parents=True)
            text = b"---\nname: my-workflow\ndescription: My personal workflow.\n---\n\nAuthored content.\n"
            (folder / "SKILL.md").write_bytes(text)
            with patch.object(update_skills, "ROOT", root):
                try:
                    update_skills.vendor_catalog(
                        {"sources": {}, "skills": {"my-workflow": {"local": True}}}
                    )
                except KeyError as error:
                    self.fail(f"authored skill was treated as an upstream import: {error}")
            self.assertEqual((folder / "SKILL.md").read_bytes(), text)

    @unittest.skipIf(os.name == "nt", "POSIX executable mode")
    def test_vendor_preserves_executable_companions(self):
        catalog = {
            "skills": {"helper": {"source": "example/skills", "upstreamPath": "skills/helper"}},
            "sources": {
                "example/skills": {
                    "ref": "a" * 40,
                    "licensePath": "LICENSE",
                    "licenseFile": "licenses/example-LICENSE",
                }
            },
        }
        files = {
            "SKILL.md": b"---\nname: helper\ndescription: Run the helper.\n---\n",
            "scripts/run": b"#!/bin/sh\nprintf 'companion ran\\n'\n",
        }

        def fetch(repository, ref, source_path, name, cache, **kwargs):
            cache[(repository, ref)] = {
                "tree": [{"path": "skills/helper/scripts/run", "type": "blob", "mode": "100755"}]
            }
            return files

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(update_skills, "ROOT", root),
                patch.object(update_skills, "fetch_skill", side_effect=fetch),
                patch.object(update_skills, "read_source", return_value=b"License.\n"),
            ):
                update_skills.vendor_catalog(catalog)
            try:
                result = subprocess.run(
                    [str(root / "skills/helper/scripts/run")],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except PermissionError as error:
                self.fail(f"upstream executable mode was lost: {error}")
            self.assertEqual(result.stdout, "companion ran\n")

    def test_vendor_refuses_symlinked_repository_ancestors(self):
        for ancestor in ("skills", "licenses"):
            with self.subTest(ancestor=ancestor), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root, outside = base / "repository", base / "outside"
                root.mkdir()
                outside.mkdir()
                (outside / "original").write_text("User work.")
                (root / ancestor).symlink_to(outside, target_is_directory=True)
                catalog = {
                    "skills": {
                        "helper": {"source": "example/skills", "upstreamPath": "skills/helper"}
                    },
                    "sources": {
                        "example/skills": {
                            "ref": "a" * 40,
                            "licensePath": "LICENSE",
                            "licenseFile": "licenses/example-LICENSE",
                        }
                    },
                }
                files = {"SKILL.md": b"---\nname: helper\ndescription: Run the helper.\n---\n"}
                with (
                    patch.object(update_skills, "ROOT", root),
                    patch.object(update_skills, "fetch_skill", return_value=files),
                    patch.object(update_skills, "read_source", return_value=b"License.\n"),
                    self.assertRaisesRegex(ValueError, "symlink|outside"),
                ):
                    update_skills.vendor_catalog(catalog)
                self.assertEqual((outside / "original").read_text(), "User work.")
                self.assertEqual({path.name for path in outside.iterdir()}, {"original"})


if __name__ == "__main__":
    unittest.main()
