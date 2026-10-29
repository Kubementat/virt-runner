# 16. `vol-create-from` writes qcow2 v2, and libvirt deletes backing files under running clones

Two libvirt 10.0 behaviours that `snapshot create`/`snapshot delete` have to work around.

## `vol-create-from` without `<compat>` gives qcow2 v2

```
$ virsh vol-create-from vm-pool snap.xml src_vda.qcow2 --inputpool vm-pool
$ qemu-img info /var/lib/libvirt/images/vms/snap.snapshot.qcow2 | grep compat
    compat: 0.10
```

A volume XML with just `<format type='qcow2'/>` produces `compat 0.10` (qcow2 version 2), while
every volume `vol-create-as` makes is `compat 1.1`. The fix is one element in the target:

```xml
<target><format type='qcow2'/><compat>1.1</compat>
  <permissions><mode>0644</mode></permissions></target>
```

`snapshot_volume_xml` always writes it, and a unit test asserts it is there. Mode `0644` keeps the
snapshot readable for `qemu-img info` without sudo. (`vol-clone` would also flatten an overlay,
but gives `root:root 0600` with no say over mode or compat; `--reflink` is rejected for any qcow2
target: `unsupported flags (0x2) in function storageBackendCreateQemuImg`.)

## Nothing in libvirt protects a backing file

```
$ virsh vol-delete snap.snapshot.qcow2 vm-pool      # while a clone runs on it
Vol snap.snapshot.qcow2 deleted
$ virsh start clone      # after the clone was stopped
error: Cannot access backing file '/var/lib/libvirt/images/vms/snap.snapshot.qcow2' ...
```

The running clone keeps reading the unlinked file and only breaks on its next start. So the
refusal has to live in virt-runner: `snapshot delete` runs `vol-dumpxml` on every pool volume and
refuses with `snapshot-in-use` while any `<backingStore><path>` names the snapshot. libvirt keeps
reporting that path even after the backing file is gone, so the same scan finds orphaned
overlays. There is deliberately no `--force`. Plain `virsh vol-delete` still bypasses the check,
and the README says so.

Small extra: `virsh vol-list` has no `--name` option (`error: command 'vol-list' doesn't support
option --name`), so the pool listing is parsed from the table — lesson 013 again.
