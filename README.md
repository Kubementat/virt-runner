# virt-runner

**One command to a running, SSH-reachable Ubuntu KVM guest — via libvirt + cloud-init.**

`virt-runner create myvm` downloads (once, then caches) and verifies an Ubuntu cloud image,
boots it, injects an SSH key (generated automatically when absent), waits for the DHCP lease,
proves SSH works with a real round-trip, and prints copy-paste-ready access commands.
`destroy` removes VM and disk. `list` shows what the tool created. Every command also
speaks JSON via `--json`.

Python (stdlib + `click`); all host interaction via `virsh`, `virt-install`, `curl`, `ssh`.
No libvirt bindings, no prompts, no sudo at run time.

## Quick start

```bash
# 0. One-time host setup (KVM, libvirt, groups, vm-pool, default network; re-runnable)
./install-prerequisites.sh      # then LOG OUT AND BACK IN (group membership)

# 1. Create a VM (~2 min cold, ~20 s on a warm image cache)
uv run virt-runner create myvm

# 2. SSH in — use the command the create output prints
ssh -i ~/.ssh/virt_runner_key ubuntu@192.168.122.x

# 3. Done
uv run virt-runner destroy myvm
```

Requirements: KVM host with `virsh` usable without sudo, active `vm-pool` storage pool and
`default` network, `virt-install` ≥ 4.1, Python ≥ 3.10 with [`uv`](https://docs.astral.sh/uv/).
`install-prerequisites.sh` sets all of it up; `--check` audits only. The tool's preflight
reports any missing piece with an explicit error.

## Commands

```bash
uv run virt-runner create NAME   # build + boot; --ram/--vcpu GiB/ count, --disk GiB,
                                 # --release (default resolute), --image URL, --user,
                                 # --ssh-key, --no-boot, --keep-going
uv run virt-runner destroy NAME  # undefine + delete disk (idempotent)
uv run virt-runner list          # only VMs whose disk lives in vm-pool
```

All three accept `--json`: stdout carries **exactly one** JSON document (success *or* error
envelope), everything else goes to stderr. `VM_JSON_TRACE=1` re-sends progress lines to
stderr for debugging. Envelope schema: `specification/python-rewrite.md` §5.

```bash
uv run virt-runner create myvm --json | jq -r .vm.ssh_command
```

## Integration test

`tests/integration_test.py` is an end-to-end suite against the real host: creates two VMs,
lists them, runs a hello world via SSH in each (using the reported `ssh_command`), destroys
both, and verifies they're gone. Every call goes through `--json`, so it also enforces that
stdout stays valid JSON at every step.

```bash
uv run tests/integration_test.py
```

It creates and destroys real VMs (`it-vm-a`, `it-vm-b`) and takes a few minutes. It cleans
up its own leftovers before starting and ignores VMs it doesn't own.

## Development

```bash
uv sync                 # create .venv (dev group adds ruff)
uv run ruff check .     # lint
uv run ruff format .    # format (line length 88, target py310)
```

Layout: `cli → cmd_* → virtualizer → core (output, errors)`. Only
[`src/virt_runner/virtualizer.py`](src/virt_runner/virtualizer.py) touches the host; text and
JSON render from one run-state dict. Exit codes: `0` success, `1` runtime, `2` usage — with
stable `error.code` values (full table: spec §5.2).

Gotchas that bite when touching the create stage (full list: spec §13, `docs/lessons-learned/`):
`vol-upload` implicitly resizes the volume (a `vol-resize` after it is mandatory), `--ram`
is **MiB** in virt-install, and `disable=on` on `--cloud-init` is mandatory.

Read next: [`specification/python-rewrite.md`](specification/python-rewrite.md) (the binding
spec), [`docs/e2e-acceptance.md`](docs/e2e-acceptance.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `libvirt not reachable` | Not in `libvirt`/`kvm` groups in this shell → re-login; `./install-prerequisites.sh --check` |
| `pool 'vm-pool' is not active` / `network 'default' is not active` | `virsh pool-start vm-pool && virsh net-start default` |
| `no DHCP lease after 120s` / `SSH … not reachable yet` | `virsh console NAME` (Ctrl-`]` to detach) to watch the guest; for SSH make sure you use the key the VM was given: `ssh -i ~/.ssh/virt_runner_key ubuntu@IP` |
| `SHA256 mismatch` | Corrupt cache: delete `~/vm-images/<release>/` and retry |
| disk survives `destroy` | `virsh vol-delete NAME_vda.qcow2 vm-pool` |
