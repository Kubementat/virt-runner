# POC Review — `vm-create` / `vm-destroy` (Agent 5: Reviewer/Verifier)

**Date:** 2026-08-25 · **Reviewed commits:** `c979e86` (S1) → `a7cfe93` (S7, HEAD)
**Reviewed artifacts:** `specification/specification.md` v1.0,
`docs/plans/poc-implementation-plan.md` v1.0, `.tickets/ticket-01..07`,
`docs/e2e-acceptance.md`, `bin/vm-create`, `bin/vm-destroy`, `README.md`.

## Overall verdict: **PASS**

All five spec acceptance criteria (§7) are satisfied with fresh, same-day,
recorded E2E evidence in `docs/e2e-acceptance.md` (definitive run
2026-08-25 ~22:05, real resolute image, verbatim outputs incl. real SSH
round-trips). I independently re-ran every safe, idempotent check (CLI
contract, preflight, name collision, lease parser, image
download/verify/cache logic, cloud-init generation) — all pass. The one
real E2E leg (VM boot + SSH + teardown) was **not** re-run by me because
the recorded evidence is fresh and convincing (see *Review environment &
method*); the host's current state matches the recorded post-run state
exactly. No blockers or majors found; minor findings listed below.

---

## 1. Per-slice checklist

| Slice (commit) | Expected (plan §2) | Found | Evidence |
|---|---|---|---|
| **S1** skeleton + preflight (`c979e86`, `bin/vm-create` +205) | CLI parsing of all flags incl. D1 `--release`>​`--image` precedence, name regex, preflight (libvirt, dup-domain, pool, net, ssh-key), exit codes 0/1/2, `set -euo pipefail` | Present in `bin/vm-create` (arg-parse loop, `die_usage`/`die`, all 5 preflight checks in order, positive-int validation). **Re-ran 10 probes myself — all correct:** no args → rc 2 + usage; `--ram 2` no NAME → rc 2; `poc-x --bogus` → rc 2 `unknown option`; `.bad` / `-bad` → rc 2 `invalid VM name`; `--ram 0` → rc 2 `must be a positive integer`; `setup-test-vm` → rc 1 `VM 'setup-test-vm' already defined`; `--ssh-key /nonexistent` → rc 1 `ssh key not found`. State unchanged after all probes | Review run (below); matches ticket-01 AC 1–10 |
| **S2** download + SHA256 + cache (`865402b`, +118/−5) | `fetch_and_verify_image()`, cache layout `<cache>/<release>\|custom/`, `VM_CREATE_CACHE_DIR` hook, D1/D4, `--keep-going`, no partial files left | **Re-tested the function in isolation** (extracted via `sed`, invoked with `VM_CREATE_CACHE_DIR=/tmp/...`, fixtures only, no VM): (1) `file://` image + same-dir `SHA256SUMS` → copied to `cache/custom/`, sums fetched, verified, `image ready`, rc 0; (2) source fixture deleted → second run **cache hit, no download**, rc 0; (3) wrong sums entry → `FAILED`/`SHA256 mismatch`, rc 1, **corrupt file removed from cache** (dir empty after); (4) dead URL, cold cache → rc 1 `download failed`; (5) `--keep-going`, cold cache → rc 1 `keep-going: … no cached image`; (6) `--keep-going`, image cached at final path → warning `reusing cached image (no download)` + `image ready`, rc 0. One minor finding: the post-failure keep-going sub-branch is dead code (see Gaps G1) — the required behavior is delivered by the earlier cache-hit branch | Review run 2026-08-25 22:38–22:45, fixtures in `/tmp/virt-review` (removed after); matches ticket-02 AC 1–5 |
| **S3** cloud-init files (`36a8bf1`, +74/−3) | `generate_cloud_init_files()`, mktemp+trap, chmod 600, fresh lowercase UUID, D2 `--user`, D3 indentation, no `network:`, dump hook | **Re-tested in isolation with `VM_CREATE_CLOUD_INIT_DUMP`:** both files parse as YAML (`python3 yaml.safe_load` OK); `meta-data`: `id` is lowercase UUID (regex match), `hostnames.local/.host` == NAME, plus `local-hostname:` (documented ticket-05 deviation, required for NoCloud to actually apply the hostname); `user-data`: `#cloud-config`, `name: ubuntu` (default) / `name: tester` (with `--user tester`), exact key line `- ssh-ed25519 AAAA_TEST_KEY poc-test`, `sudo: ["ALL=(ALL) NOPASSWD:ALL"]`, `package_update: false`, **zero** `network:` occurrences; two runs produced different `id:` values; the run's temp dirs (`/tmp/tmp.4RFJPSn9G2`, `/tmp/tmp.jJiZ5DK94V`) did not survive (trap works) | Review run 2026-08-25 22:46; matches ticket-03 AC 1–7 |
| **S4** virt-install creation (`5e55cb9`, +76/−3) | virt-install `--import`, fixed MAC `52:54:00:…`, disk `<NAME>_vda.qcow2` in `vm-pool`, `default` net, autostart, D5 `--no-boot` | Implemented, with two **documented, evidence-backed deviations** recorded in the script header: (a) `--location $IMG --import` + `--disk size=` replaced by `virsh vol-create-as` + `virsh vol-upload` + `--disk vol=…` (virt-install 4.1.0 hard-fails the spec-verbatim shape and names self-created volumes `<NAME>.qcow2`, breaking the `_vda` contract); (b) `--extra-args console=ttyS0` rejected by 4.1.0 for imported volumes — omitted. MAC scheme kept (colon-formatted: the spec's literal one-liner produces an unparseable MAC — also recorded). **Volume naming + autostart proven live**: e2e §4.1–4.6 shows real domains with MACs `52:54:00:be:b2:b1` / `1a:f1:cf` / `40:76:d6`, autostart symlinks created at creation time, and `vm-destroy` removing each `<NAME>_vda.qcow2` (a wrong name would have left orphans — it left none) | Code inspection (`bin/vm-create` Step 4 header) + `docs/e2e-acceptance.md` §2, §4.1, §4.5 |
| **S5** wait-for-IP + SSH verify (`876dc5c`, +238/−25) | Lease polling 2 s × 60, case-insensitive MAC match, domstate cross-check, mandatory SSH probe, exact §3.1 block, timeout errors naming `virsh console <NAME>`, lease-format contingency | `find_ip_for_mac` handles the **confirmed JSON lease format** (jq, python3 fallback) plus the legacy 2/3-column format. **Re-tested parser with fixtures myself:** JSON match → `192.168.122.77`; uppercase query MAC → same; no match → empty (non-fatal); legacy 3-col → correct; legacy 2-col → correct. Real-boot legs (Noble first boot in ticket 05; resolute ×3 in e2e §4) show IP acquired from lease, success block printed only after proven SSH, negative leg `poc-3` → rc 1 + `virsh console` hint after 1 m 47 s. Current on-disk lease file still shows the JSON format with stale entries — strict per-MAC match makes them harmless, as documented | Review run (parser fixtures) + `docs/e2e-acceptance.md` §4.1, §4.4, §5.1 |
| **S6** `vm-destroy` (`51828eb`, +93) | usage rc 2; not-defined rc 1 `VM '<NAME>' is not defined`; destroy-if-running → undefine → `vol-delete <NAME>_vda.qcow2`; D7 missing-volume → warn + rc 0; exact confirmation text | Script matches spec §3.2 step-for-step (ordering destroy→undefine→vol-delete; D7 branch greps for "not found"-style errors). **Re-ran:** no arg → rc 2 + usage; `poc-review-never-existed` → rc 1 `VM 'poc-review-never-existed' is not defined`. Shutoff/running/missing-volume legs covered by ticket-06 verification and re-proven in e2e §4.5 (three real VMs, incl. still-running `poc-3`, each → `VM 'poc-N' destroyed and disk removed.` rc 0) | Review run + `docs/e2e-acceptance.md` §4.5 |
| **S7** hardening + E2E evidence (`a7cfe93`) | Hardening only; `docs/e2e-acceptance.md`; plan §8 lease-format update; optional README | All present: only functional change was the SSH window 60 s → 90 s (`verify_ssh_reachable` 30→45 attempts), recorded in e2e §6 with rationale; plan §8 now carries the confirmed JSON lease format; README gained a short "How to run" section; e2e doc contains every command + verbatim output incl. AC1 download proof, AC5 18 ms timing, AC2 stat pair, AC4 autostart evidence, final pool state | `git show --stat a7cfe93` (4 files, +562); `docs/e2e-acceptance.md`; plan §8 |

Git hygiene: one commit per slice in strict S1→S7 order; commit messages
match the ticket-01..07 table exactly (incl. the `(ticket NN)` suffix);
working tree clean at review time.

## 2. Acceptance criteria (spec §7) — verdicts with evidence

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| **AC1** | `vm-create poc-1 --ram 2 --vcpu 1 --disk 10` succeeds unattended from clean state | **PASS** | `docs/e2e-acceptance.md` §4.0–4.1 (same-day, recorded verbatim): clean-state check (only `setup-test-vm`, pool = Noble volume only, resolute cache `absent`) → `time bin/vm-create poc-1 --ram 2 --vcpu 1 --disk 10` → rc 0 in 38.6 s, real 821 MiB download (`-rw-… 860447744 … resolute-server-cloudimg-amd64.img`), SHA256 `9dc7c536…` matching `SHA256SUMS`; `ssh ubuntu@192.168.122.190 'cat /etc/os-release'` → `VERSION_ID="26.04"`, `VERSION_CODENAME=resolute`; `hostname` → `poc-1`. **I re-verified the cache artifact today:** `sha256sum ~/vm-images/resolute/resolute-server-cloudimg-amd64.img` = `9dc7c5363c0146a08ba0c9aa834d82c2c6dfbb1c471ad9a2f0aba1189e21be05`, matching both the recorded hash and the `SHA256SUMS` entry |
| **AC2** | Exactly one ~820 MiB download; second same-release VM reuses cache | **PASS** | e2e §4.3: before/after `stat` pair of the cached image — identical mtime (`2026-08-25 22:06:09.041851682 +0200`), size (860447744), sha256 — across `poc-2` creation, whose output shows no download lines, only `image ready: …` (13 s total). **I re-verified the code path today** in isolation: with the source fixture deleted, `fetch_and_verify_image` returned the cached file with no download (review run, test 2). Cached image confirmed present at `~/vm-images/resolute/` with matching checksum |
| **AC3** | IP printed only after `ssh ubuntu@<ip>` succeeds; else timeout error with `virsh console` hint | **PASS (both legs)** | Positive: code path is `verify_ssh_reachable` (45×2 s, `BatchMode`, `accept-new`) gating the success block — a `die` precedes any success output (code inspection, `bin/vm-create` Step 5); e2e §4.1–4.3 show the success block appearing only after `IP acquired:` in all three positive runs. Negative (recorded): e2e §4.4 `poc-3` with a non-matching key → `vm-create: SSH to ubuntu@192.168.122.103 not reachable yet — check: virsh console poc-3`, rc 1 at 1 m 47 s, **no success block printed** |
| **AC4** | VM survives host reboot (`--autostart`); `vm-destroy` removes VM + disk, no orphan qcow2 in `vm-pool` | **PASS (reboot leg by recorded evidence per PD3)** | Autostart: e2e §2 proves `virt-install --autostart` alone creates the marker (scratch test), §4.5 shows `/etc/libvirt/qemu/autostart/poc-{1,2,3}.xml` symlinks + `virsh list --all --autostart` for all three VMs before teardown. The ticket's literal `virsh dumpxml | grep '<autostart'` command is documented (e2e §5.3, with scratch-domain proof) to **never match on this libvirt 10.0.0 build** — the host-native observables substitute. Teardown: `vm-destroy` ×3 → rc 0, autostart dir empty after. Orphan check: e2e §4.6 — `virsh list --all` = only `setup-test-vm` (shut off); `vm-pool` = exactly `noble-server-cloudimg-amd64.img`. **I re-ran both commands today: identical result.** A real host reboot remains an optional manual check (PD3 — deliberately not performed; would kill the orchestrating session) |
| **AC5** | Same-name re-run fails fast at preflight with clear error | **PASS** | e2e §4.2: immediate re-run of the exact `poc-1` command → `vm-create: VM 'poc-1' already defined`, **rc 1 in 18 ms** (preflight step 2, before any image/VM work). **I re-verified the mechanism today** against the real pre-existing domain: `bin/vm-create setup-test-vm` → rc 1 `vm-create: VM 'setup-test-vm' already defined`, no side effects |

## 3. Gaps & issues

| ID | Severity | Finding | Suggested follow-up |
|---|---|---|---|
| G1 | **minor** | Dead code in `fetch_and_verify_image` (S2): the post-failure handler runs `rm -f "$img"` **before** the `--keep-going` check `[[ -f "$img" ]]`, so the "continue with cached image" sub-branch is unreachable (a failed download is always a fresh file at the final path; a warm cache returns via the earlier cache-hit branch, which *does* deliver the spec'd warning+continue behavior — verified live in review test 6). No functional impact; the branch is just misleading. | Delete the dead branch or move the `-f` test before the `rm -f`; add a comment noting the cache-hit branch is the keep-going path. |
| G2 | **minor** | `v0.1.0` tag not present on the final commit `a7cfe93` (`git tag -l` → empty). Ticket-07 says the documentation agent places it; the implementation side did everything required for it to land. | Tag `v0.1.0` at `a7cfe93` (docs agent / release step). |
| G3 | **nit** | Success block prints `(also reachable as <NAME>.default)` per spec §3.1/D9, but on this host only the **bare** name resolves (`getent hosts poc-1` works, `poc-1.default` does not — e2e §5.2). Spec-verbatim wording kept by decision; users following the `.default` hint will hit a DNS failure. | Follow-up: drop the `.default` clause or make it host-detected; or document the bare-name form in README. |
| G4 | **nit** | `--os-variant ubuntu-lts-latest` resolves to `ubuntu24.04` in the installed osinfo-db (visible as `WARNING … for OS ubuntu24.04` in every run; e2e §5.5). Harmless today (identical virtio/x86_64 defaults, proven by three clean unattended boots of a 26.04 guest) but the warning on every run invites confusion. | When `osinfo-db` gains `ubuntu-26.04`, switch the variant; optionally suppress/log the warning. |
| G5 | **nit** | Non-TTY runs print transient virt-install 4.1.0 noise: `error: Cannot run interactive console without a controlling TTY` / `WARNING Console command returned failure.` / `Running text console command: …` (e2e §4.1) — harmless (VM boots fine, `virsh console` works) but muddies the "exactly this shape" output expectation. | Follow-up: consider `--noautoconsole` or redirecting that phase; not a POC blocker. |
| G6 | **nit** | Meta-data now contains `local-hostname:` in addition to the spec's `hostnames:` block (documented ticket-05 deviation — without it NoCloud kept the image default hostname `ubuntu`, which would have silently broken the DNS-name promise). Deviation is correct and well-documented; the spec itself should be updated to make `local-hostname:` normative. | Amend spec §4 step 3 to include `local-hostname:` (spec is frozen for the POC; do it in the next revision). |
| G7 | **info** | AC4's host-reboot leg is verified by XML/symlink + `--autostart` list evidence, not an actual reboot (plan PD3 — rebooting mid-POC would kill the orchestrating session). Accepted as POC-grade proof; a manual reboot check remains open by design. | Optional manual check: reboot host with a VM running, confirm it comes back. |
| G8 | **info** | Residual MAC-collision risk: `52:54:00:` + 24 random bits; a collision would make the per-MAC lease match return another guest's IP. Low probability, documented (e2e §5.6). | None needed for POC. |

## 4. Review environment & method

**When/where:** 2026-08-25 ~22:35–22:50 CET, on the target host (Ubuntu
24.04, libvirt 10.0.0, virt-install 4.1.0, user `user` in
`libvirt`+`kvm`, no sudo). Project at HEAD `a7cfe93`, working tree clean.

**What I read in full:** spec v1.0, plan v1.0 (incl. updated §8),
tickets 01–07 + ticket README, `docs/e2e-acceptance.md`, both scripts
line-by-line, `README.md`, `git log --oneline` + `git show --stat` for all
7 slice commits.

**What I re-ran myself (all safe/idempotent, all torn down):**
- CLI contract & preflight: 10 invocations of `bin/vm-create`
  (no args; `--ram 2` w/o NAME; `--bogus`; `.bad`; `-bad`; `--ram 0`;
  `setup-test-vm` collision; `--ssh-key /nonexistent`) and 2 of
  `bin/vm-destroy` (no arg; unknown name) — exit codes and messages
  exactly as spec'd (§1 S1/S6 rows). `bash -n` on both scripts: clean.
- State probes before and after all runs: `virsh list --all`,
  `virsh vol-list vm-pool`, `ls /etc/libvirt/qemu/autostart/`,
  `head` of the live lease file — unchanged, matching the PD4 baseline
  and e2e §7.
- Lease parser: extracted `find_ip_for_mac` and ran it against JSON,
  legacy 3-col, and 2-col fixtures (match / case-insensitive / no-match).
- Image pipeline: extracted `fetch_and_verify_image`, ran 6 scenarios
  (download+verify over `file://` with same-dir sums; cache hit with
  source removed; SHA256 mismatch; dead URL cold; `--keep-going` cold;
  `--keep-going` warm) with `VM_CREATE_CACHE_DIR` pointing at `/tmp`
  fixtures.
- Cloud-init: extracted `generate_cloud_init_files`, generated twice
  (default user, `--user tester`) via `VM_CREATE_CLOUD_INIT_DUMP`;
  YAML-parsed and grep-asserted all ticket-03 content criteria;
  confirmed temp-dir cleanup and fresh UUIDs.
- Cache artifact: `sha256sum` of the 821 MiB `~/vm-images/resolute`
  image vs its `SHA256SUMS` entry (match).

**What I relied on recorded (not re-run):** the real E2E legs (AC1
download+boot+SSH, AC3 negative leg, AC4 teardown, AC2 second VM) —
`docs/e2e-acceptance.md` is same-day (22:05), contains verbatim commands
and outputs, real SSH round-trips (`cat /etc/os-release` → 26.04,
`hostname` → `poc-1`), timings, a pre/post state audit, and its final
state **exactly matches the host state I measured today** (only
`setup-test-vm` shut off; pool = exactly the Noble volume; autostart dir
empty; resolute cache present with matching checksum). That is fresh,
convincing evidence, so per the review protocol I did not spend a second
real VM run.

**Left behind:** nothing. All fixtures in `/tmp/virt-review` (and two
accidentally-written copies in `~/vm-images/custom/` and the project root
during my own harness debugging) were removed; `git status` clean; pool
at PD4 baseline; `setup-test-vm` untouched (shut off throughout).
