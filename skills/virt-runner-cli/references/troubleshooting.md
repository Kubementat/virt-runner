# virt-runner troubleshooting and gotchas

Distilled from `README.md` "Troubleshooting" and `docs/lessons-learned/`.

## Symptom → fix

| Symptom | Fix |
|---|---|
| `libvirt not reachable` | User not in `libvirt`/`kvm` groups in this shell → log out/in; `./install-prerequisites.sh --check` |
| `pool 'vm-pool' is not active` / `network 'default' is not active` | `virsh pool-start vm-pool && virsh net-start default` |
| `no DHCP lease after 120s` / `SSH … not reachable yet` | `virsh console NAME` (Ctrl-`]` detaches) to watch boot. Manual ssh must use the injected key: `ssh -i ~/.ssh/virt_runner_key <user>@IP` (user `ubuntu`/`arch`/`fedora`); `virt-runner ssh NAME` picks it automatically |
| `SHA256 mismatch` | Corrupt cache: delete the image's cache dir (below) and retry |
| Disk survives `destroy` | `virsh vol-delete NAME_vda.qcow2 vm-pool` |
| Fedora `image-download-failed` 404 | Stale mirror index; tool already falls back to next-newest build — just retry |
| `--mount` share empty after guest reboot | Should not happen (fstab `nofail` entry). Check `/etc/fstab` and `mount | grep virtiofs` in guest |
| Fedora share `Permission denied` | SELinux AVC (`sudo ausearch -m avc -ts recent`); not observed so far |
| `script-failed` | VM kept: `virt-runner ssh NAME`; inspect stderr log of the create run |
| Script hangs/fails on prompt | stdin is `/dev/null`: use `-y` / `--noconfirm` / `DEBIAN_FRONTEND=noninteractive` |
| `python3: command not found` in script on Arch | Arch image ships no python3: use bash, or `sudo pacman -Sy --noconfirm python` first |

## Inspecting a guest that failed to come up

```bash
virsh console NAME                      # serial console; Ctrl-] to detach
virsh domifaddr NAME                    # address as libvirt sees it
virsh dominfo NAME; virsh domblkinfo NAME vda
# in the guest:
sudo cloud-init status --long
sudo less /var/log/cloud-init-output.log
```

Then clean up: `virt-runner destroy NAME` (only if it is yours).

## Image cache layout

Root `~/vm-images` (override `VM_CREATE_CACHE_DIR`):

| Source | Cache dir |
|---|---|
| Ubuntu | `~/vm-images/<release>/` (e.g. `resolute/`) |
| Arch | `~/vm-images/arch/latest/` |
| Fedora | `~/vm-images/fedora/<release>/` |
| `--image URL` | `~/vm-images/custom/<first 12 hex of sha256(URL)>/` |

- A cached image is **always reused, never refreshed** (also for `latest`/`current`). Delete the
  dir to get a newer build. Don't delete it without reason — cold downloads take minutes.
- A `.verified` marker next to the image proves verification; cache hits without it report
  `verification: "skipped"`.
- Zero-byte leftovers from interrupted downloads are discarded automatically.

## Design facts worth knowing

- SSH to guests always uses `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null`: guests
  are ephemeral and DHCP reuses IPs, so host keys change. Do the same for manual ssh and never
  pollute `~/.ssh/known_hosts`.
- The printed `ssh_command` includes `-i <private key>` so it works copy-pasted.
- `--ram` is GiB on the CLI (converted to MiB for virt-install).
- The disk is exactly `--disk` GiB (the tool resizes after `vol-upload`, which would otherwise
  shrink it to the image's virtual size); the guest grows its root partition on first boot.
- `--mount` adds shared memory backing (`memfd`) to the domain; virtiofsd runs as root under
  `qemu:///system` and exits with the domain.
- The NoCloud seed is attached for the first boot only; later boots may run without cloud-init
  (Ubuntu 26.04 disables it). Don't expect cloud-init to re-run after a reboot.
- User and SSH identity are stored in libvirt domain metadata at create; `ssh` and `list` read them.
- Domains are created with `--autostart`.
- Env overrides: `VM_CREATE_CACHE_DIR`, `VM_CREATE_LEASE_FILE` / `VM_<CMD>_LEASE_FILE` (default
  `/var/lib/libvirt/dnsmasq/virbr0.status`), `VM_JSON_TRACE`.

## Host setup details (`install-prerequisites.sh`)

Ubuntu/Debian only (apt). Installs KVM/libvirt/virt-install/virtiofsd, adds the user
(`VIRT_USERNAME`, default `$SUDO_USER`/`$USER`) to `libvirt` and `kvm`, defines+starts+autostarts
the `default` network (`VIRT_NET_NAME`) and the `vm-pool` dir pool (`VIRT_POOL_NAME`, dir
`VIRT_POOL_DIR`, default `/var/lib/libvirt/images/vms`). Flags: `--check` (audit only),
`--with-virt-manager`. Idempotent. Requires a re-login afterwards.
