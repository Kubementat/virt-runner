# `virsh undefine` refuses aarch64 UEFI domains: NVRAM must be discarded explicitly

On aarch64 hosts every VM boots UEFI (QEMU's built-in firmware; no `--boot`
change needed). The domain therefore carries an NVRAM variable store
(`<nvram>` element, e.g. `/var/lib/libvirt/qemu/nvram/<NAME>_VARS.fd`).

libvirt refuses to undefine such a domain without saying what happens to the
NVRAM:

```
$ virsh undefine it-vm-a
error: Failed to undefine domain 'it-vm-a'
error: Requested operation is not valid: cannot undefine domain with nvram
```

So `virt-runner destroy` failed with `vm-destroy-failed` and left the domain
defined plus its volume (the volume delete comes after the undefine) — exactly
the "nothing left behind" contract teardown must keep.

Fix: `virsh --quiet undefine --nvram <NAME>`. `--nvram` discards the variable
store with the domain; it is a no-op for NVRAM-less domains (x86_64/SeaBIOS),
so the same call works on both archs. Verified on libvirt 10.0: the domain,
the volume, and the `*_VARS.fd` file are all gone.

Discovered while bringing up aarch64 host support (2026-09-27): the first
real aarch64 integration run hit this in `destroy` of a VM that had already
failed earlier in the pipeline.
