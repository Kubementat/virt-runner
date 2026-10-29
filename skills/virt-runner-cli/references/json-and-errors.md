# virt-runner JSON contract and error codes

Source of truth: `README.md` ("JSON output"), `src/virt_runner/output.py`, `cmd_*.py`.

## Envelope (all commands)

```json
{
  "tool": "vm-create | vm-destroy | vm-list | vm-modify | vm-ssh",
  "version": "<package version, 0.0.0+unknown from a bare source tree>",
  "status": "success | error",
  "error": null | {"code": "<stable code>", "message": "<text>", "stage": "<create stage, optional>"},
  "...command sections...": "..."
}
```

- stdout carries exactly one document (pretty-printed, 2-space indent) on success **and** failure.
- Usage errors (bad flag, bad name/user/mount, `--script` + `--no-boot`, `--distro arch --release X`,
  missing `--script` file) → `code: "usage"`, exit 2 — provided `--json` is spelled correctly
  on the command line.
- Error documents still carry the sections known so far (e.g. `vm`, `image`), so a late failure
  tells you what real resources exist.
- `VM_JSON_TRACE=1` re-sends progress/warning lines to stderr.

## create (`vm-create`)

```json
{
  "tool": "vm-create", "status": "success", "error": null,
  "booted": true,
  "vm": {
    "name": "dev", "uuid": "…", "state": "running",
    "distro": "ubuntu", "release": "resolute", "arch": "x86_64",
    "ram_gib": 4, "vcpu": 2, "disk_gib": 30,
    "mounts": [{"source": "/home/u/src/app", "target": "/work", "tag": "mount0"}],
    "mac": "52:54:00:…", "ip": "192.168.122.57", "dns_name": "dev.default",
    "ssh_user": "ubuntu",
    "ssh_command": "ssh -i /home/u/.ssh/virt_runner_key ubuntu@192.168.122.57",
    "console_command": "virsh console dev",
    "teardown_command": "virt-runner destroy dev",
    "autostart": true,
    "hostname": "dev",
    "snapshot": null
  },
  "image": {
    "source_url": "https://cloud-images.ubuntu.com/resolute/current/…img",
    "cache_path": "/home/u/vm-images/resolute/…img",
    "cache_hit": true, "downloaded": false,
    "verification": "sha256-sums | skipped", "verified": true
  },
  "snapshot": null,
  "scripts": [{"path": "/abs/setup.sh", "exit_code": 0}]
}
```

With `--from-snapshot SNAP`: `image` is `null`, `vm.snapshot` is `"SNAP"` and the top-level
`snapshot` is `{name, volume, source_vm, created_at, keep_hostname}`. With `--keep-hostname`,
`vm.hostname` is the snapshot's guest hostname and `vm.dns_name` is `null`.

- `booted` is always present (false on `--no-boot` and early failures).
- `vm` appears once the domain exists; `image` once the image stage finished; `scripts` always
  (empty list when none; on failure it includes the failing entry).
- `ssh_command` is `null` when there is no IP (e.g. `--no-boot`).

Presence of `vm` in an error document ⇒ the domain exists ⇒ you must `destroy` it.

| Stage | Default code | VM left behind? |
|---|---|---|
| `preflight` | `libvirt-unreachable` (or specific: `pool-*`, `network-not-active`, `unsupported-arch`, `vm-already-defined`, `virtiofsd-missing`, `ssh-key-missing`) | no |
| `image` | `image-download-failed` / `image-verification-failed` / `image-no-cache` | no |
| `cloud-init` | `cloud-init-failed` | no |
| `create` | `vm-create-failed` (domain + volume removed), `volume-*-failed` | no |
| `wait-ip` | `lease-timeout` | **yes** |
| `ssh-verify` | `ssh-timeout` | **yes** |
| `script` | `script-failed` | **yes, running** |
| `snapshot` (only `--from-snapshot`, replaces `image`) | `snapshot-not-found` / `snapshot-incomplete` / `disk-shrink-unsupported` | no |

`create --from-snapshot` fails in `create` with `overlay-create-failed` when the overlay disk
cannot be made (no VM left).

## snapshot create (`vm-snapshot-create`)

```json
{
  "tool": "vm-snapshot-create", "status": "success", "error": null,
  "snapshot": {"name": "golden", "pool": "vm-pool", "volume": "golden.snapshot.qcow2",
               "path": "/var/lib/libvirt/images/vms/golden.snapshot.qcow2",
               "virtual_gib": 30, "allocated_gib": 2.12, "created_at": "2026-10-28T21:14:03Z"},
  "source": {"name": "dev", "uuid": "…", "was_running": true, "started_for_seal": false,
             "sealed": true, "state": "shut off"},
  "timings_s": {"seal": 0.9, "shutdown": 3.1, "copy": 1.5},
  "warnings": []
}
```

`snapshot` is `null` until the snapshot exists; `source` is present once the VM was found.

| Stage | Code | State afterwards |
|---|---|---|
| `preflight` | `vm-not-defined`, `not-a-virt-runner-vm`, `snapshot-already-exists`, `pool-*` | nothing changed |
| `seal` | `snapshot-seal-failed`, `lease-timeout`, `ssh-timeout` | source running, possibly half-sealed: re-run or destroy it |
| `shutdown` | `vm-shutdown-timeout` | source running and sealed; stop its workload, re-run with a larger `--shutdown-timeout` |
| `copy` | `snapshot-copy-failed` | source shut off; no snapshot volume |
| `metadata` | `snapshot-metadata-failed` | source shut off; no snapshot volume |
| `restart` | (warning only) | snapshot exists; `virsh start VM` |

## snapshot list / show / delete

- `vm-snapshot-list`: `{pool, count, snapshots: [{name, created_at, source_vm, distro, release,
  arch, virtual_gib, allocated_gib, clones}]}`. Incomplete snapshots: `{name, metadata: null,
  state: "incomplete", virtual_gib, allocated_gib, clones}` — delete them.
- `vm-snapshot-show`: `{snapshot: {<schema-1 metadata>, volume, path, allocated_gib, clones}}`.
  The metadata carries `user`, `guest_hostname`, `defaults {ram_gib, vcpu, disk_gib}`,
  `source {name, uuid, mac}`, `mounts` (informational) and `parent {image_url, snapshot}`.
- `vm-snapshot-delete`: `{name, volumes: [{name, deleted, already_absent}], warnings}`; refused
  with `snapshot-in-use` (exit 1, `clones: [...]` in the document) while any clone disk is backed
  by it.

## list (`vm-list`)

```json
{
  "tool": "vm-list", "status": "success", "error": null,
  "pool": "vm-pool", "user_override": null, "count": 1,
  "vms": [{
    "name": "dev", "uuid": "…", "state": "running", "mac": "52:54:00:…",
    "ip": "192.168.122.57", "ip_status": "lease | not-running | running-no-lease | no-mac",
    "vcpu": 2, "ram_gib": 3, "disk_gib": 22,
    "ssh_user": "ubuntu",
    "ssh_command": "ssh -i … ubuntu@192.168.122.57",
    "console_command": "virsh console dev",
    "teardown_command": "virt-runner destroy dev"
  }]
}
```

`ip`, `ssh_user`, `ssh_command` are `null` unless `ip_status == "lease"`; `vcpu`/`ram_gib`/`disk_gib`
are `null` when the size could not be read. The three sizes come from the **live** XML (plus the
pool volume for the disk), i.e. what the guest runs — a change that is only in the config reads
back as the old value until someone reboots, and `virsh dumpxml --inactive NAME` is what the next
boot gets. `list` needs libvirt
and a *defined* (not necessarily active) `vm-pool`; failures: `libvirt-unreachable`, `pool-not-found`.

## ssh (`vm-ssh`)

Success (after the interactive session ends):
```json
{"tool": "vm-ssh", "status": "success", "error": null,
 "name": "dev", "ip": "…", "ssh_user": "ubuntu", "exit_code": 0,
 "ssh_command": "…", "console_command": "…", "teardown_command": "…"}
```
The process exits with ssh's exit code when non-zero. Errors carry `name` (and `state` for
`vm-not-running`).

## destroy (`vm-destroy`)

```json
{"tool": "vm-destroy", "status": "success", "error": null,
 "name": "dev",
 "domain": {"was_running": true, "destroyed": true, "undefined": true},
 "volume": {"name": "dev_vda.qcow2", "pool": "vm-pool", "deleted": true, "already_absent": false},
 "warnings": []}
```

## modify (`vm-modify`)

```json
{"tool": "vm-modify", "status": "success", "error": null,
 "vm": {"name": "dev", "uuid": "…", "state": "running", "vcpu": 2, "ram_gib": 2, "disk_gib": 8,
        "interfaces": [{"type": "network", "source": "default", "mac": "52:54:00:…"}]},
 "changes": [{"field": "disk_gib", "from": 8, "to": 16, "effective": "now", "fs_grown": true},
             {"field": "vcpu", "from": 2, "to": 4, "effective": "on-boot"},
             {"field": "mac", "from": "52:54:00:…", "to": "52:54:00:…", "effective": "now"},
             {"field": "interface", "from": null, "to": {"type": "network", "source": "mgmt",
              "mac": "52:54:00:…"}, "effective": "now"}],
 "rebooted": false, "planned": false, "ip": "192.168.122.57", "warnings": [
   "vCPU change applies on next boot — consider --reboot"]}
```

- `vm` is the state **before** the change (in a `--plan`, the state as-is). `ram_gib` / `vcpu` /
  `disk_gib` are what the tool read from libvirt — the guest's own numbers can differ from them,
  which is why `effective` exists. For a running domain they are the *live* numbers; the config
  they came from can say something else, and that is what `from` exposes.
- A sizing request is compared to the **persisted config** (`virsh dumpxml --inactive`), not to
  the running value. So `from` is the config's number whenever that is the side that disagrees
  (`{"from": 4, "to": 1, "effective": "on-boot"}` = “the guest runs 1 GiB, its config reserves
  4 GiB, that is being corrected”). When the config already matches and only the guest is behind,
  the entry is a change with `effective: "on-boot"` and **no libvirt call** — rebooting is all it
  needs — and `modify-noop` fires only when both sides match.
- `changes[].field`: `vcpu`, `ram_gib`, `disk_gib`, `interface` (add or remove; `from`/`to` carry
  the NIC document, `null` on the absent side), `network` (the primary NIC's new source), `mac`.
  An added NIC always carries the MAC that was set: give `,mac=` or take the one the tool
  generated, and `--net-remove` that value. No `virsh domiflist` round trip is needed to learn it.
- `changes[].effective`: `"now"` (running guest changed) or `"on-boot"` (config only, applied at
  the next boot). `disk_gib` also carries `fs_grown`: `true` grown, `false` attempted and refused
  by the guest (block device stays grown), `null` not attempted (`--no-auto-grow-fs`, or a
  powered-off guest — retry after boot by re-issuing the same `--disk`).
- `changes[].live_refused` appears on an entry whose live attempt libvirt turned down, with
  libvirt's own words ("failed to find appropriate hotpluggable vcpus", …). The same text is in
  the matching warning. It explains *why* an entry is `on-boot` despite `--live`; it is not an
  error and the change is still applied to the config.
- `planned` is `true` for `--plan`; `rebooted` says whether the guest was power-cycled; `ip` is
  the current address, or the freshly waited one with `--reboot --wait`.
- `warnings[]` is where every "only on next boot" and "do this by hand inside the guest" note
  lands — always show it, never assume an empty `error` means a clean change.
- An **error document still lists the changes** the call attempted, with
  `"applied": false` on the ones that never ran (`applied` is absent everywhere
  else — including in a `--plan`, where nothing is supposed to run). So
  `status: "error"` does **not** mean "nothing happened": check
  `virsh domiflist`, `virsh dumpxml --inactive` and `virsh vol-info` before you
  conclude the VM is untouched.

```bash
# what actually changed, and what is still pending
virt-runner modify dev --json | jq '{changes:[.changes[]|{field,effective}], warnings}'
# read back the running state you ended up with
virt-runner list --json | jq '.vms[] | select(.name=="dev") | {state, mac, ip}'
# the config the next boot uses (may differ from the numbers above)
virsh -c qemu:///system dumpxml --inactive dev | grep -E '<memory|<currentMemory|<vcpu'
```

`modify --ram` fails with `runtime-error` before touching libvirt when the guest would be bigger
than the host's installed RAM — the config would have been accepted and the domain would not
start. Nothing is changed in that case.

## Exit codes

`0` success · `1` runtime or preflight failure · `2` usage error. `ssh` additionally passes
through a non-zero ssh exit code.

## Error codes and remedies

| Code | Meaning | Remedy |
|---|---|---|
| `usage` | Invalid command line (flag, VM name, `--user`, `--mount`, `--script` file, `--script`+`--no-boot`, unsupported Arch release) | Fix arguments; see `--help` |
| `runtime-error` | Uncaught failure without a specific code | Re-run with `VM_JSON_TRACE=1`, read the message |
| `libvirt-unreachable` | `virsh` cannot talk to libvirtd | Not in `libvirt`/`kvm` groups in this shell → re-login; `virt-runner setup --check` |
| `pool-not-found` | `vm-pool` not defined | `virt-runner setup` |
| `pool-not-active` | `vm-pool` not active | `virsh pool-start vm-pool` |
| `pool-path-unknown` | Pool directory path not determinable | Inspect `virsh pool-dumpxml vm-pool` |
| `network-not-active` | `default` network not active | `virsh net-start default` |
| `unsupported-arch` | Host arch is neither x86_64 nor aarch64 (ppc64le, s390x, riscv64) | Not fixable — run on an x86_64/aarch64 host |
| `vm-already-defined` | Name taken | Choose another name, or destroy the old VM **only if it is yours** |
| `vm-not-defined` | No such domain (destroy/ssh/modify) | For cleanup: treat as "already gone"; for `modify`, the name may belong to a domain this tool never created (`not-a-virt-runner-vm` says so explicitly) |
| `not-a-virt-runner-vm` | Domain exists but has no `vm-pool` disk and no virt-runner metadata | `modify` refuses it — manage that domain with `virsh` yourself, do not "adopt" it |
| `modify-noop` | Every option given already matches the VM (exit 2) | Nothing to do; a same-size `--disk` without `--no-auto-grow-fs` *is* a retry of the guest filesystem grow, so that one is not a noop |
| `disk-shrink-unsupported` | `modify --disk` smaller than the current volume | virt-runner never shrinks; destroy + recreate if a smaller disk is really needed |
| `net-change-failed` | `virsh attach-interface`/`detach-interface` failed | Target missing (`virsh net-list --all`, `ip link`) or PCI slots exhausted. **Check `virsh domiflist NAME` first** and restore a working NIC before retrying |
| `vm-reboot-failed` | `modify --reboot` could not bring the domain back up | Changes are persisted — `virsh start NAME`, or `--reboot` again; the guest is not corrupted by this |
| `volume-resize-failed` | `virsh vol-resize` failed (create, or growing the volume of a powered-off guest) | `virsh pool-info vm-pool` for space; `virsh vol-info --pool vm-pool NAME_vda.qcow2` for the real capacity |
| `vm-not-running` | Domain exists but stopped | `virsh start NAME` |
| `no-ip` | No lease/address for the VM | Wait and retry; `virsh domifaddr NAME`; `virsh console NAME` |
| `ssh-key-missing` | No usable key at `--ssh-key`, none generatable | Use a path ending in `.pub` in a writable dir |
| `image-download-failed` | Image or listing not fetchable | Network/mirror issue; retry (Fedora mirrors flap); or `--keep-going` if cached |
| `image-verification-failed` | SHA256 mismatch / no checksum entry | Delete that cache dir and retry; for `--image`, ensure the same-dir `SHA256SUMS` lists the file |
| `image-no-cache` | `--keep-going` but nothing cached | Fix the download first |
| `image-arch-unavailable` | No profile image for the host arch (`--distro arch` on aarch64) | Use `--distro ubuntu`/`fedora` on aarch64, or run on x86_64 |
| `cloud-init-failed` | Seed files could not be generated | Check tmp space / key file readability |
| `volume-create-failed` / `volume-import-failed` / `volume-resize-failed` | `virsh vol-create-as` / `vol-upload` / `vol-resize` failed | Check pool space (`virsh pool-info vm-pool`); a leftover `NAME_vda.qcow2` → `virsh vol-delete NAME_vda.qcow2 vm-pool` |
| `vm-create-failed` | `virt-install` failed (cleaned up) | Read message; with `--mount` check virtiofsd / shared memory |
| `vm-destroy-failed` | `virsh destroy`/`undefine` failed | Inspect `virsh dominfo NAME`; retry |
| `volume-delete-failed` | `vol-delete` failed during teardown | `virsh vol-delete NAME_vda.qcow2 vm-pool` |
| `lease-timeout` | No DHCP lease in 120 s | `virsh console NAME`; check `default` network; destroy the VM afterwards |
| `ssh-timeout` | SSH round-trip failed for 90 s | `virsh console NAME`; check cloud-init; right key/user; destroy afterwards |
| `virtiofsd-missing` | `--mount` but no `/usr/libexec/virtiofsd` | `sudo apt install virtiofsd` (or `virt-runner setup`) |
| `script-failed` | A `--script` exited non-zero (VM kept) | `virt-runner ssh NAME`, fix and re-run, or destroy |
| `snapshot-already-exists` | `snapshot create` with a name already in use | Pick another name, or `snapshot delete` the old one if it is yours |
| `snapshot-not-found` | No such snapshot | `virt-runner snapshot list` |
| `snapshot-incomplete` | Disk or metadata volume missing / invalid | `virt-runner snapshot delete SNAP` and re-create it |
| `snapshot-seal-failed` | The in-guest seal script failed (VM kept running) | Read the message (it quotes the guest output); re-run, or destroy the VM |
| `vm-shutdown-timeout` | Guest did not power off in `--shutdown-timeout` (no forced power-off) | Stop the workload (`virt-runner ssh`), re-run with a larger timeout |
| `snapshot-copy-failed` | `virsh vol-create-from` failed | Pool space: `virsh pool-info vm-pool` |
| `snapshot-metadata-failed` | Writing the sidecar volume failed (copy removed) | Pool space / permissions; re-run |
| `snapshot-in-use` | `snapshot delete` while clones are backed by it | Destroy the listed clones first — never `virsh vol-delete` the snapshot |
| `overlay-create-failed` | Clone overlay could not be created | Pool space; `virsh vol-info --pool vm-pool SNAP.snapshot.qcow2` |
