# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Entries are derived from the actual git history (commits `c35c32b` → v0.1.0).

## [0.1.0] - 2026-08-25

### Added

- **Project scaffold** — repository initialized
  (`c35c32b`, "chore: initial commit — project scaffold").
- **POC specification v1.0** — `specification/specification.md`: purpose,
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
- **`bin/vm-create` — skeleton, CLI parsing, preflight checks**
  (ticket 01 / slice S1; commit `c979e86`): shebang + `set -euo pipefail`,
  all defaults per spec §3.1, usage/help, full flag parsing incl. D1
  `--release`-over-`--image` precedence, `NAME` regex validation, and all
  five preflight checks with exit codes 0/1/2.
- **`bin/vm-create` — image download, SHA256 verification, cache reuse**
  (ticket 02 / slice S2; commit `865402b`): `fetch_and_verify_image()` with
  the `~/vm-images/<release>/` (and `custom/`) cache layout, D4
  download-to-final-name + in-place `sha256sum -c` verification, no
  partial/corrupt files left behind, D1 skip-verify-with-warning path for
  non-derivable sums, `--keep-going`, and the `VM_CREATE_CACHE_DIR` test
  hook.
- **`bin/vm-create` — cloud-init file generation**
  (ticket 03 / slice S3; commit `36a8bf1`): `generate_cloud_init_files()`
  writing NoCloud `user-data` (injected SSH key, `--user` per D2,
  passwordless sudo, `package_update: false`, no `network:` section) and
  `meta-data` (D3-correct `hostnames` block, fresh lowercase UUID, plus
  `local-hostname:` required for NoCloud to apply the hostname — recorded
  deviation) into a `mktemp` dir with `chmod 600` + trap cleanup, and the
  `VM_CREATE_CLOUD_INIT_DUMP` test hook.
- **`bin/vm-create` — VM creation via `virt-install`**
  (ticket 04 / slice S4; commit `5e55cb9`): fixed-MAC generation
  (`52:54:00:` + 3 random bytes, colon-formatted), the full virt-install
  invocation (fixed `vm-pool` disk `<NAME>_vda.qcow2`, `default` network,
  `--os-variant ubuntu-lts-latest`, file-based `--cloud-init` with
  `disable=on`, `--autostart`), D5 `--no-boot` stop-after-create with the
  not-booted output variant; two documented, evidence-backed deviations for
  virt-install 4.1.0 (pre-created volume + `--disk vol=` instead of
  `--location --import` + `--disk size=`; `--extra-args console=ttyS0`
  omitted — both recorded in the script header).
- **`bin/vm-create` — wait-for-IP, mandatory SSH verification, access info**
  (ticket 05 / slice S5; commit `876dc5c`): lease-file polling
  (2 s × 60, case-insensitive fixed-MAC match; JSON parser with `jq` /
  `python3` fallback plus legacy 2/3-column fallback — the real
  `virbr0.status` format turned out to be a pretty-printed JSON array, not
  the assumed space-separated columns), `domstate` cross-check, best-effort
  `virsh domifaddr` / `arp -n` fallbacks, the mandatory SSH probe before
  any success output (D6/AC3), timeout errors naming `virsh console <NAME>`,
  and the exact spec §3.1 success block (D9).
- **`bin/vm-destroy` — companion teardown script**
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
