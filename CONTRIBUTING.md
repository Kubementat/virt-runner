# Contributing to virt-runner

Thanks for taking the time. This project is a small CLI with strong opinions about error handling
and output contracts, so most of this file is about *where* changes belong rather than how to write
Python.

## Before you start

If you hit a bug or have an idea, [open an issue](https://github.com/Kubementat/virt-runner/issues)
first for anything non-trivial. A one-line fix or typo needs no discussion.

`virt-runner` deliberately drives `virsh`, `virt-install`, `virtiofsd`, `curl`, and `ssh` as
subprocesses, depends on a single third-party package (`click`), and adds no daemon, no config file,
and no state beyond the VMs it creates. Proposals that would add a dependency, a config format, or a
background process will likely be rejected — that is the whole value of the tool.

## Getting set up

You need [`uv`](https://docs.astral.sh/uv/) and a Linux host.

```bash
git clone https://github.com/Kubementat/virt-runner.git
cd virt-runner
uv sync                     # creates .venv, installs the dev group (ruff)
uv run ruff check .         # lint
uv run ruff format --check .  # format check
uv run tests/unit_test.py   # pure-logic tests: seconds, no KVM, no network
```

`uv run virt-runner --help` works from the checkout. To test the packaged CLI the way a user gets it:

```bash
uv tool install ./            # or: uvx --from /path/to/virt-runner virt-runner --version
```

If you want the **integration** suite too, your host needs the full KVM toolchain — see
[Running the integration suite](#running-the-integration-suite).

## How the code is organised

```
cli.py          click group, subcommand aliases (c/d/ls/s), --version
cmd_create.py   argument validation + the staged create workflow
cmd_destroy.py  idempotent teardown
cmd_list.py     pool-membership based listing
cmd_ssh.py      interactive shell
cmd_setup.py    thin wrapper around the bundled install-prerequisites.sh (no --json)
args.py         --json plumbing, JSON-shaped usage errors
output.py       one run-state dict rendered as text or JSON
errors.py       VirtError(code, stage) — the single error type
images.py       URL/glob resolution, checksum lookup, cache layout
profiles.py     per-distro image/user/compose facts
virtualizer.py  EVERYTHING that touches the host
```

`virtualizer.py` is the only module that runs host commands. Everything above it stays
testable. If your change needs a new subprocess call, it belongs there.

## Making a change

1. Branch from `develop`. One branch per feature.
2. Keep the contract intact unless you intend to change it:
   - `--json` stdout must always be **exactly one** JSON document, success *or* error.
   - Exit codes never change: `0` success, `1` runtime/preflight failure, `2` usage error.
   - Error codes are public API. New code gets a name in the `README.md` table and the right `stage`.
   - New `--json` keys are additive; never rename or remove a key in a patch or minor release.
3. Add tests:
   - `tests/unit_test.py` — for every parser, regex, branch, or profile fact you touch. These run
     without KVM and without network, so mock `_spawn` for anything host-facing.
   - `tests/integration_test.py` — for every user-visible flag, subcommand, or JSON key. The rule is:
     **a feature without an integration test is not finished.**
4. Update the docs in the same commit — README (flag reference, error-code table), and
   `skills/virt-runner-cli/` if the behaviour an agent needs changed.
5. Add a `## [Unreleased]` entry to `CHANGELOG.md` in [Keep a Changelog](https://keepachangelog.com/)
   form, using the same wording you will keep for the release.
6. Run `./scripts/check.sh` (lint, format, unit tests, CLI, builds, `shellcheck`) and open a pull
   request against `develop`. Nothing checks your work for you — there is no CI. The integration
   suite is run by whoever touched it, because it needs real KVM.

### Code style

- Target Python 3.10+, line length 88, formatted and linted by `ruff` (config in `pyproject.toml`).
  Run `uv run ruff format .` before committing.
- Subprocess calls always go through the `_run` / `_stdout` / `_spawn` / `_quiet` / `_passthrough`
  helpers in `virtualizer.py`, never raw `subprocess`.
- Fail with `VirtError(code, message, stage)` rather than printing and exiting.
- Comments and docstrings explain *why*, especially for libvirt/virt-install quirks. Link a
  `docs/lessons-learned/` file when you restate a gotcha.
- Prefer the smallest change that works. Delete code that is not needed rather than guarding it.

### Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/), matching the existing history:
`feat(create): ...`, `fix(images): ...`, `docs: ...`, `chore(release): ...`.
`feat`/`fix`/`docs`/`refactor`/`perf`/`test`/`build`/`ci` are the types in regular use.

## Running the integration suite

This is the real acceptance test, and the reason the project has a `develop`/`main` split. It creates
and destroys real VMs, so it needs a provisioned KVM host:

```bash
./install-prerequisites.sh   # one time; then log out and back in
uv run tests/integration_test.py
```

It takes several minutes, creates `it-vm-a`, `it-vm-b`, `it-vm-arch`, and `it-vm-fedora`, and cleans
up after itself (including leftovers from an aborted run). The Arch leg is skipped on aarch64
hosts, where the Ubuntu + Fedora legs are the real e2e coverage.

Never run it on a machine with VMs you care about.

## Releases

Releases are tag-based, and a version is not a release until its tag exists. Nothing is automated
and nothing needs to be: the checklist in
[the release section of the README](README.md#releases) is run by a human, on a machine that has
both test suites available.

1. `./scripts/check.sh --all` and `uv run tests/integration_test.py` green on the candidate tree.
2. Fold `## [Unreleased]` into `## [X.Y.Z] - YYYY-MM-DD`, leaving `## [Unreleased]` with
   `_No unreleased changes at present._`.
3. Bump `version` in `pyproject.toml`, then `uv sync` (writes `uv.lock`; `--version` and the JSON
   `version` field read the installed metadata, so no other file needs the number).
4. Confirm the tag matches the tree: `grep -m1 '^version = ' pyproject.toml` must equal
   `git describe --tags` without the leading `v`. Then commit on `develop`, tag it
   (`git tag -a vX.Y.Z -m "release vX.Y.Z"`), fast-forward `main` to that commit, and push both.
5. Publish: `uv build` then `uv publish` (see below).

Semver maps directly onto what a user can observe: **MAJOR** breaks the CLI or JSON contract,
**MINOR** adds a flag, subcommand, error code, or JSON key, **PATCH** is fixes only. The first tagged
release is `v0.4.0`; `v0.1.0`–`v0.3.0` were bump-only commits without tags, so the changelog is the
only record for them.

### Publishing to PyPI

From a clean tree, on the tagged commit:

```bash
uv build                          # dist/virt_runner-<ver>.tar.gz + .whl
uv publish                        # uploads dist/*, reads the token from your environment
```

The token belongs in your local environment (`UV_PUBLISH_TOKEN`), never in the repository and never
in a git remote. `uv publish` refuses to overwrite an existing version, and a published version is
immutable: if the upload fails or the wrong tree got tagged, `yank` the release on PyPI and publish
the next version. Do not delete tags that were pushed.

Two manual checks worth the thirty seconds before you upload, because nothing enforces them: that the
tag's version equals `pyproject.toml`'s, and that `uv run virt-runner --version` on the tagged tree
prints that same version.

## Pull request expectations

- `./scripts/check.sh` green, and the integration suite mentioned in the PR description (either run,
  or why not).
- Every user-visible change has a changelog entry and a doc update.
- A new error code appears in `README.md`, `SECURITY.md` if it is security-relevant, and the skill's
  `references/json-and-errors.md`.
- No new runtime dependency without a strong reason.

## After a bug fix

If you fixed something that surprised you, write `docs/lessons-learned/NNN-<slug>.md`: symptom,
root cause, fix, and the generalisable lesson. These files are the project's institutional memory and
the reason the next person does not rediscover the same libvirt quirk.

## Getting help

Open an issue with `virt-runner --version`, the failing command, and the `--json` output. For
security issues, use [SECURITY.md](SECURITY.md) instead of the public tracker.
