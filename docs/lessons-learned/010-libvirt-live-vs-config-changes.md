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
For a powered-off guest grow the storage instead:

```
virsh vol-resize NAME_vda.qcow2 --pool vm-pool --capacity 16G
```

There is no `--grow` option (see lesson 013) — that flag does not exist in libvirt 10.0 and the
command errors out with "doesn't support option". The never-shrink guarantee is libvirt's own:
`vol-resize` refuses a smaller capacity unless `--shrink` is explicitly passed, and virt-runner
never passes it. The planner refuses shrinks upstream as well, so there are two guards, not three.

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
virsh setmaxmem NAME 8388608 --config      # raises <memory> when there is no <maxMemory>
virsh setmem   NAME 8388608 --config       # sets what the next boot gets
```

The memory pair is not symmetric with the vCPU pair, and that cost a review round: **`virsh
setmem` has no `--maximum`** (that option belongs to `setvcpus` alone). The ceiling command is the
separate `setmaxmem`, and `setmaxmem` on its own is only half a change — it leaves
`<currentMemory>` at the old value, so the guest boots with the old amount of RAM and
`dumpxml --inactive` looks perfectly fine. Both calls are needed.

A plain `setmem`/`setvcpus --config` above the ceiling fails with "cannot set memory higher than
max memory", so the pattern is: try the single call, on failure and only when growing, raise the
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

## 6. Lowering RAM is a two-call change too, and it fails silently

Growing was covered above. **Shrinking** is worse, because nothing fails: `virsh setmem NAME KIB
--config` accepts a smaller size on the first try, moves `<currentMemory>`, and leaves `<memory>`
at the old, bigger number. Both documents then look internally consistent — and `<memory>` is
what qemu reserves. The proof, on a domain whose config had been grown past the host:

```
$ virsh -c qemu:///system dumpxml --inactive fx-b | grep -E '<memory|<currentMemory'
<memory unit='KiB'>4294967296</memory>            # 4 TiB on a 125 GiB host
<currentMemory unit='KiB'>2097152</currentMemory>  # "2 GiB", looks fine
$ virsh -c qemu:///system start fx-b
error: internal error: QEMU unexpectedly closed the monitor (vm='fx-b'):
  qemu-system-x86_64: cannot set up guest memory 'pc.ram': Cannot allocate memory
```

So a config write that returns 0 is not a change that works, and an error-free `modify --ram` is
not a bootable guest. `setmaxmem` has to follow the target **down** as well (`--config` accepts
that too), and a `--ram` above the host's installed RAM has to be refused before the write —
libvirt has no opinion about it at all.

## 7. Compare a request to the config, not to the running guest

`modify --ram 2` on the domain above answered `modify-noop`: the running guest really does have
2 GiB, so the tool concluded "already set" — while the next boot had 4 TiB queued up. A running
domain is two documents, they drift (a config-only change nobody rebooted into, a `virsh setmem
--live` by someone holding the client, a bug like this one), and comparing only the live side
turns a broken config into a green exit code.

The rule that replaced it: config matches **and** guest matches → `noop`; config matches only →
report an `on-boot` change with no libvirt call (the guest is behind, rebooting is the fix);
config disagrees → write it, with the config's number as `from` so the document is not "2 → 2".

## Lessons

- A config write that succeeds is not a correct config: `<memory>` is what qemu reserves, so
  check both halves (`dumpxml --inactive`) before you claim a resize worked.
- Ask "what does libvirt *refuse* here?" for every write. Where it refuses, you get an error and
  a retry path; where it silently accepts (sizes above host RAM, ceilings left high on a
  shrink), the failure moves to the next boot and looks unrelated.

- Read the driver function before designing a live/config fallback chain; the error strings
  tell you exactly which flag combinations are legal.
- Try the cheap call, catch, escalate to the two-call form only for the growth case — keeps
  the common path one subprocess and still handles the ceiling.
- Destructive primitives need a guard, but check that the guard you are reaching for actually
  exists before writing it down (see lesson 013). Here libvirt's own refusal to shrink without
  `--shrink` is the guard.
- A filesystem grow is a guest-side operation, not a libvirt one: it can only run on a booted
  guest, so re-issuing the same `--disk N` has to stay a valid "grow my filesystem now"
  request instead of becoming a no-op.
- Diff a request against the *persisted* definition, not the running one. Reading only the live
  side reads as "already set" to a domain that is one boot away from failing.
