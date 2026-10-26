# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Entries are derived from the actual git history (commits `c35c32b` → v0.1.0).

## [Unreleased]

_No unreleased changes at present._

## [0.6.0] - 2026-10-26

### Added

- **`virt-runner modify NAME`** (alias `m`) — change an existing virt-runner VM instead of
  recreating it: `--vcpu`/`--ram` (same units as `create`), `--disk` (grow only), `--net` to
  re-attach the primary NIC, `--net-add`/`--net-remove` for extra NICs and `--mac` to move the
  primary one, plus `--live`, `--reboot`, `--wait`, `--no-auto-grow-fs` and `--plan`. Ownership is
  gated the same way as `list` (a pool-resident disk or virt-runner metadata), so a hand-defined
  domain is reported as `not-a-virt-runner-vm` and left untouched. An undefined name answers
  `vm-not-defined`, the same code `destroy`/`ssh` already use.
  Every change reports `effective: "now" | "on-boot"` because running state and persisted config
  are two different things in libvirt: `virsh setvcpus`/`setmem --config` write the config for the
  next boot, and a live change above the domain's ceiling is refused outright — `--live` is
  therefore best effort and degrades to the persisted value with a warning rather than leaving the
  guest and its config silently apart. `--reboot` closes that gap (with `--wait` it also returns
  the fresh address); it is a valid request on its own, because rebooting is how a config that is
  already correct becomes real.
  A disk grow uses `virsh blockresize` on a running guest and a plain `virsh vol-resize` for a
  powered-off one (libvirt only resizes active domains, and refuses to shrink unless `--shrink` is
  asked for), then grows the guest's own filesystem over SSH with `growpart` +
  `resize2fs`/`xfs_growfs`/`btrfs`; that step is best effort — an unsupported layout (LVM, no
  `growpart`) keeps the grown block device and reports the commands to run by hand. Re-issuing the
  same `--disk` is the filesystem-only retry. Growing RAM past `<memory>` raises the ceiling with
  `virsh setmaxmem --config` and then sets the boot size with `setmem --config`.
  A NIC move refuses a network/bridge the host does not have **before** detaching anything, so a
  running guest can never be left without an interface by a typo; if a run stops mid-way the
  document marks the untouched entries `"applied": false` instead of reporting the whole plan.
  A sizing request is compared to the **persisted config**, not to the running value: both sides
  matching is the only `modify-noop`, a config that already matches reports `on-boot` without a
  libvirt call, and a config that disagrees is corrected (with its own value as `from`) even when
  the guest already looks right. `--ram` beyond the host's installed RAM is refused
  (`runtime-error`) because libvirt writes it happily and only the next boot fails.
  Shrinking is refused (`disk-shrink-unsupported`), removing the last NIC is a usage error, and
  asking for what the guest **and** its config already have is `modify-noop` (exit 2).
- Unit tests for the `dumpxml` → `VmSpec` parser (including `<currentMemory>` as the live size),
  the size/ceiling rules and the change planner (including every refusal path), an argv guard that
  pins every `virsh` option the tool passes, and an integration leg that plans, refuses, grows a
  running guest's vCPU/RAM/disk, verifies it from inside the guest, and round-trips a second NIC.

## [0.5.0] - 2026-10-20

> First release intended for PyPI: `uv tool install virt-runner` / `uvx virt-runner` work from here
> on, with `virt-runner setup` covering the host side.

### Added

- **`virt-runner setup`** — a subcommand that runs the host-prerequisite installer without a
  repository checkout, so `uv tool install virt-runner` / `uvx virt-runner` users get the same
  one-time host setup as source installs. `install-prerequisites.sh` is now bundled into the wheel as
  package data (`virt_runner/resources/`), so the script always matches the installed version; from a
  checkout it falls back to the file in the repository root. Flags pass through to the script
  (`--check`, `--with-virt-manager`), its environment variables still work, and its exit code is
  returned. Deliberately outside the `--json` contract and unaliased: it is a privileged host
  operation, not a VM command.
- **MIT license** — `LICENSE`, Copyright (c) 2026 Julian Weber, declared in the package metadata
  (`license` + `license-files`), so `pip show virt-runner` and the PyPI page report it.
- **Complete package metadata** — `readme`, `authors`, `keywords`, `classifiers`, and
  `[project.urls]` (homepage/repository/issues/changelog). Before this, a published wheel carried an
  empty project description.
- **`./scripts/check.sh`** — the local gate that stands in for CI: `uv sync --frozen`, lint, format
  check, unit tests, a CLI smoke test, `uv build`, and `shellcheck` for the installer (`--all` adds
  the Python 3.10 floor). Releases stay manual — `uv build` then `uv publish` from the tagged commit
  — because this project deliberately has no CI and no publish pipeline. `uv tool install
  virt-runner` and `uvx virt-runner` work once the first release is published.
- **`SECURITY.md`** — disclosure policy and the documented, deliberate security trade-offs
  (SSH host-key checking disabled, best-effort image verification, one shared keypair per host,
  `--mount` grants the guest write access to your host directories, libvirt/kvm group membership).
- **`CONTRIBUTING.md`** and **`docs/README.md`** — dev setup, code map, the two test suites, the
  contract rules for flags/error codes/JSON keys, and the release procedure.

### Changed

- **README installation story** — a new [Install](README.md#install) section separates the one-time
  host prerequisites from the tool itself and documents the `uv tool install` / `uvx` workflow. All
  examples now use the installed `virt-runner` command; `uv run` is documented as the checkout form.
  The host installer is explicitly documented as Debian/Ubuntu (`apt-get`) only.
- **Internal documents moved to `docs/archive/`** — `specification/`, `.tickets/`, `docs/plans/`,
  `docs/reviews/`, the POC narrative, the Opus code review, and the POC e2e evidence. Nothing was
  deleted; see [`docs/archive/README.md`](docs/archive/README.md). `docs/lessons-learned/` and
  `docs/architecture-diagrams/` remain the living reference material.
- **`install-prerequisites.sh` on a non-Debian host** — the error no longer points at `--help` for a
  package list it does not print; it names the packages and the README section that describes them.
- **Preflight messages that used to leave you guessing** (`libvirt-unreachable`,
  `pool-not-found`, `virtiofsd-missing`) now name `virt-runner setup` as the fix, so a fresh
  `uvx virt-runner` host no longer needs the README open to get past the first command.
- `tests/unit_test.py` reformatted — pre-existing drift that the new `ruff format --check` gate
  catches. No test logic changed.

### Fixed

- **The agent skill described the pre-aarch64 world.** `unsupported-arch` was documented as "host
  not x86_64 … not fixable", so an agent on a supported aarch64 host would have told the user the
  tool cannot help them, and `compatibility` still required x86_64 and `uv`. Both corrected, and
  `image-arch-unavailable` — added in 0.4.0 but never added to the skill's error table — is now
  there, so an agent can name the one ARM failure that is real.

## [0.4.0] - 2026-10-18

> Supersedes the `0.3.1` bump in `8fddb23`: that commit shipped new flags and new JSON keys,
> so semver puts it in a minor release. `0.3.1` was never tagged and is not a release.

### Added

- **aarch64 host support** — `create` now works on aarch64 hosts (Ubuntu and
  Fedora aarch64 cloud images, verified as before; UEFI firmware is built into
  `qemu-system-aarch64`, so no `--boot` change); `--distro arch` on aarch64
  fails with the new `image-arch-unavailable` code (arch-boxes ships
  x86_64-only images).
- **`virt-runner --version`** — prints the installed package version
  (`virt-runner, version X.Y.Z`).
- **`create --script FILE`** — repeatable flag that runs a host script in the guest
  as the cloud user after SSH is verified and cloud-init has finished. Interpreter from
  the `#!` line, else `.py` → `python3`, other → `bash`. Output streams to stderr;
  `--json` reports `scripts: [{path, exit_code}]`. A non-zero exit fails with the new
  `script-failed` code (stage `script`) and keeps the VM.
- **`create --mount HOST[:GUEST]`** — repeatable flag to share a host directory into
  the guest via virtiofs. The mount is ready when `create` returns and persists across
  guest reboots (first-boot `bootcmd` mount plus an fstab entry, since the NoCloud
  seed is first-boot-only). Default guest path is `/mnt/<basename of HOST>`.
  Requires the `virtiofsd` package (installed by `install-prerequisites.sh`).
- **`vm.mounts` in `--json` output** — always present (empty list when no mounts).
- **`virtiofsd-missing` error code** — `--mount` given but `/usr/libexec/virtiofsd`
  is not installed (stage: `preflight`).
- **Subcommand aliases** — `c` = `create`, `d` = `destroy`, `ls` = `list`,
  `s` = `ssh`. Same flags, exit codes and JSON `tool` ids (`vm-create`, not `vm-c`);
  `--help` still lists each command once.

### Changed

- **`create` preflight failures** — the error document now includes
  `"booted": false` like every other stage (key addition only).
- **Image downloads** — the hard 600 s subprocess timeout is replaced by
  curl's stall detection (`--speed-limit 10240 --speed-time 60`); a
  transfer only aborts when it stays under 10 KB/s for 60 s.
- **`virt-install` flags** — the redundant `clouduser-ssh-key=` cloud-init
  parameter is dropped (the injected key comes from user-data; verified on
  all four distro legs), and `--console none` is replaced by
  `--noautoconsole` so the recommended `virsh console NAME` actually has a
  serial console device to attach to.
- **NoCloud seed handling** — `create` no longer uses virt-install's
  `--cloud-init` flag (virt-install 4.1.0 deletes its generated ISO in exit
  cleanup while the guest is still in UEFI — fatal on aarch64, where edk2's
  CDROM boot attempt loops on `Reset System`). The seed ISO is now built by
  virt-runner (`xorrisofs -V cidata`), stored as a `vm-pool` volume
  (`<NAME>-cloudinit.iso`) attached as a plain `--disk` CDROM for the VM's
  lifetime (removed on `destroy` or when `create` fails — deleting it
  earlier would break `virsh start`, and every VM is autostart); a
  `runcmd` in user-data keeps cloud-init disabled for later boots
  (replacing `--cloud-init disable=on`) (see
  `docs/lessons-learned/008-virt-install-cloudinit-iso-deletion.md`).

### Fixed

- **`--json` usage errors from cross-option checks** — e.g. `--distro arch --release 99`
  printed click's plain-text usage error with empty stdout; now the `usage` envelope
  (exit 2) like every other usage error.
- **Unverified Fedora images during point-release mirror syncs** — some
  mirrors list a fresh build's image before its CHECKSUM file, so the
  newest glob candidate downloaded unverified and the pipeline accepted it
  (D1 skip). Rotating-image globs now fall back to the next-newest build
  when the newest one has no verifiable checksums, like they already did
  for dead (404) candidates; the skip stays the last resort when no
  candidate verifies.
- **Fedora aarch64 never booted** — libvirt's default aarch64 firmware on
  some hosts enforces Secure Boot, which rejects Fedora's aarch64 cloud
  image (unsigned GRUB) with a firmware `Security Violation` dialog: the
  domain stayed `running` but never reached the kernel, so `create` timed
  out in `wait-ip` with a silent console. aarch64 domains are now created
  with Secure Boot disabled (`--boot uefi=on,loader=<no-secboot firmware>`,
  path looked up via `virsh domcapabilities`; x86_64 is unchanged) (see
  `docs/lessons-learned/009-aarch64-secure-boot-unsigned-grub.md`).
- **aarch64 teardown** — `destroy` of a UEFI (aarch64) domain failed with
  `vm-destroy-failed` because libvirt refuses a plain `virsh undefine` while
  the domain's NVRAM variable store exists; `undefine` now passes `--nvram`
  (discards the store; no-op for NVRAM-less x86_64 domains) (see
  `docs/lessons-learned/007-aarch64-nvram-undefine.md`).
- **Fedora image download 404s** — during point releases some mirrors keep a
  stale index that still lists a build whose file is gone; the glob resolved
  to that dead "newest" name and the download failed. The glob now resolves
  to all candidates (newest first) and falls back to the next-newest name on
  a download failure (see `docs/lessons-learned/006-fedora-mirror-inconsistency.md`).
- **Fedora checksum verification** — the CHECKSUM filename was derived from
  the image basename instead of the compose name, causing a 404 and silent
  skip of verification. Now resolved via the distro profile's ``sums_url``
  callable.
- **Cache-hit verification reporting** — previously always claimed
  ``sha256-sums`` on cache hits regardless of whether a ``.verified`` marker
  existed. Now reports ``skipped`` when the marker is absent (pre-0.4
  caches will show ``skipped`` on first re-run).
- **Custom-image cache collision** — bare ``--image`` URLs with the same
  basename but different origins now cache under ``custom/<url-sha256-12>``
  instead of ``custom/<basename>``. Old orphaned entries are harmless.
- **``destroy`` error codes** — ``destroy`` now checks libvirt reachability
  first (``libvirt-unreachable``) and wraps domain teardown failures with
  the new ``vm-destroy-failed`` error code.
- **Per-VM SSH identity metadata** — ``create`` now records the SSH private
  key path in domain metadata; ``ssh`` and ``list`` read it back so the
  ``ssh_command`` carries the correct ``-i`` flag.
- **``--user`` validation** — invalid user names (not matching
  ``[a-z_][a-z0-9_-]{0,31}``) are rejected with exit code 2 and code
  ``usage``. Cloud-init scalars are now JSON-quoted to prevent ``#`` or
  ``: `` in key comments from breaking the YAML.
- **Non-x86_64 host refusal** — ``preflight_check`` now rejects hosts whose
  ``uname -m`` is not ``x86_64`` with error code ``unsupported-arch``.

## [0.3.0] - 2026-09-25

### Added

- **Fedora guests** — `virt-runner create NAME --distro fedora` boots the
  Fedora Cloud Base image (x86_64, release 44, user `fedora`) through the
  existing pipeline: block-format checksum verification, `wheel`-group
  user, `generic` osinfo (commit `d3e91e6`).

- **Arch Linux guests** — `virt-runner create NAME --distro arch` boots the
  official arch-boxes cloud image (`cloud-init`/NoCloud preinstalled,
  x86_64-only, release `latest`) through the existing pipeline: sidecar
  `.SHA256` verification, `wheel`-group user `arch`, `generic` osinfo
  (commit `f680c3f`). Distro data lives in `src/virt_runner/profiles.py`
  (data-driven profiles per the generalization spec); `--user` and
  `--release` defaults are per-distro.

- **Per-VM user metadata** — `create` records the cloud user (and SSH
  identity) in the domain's libvirt metadata; `ssh` and `list` read it back
  so they log in as the user that was actually created (commit `9c612df`).

### Fixed

- **Child `PATH`** — spawned host tools now run with the venv's `bin` dir
  stripped from `PATH`, so `#!/usr/bin/env python3` shebangs resolve the
  host interpreter, not the isolated one (commit `df970ac`).

## [0.2.0] - 2026-09-01

### Added

- **Python rewrite of the POC bash scripts** — `bin/vm-create`/`vm-destroy`
  replaced by the `virt-runner` CLI package (`cli → cmd_* → virtualizer →
  core`), stdlib + `click`, same pipeline and host tools (`virsh`,
  `virt-install`, `curl`, `ssh`) (commit `89000e0`).
- **`ssh NAME` subcommand** — interactive shell on a VM: resolves the IP
  from the DHCP lease (or `domifaddr`), logs in with the injected key and
  the user recorded at create, passes through ssh's exit code
  (commit `fcbb2fb`).
- **Integration test suite** — `tests/integration_test.py` end-to-end run
  against the real host (create Ubuntu + list + ssh hello world + destroy),
  every call through `--json` (commit `c3e5be8`).

## [0.1.0] - 2026-08-25

### Added

- **Project scaffold** — repository initialized
  (`c35c32b`, "chore: initial commit — project scaffold").
- **POC specification v1.0** — `specification/specification.md` (since removed): purpose,
  environment requirements, CLI contract, six-step pipeline, verified host
  facts & URLs, known pitfalls, acceptance criteria, decisions D1–D10
  (ticket: spec phase; commit `7583a72`).
- **Implementation plan v1.0** — `docs/plans/poc-implementation-plan.md`:
  strictly sequential vertical slices S1→S7, planner decisions PD1–PD4,
  testing strategy, risk table, read-only environment verification
  (ticket: plan phase; commit `2edf635`).
- **Implementation tickets** — `.tickets/ticket-01…07` + ticket README: one
  self-contained ticket per slice with deliverables, safety rules, checkable
  acceptance criteria, and commit messages (ticket: ticketing phase;
  commit `6a7aa60`).
- **`bin/vm-create` (since removed) — skeleton, CLI parsing, preflight checks**
  (ticket 01 / slice S1; commit `c979e86`): shebang + `set -euo pipefail`,
  all defaults per spec §3.1, usage/help, full flag parsing incl. D1
  `--release`-over-`--image` precedence, `NAME` regex validation, and all
  five preflight checks with exit codes 0/1/2.
- **`bin/vm-create` (since removed) — image download, SHA256 verification, cache reuse**
  (ticket 02 / slice S2; commit `865402b`): `fetch_and_verify_image()` with
  the `~/vm-images/<release>/` (and `custom/`) cache layout, D4
  download-to-final-name + in-place `sha256sum -c` verification, no
  partial/corrupt files left behind, D1 skip-verify-with-warning path for
  non-derivable sums, `--keep-going`, and the `VM_CREATE_CACHE_DIR` test
  hook.
- **`bin/vm-create` (since removed) — cloud-init file generation**
  (ticket 03 / slice S3; commit `36a8bf1`): `generate_cloud_init_files()`
  writing NoCloud `user-data` (injected SSH key, `--user` per D2,
  passwordless sudo, `package_update: false`, no `network:` section) and
  `meta-data` (D3-correct `hostnames` block, fresh lowercase UUID, plus
  `local-hostname:` required for NoCloud to apply the hostname — recorded
  deviation) into a `mktemp` dir with `chmod 600` + trap cleanup, and the
  `VM_CREATE_CLOUD_INIT_DUMP` test hook.
- **`bin/vm-create` (since removed) — VM creation via `virt-install`**
  (ticket 04 / slice S4; commit `5e55cb9`): fixed-MAC generation
  (`52:54:00:` + 3 random bytes, colon-formatted), the full virt-install
  invocation (fixed `vm-pool` disk `<NAME>_vda.qcow2`, `default` network,
  `--os-variant ubuntu-lts-latest`, file-based `--cloud-init` with
  `disable=on`, `--autostart`), D5 `--no-boot` stop-after-create with the
  not-booted output variant; two documented, evidence-backed deviations for
  virt-install 4.1.0 (pre-created volume + `--disk vol=` instead of
  `--location --import` + `--disk size=`; `--extra-args console=ttyS0`
  omitted — both recorded in the script header).
- **`bin/vm-create` (since removed) — wait-for-IP, mandatory SSH verification, access info**
  (ticket 05 / slice S5; commit `876dc5c`): lease-file polling
  (2 s × 60, case-insensitive fixed-MAC match; JSON parser with `jq` /
  `python3` fallback plus legacy 2/3-column fallback — the real
  `virbr0.status` format turned out to be a pretty-printed JSON array, not
  the assumed space-separated columns), `domstate` cross-check, best-effort
  `virsh domifaddr` / `arp -n` fallbacks, the mandatory SSH probe before
  any success output (D6/AC3), timeout errors naming `virsh console <NAME>`,
  and the exact spec §3.1 success block (D9).
- **`bin/vm-destroy` (since removed) — companion teardown script**
  (ticket 06 / slice S6; commit `51828eb`): defined-domain preflight
  (exit 1), destroy-if-running → undefine → `virsh vol-delete
  <NAME>_vda.qcow2 vm-pool`, D7 missing-volume tolerance (warning + exit 0),
  exact confirmation message, no prompt.
- **End-to-end acceptance evidence + hardening** (ticket 07 / slice S7;
  commit `a7cfe93`): `docs/e2e-acceptance.md` with the full recorded
  acceptance run on the real Ubuntu 26.04 (`resolute`) image — AC1 real
  821 MiB download + boot + SSH-proven (`VERSION_ID="26.04"`), AC5 18 ms
  fail-fast name collision, AC2 cache-reuse `stat` pair, AC3 negative leg
  (exit 1 + `virsh console` hint), AC4 autostart evidence + orphan-free
  final pool state; plan §8 updated with the confirmed lease-file format.
- **POC review report** — `docs/reviews/poc-review.md` (Agent 5,
  commit `6cc3a63`): independent re-verification of all safe checks,
  verdict **PASS** on all five acceptance criteria, gaps G1–G8.
- **Final documentation** (this release / commit tagged `v0.1.0`):
  rewritten `README.md` (front door, CLI reference, architecture diagram),
  this `CHANGELOG.md`, and
  `docs/POC-Implementation-Documentation.md` (development narrative,
  diagrams, traceability, verification summary, follow-ups).

### Changed

- **SSH verification window widened from ~60 s to ~90 s**
  (`verify_ssh_reachable`, 30 → 45 attempts at 2 s cadence) after a real
  boot in ticket 06 needed slightly longer than 60 s; the only functional
  change in the S7 hardening pass (ticket 07; commit `a7cfe93`).
- **`README.md`** — replaced the initial stub with a "How to run" section
  (ticket 07; commit `a7cfe93`), then fully rewritten as the project front
  door (final docs commit, `v0.1.0`).

### Fixed

- **Lease-file parser** — adapted from the assumed space-separated
  `<ip> <mac> <hostname>` format to the confirmed pretty-printed JSON array
  format of `/var/lib/libvirt/dnsmasq/virbr0.status` (first real boot in
  ticket 05, commit `876dc5c`; re-confirmed on real resolute boots in
  ticket 07, commit `a7cfe93`), with a legacy-format fallback retained.
- **`meta-data` now includes `local-hostname: <NAME>`** in addition to the
  spec's `hostnames:` block — without it, NoCloud kept the image-default
  hostname `ubuntu`, which would have silently broken the
  name-resolution promise (documented ticket-05 deviation, commit
  `876dc5c`; see review gap G6 for updating the spec in a next revision).

*Tag `v0.1.0` points at the final commit of this release (the final-documentation commit).*
