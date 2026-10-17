# Project setup
- `python` with `uv` as management tool

# Basic instructions
- use the `codegraph_codegraph_explore` tool to find relevant code
- load the `ponytail` skill

# Development workflows
- stable prod branch is `main`
- development branch is `develop`
- new features are build on feature branches within git worktrees
- features require at least a minimal integration test

# Releases (tag-based)
A release = a commit that is tagged `vX.Y.Z` (annotated) and present on `main`.
Tag + changelog section + version bump always land in the same commit; a version
that is not tagged is not a release. Tags are annotated and never moved once pushed.

Semver: MAJOR = breaking CLI/JSON contract, MINOR = new flag/subcommand/error code/JSON key,
PATCH = fixes only. Version lives in `pyproject.toml`; `uv sync` writes it to `uv.lock` and
`virt-runner --version` / the `--json` `version` field read it from the installed metadata,
so no other file needs the number.

Release checklist:
1. Both suites green on the tree: `uv run tests/unit_test.py` + `uv run tests/integration_test.py`.
2. Changelog: move the `## [Unreleased]` entries into `## [X.Y.Z] - YYYY-MM-DD`, leave
   `## [Unreleased]` with `_No unreleased changes at present._`. Reuse the wording written
   in the feature commits.
3. Bump `version` in `pyproject.toml`, then `uv sync`.
4. Commit on `develop`, tag it: `git tag -a vX.Y.Z -m "release vX.Y.Z"`.
5. Fast-forward `main` to that commit (`git branch -f main develop` — avoids churning the
   checked-out worktree), then push both remotes with tags:
   `git push forgejo main develop --follow-tags && git push github main develop --follow-tags`.

The first tag is `v0.4.0`; `v0.1.0`-`v0.3.0` were bump-only commits without a tag, so for
those the changelog headings are the only record.

# Lessons learned
When discovering bugs / problems AND solutions to those worth remembering: write a short lessons learned markdown file to the `./docs/lessons-learned/` directory

# Integration Testsuite
The project uses a minimal integration testsuite that runs the core feature workflows. 
The testsuite is started via: `uv run tests/integration_test.py`
When adding new features ensure to add a minimal test case to `tests/integration_test.py`

Pure-logic checks: `uv run tests/unit_test.py` (seconds, no KVM). Add a case for every parser or branch you touch.
