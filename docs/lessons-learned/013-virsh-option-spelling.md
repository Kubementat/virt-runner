# 13. Run the `virsh` command before you code the flag

`virt-runner modify` shipped with two libvirt invocations that do not exist. Both looked
plausible, both read fine in a commit message, and both failed on every real host — including
the libvirt 10.0 this project treats as its reference (lesson 001 was written against it).

| Code wrote | Reality on libvirt 10.0 | What it broke |
|---|---|---|
| `virsh setmem NAME <KiB> --maximum --config` | `error: command 'setmem' doesn't support option --maximum` | every `--ram` **grow** failed with `runtime-error` |
| `virsh vol-resize VOL --pool P --capacity 16G --grow` | `error: command 'vol-resize' doesn't support option --grow` | every disk grow of a **powered-off** guest failed with `volume-resize-failed` |

## Why the plausible version is wrong

`setvcpus` has `--maximum`. Reading that pattern across to `setmem` felt safe — same driver
area, same "two-step ceiling" shape — but virsh declares its options per command, and
`setmem`'s list is `--config --live --current`. The ceiling command is a *separate* command:

```
$ virsh -c qemu:///system setmaxmem dev 2097152 --config   # raises <memory>
$ virsh -c qemu:///system setmem   dev 2097152 --config    # sets what the next boot gets
$ virsh -c qemu:///system start dev && ssh ubuntu@dev grep\ MemTotal /proc/meminfo
MemTotal: 2009936 kB
```

`setmaxmem` alone is a trap too: it moves the ceiling and leaves `<currentMemory>` behind, so
the guest boots with the *old* amount of RAM while `virsh dumpxml --inactive` reports the new
`<memory>`. Half the fix, and it looks right. The mirror-image trap is lowering RAM, where
`setmem --config` succeeds and leaves `<memory>` high — lesson 010 §6.

`vol-resize` has the opposite shape: `--grow` does not exist, `--shrink` does, and the safe
default is on by itself —

```
$ virsh -c qemu:///system vol-resize dev_vda.qcow2 --pool vm-pool --capacity 8G
error: invalid argument: Can't shrink capacity below current capacity unless shrink flag explicitly specified
$ virsh -c qemu:///system vol-resize dev_vda.qcow2 --pool vm-pool --capacity 16G   # worked
```

so the "defence in depth" flag the code was carrying bought nothing. (The planner refuses a
smaller `--disk` upstream as well.)

## How to keep this out of the codebase

- **Run the exact command line** the code will build, on a host, *before* writing the
  `_run([...])` call. `virsh <cmd> --help` prints the real option list in the SYNOPSIS block.
- **The integration suite is not optional for a libvirt-touching feature.** `check.sh` cannot
  see this class of bug: it runs lint, unit tests and a CLI smoke test, none of which ever
  reaches libvirt (see lesson 011 for why the unit suite must not either). Both bugs here
  survived a green `./scripts/check.sh --all`.
- **Pin the argv.** `tests/unit_test.py` stubs `vz._run`/`vz._spawn`, records the argv of the
  ceiling/volume calls, and asserts the exact flags. That fails the moment anyone writes a
  flag that does not exist, without needing a host.
- When a command errors with "doesn't support option", the fix is usually *another command*,
  not another flag.
