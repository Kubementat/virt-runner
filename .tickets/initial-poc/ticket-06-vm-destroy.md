# Ticket 06 — S6: `vm-destroy` companion

**Slice:** S6 (plan §2)
**Goal:** Create `bin/vm-destroy`: the spec §3.2 companion teardown tool — preflight (defined-domain check), destroy-if-running → undefine → `virsh vol-delete <NAME>_vda.qcow2 vm-pool` with missing-volume tolerance (D7), exact confirmation message, and the 0/1/2 exit-code contract. Verification uses cheap real `poc-s6` VMs (shutoff, running, and missing-volume paths) and ends with proof that no orphans remain and pre-existing objects are untouched.

## Context

- **Spec sections:** §3.2 (the four-step behavior, in order: defined-domain preflight exit 1 `VM '<NAME>' is not defined` / no-arg usage exit 2; destroy-if-running → undefine; volume delete with missing-volume tolerance; exact confirmation `VM '<NAME>' destroyed and disk removed.` or an already-absent note; **no confirmation prompt** — the tool is scriptable), §6 pitfall 7 (volume naming `<NAME>_vda.qcow2` — deleting exactly that volume is what makes AC4's "no orphan qcow2" true), decisions **D7** (missing volume → warning + exit 0, idempotent teardown; only "domain not defined" is a hard error), §7 AC4.
- **Plan sections:** §2 slice S6 (scope + the four verification paths incl. never-touch proof), §1 **PD4** (expected steady-state pool contents after teardown = exactly the pre-existing `noble-server-cloudimg-amd64.img` — every "no orphan" check accounts for it), §3 (commit message), §4 safety rules.
- **Environment facts:** `vm-destroy` relies on the volume-naming proof from ticket 04 (`<NAME>_vda.qcow2` in `vm-pool`). The cached Noble image from ticket 05 already exists under `~/vm-images/custom/` (`custom/noble-server-cloudimg-amd64.img`), so the running-VM test reuses it via `--image file://` **without re-copying** (cache hit). `bin/vm-create` is fully functional (tickets 01–05 committed).
- Ordering note: `vol-delete` must come **after** destroy + undefine — deleting a volume still backed by a running/attached disk fails.

## Deliverables

- **Create** `bin/vm-destroy` — executable, `#!/usr/bin/env bash`, `set -euo pipefail`, implementing spec §3.2 exactly:
  1. No argument → usage on stderr, **exit 2**.
  2. `virsh dominfo <NAME>` fails → `VM '<NAME>' is not defined` on stderr, **exit 1** (hard error).
  3. If `virsh domstate <NAME>` is `running` → `virsh destroy <NAME>` (guard so a shutoff domain skips this safely under `set -e`); then `virsh undefine <NAME>`.
  4. `virsh vol-delete <NAME>_vda.qcow2 vm-pool` — if the volume is **absent**, print a warning/note ("volume already absent") but **exit 0** (D7: the VM definition is gone; a missing volume is already cleaned up, never a failure).
  5. Print confirmation: `VM '<NAME>' destroyed and disk removed.` (or the already-absent-volume variant of the message).
  - No prompts, no sudo.

## Implementation requirements

1. The volume name is constructed as `<NAME>_vda.qcow2` and the pool is exactly `vm-pool` (pitfall 7 — do not glob or guess other names).
2. Missing-volume detection: capture `virsh vol-delete`'s non-zero exit + stderr; treat "volume not found"-style failure as the D7 tolerance branch (warning + exit 0), any other failure as exit 1.
3. Destroy-before-undefine-before-vol-delete ordering is mandatory.
4. Exit codes: 0 success (including the D7 already-absent case), 1 runtime failure, 2 usage.
5. Messages match spec §3.2 wording as closely as possible; the primary confirmation is exactly `VM '<NAME>' destroyed and disk removed.`
6. Do not modify `bin/vm-create` in this ticket (hardening belongs to ticket 07).

## Safety rules

- Test VMs: `poc-s6` only (poc- prefix). Every VM/volume this ticket creates **must** be torn down before finishing (use `bin/vm-destroy` itself where the path allows, raw virsh otherwise).
- Final pool state must be **exactly** the pre-existing `noble-server-cloudimg-amd64.img` (PD4) — no `poc-*` volume.
- `setup-test-vm` must remain `shut off` and untouched — assert it at the end.
- The Noble image file under `/var/lib/libvirt/` is read-only input (via `--image file://` + the existing cache); never modify it.
- Any `/tmp` fixtures removed before finishing. No `sudo`; no root-owned file modifications.

## Acceptance criteria

Setup for the disposable VMs (tiny image, same recipe as ticket 04):
```bash
mkdir -p /tmp/virt-test-s6 && dd if=/dev/zero of=/tmp/virt-test-s6/tiny.img bs=1M count=8
```

1. **Usage:** `bin/vm-destroy` (no args) → **exit 2** + usage on stderr.
2. **Not-defined:** `bin/vm-destroy poc-never-existed` → **exit 1**, `VM 'poc-never-existed' is not defined`.
3. **Shutoff VM path:**
   - Create: `bin/vm-create poc-s6 --no-boot --ram 1 --vcpu 1 --disk 2 --image file:///tmp/virt-test-s6/tiny.img --ssh-key ~/.ssh/id_rsa.pub` → exit 0, domain shut off.
   - `bin/vm-destroy poc-s6` → **exit 0**, prints `VM 'poc-s6' destroyed and disk removed.`
   - `virsh list --all | grep poc-s6` → empty; `virsh vol-list vm-pool | grep poc-s6` → empty (no orphan).
4. **Running-VM path:** recreate `poc-s6` **booted** using the cached Noble image (no re-copy — cache hit): `bin/vm-create poc-s6 --ram 2 --vcpu 1 --disk 5 --image file:///var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img` (let it complete through SSH, as in ticket 05; or start it and wait for `domstate running` — record which you did); `virsh domstate poc-s6` → `running`; run `bin/vm-destroy poc-s6` **while running** → exit 0, same confirmation; re-assert domain gone and no `poc-s6` volume.
5. **Hard-error after full teardown:** `bin/vm-destroy poc-s6` again → **exit 1**, "not defined" (per spec: re-running on a gone domain is a hard error; D7 tolerance applies to the *volume*, not the domain).
6. **Missing-volume branch (D7):** the branch is reachable with a *defined* domain whose volume was already deleted. Do, in order: (a) `bin/vm-create poc-s6b --no-boot --ram 1 --vcpu 1 --disk 2 --image file:///tmp/virt-test-s6/tiny.img --ssh-key ~/.ssh/id_rsa.pub` (domain `poc-s6b` now defined, shut off); (b) manually `virsh vol-delete poc-s6b_vda.qcow2 vm-pool` (pre-delete the volume while the domain is still defined); (c) run `bin/vm-destroy poc-s6b` → **exit 0** with the "volume already absent" note (domain destroyed+undefined, missing volume tolerated per D7); (d) assert `virsh list --all | grep poc-s6b` → empty. (If the branch proves unreachable on this host for any reason, document the exact reachable path and the check you used instead.)
7. **Never-touch proof (final state):** `virsh list --all` → only `setup-test-vm`, state `shut off`; `virsh vol-list vm-pool` → exactly one volume, `noble-server-cloudimg-amd64.img` (PD4).
8. `rm -rf /tmp/virt-test-s6` done.

## Verification (run in this order)

1. Create the fixture (setup block).
2. Baseline: `virsh list --all && virsh vol-list vm-pool`.
3. Run criteria 1 → 2 (pure CLI checks, no VMs).
4. Run criterion 3 (create shutoff `poc-s6` → destroy → assert gone).
5. Run criterion 4 (booted `poc-s6` → destroy-while-running → assert gone).
6. Run criteria 5 → 6 (hard error + D7 branch).
7. Run criterion 7 (final never-touch proof); compare with step 2 baseline.
8. Teardown of fixtures (criterion 8); `git status --short` shows only the new `bin/vm-destroy`.
9. **Real VM creation IS required for this ticket** (`poc-s6` shutoff and booted; optional `poc-s6b` for the D7 branch), per the exact commands above.
10. If all pass: `git add -A && git commit -m "feat(vm-destroy): add companion teardown script (ticket 06)"`

## Definition of done

- All acceptance criteria pass; all `poc-*` domains/volumes torn down; pool back to exactly the pre-existing Noble volume; `setup-test-vm` untouched; `/tmp/virt-test-s6` removed.
- Working tree committed with exactly: `git add -A && git commit -m "feat(vm-destroy): add companion teardown script (ticket 06)"`

## Pitfalls (spec §6 / plan §2, S6-relevant)

- **Pitfall 7 / AC4:** the disk is `<NAME>_vda.qcow2` in `vm-pool`; deleting *exactly* that volume (and nothing else) is what makes "no orphan qcow2" true — proven by ticket 04, asserted here.
- **Ordering:** `vol-delete` on a volume still backed by a running/attached disk fails — destroy first, undefine, then delete (plan S6 key risk).
- **D7 vs hard error:** distinguish the two failure classes precisely — *domain not defined* → exit 1; *volume already absent* (domain was defined and is now gone) → warning + exit 0. Getting this backwards breaks scriptable idempotent teardown.
- **Mid-script death risk:** if the script dies between undefine and vol-delete an orphan results — `set -e` plus this ticket's tests plus ticket 07's final pool assertion are the mitigation (plan S6 key risk).
- **PD4 in every assertion:** "no orphans" means the pool equals exactly `{noble-server-cloudimg-amd64.img}` — a `grep poc-` check alone is necessary but write the full expected listing in the evidence.
- **Running-VM test cost:** use the cached Noble image (ticket 05's one-off copy) — do not download anything new.
