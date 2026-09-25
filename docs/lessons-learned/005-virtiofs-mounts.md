# Lesson 005 — virtiofs host-directory shares (`create --mount`)

Date: 2026-09-25

## Why `bootcmd` instead of `mounts:` in cloud-init?

The `mounts:` module rewrites bare mount tags like `mount0` to `/dev/mount0`, which
is the device-node form used by 9p/virtio-serial transports. A virtiofs tag is
**not** a device — it is a label that the guest-side `mount -t virtiofs` command
resolves via the vhost-user socket. Using `mounts:` would produce an invalid
`fs_spec` and the mount would fail silently.

`bootcmd` (in `cloud-init-local`, `Before=sshd.service`) runs on the **first**
boot, so the mount is in place before SSH answers.

## The mount does NOT survive reboots from bootcmd alone (found in review)

The plan assumed `bootcmd` re-runs on every boot. It does not, on this stack:

1. `virt-install --cloud-init` attaches the NoCloud seed CDROM **only for the
   first boot** (man page: "The device is only attached for the first boot";
   the domain XML afterwards shows an empty CDROM).
2. On Ubuntu 26.04 the `cloud-init-generator` runs `ds-identify` at every boot;
   with no seed it exits 1 ("no datasource found") and the generator
   **disables cloud-init entirely** (`ON_NOTFOUND=disabled`). All the
   `cloud-init-*` stage services then no-op silently, so even
   `frequency: always` `bootcmd` lines never run again.

Observed: `sudo reboot` in the guest → domain stays running, but
`mount | grep virtiofs` is empty afterwards.

**Fix (applied):** the bootcmd line also writes an idempotent
`<tag> <target> virtiofs defaults,nofail 0 0` entry to `/etc/fstab` on the
first boot. fstab lives on disk, so systemd mounts the share on every later
boot even with cloud-init disabled. `nofail` keeps the guest bootable if the
share is ever missing.

(Older images, e.g. Ubuntu 24.04, re-run cached "always" modules without a
seed, where bootcmd alone suffices — the fstab entry is harmless there.)

## Fedora SELinux

Observed in the integration test (Fedora cloud image, SELinux enforcing): the
plain `mount -t virtiofs` bootcmd line worked for both reads and writes from the
guest — no AVC denials, no extra mount options needed.

If a future Fedora image **does** deny access (symptoms: `cat /mnt/share/marker`
→ `Permission denied`; `sudo ausearch -m avc -ts recent` in the guest shows AVC
denials), the workaround is a SELinux context mount option in the bootcmd line:

```python
f"mount -t virtiofs -o context=system_u:object_r:nfs_t:s0 {m['tag']} {m['target']}"
```

Test the exact line by hand in the guest first (`virsh console NAME`), then adopt it.

## Why `--memorybacking source.type=memfd,access.mode=shared`?

virtiofs requires shared guest memory. Without the `--memorybacking` flag, the
domain fails to start with a shared-memory / vhost-user error. The flag is
**only** added when at least one `--mount` is requested (unmounted creates must
produce byte-identical virt-install commands).

## UIDs pass through

Under `qemu:///system`, libvirt starts virtiofsd as root. The host directory
is accessible to root, and UIDs pass straight through to the guest. The usual
guest user (`ubuntu`/`arch`/`fedora`) is uid 1000, matching the host user.
Files created by root in the guest are root-owned on the host.

## AppArmor

No AppArmor changes are needed. The existing `/etc/apparmor.d/usr.sbin.libvirtd`
profile already allows `/usr/{lib,lib64,lib/qemu,libexec}/virtiofsd PUx`, and
the `virtiofsd` profile is `unconfined`.

## `install-prerequisites.sh` integration

`virtiofsd` is added unconditionally to `BASE_PACKAGES` in the script. It
installs `/usr/libexec/virtiofsd` on Ubuntu 24.04+ (the same releases that
require virt-install ≥ 4.1). The verification block uses `[[ -x ]]` because
the binary is not on PATH.
