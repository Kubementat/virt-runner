# E2E Acceptance Evidence (ticket 07 / slice S7)

Full end-to-end acceptance of `bin/vm-create` + `bin/vm-destroy` on the real
host, with the **real Resolute cloud image** (first real ~820 MiB download).
Every command below was run for real on this host (libvirt 10.0.0,
virt-install 4.1.0, Ubuntu 24.04 user `verfeinerer`, no sudo), on
2026-08-25. Output is verbatim except where noted ("trimmed").

Acceptance-criteria status (spec §7):

| AC | Status | Where |
|---|---|---|
| 1 — clean-state `poc-1` from the real resolute image, `26.04` over SSH | **PASS** | §4.1 |
| 2 — second VM reuses cache (no re-download) | **PASS** | §4.3 |
| 3 — success only after proven SSH; negative leg exit 1 + `virsh console` hint | **PASS (both legs)** | §4.1–4.3 (positive), §4.4 (negative) |
| 4 — autostart evidence + `vm-destroy` removes VM **and** disk, pool orphan-free | **PASS** | §4.5, §4.6, §5.3 |
| 5 — same-name re-run fails fast at preflight | **PASS** | §4.2 |

The **escape hatch was NOT needed** — the resolute download succeeded on
the first attempt (§4.1).

---

## 1. Baseline (verification step 1: clean-state check)

```console
$ virsh list --all && virsh vol-list vm-pool && ls ~/vm-images/resolute 2>/dev/null || echo absent
 Id   Name            State
--------------------------------
 -    setup-test-vm   shut off

 Name                              Path
------------------------------------------------------------------------------------------------
 noble-server-cloudimg-amd64.img   /var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img

absent
```

(Re-asserted immediately before the definitive run, §4.0 — same result.)

## 2. Preliminary pass (script hardening discovery, trimmed)

Before the definitive pass, an exploratory run of the same acceptance
sequence was executed. It succeeded end-to-end (real resolute download,
SSH-proven `poc-1`, fail-fast on collision, cache reuse for `poc-2`,
negative leg for `poc-3`, clean teardown), but the AC4 evidence command
from the ticket, `virsh dumpxml poc-1 | grep -A1 '<autostart'`, came back
**empty**. Investigation:

1. `virsh autostart poc-1` reports `Domain 'poc-1' marked as autostarted`
   (rc 0) yet `virsh dumpxml poc-1 | grep -c autostart` → `0`.
2. Scratch test with a minimal defined domain (`poc-autotest`): same
   behavior — `virsh autostart --disable` also leaves no `<autostart>`
   element in `virsh dumpxml --inactive` output.
3. This libvirt 10.0.0 build uses the `/etc/libvirt/qemu/` domain-XML
   layout and stores the autostart flag as a **symlink in
   `/etc/libvirt/qemu/autostart/`**; `virsh list --all --autostart`
   correctly reflects the flag (the scratch domain appeared, and
   disappeared after `--disable`):

   ```console
   $ virsh list --all --autostart
    Id   Name            State
   -------------------------------
    -    poc-autotest   shut off
   ```
4. Scratch test that `virt-install … --autostart` **alone** (the committed
   flow, no extra commands) creates the autostart marker —
   `/etc/libvirt/qemu/autostart/poc-autotest2.xml` appeared right after
   `Domain creation completed.` So the committed script was already
   correct; the ticket's `dumpxml | grep '<autostart'` evidence command
   simply never matches on this libvirt build. **No script change was
   needed for autostart** (an explicitly-added `virsh autostart` was
   tried and then reverted as redundant). The AC4 evidence below uses the
   host-native observables: `virsh list --all --autostart` + the autostart
   symlink. All scratch VMs/volumes (`poc-as-check`, `poc-autotest`,
   `poc-autotest2`) were torn down before the definitive pass; the
   pre-existing `setup-test-vm` and the Noble volume were never touched.

One hardening change **was** kept from this phase: the SSH verification
window (see §6). After that change, the state was reset (resolute cache
removed, test-IP `known_hosts` entries purged with `ssh-keygen -R`, all
`poc-*` gone — see §3) and the whole acceptance sequence re-run
definitively in §4.

## 3. State reset before the definitive pass

```console
$ bin/vm-destroy poc-1
VM 'poc-1' destroyed and disk removed.
$ rm -rf ~/vm-images/resolute
$ ssh-keygen -R 192.168.122.177   # stale host key of an earlier test VM IP
                                    # (known_hosts IP-reuse hazard; test IPs only)
# host keys for 192.168.122.53 / .144 / .207 (earlier-leg test IPs) purged the same way
$ ls /etc/libvirt/qemu/autostart/   # empty
$ virsh list --all                  # only setup-test-vm (shut off)
$ virsh vol-list vm-pool            # only noble-server-cloudimg-amd64.img
```

## 4. Definitive acceptance run (steps 1–6, in order)

### 4.0 Pre-run clean-state check

```console
$ virsh list --all && virsh vol-list vm-pool && ls ~/vm-images/resolute 2>/dev/null || echo absent
 Id   Name            State
--------------------------------
 -    setup-test-vm   shut off

 Name                              Path
------------------------------------------------------------------------------------------------
 noble-server-cloudimg-amd64.img   /var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img

absent
```

### 4.1 STEP 1 (AC1 + positive AC3) — clean-state resolute creation

```console
$ time bin/vm-create poc-1 --ram 2 --vcpu 1 --disk 10
image ready: /home/verfeinerer/vm-images/resolute/resolute-server-cloudimg-amd64.img
cloud-init files generated: /tmp/tmp.ThdRQqNWdI (meta-data, user-data; id=4f2eaba1-4494-4954-a9a1-3464efd600b5)
WARNING  Requested memory 2048 MiB is less than the recommended 3072 MiB for OS ubuntu24.04

Starting install...
Allocating 'virtinst-i12umesa-cloudinit.iso'                |    0 B  00:00 ...
Transferring 'virtinst-i12umesa-cloudinit.iso'              |    0 B  00:00 ...
Creating domain...                                          |    0 B  00:00
error: Cannot run interactive console without a controlling TTY

WARNING  Console command returned failure.
Running text console command: virsh --connect qemu:///system console poc-1
Domain creation completed.
VM 'poc-1' created (assigned MAC: 52:54:00:be:b2:b1)
IP acquired: 192.168.122.190
VM created and running.
Name:    poc-1   (UUID 3961e1ee-0a48-4ecf-9d91-1a7a8f63a51f)
IP:      192.168.122.190   (also reachable as poc-1.default)
SSH:     ssh ubuntu@192.168.122.190
Console: virsh console poc-1     (Ctrl-] to detach)
Teardown: vm-destroy poc-1   (or: virsh destroy poc-1 && virsh undefine poc-1)

real	0m38.652s
# exit=0
```

(The two `…console…` lines are virt-install 4.1.0's harmless reaction to
`--console none` under a non-TTY; the VM boots fine and is debuggable via
`virsh console` as printed. The `WARNING` is virt-install's own, noting
the osinfo-recommended memory for `ubuntu24.04` — see §5.5.)

The ~820 MiB download happened between the start (22:05:43) and
`image ready` — from a clean cache (verified absent in §4.0). Real
download proof + SHA256 verification:

```console
$ ls -la ~/vm-images/resolute/
total 840308
-rw-rw-r-- 1 verfeinerer verfeinerer 860447744 Aug 25 22:06 resolute-server-cloudimg-amd64.img
-rw-rw-r-- 1 verfeinerer verfeinerer      8492 Aug 25 22:06 SHA256SUMS
$ du -h ~/vm-images/resolute/resolute-server-cloudimg-amd64.img
821M  /home/verfeinerer/vm-images/resolute/resolute-server-cloudimg-amd64.img
$ sha256sum ~/vm-images/resolute/resolute-server-cloudimg-amd64.img
9dc7c5363c0146a08ba0c9aa834d82c2c6dfbb1c471ad9a2f0aba1189e21be05  .../resolute-server-cloudimg-amd64.img
$ grep resolute-server-cloudimg-amd64.img ~/vm-images/resolute/SHA256SUMS
9dc7c5363c0146a08ba0c9aa834d82c2c6dfbb1c471ad9a2f0aba1189e21be05 *resolute-server-cloudimg-amd64.img
```

→ **It is resolute, not Noble:**

```console
$ ssh ubuntu@192.168.122.190 'cat /etc/os-release'
PRETTY_NAME="Ubuntu 26.04 LTS"
NAME="Ubuntu"
VERSION_ID="26.04"
VERSION="26.04 LTS (Resolute Raccoon)"
VERSION_CODENAME=resolute
$ ssh ubuntu@192.168.122.190 'hostname'
poc-1
```

The success block appeared only **after** the DHCP lease matched the
fixed MAC and a real `ssh ubuntu@192.168.122.190 exit` succeeded
(positive AC3 leg). Guest-side lease check (§5.1):

```console
$ jq -c '.[] | select(.hostname == "poc-1")' /var/lib/libvirt/dnsmasq/virbr0.status
{"ip-address":"192.168.122.190","mac-address":"52:54:00:be:b2:b1","hostname":"poc-1","client-id":"ff:56:50:4d:98:00:02:00:00:ab:11:86:b7:eb:be:da:f5:e4:30","expiry-time":1787691980}
```

### 4.2 STEP 2 (AC5) — name-collision fail-fast

Exact same command, immediately re-run:

```console
$ time bin/vm-create poc-1 --ram 2 --vcpu 1 --disk 10
vm-create: VM 'poc-1' already defined

real	0m0.018s
# exit=1
```

18 ms — preflight step 2 (`virsh dominfo`), well before any image/VM
work; the cache dir was untouched. **AC5 PASS.**

### 4.3 STEP 3 (AC2) — cache reuse, no second download

Different sizes (`--ram 1 --vcpu 1 --disk 5`) also prove the size
parameters:

```console
$ stat -c '%y %s %n' ~/vm-images/resolute/resolute-server-cloudimg-amd64.img   # BEFORE
2026-08-25 22:06:09.041851682 +0200 860447744 /home/verfeinerer/vm-images/resolute/resolute-server-cloudimg-amd64.img
$ sha256sum ~/vm-images/resolute/resolute-server-cloudimg-amd64.img           # BEFORE
9dc7c5363c0146a08ba0c9aa834d82c2c6dfbb1c471ad9a2f0aba1189e21be05  .../resolute-server-cloudimg-amd64.img

$ bin/vm-create poc-2 --ram 1 --vcpu 1 --disk 5
image ready: /home/verfeinerer/vm-images/resolute/resolute-server-cloudimg-amd64.img
cloud-init files generated: /tmp/tmp.BaK6JSoMAV (meta-data, user-data; id=3dc8e796-de54-43c3-a9ee-4969b13d570b)
WARNING  Requested memory 1024 MiB is less than the recommended 3072 MiB for OS ubuntu24.04

Starting install...
Allocating 'virtinst-hq15fxgg-cloudinit.iso'                |    0 B  00:00 ...
Transferring 'virtinst-hq15fxgg-cloudinit.iso'              |    0 B  00:00 ...
Creating domain...                                          |    0 B  00:00
error: Cannot run interactive console without a controlling TTY

WARNING  Console command returned failure.
Running text console command: virsh --connect qemu:///system console poc-2
Domain creation completed.
VM 'poc-2' created (assigned MAC: 52:54:00:1a:f1:cf)
IP acquired: 192.168.122.234
VM created and running.
Name:    poc-2   (UUID c2d14a1c-eb34-4896-bc04-c32937aee06c)
IP:      192.168.122.234   (also reachable as poc-2.default)
SSH:     ssh ubuntu@192.168.122.234
Console: virsh console poc-2     (Ctrl-] to detach)
Teardown: vm-destroy poc-2   (or: virsh destroy poc-2 && virsh undefine poc-2)
# exit=0   (total wall time: 13 s)

$ stat -c '%y %s %n' ~/vm-images/resolute/resolute-server-cloudimg-amd64.img   # AFTER
2026-08-25 22:06:09.041851682 +0200 860447744 /home/verfeinerer/vm-images/resolute/resolute-server-cloudimg-amd64.img
$ sha256sum ~/vm-images/resolute/resolute-server-cloudimg-amd64.img           # AFTER
9dc7c5363c0146a08ba0c9aa834d82c2c6dfbb1c471ad9a2f0aba1189e21be05  .../resolute-server-cloudimg-amd64.img
```

**AC2 PASS:** mtime + size + sha256 unchanged (stat pair above) and the
`poc-2` output has no download lines — only the cache-hit line
`image ready: …`. Second VM boot also re-proved the ticket-05 lease
parser against a fresh real resolute boot (§5.1).

### 4.4 STEP 4 (AC3 negative leg) — deliberately unusable key

`poc-3` created with `--ssh-key /tmp/.../poc3key.pub`: a freshly
generated, validly-formatted ed25519 pubkey that is injected into the
guest, while the host's SSH probe uses the default `~/.ssh` identities —
none of which the guest authorizes. The probe must (and did) fail:

```console
$ time bin/vm-create poc-3 --ram 1 --vcpu 1 --disk 5 --ssh-key /tmp/.../poc3key.pub
image ready: /home/verfeinerer/vm-images/resolute/resolute-server-cloudimg-amd64.img
cloud-init files generated: /tmp/tmp.k87IMwbPo2 (meta-data, user-data; id=6d8b7ece-5391-4af0-8252-2a07796ba546)
WARNING  Requested memory 1024 MiB is less than the recommended 3072 MiB for OS ubuntu24.04

Starting install...
Allocating 'virtinst-0r5njgno-cloudinit.iso'                |    0 B  00:00 ...
Transferring 'virtinst-0r5njgno-cloudinit.iso'              |    0 B  00:00 ...
Creating domain...                                          |    0 B  00:00
error: Cannot run interactive console without a controlling TTY

WARNING  Console command returned failure.
Running text console command: virsh --connect qemu:///system console poc-3
Domain creation completed.
VM 'poc-3' created (assigned MAC: 52:54:00:40:76:d6)
IP acquired: 192.168.122.103
vm-create: SSH to ubuntu@192.168.122.103 not reachable yet — check: virsh console poc-3

real	1m47.019s
# exit=1
```

**AC3 PASS (negative):** lease acquired, then the 90 s SSH probe window
expired → exit 1 with the `virsh console` hint, and **no success block**
printed. (With the pre-hardening 60 s window the same leg finished in
1 m 16 s — first exploratory run — same behavior.)

### 4.5 STEP 5 (AC4) — autostart evidence + teardown

Autostart evidence for all three VMs, taken **before** teardown (host-
native observables; see §5.3 for why the ticket's literal
`dumpxml | grep '<autostart'` cannot match on this libvirt build):

```console
$ for v in poc-1 poc-2 poc-3; do virsh list --all --autostart | grep -E "Name|$v"; done
 Id   Name    State
 3    poc-1   running
 Id   Name    State
 4    poc-2   running
 Id   Name    State
 5    poc-3   running
$ ls -la /etc/libvirt/qemu/autostart/
lrwxrwxrwx 1 root root 27 Aug 25 22:06 poc-1.xml -> /etc/libvirt/qemu/poc-1.xml
lrwxrwxrwx 1 root root 27 Aug 25 22:08 poc-2.xml -> /etc/libvirt/qemu/poc-2.xml
lrwxrwxrwx 1 root root 27 Aug 25 22:08 poc-3.xml -> /etc/libvirt/qemu/poc-3.xml
```

PD3 note: a **real host reboot was NOT performed** — it would kill the
orchestrating session. The autostart state evidence above (set during
creation by `virt-install --autostart`, proven sufficient in isolation
in §2) + the orphan-free teardown proof is the accepted AC4 leg; a
manual `reboot` followed by checking the VMs come back is an **optional
manual check**, not part of this ticket.

Teardown:

```console
$ bin/vm-destroy poc-1
VM 'poc-1' destroyed and disk removed.
# exit=0
$ bin/vm-destroy poc-2
VM 'poc-2' destroyed and disk removed.
# exit=0
$ bin/vm-destroy poc-3
VM 'poc-3' destroyed and disk removed.
# exit=0
$ ls -la /etc/libvirt/qemu/autostart/
total 8   # empty — autostart markers removed with undefine
```

### 4.6 STEP 6 — final orphan check (PD4 baseline)

```console
$ virsh list --all
 Id   Name            State
--------------------------------
 -    setup-test-vm   shut off
$ virsh vol-list vm-pool
 Name                              Path
------------------------------------------------------------------------------------------------
 noble-server-cloudimg-amd64.img   /var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img
```

Only `setup-test-vm` (shut off, untouched) and exactly the pre-existing
Noble volume. No `poc-*` domain or volume anywhere. **AC4 PASS.**

---

## 5. Findings & required notes

### 5.1 Confirmed lease-file format (spec §6 pitfall 1; plan §8)

**CONFIRMED** (first real boot in ticket 05, re-confirmed here on real
resolute boots for all of `poc-1`/`poc-2`/`poc-3`):
`/var/lib/libvirt/dnsmasq/virbr0.status` is **not** the assumed
space-separated `<ip> <mac> <hostname>` columns. It is a **pretty-
printed JSON array** of lease objects, with each value on its own line:

```json
[
  {
    "ip-address": "192.168.122.190",
    "mac-address": "52:54:00:be:b2:b1",
    "hostname": "poc-1",
    "client-id": "ff:56:50:4d:98:00:02:00:00:ab:11:86:b7:eb:be:da:f5:e4:30",
    "expiry-time": 1787691980
  }
]
```

- Columns/keys: `ip-address`, `mac-address` (**lowercase** hex, colon-
  separated), `hostname` (bare lease hostname — from cloud-init
  `local-hostname:`), `client-id`, `expiry-time` (unix seconds).
- MAC case: lowercase in the file; the parser matches case-insensitively
  anyway.
- Stale leases of deleted VMs persist until renewed/expired (visible in
  the first-pass capture: `poc-s5`, `poc-s6` from earlier tickets sat
  alongside live leases) — the strict per-MAC match makes that harmless.
- The parser in `bin/vm-create` (`find_ip_for_mac`: `jq`, with a
  `python3` fallback, plus a legacy 2/3-column space-separated fallback
  for older libvirt/dnsmasq variants) was confirmed working **again with
  the real resolute image** — all three definitive VMs got their IP from
  the lease file, no fallbacks needed.

This is recorded as the confirmed format in
`docs/plans/poc-implementation-plan.md` §8 (replacing the "assumed" note).

### 5.2 DNS-name gap (spec §3.1 output wording, decision D9)

The success block keeps the spec-mandated text
`(also reachable as <NAME>.default)`. Measured on this host:

```console
$ getent hosts poc-1
192.168.122.190  poc-1
$ getent hosts poc-1.default
(nothing — does not resolve)
```

libvirt's dnsmasq serves the **bare** lease hostname only; there is no
`.default` domain and no rootless way to add one. The bare name works
for SSH and everything else; the `.default` suffix in the output is a
spec-verbatim aspiration, not a host-resolvable name (known gap, D9
keeps the text).

### 5.3 Autostart evidence on this libvirt build (ticket AC4 command quirk)

This host runs libvirt **10.0.0** with the `/etc/libvirt/qemu/`
layout. In this build `virsh dumpxml` **never emits an `<autostart>`
element** (verified: neither enabled nor `--disable`d states render
one), so the ticket's literal AC4 evidence command
`virsh dumpxml <NAME> | grep -A1 '<autostart'` cannot match here.
The flag is real and persisted — stored as a symlink
`/etc/libvirt/qemu/autostart/<NAME>.xml` and queryable via
`virsh list --all --autostart` (both shown in §4.5). `virt-install
--autostart` (the committed flow) sets it; proven in isolation in §2.
A real reboot remains the only full proof and is deliberately **not**
performed (PD3 — optional manual check).

### 5.4 known_hosts IP-reuse hazard (hit once, handled)

Between the exploratory and definitive passes, dnsmasq re-assigned test
IPs to new VMs with fresh host keys, which would have made the
`accept-new` SSH probe fail for the wrong reason. Handled by purging
**only the test IPs** (`192.168.122.53`, `.144`, `.207`, `.177`) with
`ssh-keygen -R <ip>` before the definitive pass (§3).

### 5.5 osinfo-db gap (spec §6 pitfall 6)

`--os-variant ubuntu-lts-latest` resolves to **ubuntu 24.04** in this
host's osinfo-db (0.20250606) — visible in every run's
`WARNING … for OS ubuntu24.04` and in the domain's `<libosinfo:os>`
metadata. Harmless today: the guest is 26.04 and the effective defaults
(virtio disk/net, q35, x86_64) are identical for 24.04 and 26.04, as
proven by all three successful unattended boots. **Revisit condition:**
after any `osinfo-db` update, switch to `ubuntu-26.04` once that
osinfo exists.

### 5.6 MAC-collision residual risk (plan §6)

MACs are `52:54:00:` + 24 random bits (~16.7 M space). A collision with
another lease on `virbr0` would surface as the *wrong* guest answering
the per-MAC lease match (wrong IP/hostname). Low-probability residual
risk, accepted for a POC — noted here per plan §6.

### 5.7 First-boot timing & windows

Observed first-boot time-to-IP: ~15–17 s (all three definitive VMs),
well inside the 120 s lease window (60 × 2 s). The SSH window is now
~90 s (see §6) — all positive legs succeeded within the first few
attempts.

## 6. Hardening changes applied in this ticket

`bin/vm-create` (only script touched; `bin/vm-destroy` needed no
changes):

1. **SSH verification window widened from ~60 s to ~90 s**
   (`verify_ssh_reachable`: 30 → 45 attempts at 2 s cadence). Rationale:
   ticket 06 observed one real boot where the guest needed slightly
   longer than the built-in 60 s window (vm-create exited 1 after
   printing the IP — the failure path). Everything else (2 s cadence,
   `BatchMode`, `accept-new`, success block only after proven SSH,
   timeout error naming `virsh console <NAME>`) is unchanged. This is
   the only functional change; all other ticket-07 investigation
   (autostart) concluded the committed flow was already correct.

Post-change re-verification (quick paths): `bash -n` OK on both
scripts; `bin/vm-create` (no NAME) → usage, **exit 2**;
`bin/vm-destroy` (no arg) → usage, **exit 2**;
`bin/vm-destroy poc-never-existed` → `VM 'poc-never-existed' is not
defined`, **exit 1**. And the full acceptance sequence above ran with
this final script.

Nothing from spec §8 out-of-scope was added (no `vm-list`, no
`--template`, no static IPs, no UEFI, no NAT rules, no `--clean-cache`).

## 7. Final state (end of ticket)

```console
$ virsh list --all
 Id   Name            State
--------------------------------
 -    setup-test-vm   shut off
$ virsh vol-list vm-pool
 Name                              Path
------------------------------------------------------------------------------------------------
 noble-server-cloudimg-amd64.img   /var/lib/libvirt/images/vms/noble-server-cloudimg-amd64.img
$ ls /etc/libvirt/qemu/autostart/
(empty)
$ ls ~/vm-images/
custom   resolute     # cache artifacts only (custom = ticket 05 Noble copy;
                       # resolute = the image downloaded in this ticket) — not VMs/volumes
```

Every VM and volume created in this ticket was torn down; the pool
matches the PD4 baseline exactly.
