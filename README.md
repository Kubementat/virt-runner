# virt-runner

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

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
virt-runner modify NAME          # change an existing VM in place (see "Changing an existing VM")
                                 # sizing:   --vcpu N  --ram GiB  --disk GiB (grow only)
                                 # network:  --net SPEC  --net-add SPEC[,mac=..]  (repeatable)
                                 #           --net-remove MAC  (repeatable)  --mac MAC
                                 # control:  --live  --reboot  --wait  --no-auto-grow-fs  --plan
virt-runner ssh NAME             # interactive shell (resolves IP, uses the injected key;
                                 # picks up the user recorded at create, --user to override)
virt-runner setup [--check]      # host prerequisites — the bundled installer
                                 # (--with-virt-manager also installs the GUI)
```

Cached `latest`/`current` images are never refreshed — delete
`~/vm-images/<distro>/<release>/` to pick up a newer build.

All five VM commands accept `--json`: stdout carries **exactly one** JSON document (success *or*
error envelope), everything else goes to stderr. `VM_JSON_TRACE=1` re-sends progress lines to
stderr for debugging. Envelope and error codes: [JSON output](#json-output).

`setup` has no alias and no `--json`: it is not a VM command, it is a privileged host operation.

Short aliases for the VM commands: `c` = `create`, `d` = `destroy`, `ls` = `list`,
`s` = `ssh`, `m` = `modify` (identical flags, exit codes and JSON `tool` ids).

```bash
virt-runner create myvm --json | jq -r .vm.ssh_command
```

## JSON output

``--json`` makes stdout carry **exactly one** JSON document, the result envelope:

```json
{"tool": "vm-create", "version": "…", "status": "success", "error": null, "vm": {…}, "image": {…}}
{"tool": "vm-create", "version": "…", "status": "error", "error": {"code": "…", "message": "…"}}
```

- `tool` — the command: `vm-create`, `vm-destroy`, `vm-list`, `vm-modify`, `vm-ssh`.
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
| `vm-not-defined` | No such domain (destroy/ssh/modify of a missing VM) |
| `not-a-virt-runner-vm` | The domain exists but virt-runner did not create it (`modify`) |
| `modify-noop` | `modify` where every given option already matches the guest **and** its config (exit 2) |
| `disk-shrink-unsupported` | `modify --disk` smaller than the current disk |
| `net-change-failed` | `modify` NIC attach/detach failed — check `virsh domiflist NAME` before retrying |
| `vm-reboot-failed` | `modify --reboot` could not bring the domain back up (changes stay persisted) |
| `runtime-error` from `modify --ram` | the guest is bigger than the host's RAM — libvirt would write it, the guest would not start, nothing was changed |
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
| `volume-resize-failed` | `virsh vol-resize` failed (create, or growing the volume of a powered-off guest) |
| `vm-create-failed` | `virt-install` failed (domain and volume removed) |
| `vm-destroy-failed` | `virsh destroy`/`undefine` failed during teardown |
| `volume-delete-failed` | `virsh vol-delete` failed during teardown |
| `lease-timeout` | No DHCP lease within the wait window |
| `ssh-timeout` | The SSH round-trip never succeeded within the wait window |
| `virtiofsd-missing` | `--mount` given but `/usr/libexec/virtiofsd` is not installed |
| `script-failed` | A `--script` exited non-zero (the VM is kept running) |

## Changing an existing VM (`modify`)

`virt-runner modify NAME` changes a VM virt-runner created instead of destroying and recreating
it: vCPU count, RAM, disk size (grow), and the network interfaces. It takes at least one change
option; they are all composable in a single call.

```bash
virt-runner modify myvm --vcpu 4 --ram 8 --disk 40 --reboot --wait   # resize, reboot, wait for SSH
virt-runner modify myvm --plan --vcpu 4 --json | jq .changes         # what would change, nothing touched
virt-runner modify myvm --net-add mgmt                               # live NIC on network `mgmt`
virt-runner modify myvm --net-remove 52:54:00:12:34:56               # and later, by MAC
```

### When the guest sees it: `effective`

libvirt keeps a **running** domain and its **persisted config** apart, and the two do not change
with the same call. So every entry in `changes[]` says which one moved:

| `effective` | Means |
|---|---|
| `"now"` | the running guest (or its block device) changed — no reboot needed |
| `"on-boot"` | written to the persisted config; the guest applies it at the next boot |

A live change that libvirt refuses is **not** an error: the change lands in the config, reports
`on-boot`, and comes with a warning. Nothing is silently half-applied. `--reboot` (optionally
with `--wait`) closes the gap, and afterwards the changes report `now`. `--reboot` on its own is
also a valid request — it is how you make changes that are already sitting in the config
effective.

**What `modify` compares a request to is the config, not the running guest.** A running domain is
two documents, and they can disagree (a config-only change nobody ever rebooted into, or a
`virsh setmem --live` done by someone holding the client). Three answers, in order:

| The state | `modify` says |
|---|---|
| guest **and** config already match | `modify-noop` (exit 2) |
| config matches, guest does not | `on-boot` change with **no** libvirt call — the only thing missing is a reboot |
| config disagrees | the write you asked for, even if the running guest already looks right, with the config's value as `from` |

The last row matters: answering "already set" to a domain whose config reserves something else is
how a tool tells you everything is fine about a VM that fails on its next boot.

| Axis | What it does | Live (`--live`) | Config |
|---|---|---|---|
| `--vcpu N` | `virsh setvcpus` | hot-plug up to the domain's `<vcpu max>` | always written |
| `--ram GiB` | `virsh setmaxmem` (ceiling) + `virsh setmem` | balloon, but never above `<memory>` | always written |
| `--disk GiB` | `virsh blockresize`, or the pool volume when the guest is off | yes — the block device grows | volume is the truth |
| `--net` / `--net-add` / `--net-remove` / `--mac` | `attach-interface` / `detach-interface` | yes, when the guest runs | also written |

Growing **past** the domain's ceiling (a VM created with `--vcpu 1` that should get 4 vCPUs,
RAM beyond its `<memory>`) is a two-step write whose ceiling only becomes effective with the
next boot — so it reports `on-boot` no matter what `--live` you pass. A RAM **shrink** can be
live; a RAM **grow** usually cannot.

A RAM shrink also has to bring `<memory>` **down** again: `setmem --config` moves
`<currentMemory>` and leaves the old, bigger total, which is exactly what qemu reserves on the
next boot — libvirt writes it, so does nothing to stop you, and the guest simply fails to start.
`modify` writes both halves. It also refuses a `--ram` above the host's installed RAM
(`runtime-error`, no change): libvirt would accept that into the config without a word, and only
the boot would find out.

### Growing the disk — and the guest's filesystem

The block device grows first (`blockresize`, or `vol-resize` on the pool volume for a powered-off
guest; libvirt refuses to shrink a volume without an explicit opt-in, and `modify` never asks
for that). Then virt-runner grows the **guest's own filesystem** over SSH — `growpart` plus
`resize2fs` / `xfs_growfs` / `btrfs` — because a bigger disk is useless until the partition and
the filesystem move in. That step is best effort by contract, and the report says what happened:

- `fs_grown: true` — done, the root filesystem is bigger.
- `fs_grown: null` — not attempted (`--no-auto-grow-fs`, or the guest is powered off).
- `fs_grown: false` — attempted and refused by the guest, and the warning names the commands to
  run inside it. The block device stays grown; nothing rolls back.

The guest filesystem cannot grow when the guest does not run, so **re-issuing the same
`--disk GiB` is the documented retry** (it plans a filesystem-only grow, `from` equals `to`, and
is not a `modify-noop`). LVM layouts, a missing `growpart`, or an unbootable guest are reported,
never guessed at.

### Interfaces

- `--net SPEC` re-attaches the **primary** NIC: `network=NAME` for a libvirt virtual network,
  `bridge=NAME` for a host bridge. A bare name that is not a defined virtual network is attached
  as a bridge of that name — with a warning, because that device has to exist on the host.
- `--net-add SPEC` adds a NIC (`network=` / `bridge=`, optional `,mac=52:54:00:11:22:33`). It is
  repeatable. Adding a second NIC to the **same** network can make the guest lose SSH
  reachability on its old address (both NICs answer the same subnet) — attach extra NICs to a
  separate network or bridge.
- `--net-remove MAC` detaches one NIC by MAC, repeatable. Removing the **last** NIC is refused
  (`usage`, exit 2) — add the replacement with `--net-add` first, in the same call.
- `--mac MAC` rewires the primary NIC (detach + attach). The guest re-acquires a lease, so
  **expect a new address**: `virt-runner list` after the change is what tells you where it is now.

`--net-add`/`--net-remove`/`--mac`/`--net` change the MAC-level identity of a guest; anything you
run against its old address (ssh sessions, `known_hosts`, DNS entries) is yours to update.

### Refused, on purpose

| Request | Result |
|---|---|
| `--disk` smaller than the current size | `disk-shrink-unsupported` (exit 1) — recreate the VM for a smaller disk |
| every value given already matches the guest **and** its config | `modify-noop` (exit 2) |
| `--ram` above the host's installed RAM | `runtime-error` (exit 1) — libvirt would write it and the guest would not boot |
| `--net-remove` of the only/last NIC | `usage` (exit 2) |
| a domain virt-runner did not create | `not-a-virt-runner-vm` (exit 1) — managed with `virsh` by hand |
| a name that is not defined | `vm-not-defined` (exit 1) |
| nothing requested at all | `usage` (exit 2) |

`modify` never touches a domain that is not its own, never shrinks storage, and never leaves a
running guest without an interface. Everything it changes is visible in the `vm-modify` JSON
document: `vm` is the state **before** the change, `changes[]` is what moved, and `warnings[]`
carries every "only on next boot"/"do it by hand" note.

### Out of scope

Disk or volume shrinking, adding/removing `--mount` shares on an existing VM (destroy and
recreate — `modify` has no mount flags), renaming a domain, snapshots, changing the distro
image/release, `--net` types other than `network=`/`bridge=` (direct/user/ethernet NICs), the
CPU model or NUMA topology, and batch changes across several VMs (call it per name).

```bash
virt-runner modify myvm --json | jq '{changes: [.changes[].field], eff: [.changes[].effective]}'
```

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
the reported `ssh_command`), opens a `ssh <name>` session into each, exercises `modify` on one
running guest (plan, refusals, vCPU/RAM/disk with a reboot and an in-guest check, a NIC round
trip, the same change set on a powered-off guest), destroys them all,
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

`./scripts/check.sh` runs all of that plus a CLI smoke test, the package builds, and `shellcheck`
(`--all` adds the Python 3.10 floor). Run it before you push and before you tag — nothing else
checks your work for you.

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
same commit. Nothing is automated: `./scripts/check.sh` is the gate, and the upload is a
`uv publish` you run yourself. The current release is whatever `virt-runner --version` reports; see
[CHANGELOG.md](CHANGELOG.md) for what changed.

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
| `modify` reports `effective: on-boot` and the guest still shows the old size | expected for anything above the domain's ceiling (vCPU max, `<memory>`) or a powered-off guest: `virt-runner modify NAME --reboot --wait`, then check inside the guest (`nproc`, `MemTotal`, `lsblk`) |
| `modify` grew the disk but `df` did not move | the guest-side grow was best effort (LVM, or no `growpart`): run the commands from the warning inside the guest (`sudo growpart /dev/vda N`, `sudo resize2fs`/`xfs_growfs`), then `virt-runner modify NAME --disk <same-size>` to retry |
| `net-change-failed` after `--net`/`--net-add` | the target does not exist on the host: `virsh net-list --all` (virtual network) or `ip link` (bridge). The target is checked **before** anything is detached, so the guest keeps its NIC — but check `virsh domiflist NAME` anyway before you retry |
| the guest boots with a different amount of RAM/vCPUs than `virt-runner list` reports | the running domain and its config had drifted (`virsh dumpxml --inactive NAME` is what the next boot gets). Re-issue the `modify` for that axis: it compares against the config, writes what is missing, and reports `on-boot` |
| guest stopped answering SSH right after `--mac`/`--net` | it re-acquired a lease: `virt-runner list --json \| jq -r '.vms[] \| select(.name=="NAME") \| .ip'` and use the new address |
| `modify NAME` says the VM was not created by virt-runner | correct, if its disk is not in `vm-pool` or it carries no virt-runner metadata — that domain is not yours to resize |
