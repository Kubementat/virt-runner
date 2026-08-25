# POC Specification — `vm-create` / `vm-destroy` (libvirt/KVM + cloud-init)

**Version:** 1.0
**Status:** Ready for implementation
**Project root:** `/home/verfeinerer/dev/research/experiments/virt-runner`

## Table of Contents

1. [Purpose & Goals](#1-purpose--goals)
2. [Environment Requirements](#2-environment-requirements)
3. [CLI Interface](#3-cli-interface)
4. [Pipeline (Implementation Steps)](#4-pipeline-implementation-steps)
5. [Verified Facts & URLs](#5-verified-facts--urls)
6. [Known Pitfalls](#6-known-pitfalls)
7. [Acceptance Criteria](#7-acceptance-criteria)
8. [Out of Scope / Follow-ups](#8-out-of-scope--follow-ups)
9. [Reference Implementation (starting sketch)](#9-reference-implementation-starting-sketch)
10. [Decisions Log](#10-decisions-log)

---

## 1. Purpose & Goals

### 1.1 What the POC is

Two small bash CLI tools:

- **`vm-create`** — the primary tool. Downloads (once, into a cache) an Ubuntu
  cloud image, generates cloud-init files with an injected SSH key and
  hostname, creates and boots a KVM VM via `virt-install --import` on the
  `default` libvirt NAT network with the disk in the `vm-pool` storage pool,
  polls the dnsmasq lease file for the guest IP, verifies SSH works, and
  prints access instructions.
- **`vm-destroy`** — the companion tool. Destroys a VM created by
  `vm-create`, undefines it, and deletes its disk volume from `vm-pool`.

Placement: `~/bin` (on `PATH`) or the project `bin/` directory. Plain bash,
no dependencies beyond what is already installed (see §2).

### 1.2 Goals

1. Invoke a **single CLI command** to get a running, SSH-accessible Ubuntu
   26.04 (`resolute`) VM.
2. **Repeatable**: N VMs of different sizes, all from the same cached cloud
   image (the ~820 MiB image is downloaded exactly once per release).
3. **No GUI, no interactive prompts, no `sudo`** (the user is in the
   `libvirt` + `kvm` groups; libvirt is user-reachable via
   `qemu:///system`).
4. Print **clear SSH access instructions** on completion.

---

## 2. Environment Requirements

Verified host facts (verified 2026-08-21). The POC targets this host;
preflight (step 1 of §4) must fail fast with a clear error if any of these
is violated.

| Component | Required state |
|---|---|
| Host OS | Ubuntu 24.04.4 LTS (32 cores, 31 GiB RAM, ~241 GiB free on `/`) |
| KVM | `/dev/kvm` present; `vmx`/`svm` in `/proc/cpuinfo` |
| libvirt / virsh | 10.0.0. `libvirtd.socket` is active — libvirtd is
  **socket-activated**, so `systemctl status libvirtd` showing "inactive" is
  normal and must NOT be treated as an error |
| QEMU / qemu-img | 8.2.2 |
| virt-install | 4.1.0 — supports `--cloud-init` with file-based sub-options.
  **Does not** have inline sub-options (`user=`, `hostname=`, `ssh-key=`)
  added in 4.2+, so the script MUST use `user-data=` / `meta-data=` files.
  (A capability probe such as `virt-install --help | grep cloud-init` is
  optional for portability, but the implementation may assume 4.1.0 file
  syntax on this host.) |
| Storage pool | `vm-pool` — active, ~855 GiB. **All** tool-created disks
  live here. |
| Network | `default` — NAT, dnsmasq DHCP, autostart, bridge `virbr0`,
  guest LAN `192.168.122.0/24`. |
| User groups | current user is in `libvirt` and `kvm` groups; `virsh list`
  works **without sudo** |
| qemu-guest-agent (host) | **NOT installed** — IP discovery must NOT depend
  on it |
| osinfo-db | Only knows up to `ubuntu25.10` — **no `ubuntu-26.04`**.
  Use the `--os-variant ubuntu-lts-latest` workaround (§5, §6). |

Known host quirk (do not model on it): the pre-existing VM
`setup-test-vm` has its disk in `/root/.cache/setup-tests/` (outside
`vm-pool`). The tool must never touch or replicate that layout.

---

## 3. CLI Interface

### 3.1 `vm-create`

```
vm-create <NAME> [OPTIONS]
```

`NAME` must be a valid libvirt domain name (letters, digits, `_`, `-`; not
starting with `-` or `.`). The script validates this and rejects names
already defined as domains.

| Option | Meaning | Default |
|---|---|---|
| `--ram GiB` | Guest RAM, in GiB (positive integer; converted for `virt-install`) | `4` |
| `--vcpu N` | vCPU count (positive integer) | `2` |
| `--disk GiB` | Virtual disk size in GiB (positive integer; provisioned qcow2 in `vm-pool`) | `30` |
| `--image URL` | Direct URL of a raw cloud image (overrides `--release`'s URL) | resolute cloud image URL (§5) |
| `--release REL` | Release shorthand (`resolute`, `noble`, …); selects the standard image/SHA256SUMS URLs for that release and overrides `--image`. If both are given, `--release` wins. | `resolute` |
| `--user USER` | Cloud user name to create and to SSH as | `ubuntu` |
| `--ssh-key PATH` | Path to the public key to inject | `~/.ssh/id_ed25519.pub` |
| `--no-boot` | Create the VM (definition + disk + cloud-init ISO) but do not leave it running; skip the wait-for-IP step | off |
| `--keep-going` | On image download/verification error: do not fail; continue if a previously cached image is available, else fail with a clear error | off |

Unknown options → exit 2 with usage. Missing `NAME` → exit 2 with usage.

#### Exact success output format

On a successful, booted run the script prints exactly this shape (values
substituted):

```
VM created and running.
Name:    poc-1   (UUID 6f1c2b3a-...)
IP:      192.168.122.52   (also reachable as poc-1.default)
SSH:     ssh ubuntu@192.168.122.52
Console: virsh console poc-1     (Ctrl-] to detach)
Teardown: vm-destroy poc-1   (or: virsh destroy poc-1 && virsh undefine poc-1)
```

- `ssh` line uses the user from `--user` (default `ubuntu`).
- The IP line only appears after the wait-for-IP **and** the SSH
  verification step succeed (acceptance criterion 3). If either times out,
  the script exits non-zero with a clear error that names `virsh console
  <NAME>` as the debugging path.
- For `--no-boot`, print instead: `VM created (not booted): <NAME>` plus
  the Console/Teardown lines (no IP/SSH lines).

Exit codes: `0` success; `1` runtime failure (preflight, download,
verification, no lease in time, SSH check failed); `2` usage error.

### 3.2 `vm-destroy`

```
vm-destroy <NAME>
```

Behavior, in order:

1. Preflight: `<NAME>` must be a **defined** domain — if not, exit 1 with a
   clear error (`VM '<NAME>' is not defined`); usage error (no arg) exits 2.
2. If the domain is running, `virsh destroy <NAME>`; then `virsh undefine <NAME>`.
3. Delete the disk volume: `virsh vol-delete <NAME>_vda.qcow2 vm-pool`
   (the volume name `virt-install` assigns, see §6). If the volume is absent,
   report a warning but exit 0 (VM definition is gone — treat a missing
   volume as already cleaned up, never as a failure that blocks reporting).
4. Print confirmation: `VM '<NAME>' destroyed and disk removed.` (or note
   that the volume was already absent).

No confirmation prompt — the tool is meant to be scriptable.

---

## 4. Pipeline (Implementation Steps)

### Step 1 — Preflight checks

Fail fast (exit 1, clear one-line message naming the failing check):

- `virsh list` succeeds → libvirt reachable, no sudo needed.
- Domain `<NAME>` not already defined → `virsh dominfo <NAME>` must fail.
  (Re-running with the same name must fail fast — acceptance criterion 5.)
- `vm-pool` appears in `virsh pool-list --all` as active.
- `default` network appears in `virsh net-list --all` as active.
- `--ssh-key` file exists and is non-empty.
- `NAME` is a valid domain name (regex `^[A-Za-z][A-Za-z0-9._-]*$`).

### Step 2 — Image download + SHA256 verify + cache layout

Cache layout:

```
~/vm-images/
  └── <release>/                      e.g. ~/vm-images/resolute/
      ├── <release>-server-cloudimg-amd64.img   (raw image, reused across VMs)
      └── SHA256SUMS                                 (fetched alongside image)
```

- If the image file is **absent**:
  1. `curl -fL --retry 3 -o <img>.part <IMAGE_URL>`
  2. `curl -fsL <SUMS_URL> -o <cache>/SHA256SUMS`
  3. Verify: `(cd <cache> && grep -F "$(basename <img>.part)" SHA256SUMS |
     sha256sum -c -)` — note the sums file entries are for the final image
     name, so verify against the name the sums file uses (fetch to final
     name in a temp file, or copy `.part` → final name before verify —
     implementers must pick one and test it; see decisions log).
  4. On mismatch or any curl failure: remove partial file, exit 1 with a
     clear error — **unless** `--keep-going` is set and a cached image
     exists, in which case continue with the cache and print a warning.
- If the image file is **present**: skip download entirely (this is what
  makes the 2nd VM of the same release cost zero bandwidth — acceptance
  criterion 2).
- `virt-install --import` copies the raw image into a fresh qcow2 in the
  pool, so the cache is never mutated; an optional `--clean-cache` flag may
  be added later but is out of scope for this POC.

Default URLs (release `resolute`):

- Image: `https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img`
- Sums: `https://cloud-images.ubuntu.com/resolute/current/SHA256SUMS`

`--release` substitutes the codename into the same URL pattern
(`https://cloud-images.ubuntu.com/<rel>/current/<rel>-server-cloudimg-amd64.img`).

### Step 3 — Cloud-init user-data / meta-data generation

Write both files to a `mktemp -d` directory, `chmod 600`, and delete the dir
on exit (trap). **Never** reuse a static path.

`meta-data`:

```yaml
id: <random lowercase uuid>
hostnames:
  local: <NAME>
  host: <NAME>
```

(The `id` is a fresh `uuidgen | tr A-Z a-z` per VM; it is arbitrary for
NoCloud but required to be present.)

`user-data`:

```yaml
#cloud-config
manage_etc_hosts: true
users:
  - name: <USER>            # from --user, default ubuntu
    groups: [sudo]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    ssh_authorized_keys:
      - <contents of --ssh-key file, single line>
package_update: false
```

Rationale:

- `manage_etc_hosts: true` keeps `/etc/hosts` consistent with the hostname.
- `package_update: false` keeps first boot fast (user can `apt upgrade`).
- **No `network:` section at all** — rely on the cloud image's default
  DHCP-on-first-NIC. Never hardcode interface names (`eth0` etc.; see
  §6, canonical/cloud-init#6887).
- Do not pass any `network-config` to the ISO.

The `meta-data` `hostnames` block sets the guest hostname; that hostname is
what makes `<NAME>.default` DNS work on the host.

### Step 4 — VM creation via `virt-install --import`

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

Why each flag is there:

| Flag | Why |
|---|---|
| `--name "$NAME"` | Domain name = user-visible VM name. |
| `--ram` / `--vcpu` | Sizes from CLI options (convert GiB → KiB, or use virt-install's `G`/`M` suffixes). |
| `--disk size=…,bus=virtio,pool=vm-pool,format=qcow2` | Pre-create the disk in `vm-pool` as a **full** qcow2 (not sparse-from-image via backing chain). Named `<NAME>_vda.qcow2` by convention — `vm-destroy` relies on this. |
| `--network network=default,model=virtio,mac=$MAC` | Attach to the `default` NAT network with a **fixed, known MAC** so IP discovery in step 5 is deterministic. Model `virtio`. |
| `--os-variant ubuntu-lts-latest` | Workaround for the osinfo-db gap (no `ubuntu-26.04` entry; resolves to 24.04, whose defaults — virtio disk/NIC, x86_64 — are identical for 26.04). See §5/§6. |
| `--location "$IMAGE_PATH" --import` | `--import` = **no interactive installer**: the (raw) image is imported directly into the pre-created qcow2 and the VM boots immediately. `--location` supplies the raw image source. |
| `--extra-args "console=ttyS0"` | Serial console available for debugging via `virsh console`, even though we don't attach interactively. |
| `--console none` | No graphical/interactive console — keeps the run fully unattended. |
| `--cloud-init "user-data=…,meta-data=…,clouduser-ssh-key=…,disable=on"` | Generates a **NoCloud ISO**, attached as CDROM **for the first boot only** (virt-install detaches it afterwards). `clouduser-ssh-key` injects the key into the distro's default cloud user; `disable=on` disables cloud-init on subsequent boots so reboots never re-apply (or reset) auth data. **Never use bare `--cloud-init`** — it triggers `root-password-generate=on` (10 s pause + printed root password). See §6. |
| `--autostart` | VM survives host reboots (acceptance criterion 4). |

**MAC scheme**: `52:54:00:<3 random hex bytes>`, generated once per run,
e.g.

```bash
MAC="52:54:00:$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
```

`52:54:00` is the Red Hat/QEMU OUI (locally administered); the 3 random
bytes make it unique per VM and avoid libvirt's auto-assignment collisions.
The MAC is known **before** boot, which is what makes step 5 possible.

`--no-boot` handling: after `virt-install` returns, if the domain is
running, stop it (`virsh --quiet destroy "$NAME"`, guarded by a
`virsh domstate` check) so the VM is defined but not running; then skip
steps 5–6's IP/SSH logic and print the not-booted variant of the output.
(Decisions log: see D5.)

### Step 5 — Wait for IP (dnsmasq lease polling) + SSH verification

libvirt's dnsmasq maintains a world-readable lease file
`/var/lib/libvirt/dnsmasq/virbr0.status`, one line per IPv4 lease.
**Assumed format: `<ip> <mac> <hostname>` (space-separated, 3 columns) —
verify on first real run** (file was empty at research time; see §6).

Algorithm:

1. Poll the lease file every 2 s, up to **120 s** total, for a line whose
   MAC column (column 2, case-insensitive) equals `$MAC`; extract the IP
   (column 1). Cloud-init first boot typically takes 20–40 s.
2. When the lease is found, also confirm `virsh domstate $NAME` is
   `running` (belt-and-braces; the lease implies the guest is up).
3. **SSH verification** (required before printing success — acceptance
   criterion 3): run, retrying for up to ~60 s total (every 2 s):
   `ssh -o ConnectTimeout=2 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$USER@$IP" exit`
   and require exit status 0.
4. If the lease never appears within 120 s, or the SSH check times out:
   exit 1 with a clear error that includes the console hint, e.g.
   `no DHCP lease after 120s — check: virsh console <NAME>`.
5. Fallbacks if the lease file is absent/unreadable/empty-format (in order):
   - `virsh domifaddr $NAME` (default ARP source — **unreliable** until the
     guest has ARP-announced; use only as fallback and retry), then
   - `arp -n | grep -i "$MAC"`.
   Prefer the lease file; treat fallbacks as best-effort only.

### Step 6 — Print access info

Print the exact success block from §3.1, substituting: the domain UUID
(`virsh dominfo $NAME | awk '/UUID/ {print $3}'`), the IP from step 5, the
DNS name `<NAME>.default` (libvirt dnsmasq on `virbr0` answers
`<hostname>.default` for domains on the `default` network), the user from
`--user`, and the `vm-destroy <NAME>` teardown command.

---

## 5. Verified Facts & URLs

All verified live 2026-08-21 on the target host.

| Fact | Value / Notes |
|---|---|
| Release | Ubuntu 26.04 LTS, codename **Resolute Raccoon** (`resolute`), released 2026-04-23, current build updated 2026-07-31 |
| Cloud image URL | `https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img` — HTTP 200 verified, ~820 MiB |
| SHA256SUMS URL | `https://cloud-images.ubuntu.com/resolute/current/SHA256SUMS` — contains the `.img` hash (verified: entry `9dc7c536… *resolute-server-cloudimg-amd64.img`) |
| amd64v3 variant | `resolute-server-cloudimg-amd64v3.img` exists for newer CPU ISAs; host supports it, but plain `amd64` is the safe POC choice |
| Default cloud user | `ubuntu` — no password, key-based auth only (stable convention across Ubuntu releases) |
| NoCloud datasource | When a NoCloud config-drive/ISO is attached, cloud-init uses it on first boot. **Without** metadata, cloud-init waits on EC2/metadata endpoints and hangs — so the script **always** attaches cloud-init metadata (via `virt-install --cloud-init`) |
| osinfo-db gap | Installed DB knows only up to `ubuntu25.10`; **no `ubuntu-26.04`**. Workaround: `--os-variant ubuntu-lts-latest` (resolves to 24.04 in this DB — safe: identical virtio disk/NIC and x86_64 defaults). Alternative: `apt update` the `osinfo-db` package, then check `virt-install --osinfo list | grep 26`. Revisit the hardcoded variant if the DB is updated |
| Fixed MAC scheme | `52:54:00:<3 random hex bytes>` (locally administered, unique per VM, known before boot) |
| Lease file | `/var/lib/libvirt/dnsmasq/virbr0.status`, world-readable (644), one line per IPv4 lease, **assumed** format `<ip> <mac> <hostname>` — **verify on first real run** |
| NIC→bridge mapping | `/var/lib/libvirt/dnsmasq/virbr0.macs` |
| Hostname DNS | libvirt dnsmasq answers `<hostname>.default` on the `default` network → `ssh ubuntu@<name>.default` works from the host |
| Disk volume naming | `virt-install` places the disk in `vm-pool` as `<NAME>_vda.qcow2`; `vm-destroy` must delete exactly that volume |
| virt-install 4.1.0 `--cloud-init` sub-options | `user-data=`, `meta-data=`, `network-config=`, `clouduser-ssh-key=`, `root-ssh-key=`, `root-password-generate=on` (pauses 10 s — avoid), `root-password-file=`, `disable=on`. No inline `user=`/`hostname=`/`ssh-key=` (those are 4.2+) |

---

## 6. Known Pitfalls

Each of these is a known failure mode; the implementation must handle or
explicitly avoid it:

1. **Lease file format is unverified.** At research time
   `/var/lib/libvirt/dnsmasq/virbr0.status` was empty. The assumed column
   order is `<ip> <mac> <hostname>`, but **verify on the first real run**
   (e.g. with the existing `setup-test-vm` or the first POC VM). If the
   format differs, adjust the awk parsing or fall back to
   `virsh domifaddr`.
2. **`virsh domifaddr` is unreliable as a primary IP source.** Its default
   `arp` source only works once the guest has ARP-announced; use it
   (and `arp -n`) strictly as a fallback behind the lease file.
3. **`--cloud-init disable=on` is important.** Without it, cloud-init may
   re-run (and reset auth data) on later boots if the ISO is still attached.
   `virt-install` detaches the ISO after first boot, but `disable=on` makes
   reboot behavior deterministic. Always include it.
4. **Never use bare `--cloud-init` (no sub-options) in the script.** It
   triggers `root-password-generate=on`: a 10-second pause and a printed
   root password — fatal for unattended use.
5. **Never hardcode the guest interface name** (`eth0` etc.) in any
   cloud-init `network-config`. Ubuntu 26.04 uses predictable names
   (`ens*`/`enp*`) via netplan and has a known rename bug
   (canonical/cloud-init#6887). Rely on the image's default
   DHCP-on-first-NIC and pass no network config at all.
6. **`--os-variant ubuntu-lts-latest` currently resolves to 24.04** in the
   installed osinfo-db. Harmless today (same virtio defaults), but revisit
   after any `osinfo-db` update and switch to `ubuntu-26.04` once it exists.
7. **Disk volume naming:** the disk lands in `vm-pool` as
   `<NAME>_vda.qcow2`. `vm-destroy` **must** delete this volume
   (`virsh vol-delete <NAME>_vda.qcow2 vm-pool`), or teardown leaves an
   orphan qcow2 (acceptance criterion 4).
8. **libvirtd shows "inactive":** it is socket-activated via
   `libvirtd.socket`; do not add a `systemctl is-active libvirtd` check that
   would false-fail.
9. **Existing VM `setup-test-vm`** has its disk outside `vm-pool`
   (`/root/.cache/setup-tests/`). Do not model the tool on it; never touch
   it.

---

## 7. Acceptance Criteria

All five, from the research plan, in meaning:

1. `vm-create poc-1 --ram 2 --vcpu 1 --disk 10` succeeds unattended from a
   clean state.
2. Exactly one ~820 MiB download: a second VM of the same release reuses the
   cached image (no re-download).
3. The script exits with the IP printed **only after**
   `ssh ubuntu@<ip>` actually succeeds — or else exits with a clear timeout
   error that includes the `virsh console` debugging hint.
4. The VM survives a host reboot (`--autostart`); the teardown command
   (`vm-destroy`) removes the VM **and** its disk, leaving no orphan qcow2
   in `vm-pool`.
5. Re-running `vm-create` with the same name fails fast (during preflight)
   with a clear error.

---

## 8. Out of Scope / Follow-ups

Explicitly out of scope for this POC (follow-up ideas from the research
doc):

- `vm-list` companion; `vm-create --template <name>` (clone from a golden VM
  via qcow2 backing chain / `virt-clone`).
- Static IP per VM: `--ip` via dnsmasq `addnhosts`, or a dedicated
  `network=` with `<ipaddr>` + `<host>` entries.
- Installing `qemu-guest-agent` into the image (cloud-init `packages`) for
  reliable `virsh domifaddr --source agent` IP discovery and clean
  shutdowns.
- UEFI guests: `--boot uefi` (needs OVMF packages) for Windows or
  secure-boot tests.
- Inbound access from outside the host: NAT blocks inbound from the LAN;
  would need a static `forward` rule on the libvirt network or a host
  `iptables -t nat` DNAT / socat port forward.
- Multi-release support table beyond the `--release` shorthand:
  `resolute` (26.04), `noble` (24.04), with per-release default users
  (Ubuntu is always `ubuntu`; e.g. Fedora → `fedora`, CentOS → `centos`).
- Optional `--clean-cache` flag (delete the cached raw image after import).

---

## 9. Reference Implementation (starting sketch)

The bash sketch below is a cleaned-up **starting point**, not the
deliverable. Implementers write the real `vm-create`/`vm-destroy` scripts,
which must additionally cover: `--image`, `--release`, `--user`,
`--keep-going`, full preflight (pool check, name validation), the SSH
verification step, the fallback IP paths, and the exact output format of
§3.1.

```bash
#!/usr/bin/env bash
# vm-create (POC sketch) - create and boot a KVM VM from a distro cloud image
set -euo pipefail

RELEASE="resolute"
IMAGE_URL="https://cloud-images.ubuntu.com/${RELEASE}/current/${RELEASE}-server-cloudimg-amd64.img"
SUMS_URL="https://cloud-images.ubuntu.com/${RELEASE}/current/SHA256SUMS"
CACHE_DIR="${HOME}/vm-images/${RELEASE}"
POOL="vm-pool"; NET="default"
LEASE_FILE="/var/lib/libvirt/dnsmasq/virbr0.status"

NAME="${1:-}"; shift || true
[ -n "$NAME" ] || { echo "usage: vm-create <name> [--ram G] [--vcpu N] [--disk GIB] [--no-boot]" >&2; exit 2; }
RAM=4; VCPU=2; DISK=30; BOOT=1
USER_NAME="ubuntu"; SSHKEY="${HOME}/.ssh/id_ed25519.pub"
while [ $# -gt 0 ]; do case "$1" in
  --ram) RAM="$2"; shift 2;; --vcpu) VCPU="$2"; shift 2;;
  --disk) DISK="$2"; shift 2;; --ssh-key) SSHKEY="$2"; shift 2;;
  --no-boot) BOOT=0; shift;; *) echo "unknown option: $1" >&2; exit 2;; esac; done

# 1. preflight
virsh list >/dev/null
virsh dominfo "$NAME" >/dev/null 2>&1 && { echo "VM '$NAME' already exists" >&2; exit 1; }
virsh net-list --all | awk -v n="$NET" '$1==n {print $3}' | grep -q active \
  || { echo "network '$NET' not active" >&2; exit 1; }
virsh pool-list --all | awk -v p="$POOL" '$1==p {print $2}' | grep -q active \
  || { echo "pool '$POOL' not active" >&2; exit 1; }
[ -f "$SSHKEY" ] || { echo "ssh key not found: $SSHKEY" >&2; exit 1; }

# 2. download + verify image (verify against final filename, as SHA256SUMS lists it)
mkdir -p "$CACHE_DIR"
IMG="$CACHE_DIR/${RELEASE}-server-cloudimg-amd64.img"
if [ ! -f "$IMG" ]; then
  curl -fL --retry 3 -o "$IMG" "$IMAGE_URL"
  curl -fsL "$SUMS_URL" -o "$CACHE_DIR/SHA256SUMS"
  if ! (cd "$CACHE_DIR" && grep -F "$(basename "$IMG")" SHA256SUMS | sha256sum -c -); then
    rm -f "$IMG"; echo "SHA256 mismatch for $IMG" >&2; exit 1
  fi
fi

# 3. cloud-init files
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"
uuid="$(uuidgen | tr 'A-Z' 'a-z')"
cat > "$TMP/meta-data" <<EOF
id: $uuid
hostnames:
  local: $NAME
  host: $NAME
EOF
cat > "$TMP/user-data" <<EOF
#cloud-config
manage_etc_hosts: true
users:
  - name: $USER_NAME
    groups: [sudo]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    ssh_authorized_keys:
      - $(cat "$SSHKEY")
package_update: false
EOF
chmod 600 "$TMP"/user-data "$TMP"/meta-data

# 4. fixed MAC + create VM
MAC="52:54:00:$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
virt-install --name "$NAME" \
  --ram $((RAM * 1024)) --vcpu "$VCPU" \
  --disk "size=${DISK},bus=virtio,pool=${POOL},format=qcow2" \
  --network "network=${NET},model=virtio,mac=${MAC}" \
  --os-variant ubuntu-lts-latest \
  --location "$IMG" --import \
  --extra-args "console=ttyS0" --console none \
  --cloud-init "user-data=${TMP}/user-data,meta-data=${TMP}/meta-data,clouduser-ssh-key=${SSHKEY},disable=on" \
  --autostart

if [ "$BOOT" -eq 0 ]; then
  [ "$(virsh domstate "$NAME")" = "running" ] && virsh --quiet destroy "$NAME"
  echo "VM created (not booted): $NAME"; exit 0
fi

# 5. wait for DHCP lease (assumed format: <ip> <mac> <hostname> — verify on first run)
IP=""
for _ in $(seq 1 60); do
  IP="$(awk -v m="$MAC" 'tolower($2)==tolower(m) {print $1; exit}' "$LEASE_FILE" 2>/dev/null || true)"
  [ -n "$IP" ] && break
  sleep 2
done
[ -n "$IP" ] || { echo "no DHCP lease after 120s — check: virsh console $NAME" >&2; exit 1; }

# 5b. verify the SSH path actually works before printing success
for _ in $(seq 1 30); do
  ssh -o ConnectTimeout=2 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$USER_NAME@$IP" exit 2>/dev/null && break
  sleep 2
done
ssh -o ConnectTimeout=2 -o BatchMode=yes "$USER_NAME@$IP" exit 2>/dev/null \
  || { echo "SSH to ${USER_NAME}@${IP} not reachable yet — check: virsh console $NAME" >&2; exit 1; }

# 6. access info (final format per spec §3.1)
UUID="$(virsh dominfo "$NAME" | awk '/UUID/ {print $3}')"
echo "VM created and running."
echo "Name:    $NAME   (UUID $UUID)"
echo "IP:      $IP   (also reachable as ${NAME}.default)"
echo "SSH:     ssh ${USER_NAME}@${IP}"
echo "Console: virsh console $NAME     (Ctrl-] to detach)"
echo "Teardown: vm-destroy $NAME   (or: virsh destroy $NAME && virsh undefine $NAME)"
```

Sketch gaps vs. spec (for implementers): `--image`/`--release` handling,
`--keep-going`, full error messages, `--user` propagation into the output
block, fallback IP sources (`virsh domifaddr`, `arp -n`), and a matching
`vm-destroy` script (§3.2) are not sketched here.

---

## 10. Decisions Log

Interpretation decisions made while writing this spec (the research doc left
them implicit or ambiguous):

- **D1 — `--release` vs `--image` precedence.** When both are supplied,
  `--release` wins (the research doc describes `--release` as "shorthand,
  overrides --image"). Default release is `resolute`; a bare `--image`
  without `--release` uses the URL as-is and caches it under a
  `custom/` subdir of `~/vm-images/` with the URL's SHA256SUMS if one is
  derivable (i.e. same-directory `SHA256SUMS`), else skips verification and
  warns.
- **D2 — `--user` is honored end-to-end.** The research sketch hardcoded
  `ubuntu` in the user-data; the spec generalizes it to `$USER_NAME` (the
  `--user` value) in the user-data `users:` block, the SSH verification
  step, and the printed output. `clouduser-ssh-key` still targets the
  distro default cloud user (`ubuntu` on Ubuntu images); for non-Ubuntu
  releases where `--user` differs from the distro default, the user-data
  `users:` block is authoritative. Documented in §3.1 and §4 (step 3).
- **D3 — YAML indentation fix.** The research doc's `meta-data` snippet had
  a stray indentation (`host:` indented under `local:`), which would be
  invalid NoCloud. The spec uses the correct form: `local:` and `host:` at
  the same level under `hostnames:`.
- **D4 — SHA256 verification against the final filename.** `SHA256SUMS`
  lists the final image filename, so the sketch downloads directly to the
  final name and verifies in place; on any failure the partial/corrupt file
  is removed. (The original research sketch downloaded to `.part`, which
  would not match the sums entry — resolved here.)
- **D5 — `--no-boot` mechanics.** `virt-install` starts the domain on
  success, so `--no-boot` is implemented by stopping the domain right after
  creation (`virsh destroy`, guarded by a `domstate` check) rather than by a
  virt-install flag. The VM remains defined with `--autostart`; the
  wait-for-IP/SSH steps and IP/SSH output lines are skipped, and the output
  is the not-booted variant (§3.1).
- **D6 — SSH verification is mandatory before success output.** Acceptance
  criterion 3 is interpreted as hard: the script retries
  `ssh … exit` for ~60 s and exits 1 (with console hint) on failure — it
  never prints the "VM created and running" block without a proven SSH
  connection.
- **D7 — `vm-destroy` tolerance for a missing volume.** If the domain is
  gone but the volume is already absent, `vm-destroy` exits 0 with a note
  (idempotent teardown); only "domain not defined" is a hard error.
- **D8 — Lease-file polling window.** 120 s (60 polls × 2 s), per the
  research doc's figure, with the understanding that cloud-init first boot
  is typically 20–40 s.
- **D9 — Output format authority.** §3.1's block is authoritative (slightly
  cleaner than the sketch's echo block, which omitted the `Name:`/UUID
  line); implementers must match it exactly, including field alignment.
- **D10 — Script typo fixes carried into the sketch.** The research sketch
  had `SSHKEY="${HOME}/ssh/id_ed25519.pub"` (missing dot) — corrected to
  `~/.ssh/id_ed25519.pub` to match the spec default.
