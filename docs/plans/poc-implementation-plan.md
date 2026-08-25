# POC Implementation Plan — `vm-create` / `vm-destroy`

**Version:** 1.0 · **Date:** 2026-08-25 · **Author:** Agent 2 (Implementation Planner)
**Upstream:** [`specification/specification.md`](../../specification/specification.md) (v1.0, authoritative) ·
research doc: `~/vault/04-Technology/research-linux-virtualization.md`

---

## 1. Overview & approach

Two plain-bash CLI tools, one script per command:

- `bin/vm-create` — preflight → image download/verify (cached) → cloud-init
  file generation → `virt-install --import` (fixed MAC, `vm-pool`, `default`
  net, `ubuntu-lts-latest` osinfo) → wait-for-DHCP-lease → mandatory SSH
  verification → exact success block from spec §3.1.
- `bin/vm-destroy` — destroy (if running) → undefine →
  `virsh vol-delete <NAME>_vda.qcow2 vm-pool` (tolerant of missing volume,
  spec §3.2 / D7).

Ground rules for the implementation:

1. `#!/usr/bin/env bash` + `set -euo pipefail` in both scripts.
2. **No new dependencies** beyond what Environment Verification (§8) confirms
   installed: `virsh`, `virt-install` 4.1.0, `curl`, `sha256sum`, `uuidgen`,
   `awk`, `ssh`, `ssh-keygen`. (`genisoimage` exists but is not needed —
   virt-install builds the NoCloud ISO internally.)
3. Exit codes per spec §3.1: `0` success, `1` runtime failure, `2` usage.
4. All spec behaviors, pitfalls (§6) and decisions (D1–D10) are normative;
   this plan only sequences them into slices and records host-specific
   adaptations.
5. **Testability hooks** (planner decision): two environment-variable
   overrides, documented in the script header, used *only* by slice
   verifications — never required at runtime:
   - `VM_CREATE_CACHE_DIR` — relocate `~/vm-images` (default unchanged).
   - `VM_CREATE_LEASE_FILE` — relocate the dnsmasq lease file (default
     `/var/lib/libvirt/dnsmasq/virbr0.status`).
   This lets S2/S5 verify download/verify and lease-parsing logic with
   fixtures in `/tmp` without touching the real cache or root-owned files.
6. Scripts live in the project `bin/` and are `chmod +x`. The E2E slice runs
   them by absolute path; a later follow-up may symlink them into `~/bin`.
7. Every `vm-create` run for verification uses the `poc-` name prefix
   (Testing Strategy, §4).

Planner decisions recorded here (all recorded in the plan per task rules;
they do not contradict the spec):

- **PD1 — S5 first real boot uses the host's existing Noble image.**
  A 674 MiB `noble-server-cloudimg-amd64.img` already sits in `vm-pool`
  (`/var/lib/libvirt/images/vms/`, root-owned, world-readable). S5 verifies
  the full boot→lease→SSH path against that image via the spec's existing
  `--image` override (`file:///var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img`),
  which exercises D1's "custom image, no derivable SHA256SUMS → skip
  verification and warn" path. The **real 820 MiB resolute download happens
  only in S7**, keeping the suggested slice boundaries intact. Noble uses the
  same `ubuntu` key-auth convention, so the code path is identical.
- **PD2 — Default SSH key must be generated before real boots.**
  `~/.ssh/id_ed25519.pub` does **not** exist on this host (see §8). The
  spec's default `--ssh-key` path would fail preflight. S5's first real-boot
  step generates it idempotently (`ssh-keygen -t ed25519 -N '' -f
  ~/.ssh/id_ed25519` only if absent) so the spec-default path is exercised
  unmodified. Existing host keys (`id_rsa`, `id_forgejo`, …) are never used
  or modified.
- **PD3 — Acceptance criterion 4's host-reboot leg is verified by XML
  evidence, not by rebooting the host.** Rebooting the host mid-POC would
  kill the orchestrating session and other work. S7 records
  `virsh dumpxml poc-1 | grep -A1 autostart` (must show `<autostart/>`
  enabled) plus the `vm-destroy` no-orphan proof; a real reboot is noted in
  `docs/e2e-acceptance.md` as an optional manual check.
- **PD4 — The pre-existing `vm-pool` volume `noble-server-cloudimg-amd64.img`
  is a known fixture.** Every "no orphan qcow2" check must account for it
  (expected pool contents after teardown: exactly that one volume).

---

## 2. Vertical-slice decomposition

Slices are executed **strictly in order** S1 → S7. Each slice ends with its
verification passing on this host and a single git commit. Every slice is an
end-to-end increment: it adds *working* functionality that can be invoked
and observed, never a horizontal "all the plumbing first" layer.

### S1 — Project skeleton + `vm-create` preflight

- **Scope (files):** `bin/vm-create` (new: shebang, `set -euo pipefail`,
  constants from spec §4, full usage/help text, argument parsing for all
  flags incl. `--release`/`--image` precedence per D1, name validation
  regex `^[A-Za-z][A-Za-z0-9._-]*$`), `.gitignore` (new: `*.tmp` if needed),
  `bin/vm-destroy` placeholder **not** created yet.
  The script body after preflight is a stub that prints a clear
  "not implemented in this slice" message and exits 1 — so the CLI surface
  (flags, precedence, error paths) is fully exercised while later slices
  replace the stub one feature at a time.
- **Why it is a complete slice (verification, all run on this host):**
  - `bin/vm-create` (no args) → exit 2 + usage on stderr.
  - `bin/vm-create --ram 2` (no NAME) → exit 2 + usage.
  - `bin/vm-create poc-x --bogus` (unknown option) → exit 2 + usage.
  - `bin/vm-create -bad` / `bin/vm-create .bad` (invalid NAME regex) → exit 1
    or 2 with clear message (planner: invalid NAME is a usage error, exit 2).
  - `bin/vm-create setup-test-vm` (pre-existing domain) → exit 1,
    `VM 'setup-test-vm' already defined`-style error, **no side effects**
    (proves the duplicate-name preflight against a real domain).
  - `bin/vm-create poc-preflight --ssh-key /nonexistent` → exit 1, "ssh key
    not found" (real negative case, since no default key exists — see §8).
  - `bin/vm-create poc-preflight --help`-free normal run with a valid key
    (`--ssh-key ~/.ssh/id_rsa.pub`) → passes preflight, hits the stub
    "not implemented" exit 1. (Proves libvirt/pool/net/key checks all pass.)
- **Dependencies:** none.
- **Key risks:** none significant. Watch: `virsh dominfo` exit-status
  semantics for the duplicate-name check; `set -u` interactions with the
  option-parsing loop.

### S2 — Image download + SHA256 verification + cache reuse

- **Scope:** extend `bin/vm-create` — `fetch_and_verify_image()` implementing
  spec §4 step 2 + D4 (download to final name, verify in place, delete on
  failure, `--keep-going` fallback to existing cache, D1 custom/`--release`
  URL + cache layout `~/vm-images/<release>/`, `VM_CREATE_CACHE_DIR` hook).
  The stub after the download step now reports the image path, then exits 1
  (cloud-init still not implemented).
- **Why it is a complete slice (verification — no 820 MiB download):**
  Build a fixture in `/tmp/virt-test-s2/`: a small file
  `demo-server-cloudimg-amd64.img` (~1 KiB of `dd if=/dev/zero`), plus a
  `SHA256SUMS` in the same directory generated with `sha256sum`. Then, with
  `VM_CREATE_CACHE_DIR=/tmp/virt-test-s2/cache`:
  - Run with `--image file:///tmp/virt-test-s2/demo-server-cloudimg-amd64.img`
    → image copied/cached under `cache/custom/`, sums-fetched-and-verified
    path succeeds (or the documented warn path if `file://` sums fetch is
    unsupported — the implementer must make same-directory sums fetch work
    over `file://` and record which branch ran).
  - **Corruption test:** flip a byte in a second copy's expected hash /
    truncate the fixture → run → exit 1, "SHA256 mismatch", partial/final
    file removed from cache.
  - **Reuse test:** re-run with a fresh cache-missing scenario satisfied →
    second run skips download entirely (assert via a marker: remove the
    source fixture after first run; second run must still succeed from cache).
  - **`--keep-going` test:** point `--image` at a dead URL with a valid
    cached image present → warning + continue; without cache → exit 1.
  - `rm -rf /tmp/virt-test-s2` afterwards.
- **Dependencies:** S1.
- **Key risks:** SHA256SUMS filename-matching subtlety (D4: sums list the
  *final* name); `file://` support in the sums-derivation logic; ensuring a
  failed download never leaves a `.part`/corrupt final file in the cache;
  820 MiB real download only in S7 — S2 must not accidentally hit it.

### S3 — Cloud-init user-data / meta-data generation

- **Scope:** extend `bin/vm-create` — `generate_cloud_init_files()` per spec
  §4 step 3 + D3 (correct `hostnames` YAML, no `network:` section,
  `package_update: false`, `manage_etc_hosts: true`, `--user` per D2,
  `mktemp -d` + `chmod 600` + trap cleanup, fresh lowercase UUID via
  `uuidgen | tr A-Z a-z`). Stub after cloud-init reports the temp dir,
  exits 1.
- **Why it is a complete slice (verification):**
  Add a test-only hook (`VM_CREATE_CLOUD_INIT_DUMP=/tmp/path` env var, or a
  `--debug` flag — implementer picks one, documents it) that copies the
  generated files before the exit. Then:
  - Generated `meta-data` and `user-data` both parse as YAML:
    `python3 -c 'import yaml,sys; yaml.safe_load(open(sys.argv[1]))' <file>`
    (`python3` + PyYAML present on host; if PyYAML missing, fall back to a
    structural grep check and record that).
  - Content assertions: `hostnames.local`/`hostnames.host` == NAME;
    `id` is a lowercase UUID; user-data has `#cloud-config` header,
    `name: <user>` matching `--user`, the exact public-key line from
    `--ssh-key` file, `sudo: ["ALL=(ALL) NOPASSWD:ALL"]`,
    `package_update: false`, and **no** `network:` key anywhere.
  - Run twice → the two `id:` values differ (fresh UUID per VM).
  - `ls /tmp` after exit shows no leaked temp dirs (trap works).
- **Dependencies:** S1 (S2 not required, but S2 already in place — ordering
  preserved).
- **Key risks:** YAML indentation (D3 was a bug in the research doc — the
  classic failure); multi-line/malformed key content in `ssh_authorized_keys`;
  `--user` propagation (D2).

### S4 — VM creation via `virt-install --import`

- **Scope:** extend `bin/vm-create` — MAC generation
  (`52:54:00:$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')`), the full
  `virt-install` invocation from spec §4 step 4 (all flags + rationale
  table honored: `--os-variant ubuntu-lts-latest`, `--import --location`,
  `--extra-args console=ttyS0 --console none`, full
  `--cloud-init user-data=…,meta-data=…,clouduser-ssh-key=…,disable=on`,
  `--autostart`), plus the D5 `--no-boot` stop-after-create handling and its
  alternate output block.
- **Why it is a complete slice (verification — real libvirt object, tiny
  image, no 820 MiB download, no real boot needed):**
  `bin/vm-create poc-s4 --no-boot --ram 1 --vcpu 1 --disk 2 \
     --image file:///tmp/virt-test-s4/tiny.img \
     --ssh-key ~/.ssh/id_rsa.pub` where `tiny.img` is a few-MiB zero file
  (`virt-install --import` copies bytes; a blank disk is fine when the VM is
  stopped immediately). Then assert, in order:
  - `virsh list --all | grep poc-s4` → state **shut off** (D5 stop worked).
  - `virsh dominfo poc-s4` → UUID present; `virsh dumpxml poc-s4` contains
    `<autostart/>`, MAC `52:54:00:…` (12 hex after OUI),
    `type='network' name='default'`, disk source in `vm-pool`.
  - `virsh vol-list vm-pool` → `poc-s4_vda.qcow2` present
    (the exact name `vm-destroy` will later rely on — record it as evidence
    for S6).
  - `virsh domstate poc-s4` = `shut off` and the script printed exactly the
    not-booted output variant (no IP/SSH lines).
  - **Cleanup (mandatory, part of the slice):** `virsh destroy poc-s4`
    (no-op if off) `&& virsh undefine poc-s4 &&
    virsh vol-delete poc-s4_vda.qcow2 vm-pool`; re-assert the pool shows no
    `poc-*` volume. (S6's `vm-destroy` doesn't exist yet — raw virsh is the
    sanctioned cleanup here.)
- **Dependencies:** S2 (image path), S3 (cloud-init files).
- **Key risks:** `virt-install` import of a blank/foreign "image" — should
  succeed (it's a byte copy) but if virt-install rejects a non-bootable
  source, fallback is to use a small real slice of a cloud image or skip
  boot-irrelevance (record the actual behavior); cloud-init ISO generation
  failure modes on 4.1.0 file syntax; `--no-boot` race (virt-install
  auto-starts — the `domstate`-guarded destroy must handle VM already
  finished/stopped); disk volume naming assumption (`<NAME>_vda.qcow2`) is
  *proven* here, de-risking S6.

### S5 — Wait-for-IP + access-info output + mandatory SSH verification

- **Scope:** extend `bin/vm-create` — spec §4 steps 5–6: lease-file polling
  (2 s × 60, MAC case-insensitive match, `VM_CREATE_LEASE_FILE` hook),
  domstate cross-check, SSH probe loop (`ssh -o ConnectTimeout=2 -o
  BatchMode=yes -o StrictHostKeyChecking=accept-new … exit`, ~60 s),
  fallbacks (`virsh domifaddr`, `arp -n`) best-effort only, exact §3.1
  output block (D9 — including `Name:`/UUID line and field alignment),
  timeout errors naming `virsh console <NAME>`.
- **Why it is a complete slice (two verifications — fixture + real first
  boot):**
  1. *Fixture (no VM):* write a fake lease file in `/tmp/virt-test-s5/`
     with the assumed 3-column format (`192.168.122.77 52:54:00:aa:bb:cc
     poc-fake`) and exercise the parser (via the test hook or an extracted
     `find_ip_for_mac()` invoked from a small test snippet) → correct IP
     extracted; wrong-MAC line → no match; 2-column line → tolerated via
     fallback path. This pins the parser contract before the first real
     lease exists.
  2. *Real first boot (the lease-format verification the spec demands):*
     per PD1/PD2 — `ssh-keygen` the default ed25519 key if absent, then:
     `bin/vm-create poc-s5 --ram 2 --vcpu 1 --disk 5 \
        --image file:///var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img`
     (PD1; expect the documented "no derivable SHA256SUMS — verification
     skipped" warning). Assert: script blocks until SSH actually succeeds
     (20–40 s first boot), prints the exact success block, and
     `ssh ubuntu@<printed IP> 'hostname'` returns `poc-s5` (proves
     meta-data hostname + `manage_etc_hosts`). **Record the real
     `virbr0.status` line format** (columns, MAC case) into the plan
     evidence / `docs/e2e-acceptance.md` draft notes; if it differs from the
     assumed `<ip> <mac> <hostname>`, adapt the awk parser *now* (this is
     the slice's explicit contingency) and re-run. Then `ssh
     ubuntu@poc-s5.default 'hostname'` proves libvirt DNS.
     **Cleanup (mandatory):** `virsh destroy poc-s5 && virsh undefine poc-s5
     && virsh vol-delete poc-s5_vda.qcow2 vm-pool`; re-assert no `poc-*`
     volume in `vm-pool`.
- **Dependencies:** S4.
- **Key risks:** (a) lease file format unverified (was 0 bytes at planning
  time — §8) — the single riskiest unknown; the fixture pins the contract,
  the real boot resolves it, parser adaptation is an in-scope contingency;
  (b) Noble image is 24.04, not 26.04 — same user/SSH conventions, but if
  anything differs it's a *guest-version* surprise, not a code surprise;
  (c) first-boot 20–40 s vs the 120 s window — plenty of margin, but a
  hung cloud-init (missing metadata → EC2 wait) would consume it; the
  timeout message must point at `virsh console`; (d) the 674 MiB
  `file://`→cache copy under `custom/` (PD1) is a one-off cost, ~1 GB total,
  well within 227 GiB free.

### S6 — `vm-destroy` companion

- **Scope (files):** `bin/vm-destroy` (new): usage (exit 2 on no arg),
  defined-domain preflight (exit 1 `VM '<NAME>' is not defined`), destroy-if-
  running → undefine → `virsh vol-delete <NAME>_vda.qcow2 vm-pool` with
  missing-volume tolerance (D7, exit 0 + note), exact confirmation message
  from spec §3.2.
- **Why it is a complete slice (verification — cheap real objects):**
  - `bin/vm-destroy` (no args) → exit 2 + usage.
  - `bin/vm-destroy poc-never-existed` → exit 1, clear "not defined" error.
  - Create a disposable VM exactly as in S4
    (`bin/vm-create poc-s6 --no-boot … --image file:///tmp/…tiny.img`),
    then:
    - `bin/vm-destroy poc-s6` on the **shutoff** VM → exit 0,
      `VM 'poc-s6' destroyed and disk removed.`
    - `virsh list --all | grep poc-s6` → empty;
      `virsh vol-list vm-pool | grep poc-s6` → empty (no orphan).
  - **Running-VM path:** recreate `poc-s6` *booted* (Noble `file://` image
    from S5's cache — now cached under `custom/`, so no re-copy), `bin/vm-destroy
    poc-s6` while running → destroy+undefine+vol-delete, same assertions.
  - **Missing-volume path (D7 idempotency):** re-run `bin/vm-destroy poc-s6`
    → exit 1 "not defined" (hard error, per spec). Separately, to exercise
    the missing-*volume* branch, destroy a second `poc-s6` VM, manually
    `virsh vol-delete` its volume first, then run `bin/vm-destroy` → exit 0
    with "volume already absent" note. (If the branch is unreachable without
    a defined domain + pre-deleted volume, implementer documents the
    exact reachable path and the unit-ish check used.)
  - **Never-touch proof:** at the end, `virsh list --all` shows
    `setup-test-vm` untouched (still shut off); `virsh vol-list vm-pool`
    shows only the pre-existing `noble-server-cloudimg-amd64.img` (PD4).
- **Dependencies:** S4 (volume-naming proof), S5 (not strictly, but keeps
  ordering; the running-VM test reuses S5's cached image).
- **Key risks:** `vol-delete` on a volume backed by a running/attached disk
  (must destroy first — ordering matters); missing-volume exit-0 branch vs
  the spec's "report a warning but exit 0"; leaving an orphan if the script
  dies between undefine and vol-delete (mitigation: `set -e` + the S6 tests
  plus S7's final pool assertion).

### S7 — Hardening + full end-to-end acceptance run

- **Scope:** `bin/vm-create` / `bin/vm-destroy` (hardening fixes found in
  earlier slices and this one: message wording, `set -u` edges, fallback
  order, output alignment vs §3.1); `docs/e2e-acceptance.md` (new —
  recorded evidence: every command + its output, lease-file format finding,
  download/cache-reuse proof, autostart XML evidence per PD3, final
  pool-state listing); `README.md` (short "how to run" update acceptable).
  No behavior changes beyond hardening.
- **Why it is a complete slice — the acceptance script, in order, all on
  this host with the real resolute image (first real download):**
  1. **AC1:** `bin/vm-create poc-1 --ram 2 --vcpu 1 --disk 10` from clean
     state (no `poc-1`, resolute cache absent) → real 820 MiB download,
     SHA256-verified, success block printed; then manually
     `ssh ubuntu@<ip> 'cat /etc/os-release'` shows `26.04` (proves it's
     resolute, not Noble).
  2. **AC5:** immediately re-run the exact same command → exit 1 at
     preflight, "already defined"-style error, *before* any download/VM
     work (record timing to show fail-fast).
  3. **AC2:** `bin/vm-create poc-2 --ram 1 --vcpu 1 --disk 5` (different
     sizes → also proves size parameters) → completes with **no download**
     (evidence: cache file mtime/checksum unchanged before/after per PD4-
     style stat pair, and no download lines in output).
  4. **AC3 (negative leg):** optional if time-safe — create `poc-3` with a
     deliberately broken `--ssh-key` (key never injected, so SSH probe
     fails) → expect exit 1 with the `virsh console` hint within
     ~180 s (lease+SSH windows). Tear down `poc-3` first. *(If this leg is
     deemed too costly, AC3's positive leg from steps 1–3 — SSH proven
     before success output — satisfies the criterion; record the decision.)*
  5. **AC4:** `virsh dumpxml poc-1 | grep -A1 '<autostart'` shows enabled
     (PD3); then `bin/vm-destroy poc-1`, `bin/vm-destroy poc-2` (and
     `poc-3`) → each exit 0 with confirmation.
  6. **Final orphan check:** `virsh list --all` → only `setup-test-vm`
     (shut off); `virsh vol-list vm-pool` → exactly
     `noble-server-cloudimg-amd64.img` (PD4) — no `poc-*` anywhere.
  7. Write `docs/e2e-acceptance.md` with all commands/outputs; update
     `docs/plans/poc-implementation-plan.md` §8 lease-format note with the
     confirmed format.
- **Dependencies:** S1–S6.
- **Key risks:** 820 MiB download time/outage (mitigation: `curl --retry 3`
  already in S2; `--keep-going` exists; the Noble-cached image from S5 is a
  documented escape hatch for the boot/SSH path if the resolute URL fails
  — record that AC1 then re-runs when network permits); first-boot variance;
  S7 must be the *last* slice so the `v0.1.0` tag lands on the final commit.

---

## 3. Slice order & git strategy

Order: **S1 → S2 → S3 → S4 → S5 → S6 → S7**, strictly sequential (each slice
commits only after its verification passes and its test VMs are torn down).

One git commit per slice, Conventional-Commit style:

| Slice | Suggested message |
|---|---|
| S1 | `feat(vm-create): add script skeleton, CLI parsing, and preflight checks` |
| S2 | `feat(vm-create): add image download, sha256 verification, and cache reuse` |
| S3 | `feat(vm-create): generate cloud-init user-data and meta-data` |
| S4 | `feat(vm-create): create VM via virt-install --import with fixed MAC` |
| S5 | `feat(vm-create): wait for DHCP lease, verify SSH, print access info` |
| S6 | `feat(vm-destroy): add companion teardown script` |
| S7 | `test(e2e): record full acceptance evidence and apply hardening fixes` |

- `git add -A` per slice (test artifacts live in `/tmp`, never in the repo).
- The documentation agent will tag **`v0.1.0`** on the S7 commit (final
  commit). No tags are created by the implementation slices.
- The planner commit for *this* document already follows the same
  convention (`docs(plan): …`).

## 4. Testing strategy

**What is verified per slice:**

| Slice | Level | What's proven |
|---|---|---|
| S1 | Unit-ish shell (no VM touched) | CLI contract, exit codes, preflight pass/fail paths |
| S2 | Unit-ish shell (fixture files in `/tmp`) | Download, sums verify, corruption rejection, cache reuse, `--keep-going` |
| S3 | Unit-ish shell (YAML + content asserts) | Valid cloud-config, key/hostname/user/UUID correctness, temp cleanup |
| S4 | **Real libvirt objects** (tiny image, `--no-boot`) | Domain created shutoff, fixed MAC, autostart, `vm-pool` volume named `<NAME>_vda.qcow2` |
| S5 | Fixture parser + **real first boot** (Noble `file://`, PD1) | Lease-file format confirmed/adapted, 20–40 s boot, SSH proven before success output, DNS name, hostname |
| S6 | **Real libvirt objects** (cheap `poc-*` VMs, shutoff + running + missing-volume) | Full teardown paths, idempotency semantics, no orphans, pre-existing objects untouched |
| S7 | **Full E2E** (real resolute image) | All 5 acceptance criteria, recorded in `docs/e2e-acceptance.md` |

Unit-ish shell checks suffice for S1–S3 (pure logic, no privileged state).
A real (stopped) VM is needed from S4 (volume naming + import behavior). A
real *booted* guest is needed only from S5 (lease format + SSH) and S7
(resolute end-to-end).

**Safety rules (normative for every slice):**

1. Every test VM name starts with `poc-` (or the slice tag `poc-sN`).
2. Every VM created during a slice is destroyed before the slice is
   committed: `virsh destroy` (if running) → `virsh undefine` →
   `virsh vol-delete <NAME>_vda.qcow2 vm-pool`; the slice's final state
   check re-asserts the pool contains no `poc-*` volume.
3. Never leave orphans in `vm-pool`: expected steady-state pool contents are
   exactly the pre-existing `noble-server-cloudimg-amd64.img` (PD4).
4. Never touch pre-existing objects: VM `setup-test-vm` (verify it remains
   `shut off` at the end of S6 and S7) and the Noble volume.
5. Never modify root-owned files (`/var/lib/libvirt/**`), the host's
   `~/.ssh` (except the one idempotent default-key generation, PD2), or any
   libvirt pool/network configuration. `VM_CREATE_*` test hooks keep all
   fixtures in `/tmp/virt-test-sN/`, removed at slice end.
6. No `sudo` anywhere; all commands must work as the current user.

## 5. Acceptance-criteria mapping

| # | Acceptance criterion (spec §7) | Satisfied by |
|---|---|---|
| 1 | `vm-create poc-1 --ram 2 --vcpu 1 --disk 10` succeeds unattended from clean state | **S7 step 1** (real resolute image; `cat /etc/os-release` = 26.04 over SSH) |
| 2 | Exactly one ~820 MiB download; second same-release VM reuses cache | **S2** (mechanism: cache-skip, reuse test) + **S7 step 3** (evidence: `poc-2` created with cache mtime/checksum unchanged) |
| 3 | IP printed only after `ssh ubuntu@<ip>` actually succeeds; else clear timeout error with `virsh console` hint | **S5** (real boot: SSH probe before success output; fixture: parser contract) + **S7 step 4** (negative leg, optional per S7 note) |
| 4 | VM survives host reboot (`--autostart`); teardown removes VM + disk, no orphan qcow2 in `vm-pool` | **S4** (autostart in XML) + **S6** (teardown paths, orphan-free pool) + **S7 step 5–6** (autostart XML evidence per PD3; `vm-destroy` ×2; final pool check = only the pre-existing Noble volume) |
| 5 | Re-run with same name fails fast at preflight with clear error | **S1** (mechanism: duplicate-domain check, proven vs real `setup-test-vm`) + **S7 step 2** (evidence: exact `poc-1` re-run, fail before download/VM work) |

## 6. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Lease file format differs from assumed `<ip> <mac> <hostname>`** (file was 0 bytes at planning time) | Medium | S5 fixture pins the parser contract up front; S5's real Noble boot is the *designated* point to confirm the format; parser adaptation (awk columns / `tolower`) is in-scope there; fallbacks (`virsh domifaddr`, `arp -n`) already specified best-effort; worst case IP discovery stays manual via `virsh console` and the script's timeout error names it. |
| **osinfo-db gap** (no `ubuntu-26.04`; `ubuntu-lts-latest` → 24.04) | Certain (known) | Use `--os-variant ubuntu-lts-latest` everywhere (spec §4). Defaults (virtio disk/NIC, x86_64) identical for 26.04. Revisit only if `osinfo-db` is updated — noted in `docs/e2e-acceptance.md`. |
| **MAC collision** with an existing guest | Low | Fixed scheme `52:54:00:` + 3 random bytes (24 bits); S4 asserts the assigned MAC matches what was passed; a collision would surface as a wrong guest answering the lease — acceptable residual risk for a POC, noted in S7 doc. |
| **820 MiB download time / network failure** | Medium | `curl -fL --retry 3`; SHA256 gate; `--keep-going` + cache; S7 escape hatch: boot/SSH path already proven in S5 via the cached Noble image, so a resolute outage delays only the AC1 image leg, not the whole acceptance. |
| **First-boot takes 20–40 s (or more)** | Low–medium | 120 s lease window (60×2 s) and 60 s SSH window per D8; `package_update: false` keeps first boot fast; timeout errors include the `virsh console <NAME>` hint; `console=ttyS0` guarantees a debug path even headless. |
| **`virt-install --import` rejects the tiny/blank S4 test image** | Low | If rejected, S4 fallback: carve a few MiB from a real cloud image header or use a small real image (Alpine/noble slice) — record the actual behavior in the S4 commit message. |
| **A step fails mid-slice (partial VM state)** | Medium | Safety rule 2: each slice ends with an explicit teardown + pool re-check before committing; raw `virsh destroy/undefine/vol-delete` is the manual recovery; S7's final orphan check is the backstop. |
| **Cloud-init hangs waiting for EC2 metadata** (missing metadata) | Low | Metadata is *always* attached (`--cloud-init` with user-data/meta-data — bare `--cloud-init` explicitly forbidden); S5 would catch it as a lease timeout with the console hint. |
| **`~/.ssh/id_ed25519.pub` absent (spec default key)** | Certain (known) | PD2: idempotent generation in S5 before first real boot; preflight's key-exists check (S1) makes the failure loud, never silent. |

**What to do if a step fails (general):** stop, capture command + output +
relevant state (`virsh list --all`, `virsh vol-list vm-pool`,
`virsh dominfo`, lease file), tear down any `poc-*` state per safety rule 2,
fix forward within the same slice, and re-run the slice's verification from
the start. Never commit a slice whose verification is failing; never carry a
`poc-*` VM or volume into the next slice.

## 7. Out-of-scope reminder for implementers

Per spec §8: no `vm-list`, no `--template`, no static IPs, no UEFI, no
inbound NAT rules, no `--clean-cache`. `bin/` scripts are invoked by path
for the POC; `~/bin` symlinks are a follow-up.

## 8. Environment verification (read-only, 2026-08-25)

Gathered with read-only commands only; nothing was created, modified, or
destroyed.

| Check | Result |
|---|---|
| `virsh list --all` | Only `setup-test-vm` — **shut off** (pre-existing; must never be touched) |
| `virsh pool-list` | `vm-pool` — **active**, autostart yes |
| `virsh net-list` | `default` — **active**, autostart yes, persistent |
| `virsh vol-list vm-pool` | One pre-existing volume: `noble-server-cloudimg-amd64.img` (→ PD4: this is the expected steady-state pool contents) |
| `virt-install --version` | 4.1.0 (file-based `--cloud-init` sub-options only — spec constraint confirmed) |
| `qemu-img --version` | 8.2.2 |
| `ls -la /var/lib/libvirt/dnsmasq/` | `virbr0.status` present, **0 bytes**, mode 644 (world-readable); `virbr0.macs` present (5 bytes, empty array); no leases currently |
| `cat /var/lib/libvirt/dnsmasq/virbr0.status` | **Readable but empty** — lease-file format still unconfirmed; S5 remains the designated format-verification point (no fallback slice needed: file is readable, so the primary path is viable) |
| `which genisoimage curl sha256sum uuidgen` | All present: `/usr/bin/genisoimage`, `/usr/bin/curl`, `/usr/bin/sha256sum`, `/usr/bin/uuidgen` |
| `osinfo-db` (extra) | `0.20250606-0ubuntu0.24.04.1` — pre-26.04, confirms the osinfo gap; `ubuntu-lts-latest` workaround required |
| `~/.ssh` (extra) | **No `id_ed25519.pub`** (default key absent — drives PD2); existing keys (`id_rsa.pub`, `id_forgejo.pub`, …) usable as alternate `--ssh-key` for pre-S5 tests |
| `~/vm-images` (extra) | Does not exist — clean state; first real resolute download happens in S7 |
| Disk / KVM (extra) | 227 GiB free on `/` (`/dev/nvme0n1p8`, 856 G total); `/dev/kvm` present (`crw-rw----+ root kvm`) |
| `python3`, `ssh`, `ssh-keygen` (extra) | All present (`/usr/bin/`) — S3 YAML checks and PD2 are viable |
| Noble image (extra) | `/var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img` — 674 MiB, root-owned, **world-readable** → PD1 feasible |
| Host | Ubuntu 24.04 user `verfeinerer` in `libvirt`+`kvm` groups (all `virsh` calls above ran without sudo); `libvirtd` socket-activated |

**Environment-driven adaptations:** none of the suggested slice boundaries
had to change. PD1 (Noble `file://` image for S5's first real boot) *preserves*
the suggested layout — "the real resolute download happens in the final E2E
slice" — instead of pulling it forward. PD2 (default SSH key generation) and
PD3 (autostart verified via XML, no host reboot) are small, recorded
decisions forced by the host state. PD4 (known pool fixture) shapes every
orphan check.
