# Ticket 07 — S7: Hardening + full end-to-end acceptance run

**Slice:** S7 (plan §2) — the **final** slice
**Goal:** Run the complete end-to-end acceptance on the real host with the **real resolute cloud image** (first real download), apply small hardening fixes discovered along the way, and record every command and output as evidence in `docs/e2e-acceptance.md` — including the cache-reuse proof (AC2), name-collision fail-fast (AC5), autostart evidence (AC4, via XML per PD3), teardown with a final orphan-free `vm-pool` proof, and the confirmed lease-file format.

## Context

- **Spec sections:** §7 (all five acceptance criteria — this ticket is where AC1/AC2/AC5 are proven on the real image and AC3/AC4 closed), §3.1 (exact output blocks), §5 (resolute URLs, ~820 MiB), §6 pitfalls 1 & 6 (record the confirmed lease-file format; note the osinfo-db gap / `ubuntu-lts-latest` revisit condition), §8 (out-of-scope reminder: do not add `vm-list`, `--template`, static IPs, UEFI, NAT rules, or `--clean-cache` — no scope creep while hardening).
- **Plan sections:** §2 slice S7 (the 7-step acceptance script below is the plan's, kept in order), §1 **PD3** (AC4's host-reboot leg is proven by `virsh dumpxml` autostart evidence + the no-orphan proof, NOT by rebooting the host — a real reboot would kill the orchestrating session; note it in the doc as an optional manual check) and **PD4** (expected steady-state pool contents = exactly the pre-existing `noble-server-cloudimg-amd64.img`), §3 (commit message; **S7 must be the last slice** so the `v0.1.0` tag lands on the final commit — the documentation agent tags it; you create no tags), §4 safety rules, §5 (acceptance-criteria mapping), §6 (820 MiB download risk row: `curl --retry 3` already in place since ticket 02; `--keep-going` exists; escape hatch below).
- **Environment facts:** `~/vm-images` currently holds only ticket 05's one-off cache (`custom/noble-server-cloudimg-amd64.img`); the resolute image is **not** cached yet, so AC1 performs the first real ~820 MiB download. `bin/vm-create` (tickets 01–05) and `bin/vm-destroy` (ticket 06) are committed and were each verified; this ticket re-verifies them together. `~/.ssh/id_ed25519.pub` exists (generated in ticket 05, PD2) so the spec-default key path is used with no `--ssh-key` flag. 227 GiB free.
- **Escape hatch (plan §6 risk row):** if the resolute URL fails during AC1 (network outage), the boot/SSH path is already proven by the cached Noble image — you may provisionally run AC1's boot/SSH leg with `--image file:///var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img` so the rest of acceptance proceeds, but **record that AC1's resolute leg must be re-run when network permits** and clearly mark it as such in `docs/e2e-acceptance.md`. The ticket is not done until the real resolute leg has succeeded (or the re-run is explicitly the only open item — in which case say so in the final report).

## Deliverables

- **Modify** `bin/vm-create` / `bin/vm-destroy` — hardening only (no behavior changes beyond fixes): message wording vs spec §3/§4, `set -u` edges, fallback order, output alignment vs §3.1/D9. Record each change in the commit message body.
- **Create** `docs/e2e-acceptance.md` — the recorded evidence (below).
- **Modify** `docs/plans/poc-implementation-plan.md` §8 — add the **confirmed lease-file line format** (columns, MAC case) observed in ticket 05/this ticket, replacing the "assumed" note.
- Optionally: short "how to run" section in `README.md` (invocation examples for both scripts). Acceptable, keep it brief.

## Implementation requirements (acceptance script — run in this exact order)

1. **AC1 — clean-state resolute creation (real first download):** from a clean state (no `poc-1` domain, no resolute cache: `ls ~/vm-images/resolute 2>/dev/null || echo absent`), run:
   ```bash
   bin/vm-create poc-1 --ram 2 --vcpu 1 --disk 10
   ```
   → real ~820 MiB download from `https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img`, SHA256-verified (sums fetched from the standard URL), success block printed per §3.1/D9. Then prove it is resolute: `ssh ubuntu@<printed-IP> 'cat /etc/os-release'` shows `26.04` (not Noble/24.04).
2. **AC5 — name-collision fail-fast:** immediately re-run the **exact same command** (`bin/vm-create poc-1 --ram 2 --vcpu 1 --disk 10`) → **exit 1 at preflight** with the `VM 'poc-1' already defined`-style error, **before** any download/VM work (record wall-clock time to show fail-fast — it must return in well under a second, i.e. no download attempted).
3. **AC2 — cache reuse (no second download):** `bin/vm-create poc-2 --ram 1 --vcpu 1 --disk 5` (different sizes also proves size parameters) → completes with **no download**. Evidence (record both): a before/after `stat` pair of `~/vm-images/resolute/resolute-server-cloudimg-amd64.img` (mtime + sha256sum unchanged) **and** no download lines in `poc-2`'s output.
4. **AC3 negative leg (optional if time-safe):** create `poc-3` with a deliberately broken key (a key file whose contents will never be injected, e.g. a key you then refuse to make usable for the guest — simplest: a validly-formatted pubkey for a *different* keypair is NOT broken; instead use a key the guest won't have: the point is the SSH probe must fail) → expect **exit 1** with the `virsh console` hint within ~180 s (lease 120 s + SSH 60 s windows), then tear down `poc-3` with `bin/vm-destroy poc-3`. *If this leg is deemed too costly, the positive leg from steps 1–3 (SSH proven before success output in every run) satisfies AC3's criterion — record the decision in the doc.*
5. **AC4 — autostart evidence + teardown (PD3):** `virsh dumpxml poc-1 | grep -A1 '<autostart'` shows autostart enabled (record output; a real host reboot is noted in the doc as an optional manual check, not performed). Then `bin/vm-destroy poc-1` and `bin/vm-destroy poc-2` (and `poc-3` if created) → each **exit 0** with `VM '<NAME>' destroyed and disk removed.`
6. **Final orphan check:** `virsh list --all` → only `setup-test-vm` (shut off); `virsh vol-list vm-pool` → **exactly** `noble-server-cloudimg-amd64.img` (PD4) — no `poc-*` anywhere. Record the full output of both commands.
7. **Write `docs/e2e-acceptance.md`** containing, in order: every command run in this ticket with its output (trimmed only where clearly redundant), the confirmed lease-file format finding (from ticket 05, restated), the AC2 cache-reuse stat pair, the AC5 timing, the AC4 autostart XML excerpt, the optional-reboot note (PD3), the osinfo-db gap note (pitfall 6: `ubuntu-lts-latest` → 24.04 today; revisit when osinfo-db gains `ubuntu-26.04`), the MAC-collision residual-risk note (plan §6), and the final pool-state listing. Then update `docs/plans/poc-implementation-plan.md` §8 with the confirmed lease format.
8. **Hardening pass:** apply any small fixes found (message wording, `set -u` edges, fallback order, output alignment). No behavior changes, no new features, nothing from §8 out-of-scope list.

## Safety rules

- Test VMs: `poc-1`, `poc-2`, `poc-3` (poc- prefix). **Every VM and volume created in this ticket must be torn down before finishing** — the final pool must contain exactly the pre-existing `noble-server-cloudimg-amd64.img` (PD4).
- `setup-test-vm` must remain `shut off` and untouched — assert at the end.
- The cached images under `~/vm-images/` (resolute + the ticket-05 Noble custom copy) are legitimate cache artifacts and may remain — they are not VMs/volumes.
- No `sudo`; no root-owned file or libvirt config modifications; no host reboot.
- No `poc-*` VM or volume may be carried into any state after this ticket (it is the last one).

## Acceptance criteria

1. `bin/vm-create poc-1 --ram 2 --vcpu 1 --disk 10` succeeded unattended from clean state with the **real resolute image**; `ssh ubuntu@<ip> 'cat /etc/os-release'` → `26.04`. (AC1)
2. `poc-2` creation showed **no download** (stat pair unchanged + no download lines). (AC2)
3. Success output appeared only after proven SSH in every run; the negative leg ran (exit 1 + `virsh console` hint) or the documented decision to rely on the positive leg is recorded. (AC3)
4. `virsh dumpxml poc-1 | grep -A1 '<autostart'` evidence recorded; `vm-destroy` removed VM + disk for each; final `vm-pool` contains exactly `noble-server-cloudimg-amd64.img`. (AC4)
5. Immediate re-run of the `poc-1` command failed at preflight, exit 1, in under ~1 s, before any download/VM work. (AC5)
6. `docs/e2e-acceptance.md` exists with all required evidence; plan §8 lease-format note updated; final `virsh list --all` shows only `setup-test-vm` (shut off).
7. Working tree contains only: `bin/vm-create`, `bin/vm-destroy` (hardening diffs), `docs/e2e-acceptance.md`, `docs/plans/poc-implementation-plan.md`, and optionally `README.md`.

## Verification (run in this order)

1. Clean-state check: `virsh list --all && virsh vol-list vm-pool && ls ~/vm-images/resolute 2>/dev/null || echo absent` (expect: only `setup-test-vm`; only Noble volume; resolute cache absent).
2. Run the acceptance script steps 1 → 7 in order, capturing each command's output as you go (you are building the doc as you run).
3. **Real VM creation IS required** — three times:
   - `bin/vm-create poc-1 --ram 2 --vcpu 1 --disk 10` (step 1, real resolute download; the exact invocation the task/spec demands)
   - `bin/vm-create poc-2 --ram 1 --vcpu 1 --disk 5` (step 3)
   - optionally `bin/vm-create poc-3 …` with the broken key (step 4)
4. Run step 8 (hardening) — after any script change, re-verify at least one quick path (e.g. `bin/vm-create` usage → exit 2; `bin/vm-destroy poc-never-existed` → exit 1).
5. Final state: `virsh list --all && virsh vol-list vm-pool` matches the baseline (only `setup-test-vm` / only the Noble volume).
6. `git status --short` shows only the allowed files; no `/tmp` artifacts in the repo.
7. If all pass: `git add -A && git commit -m "test(e2e): record full acceptance evidence and apply hardening fixes (ticket 07)"` — this must be the **final** implementation commit (the `v0.1.0` tag is placed on it by the documentation agent; create no tags yourself).

## Definition of done

- All five spec acceptance criteria are evidenced in `docs/e2e-acceptance.md` (with the escape hatch explicitly noted if used); verification sequence ran successfully; complete teardown proven (no `poc-*` domain or volume, pool = exactly the pre-existing Noble volume, `setup-test-vm` untouched).
- Working tree committed with exactly: `git add -A && git commit -m "test(e2e): record full acceptance evidence and apply hardening fixes (ticket 07)"`

## Pitfalls (spec §6 / plan §2, S7-relevant)

- **820 MiB download risk (plan §6):** outage mid-AC1 — `curl --retry 3` (ticket 02) + `--keep-going` + the cached-Noble escape hatch keep the acceptance moving; the resolute leg must still be evidenced (or explicitly outstanding).
- **Lease format (pitfall 1):** confirmed in ticket 05 — record the *confirmed* format in the doc and plan §8; if ticket 05 had to adapt the parser, double-check the adaptation still works with the real resolute boot.
- **osinfo gap (pitfall 6):** `--os-variant ubuntu-lts-latest` resolves to 24.04 today; identical virtio/x86_64 defaults for 26.04; note the revisit condition in the doc.
- **MAC collision (plan §6):** low-probability residual risk (24-bit random space) — note it in the doc; a collision would surface as a wrong guest answering the lease.
- **First-boot variance:** 20–40 s typical; windows (120 s lease / 60 s SSH) have margin; a hung cloud-init shows up as a lease timeout naming `virsh console`.
- **Scope creep (spec §8):** hardening only — no `vm-list`, `--template`, static IPs, UEFI, NAT rules, `--clean-cache`, or `~/bin` symlinks in this ticket.
- **Tag discipline:** no git tags in this slice; S7 is the last slice so `v0.1.0` lands on the final commit.
