# virt-install's `--cloud-init` ISO is deleted while the guest is still booting — fatal on aarch64

`virt-runner create` used to hand cloud-init data to virt-install via
`--cloud-init user-data=…,meta-data=…,disable=on`. On hosts where the user
cannot write `/var/lib/libvirt/boot` directly (virt-install 4.1.0),
virt-install uploads its generated NoCloud ISO as a volume in a `boot-scratch`
pool it creates there — and **deletes that volume in its exit cleanup, a few
seconds after starting the domain, while the guest is still in UEFI**
(`Removing volume 'virtinst-…-cloudinit.iso'` in `~/.cache/virt-manager/
virt-install.log`). This is upstream virt-manager issue [#178](https://github.com/virt-manager/virt-manager/issues/178),
still present in 4.1.0.

On x86_64 the deleted file is invisible: SeaBIOS boots straight from the disk
and the guest reads the seed through qemu's still-open file descriptor. On
aarch64 it is fatal: edk2 UEFI tries the CDROM as a boot option first; the
missing backing file turns that into a device error, and BdsDxe reacts with
`Reset System` — a boot loop that never reaches the disk:

```
BdsDxe: loading Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x1,0x4)/Pci(0x0,0x0)
BdsDxe: starting Boot0001 "UEFI Misc Device" ...
Reset System
```

Symptom: `create` reaches `wait-ip`, the domain dies ~5–10 s after creation
(`qemu …log` empty, libvirt journal: `child reported (status=125): Unable to
get XATTR trusted.libvirt.security.ref_dac on …/virtinst-…-cloudinit.iso: No
such file or directory`), and the pipeline ends in `lease-timeout`.

Fix: virt-runner now builds the NoCloud seed ISO itself (`xorrisofs -V
cidata`, user-data + meta-data at the ISO root) and uploads it to the
storage pool as a volume (`<NAME>-cloudinit.iso` in `vm-pool` — the
hypervisor user must be able to read the file, which rules out the user's
home dir), attached as a plain `--disk vol=…,device=cdrom` that
virt-install does not delete. The volume stays attached for the VM's
lifetime and is removed on `destroy` (or in `create`'s own failure
cleanup). It must NOT be deleted after a proven first boot: libvirt
resolves the volume to a file path in the domain XML, so a missing seed
file breaks every later `virsh start` — and every virt-runner VM is
autostart, so a host reboot would leave all of them unable to start
(verified: `error: Cannot access storage file '…-cloudinit.iso'`). The
`runcmd` line in `render_user_data` (writing
`/etc/cloud/cloud-init.disabled`) replaces `--cloud-init disable=on`, so
later boots skip cloud-init even while the seed stays attached.

Discovered while bringing up aarch64 host support (2026-09-27): every real
aarch64 boot looped in UEFI until the seed deletion was captured in the
console (`BdsDxe … Reset System`).
