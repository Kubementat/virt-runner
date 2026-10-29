# Handoff: implement snapshots and clones

> **Historical.** Implemented on `feature/snapshots` (2026-10-29). The README section "Snapshots and clones" and the code are the reference now; this file records how the feature was designed.

Status: ready to implement. Written 2026-10-28 against `develop` at `e9c97f4` (v0.7.0).
Nothing has been implemented yet.

This is the entry point for the agent that implements the feature. Everything below is a
pointer into the research document, which is the source of truth:
[`snapshot-generation-and-loading-research.md`](snapshot-generation-and-loading-research.md).
§ numbers in this file refer to that document.

## 1. Read first, in this order

1. `AGENTS.md` and `CONTRIBUTING.md`: workflow, test rules, release rules.
2. Research §1 (summary), §5 (CLI and JSON contracts), §6 (implementation plan), §9
   (real-VM results). §4 only when you need the reasoning behind a decision.
3. `docs/lessons-learned/`, especially `005` (virtiofs), `008` (seed ISO deletion), `011`
   (the unit suite must not spawn virsh), `013` (run the virsh command before coding the
   flag).
4. The code the plan extends: `src/virt_runner/cmd_create.py`, and in `virtualizer.py`
   `render_meta_data` (:504), `provision_volume` (:994), `create_vm` (:1042),
   `set_domain_metadata` (:1172), `reboot` (:1923), and `_FS_GROW_SCRIPT` with its ssh
   delivery (:1979-2034). Line numbers are as of `e9c97f4`.

## 2. What the user wants (one paragraph)

Create a VM as today and work on it, for example by installing k3s and kubementat. Then
take a snapshot of it with one command. The snapshot outlives the source VM. From that
snapshot, start any number of new VMs in seconds. Each new VM is an exact copy of the disk,
but gets its own domain name, MAC, IP, machine-id and SSH host keys, and several can run at
once. The first consumer is kubementat's E2E harness: it snapshots after install and then
re-runs a single feature test in about a minute instead of about 9 (research §7, "How
kubementat's E2E harness would use it").

## 3. Decisions that are locked: do not re-open them

| # | Decision | Where |
|---|---|---|
| D1 | Cold, flattened copy into `vm-pool`. No libvirt domain snapshots, no memory state. | §1, §4.1 |
| D2 | Clones are thin qcow2 overlays backed by the snapshot volume. | §1, §4.1 |
| D3 | Identity comes from re-running cloud-init: an in-guest seal script before the copy, then a new NoCloud seed per clone. No libguestfs. | §4.2.3 |
| D4 | CLI: a `snapshot` group (`create`, `list`, `show`, `delete`, alias `snap`) plus `create NAME --from-snapshot SNAP [--keep-hostname]`. A clone's envelope is the normal `vm-create` shape with `image: null` and an added `snapshot` section. | §4.4, §5 |
| D5 | Store: `<SNAP>.snapshot.qcow2` plus a raw sidecar volume `<SNAP>.snapshot.json` (schema v1) in `vm-pool`, written and read only through `virsh`. No sudo. | §4.5, §5.6 |
| D6 | **User decision:** after `snapshot create` the source stays **shut off**, and this is communicated in three places: a notice in text output, `source.state`/`source.sealed` in JSON, and the README. Verified wording: restarting the source re-runs cloud-init and gives **new SSH host keys only**. machine-id and IP stay the same (§9 row 4). | §7 open question 1 |
| D7 | **User decision:** clones get only the mounts passed as `--mount` at clone time and never inherit recorded mounts. The seal strips virt-runner's own `mountN … virtiofs` fstab lines, so a clone can mount any host directory at any target (§9 row 5). | §7 open question 2, §5.6 |
| D8 | **User decision:** with `--keep-hostname`, `vm.dns_name` is `null`. Without it, the clone keeps `<name>.default` as today. | §7 open question 4 |
| D9 | `snapshot delete` refuses with `snapshot-in-use` while any pool volume is backed by the snapshot. There is no `--force`. libvirt itself does **not** refuse (§9 row 9), so the check lives entirely in virt-runner. | §5.5 |
| D10 | Out of scope for v1: `--reflink` (rejected by libvirt for qcow2, §9 row 11), `--sysprep guestfs`, k3s-specific flags, memory snapshots, a `--no-touch-source` sealer. | §7 |

## 4. Verified facts the code must respect (from §9)

- The seal must use `uname -n`, not `hostname`. Arch has no `hostname` binary, so the seal
  exits 127 (row 10). Keep the script to coreutils, `sed` and `cloud-init`.
- The `vol-create-from` XML **must** contain `<compat>1.1</compat>`. Without it the snapshot
  is qcow2 v2 (`compat 0.10`) (row 12). Mode `0644`.
- The shutdown timeout defaults to **300 s**. A k3s guest took 91 s (row 7). Never force a
  power-off.
- A snapshot is an uncompressed copy: 0.86 GiB of source data became 2.12 GiB, and a k3s VM
  became 3.25 GiB. Report `allocated_gib`.
- `vol-dumpxml` of an overlay lists `<backingStore><path>`, including dangling paths after
  the backing volume was deleted. That is what the dependents scan reads (rows 3, 9).
- Backing-file permissions work as the unprivileged libvirt-group user under AppArmor, with
  no extra steps (row 3).
- `modify --disk` works on a clone unchanged (row 13).
- Timings to keep in the README, from single runs: clone to SSH-ready is 11-14 s on Ubuntu,
  ~20 s on Fedora and ~50 s on Arch. Seal ~1 s, idle shutdown 2-4 s, copy ~1.5 s.
- Fedora: a clone's DHCP lease may carry the source's hostname. This is known and
  low-priority; document it and don't fix it in v1 (§7 risks).

## 5. Workflow (from `AGENTS.md`)

- Create a feature branch in a git worktree off `develop`, for example
  `git worktree add ../virt-runner-snapshots -b feature/snapshots develop`.
- Load the `ponytail` skill and use codegraph (`codegraph_explore`, or `codegraph explore`
  in a shell) to find code.
- Before coding any `virsh`/`qemu-img` call, run that exact command line on the host
  (lesson 013).
- Pure logic goes in `tests/unit_test.py` and must never spawn virsh (lesson 011). Every
  feature needs a case in `tests/integration_test.py`.
- Gates: `./scripts/check.sh --all` and `uv run tests/integration_test.py`, both green.
- When a libvirt/qemu/cloud-init gotcha costs debugging time, write
  `docs/lessons-learned/NNN-<slug>.md`. The next free number is **015**.
- Do **not** bump the version, tag, merge to `main`, push or publish. Add entries under
  `## [Unreleased]` in `CHANGELOG.md` only. The release (MINOR, `0.8.0`) is the
  maintainer's call.

## 6. Work packages

Do them in order. Each one is a reviewable commit with green `check.sh`. They map onto
research §6.1 steps 1-8.

### WP1: pure helpers and the seal script (§6.1 step 1, §6.2)

- `virtualizer.py`: `snapshot_volume`, `snapshot_meta_volume`, `render_meta_data(...,
  hostname=None)`, `_SEAL_SCRIPT` (the §4.2.3 script, verbatim), `snapshot_volume_xml`,
  `backing_path_of`, `parse_snapshot_meta` returning a frozen `SnapshotInfo`.
- Done when the §6.2 unit cases for these helpers pass, including the byte-identical
  `render_meta_data` regression guard and the "no `ssh_host_`, no bare `hostname`" check on
  the seal script.

### WP2: `Virtualizer` methods (§6.1 step 2)

- `seal_guest`, `shutdown_and_wait`, `copy_to_snapshot`, `write_snapshot_meta`,
  `read_snapshot_meta`, `list_snapshots`, `snapshot_dependents`, `delete_snapshot`,
  `create_overlay_volume`, plus the extended `set_domain_metadata` (`release`, `image_url`,
  `mounts`, `snapshot`; old metadata stays valid).
- Every call goes through `_run`/`_spawn`/`_quiet` and raises the §5.7 codes.
- Done when unit tests pin the argv of each virsh call (the lesson 013 technique) and each
  command line has been run once by hand on the host.

### WP3: the `snapshot` command group (§6.1 steps 3, 5, 7)

- New `cmd_snapshot.py` with `create`, `list`, `show` and `delete`. Register it in `cli.py`
  with the `snap` alias, and update the stage list in the `errors.py` docstring.
- `snapshot create` follows the `create` pattern: `run_state`, a `stage` variable,
  `_DEFAULT_CODE`, and one document renderer for success and failure. Stages: `preflight`,
  `seal`, `shutdown`, `copy`, `metadata`, `restart` (§5.1). It also supports
  `--start-source` and `--shutdown-timeout`.
- The D6 notice is printed in text mode.
- Atomicity: a failure in `copy` or `metadata` leaves no snapshot volumes behind.
- Done when the §5.1-§5.3 and §5.5 documents match the examples key for key, and the
  FakeVz flow cases in §6.2 pass (seal failure, copy failure, delete with dependents).

### WP4: `create --from-snapshot` (§6.1 steps 4, 6)

- `cmd_create.py`:
  - add `--from-snapshot` and `--keep-hostname`
  - change `--ram/--vcpu/--disk` to `default=None`, resolved from the snapshot's
    `defaults`, otherwise 4/2/30
  - add the §5.4 usage checks
  - add stage `snapshot` in place of `image`
  - use `create_overlay_volume` in place of `provision_volume`
  - add `vm.hostname`, `vm.snapshot` and a top-level `snapshot` to the document
  - set `image: null` for clones
  - set `dns_name: null` with `--keep-hostname` (D8)
- `cmd_list.py`: add an optional `snapshot` key per VM.
- Done when the §5.4 example document matches, a normal `create` document only gains
  `vm.hostname`, `vm.snapshot: null` and `snapshot: null` (additive, nothing else changes),
  and the §6.2 CLI usage cases pass.

### WP5: integration test (§6.3)

- Add the eight steps of §6.3 to `tests/integration_test.py` (Ubuntu, 1 GiB/1 vCPU/8 GiB).
  Add the names to `ALL`, and add a best-effort `snapshot delete it-snap` to pre-clean.
- Add one D7 check: the source is created with `--mount A:/mnt/a`, a clone gets
  `--mount B:/mnt/b`, and only `/mnt/b` is mounted on the clone.
- Done when `uv run tests/integration_test.py` is green, and `virt-runner list` plus
  `virsh vol-list vm-pool` show nothing left over.

### WP6: docs (§6.1 step 8)

- README:
  - a new "Snapshots and clones" section, including the D6 source notice, the D8
    `dns_name` rule, the k3s `--keep-hostname` caveat, pool-space sizes and timings
  - the error-code rows from §5.7
  - JSON examples
  - a quick-start line
- `skills/virt-runner-cli/SKILL.md` and its `references/json-and-errors.md`.
- `docs/architecture-diagrams/`: the `snapshot` control flow.
- `CHANGELOG.md` `## [Unreleased]` / `### Added`.
- Lessons learned, at least:
  - `015`: virt-runner's own `cloud-init.disabled` marker and the MAC-matched netplan
    block clones. The machine-id/DUID lease clash. Seal before copying.
  - `016`: `vol-create-from` writes qcow2 v2 without `<compat>1.1</compat>`, and libvirt
    deletes a backing volume under a running clone without complaint.
- Afterwards, move or mark the research and handoff documents as historical according to
  `docs/README.md` ("Design documents and plans → `archive/`").

## 7. Definition of done

- [ ] WP1-WP6 committed on `feature/snapshots`. `./scripts/check.sh --all` and
      `uv run tests/integration_test.py` are green on the final tree.
- [ ] Every JSON document matches research §5. New keys are additive only, so existing
      callers (kubementat `tests/e2e/harness/vmm.py`) keep working unchanged.
- [ ] Manual run on Fedora and Arch: seal, snapshot, two clones, distinct IPs. Only then
      does the README claim those distros; otherwise it says "Ubuntu verified".
- [ ] No sudo is used at run time.
- [ ] Nothing is tagged, pushed or published. Hand back with a summary and the branch name.

## 8. Not part of this work

- **The kubementat side** (`VMM.create(from_snapshot=…)`, `--resume`, a resync-and-wait
  clone script) is a separate ticket in the kubementat repo, after this ships. Research
  §7 lists what it needs.
- **aarch64** was never tested (the host is x86_64). Don't claim it.

## 9. Kickoff prompt

Paste this to the implementing agent, from the virt-runner repo root:

> Implement the snapshot and clone feature described in
> `docs/research/snapshot-clone-implementation-handoff.md`. Read that file first and follow
> it: its locked decisions are not up for discussion, and the research document it points
> to is the source of truth for contracts and behavior. Work on `feature/snapshots` in a
> worktree off `develop`, package by package (WP1-WP6), with one commit per package and
> `./scripts/check.sh --all` green after each. Run every new virsh/qemu-img command line by
> hand before you code it. Finish with `uv run tests/integration_test.py` green and no
> leftover VMs or volumes. Do not bump the version, tag, merge, push or publish. Report
> back with the branch name, what was verified on real VMs, and anything that contradicted
> the research.
