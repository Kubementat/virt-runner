# Ticket 04 — S4: VM creation via `virt-install --import`

**Slice:** S4 (plan §2)
**Goal:** Extend `bin/vm-create` (committed by ticket 03) to actually create the VM: generate the fixed MAC, run the full `virt-install --import` command from spec §4 step 4 (all flags and rationale), and implement D5 `--no-boot` stop-after-create with the not-booted output variant. Verification uses a **real libvirt domain** built from a tiny blank image with `--no-boot` — no 820 MiB download, no real guest boot. This slice *proves* the `<NAME>_vda.qcow2` volume-naming assumption that `vm-destroy` (ticket 06) depends on.

## Context

- **Spec sections:** §4 step 4 (the exact `virt-install` invocation, the flag-rationale table, the MAC scheme, `--no-boot` handling), §5 (fixed MAC scheme; disk volume naming `<NAME>_vda.qcow2`; virt-install 4.1.0 `--cloud-init` file sub-options only), §3.1 (the `--no-boot` output variant: `VM created (not booted): <NAME>` plus Console/Teardown lines, no IP/SSH lines; exit codes), §6 pitfalls 3 & 4 (`disable=on` mandatory; **never bare `--cloud-init`**), pitfall 6 (`--os-variant ubuntu-lts-latest`), decisions **D5** (stop-after-create via guarded `virsh destroy`) and **D9** (output block is authoritative, including field alignment).
- **Plan sections:** §2 slice S4 (scope, verification with `poc-s4` + mandatory raw-virsh cleanup), §3 (commit message), §4 safety rules (raw `virsh destroy/undefine/vol-delete` is the sanctioned cleanup because `vm-destroy` does not exist yet), §6 (risk row: blank-image import behavior; `--no-boot` race).
- **Environment facts:** `virt-install` 4.1.0 (file-based `--cloud-init` sub-options only); `vm-pool` active (autostart); `default` network active; the pre-existing volume `noble-server-cloudimg-amd64.img` is the expected pool steady state (PD4) — every "no orphan" check must account for it.
- **MAC scheme (verbatim):** `52:54:00:<3 random hex bytes>`, generated once per run:
  ```bash
  MAC="52:54:00:$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  ```

## Deliverables

- **Modify** `bin/vm-create`:
  - MAC generation exactly per the scheme above (known *before* boot — ticket 05's lease matching relies on it).
  - The full `virt-install` invocation (spec §4 step 4, verbatim shape — substitute your variables):
    ```bash
    virt-install \
      --name "$NAME" \
      --ram "$RAM_KIB" --vcpu "$VCPUS" \
      --disk "size=${DISK_GIB},bus=virtio,pool=vm-pool,format=qcow2" \
      --network "network=default,model=virtio,mac=${MAC}" \
      --os-variant ubuntu-lts-latest \
      --location "$IMAGE_PATH" --import \
      --extra-args "console=ttyS0" \
      --console none \
      --cloud-init "user-data=${TMP}/user-data,meta-data=${TMP}/meta-data,clouduser-ssh-key=${SSHKEY},disable=on" \
      --autostart
    ```
    Honor the rationale table: GiB→KiB RAM conversion (or virt-install suffixes), `--os-variant ubuntu-lts-latest` (osinfo gap workaround), `--import` + `--location` (no installer), `--console none` + `console=ttyS0` (unattended but debuggable), the **full** `--cloud-init` sub-option string with `disable=on` (bare `--cloud-init` is forbidden), `--autostart`.
  - **D5 `--no-boot`:** after `virt-install` returns, if `virsh domstate "$NAME"` is `running`, run `virsh --quiet destroy "$NAME"` (guarded by the domstate check — virt-install auto-starts, so handle the already-stopped case under `set -e`); skip the IP/SSH steps (not implemented yet anyway) and print the not-booted output variant:
    ```
    VM created (not booted): <NAME>
    Console: virsh console <NAME>     (Ctrl-] to detach)
    Teardown: vm-destroy <NAME>   (or: virsh destroy <NAME> && virsh undefine <NAME>)
    ```
    with exit 0.
  - For the normal (boot) path, keep the ticket-03 stub *after* creation (wait-for-IP/SSH/output block arrive in ticket 05) — but note this means a normal-path run in this ticket leaves a running VM; verification therefore uses `--no-boot` exclusively, and the boot-path stub must clearly tell the operator the VM exists and how to tear it down (print the same Console/Teardown lines before exiting 1) so a test VM is never orphaned.
- **Create** a tiny test image for verification: `/tmp/virt-test-s4/tiny.img` (a few MiB of zeros, e.g. `dd if=/dev/zero of=/tmp/virt-test-s4/tiny.img bs=1M count=8`).

## Implementation requirements

1. `--cloud-init` always uses the four sub-options `user-data=,meta-data=,clouduser-ssh-key=,disable=on`; the code path with bare `--cloud-init` must not exist.
2. The disk argument is exactly `size=${DISK_GIB},bus=virtio,pool=vm-pool,format=qcow2` (full qcow2 in `vm-pool`; virt-install names it `<NAME>_vda.qcow2` — this slice proves that name).
3. `--no-boot` must be race-safe: guard the destroy with a `domstate` check so an already-shut-off domain does not abort the script under `set -e`.
4. Exit 0 with the exact not-booted output variant (field layout matching spec §3.1 style) when `--no-boot` succeeds; exit 1 on virt-install failure with the tool's error visible.
5. The assigned MAC in the domain XML must equal the generated MAC (asserted in verification).
6. `<autostart/>` must be present in the domain XML (acceptance criterion 4 evidence begins here).

## Safety rules

- Test VM: `poc-s4` only (poc- prefix). **Mandatory cleanup before finishing** (sanctioned raw virsh, since `vm-destroy` does not exist yet):
  ```bash
  virsh destroy poc-s4 2>/dev/null || true
  virsh undefine poc-s4
  virsh vol-delete poc-s4_vda.qcow2 vm-pool
  ```
  then re-assert the pool state.
- Steady-state pool contents after cleanup must be **exactly** the pre-existing `noble-server-cloudimg-amd64.img` (PD4) — no `poc-*` volume anywhere.
- `setup-test-vm` must remain untouched (still `shut off`).
- Do not run the default (resolute) image path — fixture image only (no 820 MiB download; ticket 07 does that).
- All fixtures under `/tmp/virt-test-s4/`, removed (`rm -rf /tmp/virt-test-s4`) before finishing.
- No `sudo`; no modification of root-owned files or libvirt pool/network config.

## Acceptance criteria

The creation command (use it exactly, adjusting only the key path):

```bash
bin/vm-create poc-s4 --no-boot --ram 1 --vcpu 1 --disk 2 \
  --image file:///tmp/virt-test-s4/tiny.img \
  --ssh-key ~/.ssh/id_rsa.pub
```

1. Command exits **0** and prints the not-booted variant (`VM created (not booted): poc-s4` + Console/Teardown lines; **no** IP/SSH lines).
2. `virsh list --all | grep poc-s4` → domain listed, state **shut off** (D5 stop worked).
3. `virsh dominfo poc-s4` → UUID present; `virsh dumpxml poc-s4` contains: `<autostart/>`; a MAC of the form `52:54:00:xxxxxx` (12 hex chars after the OUI) matching the generated one; `<interface type='network'>` with `network name='default'` (or equivalent dumpxml attribute layout); disk `<source pool='vm-pool' volume='poc-s4_vda.qcow2'/>` (or equivalent).
4. `virsh vol-list vm-pool` → `poc-s4_vda.qcow2` present (**record this exact volume name as evidence for ticket 06**).
5. `virsh domstate poc-s4` → `shut off`.
6. **Cleanup (mandatory):** run the three raw-virsh cleanup commands above, then `virsh list --all | grep poc-s4` → empty, and `virsh vol-list vm-pool | grep poc-` → empty (pool back to exactly the Noble volume).

## Verification (run in this order)

1. `mkdir -p /tmp/virt-test-s4 && dd if=/dev/zero of=/tmp/virt-test-s4/tiny.img bs=1M count=8`
2. Baseline: `virsh list --all && virsh vol-list vm-pool` (expect only `setup-test-vm` / only the Noble volume).
3. Run the creation command; record full output and exit code.
4. Run criteria 2–5 checks in order; capture the `virsh dumpxml` and `vol-list` outputs as evidence.
5. Run criterion 6 (mandatory cleanup + re-assertion).
6. Final state: baseline identical to step 2; `rm -rf /tmp/virt-test-s4`.
7. If virt-install **rejects** the blank image (recorded risk): fall back to carving a few MiB from a real cloud image header (e.g. `dd if=/var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img of=/tmp/virt-test-s4/tiny.img bs=1M count=8` — read-only access to the Noble file is allowed) or a small real image, and **record the actual behavior in the commit message** (amend before committing).
8. **Real VM creation IS required for this ticket** (a stopped `poc-s4` domain + volume), exactly per the command in "Acceptance criteria".
9. If all pass: `git add -A && git commit -m "feat(vm-create): create VM via virt-install --import with fixed MAC (ticket 04)"`

## Definition of done

- All acceptance criteria pass including the mandatory teardown; pool restored to the single pre-existing Noble volume; `/tmp/virt-test-s4` removed; `setup-test-vm` untouched.
- Working tree committed with exactly: `git add -A && git commit -m "feat(vm-create): create VM via virt-install --import with fixed MAC (ticket 04)"`

## Pitfalls (spec §6 / plan §2, S4-relevant)

- **Pitfall 3 / D5:** `disable=on` must be in the `--cloud-init` string, or cloud-init may re-apply/reset auth on later boots.
- **Pitfall 4:** never bare `--cloud-init` — it triggers `root-password-generate=on` (10 s pause + printed root password), fatal for unattended use.
- **Pitfall 6:** `--os-variant ubuntu-lts-latest` resolves to 24.04 in the installed osinfo-db (no `ubuntu-26.04` entry) — harmless (identical virtio/x86_64 defaults), revisit only if osinfo-db is updated.
- **Pitfall 7 / volume naming:** the disk lands as `<NAME>_vda.qcow2` in `vm-pool`; ticket 06's `vm-destroy` deletes exactly that name — this slice's criterion 4 is the de-risking proof.
- **`--no-boot` race:** virt-install auto-starts the domain; the guarded destroy must tolerate an already-stopped VM under `set -e` (plan S4 key risk).
- **Blank-image import:** `--import` of a zeros file is a byte copy and should work while the VM is stopped immediately; if virt-install rejects it, use the recorded fallback and note it in the commit message.
- **Cloud-init ISO generation on 4.1.0:** only file-based sub-options exist (no inline `user=`/`hostname=`/`ssh-key=`); a syntax slip here fails at creation time.
