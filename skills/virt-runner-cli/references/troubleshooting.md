# virt-runner troubleshooting and gotchas

Distilled from `README.md` "Troubleshooting" and `docs/lessons-learned/`.

## Symptom → fix

| Symptom | Fix |
|---|---|
| `libvirt not reachable` | User not in `libvirt`/`kvm` groups in this shell → log out/in; `virt-runner setup --check` |
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

## `modify` playbook

The report describes **libvirt**; the guest may know differently. Confirm from the guest before
claiming a change took effect.

```bash
virsh -c qemu:///system dumpxml NAME | grep -E '<(memory|currentMemory|vcpu|maxMemory)'   # live
virsh -c qemu:///system dumpxml --inactive NAME | grep -E '<(memory|vcpu|maxMemory)'       # config
virsh -c qemu:///system vol-info --pool vm-pool NAME_vda.qcow2 | grep Capacity              # disk truth
virsh -c qemu:///system domiflist NAME                                                      # NICs now
# in the guest:
nproc; grep MemTotal /proc/meminfo; lsblk -bdno SIZE /dev/vda; df -BG / | tail -1; ls /sys/class/net
```

| Symptom | What happened / what to do |
|---|---|
| `effective: "on-boot"`, guest unchanged | the change is in the config, above the live ceiling (`<vcpu max>`, `<memory>`) or the guest was off: `virt-runner modify NAME --reboot --wait`, then re-read from inside the guest |
| `--ram`/`--vcpu` grow refused with no `--live` effect | expected — a live change cannot move the ceiling. It is still persisted; reboot closes it |
| `--vcpu` refused live **below** the ceiling | look at the live `<vcpu>`: if max equals current there are no hotpluggable CPUs, so no live change is possible at all. Raise the ceiling through the config (`modify --vcpu N --plan` shows what it will write), reboot once, and live grows will work from then on. `changes[].live_refused` names the reason |
| vCPU said `"now"`, `nproc` did not move | libvirt enabled the CPU; the guest kernel has not brought it online. `/sys/devices/system/cpu/present` shows it, `/sys/devices/system/cpu/cpuN/online` is `0`. Reboot (`--reboot --wait`) — the guest onlines CPUs while booting |
| `--ram` live shrink, then the guest has the old size again | a balloon change moves `<currentMemory>`, not `<memory>`, so it can inflate back. Reboot to pin it, and check with `grep MemTotal` rather than `grep memory` in the XML |
| disk grew, `df` did not move (`fs_grown: false`) | guest-side grow was refused (LVM, missing `growpart`). Run the commands the warning names, then `modify NAME --disk <same-size>` — that is the filesystem-only retry |
| `fs_grown: null` after a powered-off grow | the guest could not be asked. Boot it, then re-issue the same `--disk` |
| `net-change-failed` | the target does not exist: `virsh net-list --all` for a virtual network, `ip link` for a bridge. Nothing was detached — the target is validated **before** the old NIC is given up, so the guest is still reachable (`virsh domiflist NAME` proves it). Fix the name and retry |
| guest unreachable right after `--mac`/`--net` | it re-acquired a lease: `virt-runner list --json \| jq -r '.vms[]\|select(.name=="NAME")\| .ip, .ssh_command'`. Old `known_hosts` entries and open sessions are stale |
| two NICs, guest slow/unreachable on the first address | both NICs are on the same subnet. Put the extra interface on a different `network=`/bridge, or remove it |
| `not-a-virt-runner-vm` | correct behaviour — the domain has no `vm-pool` disk and no virt-runner metadata. Do not hand-edit it to "make it ours"; change it with `virsh` or report it |
| config already carries the change, the guest does not | `modify` calls that a change (`effective: "on-boot"`, nothing written) — it is the `--reboot --wait` request speaking. If both sides match you get `modify-noop` (exit 2), which is the honest answer |
| `modify-noop` but `virsh dumpxml --inactive` shows a different size | fixed in this build: sizing is compared to the config, so that case is now a change. If it still says noop, the config and the guest really do agree — look at `vm.ram_gib`/`vm.vcpu` in the document, which are the *running* numbers |
| `runtime-error` naming the host's RAM | `--ram` was bigger than the machine. libvirt accepts that into the config and qemu fails on the next boot (`cannot set up guest memory 'pc.ram': Cannot allocate memory`), so nothing was changed |

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

## Host setup details (`virt-runner setup`)

Ubuntu/Debian only (apt). Installs KVM/libvirt/virt-install/virtiofsd, adds the user
(`VIRT_USERNAME`, default `$SUDO_USER`/`$USER`) to `libvirt` and `kvm`, defines+starts+autostarts
the `default` network (`VIRT_NET_NAME`) and the `vm-pool` dir pool (`VIRT_POOL_NAME`, dir
`VIRT_POOL_DIR`, default `/var/lib/libvirt/images/vms`). Flags: `--check` (audit only),
`--with-virt-manager`. Idempotent. Requires a re-login afterwards.
