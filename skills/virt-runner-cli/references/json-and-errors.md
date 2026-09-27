# virt-runner JSON contract and error codes

Source of truth: `README.md` ("JSON output"), `src/virt_runner/output.py`, `cmd_*.py`.

## Envelope (all commands)

```json
{
  "tool": "vm-create | vm-destroy | vm-list | vm-ssh",
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
    "autostart": true
  },
  "image": {
    "source_url": "https://cloud-images.ubuntu.com/resolute/current/…img",
    "cache_path": "/home/u/vm-images/resolute/…img",
    "cache_hit": true, "downloaded": false,
    "verification": "sha256-sums | skipped", "verified": true
  },
  "scripts": [{"path": "/abs/setup.sh", "exit_code": 0}]
}
```

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

## list (`vm-list`)

```json
{
  "tool": "vm-list", "status": "success", "error": null,
  "pool": "vm-pool", "user_override": null, "count": 1,
  "vms": [{
    "name": "dev", "uuid": "…", "state": "running", "mac": "52:54:00:…",
    "ip": "192.168.122.57", "ip_status": "lease | not-running | running-no-lease | no-mac",
    "ssh_user": "ubuntu",
    "ssh_command": "ssh -i … ubuntu@192.168.122.57",
    "console_command": "virsh console dev",
    "teardown_command": "virt-runner destroy dev"
  }]
}
```

`ip`, `ssh_user`, `ssh_command` are `null` unless `ip_status == "lease"`. `list` needs libvirt
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

## Exit codes

`0` success · `1` runtime or preflight failure · `2` usage error. `ssh` additionally passes
through a non-zero ssh exit code.

## Error codes and remedies

| Code | Meaning | Remedy |
|---|---|---|
| `usage` | Invalid command line (flag, VM name, `--user`, `--mount`, `--script` file, `--script`+`--no-boot`, unsupported Arch release) | Fix arguments; see `--help` |
| `runtime-error` | Uncaught failure without a specific code | Re-run with `VM_JSON_TRACE=1`, read the message |
| `libvirt-unreachable` | `virsh` cannot talk to libvirtd | Not in `libvirt`/`kvm` groups in this shell → re-login; `./install-prerequisites.sh --check` |
| `pool-not-found` | `vm-pool` not defined | `./install-prerequisites.sh` |
| `pool-not-active` | `vm-pool` not active | `virsh pool-start vm-pool` |
| `pool-path-unknown` | Pool directory path not determinable | Inspect `virsh pool-dumpxml vm-pool` |
| `network-not-active` | `default` network not active | `virsh net-start default` |
| `unsupported-arch` | Host not x86_64 | Not fixable; images are x86_64 only |
| `vm-already-defined` | Name taken | Choose another name, or destroy the old VM **only if it is yours** |
| `vm-not-defined` | No such domain (destroy/ssh) | For cleanup: treat as "already gone" |
| `vm-not-running` | Domain exists but stopped | `virsh start NAME` |
| `no-ip` | No lease/address for the VM | Wait and retry; `virsh domifaddr NAME`; `virsh console NAME` |
| `ssh-key-missing` | No usable key at `--ssh-key`, none generatable | Use a path ending in `.pub` in a writable dir |
| `image-download-failed` | Image or listing not fetchable | Network/mirror issue; retry (Fedora mirrors flap); or `--keep-going` if cached |
| `image-verification-failed` | SHA256 mismatch / no checksum entry | Delete that cache dir and retry; for `--image`, ensure the same-dir `SHA256SUMS` lists the file |
| `image-no-cache` | `--keep-going` but nothing cached | Fix the download first |
| `cloud-init-failed` | Seed files could not be generated | Check tmp space / key file readability |
| `volume-create-failed` / `volume-import-failed` / `volume-resize-failed` | `virsh vol-create-as` / `vol-upload` / `vol-resize` failed | Check pool space (`virsh pool-info vm-pool`); a leftover `NAME_vda.qcow2` → `virsh vol-delete NAME_vda.qcow2 vm-pool` |
| `vm-create-failed` | `virt-install` failed (cleaned up) | Read message; with `--mount` check virtiofsd / shared memory |
| `vm-destroy-failed` | `virsh destroy`/`undefine` failed | Inspect `virsh dominfo NAME`; retry |
| `volume-delete-failed` | `vol-delete` failed during teardown | `virsh vol-delete NAME_vda.qcow2 vm-pool` |
| `lease-timeout` | No DHCP lease in 120 s | `virsh console NAME`; check `default` network; destroy the VM afterwards |
| `ssh-timeout` | SSH round-trip failed for 90 s | `virsh console NAME`; check cloud-init; right key/user; destroy afterwards |
| `virtiofsd-missing` | `--mount` but no `/usr/libexec/virtiofsd` | `sudo apt install virtiofsd` (or `./install-prerequisites.sh`) |
| `script-failed` | A `--script` exited non-zero (VM kept) | `virt-runner ssh NAME`, fix and re-run, or destroy |
