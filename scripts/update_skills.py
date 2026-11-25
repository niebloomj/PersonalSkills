#!/usr/bin/env python3
"""Check selected upstream skill paths and safely refresh their vendored copies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import IO, Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "catalog.json"
MARKETPLACE_FILE = ".claude-plugin/marketplace.json"
MARKETPLACE = {
    "$schema": "https://www.schemastore.org/claude-code-marketplace.json",
    "name": "niebloomj-skills",
    "description": "Reviewed agent skills, one plugin per skill, pinned to upstream commits.",
    "owner": {"name": "niebloomj", "url": "https://github.com/niebloomj"},
}
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
MAX_FILE_BYTES = 5_000_000
MAX_SKILL_BYTES = 20_000_000
MAX_ACTIVITY_PAGES = 10
MAX_ARCHIVE_BYTES = 200_000_000
ARCHIVE_CHUNK_BYTES = 65_536
ARCHIVE_TIMEOUT_SECONDS = 300
GIT_TIMEOUT_SECONDS = 60
ARCHIVES: dict[tuple[str, str, tuple[str, ...]], dict[str, bytes]] = {}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ZERO_SHA = "0" * 40
NEXT_PAGE = re.compile(r'<([^>]+)>;\s*rel="next"')
AUTOMATIC_SUFFIXES = {".md", ".txt"}
LICENSE_NAME = re.compile(r"(?i)^(?:licen[cs]e|copying|notice)(?:[.-]|$)")
INSTRUCTION_FILES = {"agents.md", "claude.md", "claude.local.md", "gemini.md"}
GRANTING_SETTINGS = {"allowed-tools", "hooks"}
SHELL_SYNTAX = ("!`", "```!")
UNSAFE_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}
ROOT_PLUGIN_COMPONENTS = (
    ".lsp.json",
    ".mcp.json",
    "agents",
    "bin",
    "commands",
    "hooks",
    "monitors",
    "output-styles",
    "themes",
    "workflows",
)


def request(url: str, *, include_headers: bool = False) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname == "api.github.com":
        endpoint = parsed.path.lstrip("/") + ("?" + parsed.query if parsed.query else "")
        arguments = ["gh", "api", endpoint]
        if include_headers:
            arguments.append("--include")
        if "/git/trees/" in endpoint:
            arguments.extend(["--cache", "24h"])
    elif parsed.hostname == "raw.githubusercontent.com":
        owner, repository, ref, path = parsed.path.lstrip("/").split("/", 3)
        arguments = [
            "gh",
            "api",
            f"repos/{owner}/{repository}/contents/{path}?ref={ref}",
            "-H",
            "Accept: application/vnd.github.raw+json",
            "--cache",
            "24h",
        ]
    else:
        raise ValueError(f"Unsupported upstream host: {parsed.hostname}")
    result = subprocess.run(arguments, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def json_request(url: str) -> Any:
    payload = request(url)
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError(f"GitHub API response exceeds {MAX_FILE_BYTES} bytes: {url}")
    return json.loads(payload)


def json_pages(url: str, max_pages: int) -> Iterator[Any]:
    for _ in range(max_pages):
        payload = request(url, include_headers=True)
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError(f"GitHub API response exceeds {MAX_FILE_BYTES} bytes: {url}")
        head, _, body = payload.replace(b"\r\n", b"\n").partition(b"\n\n")
        yield json.loads(body)
        following = NEXT_PAGE.search(head.decode("utf-8", errors="replace"))
        if following is None:
            return
        url = following[1]
    raise ValueError(f"GitHub returned more than {max_pages} pages: {url}")


def safe_relative_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if (
        "\\" in raw
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"Unsafe repository path: {raw!r}")
    return path


def repository_path(relative: str, root: Path | None = None) -> Path:
    project = ROOT if root is None else root
    path = project
    for part in safe_relative_path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Repository paths cannot contain a symlink: {path}")
    if not path.resolve().is_relative_to(project.resolve()):
        raise ValueError(f"Repository path points outside the checkout: {path}")
    return path


def source_prefix(source_path: str) -> str:
    return (
        "" if source_path == "." else safe_relative_path(source_path).as_posix().rstrip("/") + "/"
    )


def skill_files(tree: dict[str, Any], source_path: str) -> list[dict[str, Any]]:
    prefix = source_prefix(source_path)
    entries = [entry for entry in tree["tree"] if entry["path"].startswith(prefix)]
    if not entries:
        raise ValueError(f"Upstream skill directory not found: {source_path}")
    if not any(entry["path"] == prefix + "SKILL.md" for entry in entries):
        raise ValueError(f"Upstream skill has no root SKILL.md: {source_path}")
    if any(
        entry["type"] not in {"blob", "tree"}
        or (entry["type"] == "blob" and entry.get("mode") not in {"100644", "100755"})
        or (entry["type"] == "tree" and entry.get("mode") != "040000")
        for entry in entries
    ):
        raise ValueError(
            f"Upstream skill contains a symlink, submodule, or non-regular file: {source_path}"
        )
    return sorted(
        (entry for entry in entries if entry["type"] == "blob"), key=lambda entry: entry["path"]
    )


def path_key(path: str) -> str:
    return unicodedata.normalize("NFKC", path).casefold()


def validate_skill(name: str, files: dict[str, bytes]) -> None:
    seen: dict[str, str] = {}
    for path in sorted(files):
        if any(unicodedata.category(character) in UNSAFE_CATEGORIES for character in path):
            raise ValueError(f"{name}: unsafe characters in path {path!r}")
        parts = PurePosixPath(path).parts
        for depth in range(1, len(parts) + 1):
            node = "/".join(parts[:depth])
            if (other := seen.setdefault(path_key(node), node)) != node:
                raise ValueError(f"{name}: {other} and {node} differ only in case or Unicode form")
    content = files.get("SKILL.md")
    if content is None:
        raise ValueError(f"{name}: SKILL.md is missing")
    text = content.decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"{name}: SKILL.md has no valid YAML frontmatter block")
    frontmatter = text[4 : text.index("\n---\n", 4)]
    skill_name = re.search(
        r"^name:\s*(?P<quote>['\"]?)(?P<name>[A-Za-z0-9_-]+)(?P=quote)\s*$",
        frontmatter,
        re.MULTILINE,
    )
    if not skill_name or skill_name.group("name") != name:
        raise ValueError(f"{name}: upstream skill name changed or does not match the catalog")
    if not skill_description(content):
        raise ValueError(f"{name}: SKILL.md description is missing or empty")
    for path, data in files.items():
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{name}/{path}: file exceeds {MAX_FILE_BYTES} bytes")
        if b"\0" in data:
            raise ValueError(f"{name}/{path}: binary content is not permitted")
        data.decode("utf-8")


def codex_manual_policy(content: bytes) -> bytes:
    text = content.decode("utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    block = re.search(r"(?m)^policy:[ \t]*\n(?P<body>(?:[ \t]+[^\n]*\n|[ \t]*\n)*)", text)
    if block is None:
        if re.search(r"(?m)^policy:", text):
            raise ValueError("Unsupported Codex policy syntax; review the upstream metadata")
        return (text + "policy:\n  allow_implicit_invocation: false\n").encode("utf-8")
    body = block["body"]
    if re.search(r"(?m)^[ \t]+allow_implicit_invocation:", body):
        body, count = re.subn(
            r"(?m)^([ \t]+allow_implicit_invocation:[ \t]*)(?:true|false)([ \t]*(?:#.*)?)$",
            r"\g<1>false\2",
            body,
        )
        if count != 1:
            raise ValueError("Ambiguous Codex invocation policy; review the upstream metadata")
    else:
        body = "  allow_implicit_invocation: false\n" + body
    return (text[: block.start("body")] + body + text[block.end("body") :]).encode("utf-8")


def render_skill(name: str, entry: dict[str, Any], files: dict[str, bytes]) -> dict[str, bytes]:
    upstream_name = entry.get("upstreamName", name)
    validate_skill(upstream_name, files)
    rendered = dict(files)
    if name != upstream_name or entry.get("manual"):
        text = rendered["SKILL.md"].decode("utf-8")
        boundary = text.index("\n---\n", 4)
        frontmatter = text[4:boundary]
        if name != upstream_name:
            frontmatter = re.sub(
                r"^name:.*$", f"name: {name}", frontmatter, count=1, flags=re.MULTILINE
            )
        if entry.get("manual"):
            if re.search(r"^disable-model-invocation:", frontmatter, re.MULTILINE):
                frontmatter = re.sub(
                    r"^disable-model-invocation:.*$",
                    "disable-model-invocation: true",
                    frontmatter,
                    flags=re.MULTILINE,
                )
            else:
                frontmatter += "\ndisable-model-invocation: true"
        rendered["SKILL.md"] = ("---\n" + frontmatter + text[boundary:]).encode("utf-8")
        if name != upstream_name and "agents/openai.yaml" in rendered:
            metadata = rendered["agents/openai.yaml"].decode("utf-8")
            metadata = re.sub(
                r"(?m)^([ \t]*display_name:)[^\n]*$",
                lambda match: match[1] + " " + json.dumps(name),
                metadata,
            )
            rendered["agents/openai.yaml"] = metadata.encode("utf-8")
    for relative, content in rendered.items():
        if relative.endswith(".md"):
            text = content.decode("utf-8")
            for old, new in entry.get("replaceMarkdown", {}).items():
                text = text.replace(old, new)
            rendered[relative] = text.encode("utf-8")
    frontmatter = rendered["SKILL.md"].decode("utf-8").split("\n---\n", 1)[0]
    if re.search(r"(?m)^disable-model-invocation:[ \t]*true[ \t]*$", frontmatter):
        rendered["agents/openai.yaml"] = codex_manual_policy(
            rendered.get("agents/openai.yaml", b"")
        )
    validate_skill(name, rendered)
    return rendered


def catalog_text(catalog: dict[str, Any]) -> str:
    return json.dumps(catalog, indent=2, sort_keys=True) + "\n"


def validate_previous_names(name: str, entry: dict[str, Any]) -> list[str]:
    aliases = entry.get("previousNames", [])
    if not isinstance(aliases, list) or any(
        not isinstance(alias, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", alias)
        or alias == name
        for alias in aliases
    ):
        raise ValueError(f"{name}: previousNames must be a list of distinct, previous skill IDs")
    if len(aliases) != len(set(aliases)):
        raise ValueError(f"{name}: previousNames contains duplicate skill IDs")
    return aliases


def load_catalog() -> dict[str, Any]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if (
        catalog.get("schemaVersion") != 1
        or not isinstance(catalog.get("sources"), dict)
        or not catalog.get("skills")
    ):
        raise ValueError("catalog.json has an unsupported or incomplete schema")
    destinations: set[str] = set()
    for name, skill in catalog["skills"].items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError(f"Invalid skill name: {name}")
        validate_previous_names(name, skill)
        destination = f"skills/{name}"
        if destination in destinations:
            raise ValueError(f"Duplicate destination: {destination}")
        destinations.add(destination)
        if skill.get("local") is True:
            if "source" in skill or "upstreamPath" in skill:
                raise ValueError(f"{name}: authored skills cannot also declare an upstream source")
            continue
        if skill.get("source") not in catalog["sources"]:
            raise ValueError(f"{name}: source is not present in catalog.json")
        source_prefix(skill["upstreamPath"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", skill.get("upstreamName", name)):
            raise ValueError(f"{name}: invalid upstreamName")
        if "manual" in skill and not isinstance(skill["manual"], bool):
            raise ValueError(f"{name}: manual must be a boolean")
        replacements = skill.get("replaceMarkdown", {})
        if not isinstance(replacements, dict) or any(
            not isinstance(old, str) or not old or not isinstance(new, str)
            for old, new in replacements.items()
        ):
            raise ValueError(f"{name}: replaceMarkdown must map nonempty strings to strings")
        excludes = skill.get("exclude", [])
        if not isinstance(excludes, list):
            raise ValueError(f"{name}: exclude must be a list of relative glob patterns")
        for pattern in excludes:
            safe_relative_path(pattern)
    for source, details in catalog["sources"].items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
            raise ValueError(f"Invalid GitHub repository: {source}")
        if not SHA_RE.fullmatch(details.get("ref", "")):
            raise ValueError(f"{source}: ref must be a full immutable commit SHA")
        license_file = safe_relative_path(details["licenseFile"])
        if license_file.parts[0] != "licenses":
            raise ValueError(f"{source}: license must be stored under licenses/")
        safe_relative_path(details["licensePath"])
    return catalog


def ignored_paths(root: Path) -> set[str]:
    def git(*arguments: str) -> subprocess.CompletedProcess[bytes] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *arguments],
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    top = git("rev-parse", "--show-toplevel")
    if top is None or top.returncode:
        return set()
    if Path(top.stdout.decode("utf-8", "surrogateescape").strip()).resolve() != root.resolve():
        return set()
    result = git(
        "ls-files",
        "-z",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "--",
        "skills",
    )
    if result is None or result.returncode:
        return set()
    return {
        entry for entry in result.stdout.decode("utf-8", "surrogateescape").split("\0") if entry
    }


def is_ignored(path: Path, root: Path, ignored: set[str]) -> bool:
    relative = path.relative_to(root).as_posix()
    return relative in ignored or any(
        entry.endswith("/") and f"{relative}/".startswith(entry) for entry in ignored
    )


def validate_local(catalog: dict[str, Any]) -> None:
    ignored = ignored_paths(ROOT)
    expected_dirs = {name for name in catalog["skills"]}
    actual_dirs = {
        path.name
        for path in repository_path("skills").iterdir()
        if path.is_dir() and not is_ignored(path, ROOT, ignored)
    }
    if actual_dirs != expected_dirs:
        raise ValueError(
            f"Catalog folder mismatch; missing={sorted(expected_dirs - actual_dirs)}, extra={sorted(actual_dirs - expected_dirs)}"
        )
    for name, entry in catalog["skills"].items():
        folder = repository_path(f"skills/{name}")
        files: dict[str, bytes] = {}
        for path in folder.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"{name}: symlinks are not permitted in the catalog")
            if is_ignored(path, ROOT, ignored):
                continue
            if path.is_file():
                files[path.relative_to(folder).as_posix()] = path.read_bytes()
        validate_skill(name, files)
    for details in catalog["sources"].values():
        license_file = repository_path(details["licenseFile"])
        if not license_file.is_file() or not license_file.read_bytes().strip():
            raise ValueError(f"Missing or empty license file: {details['licenseFile']}")
    marketplace = repository_path(MARKETPLACE_FILE)
    if not marketplace.is_file() or marketplace.read_text(encoding="utf-8") != marketplace_text(
        catalog
    ):
        raise ValueError(f"{MARKETPLACE_FILE} is out of date; run update_skills.py --vendor")
    if present := [path for path in ROOT_PLUGIN_COMPONENTS if repository_path(path).exists()]:
        raise ValueError(f"Every plugin would load these root components: {', '.join(present)}")


def branch_head_at(repository: str, branch: str, cutoff: datetime, pushed_at: str) -> str | None:
    ref = urllib.parse.quote(f"refs/heads/{branch}", safe="")
    url = f"{API}/repos/{repository}/activity?ref={ref}&per_page=100"
    oldest = None
    for page in json_pages(url, MAX_ACTIVITY_PAGES):
        for entry in page:
            if datetime.fromisoformat(entry["timestamp"]) <= cutoff:
                return entry["after"]
            oldest = entry
    if oldest is not None:
        return oldest["before"]
    if datetime.fromisoformat(pushed_at) > cutoff:
        return None
    quoted = urllib.parse.quote(branch, safe="")
    return json_request(f"{API}/repos/{repository}/commits/{quoted}")["sha"]


def latest_ref(repository: str, min_age_days: int = 0, now: datetime | None = None) -> str | None:
    metadata = json_request(f"{API}/repos/{repository}")
    branch = metadata["default_branch"]
    quoted = urllib.parse.quote(branch, safe="")
    if min_age_days:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=min_age_days)
        sha = branch_head_at(repository, branch, cutoff, metadata["pushed_at"])
    else:
        sha = json_request(f"{API}/repos/{repository}/commits/{quoted}")["sha"]
    if sha is None or sha == ZERO_SHA:
        return None
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ValueError(f"{repository}: GitHub returned an invalid commit SHA")
    if min_age_days:
        comparison = json_request(f"{API}/repos/{repository}/compare/{sha}...{quoted}?per_page=1")
        if comparison["status"] not in {"ahead", "identical"}:
            return None
    return sha


def pin_relation(repository: str, current: str, candidate: str) -> str:
    if current == candidate:
        return "identical"
    comparison = json_request(
        f"{API}/repos/{repository}/compare/{current}...{candidate}?per_page=1"
    )
    return comparison["status"]


def current_executables(name: str) -> set[str]:
    folder = repository_path(f"skills/{name}")
    ignored = ignored_paths(ROOT)
    return {
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.stat().st_mode & 0o111
        and not is_ignored(path, ROOT, ignored)
    }


def upstream_executables(tree: dict[str, Any], source_path: str) -> set[str]:
    prefix = source_prefix(source_path)
    return {
        entry["path"][len(prefix) :]
        for entry in tree.get("tree", [])
        if entry.get("type") == "blob"
        and entry.get("mode") == "100755"
        and entry["path"].startswith(prefix)
    }


def frontmatter(skill: bytes) -> str:
    text = skill.decode("utf-8")
    return text[4 : text.index("\n---\n", 4)]


def top_level_settings(text: str) -> dict[str, list[str]]:
    settings: dict[str, list[str]] = {}
    key = ""
    for line in text.splitlines():
        if line and not line[0].isspace() and not line.startswith(("-", "#")):
            key = line.split(":", 1)[0].strip().strip("\"'")
        settings.setdefault(key, []).append(line)
    return settings


def changed_settings(old: str, new: str, descriptive: set[str]) -> list[str]:
    before, after = top_level_settings(old), top_level_settings(new)
    return sorted(
        key
        for key in before.keys() | after.keys()
        if key not in descriptive and before.get(key) != after.get(key)
    )


def safe_text(text: str) -> bool:
    return "---" not in text and not any(
        character != "\n"
        and (
            unicodedata.category(character) in UNSAFE_CATEGORIES
            or (unicodedata.category(character) == "Zs" and character != " ")
        )
        for character in text
    )


def safe_scalar(value: str) -> bool:
    if not value:
        return False
    if value[0] == '"':
        return re.fullmatch(r'"(?:[^"\\]|\\["\\])*"', value) is not None
    if value[0] == "'":
        return re.fullmatch(r"'(?:[^']|'')*'", value) is not None
    return (
        value[0].isalpha()
        and " " in value
        and ": " not in value
        and " #" not in value
        and not value.endswith(":")
    )


def simple_description(front: str) -> bool:
    lines = top_level_settings(front).get("description", [])
    while lines and lines[-1] == "":
        lines = lines[:-1]
    return (
        safe_text(front)
        and len(lines) == 1
        and lines[0].startswith("description: ")
        and safe_scalar(lines[0].removeprefix("description: ").strip(" "))
    )


def review_reasons(
    name: str,
    old_files: dict[str, bytes],
    new_files: dict[str, bytes],
    old_executables: set[str],
    new_executables: set[str],
) -> list[str]:
    prefix = f"skills/{name}"
    reasons: list[str] = []
    changed = [
        path
        for path in sorted(old_files.keys() | new_files.keys())
        if old_files.get(path) != new_files.get(path)
        or (path in old_executables) != (path in new_executables)
    ]
    if not changed:
        return reasons
    for path in changed:
        data, executable = new_files.get(path), path in new_executables
        pure = PurePosixPath(path)
        if any(part.startswith(".") for part in pure.parts):
            reasons.append(f"{prefix}/{path}: adds, changes, or removes a hidden file")
        elif pure.suffix.lower() not in AUTOMATIC_SUFFIXES:
            reasons.append(
                f"{prefix}/{path}: adds, changes, or removes a file that is not Markdown"
                " or plain text"
            )
        elif executable or (data or b"").startswith(b"#!"):
            reasons.append(f"{prefix}/{path}: adds or changes an executable file")
        elif LICENSE_NAME.match(pure.name):
            reasons.append(f"{prefix}/{path}: changes license text")
        elif path_key(pure.name) in INSTRUCTION_FILES:
            reasons.append(f"{prefix}/{path}: adds or changes an agent instruction file")
        elif path != "SKILL.md" and path_key(pure.name) == "skill.md":
            reasons.append(f"{prefix}/{path}: adds or changes a nested SKILL.md")
    old_skill, new_skill = old_files["SKILL.md"], new_files["SKILL.md"]
    old_front = frontmatter(old_skill)
    if GRANTING_SETTINGS & top_level_settings(old_front).keys():
        reasons.append(
            f"{prefix}: pre-approves tools or defines hooks, so every change needs review"
        )
    if old_skill != new_skill:
        new_front = frontmatter(new_skill)
        settings = changed_settings(old_front, new_front, {"description"})
        if settings:
            reasons.append(
                f"{prefix}/SKILL.md: changes frontmatter settings: {', '.join(settings)}"
            )
        elif new_front != old_front and not simple_description(new_front):
            reasons.append(
                f"{prefix}/SKILL.md: changes the description in a form agents may parse differently"
            )
        if any(token in new_skill.decode("utf-8") for token in SHELL_SYNTAX):
            reasons.append(
                f"{prefix}/SKILL.md: contains shell commands that run when the skill loads"
            )
    return reasons


def skill_description(skill: bytes) -> str:
    lines = top_level_settings(frontmatter(skill)).get("description", [""])
    value = lines[0].partition(":")[2].strip()
    if re.fullmatch(r"[>|][+-]?", value):
        value = " ".join(lines[1:])
    elif value.startswith('"'):
        try:
            value = json.loads(value)
        except ValueError:
            value = value.strip('"')
    elif len(value) > 1 and value[0] == value[-1] == "'":
        value = value[1:-1].replace("''", "'")
    else:
        for line in lines[1:]:
            if line.lstrip().startswith("#"):
                break
            if line[:1].isspace():
                value += " " + line
    return " ".join(value.split())


def marketplace_text(catalog: dict[str, Any], skills: dict[str, bytes] | None = None) -> str:
    plugins = []
    for name in sorted(catalog["skills"]):
        skill = (skills or {}).get(name) or repository_path(f"skills/{name}/SKILL.md").read_bytes()
        plugin: dict[str, Any] = {"name": name, "description": skill_description(skill)}
        if source := catalog["skills"][name].get("source"):
            if license_name := catalog["sources"].get(source, {}).get("license"):
                plugin["license"] = license_name
            plugin["repository"] = f"https://github.com/{source}"
        plugins.append({**plugin, "source": "./", "strict": False, "skills": [f"./skills/{name}"]})
    return json.dumps({**MARKETPLACE, "plugins": plugins}, indent=2) + "\n"


def read_source(repository: str, ref: str, path: str) -> bytes:
    url = f"{RAW}/{repository}/{ref}/{urllib.parse.quote(path, safe='/')}"
    data = request(url)
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Missing, empty, or oversized upstream file: {repository}/{path}")
    return data


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class ArchiveStream:
    def __init__(self, stream: IO[bytes]) -> None:
        self.stream = stream
        self.deadline = time.monotonic() + ARCHIVE_TIMEOUT_SECONDS
        self.remaining = MAX_ARCHIVE_BYTES

    def read(self, size: int) -> bytes:
        wait = self.deadline - time.monotonic()
        if wait <= 0 or not select.select([self.stream], [], [], wait)[0]:
            raise ValueError(f"download took longer than {ARCHIVE_TIMEOUT_SECONDS} seconds")
        data = self.stream.read(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ValueError(f"download exceeds {MAX_ARCHIVE_BYTES} bytes")
        return data


def archive_command(repository: str, ref: str) -> list[str]:
    return ["gh", "api", f"repos/{repository}/tarball/{ref}"]


def unpack_archive(stream: ArchiveStream, paths: tuple[str, ...]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            path = member.name.partition("/")[2]
            if not member.isreg() or not path.startswith(paths) or member.size > MAX_FILE_BYTES:
                continue
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError(f"files kept from the archive exceed {MAX_ARCHIVE_BYTES} bytes")
            handle = archive.extractfile(member)
            if handle is not None:
                files[path] = handle.read()
    return files


def download_archive(repository: str, ref: str, paths: tuple[str, ...]) -> dict[str, bytes]:
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            archive_command(repository, ref), stdout=subprocess.PIPE, stderr=errors, bufsize=0
        )
        try:
            stream = ArchiveStream(process.stdout)
            files = unpack_archive(stream, paths)
            while stream.read(ARCHIVE_CHUNK_BYTES):
                pass
            status = process.wait(timeout=max(stream.deadline - time.monotonic(), 0))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
        if status:
            errors.seek(0)
            detail = errors.read(MAX_FILE_BYTES).decode("utf-8", errors="replace").strip()
            raise ValueError(detail or f"gh exited with status {status}")
    return files


def archive_files(repository: str, ref: str, paths: tuple[str, ...]) -> dict[str, bytes]:
    key = (repository, ref, paths)
    if key not in ARCHIVES:
        try:
            ARCHIVES[key] = download_archive(repository, ref, paths)
        except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
            ARCHIVES[key] = {}
    return ARCHIVES[key]


def catalog_paths(catalog: dict[str, Any], repository: str) -> tuple[str, ...]:
    prefixes = {
        source_prefix(skill["upstreamPath"])
        for skill in catalog["skills"].values()
        if skill.get("source") == repository
    }
    return (*sorted(prefixes), catalog["sources"][repository]["licensePath"])


def read_blob(
    repository: str, ref: str, path: str, blob: str | None, archive_paths: tuple[str, ...]
) -> bytes:
    if blob and archive_paths:
        data = archive_files(repository, ref, archive_paths).get(path)
        if data and git_blob_id(data) == blob:
            return data
    return read_source(repository, ref, path)


def read_license(
    repository: str,
    ref: str,
    path: str,
    tree_cache: dict[tuple[str, str], dict[str, Any]],
    *,
    archive_paths: tuple[str, ...],
) -> bytes:
    entries = tree_cache.get((repository, ref), {}).get("tree", [])
    blob = next(
        (
            entry.get("sha")
            for entry in entries
            if entry.get("path") == path and entry.get("type") == "blob"
        ),
        None,
    )
    return read_blob(repository, ref, path, blob, archive_paths)


def fetch_skill(
    repository: str,
    ref: str,
    source_path: str,
    name: str,
    tree_cache: dict[tuple[str, str], dict[str, Any]],
    exclude: list[str] | None = None,
    *,
    archive_paths: tuple[str, ...],
) -> dict[str, bytes]:
    key = (repository, ref)
    if key not in tree_cache:
        tree = json_request(f"{API}/repos/{repository}/git/trees/{ref}?recursive=1")
        if tree.get("truncated"):
            raise ValueError(f"GitHub returned a truncated repository tree for {repository}")
        tree_cache[key] = tree
    prefix = source_prefix(source_path)
    files: dict[str, bytes] = {}
    total = 0
    for entry in skill_files(tree_cache[key], source_path):
        relative = entry["path"][len(prefix) :]
        safe_relative_path(relative)
        if any(PurePosixPath(relative).match(pattern) for pattern in exclude or []):
            continue
        data = read_blob(repository, ref, entry["path"], entry.get("sha"), archive_paths)
        total += len(data)
        if total > MAX_SKILL_BYTES:
            raise ValueError(f"{name}: skill exceeds {MAX_SKILL_BYTES} bytes")
        files[relative] = data
    validate_skill(name, files)
    return files


def current_files(name: str) -> dict[str, bytes]:
    folder = repository_path(f"skills/{name}")
    files: dict[str, bytes] = {}
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError(f"Missing or unsafe catalog folder: skills/{name}")
    ignored = ignored_paths(ROOT)
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"{name}: symlinks are not permitted in the catalog")
        if is_ignored(path, ROOT, ignored):
            continue
        if path.is_file():
            files[path.relative_to(folder).as_posix()] = path.read_bytes()
    return files


def verify_pins(catalog: dict[str, Any]) -> None:
    tree_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for name, skill in catalog["skills"].items():
        if skill.get("local") is True:
            continue
        source = catalog["sources"][skill["source"]]
        pinned_files = render_skill(
            name,
            skill,
            fetch_skill(
                skill["source"],
                source["ref"],
                skill["upstreamPath"],
                skill.get("upstreamName", name),
                tree_cache,
                exclude=skill.get("exclude"),
                archive_paths=catalog_paths(catalog, skill["source"]),
            ),
        )
        if pinned_files != current_files(name):
            raise ValueError(f"skills/{name} does not match its pinned upstream source")
    for repository, details in catalog["sources"].items():
        license_data = read_license(
            repository,
            details["ref"],
            details["licensePath"],
            tree_cache,
            archive_paths=catalog_paths(catalog, repository),
        )
        license_path = repository_path(details["licenseFile"])
        if license_data != license_path.read_bytes():
            raise ValueError(
                f"{details['licenseFile']} does not match {repository}@{details['ref']}"
            )
    count = sum(skill.get("local") is not True for skill in catalog["skills"].values())
    print(
        f"Pinned content verified: {count} upstream skills from {len(catalog['sources'])} repositories."
    )


def stage_skill(
    folder: Path,
    skill: dict[str, Any],
    ref: str,
    files: dict[str, bytes],
    tree_cache: dict[tuple[str, str], dict[str, Any]],
) -> None:
    tree = tree_cache.get((skill["source"], ref), {"tree": []})
    modes = {entry["path"]: entry.get("mode") for entry in tree["tree"]}
    prefix = source_prefix(skill["upstreamPath"])
    for relative, data in files.items():
        target = folder / safe_relative_path(relative).as_posix()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if modes.get(prefix + relative) == "100755" else 0o644)


def vendor_catalog(catalog: dict[str, Any]) -> None:
    repository_path("skills")
    repository_path("licenses")
    for name in catalog["skills"]:
        repository_path(f"skills/{name}")
    for source in catalog["sources"].values():
        repository_path(source["licenseFile"])
    tree_cache: dict[tuple[str, str], dict[str, Any]] = {}
    replacements: list[tuple[Path, Path | None]] = []
    with tempfile.TemporaryDirectory(prefix="skills-vendor-", dir=ROOT) as temporary:
        staging = Path(temporary)
        destinations: list[Path] = []
        rendered: dict[str, bytes] = {}
        for name, skill in catalog["skills"].items():
            if skill.get("local") is True:
                validate_skill(name, current_files(name))
                continue
            source = catalog["sources"][skill["source"]]
            files = render_skill(
                name,
                skill,
                fetch_skill(
                    skill["source"],
                    source["ref"],
                    skill["upstreamPath"],
                    skill.get("upstreamName", name),
                    tree_cache,
                    exclude=skill.get("exclude"),
                    archive_paths=catalog_paths(catalog, skill["source"]),
                ),
            )
            stage_skill(staging / "skills" / name, skill, source["ref"], files, tree_cache)
            destinations.append(Path("skills") / name)
            rendered[name] = files["SKILL.md"]
            print(f"Staged skills/{name}", flush=True)
        for repository, source in catalog["sources"].items():
            relative = Path(safe_relative_path(source["licenseFile"]).as_posix())
            data = read_license(
                repository,
                source["ref"],
                source["licensePath"],
                tree_cache,
                archive_paths=catalog_paths(catalog, repository),
            )
            data.decode("utf-8")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            destinations.append(relative)
        (staging / "catalog.json").write_text(catalog_text(catalog), encoding="utf-8")
        destinations.append(Path("catalog.json"))
        (staging / MARKETPLACE_FILE).parent.mkdir(parents=True, exist_ok=True)
        (staging / MARKETPLACE_FILE).write_text(
            marketplace_text(catalog, rendered), encoding="utf-8"
        )
        destinations.append(Path(MARKETPLACE_FILE))
        catalog_path = repository_path("catalog.json")
        if (
            catalog_path.exists()
            and json.loads(catalog_path.read_text(encoding="utf-8")) != catalog
        ):
            raise ValueError("catalog.json changed while vendoring; rerun --vendor")
        try:
            for index, relative in enumerate(destinations):
                destination = repository_path(relative.as_posix())
                if destination.is_symlink():
                    raise ValueError(f"Refusing to replace a symlink: {relative}")
                backup = staging / "backups" / str(index) if destination.exists() else None
                if backup is not None:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, backup)
                replacements.append((destination, backup))
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging / relative, destination)
        except Exception:
            for destination, backup in reversed(replacements):
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink(missing_ok=True)
                if backup is not None:
                    os.replace(backup, destination)
            raise


def source_change(
    catalog: dict[str, Any],
    repository: str,
    details: dict[str, Any],
    tree_cache: dict[tuple[str, str], dict[str, Any]],
    min_age_days: int,
    now: datetime | None,
) -> tuple[str, dict[str, dict[str, bytes]], list[str], bytes | None] | None:
    candidate = latest_ref(repository, min_age_days, now=now)
    if candidate is None:
        return None
    relation = pin_relation(repository, details["ref"], candidate)
    if relation in {"identical", "behind"}:
        return None
    updates: dict[str, dict[str, bytes]] = {}
    reasons: list[str] = []
    for name, skill in catalog["skills"].items():
        if skill.get("source") != repository:
            continue
        files = render_skill(
            name,
            skill,
            fetch_skill(
                repository,
                candidate,
                skill["upstreamPath"],
                skill.get("upstreamName", name),
                tree_cache,
                exclude=skill.get("exclude"),
                archive_paths=catalog_paths(catalog, repository),
            ),
        )
        old_files = current_files(name)
        old_executables = current_executables(name)
        new_executables = upstream_executables(
            tree_cache.get((repository, candidate), {}), skill["upstreamPath"]
        ) & set(files)
        if files != old_files or new_executables != old_executables:
            updates[name] = files
            reasons.extend(review_reasons(name, old_files, files, old_executables, new_executables))
    license_data = read_license(
        repository,
        candidate,
        details["licensePath"],
        tree_cache,
        archive_paths=catalog_paths(catalog, repository),
    )
    license_data.decode("utf-8")
    license_file = repository_path(details["licenseFile"])
    if not license_file.is_file():
        raise ValueError(f"Missing current license file: {details['licenseFile']}")
    license_changed = license_data != license_file.read_bytes()
    if not updates and not license_changed:
        return None
    if license_changed:
        reasons.append(f"{repository}: license text changed")
    if relation != "ahead":
        reasons.append(f"{repository}: upstream history no longer contains the pinned commit")
    return candidate, updates, reasons, license_data if license_changed else None


def update_catalog(
    catalog: dict[str, Any],
    *,
    min_age_days: int = 0,
    only: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    catalog = json.loads(json.dumps(catalog))
    tree_cache: dict[tuple[str, str], dict[str, Any]] = {}
    skill_updates: dict[str, dict[str, bytes]] = {}
    license_updates: dict[str, bytes] = {}
    report: dict[str, Any] = {"clean": {}, "errors": {}, "review": {}, "written": []}

    for repository, details in catalog["sources"].items():
        try:
            change = source_change(catalog, repository, details, tree_cache, min_age_days, now)
        except (OSError, ValueError, KeyError) as error:
            report["errors"][repository] = str(error)
            print(f"Could not check {repository}: {error}", file=sys.stderr)
            continue
        if change is None:
            continue
        candidate, updates, reasons, license_data = change
        entry: dict[str, Any] = {"ref": candidate, "skills": sorted(updates)}
        if reasons:
            entry["reasons"] = reasons
        report["review" if reasons else "clean"][repository] = entry
        if (only == "clean" and reasons) or (only == "review" and not reasons):
            continue
        details["ref"] = candidate
        skill_updates.update(updates)
        if license_data is not None:
            license_updates[details["licenseFile"]] = license_data
        report["written"].append(repository)

    if not report["written"]:
        print("No selected skills or licenses to update.")
        return report

    temp_root = Path(tempfile.mkdtemp(prefix="skills-update-", dir=ROOT))
    replacements: list[tuple[Path, Path]] = []
    try:
        for name, files in skill_updates.items():
            target = temp_root / "skills" / name
            skill = catalog["skills"][name]
            stage_skill(
                target, skill, catalog["sources"][skill["source"]]["ref"], files, tree_cache
            )
        for path, data in license_updates.items():
            target = temp_root / safe_relative_path(path).as_posix()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        catalog_temp = temp_root / "catalog.json"
        catalog_temp.write_text(catalog_text(catalog), encoding="utf-8")
        marketplace_temp = temp_root / MARKETPLACE_FILE
        marketplace_temp.parent.mkdir(parents=True)
        skills = {name: files["SKILL.md"] for name, files in skill_updates.items()}
        marketplace_temp.write_text(marketplace_text(catalog, skills), encoding="utf-8")

        staged: list[tuple[Path, Path]] = [
            (ROOT / "skills" / name, temp_root / "skills" / name) for name in skill_updates
        ]
        staged.extend(
            (
                ROOT / safe_relative_path(path).as_posix(),
                temp_root / safe_relative_path(path).as_posix(),
            )
            for path in license_updates
        )
        staged.append((CATALOG_PATH, catalog_temp))
        staged.append((ROOT / MARKETPLACE_FILE, marketplace_temp))
        backup_root = temp_root / "backups"
        for index, (destination, replacement) in enumerate(staged):
            destination = repository_path(destination.relative_to(ROOT).as_posix())
            if destination.is_symlink() or not destination.exists():
                raise ValueError(f"Missing or unsafe destination: {destination.relative_to(ROOT)}")
            backup = backup_root / str(index)
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, backup)
            replacements.append((destination, backup))
            os.replace(replacement, destination)
    except Exception:
        for destination, backup in reversed(replacements):
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
            if backup.exists():
                os.replace(backup, destination)
        raise
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    print("Updated selected upstream content:")
    for repository in report["written"]:
        print(f"  {repository} -> {catalog['sources'][repository]['ref']}")
    for name in sorted(skill_updates):
        print(f"  skills/{name}")
    for path in sorted(license_updates):
        print(f"  {path}")
    return report


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or more")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="validate the local catalog without network access"
    )
    parser.add_argument(
        "--verify-pins",
        action="store_true",
        help="verify the vendored files match their pinned upstream commits",
    )
    parser.add_argument(
        "--vendor",
        action="store_true",
        help="import all recorded upstream pins, including new catalog entries",
    )
    parser.add_argument(
        "--min-age-days",
        type=non_negative_int,
        default=0,
        help="only adopt upstream commits at least this many days old",
    )
    parser.add_argument(
        "--only",
        choices=("clean", "review"),
        help="apply only updates that pass, or only updates that need, review checks",
    )
    parser.add_argument("--report", type=Path, help="write a JSON summary of the update")
    args = parser.parse_args()
    if sum((args.check, args.verify_pins, args.vendor)) > 1:
        parser.error("--check, --verify-pins, and --vendor are mutually exclusive")
    if (args.check or args.verify_pins or args.vendor) and (
        args.min_age_days or args.only or args.report
    ):
        parser.error("--min-age-days, --only, and --report apply only to upstream updates")
    try:
        catalog = load_catalog()
        if args.vendor:
            vendor_catalog(catalog)
        validate_local(catalog)
        if args.check:
            print(
                f"Catalog valid: {len(catalog['skills'])} skills from {len(catalog['sources'])} pinned sources."
            )
        elif args.verify_pins:
            verify_pins(catalog)
        elif not args.vendor:
            report = update_catalog(catalog, min_age_days=args.min_age_days, only=args.only)
            validate_local(load_catalog())
            if args.report:
                args.report.write_text(
                    json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
