---
name: virt-runner-cli
description: |
  Expert use of the `virt-runner` CLI: one command to a running, SSH-reachable Ubuntu, Arch or Fedora KVM guest via libvirt (qemu:///system) + cloud-init, with `create`, `destroy`, `list`, `modify` and `ssh` subcommands (short aliases `c`, `d`, `ls`, `m`, `s`), a `setup` subcommand for host prerequisites, and a stable `--json` contract for the VM commands. Use when the user mentions virt-runner, wants to create, provision, list, resize, change the networking of, SSH into or destroy throwaway/test KVM or libvirt VMs, spin up a clean Ubuntu/Arch/Fedora VM to test something, share host directories into a VM (virtiofs `--mount`), provision a VM with setup scripts (`--script`), grow a VM's disk or change its vCPU/RAM/NICs/MAC, run a multi-distro test matrix, script VM lifecycles with JSON/jq, or debug a failed VM create (lease/SSH timeouts, image download/verification, script failures).
compatibility: Requires a KVM host (x86_64 or aarch64) with libvirt, virt-install >= 4.1 and Python >= 3.10. Run as `virt-runner ...` when installed (`uv tool install virt-runner`), or `uv run virt-runner ...` from a checkout.
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
8. **`modify` edits a live resource.** Read first (`list --json`, which reports state, MAC and
   disk), plan first (`--plan`) when the change is not obvious, and re-check the guest afterwards
   (`--reboot --wait`, or the reported `effective`). It never shrinks a disk and never touches a
   domain virt-runner did not create — do not try to work around that; report it to the user.

## Prerequisites / preflight

One-time host setup (Ubuntu/Debian, apt, uses sudo internally, idempotent):

```bash
virt-runner setup --check  # audit only, changes nothing
virt-runner setup          # KVM, libvirt, virt-install, virtiofsd, groups,
                           # `default` NAT network + `vm-pool` dir pool (active+autostart)
# then LOG OUT AND BACK IN (libvirt/kvm group membership)
```
`setup` runs the installer script bundled in the wheel (`virt_runner/resources/`); a checkout also
has `./install-prerequisites.sh` at the repository root. Needs sudo, idempotent, and is the only
subcommand that does. Flags: `--check`, `--with-virt-manager`; it takes no `--json` and has no alias.

`create` preflight checks (stage `preflight`): `virsh` reaches libvirtd without sudo, host is
x86_64 or aarch64, storage pool `vm-pool` defined and active, network `default` active, the VM name is not
already defined, `/usr/libexec/virtiofsd` exists (only with `--mount`), and an SSH key is usable.

SSH key: default public key `~/.ssh/virt_runner_key.pub`; private key = same path without `.pub`.
If absent, an ed25519 keypair without passphrase is generated automatically. A custom
`--ssh-key` path must end in `.pub` to be auto-generated.

Quick manual fixes: `virsh pool-start vm-pool && virsh net-start default`.

## Commands

Short aliases work everywhere: `c` = `create`, `d` = `destroy`, `ls` = `list`, `m` = `modify`,
`s` = `ssh`. Identical behaviour, flags and exit codes; the JSON `tool` id stays canonical (`vm-create`,
not `vm-c`). Use the full names in anything you write to disk so the command stays greppable.

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

### modify NAME

`virt-runner modify NAME [--vcpu N] [--ram GiB] [--disk GiB] [--net SPEC] [--net-add SPEC]
[--net-remove MAC] [--mac MAC] [--live] [--reboot] [--wait] [--no-auto-grow-fs] [--plan] [--json]`
— change an existing virt-runner VM in place instead of destroy + create. Needs at least one
change option; all composable in one call.

| Flag | Default | Notes |
|---|---|---|
| `--vcpu N` | — | int ≥ 1. `virsh setvcpus`; live hot-plug works only up to `<vcpu max>`, so a grow past it lands `on-boot` |
| `--ram GiB` | — | int ≥ 1, **GiB**. `virsh setmaxmem` (ceiling) + `virsh setmem`; grow above `<memory>` is config-only, a shrink can be live. Refused above the host's installed RAM (`runtime-error`), because libvirt would write it and only the boot would notice |
| `--disk GiB` | — | int ≥ 1, **grow only** (`disk-shrink-unsupported`). `blockresize`, or the pool volume when the guest is off, then the guest-side filesystem grow |
| `--no-auto-grow-fs` | off | skip the SSH filesystem grow: the block device grows, `fs_grown: null`, warning with the manual commands |
| `--net SPEC` | — | re-attach the **primary** NIC. `network=NAME` (libvirt virtual network) or `bridge=NAME` (host bridge). A bare name that is not a defined network is attached as a bridge of that name — and that bridge must exist on the host |
| `--net-add SPEC` | — | repeatable. `network=NAME` / `bridge=NAME`, optional `,mac=52:54:00:11:22:33`. Nothing else is accepted in the spec |
| `--net-remove MAC` | — | repeatable, exact lower-case MAC. The **last** NIC is refused (`usage`, exit 2) — `--net-add` the replacement in the same call |
| `--mac MAC` | — | primary NIC's new MAC (detach + attach). The guest re-acquires a lease ⇒ new IP, re-read it with `list` |
| `--live` | off | ask libvirt for the running change of vCPU/RAM too. Best effort — a refusal degrades to `on-boot` + warning, it is not a failure |
| `--reboot` | off | reboot the guest so `on-boot` changes land (starts a powered-off guest). Valid on its own — it applies changes already pending in the config |
| `--wait` | off | with `--reboot`: wait for the lease and a real SSH round-trip, and report the new `ip` |
| `--plan` | off | same document with `"planned": true`; libvirt untouched |

**Read `effective` before you trust a result.** A running guest and its persisted config are
distinct, and one libvirt call changes only one of them. `modify` compares a sizing request to
the **config**, not to the running value, so a domain whose config drifted from what it runs
(never rebooted, or someone used `virsh` by hand) is fixed rather than called "already set":
both agree → `modify-noop`; config only → `on-boot` with no libvirt call (a reboot is all that is
missing); config disagrees → the write happens, with the config's value as `from`.

- `"now"` — the running guest changed. Verify in-guest if it matters (`nproc`, `MemTotal`,
  `lsblk -bdno SIZE /dev/vda`, `df`, `ls /sys/class/net`) via the `ssh_command` from `list`.
- `"on-boot"` — config only. Re-run with `--reboot --wait` if the change has to be live now,
  otherwise say so in your report rather than pretending the guest has it.
- After `--reboot`, entries report `now`; if you did not reboot, do not report "the VM has 4
  vCPUs" on the strength of a `"now"` that came from a different axis.

**Disk grows in two steps, and only the first one is guaranteed:** the block device, then the
guest's own partition + filesystem over SSH (`growpart` + `resize2fs`/`xfs_growfs`/`btrfs`).
`fs_grown` is `true` (done) / `null` (not attempted: `--no-auto-grow-fs` or guest powered off) /
`false` (the guest refused — LVM, no `growpart`; the warning lists the commands). **Re-issuing
the same `--disk GiB` is the retry** — it plans a filesystem-only grow (`from == to`) and is not
a noop. A powered-off guest gets a bigger volume and grows its filesystem on the next boot.

**Interfaces:** attach/detach is live when the guest runs. Adding a second NIC to the *same*
network can make the guest lose SSH on its old address — put extra NICs on a separate
network/bridge. A `net-change-failed` means the target does not exist (`virsh net-list --all`,
`ip link`) and nothing was detached — the target is validated **before** the old NIC is given up,
so a typo cannot leave a running guest without an interface. `virsh domiflist NAME` confirms.

**If the command fails, read the `applied` flag.** A failed apply lists the changes
it *attempted*; the ones that never ran carry `"applied": false`. Never infer from
`status: "error"` that the VM is untouched — some of it may already be changed.

```bash
# resize a running guest, then confirm from the inside
virt-runner modify dev --vcpu 4 --ram 8 --disk 40 --reboot --wait --json | jq '.changes, .warnings'
target=$(virt-runner list --json | jq -r '.vms[] | select(.name=="dev" and .ip) | "\(.ssh_user)@\(.ip)"')
ssh "${SSHO[@]}" "$target" 'nproc; grep MemTotal /proc/meminfo; lsblk -bdno SIZE /dev/vda; df -BG / | tail -1'

# round-trip a NIC: add, prove it, remove it
virt-runner modify dev --net-add mgmt --json | jq -r '.changes[0].to.mac'
virt-runner modify dev --net-remove 52:54:00:11:22:33 --json

# powered-off guest: everything is config, nothing is live
virt-runner modify dev --vcpu 2 --disk 20 --plan --json | jq '.changes[].effective'   # ["on-boot","now"]
```

Errors: `vm-not-defined` (exit 1), `not-a-virt-runner-vm` (exit 1), `modify-noop` (exit 2),
`disk-shrink-unsupported`, `net-change-failed`, `volume-resize-failed`, `vm-reboot-failed`, a
`runtime-error` when `--ram` is bigger than the host's RAM,
plus `usage` (exit 2) for a bad MAC / missing network / last NIC / no option at all. Exit codes
follow the usual rule (`0` ok, `1` runtime, `2` usage).

### setup

`virt-runner setup [--check] [--with-virt-manager]` — runs the host installer (bundled inside the
wheel). Not a VM command: no `--json`, no alias, and the only subcommand that needs sudo. Use
`--check` first — it changes nothing and needs no sudo — and report to the user that a re-login is
required after a real run. On non-Debian hosts it fails fast; then install the packages named in
`README.md` by hand.

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

- `tool`: `vm-create` / `vm-destroy` / `vm-list` / `vm-modify` / `vm-ssh`. Usage errors also produce an
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

**Resize / rewire an existing VM (plan → apply → prove):**

```bash
name=agent-resize-$$          # track the name, same rule as create
virt-runner modify "$name" --vcpu 4 --ram 8 --disk 40 --plan --json | jq '.changes, .warnings'
out=$(virt-runner modify "$name" --vcpu 4 --ram 8 --disk 40 --reboot --wait --json 2>/tmp/res.log); rc=$?
if [ $rc -ne 0 ]; then jq -r '.error.code, .error.message' <<<"$out"; exit 1; fi
jq -r '.changes[] | "\(.field): \(.from) -> \(.to) (\(.effective))"' <<<"$out"
jq -r '.changes[] | select(.field=="disk_gib") | .fs_grown' <<<"$out"     # true / null / false
jq -r '[.changes[] | select(.effective=="on-boot") | .field] | "pending next boot: \(.)"' <<<"$out"

# and prove it inside the guest (the report describes libvirt, this describes the OS):
target=$(virt-runner list --json | jq -r '.vms[] | select(.name=="'$name'" and .ip) | "\(.ssh_user)@\(.ip)"')
ssh "${SSHO[@]}" "$target" 'nproc; grep MemTotal /proc/meminfo; lsblk -bdno SIZE /dev/vda'
```

If `fs_grown` came back `false` or `null` after a powered-off grow, re-run the same
`--disk GiB` once the guest is up — that is the filesystem-only retry, not a duplicate change.

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
- Hosts are x86_64 or aarch64; the other archs fail with `unsupported-arch`. On aarch64
  only Ubuntu and Fedora work — `--distro arch` fails with `image-arch-unavailable`, because
  arch-boxes ships x86_64 images only.
- `list` shows only VMs with a disk in `vm-pool`; VMs from other tools are invisible (and must not
  be touched). The same guard protects them from `modify` (`not-a-virt-runner-vm`).
- **`modify` reports libvirt, not the guest.** `"now"` is trustworthy for the block device and a
  successful hot-plug; `"on-boot"` means the guest does not have it yet. Only an in-guest
  `nproc` / `MemTotal` / `lsblk` says what the OS sees, and `vm` in the document is the state
  **before** the change.
- A RAM **grow** almost never lands live (it is capped by `<memory>`), so `--live` is not a
  promise. A RAM **shrink** can balloon live. Growing vCPUs past the domain's ceiling is the
  same story: config now, guest at the next boot.
- `--mac`/`--net` change the interface's identity, so the guest gets a **new lease**: an old
  `known_hosts` entry, a running session, or a hardcoded IP from earlier output is stale.
- `--net` with a name libvirt does not know is attached as a *bridge* of that name. If no such
  bridge exists on the host the change fails — and `virsh domiflist NAME` is what tells you
  whether the guest kept a NIC. Prefer `network=` of a real `virsh net-list --all` entry.
- `modify NAME --disk N` where N equals the current size is **not** a noop: it is the filesystem
  retry. `--disk` smaller is `disk-shrink-unsupported` and there is no override — destroy and
  recreate if a smaller disk is genuinely needed.
- Guest host keys change on every recreate and IPs are reused — always use
  `StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` for manual ssh to these guests.
- Running `uv run virt-runner` inside a venv is fine; host tools are spawned with the venv
  stripped from PATH.
