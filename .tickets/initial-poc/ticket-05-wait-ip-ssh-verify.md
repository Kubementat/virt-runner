# Ticket 05 — S5: Wait-for-IP + access-info output + mandatory SSH verification

**Slice:** S5 (plan §2)
**Goal:** Extend `bin/vm-create` (committed by ticket 04) with spec §4 steps 5–6: dnsmasq lease-file polling for the guest IP (2 s × 60, MAC case-insensitive match), domstate cross-check, **mandatory** SSH verification before any success output (D6/AC3), best-effort fallbacks, and the exact §3.1 success block (D9). Verification has two legs: (1) a `/tmp` fixture that pins the lease-parser contract, and (2) the **real first boot** against the host's pre-existing Noble cloud image (PD1), which confirms the real `virbr0.status` line format and, if it differs from the assumption, adapts the parser *in this slice*.

## Context

- **Spec sections:** §4 step 5 (lease-polling algorithm, SSH probe, timeout errors naming `virsh console`, fallback order) and step 6 (access-info block inputs), §3.1 (exact success output format — authoritative per D9, including the `Name:`/UUID line and field alignment), §5 (lease file path/facts, `<NAME>.default` DNS), §7 AC3, §6 pitfalls 1 & 2 (lease format **unverified** — 0 bytes at research time; `virsh domifaddr` unreliable as primary), decisions **D6** (SSH verification hard-required before success output), **D8** (120 s lease window = 60 polls × 2 s; ~60 s SSH window), **D9** (output format authority).
- **Plan sections:** §1 ground rule 5 (test hook `VM_CREATE_LEASE_FILE` — relocate the lease file for fixture tests; default `/var/lib/libvirt/dnsmasq/virbr0.status`), §2 slice S5 (both verification legs; parser adaptation is an explicit in-scope contingency), §1 **PD1** (S5's first real boot uses the host's existing Noble image via `--image file://` — this exercises the D1 "custom image, no derivable SHA256SUMS → skip verification and warn" path; the real resolute download happens only in ticket 07), **PD2** (`~/.ssh/id_ed25519.pub` does not exist — generate it idempotently before the real boot so the spec-default key path is exercised unmodified), **PD4** (pre-existing Noble volume is the expected steady-state pool contents), §3 (commit message), §4 safety rules, §6 (lease-format risk row: S5 is the designated verification point).
- **Environment facts:**
  - Lease file: `/var/lib/libvirt/dnsmasq/virbr0.status` — exists, mode 644 (world-readable), **0 bytes / no leases currently** → format is still assumed `<ip> <mac> <hostname>` (space-separated, 3 columns); this slice confirms it.
  - Noble image fixture: `/var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img` — 674 MiB, root-owned, **world-readable** (read-only access only; never modify).
  - `~/.ssh/id_ed25519.pub` absent; existing `id_rsa`/`id_forgejo` keys must never be used or modified for the real boot.
  - First-boot cloud-init on this host: expect ~20–40 s; windows are 120 s (lease) + 60 s (SSH) — ample margin.
  - The Noble image (24.04) uses the same `ubuntu` key-auth convention as resolute — same code path.
  - The ~674 MiB `file://`→cache copy lands under `~/vm-images/custom/` (one-off; well within 227 GiB free).

## Deliverables

- **Modify** `bin/vm-create`:
  - `find_ip_for_mac()` / lease-polling per spec §4 step 5: poll `$LEASE_FILE` (overridable via `VM_CREATE_LEASE_FILE`) every 2 s, up to 60 polls (120 s), for a line whose MAC column equals `$MAC` **case-insensitively** (awk `tolower` on both sides); extract the IP (column 1) from the assumed format `<ip> <mac> <hostname>`. If the real format (verified in leg 2) differs, adapt the parser **now** and record the confirmed format.
  - When the lease is found: cross-check `virsh domstate "$NAME"` is `running` (belt-and-braces).
  - Fallbacks (best-effort, in order, only if the lease file is absent/unreadable/empty): `virsh domifaddr "$NAME"` (default ARP source — unreliable until the guest ARP-announces; retry), then `arp -n | grep -i "$MAC"`.
  - **SSH verification (mandatory, D6/AC3):** after the lease, retry up to ~60 s total (every 2 s):
    `ssh -o ConnectTimeout=2 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$USER_NAME@$IP" exit`
    and require exit 0. On timeout: exit 1 with a clear error that includes the console hint, e.g. `SSH to ${USER}@${IP} not reachable yet — check: virsh console $NAME`.
  - Lease timeout: exit 1, e.g. `no DHCP lease after 120s — check: virsh console $NAME`.
  - The exact §3.1 success block (only after lease **and** SSH succeed — never before), substituting the domain UUID (`virsh dominfo $NAME | awk '/UUID/ {print $3}'`), IP, `<NAME>.default` DNS name, `--user`, and the teardown command:
    ```
    VM created and running.
    Name:    <NAME>   (UUID <uuid>)
    IP:      <ip>   (also reachable as <NAME>.default)
    SSH:     ssh <user>@<ip>
    Console: virsh console <NAME>     (Ctrl-] to detach)
    Teardown: vm-destroy <NAME>   (or: virsh destroy <NAME> && virsh undefine <NAME>)
    ```
  - The `--no-boot` path (ticket 04) stays unchanged.

## Safety rules

- Test VMs: `poc-s5` (real boot) only — poc- prefix. **Mandatory cleanup before finishing:**
  ```bash
  virsh destroy poc-s5 2>/dev/null || true
  virsh undefine poc-s5
  virsh vol-delete poc-s5_vda.qcow2 vm-pool
  ```
  then re-assert: `virsh list --all | grep poc-s5` empty; `virsh vol-list vm-pool | grep poc-` empty (pool = exactly the pre-existing Noble volume, PD4).
- `setup-test-vm` must remain `shut off` and untouched; the Noble image file is **read-only** (its cache copy under `~/vm-images/custom/` created by the run may stay — it is a cache artifact, not a VM; note it in the report).
- SSH key: generate the default key **idempotently** per PD2 — `ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519` only if `~/.ssh/id_ed25519` does not exist. Never use or modify `id_rsa`, `id_forgejo`, or any other existing key.
- Fixtures under `/tmp/virt-test-s5/`, removed (`rm -rf /tmp/virt-test-s5`) before finishing.
- No `sudo`; no modification of root-owned files or libvirt config.

## Acceptance criteria

**Leg 1 — fixture (no VM), pins the parser contract before the first real lease exists:**

Setup:
```bash
mkdir -p /tmp/virt-test-s5
printf '192.168.122.77 52:54:00:aa:bb:cc poc-fake\n192.168.122.78 52:54:00:de:ad:be other\n' > /tmp/virt-test-s5/fake-leases
```

1. With `VM_CREATE_LEASE_FILE=/tmp/virt-test-s5/fake-leases`, invoke the parser (via the test hook or an extracted `find_ip_for_mac()` called from a small bash snippet): MAC `52:54:00:aa:bb:cc` → returns `192.168.122.77`; uppercase input `52:54:00:AA:BB:CC` → same result (case-insensitive); MAC `52:54:00:de:ad:be` against a line missing it → no match (empty output, non-fatal).
2. A 2-column lease line (e.g. `192.168.122.99 52:54:00:aa:bb:cc`) is tolerated (parsed or gracefully ignored per your documented fallback path).

**Leg 2 — real first boot (the lease-format verification the spec demands):**

3. Idempotent default key (PD2): `[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519` → `~/.ssh/id_ed25519.pub` exists.
4. Run (the **real VM creation** for this ticket; expect the D1 "no derivable SHA256SUMS — verification skipped" warning for the `file://` Noble image):
   ```bash
   bin/vm-create poc-s5 --ram 2 --vcpu 1 --disk 5 \
     --image file:///var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img
   ```
   → script blocks until SSH actually succeeds (typically 20–40 s first boot), exits **0**, and prints the exact success block (D9 alignment, `Name:`/UUID line present, IP line only after proven SSH).
5. `ssh ubuntu@<printed-IP> 'hostname'` → `poc-s5` (proves meta-data hostname + `manage_etc_hosts`); `ssh ubuntu@poc-s5.default 'hostname'` → `poc-s5` (proves libvirt dnsmasq `<NAME>.default` DNS).
6. **Lease-format confirmation (explicit contingency):** get the domain's MAC (`virsh dumpxml poc-s5 | grep -o 'mac="[0-9a-f:]*"'`) and inspect its real lease line: `grep -i "<that-mac>" /var/lib/libvirt/dnsmasq/virbr0.status` — record the actual column layout and MAC case. If it differs from assumed `<ip> <mac> <hostname>`, adapt the awk parser (columns/`tolower`) **in this ticket** and re-run criterion 4 with a fresh `poc-s5` (tear down the first one first).
7. **Cleanup (mandatory):** run the three cleanup commands from Safety rules; re-assert no `poc-*` domain/volume remains; `virsh list --all` still shows `setup-test-vm` shut off.

## Verification (run in this order)

1. Build the fixture (Leg 1 setup) and run criteria 1–2 (parser contract).
2. Run criterion 3 (idempotent key generation; verify the key file exists and no other key changed: `ls -la ~/.ssh`).
3. Baseline: `virsh list --all && virsh vol-list vm-pool`.
4. Run criterion 4 (the real boot; record start/end times to show the 20–40 s first-boot window) and criteria 5–6.
5. If criterion 6 forced a parser change: apply it, tear down `poc-s5`, and re-run criterion 4 from scratch.
6. Run criterion 7 (mandatory cleanup + state re-assertion); compare to step 3 baseline (the only permitted delta: the cached Noble image under `~/vm-images/custom/`).
7. `rm -rf /tmp/virt-test-s5`.
8. Record the confirmed lease-file line format in the output of this run for the plan §8 note update (ticket 07 writes it into the docs).
9. **Real VM creation IS required for this ticket**: exactly `bin/vm-create poc-s5 --ram 2 --vcpu 1 --disk 5 --image file:///var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img` (criterion 4).
10. If all pass: `git add -A && git commit -m "feat(vm-create): wait for DHCP lease, verify SSH, print access info (ticket 05)"` (include the confirmed lease-file format in the commit message body).

## Definition of done

- Both legs pass: parser contract pinned by fixture; real Noble boot reached the success block only after proven SSH; lease format confirmed (or parser adapted and re-verified); mandatory teardown completed with the pool back to exactly the pre-existing Noble volume; `/tmp/virt-test-s5` removed; `setup-test-vm` untouched.
- Working tree committed with exactly: `git add -A && git commit -m "feat(vm-create): wait for DHCP lease, verify SSH, print access info (ticket 05)"`

## Pitfalls (spec §6 / plan §2, S5-relevant)

- **Pitfall 1 (the slice's headline risk):** lease-file format was unverified (file was 0 bytes at planning time). The fixture pins the contract first; the real boot resolves it; parser adaptation is explicitly in scope *here* — do not defer.
- **Pitfall 2:** `virsh domifaddr` (ARP source) is unreliable until the guest ARP-announces — keep it strictly a fallback behind the lease file, never the primary source.
- **D6/AC3:** the script must **never** print the "VM created and running" block without a proven `ssh … exit 0`; on any timeout, exit 1 with the `virsh console <NAME>` hint.
- **First-boot window:** 20–40 s typical vs the 120 s lease window — plenty, but a hung cloud-init (missing metadata → EC2 wait) would consume it; `console=ttyS0` guarantees a debug path, and the timeout message must name `virsh console`.
- **Noble vs resolute guest version:** 24.04 guest, same `ubuntu` key-auth conventions — any surprise here is guest-version, not code; do not "fix" code based on Noble quirks that won't apply to 26.04 without noting it.
- **`file://` + no sums (D1):** the Noble `file://` image has no derivable same-directory `SHA256SUMS` — the expected behavior is a printed *warning* and skipped verification, not an error (ticket 02 branch).
- **MAC case in the lease file:** dnsmasq may record the MAC upper- or lowercase — the `tolower` match is not optional.
