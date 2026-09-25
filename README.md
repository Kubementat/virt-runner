# virt-runner

**One command to a running, SSH-reachable Ubuntu, Arch, or Fedora KVM guest — via libvirt + cloud-init.**

`virt-runner create myvm` downloads (once, then caches) and verifies an Ubuntu cloud image,
boots it, injects an SSH key (generated automatically when absent), waits for the DHCP lease,
proves SSH works with a real round-trip, and prints copy-paste-ready access commands.
`destroy` removes VM and disk. `list` shows what the tool created. `ssh NAME` drops you
straight into a shell on the VM. Every command also speaks JSON via `--json`.

Python (stdlib + `click`); all host interaction via `virsh`, `virt-install`, `curl`, `ssh`.
No libvirt bindings, no prompts, no sudo at run time.

## Quick start

```bash
# 0. One-time host setup (KVM, libvirt, groups, vm-pool, default network; re-runnable)
./install-prerequisites.sh      # then LOG OUT AND BACK IN (group membership)

# 1. Create a VM (~2 min cold, ~20 s on a warm image cache)
uv run virt-runner create myvm

#    ...or an Arch Linux guest (official arch-boxes cloud image, user `arch`)
uv run virt-runner create myvm --distro arch

#    ...or a Fedora guest (Fedora Cloud Base, user `fedora`)
uv run virt-runner create myvm --distro fedora

# 2. SSH in
uv run virt-runner ssh myvm

# 3. Done
uv run virt-runner destroy myvm
```

Requirements: KVM host with `virsh` usable without sudo, active `vm-pool` storage pool and
`default` network, `virt-install` ≥ 4.1, Python ≥ 3.10 with [`uv`](https://docs.astral.sh/uv/).
`install-prerequisites.sh` sets all of it up; `--check` audits only. The tool's preflight
reports any missing piece with an explicit error. `virtiofsd` (only needed for `--mount`)
is also installed by `install-prerequisites.sh`.

## Commands

```bash
uv run virt-runner create NAME   # build + boot; --distro (ubuntu|arch|fedora),
                                 # --ram/--vcpu GiB/ count, --disk GiB,
                                 # --release (default per-distro), --image URL, --user,
                                 # --ssh-key, --mount HOST[:GUEST] (repeatable; virtiofs share),
                                 # --no-boot, --keep-going
uv run virt-runner destroy NAME  # undefine + delete disk (idempotent)
uv run virt-runner list          # only VMs whose disk lives in vm-pool
uv run virt-runner ssh NAME      # interactive shell (resolves IP, uses the injected key;
                                 # picks up the user recorded at create, --user to override)
```

Cached `latest`/`current` images are never refreshed — delete
`~/vm-images/<distro>/<release>/` to pick up a newer build.

All four accept `--json`: stdout carries **exactly one** JSON document (success *or* error
envelope), everything else goes to stderr. `VM_JSON_TRACE=1` re-sends progress lines to
stderr for debugging. Envelope and error codes: [JSON output](#json-output).

```bash
uv run virt-runner create myvm --json | jq -r .vm.ssh_command
```

## JSON output

``--json`` makes stdout carry **exactly one** JSON document, the result envelope:

```json
{"tool": "vm-create", "version": "…", "status": "success", "error": null, "vm": {…}, "image": {…}}
{"tool": "vm-create", "version": "…", "status": "error", "error": {"code": "…", "message": "…"}}
```

- `tool` — the command: `vm-create`, `vm-destroy`, `vm-list`, `vm-ssh`.
- `version` — installed package version.
- `status` — `success` or `error`.
- `error` — `null` on success, else `{code, message}` plus `stage` when the
  failure belongs to a `create` stage (`preflight`, `image`, `cloud-init`,
  `create`, `wait-ip`, `ssh-verify`).
- The remaining top-level keys are the command's sections (`vm`, `image`,
  `vms`, …) — the same state the text report prints.

Exit codes never change with `--json`: `0` success, `1` runtime or preflight
failure, `2` usage error.

### Error codes

| Code | Meaning |
|---|---|
| `usage` | Invalid command line (flag, VM name, or `--user` value) |
| `runtime-error` | Uncaught failure without a more specific code |
| `libvirt-unreachable` | `virsh` cannot talk to `libvirtd` |
| `pool-not-active` | Storage pool `vm-pool` is not active |
| `pool-not-found` | Storage pool `vm-pool` is not defined |
| `pool-path-unknown` | The pool's directory path could not be determined |
| `network-not-active` | The `default` network is not active |
| `unsupported-arch` | Host is not x86_64 (images are x86_64 only) |
| `vm-already-defined` | A domain with the given name already exists |
| `vm-not-defined` | No such domain (destroy/ssh of a missing VM) |
| `vm-not-running` | The domain exists but is not running |
| `no-ip` | No DHCP lease/address found for the VM |
| `ssh-key-missing` | No usable key at `--ssh-key` and none could be generated |
| `image-download-failed` | The image (or its directory listing) could not be fetched |
| `image-verification-failed` | SHA256 mismatch or missing checksum entry |
| `image-no-cache` | `--keep-going` with a failed download/verify and no cached image |
| `cloud-init-failed` | The cloud-init user/meta-data files could not be generated |
| `volume-create-failed` | `virsh vol-create-as` failed |
| `volume-import-failed` | `virsh vol-upload` failed |
| `volume-resize-failed` | `virsh vol-resize` failed |
| `vm-create-failed` | `virt-install` failed (domain and volume removed) |
| `vm-destroy-failed` | `virsh destroy`/`undefine` failed during teardown |
| `volume-delete-failed` | `virsh vol-delete` failed during teardown |
| `lease-timeout` | No DHCP lease within the wait window |
| `ssh-timeout` | The SSH round-trip never succeeded within the wait window |
| `virtiofsd-missing` | `--mount` given but `/usr/libexec/virtiofsd` is not installed |

## Shared directories

Use `--mount HOST[:GUEST]` to share a host directory into the guest via virtiofs.
`HOST` is expanded (`~`), resolved to an absolute path, and must be an existing
directory. `GUEST` defaults to `/mnt/<basename of HOST>` and must be an absolute path
with components matching `[A-Za-z0-9._-]` (no `.` or `..` component, no spaces,
and no commas in the host path — virt-install splits sub-options on commas).

The mount is ready as soon as `create` returns (cloud-init `bootcmd` runs before
sshd). It persists across guest reboots: the first boot also writes a `nofail`
fstab entry, because the NoCloud seed is only attached for the first boot and
some images (e.g. Ubuntu 26.04) disable cloud-init entirely on later boots —
fstab is what keeps the mount alive then. It is read-write; UIDs pass straight through
(guest user `ubuntu`/`arch`/`fedora` is uid 1000, matching the usual host user).
Files created by root in the guest are root-owned on the host.

`destroy` never touches the host directory. The virtiofsd daemon exits when the domain
terminates.

Out of scope: read-only mounts, adding/removing mounts on an existing VM, showing
mounts in `list`, UID/GID remapping, and older hosts where virtiofsd lives at
`/usr/lib/qemu/virtiofsd` (Ubuntu 22.04).

## Integration test

`tests/integration_test.py` is an end-to-end suite against the real host: creates
four VMs (two Ubuntu, one Arch, one Fedora), lists them, runs a hello world via SSH in each (using
the reported `ssh_command`), opens a `ssh <name>` session into each, destroys them all,
and verifies they're gone. Every call goes through `--json`, so it also enforces that
stdout stays valid JSON at every step.

```bash
uv run tests/integration_test.py
```

It creates and destroys real VMs (`it-vm-a`, `it-vm-b`, `it-vm-arch`, `it-vm-fedora`) and takes a few
minutes. It cleans
up its own leftovers before starting and ignores VMs it doesn't own.

## Development

```bash
uv sync                 # create .venv (dev group adds ruff)
uv run ruff check .     # lint
uv run ruff format .    # format (line length 88, target py310)
uv run tests/unit_test.py    # pure-logic checks (seconds, no KVM)
```

Layout: `cli → cmd_* → virtualizer → core (output, errors)`. Only
[`src/virt_runner/virtualizer.py`](src/virt_runner/virtualizer.py) touches the host; text and
JSON render from one run-state dict. Exit codes: `0` success, `1` runtime, `2` usage — with
stable `error.code` values (full table: [JSON output](#json-output)).

Gotchas that bite when touching the create stage (full list: `docs/lessons-learned/`):
`vol-upload` implicitly resizes the volume (a `vol-resize` after it is mandatory), `--ram`
is **MiB** in virt-install, and `disable=on` on `--cloud-init` is mandatory.

Read next: [`docs/e2e-acceptance.md`](docs/e2e-acceptance.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `libvirt not reachable` | Not in `libvirt`/`kvm` groups in this shell → re-login; `./install-prerequisites.sh --check` |
| `pool 'vm-pool' is not active` / `network 'default' is not active` | `virsh pool-start vm-pool && virsh net-start default` |
| `no DHCP lease after 120s` / `SSH … not reachable yet` | `virsh console NAME` (Ctrl-`]` to detach) to watch the guest; for SSH make sure you use the key the VM was given: `ssh -i ~/.ssh/virt_runner_key ubuntu@IP` (`virt-runner ssh NAME` picks up the right user automatically; for Arch that is `arch`, for Fedora that is `fedora`) |
| `SHA256 mismatch` | Corrupt cache: delete `~/vm-images/<release>/` and retry |
| disk survives `destroy` | `virsh vol-delete NAME_vda.qcow2 vm-pool` |
