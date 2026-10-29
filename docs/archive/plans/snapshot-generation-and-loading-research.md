# Research: VM snapshots and clones from snapshots

> **Historical.** Implemented on `feature/snapshots` (2026-10-29). The README section "Snapshots and clones" and the code are the reference now; this file records how the feature was designed.

Status: research and plan only. No code has been written. Assumptions checked on real VMs in §9. Date: 2026-10-28. Base: `v0.7.0`
(`e9c97f4`). Implementation entry point: [`snapshot-clone-implementation-handoff.md`](snapshot-clone-implementation-handoff.md).

## 1. Summary and recommendation

**What we want.** Turn a provisioned VM (for example k3s plus kubementat installed, about
5-6 min of work) into a stored, immutable **snapshot**. Then start any number of
**clones** from it in seconds. Each clone gets the same disk contents but its own identity.

**Recommended design. Every decision is argued in §4.**

| Decision | Choice |
|---|---|
| Snapshot mechanics | **Cold, flattened copy.** First seal the guest (in-guest, over SSH), then shut it down cleanly. Then copy its disk into a new, standalone qcow2 **pool volume** (`virsh vol-create-from`, i.e. `qemu-img convert` run by libvirtd). No libvirt domain snapshots (internal or external) and no memory state. |
| Clone disks | **Thin qcow2 overlay** in the same pool, backed by the snapshot volume (`virsh vol-create-as … --backing-vol … --backing-vol-format qcow2`). Creating one takes well under 1 s. The snapshot is treated as immutable, and deleting it is refused while overlays still reference it. |
| Identity regeneration | **cloud-init re-run.** Before the copy, a "seal" script runs inside the source guest. It removes virt-runner's own `/etc/cloud/cloud-init.disabled` marker, runs `cloud-init clean --logs`, and resets `/etc/machine-id`. Each clone then boots with a **new NoCloud seed** that carries a new instance-id and hostname. cloud-init treats that boot as a first boot: new SSH host keys, a hostname, network config rendered for the **new MAC**, the user and key, and growpart. systemd generates a new machine-id. No libguestfs dependency. |
| Hostname | New hostname (= clone name) by default. `--keep-hostname` keeps the guest hostname recorded in the snapshot. That is the mode for k3s and other stateful apps whose identity is tied to the hostname. |
| CLI | `virt-runner snapshot create VM SNAP` / `snapshot list` / `snapshot show SNAP` / `snapshot delete SNAP`, plus **`virt-runner create NAME --from-snapshot SNAP`** for clones. A clone's `vm-create` envelope keeps the normal create shape. The `image` section is `null`, and a new `snapshot` section is added. |
| Store | Pool `vm-pool` (`/var/lib/libvirt/images/vms`). The disk is `<SNAP>.snapshot.qcow2`, and next to it a small raw volume `<SNAP>.snapshot.json` holds the metadata. Both are written and read only through `virsh`, so this works as an unprivileged `libvirt` group member, just like the seed ISO. |
| Distros | The seal script is distro-neutral. Ubuntu comes first, because it is the kubementat consumer and goes in the integration suite. Arch and Fedora are covered by the manual checklist before they are claimed in the README. |

**Expected speed (measured on real VMs, §9).** A clone takes about **11-14 s** from overlay
creation to SSH-ready on Ubuntu 26.04, ~20 s on Fedora 44 and ~50 s on Arch (Arch's sshd
waits for `systemd-time-wait-sync`). The floor is boot plus cloud-init plus DHCP. k3s on a
`--keep-hostname` clone re-registers its node ~16 s after the clone starts, and all pods are
Ready again after ~50 s. Creating a snapshot takes the seal (~1 s), a guest shutdown (2-4 s
for an idle guest, **91 s** for a guest running k3s workloads) and a copy of the allocated
disk data (~1.5 s for 2-3 GiB on this host's NVMe, page cache warm).

## 2. Current virt-runner architecture relevant to this

All paths are relative to `src/virt_runner/`.

### Disks: full copies, no backing files

- `cmd_create.py:325` → `Virtualizer.provision_volume` (`virtualizer.py:994`) does three
  things: `create_volume` (`:926`, `virsh vol-create-as vm-pool <NAME>_vda.qcow2 <disk>G --format qcow2`),
  then `upload_image` (`:956`, `virsh vol-upload` of the cached cloud image's bytes into that volume),
  then `resize_volume` (`:971`, which is mandatory because `vol-upload` silently resizes; see
  `docs/lessons-learned/001`).
  **Every VM disk is therefore a standalone qcow2 copy of the cloud image. No backing chain
  exists anywhere today.**
- Volume naming contract: `volume_name(name)` = `<NAME>_vda.qcow2` (`virtualizer.py:204`).
- Pool: `Virtualizer.POOL = "vm-pool"` (`virtualizer.py:555`). `install-prerequisites.sh:73`
  defines it as a `dir` pool at `/var/lib/libvirt/images/vms`, owned by `root:root` with mode
  `0755`. Verified on this host with `virsh pool-dumpxml vm-pool`. The invoking user is
  **not** root and cannot write that directory. **Everything virt-runner puts there goes
  through `virsh vol-*`.** The seed ISO lives there for the same reason: the hypervisor user
  cannot traverse a home directory (`virtualizer.py:770-778`).
- Image cache: `~/vm-images` (or `VM_CREATE_CACHE_DIR`) (`images.py:170`). This is
  host-user space that qemu never reads directly, because the image is copied into the pool.

### Domain definition

- `Virtualizer.create_vm` (`virtualizer.py:1042`) runs `virt-install --import` with:
  - `--disk vol=vm-pool/<vol>,bus=virtio`
  - `--network network=default,model=virtio,mac=<random 52:54:00:…>`, with the MAC from
    `generate_mac` at `:1017`
  - the seed ISO as a `--disk …,device=cdrom` from the pool (not `--cloud-init`; see
    `docs/lessons-learned/008`)
  - `--autostart`
  - on aarch64, a `--boot uefi=on,loader=<no-secboot>` (`:1107-1110`)
  - for mounts, `--memorybacking source.type=memfd,access.mode=shared` plus one
    `--filesystem …driver.type=virtiofs` per mount (`:1112-1119`)
- The domain XML is generated by virt-install; virt-runner does not template XML. Per-VM
  facts are recorded afterwards as libvirt metadata (`set_domain_metadata`, `:1172`). This is
  an element `<virt-runner distro= user= identity=/>` under namespace
  `https://virt-runner.local/vm`, read back by `domain_meta` (`:1197`). **This is the only
  per-VM state virt-runner keeps. There is no state file on the host.**

### cloud-init seed and identity

- `generate_cloud_init_files` (`virtualizer.py:869`) creates a fresh `uuid4` as the meta-data
  `id:`, which is the instance-id (`:886`). It also renders `render_meta_data(id, name)`
  (`:504`: `local-hostname: <NAME>`, `hostnames.local/host`) and `render_user_data` (`:446`).
- `render_user_data` writes `manage_etc_hosts: true`, the user with sudo and the
  `ssh_authorized_keys` key, `package_update: false`, mount `bootcmd` lines, and a
  `runcmd` that runs
  **`echo "Disabled by virt-runner" > /etc/cloud/cloud-init.disabled`** (`:481`).
  **That line matters most for this feature.** After a virt-runner VM's first boot,
  cloud-init is disabled for good. A clone booted from a copy of that disk would ignore any
  new seed. It would keep the source's hostname and host keys, and it would keep the network
  config rendered for the source's MAC.
- No `network-config` file goes into the seed. cloud-init therefore renders its **fallback**
  network config. On netplan and networkd systems that config matches the first NIC by
  `macaddress` and renames it with `set-name` (cloud-init network v2 docs: "`macaddress` is
  **required** for cloud-init to perform the rename"). **A clone with a new MAC and the
  source's rendered network config gets no IP.** This is a well-known template/clone failure
  (see Sources: the XCP-ng and Proxmox threads). *Verified, with one refinement (§9, rows
  1-2):* Ubuntu 26.04's `/etc/netplan/50-cloud-init.yaml` does carry `match: macaddress`
  plus `set-name`, and an unsealed Arch clone gets **no lease**. An unsealed Ubuntu 26.04
  clone, however, still gets an IP, through dracut's catch-all
  `/run/systemd/network/zzzz-dracut-default.network` (`DHCP=yes`, `ClientIdentifier=mac`).
  It keeps the source's hostname, machine-id and SSH host keys and ignores the new seed, so
  the seal is still required; "no network" is just not the symptom on every distro.
- The seed ISO is built with `xorrisofs -V cidata` and uploaded as pool volume
  `<NAME>-cloudinit.iso` (`build_cloud_init_iso`, `:792`). It stays attached for the VM's
  lifetime, and `destroy` removes it (`:1576`).

### SSH key, IP lease, SSH verify, scripts

- `ensure_ssh_key` (`:673`) creates `~/.ssh/virt_runner_key[.pub]` when absent. The private
  half's path is recorded as metadata `identity`, and `access_commands` (`output.py:189`)
  puts it into `ssh_command` as `-i <identity>`. kubementat's `ssh_identity_from` parses
  exactly that.
- `wait_for_ip` (`:1284`) polls the dnsmasq JSON lease file
  `/var/lib/libvirt/dnsmasq/virbr0.status` **by MAC**. The freshest lease wins (`:1234`).
  `domifaddr` and `arp` are fallbacks.
- `verify_ssh_reachable` (`:1350`) uses `StrictHostKeyChecking=no` and
  `UserKnownHostsFile=/dev/null`, so regenerated host keys on a clone never trip virt-runner
  itself.
- `run_script` (`:1389`) and `script_command` (`:486`): the script is piped over SSH stdin,
  and `cloud-init status --wait` runs first.

### `--mount`

- `_validate_mounts` (`cmd_create.py:61`) turns the specs into `{source, target, tag: mountN}`.
- The guest side is a first-boot `bootcmd` that `mkdir`s the target, appends an idempotent
  fstab line `mountN <target> virtiofs defaults,nofail 0 0`, and mounts. Later boots rely
  on fstab, because cloud-init is disabled then (`docs/lessons-learned/005`).

### destroy, list, ownership

- `destroy_vm` (`virtualizer.py:1530`) runs `virsh destroy` (if running), then
  `undefine --nvram`, then `vol-delete <NAME>_vda.qcow2`, then a best-effort delete of the
  seed ISO. **It only ever deletes the two volumes named after the VM**, so it would not
  touch a snapshot volume or a clone's backing file.
- `list_vms` (`:1481`) lists domains whose disk `<source file>` lies under the pool path
  (`_has_disk_in_pool`, `:1464`). A clone's overlay lives in the pool, so clones are listed.
  Snapshot volumes are attached to no domain, so they never show up as VMs.
- `owns_domain` (`:1600`) uses the metadata or a disk in the pool. It is what `modify` uses.

### JSON envelope and errors

- `output.document` (`output.py:120`) writes `{tool: "vm-<cmd>", version, status, error, …sections}`.
  `output.fail` (`:148`) writes `error = {code, message, stage?}`. Exit codes are 0, 1, and 2 for usage.
- `VirtError(message, code, stage)` (`errors.py`). Stable codes are listed in README
  "### Error codes" (`README.md:157-194`); a new code must be added there first.
- `create` keeps an accumulating `run_state` and a `stage` variable. Its default code per
  stage is `_DEFAULT_CODE` (`cmd_create.py:25`), and `_create_document` (`:96`) renders the
  sections finished so far, also on failure (`:370`).
- `JsonCommand` (`args.py`) adds `--json` and turns usage errors into envelopes. The `tool`
  id is `vm-` + the command name (`output.tool_name`).
- Conventions to keep: no libvirt Python bindings. Every subprocess goes through
  `_run/_stdout/_spawn/_quiet/_passthrough` (`virtualizer.py:1-15`). Pure logic sits in
  module-level functions so `tests/unit_test.py` can test it with no host. Every feature
  gets a minimal case in `tests/integration_test.py` (`AGENTS.md`).

## 3. Requirements

### Functional

- **R1.** `create` stays as it is today. Snapshots are an addition, and existing flags,
  outputs and codes do not change (semver MINOR).
- **R2.** A single command snapshots a virt-runner VM:
  `virt-runner snapshot create VM SNAP [--json]`. The snapshot is **durable and independent
  of the source**. Destroying the source afterwards must leave the snapshot intact.
- **R3.** A single command creates a VM from a snapshot. Its disk state equals the
  snapshot, and it gets a new domain name, UUID, MAC, DHCP IP, machine-id and SSH host
  keys. It gets a new hostname by default (R6 covers keeping it). **N clones of one
  snapshot run at the same time** without IP or DHCP-identity clashes.
- **R4.** The clone's `--json` document is a normal `vm-create` document (`vm.ip`,
  `vm.ssh_command` with `-i`, `vm.ssh_user`, `booted`, `scripts`, `error.code` and
  `error.stage`), so `lease_from`/`ssh_identity_from` in kubementat's `harness/vmm.py`
  work unchanged.
- **R5.** `--script` works on a clone (post-clone fix-ups, the feature test itself).
  `--mount`, `--ram`, `--vcpu` and `--disk` (grow only) can be given at clone time.
- **R6.** There is an opt-in way to keep the guest hostname, for apps whose identity is the
  hostname (the k3s node name, local-path PV node affinity).
- **R7.** Snapshot management: `list`, `show` (inspect), and `delete`, all with `--json`.
  `delete` refuses while clones depend on the snapshot.
- **R8.** Works as an unprivileged member of the `libvirt` group against `qemu:///system`,
  the same as every other command. No new sudo, and no new mandatory host package.

### From the kubementat consumer (`tests/e2e/`)

- The harness calls `virt-runner … --json` and parses one document from stdout. On error
  it reports `tool`, `error.code` and `error.stage` (`vmm.py:47-98`).
- It claims ownership *before* create (`vmm.py:131`) and destroys unconditionally,
  tolerating `vm-not-defined`. A clone must be destroyable with plain `destroy`.
- `stage_provision` (`harness/stages.py:347`) needs `vm.ip` and the identity. It then
  waits for **"exactly one Ready node"** (`:398`, `_nodes_ready` `:469`). `stage_install`
  ends with `_expect_pvcs` (no Pending PVCs, `:742`) and `_expect_single_ready_node`
  (`:751`). **A clone that renames the k3s node breaks both checks.** The old Node stays
  NotReady, and local-path PVs carry node affinity to the old hostname. So the harness
  needs `--keep-hostname`.
- The harness uses `StrictHostKeyChecking=accept-new` with a per-run known_hosts file
  (`harness/guest.py:24,80`). New host keys on a new IP are fine. A DHCP IP reused by a
  *later* clone within the same run, with different host keys, would be rejected. That is
  the harness's concern, and the fix lives there (a known_hosts file per VM). It is noted
  in §7.

### Non-goals

- Memory or RAM-state snapshots, and "resume where it was" (§4.1.4).
- Live snapshots of a running guest with no downtime (§4.1.3).
- Reverting the source VM to a snapshot. A "snapshot" here is a template for *new* VMs,
  not a restore point. To go back, destroy the VM and clone it again.
- Exporting or importing snapshots between hosts. Possible later through `vol-download`
  of the flattened volume.
- Multi-disk VMs. virt-runner creates exactly one disk.
- Clones on a different pool or network than `vm-pool`/`default`.
- Application-level re-identification (k3s, databases), beyond documenting it and
  offering `--keep-hostname` and `--script`.

## 4. Options considered

### 4.1 Snapshot mechanics

#### 4.1.1 libvirt internal snapshots (`virsh snapshot-create-as`, qcow2-internal)

- They are stored inside the VM's own qcow2 and tied to the domain's snapshot metadata.
  `destroy` deletes `<NAME>_vda.qcow2`, which deletes the snapshots too. **That fails R2.**
  A new domain also cannot boot "from an internal snapshot" without first copying it out
  (`qemu-img convert -l`).
- Internal snapshots were historically refused for pflash/OVMF firmware: "internal
  snapshots of a VM with pflash based firmware are not supported". That covers all aarch64
  virt-runner guests. Newer libvirt narrows this to "requires QCOW2 nvram format"
  (Sources: libvirt patch, Arch FS#78136). virt-install's NVRAM is raw.
- With memory state they are also blocked or unreliable when virtiofs is attached (below).
- **Rejected.**

#### 4.1.2 libvirt external disk-only snapshots (`snapshot-create-as --disk-only`)

- libvirt swaps the source's disk to a new overlay, and the old file becomes the read-only
  base. The source then *depends* on the base. That inverts what we need: the base is
  named `<SRC>_vda.qcow2` and `destroy SRC` deletes that name. Every snapshot also adds a
  layer to the source's chain, and libvirt's external-snapshot delete/revert support only
  arrived in recent versions (9.x/10.x).
- **Rejected.** It complicates destroy and naming for no gain over a copy.

#### 4.1.3 Live copy: running source + qemu-guest-agent `fsfreeze`

- `virsh domfsfreeze`/`domfsthaw` need `qemu-guest-agent` in the guest. It is not in
  virt-runner's user-data, and the Ubuntu cloud image does not have it by default
  (*verified for 26.04, §9: `dpkg -l qemu-guest-agent` finds no package*). A frozen copy is filesystem-consistent but only
  *crash-consistent* for applications (k3s/sqlite, containerd).
- The seal step (§4.2) mutates cloud-init state. If the guest keeps running after that,
  its state is "sealed" but live, and a reboot re-runs cloud-init anyway. So the downtime
  buys nothing.
- **Rejected for v1.** It could come back later as `--live` for VMs whose apps tolerate
  crash consistency.

#### 4.1.4 Memory-state snapshots

- They would preserve RAM: the old IP and DHCP lease in kernel state, the hostname, the
  machine-id in every process, k3s in-memory state and TCP sessions. A memory image cannot
  be re-identified, and N clones of one memory image are N copies of the same running
  identity. The size equals the RAM, and restore needs the identical device model.
- libvirt documents that virtiofs blocks save, managed-save and memory snapshots with
  virtiofsd < 1.11. Even when it works, libvirt "do[es] not snapshot the state of the
  files shared via virtiofs" (Sources: libvirt kbase virtiofs, libvirt-users thread).
- **Out of scope.**

#### 4.1.5 Cold flattened copy + thin overlays (chosen)

1. Seal the source in the guest (§4.2), then power it off cooperatively.
   `Virtualizer.reboot` already shows the shutdown-poll-destroy pattern
   (`virtualizer.py:1923`). For a snapshot we **do not** fall back to `destroy`: a hard
   power cut after the seal would capture an unclean filesystem. Time out with an error.
2. Copy the source volume to `<SNAP>.snapshot.qcow2` in `vm-pool` with
   **`virsh vol-create-from vm-pool <xml> <SRC>_vda.qcow2 --inputpool vm-pool`**, where
   `<xml>` is a volume definition that virt-runner writes itself:
   ```xml
   <volume><name>SNAP.snapshot.qcow2</name><capacity unit='bytes'>N</capacity>
     <target><format type='qcow2'/><compat>1.1</compat>
       <permissions><mode>0644</mode></permissions></target></volume>
   ```
   The `<compat>1.1</compat>` element is required: without it libvirt 10.0 writes a
   **qcow2 v2 (`compat: 0.10`)** image, while the volumes virt-runner creates today are
   `compat 1.1` (§9).
   libvirtd does the copy as root with `qemu-img convert`. That gives a sparse output with
   only the allocated clusters, and it **flattens** any backing chain, which matters for
   snapshots of clones (*verified, §9 row 8: the copy of an overlay has no backing file*).
   The copy is **not compressed**: the zlib-compressed clusters of the uploaded cloud image
   are expanded, so a fresh Ubuntu VM of 0.86 GiB allocation gives a 2.12 GiB snapshot.
   `vol-create-from` is preferred over `vol-clone`. *Verified (§9):* `vol-clone` of an
   overlay also flattens, so the explicit XML is not needed for that, but `vol-clone`
   produces a `root:root 0600` file with no say over mode or compat. The explicit `0644`
   mode keeps the base readable by the hypervisor user and by the invoking user
   (`qemu-img info` without sudo) no matter how DAC relabeling goes (§7).
   - ~~Optional speed-up: `--reflink`.~~ *Refuted (§9 row 11):* libvirt 10.0 rejects
     `--reflink` for any qcow2 target (`unsupported flags (0x2) in function
     storageBackendCreateQemuImg`), whatever the filesystem; the pool is ext4 anyway.
     Dropped.
3. Write the metadata sidecar `<SNAP>.snapshot.json` (§5.6) the same way as the seed ISO:
   `vol-create-as … --format raw` plus `vol-upload`.
4. Leave the source **shut off**. With `--start-source`, start it again (§4.2.4 covers
   what that boot does).

A clone disk (§5.4) is created with
**`virsh vol-create-as vm-pool <CLONE>_vda.qcow2 <disk>G --format qcow2 --backing-vol <SNAP>.snapshot.qcow2 --backing-vol-format qcow2`**.
That is instant, and the clone uses only as much space as it writes. libvirt's dir-pool
backend runs `qemu-img create -f qcow2 -b <abs path> -F qcow2`, so the backing path is
absolute inside the pool. libvirt reads the chain from the image header when the domain
starts. AppArmor's `virt-aa-helper` adds read rules for backing files, and DAC labels the
chain (*verified, §9 row 3: two clones run at once, the snapshot is relabeled
`libvirt-qemu:kvm` with mode `0644` kept, AppArmor grants it `rk`, no denials, and the
label stays after the clones stop*).

**Why copy instead of reusing the source file as the base:** independence (R2), immutable
semantics, and the existing `destroy` stays correct without a single change. The cost is
one copy of the allocated data per snapshot, done once.

**Decision: 4.1.5.**

### 4.2 Identity regeneration

What must differ per clone, and who regenerates it:

| Identity | Why it must differ | Regenerated by |
|---|---|---|
| Domain name/UUID | libvirt | virt-install (new domain) |
| MAC | DHCP, ARP | `generate_mac()` |
| IP | — | dnsmasq, **but see machine-id** |
| machine-id | systemd-networkd hashes it into the DHCP client-id/DUID, and **dnsmasq prefers client-id over MAC**. Identical machine-ids mean the same lease or IP for two clones (Sources: arch-boxes #25, Broadcom KB 82229, Proxmox threads) | sealed to empty/`uninitialized`, then systemd generates a new one at boot. In a KVM guest `systemd-machine-id-setup` derives it from the SMBIOS UUID, and virt-install gives every domain a new UUID, so the result is unique per clone (*verified, §9: clone machine-id = its domain UUID without dashes; Ubuntu/Arch leases carry distinct DUID client-ids; Fedora NM uses `01:<MAC>`*) |
| SSH host keys | security, `known_hosts` | cloud-init `cc_ssh` (`ssh_deletekeys` defaults to true) on a new instance |
| Hostname, `/etc/hosts` | DNS `<name>.default`, apps | cloud-init `set_hostname`/`update_hostname` from meta-data, plus `manage_etc_hosts` |
| Network config (MAC match) | otherwise no network at all | cloud-init re-renders the fallback config for the new MAC on a new instance |
| Root fs size | `--disk` grow at clone | cloud-init `growpart`/`resizefs` (in default `cloud.cfg`) |
| authorized_keys | — | cloud-init `users` (adds the clone's key; the snapshot's key stays) |

#### 4.2.1 (a) libguestfs: `virt-sysprep` / `virt-customize` on the copy (offline)

- Pros: the source is never touched, and the operations are well tested (`machine-id`,
  `ssh-hostkeys`, `net-hwaddr`, `customize --delete`).
- Cons:
  - It is a heavy host dependency (supermin appliance). It is installed on this dev host but
    is not in `install-prerequisites.sh`.
  - On Ubuntu hosts it fails for non-root users unless `/boot/vmlinuz-*` is made readable.
    That is a long-standing Ubuntu packaging issue (*from experience, not re-verified
    here*).
  - It needs **write access to the snapshot file**, which lives in the root-owned pool.
    The unprivileged user cannot write it, so it would need sudo or the libvirt guestfs
    backend with extra plumbing. That breaks R8.
  - It does not know virt-runner's `cloud-init.disabled` marker unless told.
- **Rejected as a default.** It could be an optional `--sysprep=guestfs` later.

#### 4.2.2 (b) New instance-id alone

- cloud-init compares the cached instance-id with the datasource's. "If they differ, the
  boot is treated as a new instance's first boot" and "run[s] all per-instance
  configuration", including host key rotation (Sources: cloud-init first-boot docs).
- **But it does not work alone for virt-runner.** The source's `/etc/cloud/cloud-init.disabled`
  (`virtualizer.py:481`) stops cloud-init before it ever looks at the new seed. machine-id
  is also not cloud-init's job.
- **Necessary but not sufficient.**

#### 4.2.3 (c) In-guest seal before shutdown + new seed per clone (chosen)

The `snapshot create` command runs a small, distro-neutral bash script in the source over
SSH with `sudo -n bash -s`. This is the same delivery as `_FS_GROW_SCRIPT`
(`virtualizer.py:1949-2034`):

```bash
set -eu
cloud-init status --wait >/dev/null 2>&1 || true   # never seal mid-first-boot
uname -n                                            # recorded as guest_hostname (first stdout line)
rm -f /etc/cloud/cloud-init.disabled                # virt-runner's own first-boot marker
cloud-init clean --logs                             # /var/lib/cloud + logs: next boot = first boot
if [ -d /run/systemd/system ]; then                 # same effect as `cloud-init clean --machine-id`
  printf 'uninitialized\n' > /etc/machine-id        # (older cloud-init lacks the flag)
else rm -f /etc/machine-id; fi
[ -L /var/lib/dbus/machine-id ] || rm -f /var/lib/dbus/machine-id
rm -f /var/lib/systemd/random-seed                  # systemd: do not ship a shared seed
rm -f /var/lib/NetworkManager/*.lease /var/lib/dhcp/*.leases 2>/dev/null || true
# virt-runner's own virtiofs lines; the clone's first boot writes its own --mount set
sed -i -E '/^mount[0-9]+[[:space:]]+[^[:space:]]+[[:space:]]+virtiofs[[:space:]]/d' /etc/fstab
sync
```

The script then exits. virt-runner itself sends `virsh shutdown` and polls (step 1 of
§4.1.5). That way the exit code of the seal is collected before the guest goes away.

Notes:

- **`uname -n`, not `hostname`.** The first draft used `hostname`. The Arch cloud image
  ships no `hostname` binary (inetutils is not installed), so under `set -e` the seal died
  on line 3 with exit 127 before it changed anything (§9 row 10). `uname -n` is in
  coreutils on every target. The script above is the corrected, verified version; it ran
  with exit 0 on Ubuntu 26.04, Fedora 44 and Arch.
- **SSH host keys are not deleted by the seal.** If cloud-init failed on a clone, a
  key-less sshd would make it unreachable. Rotation is cloud-init's job on the new
  instance. *Refined (§9):* Ubuntu 26.04 **does** ship `sshd-keygen.service`
  (`ConditionFirstBoot=yes`, `ssh-keygen -A`), so a fallback exists there. Keeping the
  keys is still the simpler choice, and every sealed clone got new keys from cloud-init.
- **The seal makes no difference to running mounts.** It strips the fstab lines while the
  share is still mounted; the shutdown is clean (§9 row 5).
- **The network config is not deleted** (`--configs network` is not used). cloud-init
  overwrites it for the new MAC on the new instance. If cloud-init fails, the clone has no
  network either way.
- On the clone, virt-runner's normal user-data runs again, and its `runcmd` re-creates the
  `cloud-init.disabled` marker. **A clone ends up in exactly the state of a freshly created
  VM**, and later reboots behave the same as today.
- `cloud-init clean` (Sources: cloud-init CLI docs) recommends `--machine-id` as "best
  practice when cloning a golden image". The script writes the same value itself so it
  also works on older cloud-init.

**Per distro:**

- **Ubuntu (resolute):** netplan → networkd with DUID client-id. Every row above applies.
  First target: integration test plus the kubementat harness.
- **Arch (arch-boxes):** systemd-networkd and cloud-init, same as Ubuntu. arch-boxes
  itself ships an empty machine-id for this reason (arch-boxes #25). Manual checklist.
- **Fedora (44):** NetworkManager. cloud-init renders a keyfile matched by MAC.
  *Verified (§9 row 10):* NM's DHCP client-id is `01:<MAC>`, so two clones got distinct
  IPs; there is no `/var/lib/NetworkManager/secret_key` on the image. The kernel command
  line carries `systemd.firstboot=off`, so nothing blocks the console. Two side effects:
  the source's keyfile (`cloud-init-<old if>.nmconnection`, old MAC) stays on the clone,
  inert; and NM sends its first DHCP request **before** cloud-init sets the new hostname,
  so one clone's lease registered the *source's* hostname with dnsmasq (the other none).
  A fresh Fedora VM registers no name at all (`localhost` at DHCP time), so Fedora's
  `dns_name` was never reliable; see §7.
- **aarch64:** every clone gets a fresh NVRAM from virt-install, so boot entries written
  by the source are lost. Ubuntu and Fedora aarch64 cloud images boot through the
  removable-media fallback path (`\EFI\BOOT\BOOTAA64.EFI`), which is expected to work
  (*not tested: the verification host is x86_64, §9*).

**Decision: (c).** It needs no host dependency, works unprivileged, uses the same SSH
channel as `--script`, and covers all three distros with one script.

#### 4.2.4 Consequence for the source VM

The seal mutates the source. When the source is started again, by `--start-source` or by
`virsh start`, cloud-init runs a full first boot with **its own** seed:

- It keeps the same hostname (same meta-data) and the same MAC.
- It gets new SSH host keys. *Verified (§9 row 4):* the machine-id does **not** change,
  because systemd re-derives it from the SMBIOS UUID, which is the source domain's
  unchanged UUID. The DHCP client-id therefore stays the same, and the source got its old
  IP back.
- Its own seed's mount `bootcmd` re-adds its fstab lines, so its `--mount` set returns.
- Afterwards it is back to the normal disabled-after-first-boot state.

This is documented and stated in the JSON (`source.sealed: true`). In the kubementat flow
the source is destroyed right after the snapshot, so it does not matter there.

The alternative that leaves the source untouched is a "sealer" domain. It would boot a
temporary overlay of the source's disk with the **source's MAC** (safe, because the source
is shut off), seal it, and flatten. That costs one extra boot (~20 s) and a fair amount of
code. **Not in v1**, but it is a compatible later addition (`--no-touch-source`).

### 4.3 Application identity caveat, worked example: k3s

These facts come from k3s docs and community reports; the starred items were unverified when written and are now checked in §9 row 7:

- **The node name is the hostname** unless `--node-name`/`K3S_NODE_NAME` is set. k3s
  stores a node password in `/etc/rancher/node/password` and a hash in the secret
  `kube-system/<node>.node-password.k3s`. Re-registering a node name needs the original
  password (Sources: k3s architecture docs, lxz.is-a.dev).
- **A clone with a new hostname** registers a second Node. The old Node object stays
  `NotReady`. Pods on it are evicted after the default 5-minute toleration. Pods with
  **local-path PVCs stay Pending forever**, because the PV's `nodeAffinity` names the old
  hostname. `--with-node-id` does not help, because it also produces a new node name.
  *Verified (§9 row 7), with one correction:* the **PVC stays `Bound`**; it is the pod
  that stays `Pending` ("1 node(s) didn't match PersistentVolume's node affinity"). So
  kubementat's `_expect_pvcs` (no Pending PVCs) would *pass*, and the failure shows in
  `_expect_single_ready_node` and in pods never becoming Ready. For the first ~40 s the
  old Node even still reads `Ready` (stale status from the datastore).
- **A clone with the kept hostname** reuses the Node object, the node password file
  matches the stored secret, and local-path PVs still match. Its IP is new:
  - k3s with the default **sqlite/kine** datastore keeps no IP in the datastore. On
    startup the agent re-detects the node IP and updates the Node addresses and flannel
    annotations (\*).
  - The serving certificate: "Dynamiclistener adds SANs for any hostname or IP address
    requested via a HTTP Host header or TLS SNI handshake" (Brad Davidson, quoted in
    Sources). The new IP is added on first use. The in-guest kubeconfig points at
    `https://127.0.0.1:6443` and is not affected anyway.
  - Breakage is expected when the config **pins** `node-ip`/`advertise-address`/`tls-san`
    to the old IP ("failed to find interface with specified node ip", Rancher Slack
    archive). The same goes for **embedded etcd** (`--cluster-init`), whose member peer
    URLs carry the old IP (SUSE forum: "K3s w/ etcd fails to restart after IP change").
    Kubementat uses neither: its provision script writes only `write-kubeconfig-mode`.
  - Several clones with the same hostname run at once without trouble, because they are
    separate clusters. The only shared thing is the libvirt dnsmasq DNS name.
    *Corrected (§9 row 6):* the name goes to the **last** clone to request a lease;
    dnsmasq removes it from the earlier lease. Leases still work.
- (\*) *Verified (§9 row 7):* on a kept-hostname clone the Node name is unchanged, the
  node re-registers ~16 s after the clone starts, `INTERNAL-IP` and the
  `k3s.io/internal-ip`/flannel `public-ip` annotations show the new IP, the PVC is Bound
  and the PV-bound pod runs again (~19 s), and all pods are Ready after ~50 s. The serving
  certificate already lists the new IP SAN at startup (k3s adds the node IP), next to the
  old one. Two kept-hostname k3s clones ran at once without interference. Caveat for any
  "wait for Ready" script: right after boot the Node still shows the *stale* `Ready`
  stored before the shutdown, so wait for pods (or `kubectl wait` on a fresh condition),
  not for the node status alone.

**What virt-runner offers:**

- `--keep-hostname` on `create --from-snapshot`. The clone's meta-data then carries the
  `guest_hostname` recorded at seal time. Only domain name, UUID, MAC, IP, machine-id and
  host keys differ.
- `--script` on clones, for post-clone fix-ups (for example
  `sudo systemctl restart k3s && kubectl wait node --all --for=condition=Ready`).
- README guidance with k3s as the example, covering both of the cases above.

The default stays **new hostname**. That is requirement 4, the `vm.dns_name =
<name>.default` contract holds, and it is correct for stateless guests. With
`--keep-hostname`, `vm.hostname` reports the kept name and `vm.dns_name` is `null`,
because the libvirt DNS name is the guest hostname and may be held by another clone.

### 4.4 CLI shape

| Option | For | Against |
|---|---|---|
| **`create NAME --from-snapshot SNAP`** | The envelope is `vm-create` by construction (R4), which is exactly what kubementat's `VMM.create` parses. `--ram/--vcpu/--disk/--mount/--script/--no-boot/--ssh-key` already exist. One pipeline with the same stages and codes. | `--distro/--release/--image/--user/--keep-going` become invalid together with the flag, which needs a usage check. |
| `clone SNAP NAME` | A clearer verb | Duplicates create's 10 options and envelope, or forces a shared refactor. Adds a second `tool` id the harness has to learn. |
| `snapshot restore` | — | Wrong meaning: this is not a restore. |

**Decision:** `create --from-snapshot`. Snapshot management goes in a click **group**
`snapshot` with subcommands `create`, `list`, `show`, `delete`. Each is a `JsonCommand`,
with tool ids `vm-snapshot-create`, `vm-snapshot-list`, `vm-snapshot-show` and
`vm-snapshot-delete` (`output.tool_name("snapshot-create")`). Alias: `snap`.

`virt-runner snapshot VM SNAP` (the bare form) is not offered. A group plus positional
arguments is ambiguous in click, and `snapshot create` reads unambiguously in scripts.

### 4.5 Snapshot store and metadata

| Option | For | Against |
|---|---|---|
| **Pool volume + pool sidecar JSON** | The hypervisor user can read it (needed for a backing file). Writable through `virsh` without sudo. One place to look, visible in `virsh vol-list`. Any libvirt-group user sees the same set, just as with `list`. | Reading metadata takes a `vol-download` per snapshot, which is cheap. |
| Volume in the pool, metadata in `~/.local/share/virt-runner/` | Simple file I/O | Split brain: metadata without a volume, or a volume nobody knows. It is per-user, while VMs are per-host. |
| Dedicated `vm-snapshots` pool | Clean separation | `setup` and preflight changes. Backing paths across pools work but complicate the dependents scan. |
| `~/vm-images/snapshots/` (host user dir) | Like the image cache | **libvirt-qemu cannot traverse `$HOME`** (`virtualizer.py:773-776`), so it cannot be a backing file. |

**Decision:** pool volume plus pool sidecar.

Naming: snapshot names follow the VM-name regex `^[A-Za-z][A-Za-z0-9._-]*$`
(`_validate_name`, `cmd_create.py:36`).

- Volumes: `<SNAP>.snapshot.qcow2` and `<SNAP>.snapshot.json`. They cannot collide with VM
  volumes (`<NAME>_vda.qcow2`, `<NAME>-cloudinit.iso`), because those suffixes differ.
- Snapshot and VM namespaces are separate: a VM and a snapshot may share a name.

## 5. Proposed CLI and `--json` documents

### 5.1 `virt-runner snapshot create VM SNAP`

```
virt-runner snapshot create VM SNAP [--start-source] [--shutdown-timeout SECONDS] [--json]
```

- **Preconditions (`preflight`):** libvirt reachable and the pool active. `VM` is defined
  (`vm-not-defined`) and owned (`not-a-virt-runner-vm`). `SNAP` does not exist
  (`snapshot-already-exists`).
- **Source state:**
  - If `VM` is **running**, it is sealed over SSH.
  - If it is **shut off**, virt-runner starts it, waits for its IP and SSH, then seals it.
    The document reports `source.started_for_seal: true`. An unsealed disk is never
    copied: it would produce clones without network.
- **Stages:**
  - `preflight`
  - `seal`: SSH plus the script; code `snapshot-seal-failed`. Reaching the guest uses the
    existing `no-ip`/`ssh-timeout` codes.
  - `shutdown`: `vm-shutdown-timeout`, default **300 s**. No forced power-off. (The
    first draft said 120 s. A sealed k3s guest with one workload pod took **91 s** to
    power off, §9 row 7, which leaves too little margin.)
  - `copy`: `snapshot-copy-failed`. A half-written volume is deleted.
  - `metadata`: `snapshot-metadata-failed`. The copied volume is deleted, because a
    snapshot without metadata is not listed.
  - `restart`: only with `--start-source`. A failure here is a **warning**; the snapshot
    exists.
- **Atomicity:** after a failure in `copy` or `metadata`, no snapshot volumes are left.
  After a failure in `seal`, the source is still running and possibly half-sealed. The
  message says to re-run or to destroy the VM.

Success document:

```json
{
  "tool": "vm-snapshot-create",
  "version": "0.8.0",
  "status": "success",
  "error": null,
  "snapshot": {
    "name": "km-installed",
    "pool": "vm-pool",
    "volume": "km-installed.snapshot.qcow2",
    "path": "/var/lib/libvirt/images/vms/km-installed.snapshot.qcow2",
    "virtual_gib": 30,
    "allocated_gib": 7.4,
    "created_at": "2026-10-28T21:14:03Z"
  },
  "source": {
    "name": "km-e2e-1",
    "uuid": "…",
    "was_running": true,
    "started_for_seal": false,
    "sealed": true,
    "state": "shut off"
  },
  "timings_s": {"seal": 4.1, "shutdown": 7.9, "copy": 31.6},
  "warnings": []
}
```

`timings_s` is optional. It is cheap and useful for the harness's budget logs, so it is
recommended.

### 5.2 `virt-runner snapshot list`

```json
{
  "tool": "vm-snapshot-list", "version": "…", "status": "success", "error": null,
  "pool": "vm-pool",
  "count": 1,
  "snapshots": [
    {"name": "km-installed", "created_at": "2026-10-28T21:14:03Z", "source_vm": "km-e2e-1",
     "distro": "ubuntu", "release": "resolute", "arch": "x86_64",
     "virtual_gib": 30, "allocated_gib": 7.4, "clones": ["km-clone-a", "km-clone-b"]}
  ]
}
```

- `clones` comes from the dependents scan (§5.5).
- A `*.snapshot.qcow2` without a readable sidecar is listed with
  `"metadata": null, "state": "incomplete"`, so it never becomes invisible garbage.
- Text mode prints one block per snapshot, in the style of `cmd_list._text_block`.

### 5.3 `virt-runner snapshot show SNAP`

This prints the whole metadata (§5.6) plus `clones`, `path` and `allocated_gib`. Error:
`snapshot-not-found`.

### 5.4 `virt-runner create NAME --from-snapshot SNAP`

```
virt-runner create NAME --from-snapshot SNAP [--keep-hostname] [--ram N] [--vcpu N] [--disk N]
                   [--ssh-key PUB] [--mount HOST[:GUEST]]... [--script FILE]... [--no-boot] [--json]
```

**Usage errors (exit 2, `usage`):**

- `--from-snapshot` together with `--distro`, `--release`, `--image`, `--user` or
  `--keep-going`. The user is the snapshot's. `--user` would only change which login
  virt-runner tries.
- `--keep-hostname` without `--from-snapshot`.
- `--disk` below the snapshot's `virtual_gib`. Code `disk-shrink-unsupported` exists
  already, but it is a runtime code; reuse it with exit 1 for consistency with `modify`.

**Defaults:**

- `--ram/--vcpu/--disk` default to the values recorded in the snapshot when it is given,
  and otherwise to the current 4/2/30.
- This means changing the click `default=` to `None` and resolving in the body. The help
  text keeps showing `[default: 4]` with "(snapshot: recorded)".

**Stages, the same names as today except one:**

- `preflight`: plus `snapshot-not-found` and `snapshot-incomplete`.
- `snapshot` **replaces `image`**. It resolves the snapshot and reads its metadata.
  Default code `snapshot-not-found`.
- `cloud-init`: a new seed. Meta-data has a new uuid4 and
  `local-hostname = NAME`, or the recorded `guest_hostname` with `--keep-hostname`.
  User-data is the unchanged `render_user_data` with the snapshot's user and sudo group.
- `create`: the overlay instead of `provision_volume`, then the same `create_vm`. Domain
  metadata gains `snapshot=<SNAP>`.
- `wait-ip`, `ssh-verify`, `script`: unchanged.

Success document. This is exactly today's `vm-create` shape plus the new keys
`vm.hostname`, `vm.snapshot` and the top-level `snapshot`. `image` is `null`.

```json
{
  "tool": "vm-create", "version": "0.8.0", "status": "success", "error": null,
  "booted": true,
  "vm": {
    "name": "km-clone-a", "uuid": "…", "state": "running",
    "distro": "ubuntu", "release": "resolute", "arch": "x86_64",
    "ram_gib": 8, "vcpu": 4, "disk_gib": 30, "mounts": [],
    "mac": "52:54:00:3a:9c:01", "ip": "192.168.122.57",
    "hostname": "km-e2e-1",
    "dns_name": null,
    "ssh_user": "ubuntu",
    "ssh_command": "ssh -i /home/u/.ssh/virt_runner_key ubuntu@192.168.122.57",
    "console_command": "virsh console km-clone-a",
    "teardown_command": "virt-runner destroy km-clone-a",
    "autostart": true,
    "snapshot": "km-installed"
  },
  "image": null,
  "snapshot": {"name": "km-installed", "volume": "km-installed.snapshot.qcow2",
               "source_vm": "km-e2e-1", "created_at": "2026-10-28T21:14:03Z",
               "keep_hostname": true},
  "scripts": []
}
```

On a normal create, `vm.hostname = name`, `vm.snapshot = null` and the top-level
`snapshot = null`. These are additive keys, so the change is MINOR. `harness/vmm.py`
reads `image.cache_hit` only for a log line, and `(None or {}).get(...)` already
tolerates `null`.

### 5.5 `virt-runner snapshot delete SNAP`

- Dependents scan: every volume in `vm-pool` whose `vol-dumpxml`
  `<backingStore><path>` is the snapshot's path. That covers shut-off clones and orphaned
  overlays too, not just running domains.
- If any are found, the delete is refused with code `snapshot-in-use` (exit 1). The
  document carries `"clones": [...]` and the message says to destroy them first. There is
  no `--force`: deleting a backing file corrupts every dependent disk.
- *Verified (§9 row 9):* libvirt itself does **not** refuse. `virsh vol-delete` of a
  backing volume succeeds while a clone runs on it; the running clone keeps reading the
  unlinked file, but its next start fails with `Cannot access backing file … (as
  uid:64055, gid:993): No such file or directory`. `vol-dumpxml` of the overlay still
  reports the dangling `<backingStore><path>` after `pool-refresh`, so the scan also finds
  orphans. `virt-runner destroy` of such a broken clone works. The refusal must therefore
  live entirely in virt-runner.
- Otherwise it runs `vol-delete` for both volumes. A sidecar that is already absent is a
  warning (mirroring destroy's D7).

```json
{"tool": "vm-snapshot-delete", "version": "…", "status": "success", "error": null,
 "name": "km-installed",
 "volumes": [{"name": "km-installed.snapshot.qcow2", "deleted": true, "already_absent": false},
             {"name": "km-installed.snapshot.json", "deleted": true, "already_absent": false}],
 "warnings": []}
```

### 5.6 Metadata sidecar (`<SNAP>.snapshot.json`, schema v1)

```json
{
  "schema": 1,
  "name": "km-installed",
  "created_at": "2026-10-28T21:14:03Z",
  "virt_runner_version": "0.8.0",
  "source": {"name": "km-e2e-1", "uuid": "…", "mac": "52:54:00:…"},
  "distro": "ubuntu", "release": "resolute", "arch": "x86_64",
  "os_variant": "ubuntu-lts-latest", "sudo_group": "sudo",
  "user": "ubuntu",
  "ssh_identity": "/home/u/.ssh/virt_runner_key",
  "guest_hostname": "km-e2e-1",
  "defaults": {"ram_gib": 8, "vcpu": 4, "disk_gib": 30},
  "virtual_gib": 30,
  "mounts": [{"source": "/home/u/dev/kubementat", "target": "/mnt/kubementat", "tag": "mount0"}],
  "parent": {"image_url": "https://cloud-images.ubuntu.com/…", "snapshot": null}
}
```

Notes on the fields:

- `distro`, `user` and `identity` come from the domain metadata (`domain_meta`).
- `release`, `mounts` and `image_url` are **not recorded today**. `set_domain_metadata`
  should gain `release`, `image_url` and a JSON `mounts` attribute at create time. For
  older VMs, they are `null`.
- `parent.snapshot` is set when the source was itself a clone. Then the provenance chain
  stays visible even though the disk is flattened.
- `mounts` is informational. Clones do **not** inherit mounts implicitly, because host
  paths move and the harness passes `--mount` explicitly.
- Stale mount lines are handled by the seal step, not left as a documented edge case. It
  strips virt-runner's `mountN … virtiofs` fstab lines, and the clone's first boot writes
  lines for its own `--mount` list only. A clone can therefore mount any host directory at
  any guest target, whatever the source VM had mounted (decision 2 in §7).

### 5.7 New error codes and stages (README "Error codes" table first)

| Code | Meaning |
|---|---|
| `snapshot-already-exists` | `snapshot create` with a name already used by a snapshot |
| `snapshot-not-found` | No snapshot of that name (`show`/`delete`/`create --from-snapshot`) |
| `snapshot-incomplete` | The disk volume exists but its metadata does not (or vice versa); delete and re-create it |
| `snapshot-seal-failed` | The in-guest seal script exited non-zero (the VM is kept running) |
| `vm-shutdown-timeout` | The guest did not power off within `--shutdown-timeout`; no forced power-off was done |
| `snapshot-copy-failed` | `virsh vol-create-from` failed (no partial volume left) |
| `snapshot-metadata-failed` | Writing the metadata sidecar failed (the copied volume was removed) |
| `snapshot-in-use` | `snapshot delete` while clone disks are backed by it |
| `overlay-create-failed` | `virsh vol-create-as --backing-vol` failed during `create --from-snapshot` |

New stage values:

- `snapshot create`: `preflight`, `seal`, `shutdown`, `copy`, `metadata`, `restart`.
- `create`: `snapshot`, which takes the place of `image`.

The `VirtError` docstring's stage list (`errors.py`) has to grow accordingly.

## 6. Implementation plan

The change is ordered so that each step can be reviewed and tested on its own. It goes on a
feature branch in a worktree off `develop` (`AGENTS.md`).

### 6.1 Code

1. **`virtualizer.py`: pure helpers** (unit-testable):
   - `snapshot_volume(snap)`, `snapshot_meta_volume(snap)`, next to `volume_name`.
   - `render_meta_data(id_val, name, hostname=None)`: the hostname defaults to `name`.
   - `_SEAL_SCRIPT` (constant, §4.2.3), beside `_FS_GROW_SCRIPT`.
   - `snapshot_volume_xml(name, capacity_bytes)` returns the `vol-create-from` XML with
     `<compat>1.1</compat>` and mode `0644`.
   - `backing_path_of(vol_xml_text)` parses `<backingStore><path>`.
   - `parse_snapshot_meta(text)` validates schema v1 and returns a frozen
     `SnapshotInfo` dataclass, in the same pattern as `VmInfo`/`TeardownResult`.
2. **`virtualizer.py`: `Virtualizer` methods.** Each one goes through
   `_run/_spawn/_quiet` and raises the §5.7 codes:
   - `seal_guest(user, ip, identity) -> str` (the guest hostname). SSH as in
     `grow_guest_filesystem`, with `ConnectTimeout` and `BatchMode`.
   - `shutdown_and_wait(name, timeout_s)`. Same poll as `reboot` (`:1923`), but **never**
     `destroy`.
   - `copy_to_snapshot(src_vol, snap) -> path`. `vol-create-from` with a temp XML file. On
     failure, `delete_volume_quiet`.
   - `write_snapshot_meta(snap, dict)` and `read_snapshot_meta(snap)`. Upload: copy
     `build_cloud_init_iso`'s `vol-create-as raw` plus `vol-upload`. Read: `vol-download`
     to a temp file.
   - `list_snapshots() -> list[SnapshotInfo]`. `virsh vol-list vm-pool` is filtered by
     suffix; `vol-info` gives the sizes.
   - `snapshot_dependents(snap) -> list[str]` runs `vol-dumpxml` on every pool volume. The
     `vol-dumpxml` `<backingStore>` content was verified on libvirt 10.0 (§9 rows 3, 9; it also lists dangling paths).
     The scan maps each overlay volume back to a domain via `<NAME>_vda.qcow2`.
   - `delete_snapshot(snap)`.
   - `create_overlay_volume(name, disk_gib, snap) -> vol`.
     `vol-create-as … --backing-vol <snap vol> --backing-vol-format qcow2`.
   - `set_domain_metadata(...)` gains optional `release`, `image_url`, `mounts` (JSON),
     `snapshot`. Old metadata without them stays valid.
3. **New `cmd_snapshot.py`.** A `click.Group("snapshot")` with four `@command` subcommands,
   each calling `output.set_json_mode`. Reuse `_validate_name` from `cmd_create` for SNAP.
   `snapshot create` follows the `create` pattern: a `run_state`, a `stage` variable, a
   `_DEFAULT_CODE` map, and a `_snapshot_document(run_state)` that is rendered on success
   and failure.
4. **`cmd_create.py`:**
   - Add `--from-snapshot` (value validated with the name regex) and `--keep-hostname`.
   - Move `--ram/--vcpu/--disk` to `default=None` with resolution in the body.
   - Add the usage checks from §5.4.
   - Branch the `image` stage: `stage = "snapshot"` reads the metadata. No image fetch; the
     effective `distro`, `release`, `user`, `sudo_group`, `os_variant` and identity come
     from it.
   - Branch the volume step: `create_overlay_volume` instead of `provision_volume`.
   - `_DEFAULT_CODE["snapshot"] = "snapshot-not-found"`.
   - `_create_document`: add `vm.hostname`, `vm.snapshot`, a top-level `snapshot`, and
     `image: null` when it came from a snapshot.
     - `dns_name` becomes `None` with `--keep-hostname`.
   - Text mode: a `Snapshot: <SNAP>` line.
   - `_record_metadata` passes the extra attributes (step 2).
5. **`cli.py`:** register the group (`main.add_command(cmd_snapshot, "snapshot")`), add the
   `"snap"` alias, and update the module docstring.
6. **`cmd_list.py`:** optional `snapshot` per VM from `vm.meta.get("snapshot")`. This is an
   additive key.
7. **`errors.py`:** update the docstring's stage list.
8. **Docs, as per the conventions:**
   - README:
     - a new "## Snapshots and clones" section, with the k3s caveat and the "the source is
       sealed" consequence
     - the error-code rows
     - JSON examples
     - the Quick start
   - `skills/virt-runner-cli/SKILL.md` and `references/json-and-errors.md`.
   - CHANGELOG `## [Unreleased]` / `### Added`. This is a MINOR bump to `0.8.0`.
   - A lessons-learned note
     `docs/lessons-learned/015-cloud-init-disabled-marker-blocks-clones.md` once verified.
     It covers the marker, the MAC-matched netplan, and the machine-id/DUID lease clash.

### 6.2 Unit tests (`tests/unit_test.py`: same style, plain `assert` in `main()`, no virsh, see lessons-learned 011)

- `render_meta_data(id, "c1", hostname="src")` → `local-hostname: src`. Without a hostname,
  today's output stays byte-identical, as a regression guard.
- `_SEAL_SCRIPT` contains `rm -f /etc/cloud/cloud-init.disabled`, `cloud-init clean --logs`
  the machine-id reset and the `mountN … virtiofs` fstab strip, prints the hostname with
  `uname -n`, and **does not** contain `ssh_host_` or a bare `hostname` command (Arch has
  none, §9).
- `snapshot_volume("a.b")` gives `"a.b.snapshot.qcow2"`. None of
  `{volume_name(n), cloud_init_iso_volume(n)}` ever ends in `.snapshot.qcow2` or
  `.snapshot.json`, for sample names.
- `snapshot_volume_xml` parses as XML and contains the capacity, `qcow2`, compat `1.1` and
  mode `0644`.
- `backing_path_of`: an overlay sample XML gives the path; a flat volume XML gives `None`;
  malformed input gives `None`.
- `parse_snapshot_meta`: a valid v1 parses. A missing `name`, a wrong `schema` or bad JSON
  each raise `snapshot-incomplete`.
- CLI usage via `click.testing.CliRunner`, as `modify_cli` does with a fake:
  - `create x --from-snapshot s --image u --json` gives `usage`, exit 2.
  - `--keep-hostname` without `--from-snapshot` gives `usage`.
  - `snapshot create 1bad s --json` gives `usage`.
- `_create_document` with a snapshot `run_state`: `image is None`, `vm.snapshot == "s"`,
  `dns_name is None` with keep-hostname, and every key of today's `vm` section still
  present.
- `snapshot create` and `delete` flow against a `FakeVz`-style double, following the
  existing `FakeVz` pattern:
  - a seal failure gives `snapshot-seal-failed` with stage `seal`, and copy is not called
  - a copy failure deletes nothing but the half volume
  - delete with dependents gives `snapshot-in-use` with `clones` listed and `vol-delete`
    not called

### 6.3 Integration test (`tests/integration_test.py`, real KVM, ubuntu, 1 GiB/1 vCPU/8 GiB as today)

1. `create it-snap-src --script mark.sh`. The script writes `~/marker`.
2. `snapshot create it-snap-src it-snap --json` succeeds, and `source.state == "shut off"`.
3. `destroy it-snap-src`. Then `snapshot show it-snap` still succeeds (R2).
4. `create it-clone-a --from-snapshot it-snap` and `create it-clone-b --from-snapshot it-snap`,
   both with `--json`. Check that:
   - Both are running, with distinct `vm.ip` and `mac`.
   - `cat ~/marker` works on both.
   - `/etc/machine-id` differs.
   - `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` differs.
   - `hostname` equals the clone name.
   - `test -f /etc/cloud/cloud-init.disabled` succeeds again.
5. `create it-clone-k --from-snapshot it-snap --keep-hostname --disk 10`. Check that:
   - `hostname` is `it-snap-src`.
   - `dns_name` is `null`.
   - `df` shows the root grown to ~10 GiB.
6. `snapshot list` reports `count == 1` and the 3 clones. `snapshot delete it-snap` gives
   `snapshot-in-use`.
7. Destroy all clones, then `snapshot delete it-snap` succeeds. `snapshot list` reports
   `count == 0`, and `virsh vol-list vm-pool` has no `it-snap.*`.
8. Negative: `create x --from-snapshot nope --json` gives `snapshot-not-found` with stage
   `snapshot`.

Add the names to `ALL` so the best-effort pre-clean destroys them. Also add a best-effort
`snapshot delete it-snap` at the start.

### 6.4 Manual and real-VM verification checklist (to resolve the *unverified* marks)

Results of the 2026-10-28 run are in §9; items marked [x] were done by hand-prototyping
the plan (the feature is not implemented), [ ] are still open.

- [x] On a current ubuntu VM, `/etc/netplan/50-cloud-init.yaml` contains
      `match: macaddress` and `set-name` (§2). cloud-init 26.1 (Ubuntu), 25.3 (Fedora),
      26.2 (Arch).
- [x] A clone created **without** the seal gets no IP: true on Arch; on Ubuntu 26.04 it
      gets an IP via dracut's catch-all network file but keeps every identity (§9 row 1).
- [x] Two simultaneous clones get distinct IPs, distinct DUID client-ids.
- [x] `vol-create-from` of an overlay (a snapshot of a clone) gives a flat volume
      (`qemu-img info --backing-chain`, no sudo needed thanks to mode `0644`).
- [x] `vol-dumpxml <clone>_vda.qcow2` shows `<backingStore><path>` on libvirt 10.0.
- [x] DAC/AppArmor: start clone A and clone B, destroy one, the other keeps running and
      doing I/O; owner `libvirt-qemu:kvm`, mode `0644`; clone C starts.
- [x] `modify --disk` on a clone works (overlay grown 10→14 GiB, guest fs grown).
- [x] A destroyed clone leaves the snapshot volume untouched (`vol-info`).
- [x] Restarting the sealed source: boots, SSH, same hostname, same IP, same machine-id,
      new host keys.
- [x] Arch: seal + two clones (after the `uname -n` fix).
- [x] Fedora 44: seal + two clones, `systemd.firstboot=off`, NM client-id `01:<MAC>`.
- [ ] aarch64 host (if available): a clone boots with fresh NVRAM. No such host.
- [x] k3s: provision, then `snapshot create`, then a clone with
      `--keep-hostname`. Check that:
      - the node is Ready in under 60 s and the Node name is unchanged
      - `INTERNAL-IP` is the new IP
      - no PVC is Pending
      - `curl -k https://<new-ip>:6443/` presents a cert with the new IP SAN after the
        first request
- [x] k3s: the same with a **new** hostname, to record the failure mode for the README
      (a second Node, the old one NotReady, the PV-bound *pod* Pending; the PVC stays Bound).
- [ ] Timings: snapshot (seal, shutdown, copy) and clone to SSH-ready, with 5 runs each.
      Single runs only so far (§9); repeat with the real implementation for the README.

## 7. Risks, open questions, kubementat usage

### Risks

- **Deleting a backing file corrupts its clones.** Mitigated by the dependents scan and by
  having no `--force`. Plain `virsh vol-delete` or `rm` bypasses it. The README should say
  so.
- **DAC relabeling on shared backing files.** libvirt relabels the image chain at domain
  start. Whether the backing image's ownership is restored at the *first* clone's stop
  while a second clone still reads it is the main concern. libvirt is designed not to
  restore read-only or shared chain members. *Verified on 10.0 (§9 row 3):* the snapshot
  becomes `libvirt-qemu:kvm 0644` at the first clone start and stays so after clones stop;
  the remaining clone kept running and a new clone started. No AppArmor denials.
- **The source is mutated by the seal** (§4.2.4). Mitigated by documentation,
  `source.sealed: true`, and the later `--no-touch-source` sealer-domain option.
- **The seal is not a full sysprep.** It does not clear shell history, application
  secrets, or the authorized_keys of the snapshot's key. Clones of one snapshot share
  every app-level secret (k3s CA, tokens, kubementat secrets) by design. Do not share
  snapshots across trust boundaries.
- **Pool space.** Each snapshot is a full, **uncompressed** copy of the allocated data:
  a fresh Ubuntu VM (0.86 GiB, mostly compressed cloud-image clusters) gave a 2.12 GiB
  snapshot, a k3s VM 3.25 GiB. kubementat-sized snapshots run about 5-15 GB (*estimate*).
  The host's pool (ext4 on `/`) had only 37-41 GB free during verification. `snapshot
  list` reports `allocated_gib`.
- **Shutdown time of loaded guests.** An idle guest powered off in 2-4 s; a k3s guest with
  one workload pod took 91 s (systemd stop timeouts of containers). Hence the 300 s
  default in §5.1. A caller in a hurry can stop the app before `snapshot create` (for k3s,
  `k3s-killall.sh`) — but that is app-specific and left to `--script`/the caller.
- **Distro tool assumptions in the seal.** The first draft called `hostname`, which Arch
  lacks; the seal then fails before changing anything (fine: `snapshot-seal-failed`, not
  a half seal). Keep the script to coreutils, `sed`, `cloud-init`, and test it per distro.
- **A cloud-init failure on a clone** (for example a newer image that changes fallback
  networking) shows up as `lease-timeout`. The `virsh console` hint already exists in that
  message.
- **The fstab line for a reused mount tag** pointing at a different target (§5.6). *Resolved
  by the seal's fstab strip; verified (§9 row 5):* a clone of a source with `mount0 →
  /mnt/a` got only its own `mount0 → /mnt/b`, no failed units. Only the empty `/mnt/a`
  directory stays.
- **Fedora lease hostname.** NM's first DHCP request on a Fedora clone carries the
  source's hostname, so dnsmasq may register the *source* name for a clone (§4.2.3). A
  candidate fix is `truncate -s 0 /etc/hostname` in the seal (*untested*). Since
  `dns_name` never resolved on this host anyway (decision 4 note), it is low priority.

### Open questions

1. **Decided (2026-10-28):** `snapshot create` leaves the source **shut off**. This must be
   communicated, not just documented in code. The human output of `snapshot create` prints
   a notice that the source is sealed and shut off, and that starting it again runs
   first-boot setup again: new SSH host keys (*verified, §9 row 4: the machine-id and the
   IP did not change on KVM, so the notice should say only "new SSH host keys" and that
   cloud-init runs again*). The
   JSON envelope carries `"source": {"state": "shut off", "sealed": true}`, and the README
   snapshot section states the same thing.
2. **Decided (2026-10-28):** clones get only the mounts passed as `--mount` at clone time.
   They never inherit the snapshot's recorded mounts. A snapshot made with one host
   directory mounted must be usable with a completely different host directory. To make
   that robust and not just documented (see §5.6), the seal step removes virt-runner's own
   virtiofs fstab lines (`mountN … virtiofs`), which are safe to remove because virt-runner
   tagged and wrote them. Because the sealed clone re-runs cloud-init on first boot, the
   clone's `bootcmd` then writes fresh fstab lines for exactly the clone's `--mount` list.
   The recorded `mounts` in the metadata stay informational only.
3. Should virt-runner add `--node-name`-style hints for k3s? The proposal says no: that is
   app-specific, and `--keep-hostname` plus `--script` cover it.
4. **Decided (2026-10-28):** with `--keep-hostname`, `dns_name` is `null`. The libvirt DNS
   name follows the guest hostname, so `<clone>.default` does not resolve, and the kept
   `<hostname>.default` may resolve to another clone with the same hostname. An absent
   value fails visibly, while either name would fail silently, so callers must use `ip`.
   The README states this next to `--keep-hostname`. Clones without the flag keep
   `<name>.default` as today. *Verified (§9 row 6):* with two kept-hostname clones the
   hostname resolves (`dig @192.168.122.1`, nss `libvirt`) to the **last** clone that took
   a lease, and the earlier clone's lease loses the name. Two further findings: on this
   host `<anything>.default` resolves for **no** VM, clone or not (the `default` network
   has no `<domain>` element; existing behavior, not snapshot-specific), and the nss
   `libvirt_guest` module resolves the *domain* name (`getent hosts <clone>`) correctly
   for kept-hostname clones. The decision stands.
5. Should there be an optional `--sysprep guestfs` for users who want an untouched source
   and have libguestfs? Defer it until someone asks.
6. ~~Should `--reflink` be part of v1?~~ **Answered (§9 row 11):** no. The pool is on ext4
   (`df -T`), and libvirt 10.0 rejects `--reflink` for qcow2 targets on any filesystem.

### How kubementat's E2E harness would use it (virt-runner side only)

```bash
# once: after stage 02-install succeeded on VM km-e2e-1 (~9 min total)
virt-runner snapshot create km-e2e-1 km-installed --json        # seal + shutdown + copy (~1 min)
virt-runner destroy km-e2e-1 --json

# golden base variant: same call made right after 01-provision tooling, before k3s starts
# (e.g. a provision script split at the k3s block), named e.g. km-tooling

# per feature test (seconds to SSH, then k3s ~tens of seconds):
virt-runner create km-feat-1 --from-snapshot km-installed --keep-hostname \
  --ram 8 --vcpu 4 --mount "$REPO:/mnt/kubementat" --script resync-and-wait.sh --json
#   -> envelope: vm.ip, vm.ssh_command (-i …), scripts[0].exit_code, as today
virt-runner destroy km-feat-1 --json

virt-runner snapshot list --json                                 # reuse if present
virt-runner snapshot delete km-installed --json                  # when the tree/config changed
```

On the harness side (out of scope here, noted only):

- `VMM.create` gains an optional `from_snapshot`/`keep_hostname` and stops passing
  `--distro/--release` in that case.
- `stage_provision`'s `image` state tolerates `image: null`.
- The `resync-and-wait.sh` clone script re-copies the mounted tree into `@GUEST_TREE@` and
  waits for exactly one Ready node.
- known_hosts is kept per VM, because DHCP IPs get reused between clones.

## 9. Real-VM verification results (2026-10-28)

Method: the feature is not implemented, so every step was prototyped by hand on the dev
host the way the plan describes it. Sources were made with `virt-runner create` (0.7.0,
from the repo). The seal ran over SSH with `sudo -n bash -s`. Snapshots were made with
`virsh vol-create-from` and the §4.1.5 XML, clone disks with `virsh vol-create-as
--backing-vol`. Clone seeds came from virt-runner's own `generate_cloud_init_files`,
`render_meta_data` (new uuid4, new or kept hostname) and `build_cloud_init_iso`, and
clones were defined by `Virtualizer.create_vm` (virt-install, new name, new MAC), via
`uv run python`. Everything ran as the unprivileged `libvirt`-group user against
`qemu:///system`. **Nothing needed sudo** (and none was available non-interactively).
Host: libvirt 10.0.0, qemu-img 8.2.2, pool `vm-pool` on **ext4** (`/`, 95-96 % used, 37-41 GB
free). Guests: Ubuntu 26.04 (cloud-init 26.1), Fedora 44 (25.3), Arch latest (26.2);
1 vCPU/1-2 GiB, k3s guests 2 vCPU/4 GiB. Timings are single runs, with a kubementat E2E
VM running in parallel. "t_ssh" is from the `vol-create-as` of the overlay to the first
successful SSH round-trip.

| # | Assumption | Test performed | Result | Evidence |
|---|---|---|---|---|
| 1 | Without the seal, a disk copy with a new MAC gets no network and ignores the new seed (§2, §4.2.2) | Shut off an Ubuntu source, `vol-create-from` it, booted two overlays with new MACs and new seeds (new hostname); same for Arch (whose seal had failed, row 10) | **Partially confirmed** | Ubuntu 26.04: netplan has `match: macaddress: "52:54:00:38:c3:6a"` + `set-name: enp2s0`, but the clone still got an IP via `/run/systemd/network/zzzz-dracut-default.network` (`DHCP=yes`, `ClientIdentifier=mac`) on `enp1s0`. It kept hostname `snapverify-src`, the source machine-id and host key, `cloud-init.disabled` present, the new seed was ignored, `mnt-a.mount` failed. Two such clones at once got distinct IPs (MAC client-id). Arch: `10-cloud-init-eth0.network` `[Match] MACAddress=<old>` → both clones `no DHCP lease after 180s` |
| 2 | With the seal, two clones of one snapshot boot simultaneously with distinct IP, machine-id, host keys, the seed's hostname, and SSH via the injected key | Sealed Ubuntu source, snapshot, two clones started in parallel (one with `--mount`, one with a 12 GiB disk) | **Confirmed** | IPs .171/.94; machine-ids `c6d735b1…`/`e23df579…` (= each domain's UUID); ed25519 `P5eg8…`/`15RgX…`; `hostname` = clone name, `/etc/hosts` `127.0.1.1 <clone>`; netplan re-rendered for the new MAC; leases with distinct DUID client-ids and the clone hostnames; `cloud-init.disabled` re-created; `~/marker` from the source present; growpart: 11 G root with 12 GiB disk. t_ssh 14.1 s / 13.4 s (parallel), later single clones 11.3-12.2 s. Seal 0.85 s, shutdown 4.1 s, copy 1.4 s |
| 3 | A snapshot shared as backing file by two running clones works under DAC/AppArmor | Two clones running on one snapshot; destroyed one; I/O on the other; started a third | **Confirmed** | Snapshot relabeled `-rw-r--r-- libvirt-qemu kvm`, stays so after clones stop; `/etc/apparmor.d/libvirt/*.files`: `"…/snapverify-s1c.snapshot.qcow2" rk`; 0 `apparmor="DENIED"` in `journalctl -k`; after destroying clone B, clone A re-read `/usr` (caches dropped) and wrote 50 MB without I/O errors; clone C t_ssh 11.9 s |
| 4 | A restarted sealed source re-runs first boot: new host keys, new machine-id, maybe new IP (decision 1) | `virsh start` of the sealed, shut-off Ubuntu source | **Partially confirmed** | cloud-init ran again (`cloud-init.disabled` re-created, fstab `mount0 /mnt/a` re-added, `/mnt/a` mounted), new ed25519 key (`HlLj…` → `vDOo…`), hostname kept, but **same machine-id** `55b1d8c4…` (from the unchanged SMBIOS UUID) and **same IP** .135 |
| 5 | Mounts (decision 2): the seal strips the fstab virtiofs lines; a clone mounts only its own directory B at its own target | Source with `--mount dirA:/mnt/a`; clone with `dirB:/mnt/b` (same tag `mount0`) and one without mounts; snapshot of that clone (row 8) | **Confirmed** | After seal `/etc/fstab` had only the 3 image lines. Clone: fstab `mount0 /mnt/b virtiofs …`, `mount0 on /mnt/b type virtiofs`, `b.txt` visible, `systemctl --failed` empty; clone without mounts: no virtiofs line, no failed unit. Only an empty `/mnt/a` directory stays |
| 6 | `--keep-hostname` and `dns_name` (decision 4): the kept hostname's DNS name goes to the first clone; `<clone>.default` does not resolve | Two clones with kept hostname `snapverify-src` (ck1 .224, then ck2 .199), queried `dig @192.168.122.1` and `getent hosts` (nsswitch: `libvirt libvirt_guest`) | **Partially refuted** | `snapverify-src` → **.199 (the last clone)**; ck1's lease has no hostname. `getent hosts snapverify-ck1` → .224 (nss `libvirt_guest`, by domain name). `<name>.default` resolved for **no VM at all**, incl. normal ones (`dig … snapverify-ca.default` empty; network XML has no `<domain>`). Decision 4 (`dns_name: null`) still right |
| 7 | k3s on a kept-hostname clone: node Ready under the new IP, certs OK, no old NotReady node; new hostname: second node, old NotReady, PVC pinned | k3s v1.36.5+k3s1 (`--write-kubeconfig-mode 644`), a busybox Deployment on a 100 Mi local-path PVC; sealed, snapshot (3.25 GiB, copy 1.7 s); clones kk1, kk2 (kept, simultaneously) and kn1 (new hostname) | **Confirmed, one correction** | Seal 0.97 s, **shutdown 91 s**. Kept: one Node `snapverify-k3s`, `INTERNAL-IP` 192.168.122.227 resp. .85, annotations `k3s.io/internal-ip` and flannel `public-ip` updated; PVC Bound, writer pod Running; serving cert SANs `IP:192.168.122.227, IP:192.168.122.7` (new IP present from startup, old kept). Timeline kk2: boot +1 s, `Successfully registered node` +16 s, pods Ready +18…+49 s (writer +19 s, metrics-server last). Node status reads `Ready` from the first API answer (+13 s) because the stored status is stale. New hostname: nodes `snapverify-k3s NotReady` (after ~1 min) + `snapverify-kn1 Ready`, a second `*.node-password.k3s` secret; after ~5 min pods evicted, new writer pod **Pending** ("didn't match PersistentVolume's node affinity"), **PVC still Bound** |
| 8 | A snapshot of a clone (overlay with backing chain) is flattened by `vol-create-from` and can be cloned | Sealed clone A, `vol-create-from` its overlay, cloned it | **Confirmed** | `qemu-img info --backing-chain` of the new snapshot: one image, no backing file, 2.12 GiB, no `<backingStore>` in `vol-dumpxml`. Clone had both markers, fstab without the old `mount0 /mnt/b` line, t_ssh 11.3 s. Also `vol-clone` of the overlay flattens (2.28 GB) but writes `root:root 0600` |
| 9 | Destroying a clone leaves the snapshot intact; deleting a snapshot under a clone is what `snapshot-in-use` must prevent | `virt-runner destroy` clones; `virsh vol-delete` of a snapshot under a running clone; restart that clone | **Confirmed** | After destroys: `vol-info` unchanged (2.12 GiB). `vol-delete` of the backing volume: `Vol … deleted`, no refusal. Running clone kept working (reads after cache drop OK). `vol-dumpxml` of the overlay still shows the dangling `<backingStore><path>`. `virsh start`: `Cannot access backing file '/var/lib/libvirt/images/vms/snapverify-s2.snapshot.qcow2' … (as uid:64055, gid:993): No such file or directory`. `virt-runner destroy` of it: success |
| 10 | The seal is distro-neutral; Fedora (NM) and Arch clones get network and distinct IPs; nothing blocks the console | Same as row 2 with `--distro fedora` and `--distro arch` | **Fedora confirmed; Arch confirmed after a seal fix; aarch64 not tested** | Arch: original seal `hostname: command not found`, exit 127, nothing sealed → fixed to `uname -n`, then exit 0. Arch clones: .185/.241, distinct machine-ids/keys/DUIDs, hostnames set, t_ssh ~50 s (sshd ordered after `systemd-time-wait-sync`, 39 s). Fedora clones: .120/.209, distinct machine-ids/keys, client-id `01:<MAC>`, t_ssh 19.7 s, `systemd.firstboot=off` on the cmdline, `is-system-running: running`; the source's old-MAC NM keyfile remains (inert); one clone's lease got the **source** hostname. aarch64: host is x86_64 |
| 11 | `--reflink` speeds up the copy on btrfs/XFS and falls back on ext4 (open question 6) | `df -T` of the pool; `vol-create-from … --reflink` | **Refuted** | Pool: `/dev/nvme0n1p8 ext4`. `error: unsupported flags (0x2) in function storageBackendCreateQemuImg` (libvirt's qemu-img backend never supports it for qcow2), no partial volume left |
| 12 | `vol-create-from` with the §4.1.5 XML gives a readable, modern qcow2 | `qemu-img info` on the result (no sudo) | **Partially refuted** | Readable without sudo (0644), but `compat: 0.10` (qcow2 v2) unless `<compat>1.1</compat>` is in the XML; with it, `compat: 1.1`. Source volumes are `compat 1.1`. Copy is uncompressed (0.86 → 2.12 GiB) |
| 13 | `modify --disk` works on a clone (overlay) | `virt-runner modify <clone> --disk 14` on a running clone | **Confirmed** | `changes: disk_gib 10→14, fs_grown: true`, `vol-info` capacity 14 GiB, guest `/` 13 G; backingStore unchanged |
| 14 | qemu-guest-agent absent; Ubuntu has no `sshd-keygen` fallback (§4.1.3, §4.2.3) | `dpkg -l`, `systemctl cat` on Ubuntu 26.04 | **Confirmed / refuted** | No `qemu-guest-agent` package. But `sshd-keygen.service` exists and is enabled (`ConditionFirstBoot=yes`, `ExecStart=ssh-keygen -A`) |

Not tested: aarch64 (no host), five-run timing statistics (single runs only),
`--start-source` as a CLI flag (only the equivalent `virsh start`), and the untested
`truncate -s 0 /etc/hostname` idea for Fedora lease names (§7). All `snapverify-*` VMs and
volumes were destroyed afterwards; `virt-runner list` and `virsh vol-list vm-pool` show
none.

## 8. Sources

- cloud-init CLI (`clean --logs/--machine-id/--configs/--seed`; "best practice when cloning a golden image"): https://docs.cloud-init.io/en/latest/reference/cli.html
- cloud-init first boot / instance-id change: https://docs.cloud-init.io/en/latest/explanation/first_boot.html
- cloud-init disabling (`/etc/cloud/cloud-init.disabled`, `cloud-init=disabled`): https://docs.cloud-init.io/en/latest/howto/disable_cloud_init.html
- cloud-init network config v2 (`macaddress` required for rename): https://docs.cloud-init.io/en/25.3/_sources/reference/network-config-format-v2.rst.txt
- Template clones without network / `cloud-init clean` before templating (XCP-ng forum): https://xcp-ng.org/forum/post/100021
- Proxmox: cloud-init with golden/base templates: https://forum.proxmox.com/threads/cloudinit-issues-with-golden-and-base-templates.174306/
- Duplicate machine-id leads to the same DHCP IP (networkd DUID, dnsmasq prefers client-id): https://gitlab.archlinux.org/archlinux/arch-boxes/-/issues/25 , https://knowledge.broadcom.com/external/article?legacyId=82229 , https://forum.proxmox.com/goto/post?id=868716 , https://github.com/canonical/cloud-init/issues/4066
- libvirt: forbid internal snapshots with pflash firmware: https://lists.libvirt.org/archives/list/devel@lists.libvirt.org/thread/3DEE3C4IORBAN64ZJ24LK3673KWINY6Z/ ; Arch regression report: https://bugs.archlinux.org/task/78136
- libvirt snapshot XML format (disk-only vs full system, crash-consistency): https://libvirt.org/formatsnapshot.html
- libvirt virtiofs kbase (migration/save/memory snapshots; virtiofs content not snapshotted): https://libvirt.org/kbase/virtiofs.html ; "migration with virtiofs device is not supported": https://lists.libvirt.org/archives/list/users@lists.libvirt.org/thread/GIWW4RTCYTEUZWHWO2W23HZWCSF6AP7H/
- k3s architecture (node password, `/etc/rancher/node/password`, `--with-node-id`): https://docs.k3s.io/architecture
- k3s server CLI (`--node-name`, `--node-ip`, `--tls-san`): https://docs.k3s.io/cli/server
- k3s DynamicListener adds SANs on demand (Brad Davidson, quoted): https://dev.to/algo7/k3s-update-tls-sans-2kg3 ; related: https://github.com/k3s-io/k3s/issues/1381
- k3s cloned VM rejected for a duplicate node name/password: https://lxz.is-a.dev/2023/02/01/solved-duplicate-hostname-or-contents/
- k3s IP change failures (pinned node-ip; etcd): https://slack-archive.rancher.com/t/30188211/cggqehppw-k3s-single-node-fails-to-start-networking-after-ip , https://forums.suse.com/t/k3s-w-etcd-fails-to-restart-after-ip-change/42496
- Local verification on this host (2026-10-28): `virsh --version` 10.0.0, `qemu-img` 8.2.2, `virsh pool-dumpxml vm-pool` (dir pool, `/var/lib/libvirt/images/vms`, root 0755), `virsh help vol-create-as|vol-clone|vol-create-from|vol-download` (the `--backing-vol`, `--reflink` and `--inputpool` options exist).
- virt-runner source at `e9c97f4` (file:line references in §2); kubementat `tests/e2e/harness/vmm.py`, `harness/stages.py`, `harness/guest.py`, `stages/01-provision.sh`.
