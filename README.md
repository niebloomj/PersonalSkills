# Personal Agent Skills Catalog

A personal, portable collection of third-party and locally authored agent skills.
This repository is the source of truth, not a list of upstream installers.
Third-party content is pinned to reviewed commits in [`catalog.json`](catalog.json).

## Install on a machine

Requires Python 3.11+, symlink support, and a persistent checkout. GitHub CLI is needed to clone or
check upstream sources, but installation itself uses only local files.

```sh
gh repo clone niebloomj/Skills ~/Documents/PersonalSkills
cd ~/Documents/PersonalSkills
python3 scripts/sync_skills.py
python3 scripts/sync_skills.py --apply
python3 scripts/sync_skills.py --audit
```

The first command previews changes without modifying installed skills or profile data. `--apply` backs up
existing copies and retired entries before replacing them. `--audit` exits
nonzero for unmanaged, divergent, retired, or unexpectedly nested global skills,
and for a skipped Codex plugin edit. Untracked files that git ignores, such as
Python caches and `.DS_Store`, don't count; committed files are always checked.
Re-running an unchanged installation is a no-op.

The installer creates one definition per personal skill:

```text
agent/skills/name -> ~/.agents/skills/name -> this-checkout/skills/name
```

It detects existing Claude Code, Codex, OpenCode, Gemini, Pi, Cursor, and
CC-Switch skill directories. It also manages `$OPENCODE_CONFIG_DIR/skills` and
remembers it as `openCodeSkillsRoot` in the private configuration, so later runs
from any shell keep it in sync while its parent folder exists. Set that entry to
`false` to stop managing it; relative `OPENCODE_CONFIG_DIR` values are ignored. Keep this checkout in place: moving it requires another
sync. Branch switches also change the live skills, so keep the canonical checkout
on an approved catalog revision. `scripts/sync-skills.sh` wraps the same Python command.

Installation never executes upstream scripts or installs their dependencies.
Individual skills may require tools, subscriptions, plugins, or MCP servers.
For example, `gpt-image-2` needs the Codex CLI and a suitable ChatGPT subscription.

## Keep a Mac in sync automatically

`scripts/auto_sync.py` removes the manual pull-and-sync step. Its LaunchAgent runs
at login and every six hours. Each run fast-forwards a clean `main` checkout,
applies any pending link changes, and audits the result. Updated skill content is
live as soon as the checkout moves, because installed skills link to it.

```sh
python3 scripts/auto_sync.py install          # preview the LaunchAgent
python3 scripts/auto_sync.py install --apply  # install it; it also runs once now
python3 scripts/auto_sync.py uninstall --apply
```

Instead of the LaunchAgent, you can call `python3 scripts/auto_sync.py run` from a
script you already run every day, such as a morning routine. It runs the same
checks as the LaunchAgent.

A run leaves the checkout alone when it is on another branch or has uncommitted
changes to tracked files. It only fast-forwards, so a `main` that has diverged
from `origin/main` stops the run. It also fast-forwards only skill content
(`skills/`, `licenses/`, `catalog.json`, and the marketplace file); when
`origin/main` changes anything else, such as these scripts, it skips and asks you
to review and pull by hand, so a push to this repository cannot run new code on
your Mac unattended. When CC-Switch manages skills, link changes
wait until CC-Switch is fully quit. Waiting changes, audit problems, and errors
raise a macOS notification, as does syncing that stays blocked for more than a
day (at most once a day). Every run is logged to
`~/.local/state/personal-skills/auto-sync.log`. After a run whose audit finds no
problems, it deletes automatic backups older than 30 days. It always keeps the five
newest link backups, the five newest CC-Switch snapshots, and any backup that a
manually named folder or file mentions by name or timestamp; manually named
entries, such as curation receipts, are never removed, and renaming a backup keeps
it. `python3 scripts/auto_sync.py run`
performs a run immediately; unlike the commands above, it applies changes without
a preview.

`install --apply` waits for the first run and reports how it ended. Because this
checkout lives in `~/Documents`, macOS may ask whether `python3` can access that
folder; allow it, or grant access later in System Settings > Privacy & Security >
Files and Folders. A Homebrew Python upgrade can ask again. The LaunchAgent starts
the job through a small shell wrapper, so a job that cannot start at all, because of
a missing permission, interpreter, or checkout, still raises a notification.

## Install as Claude Code plugins

This repository is also a Claude Code plugin marketplace, `niebloomj-skills`, with
one plugin per skill. Use it on machines that don't run the sync:

```sh
claude plugin marketplace add niebloomj/Skills
claude plugin install i-have-adhd@niebloomj-skills
claude plugin marketplace update niebloomj-skills
```

A plugin skill runs with its plugin name as a prefix, such as
`/i-have-adhd:i-have-adhd`. Plugins come from `main`, so they carry the same
reviewed content. Don't also install them on a machine that runs the sync, or
each skill appears twice.
[`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json) is generated
from the catalog by the updater and `--vendor`; `--check` fails when it is stale.

## Coexist with a company registry

Keep company-managed skills with their registry rather than copying internal
content into this public repository. Register its lockfile once:

```sh
python3 scripts/sync_skills.py --external-lock /absolute/path/to/registry-lock.json
python3 scripts/sync_skills.py --external-lock /absolute/path/to/registry-lock.json --apply
```

An external lock must contain a `skills` mapping keyed by skill ID. The installer
preserves those canonical definitions and repairs stale projection links. It
does not replace registry-owned Claude-specific paths, whether real directories
or symlinked client variants. Missing registered lockfiles are errors, not silently ignored.

Machine-specific configuration is private, outside this repository:
`~/.local/state/personal-skills/config.json`. It remembers external lockfiles and
can specify absolute `extraRoots`, explicit runtime-owned `exemptRoots`, and
additional `retired` IDs. `externalOwners` preserves selected company repository
discovery entries when migrating CC-Switch.

An exact-path `retainedDanglingLinks` list can acknowledge deliberately retained
broken links. Audits still display them as warnings. If a target becomes an
active unmanaged skill, it is an error again, not a permanent ownership exception.

Application-bundled/system skills, enabled native runtime plugins, and
project-local contributor skills remain with their owners. Runtime-synchronized
skill containers must be explicitly listed in `exemptRoots`. Unknown personal
skills, including dangling links to temporarily unavailable storage, are reported
but never removed unless explicitly retired.

## CC-Switch

Fully quit CC-Switch, including its tray process, then preview and apply:

```sh
python3 scripts/sync_skills.py --cc-switch
python3 scripts/sync_skills.py --cc-switch --apply
python3 scripts/sync_skills.py --audit
```

The optional Unix offline adapter is tested against CC Switch 4.0.35. It snapshots the
database and settings, checks the schema and concurrent changes, preserves every
existing app-enablement flag and installation timestamp, and selects shared
storage with symlink-only projections. Unrelated preferences are preserved.

Personal rows intentionally become **Local** in CC-Switch. This prevents its
updater from replacing repository links with independent upstream downloads.
Upstream provenance remains in this catalog and each row's repository URL.
Only superseded source repositories are disabled; unrelated discovery choices
are preserved. Use this repository for content updates, not CC-Switch's update or uninstall buttons. CC-Switch can
still manage app projections; shared compatibility directories mean its flags
do not provide complete per-agent skill isolation.

The offline adapter is remembered for subsequent syncs, which require CC-Switch
to remain closed. If metadata migration fails, unchanged filesystem edits roll
back. Concurrent user edits are left intact and reported, with originals retained
in the private backup for reconciliation.
Private backups are under `~/.local/state/personal-skills/backups/`; they can
contain private configuration and must never be committed or shared.

## Names, invocation, and dependencies

- `sentry-code-review`: Sentry's general correctness, security, performance, and
  testing checklist.
- `standards-spec-review`: Matt Pocock's separate standards/spec review,
  explicitly invoked. There is no generic `code-review` ID in this catalog;
  application-bundled commands are outside its scope.
- Superpowers' complete fourteen-skill set shares one upstream revision. Its
  skill references are unqualified so no `superpowers:` plugin namespace is
  required. The duplicate Codex Superpowers plugin is disabled only when all
  fourteen replacements are present; native runtime plugins are untouched. If the
  Codex config cannot be edited safely, sync leaves it unchanged, reports it as
  skipped, and audit fails until it is resolved.
- `grill-me` is retired in favor of `grilling`.
- Trail of Bits retired `ask-questions-if-underspecified`; its obsolete copy is
  removed rather than frozen into this collection.
- Standalone Codex runtime-helper copies are retired. Their native plugin
  remains responsible for the matching helpers and runtime scripts.
- Curation retirements are recorded in `catalog.json`. Sync backs up and removes
  their global projections and CC-Switch rows instead of reinstalling them.

Vendored invocation-only skills carry both standard `disable-model-invocation` metadata
and Codex's `policy.allow_implicit_invocation: false`. Workflows such as
`second-opinion`, `standards-spec-review`, and `i-have-adhd` are explicit-only. User and project
approval, scope, and delegation rules still take precedence over imported workflows.

`i-have-adhd` stays on for the rest of a session once invoked, until you say "stop
adhd mode". Only its skill folder is vendored; its upstream always-on hooks and
editor plugins are not included.

`skill-scanner`, `agentic-actions-auditor`, and `gha-security-review` read files
that someone else may have written, so their upstream pre-approval of `Bash` is
removed and Claude Code asks before they run a command. Read-only commands such as
`grep` and `cat` still run without asking. A test fails if an update brings a grant
back.

## Add your own skill

Create `skills/my-workflow/SKILL.md` with `name: my-workflow` and a useful
`description` in YAML frontmatter. Add this entry under `skills` in `catalog.json`:

```json
"my-workflow": {
  "local": true
}
```

Locally authored skills have no invented upstream pin and are never replaced by
the upstream updater. Keep their supporting files beside `SKILL.md`.

## Review and update

The weekly **Check upstream skills** workflow keeps the catalog current without a
human in the loop. For each source it adopts the commit that headed the default
branch seven days earlier, timed by GitHub's push records rather than commit
dates, and it never moves a pin backwards. Updates that pass the tests, pin
verification, and the safe-change rule merge automatically through a pull
request, so each one can be reverted with one click.

An update merges automatically only when its shape is known to be safe:

- every changed file is Markdown or plain text, and none is hidden, executable,
  a license, an agent instruction file (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`), or a
  nested `SKILL.md`;
- `SKILL.md` frontmatter is unchanged except for a one-line `description` written
  as a plain sentence or a quoted string;
- `SKILL.md` contains no `` !` `` or ```` ```! ```` shell command; and
- the skill does not pre-approve tools or define hooks.

Everything else, including license changes and rewritten upstream history,
becomes one draft pull request that lists the reasons. A source whose files cannot
be vendored safely, such as names that differ only in case or Unicode form, fails
the run with its reason while the other sources still update. Merge it to accept the
updates; close it to reject that exact update, which is then not proposed again.
Plain instruction changes still merge after the wait, so the rule limits what can
run commands or grant permissions, not what skill text asks an agent to do. A
manual run can use a shorter waiting period.

Macs that run the sync job pick up merged updates on its next run: within six
hours with the LaunchAgent, or the next day from a daily script. Elsewhere, pull
the catalog and run the sync/audit commands. Do not refresh this collection
through separate upstream installers. Dependabot separately opens a monthly pull
request when one of the workflow's pinned GitHub Actions has a new release.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/update_skills.py --check
python3 scripts/update_skills.py --verify-pins
```

To import new third-party entries after recording their source, commit, path,
and license, use `python3 scripts/update_skills.py --vendor`. It stages all
downloads before replacement, preserves executable companions, and fails closed
on missing paths, renamed skills, unsafe file types, or oversized content.

`catalog.json` is tool-written. The updater and `--vendor` write it with sorted
keys and two-space indentation, and the tests reject any other layout, so update
PRs change only the pins that moved. After editing it by hand, run `--vendor`,
which also regenerates the plugin marketplace.

Renames (`upstreamName`), explicit invocation (`manual`), Markdown reference
rewrites (`replaceMarkdown`), and file exclusions (`exclude`) are
recorded transformations, reproduced by imports, verification, and updates.
Codex display names and manual-only policy are normalized for portable discovery.
The GPT Image 2 example gallery is excluded: its images are linked remotely and
are not dependencies of the included generation helpers. `humanizer` is published
as a whole repository. Its plugin manifests, CI files, `AGENTS.md`, and maintainer
files are excluded, which currently leaves only its `SKILL.md` and Codex metadata.
Files it adds later are vendored unless they match an exclusion, and like any
update, additions other than Markdown or plain text wait for review.

## Attribution and licenses

[`catalog.json`](catalog.json) records every upstream repository, immutable
commit, selected path, transformation, and license notice.
[`licenses/`](licenses/) preserves those notices verbatim. Trail of Bits content
is CC-BY-SA-4.0; Sentry CLI uses FSL-1.1-Apache-2.0. Other selected sources declare
MIT or Apache-2.0. Those source-specific terms still apply to their content.

Remotion remains excluded pending licensing clarification. Browser fingerprint
spoofing/identity-evasion skills are not part of this curated collection.
