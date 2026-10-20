# virt-runner

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![CI](https://github.com/Kubementat/virt-runner/actions/workflows/ci.yml/badge.svg)

**One command to a running, SSH-reachable Ubuntu, Arch, or Fedora KVM guest — via libvirt + cloud-init.**

`virt-runner create myvm` downloads (once, then caches) and verifies an Ubuntu cloud image,
boots it, injects an SSH key (generated automatically when absent), waits for the DHCP lease,
proves SSH works with a real round-trip, and prints copy-paste-ready access commands.
`destroy` removes VM and disk. `list` shows what the tool created. `ssh NAME` drops you
straight into a shell on the VM. Every command also speaks JSON via `--json`.

Python (stdlib + `click`); all host interaction via `virsh`, `virt-install`, `curl`, `ssh`.
No libvirt bindings, no prompts, no sudo at run time.

## Install

Installing the tool and preparing the host are two steps, because a Python package cannot install
KVM, configure libvirt, or add you to a group. Do them in this order — the first needs no
privileges at all.

### 1. The tool

```bash
uv tool install virt-runner        # one time, then `virt-runner` is on PATH
# ...or straight from the repository:
uv tool install "virt-runner @ git+https://github.com/Kubementat/virt-runner.git"
```

Every example below is written for the installed command. Working from a source checkout instead,
run `uv sync` once and prefix the examples with `uv run` — everything else is identical.

### 2. Host prerequisites (once per machine, needs sudo)

```bash
virt-runner setup --check   # audit: what is missing? changes nothing
virt-runner setup           # install + configure, then LOG OUT AND BACK IN (groups)
```

`setup` runs the `install-prerequisites.sh` that is bundled inside the wheel — so the version always
matches the tool you installed. It installs KVM, libvirt, `virt-install`, `virtiofsd`, `ovmf`,
`xorriso`, `jq` and friends, defines/autostarts the `vm-pool` storage pool and the `default` network,
and adds your user to the `libvirt` and `kvm` groups so `virsh` needs no sudo. It is idempotent,
and `setup --help` shows its flags: `--check` audits, `--with-virt-manager` also installs the GUI.
`VIRT_USERNAME` / `VIRT_POOL_NAME` / `VIRT_POOL_DIR` / `VIRT_NET_NAME` are read from the environment.

Prefer to run the script yourself, or have no tool installed yet? It is also a plain bash file:

```bash
# from a checkout
./install-prerequisites.sh --check
# or fetch it, read it, then run it — you are about to sudo it
curl -fsSL https://raw.githubusercontent.com/Kubementat/virt-runner/main/install-prerequisites.sh \
  -o /tmp/ip.sh && less /tmp/ip.sh && sudo bash /tmp/ip.sh
```

**Host OS support:** the installer is Debian/Ubuntu only (`apt-get`). On other hosts, install the
equivalents of: qemu with the KVM accelerator + `qemu-img`, `libvirtd` + `virsh`, `virt-install`,
`virtiofsd`, UEFI/`ovmf` firmware, `xorrisofs`, `uuidgen`, `ssh`, `curl`, `jq`, and `cpu-checker`
(`kvm-ok`) — then create the `vm-pool` dir pool and the `default` NAT network yourself, active and
autostarted, and add your user to the `libvirt` and `kvm` groups. The tool's preflight then confirms
whether you got it right. Guest distros are unrelated: you can run Arch or Fedora guests on an
Ubuntu host.

### One-liner without installation

`uvx virt-runner create myvm` runs the tool ephemerally, but it still needs a prepared host — run
`uvx virt-runner setup` first if `--check` has not been done. Python ≥ 3.10 and `curl`, `ssh`,
`virsh`, `virt-install` on `PATH`; `virtiofsd` only for `--mount`. Anything missing is reported by
the preflight with an explicit error code before your VM is touched.

## Quick start

```bash
# 0. Host setup — once per machine: `virt-runner setup` (see [Install](#install))

# 1. Create a VM (~2 min cold, ~20 s on a warm image cache)
virt-runner create myvm

#    ...or an Arch Linux guest (official arch-boxes cloud image, user `arch`)
virt-runner create myvm --distro arch

#    ...or a Fedora guest (Fedora Cloud Base, user `fedora`)
virt-runner create myvm --distro fedora

# 2. SSH in
virt-runner ssh myvm

# 3. Done
virt-runner destroy myvm
```

Requirements: the host setup from [Install](#install) — a KVM host where `virsh` works without
sudo, an active `vm-pool` storage pool and `default` network, and `virt-install` ≥ 4.1.

## Commands

```bash
virt-runner create NAME          # build + boot; --distro (ubuntu|arch|fedora),
                                 # --ram/--vcpu GiB/ count, --disk GiB,
                                 # --release (default per-distro), --image URL, --user,
                                 # --ssh-key, --mount HOST[:GUEST] (repeatable; virtiofs share),
                                 # --script FILE (repeatable; runs in the guest after boot),
                                 # --no-boot, --keep-going
virt-runner destroy NAME         # undefine + delete disk (idempotent)
virt-runner list                 # only VMs whose disk lives in vm-pool
virt-runner ssh NAME             # interactive shell (resolves IP, uses the injected key;
                                 # picks up the user recorded at create, --user to override)
virt-runner setup [--check]      # host prerequisites — the bundled installer
                                 # (--with-virt-manager also installs the GUI)
```

Cached `latest`/`current` images are never refreshed — delete
`~/vm-images/<distro>/<release>/` to pick up a newer build.

All four VM commands accept `--json`: stdout carries **exactly one** JSON document (success *or*
error envelope), everything else goes to stderr. `VM_JSON_TRACE=1` re-sends progress lines to
stderr for debugging. Envelope and error codes: [JSON output](#json-output).

`setup` has no alias and no `--json`: it is not a VM command, it is a privileged host operation.

Short aliases for the VM commands: `c` = `create`, `d` = `destroy`, `ls` = `list`, `s` = `ssh`
(identical flags, exit codes and JSON `tool` ids).

```bash
virt-runner create myvm --json | jq -r .vm.ssh_command
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
  `create`, `wait-ip`, `ssh-verify`, `script`).
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
| `unsupported-arch` | Host arch is not x86_64 or aarch64 |
| `vm-already-defined` | A domain with the given name already exists |
| `vm-not-defined` | No such domain (destroy/ssh of a missing VM) |
| `vm-not-running` | The domain exists but is not running |
| `no-ip` | No DHCP lease/address found for the VM |
| `ssh-key-missing` | No usable key at `--ssh-key` and none could be generated |
| `image-download-failed` | The image (or its directory listing) could not be fetched |
| `image-verification-failed` | SHA256 mismatch or missing checksum entry |
| `image-no-cache` | `--keep-going` with a failed download/verify and no cached image |
| `image-arch-unavailable` | The distro profile has no image for the host arch (e.g. `--distro arch` on aarch64 — arch-boxes is x86_64-only) |
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
| `script-failed` | A `--script` exited non-zero (the VM is kept running) |

## Shared directories

Use `--mount HOST[:GUEST]` to share a host directory into the guest via virtiofs.
`HOST` is expanded (`~`), resolved to an absolute path, and must be an existing
directory. `GUEST` defaults to `/mnt/<basename of HOST>` and must be an absolute path
with components matching `[A-Za-z0-9._-]` (no `.` or `..` component, no spaces,
and no commas in the host path — virt-install splits sub-options on commas).

The mount is ready as soon as `create` returns (cloud-init `bootcmd` runs before
sshd). It persists across guest reboots: the first boot also writes a `nofail`
fstab entry and disables cloud-init for later boots (the NoCloud seed stays
attached, so without that flag cloud-init would re-run on every boot) —
fstab is what re-mounts the share then. It is read-write; UIDs pass straight through
(guest user `ubuntu`/`arch`/`fedora` is uid 1000, matching the usual host user).
Files created by root in the guest are root-owned on the host.

`destroy` never touches the host directory. The virtiofsd daemon exits when the domain
terminates.

Out of scope: read-only mounts, adding/removing mounts on an existing VM, showing
mounts in `list`, UID/GID remapping, and older hosts where virtiofsd lives at
`/usr/lib/qemu/virtiofsd` (Ubuntu 22.04).

## Provisioning scripts

Use `--script FILE` to run a host-side script in the guest once `create` has verified
SSH, e.g. to pre-install software so the VM is ready to use. Repeatable; scripts run
in the given order and the first non-zero exit stops the run.

- Runs as the cloud user (`--user`), who has passwordless `sudo` — use `sudo` for
  package installs. stdin is `/dev/null`, so pass `-y` to package managers.
- Waits for `cloud-init status --wait` first (package-manager locks, `--mount` shares).
- Interpreter: a `#!` line wins; otherwise `.py` runs under `python3`, anything else
  under `bash`. The Arch image ships no `python3`.
- Output streams to stderr (also under `--json`). The JSON document carries
  `scripts: [{path, exit_code}]` (empty when none).
- A failing script exits `1` with `script-failed` (stage `script`); the VM stays up
  for debugging via `virt-runner ssh NAME`. `--script` with `--no-boot` is a usage error.

```bash
virt-runner create dev --script setup.sh --script configure.py
```

## Integration test

`tests/integration_test.py` is an end-to-end suite against the real host: creates
four VMs (two Ubuntu, one Arch, one Fedora; the Arch leg is skipped on aarch64
hosts — arch-boxes ships x86_64-only images), lists them, runs a hello world via SSH in each (using
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
is **MiB** in virt-install, the NoCloud seed ISO must survive the guest's first boot
(virt-install's own `--cloud-init` ISO does not — its exit cleanup deletes it, which
loops aarch64 UEFI in `Reset System`), and aarch64 VMs must be created with Secure Boot
disabled (Fedora's aarch64 image ships an unsigned GRUB — the Secure Boot firmware
build rejects it with a silent `Security Violation` dialog: no kernel, no lease, an
empty `virsh console`).

Read next: [`docs/`](docs/README.md) — architecture diagrams, lessons learned, and the
[contributing guide](CONTRIBUTING.md).

## Releases

Releases are tag-based: an annotated `vX.Y.Z` tag, a changelog section, and a version bump in the
same commit, then published to PyPI by
[`release.yml`](.github/workflows/release.yml). The current release is whatever
`virt-runner --version` reports; see [CHANGELOG.md](CHANGELOG.md) for what changed.

Semver maps onto what you can observe: a **major** release breaks the CLI or the `--json` contract,
a **minor** release adds a flag, subcommand, error code, or JSON key, and a **patch** release only
fixes things. Breaking changes therefore land on a major version only.

## Contributing & support

- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, the two test suites, branch model, releases.
- [SECURITY.md](SECURITY.md) — how to report a vulnerability, and which security trade-offs the tool
  makes deliberately (host-key checking, image verification, shared keys, `--mount`).
- Issues: https://github.com/Kubementat/virt-runner/issues
- License: [MIT](LICENSE), Copyright (c) 2026 Julian Weber.

## Agent skill

[`skills/virt-runner-cli/`](skills/virt-runner-cli/SKILL.md) is an agent skill that teaches
coding agents to use virt-runner well: every flag, the `--json` contract and error codes, common
workflows, and safety rules (for example, only destroy VMs you created). To install it, link it
into your skills directory with `ln -s "$PWD/skills/virt-runner-cli" ~/.claude/skills/` (or into a
project's `.claude/skills/`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `libvirt not reachable` | Not in `libvirt`/`kvm` groups in this shell → re-login, then `virt-runner setup --check` (see [Install](#install)) |
| `pool 'vm-pool' is not active` / `network 'default' is not active` | `virsh pool-start vm-pool && virsh net-start default` |
| `no DHCP lease after 120s` / `SSH … not reachable yet` | `virsh console NAME` (Ctrl-`]` to detach) to watch the guest; for SSH make sure you use the key the VM was given: `ssh -i ~/.ssh/virt_runner_key ubuntu@IP` (`virt-runner ssh NAME` picks up the right user automatically; for Arch that is `arch`, for Fedora that is `fedora`) |
| `SHA256 mismatch` | Corrupt cache: delete `~/vm-images/<release>/` and retry |
| disk survives `destroy` | `virsh vol-delete NAME_vda.qcow2 vm-pool` |
