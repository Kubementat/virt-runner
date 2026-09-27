---
name: virt-runner-cli
description: |
  Expert use of the `virt-runner` CLI: one command to a running, SSH-reachable Ubuntu, Arch or Fedora KVM guest via libvirt (qemu:///system) + cloud-init, with `create`, `destroy`, `list` and `ssh` subcommands and a stable `--json` contract. Use when the user mentions virt-runner, wants to create, provision, list, SSH into or destroy throwaway/test KVM or libvirt VMs, spin up a clean Ubuntu/Arch/Fedora VM to test something, share host directories into a VM (virtiofs `--mount`), provision a VM with setup scripts (`--script`), run a multi-distro test matrix, script VM lifecycles with JSON/jq, or debug a failed VM create (lease/SSH timeouts, image download/verification, script failures).
compatibility: Requires an x86_64 KVM host with libvirt, virt-install >= 4.1, uv. Run from the repo as `uv run virt-runner ...`, or `virt-runner ...` when installed.
---

# virt-runner CLI

`virt-runner create NAME` downloads (once, then caches) and SHA256-verifies a cloud image,
boots it under `qemu:///system`, injects an SSH key (generated when absent), waits for the
DHCP lease, proves SSH with a real round-trip, optionally mounts host dirs and runs
provisioning scripts, and prints access commands. No prompts, no sudo at run time.

Invocation: from the repo checkout `uv run virt-runner <cmd>`; when installed `virt-runner <cmd>`.
Examples below use `virt-runner` for brevity.

References (load when needed):
- `references/json-and-errors.md` — JSON schema per command, every `error.code` with remedy, create stages.
- `references/troubleshooting.md` — failure playbook, host setup, gotchas from lessons-learned.

## Agent safety rules

1. **Prefer `--json`** for anything you parse. stdout is exactly one JSON document; branch on
   the exit code and `.status` / `.error.code`, never on human text.
2. **`destroy` is irreversible** — it force-stops, undefines and deletes the disk. Only destroy
   VMs *you* created in this task (track their names), or ones the user names explicitly.
   Never "clean up" unknown VMs from `list`.
3. **Use a unique, recognisable name prefix** for your VMs (e.g. `agent-<task>-ubuntu`) so they
   cannot collide with the user's VMs. Names must match `^[A-Za-z][A-Za-z0-9._-]*$`.
4. **A failed `create` may leave a VM behind.** If the JSON error document contains a `vm`
   section (stages `wait-ip`, `ssh-verify`, `script`), the domain exists and still needs
   `destroy`. `vm-create-failed` already removes domain + volume itself.
5. **Don't create/destroy VMs just to check help or syntax** — use `--help`.
6. VMs are defined with `--autostart`: a forgotten VM comes back after a host reboot. Tear down
   when done unless the user wants it kept.
7. `virt-runner ssh` is **interactive only** (no remote-command argument). For non-interactive
   commands use the `ssh_command` from `create --json` / `list --json` (see below).

## Prerequisites / preflight

One-time host setup (Ubuntu/Debian, apt, uses sudo internally, idempotent):

```bash
./install-prerequisites.sh          # KVM, libvirt, virt-install, virtiofsd, groups,
                                    # `default` NAT network + `vm-pool` dir pool (active+autostart)
./install-prerequisites.sh --check  # audit only, changes nothing
# then LOG OUT AND BACK IN (libvirt/kvm group membership)
```

`create` preflight checks (stage `preflight`): `virsh` reaches libvirtd without sudo, host is
x86_64, storage pool `vm-pool` defined and active, network `default` active, the VM name is not
already defined, `/usr/libexec/virtiofsd` exists (only with `--mount`), and an SSH key is usable.

SSH key: default public key `~/.ssh/virt_runner_key.pub`; private key = same path without `.pub`.
If absent, an ed25519 keypair without passphrase is generated automatically. A custom
`--ssh-key` path must end in `.pub` to be auto-generated.

Quick manual fixes: `virsh pool-start vm-pool && virsh net-start default`.

## Commands

### create NAME

| Flag | Default | Notes |
|---|---|---|
| `--distro ubuntu\|arch\|fedora` | `ubuntu` | Selects image, user, sudo group |
| `--release TEXT` | ubuntu `resolute`, arch `latest`, fedora `44` | Ubuntu: codename (e.g. `noble`); Fedora: release number; Arch: only `latest` (else usage error, use `--image`) |
| `--image URL` | — | Direct cloud-image URL (http/https/file). `--release` wins if both given. Verified against a same-dir `SHA256SUMS` if present, else verification `skipped` |
| `--ram N` | `4` | **GiB** (integer >= 1) |
| `--vcpu N` | `2` | |
| `--disk N` | `30` | GiB; guest root auto-grows |
| `--user NAME` | ubuntu `ubuntu`, arch `arch`, fedora `fedora` | Must match `[a-z_][a-z0-9_-]{0,31}`; passwordless sudo; recorded in domain metadata so `ssh`/`list` reuse it |
| `--ssh-key PATH` | `~/.ssh/virt_runner_key.pub` | Public key to inject; generated when absent |
| `--mount HOST[:GUEST]` | GUEST `/mnt/<basename HOST>` | Repeatable virtiofs read-write share (see below) |
| `--script FILE` | — | Repeatable provisioning script run in the guest (see below) |
| `--no-boot` | off | Define VM + disk but don't boot/wait. Incompatible with `--script` (usage error, exit 2) |
| `--keep-going` | off | On download/verify error continue with a cached image, else `image-no-cache` |
| `--json` | off | One JSON document on stdout |

Timing: ~2 min cold (image download), ~20 s with a warm cache. IP wait up to 120 s, SSH
verify up to 90 s. VM is reachable by IP and as `NAME.default`.

Pipeline stages (reported as `error.stage`): `preflight` → `image` → `cloud-init` → `create`
→ `wait-ip` → `ssh-verify` → `script`.

### list

`virt-runner list [--user NAME] [--json]` — only VMs whose disk lives in `vm-pool` (i.e.
created by virt-runner). `--user` overrides the user shown in the SSH command (default: per-VM
recorded user, else `ubuntu`). Empty list is success (`count: 0`). `ip`/`ssh_command` are
`null` unless `ip_status == "lease"` (others: `not-running`, `running-no-lease`, `no-mac`).

### ssh NAME

`virt-runner ssh NAME [--user NAME] [--json]` — interactive shell using the recorded user and
key; host-key checks off (`UserKnownHostsFile=/dev/null`, DHCP recycles IPs). Passes ssh's exit
code through. Errors: `vm-not-defined`, `vm-not-running`, `no-ip`. Under `--json` the session's
output goes to stderr and a summary document is printed at the end.

### destroy NAME

`virt-runner destroy NAME [--json]` — `virsh destroy` (if running) + `undefine` + delete volume
`NAME_vda.qcow2` from `vm-pool`. Already-absent volume is only a warning. A **missing domain is
an error** (`vm-not-defined`, exit 1) — treat that code as "already gone" for idempotent cleanup.
Never touches `--mount` host directories or the image cache.

## Shared directories (`--mount`)

- `HOST` is `~`-expanded, resolved absolute, must be an existing directory, no `,` in the path.
- `GUEST` must be absolute, components `[A-Za-z0-9._-]`, no `.`/`..`, unique per VM.
- Ready when `create` returns (cloud-init `bootcmd` before sshd); persists across guest reboots
  via a `nofail` fstab entry. Read-write; UIDs pass through (guest user uid 1000 = usual host
  user); files created by guest root are root-owned on the host.
- Needs `virtiofsd` at `/usr/libexec/virtiofsd` (else `virtiofsd-missing`).
- Not supported: read-only mounts, adding/removing mounts on existing VMs, mounts in `list`.

```bash
virt-runner create dev --mount ~/src/myproj              # -> /mnt/myproj
virt-runner create dev --mount ~/src/myproj:/work --mount /data/sets:/data
```

## Provisioning scripts (`--script`)

Runs a **host** file inside the guest over SSH after `ssh-verify`, in the order given; the first
non-zero exit stops the run.

- Runs as the cloud user (`--user`), who has **passwordless sudo** → use `sudo` for installs.
- Waits for `cloud-init status --wait` first (package-manager locks, mounts ready).
- Interpreter: a `#!` line wins; else `.py` → `python3`; anything else → `bash`.
  **The Arch image has no python3** — use bash scripts (or install python first) on Arch.
- stdin is `/dev/null` → always non-interactive: `apt-get install -y`, `dnf -y`, `pacman --noconfirm`,
  `DEBIAN_FRONTEND=noninteractive`.
- Output streams to **stderr** (also under `--json`); JSON gets `scripts: [{path, exit_code}]`
  (empty list when none).
- Failure → `script-failed` (stage `script`), exit 1, **VM kept running** for inspection.
- The file must exist on the host (else usage error, exit 2). It is copied to a temp file in
  the guest, so it cannot reference sibling files — put shared assets in a `--mount` instead.

```bash
cat > setup.sh <<'EOF'
#!/usr/bin/env bash
set -euxo pipefail
sudo DEBIAN_FRONTEND=noninteractive apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential git
EOF
virt-runner create dev --script setup.sh --script configure.py
```

## JSON mode essentials

```json
{"tool":"vm-create","version":"…","status":"success","error":null,"booted":true,"vm":{…},"image":{…},"scripts":[]}
{"tool":"vm-create","version":"…","status":"error","error":{"code":"ssh-timeout","message":"…","stage":"ssh-verify"},"booted":true,"vm":{…},"image":{…},"scripts":[]}
```

- `tool`: `vm-create` / `vm-destroy` / `vm-list` / `vm-ssh`. Usage errors also produce an
  envelope (`code: "usage"`) when `--json` is on the command line.
- Exit codes: `0` success, `1` runtime/preflight failure, `2` usage error (same with/without `--json`).
- Human progress is suppressed under `--json`; set `VM_JSON_TRACE=1` to see it on stderr.
- Keep stderr separate from stdout (`2>/dev/null` or `2>log`) — script and ssh output land there.

jq recipes:

```bash
out=$(virt-runner create t1 --json 2>/tmp/t1.log); rc=$?
jq -r .vm.ip            <<<"$out"   # IP
jq -r .vm.ssh_command   <<<"$out"   # ssh -i ~/.ssh/virt_runner_key ubuntu@192.168.122.x
jq -r '.error.code, .error.stage // empty' <<<"$out"

# Run a non-interactive command in a VM (default key; ssh_command shows the -i path used)
SSHO=(-i ~/.ssh/virt_runner_key -o IdentitiesOnly=yes -o BatchMode=yes
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
target=$(virt-runner list --json | jq -r '.vms[] | select(.name=="t1" and .ip) | "\(.ssh_user)@\(.ip)"')
ssh "${SSHO[@]}" "$target" 'uname -a'
ssh "${SSHO[@]}" "$target" 'bash -s' < fix.sh      # re-run a local script

virt-runner list --json | jq -r '.vms[] | "\(.name)\t\(.state)\t\(.ip // "-")"'
```

Full per-command schema and the error-code table: `references/json-and-errors.md`.

## Workflows

**Throwaway test VM (always cleans up):**

```bash
name=agent-test-$$
trap 'virt-runner destroy "$name" --json >/dev/null 2>&1' EXIT
out=$(virt-runner create "$name" --ram 2 --vcpu 2 --disk 10 --json) || { echo "$out" | jq .error; exit 1; }
ssh "${SSHO[@]}" "$(jq -r '"\(.vm.ssh_user)@\(.vm.ip)"' <<<"$out")" 'cat /etc/os-release'   # SSHO: see JSON recipes
```

**Fully provisioned dev VM:** `virt-runner create dev --ram 8 --vcpu 4 --disk 60 --mount ~/src/app:/work --script setup.sh`, then `virt-runner ssh dev` (the source is live at `/work`).

**Multi-distro matrix** (sequential is simplest; parallel creates work but download the same image
concurrently on a cold cache — warm the cache with one create per distro first):

```bash
for d in ubuntu arch fedora; do
  virt-runner create "mx-$d" --distro "$d" --script test.sh --json > "mx-$d.json" 2>"mx-$d.log"
  echo "$d exit=$? $(jq -r '.status + " " + (.error.code // "")' "mx-$d.json")"
done
for d in ubuntu arch fedora; do virt-runner destroy "mx-$d" --json >/dev/null 2>&1; done
```

**Idempotent cleanup:**

```bash
out=$(virt-runner destroy "$name" --json); rc=$?
[ $rc -eq 0 ] || [ "$(jq -r .error.code <<<"$out")" = vm-not-defined ] || { echo "$out"; exit 1; }
```

**Debugging a failed create:**
- `script-failed`: VM is up — `virt-runner ssh NAME`, re-run the script manually, read the stderr log.
  Fix the script and either re-run it via `ssh "${SSHO[@]}" user@ip 'bash -s' < setup.sh` or destroy + recreate.
- `lease-timeout` / `ssh-timeout`: `virsh console NAME` (detach with `Ctrl-]`) to watch boot/cloud-init;
  in-guest `sudo cloud-init status --long`, `/var/log/cloud-init-output.log`.
- Then `destroy` the VM — failed creates past stage `create` leave it defined.

More in `references/troubleshooting.md`.

## Key gotchas

- **Image cache is never refreshed**, even for `latest`/`current`: a cache hit skips download and
  verification entirely. For a newer build delete the cache dir: Ubuntu `~/vm-images/<release>/`,
  Arch `~/vm-images/arch/latest/`, Fedora `~/vm-images/fedora/<release>/`, `--image` URLs
  `~/vm-images/custom/<sha12>/`. Override the root with `VM_CREATE_CACHE_DIR`. Do not wipe the
  cache casually — it turns a 20 s create into minutes.
- `image.verification` is `sha256-sums` or `skipped` (no sums found / old cache without marker).
- Fedora downloads go through mirrors; a stale mirror index can 404 — the tool falls back to the
  next-newest build automatically; a retry usually fixes `image-download-failed`.
- Only x86_64 hosts and images.
- `list` shows only VMs with a disk in `vm-pool`; VMs from other tools are invisible (and must not
  be touched).
- Guest host keys change on every recreate and IPs are reused — always use
  `StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` for manual ssh to these guests.
- Running `uv run virt-runner` inside a venv is fine; host tools are spawned with the venv
  stripped from PATH.
