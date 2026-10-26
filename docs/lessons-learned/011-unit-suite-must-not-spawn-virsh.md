# 11. The pure-logic suite crashed on hosts without a libvirt client

Discovered while re-running the pipeline gate for the `modify` feature: `uv run tests/unit_test.py`
died with

```
FileNotFoundError: [Errno 2] No such file or directory: 'virsh'
```

inside `Virtualizer.preflight_check()` → `require_libvirt()` → `_spawn(["virsh", "list"])`.

## Why it is a real bug, not just a missing package

`tests/unit_test.py` is documented as the seconds-fast, **no KVM** suite (`AGENTS.md`, and
`scripts/check.sh` runs it as the "unit tests" step on any machine). Calling a real
`preflight_check()` breaks that promise in two ways:

1. `subprocess.run(["virsh", …])` raises `FileNotFoundError` when the binary is absent — not
   `VirtError` — so the `except VirtError` around it does not catch it and the whole suite dies
   before any other check reports.
2. Even where `virsh` exists, the assertion depended on host state ("any later failure is a
   different code — or nothing at all"), so the same file passed on a provisioned host and failed
   or silently skipped meaning on another.

## Fix

Stub `virt_runner.virtualizer._spawn` for the arch-gate cases. Both `require_libvirt()` and the
pool/network tables are reached through `_spawn` (`_stdout()` is `_spawn().stdout`), so one stub
carries the whole check:

```python
vz._spawn = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
```

With an empty pool table the aarch64 case now fails deterministically at `pool-not-active`, which
proves exactly what the test wanted to prove (the arch gate let it through) without touching a
hypervisor. Verify the suite stays hermetic by running it with a PATH that has no `virsh` /
`virt-install`:

```bash
mkdir -p /tmp/minbin && for f in /usr/bin/*; do case "$(basename $f)" in virsh|virt-*) continue;; esac; ln -sf "$f" /tmp/minbin/; done
env PATH=/tmp/minbin:$HOME/.local/bin uv run tests/unit_test.py
```

Rule of thumb: in this suite, patch the module-level `_spawn` / `_passthrough` seam before
asserting on behaviour that only *shape-checks* a `virsh` call; leave real spawns to
`tests/integration_test.py`.
