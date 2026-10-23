# 10. Growing an existing VM: libvirt's live and config paths are not symmetric

Discovered while implementing `virt-runner modify` (vCPU / RAM / disk / NIC changes on a
running or powered-off guest). The commands look symmetric in the docs and are not — each
axis has its own rule, and guessing produces a tool that either fails on a live guest or
silently changes only one of the two definitions.

Verified against libvirt master (`src/qemu/qemu_driver.c`), not guessed:

## 1. `blockresize` needs an active domain

```
qemuDomainBlockResize():  if (virDomainObjCheckActive(vm) < 0) goto endjob;
```

So `virsh blockresize NAME vda 16G` on a shut-off domain fails ("domain is not running").
For a powered-off guest grow the storage instead, with the grow-only flag so a bug in our
own planner can never truncate a disk:

```
virsh vol-resize NAME_vda.qcow2 --pool vm-pool --capacity 16G --grow
```

`--grow` is the important half: without it `vol-resize` happily shrinks.

## 2. A live domain's *ceiling* is fixed — but `--config` alone may still raise it

Both sizing APIs refuse to move a maximum when the live definition is part of the request:

```
qemuDomainSetVcpusMax():   if (def) "maximum vcpu count of a live domain can't be modified"
qemuDomainSetMemoryFlags(): if (def) "cannot resize the maximum memory on an active domain"
```

`def` is the *live* definition, and `virDomainObjGetDefs()` only fills it for
`VIR_DOMAIN_AFFECT_LIVE`. `virsh setvcpus --current` (the default) and `--live --config`
therefore fail on a running guest, while `--config` **alone** is accepted and writes the
persisted definition. Practical consequence — a grow past the current ceiling is two calls,
in this order:

```
virsh setvcpus NAME 8 --maximum --config   # raises <vcpu> (the ceiling)
virsh setvcpus NAME 8 --config             # sets the boot count (<vcpu current=…>)
virsh setmem NAME 8388608 --maximum --config   # writes <memory> when there is no <maxMemory>
virsh setmem NAME 8388608 --config
```

`setmem --maximum` maps to `virDomainDefSetMemoryTotal()` — for the flat `<memory>` layout
virt-runner creates (no `<maxMemory>`, no virtio-mem) it *is* the boot memory. A plain
`setmem`/`setvcpus --config` above the ceiling fails with "cannot set memory higher than max
memory", so the pattern is: try the single call, on failure and only when growing, raise the
ceiling and retry. A shrink needs no ceiling change, and stays live-able (balloon down).

Because the ceiling change only lands at the next boot, `--live` cannot promise anything here:
report `effective: "on-boot"`, warn, and let `--reboot` close the gap.

## 3. Capacity units differ per command

`<memory unit='KiB'>` in domain XML is KiB, but `virsh vol-info` / `virsh domblkinfo` print
`Capacity: 17179869184` in **bytes** and `Capacity: 16.0 GiB` with a unit suffix depending on
version. One parser for both (`parse_capacity_gib`) with an explicit default unit, and one
for XML (`parse_size_kib`) — sharing them is how a 1024× error sneaks in.

## 4. `dataclasses.replace()` does not know about properties

`VmSpec.running` is a `@property` derived from `state`. `replace(spec, running=...)` raises
`TypeError: unexpected keyword argument 'running'` — and in `domain_spec()` it looked
harmless next to `state=`, because it only fires on the code path that re-reads a live domain.
Set `state` and let the property do its job.

## 5. `virsh` table columns are not uniform

`virsh pool-list` prints `Name State Autostart` (no `Id` column), `virsh list` prints
`Id Name State`. A `_table_has()` helper that matches the name in the first column works for
one and silently never matches for the other. Check the real output before parsing a table —
the failure mode is "pool not found", not a parse error.

## Lessons

- Read the driver function before designing a live/config fallback chain; the error strings
  tell you exactly which flag combinations are legal.
- Try the cheap call, catch, escalate to the two-call form only for the growth case — keeps
  the common path one subprocess and still handles the ceiling.
- Destructive primitives get their guard flag (`--grow`) even when the planner already
  refuses; defence in depth on disk sizes.
- A filesystem grow is a guest-side operation, not a libvirt one: it can only run on a booted
  guest, so re-issuing the same `--disk N` has to stay a valid "grow my filesystem now"
  request instead of becoming a no-op.
