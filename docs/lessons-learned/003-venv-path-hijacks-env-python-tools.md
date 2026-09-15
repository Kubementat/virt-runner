# Bug 003 — `uv run` venv hijacks `#!/usr/bin/env python3` tools spawned by virt-runner

| | |
|---|---|
| **Severity** | High (`create` fails with `vm-create-failed` on Ubuntu 26.04 hosts/guests) |
| **Component** | `Virtualizer` subprocess helpers (`_spawn`, `_passthrough`) |
| **Discovered** | 2026-09-15 (first-time-setup test inside a fresh VM) |
| **Fixed** | 2026-09-15 |
| **Status** | Fixed and verified (nested create inside a virt-runner VM) |

---

## 1. Symptom

Everything passed inside a freshly created Ubuntu guest — `install-prerequisites.sh`,
`uv sync`, `virt-runner list` — but `virt-runner create` died at stage `create`:

```
File "/usr/bin/virt-install", line 5, in <module>
    from virtinst import virtinstall
ModuleNotFoundError: No module named 'gi'
```

`python3-gi` **was installed** and `python3 -c "import gi"` worked in the same
shell. On the developer's host (older virt-install) the same flow never failed.

## 2. Root cause

`uv run` prepends the project venv's `bin/` to `PATH`. The Ubuntu 26.04
`/usr/bin/virt-install` uses a **`#!/usr/bin/env python3`** shebang, so it
resolved to the venv's *isolated* python — which cannot see
`/usr/lib/python3/dist-packages`, where `gi` lives. The host's older
virt-install uses `#!/usr/bin/python3` (absolute path) and was unaffected —
which is why only fresh environments reproduced it.

Any Python-based host tool with an `env` shebang, spawned from a venv-run
parent, breaks this way — the failure lands in a *child's* traceback, far from
the PATH manipulation that caused it.

## 3. Fix

`virtualizer.py` computes `_child_env()` once: when running inside a venv
(`sys.prefix != sys.base_prefix`), it strips `sys.prefix/bin` from `PATH` and
passes that environment to every subprocess (`_spawn`, `_passthrough`).
Spawned host tools therefore always resolve interpreters from the *host* PATH.

## 4. Lessons

- A venv is an isolation boundary that **leaks through `PATH`** into every
  spawned process; tools with `env python` shebangs silently run inside it.
- Absolute-path shebangs mask this bug — "works on my machine" often means
  "my distro version happened to avoid the shebang". Test first-time setup in
  a fresh environment of the target release.
- Test your tool inside an artifact it produced (nested VM): fresh guest +
  fresh packages is exactly what real first-time users get.
