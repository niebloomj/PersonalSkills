# Personal Agent Skills Catalog

A curated, portable catalog of community skills for use across your projects
and AI agents. This personal repository is the source of truth for selected
skills, each pinned to an upstream commit with its provenance and license. It is
not a FrumFinance-mandated catalog; any team rollout should be a separate,
explicit decision.

## Install the catalog for your agents

Install the whole catalog into your user-level folders for every supported agent on this machine:

```sh
npx --yes skills@1.8.1 add niebloomj/Skills --all --global --copy
```

To browse it first, run `npx --yes skills@1.8.1 add niebloomj/Skills --list`. To install selected skills, add `--skill <name>`. To sync after an approved catalog update, run `scripts/sync-skills.sh`; it removes retired `cloudflare` and `grill-me` copies before refreshing. Re-running the install command with `--force` refreshes included skills but does not remove skills dropped from the catalog:

```sh
npx --yes skills@1.8.1 remove --global cloudflare grill-me --yes
npx --yes skills@1.8.1 add niebloomj/Skills --all --global --copy --force
```

For project-scoped skills, run the command from the project directory and omit `--global`. The CLI writes to each selected agent's expected skills directory. For example, the test install wrote to both `~/.openhands/skills/` and `~/.agents/skills/`.

Merging an upstream update PR changes your approved personal catalog; it does
not install skills anywhere. Run the sync script from your devcontainer,
workstation setup, or agent bootstrap to update your managed environments. A
FrumFinance-wide rollout is separate: the organization should review and pin its
chosen catalog version rather than silently inheriting changes from this
personal repo.

The official [Vercel Skills CLI](https://github.com/vercel-labs/skills) detects supported agents and creates the appropriate agent-specific skill directories. `skills/` in this repository is the canonical catalog; the CLI can discover its skills from the GitHub repository or a local clone.

For OpenHands only, use `npx --yes skills@1.8.1 add niebloomj/Skills --all --global --copy --agent openhands`. To share a consistent selection with a particular codebase, use the project-scoped CLI install and commit that project's generated lockfile rather than asking every agent to discover skills independently.

## How updates work

Third-party copies are pinned to immutable upstream commits in
[`catalog.json`](catalog.json). A weekly workflow and a manual **Check upstream
skills** action check those sources and open a pull request when selected skills
change. It does not merge the pull request or execute upstream install scripts.
Review the diff, skill instructions, companion files, and license before
merging; merged content becomes the approved version in your personal catalog.
In repository settings, select **Read and write permissions** and enable
**Allow GitHub Actions to create and approve pull requests** under `Settings` →
`Actions` → `General` → `Workflow permissions`. The workflow grants write
access only to its updater job.

Use the **Code** tab's **Pull requests** list to review updates. Add or remove catalog entries by editing `catalog.json` and reviewing the resulting content. The update script fails closed if a source path disappears, a skill's name changes, or an upstream file is not a regular text file.

## Catalog and attribution

- [`skills/`](skills/) contains the selected skill files and required companion references.
- [`catalog.json`](catalog.json) records each source repository, exact commit, upstream path, and license.
- [`licenses/`](licenses/) preserves source license texts. `sentry-cli` is under Sentry's Functional Source License (FSL-1.1-Apache-2.0), whose permitted-purpose and notice terms apply. The other included source repositories identify MIT or Apache-2.0 licenses.

The curated set includes the requested actively maintained skills plus explicit skill dependencies. `grill-with-docs` delegates to `grilling` and `domain-modeling`. `break-ui` refers to `emil-design-eng` and `review-animations`; those skills' `choreograph` reference is included too. The chief-of-staff workflow includes its referenced sibling skills. The writing-skills skill includes Superpowers' `test-driven-development` reference; the separate `tdd` skill remains in the catalog as well. Architecture/spec skills include the codebase-design and TDD companions they call.

Some requested sources were not copied:

- Matt Pocock's `resolving-merge-conflicts` has been removed upstream. The old `diagnose` skill was renamed `diagnosing-bugs`; that current skill is included.
- The Remotion skills repository has no repository-level license declaration, so its content is not redistributed here pending permission or licensing clarification.
- The anti-detect-browser skills teach browser fingerprint spoofing and cross-site identity techniques. They are excluded from this catalog's default selection because they can enable identity and bot-detection evasion.
- Sentry CLI asks agents to confirm destructive commands before running them. Review any skill before installing it: skills are instructions to agents and may guide privileged actions in the user's environment.

This catalog is intentionally a small, reviewed distribution point, not an automatic mirror of every upstream repository.
