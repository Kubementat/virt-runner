# virt-runner

**`vm-create`: one command to a running, SSH-accessible Ubuntu KVM VM via libvirt + cloud-init.**

A small POC project (three plain-bash scripts, no dependencies beyond the
host's libvirt toolchain) that turns `vm-create <name>` into a booted,
SSH-verified Ubuntu 26.04 (`resolute`) KVM guest, `vm-destroy <name>` into
a clean teardown that leaves no orphan disk behind, and `vm-list` into an
at-a-glance view of every VM the tool created (with its SSH command).

## What it does

`vm-create` runs a six-step pipeline, fully unattended (no prompts, no sudo):

1. **Preflight** — fail fast (exit 1/2) if libvirt is unreachable, the name is
   invalid or already defined, or the `vm-pool` pool / `default` network /
   SSH key are missing.
2. **Download + verify cloud image** — fetches the release's Ubuntu cloud
   image into `~/vm-images/<release>/` and verifies it against the
   published `SHA256SUMS`. A warm cache is reused, so the ~820 MiB image is
   downloaded **exactly once per release**; every VM after the first costs
   zero bandwidth.
3. **cloud-init generation** — writes NoCloud `user-data` (injected SSH key,
   cloud user, passwordless sudo) and `meta-data` (hostname) into a
   throwaway `mktemp` dir (chmod 600, trap-cleaned).
4. **VM creation** — `virt-install --import` with a pre-created
   `<NAME>_vda.qcow2` in `vm-pool`, the `default` NAT network, a
   **fixed MAC known before boot**, `--os-variant ubuntu-lts-latest`
   (osinfo-db gap workaround), file-based `--cloud-init` sub-options with
   `disable=on`, and `--autostart`.
5. **IP discovery + SSH verification** — polls the dnsmasq lease file
   (`/var/lib/libvirt/dnsmasq/virbr0.status`) for the fixed MAC (120 s
   window), then proves `ssh <user>@<ip>` actually works (90 s window). The
   success block is printed **only after** a real SSH round-trip succeeds;
   otherwise the script exits 1 with a `virsh console` hint.
6. **Access info** — prints the VM's name/UUID, IP, SSH command, console
   command, and the `vm-destroy` teardown command.

`vm-destroy <name>` destroys (if running) and undefines the domain, then
deletes the `<NAME>_vda.qcow2` volume from `vm-pool`. A missing volume is a
warning, not an error (idempotent teardown); an undefined domain is a hard
error. No confirmation prompt — it is scriptable.

`vm-list` lists the VMs configured by `vm-create` — domains whose disk lives
in the `vm-pool` storage pool — and prints one access-info block per VM in
the same format as `vm-create`'s success output (name/UUID, IP, `ssh`
command, console, teardown). The IP comes from the dnsmasq lease file via
the VM's fixed MAC; a shut-off VM (or one without a lease yet) gets clear
`IP:`/`SSH:` placeholders instead. VMs outside `vm-pool` (e.g.
`setup-test-vm`) are never listed. The guest user is not stored in the
domain XML, so the printed SSH user defaults to `ubuntu` and can be
overridden with `--user`. Exit 0 even when no VMs are configured.

## Requirements

Host prerequisites (the POC was built and accepted on a matching host;
preflight fails fast with a clear message if any of these are violated):

- **KVM**: `/dev/kvm` present (`vmx`/`svm` CPU flag).
- **libvirt** with:
  - an **active `vm-pool`** storage pool (all tool-created disks live here), and
  - the **active `default` NAT network** (dnsmasq DHCP, bridge `virbr0`,
    guest LAN `192.168.122.0/24`).
- **`virt-install` ≥ 4.1** (file-based `--cloud-init` sub-options —
  `user-data=`, `meta-data=`, `clouduser-ssh-key=`, `disable=on`).
- **`curl`**, **`uuidgen`** (plus `virsh`, `sha256sum`, `ssh`, `awk` — all
  standard).
- **User in the `libvirt` and `kvm` groups** — `virsh` must work **without
  sudo** (`qemu:///system`).
- A public SSH key to inject (default: `~/.ssh/id_ed25519.pub`).

Note: libvirt may be *socket-activated* (`libvirtd` shows "inactive" while
`libvirtd.socket` is active) — that is normal and not an error.

## Quickstart

Install (either works):

```console
mkdir -p ~/bin
ln -s "$PWD/bin/vm-create" ~/bin/vm-create
ln -s "$PWD/bin/vm-destroy" ~/bin/vm-destroy
ln -s "$PWD/bin/vm-list" ~/bin/vm-list
# …or: cp bin/vm-create bin/vm-destroy bin/vm-list ~/bin/
```

Create a 2 GiB / 1 vCPU / 10 GiB VM named `poc-1` from the default
(`resolute`, 26.04) release:

```console
vm-create poc-1 --ram 2 --vcpu 1 --disk 10
```

Expected output (values substituted; the block is printed only after the
DHCP lease is found **and** a real SSH round-trip succeeds):

```console
VM 'poc-1' created (assigned MAC: 52:54:00:be:b2:b1)
IP acquired: 192.168.122.190
VM created and running.
Name:    poc-1   (UUID 3961e1ee-0a48-4ecf-9d91-1a7a8f63a51f)
IP:      192.168.122.190   (also reachable as poc-1.default)
SSH:     ssh ubuntu@192.168.122.190
Console: virsh console poc-1     (Ctrl-] to detach)
Teardown: vm-destroy poc-1   (or: virsh destroy poc-1 && virsh undefine poc-1)
```

More examples:

```console
# Different release, explicit custom key:
vm-create poc-2 --release noble --user ubuntu --ssh-key ~/.ssh/id_ed25519.pub

# Create but do not boot (skips IP/SSH wait):
vm-create poc-3 --no-boot

# Tear down (VM definition + disk volume, no prompt):
vm-destroy poc-1

# List all tool-created VMs (IP + ssh command per VM):
vm-list
```

Full evidence of a real end-to-end run (downloads, timings, SSH round-trips,
teardown, final pool state) is in [`docs/e2e-acceptance.md`](docs/e2e-acceptance.md).

## CLI reference

### `vm-create`

```
vm-create <NAME> [OPTIONS]
```

`NAME` must match `^[A-Za-z][A-Za-z0-9._-]*$` and must not be an
already-defined domain.

| Option | Meaning | Default |
|---|---|---|
| `--ram GiB` | Guest RAM in GiB (positive integer) | `4` |
| `--vcpu N` | vCPU count (positive integer) | `2` |
| `--disk GiB` | Virtual disk size in GiB (positive integer; qcow2 in `vm-pool`) | `30` |
| `--release REL` | Release codename (`resolute`, `noble`, …); selects the standard image/`SHA256SUMS` URLs and **wins over `--image`** if both are given | `resolute` |
| `--image URL` | Direct URL of a raw cloud image (cached under `~/vm-images/custom/`; verified only if a same-directory `SHA256SUMS` is derivable, else a warning is printed and verification skipped) | `https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img` |
| `--user USER` | Cloud user name to create and to SSH as | `ubuntu` |
| `--ssh-key PATH` | Path to the public key to inject | `~/.ssh/id_ed25519.pub` |
| `--no-boot` | Create the VM (definition + disk + cloud-init) but leave it stopped; skips the IP/SSH steps and prints the not-booted variant | off |
| `--keep-going` | On download/verification error: continue if a previously cached image exists, else fail with a clear error | off |

Unknown option or missing `NAME` → exit 2 with usage.
Exit codes: `0` success · `1` runtime/preflight failure · `2` usage error.

### `vm-destroy`

```
vm-destroy <NAME>
```

1. Preflight: `<NAME>` must be a **defined** domain — otherwise exit 1
   (`VM '<NAME>' is not defined`); no argument → exit 2 with usage.
2. `virsh destroy <NAME>` (if running), then `virsh undefine <NAME>`.
3. `virsh vol-delete <NAME>_vda.qcow2 vm-pool` — if the volume is already
   absent, a warning is printed but the exit code is still 0 (idempotent
   teardown).
4. Prints `VM '<NAME>' destroyed and disk removed.` (or the already-absent
   variant).

No confirmation prompt. Same exit-code contract as `vm-create`.

### `vm-list`

```
vm-list [--user USER]
```

Lists every VM whose disk lives in the `vm-pool` storage pool (i.e.
configured by `vm-create`) — one block per VM in the same access-info
format `vm-create` prints on success, including the `ssh` command. The IP
is looked up in the dnsmasq lease file by the VM's fixed MAC (same
parsing as `vm-create` step 5; honors `VM_LIST_LEASE_FILE`, falling back
to `VM_CREATE_LEASE_FILE`).

| Option | Meaning | Default |
|---|---|---|
| `--user USER` | User name shown in the printed SSH command (the actual guest user is whatever `vm-create --user` created) | `ubuntu` |

| State | Printed `IP:` / `SSH:` lines |
|---|---|
| running + lease | IP (with `<NAME>.default` note) + `ssh <user>@<ip>` |
| running, no lease yet | `(no DHCP lease found yet)` / `(unavailable — no IP yet)` |
| shut off | `(no DHCP lease — VM not running)` / `(unavailable — start the VM first: virsh start NAME)` |

No VMs configured → a hint line, still exit 0. Same exit-code contract as
`vm-create`.

## How it works

```mermaid
flowchart TD
    A[vm-create NAME options] --> B{Step 1 — Preflight<br/>libvirt reachable · name valid & unused<br/>vm-pool active · default net active<br/>SSH key present}
    B -- fail --> X1[exit 1 / exit 2 + clear error]
    B -- pass --> C[Step 2 — Image cache<br/>~/vm-images/ release or custom/<br/>curl + SHA256SUMS verify<br/>warm cache → skip download]
    C --> D[Step 3 — cloud-init files<br/>mktemp dir, chmod 600, trap cleanup<br/>user-data: SSH key, user, sudo<br/>meta-data: hostname + fresh UUID]
    D --> E[Step 4 — virt-install --import<br/>disk NAME_vda.qcow2 in vm-pool<br/>default NAT net · fixed MAC 52:54:00:xxxxxx<br/>--os-variant ubuntu-lts-latest<br/>--cloud-init user-data=,meta-data=,clouduser-ssh-key=,disable=on<br/>--autostart]
    E -- --no-boot --> N[stop domain, print not-booted block, exit 0]
    E --> F[Step 5 — Wait for IP<br/>poll /var/lib/libvirt/dnsmasq/virbr0.status<br/>match fixed MAC (JSON, legacy fallback)<br/>120 s window]
    F --> G[Step 5b — SSH verification<br/>ssh BatchMode accept-new user@IP exit<br/>90 s window — mandatory]
    G -- timeout --> X2[exit 1 + 'check: virsh console NAME']
    G -- proven --> H[Step 6 — Access info<br/>name/UUID · IP · SSH · console<br/>vm-destroy teardown line]
    D2[vm-destroy NAME] --> D3{defined domain?}
    D3 -- no --> X3[exit 1 'not defined']
    D3 -- yes --> D4[destroy if running → undefine<br/>→ vol-delete NAME_vda.qcow2 vm-pool<br/>missing volume = warn, still exit 0]
L1[vm-list] --> L2{defined domains with a<br/>disk under vm-pool?}
L2 -- yes --> L3[access-info block per VM<br/>name/UUID · state · IP from lease file (fixed MAC)<br/>ssh command · console · teardown line]
L2 -- none --> L4[hint line, exit 0]
```

Key design points: the **fixed MAC generated before boot** makes IP
discovery deterministic against the dnsmasq lease file (no
`qemu-guest-agent` needed); `cloud-init … disable=on` makes reboots
deterministic; `--autostart` makes the VM survive host reboots; the
`<NAME>_vda.qcow2` volume-naming convention is what lets `vm-destroy`
remove exactly the right disk.

## Project layout

```
virt-runner/
├── bin/
│   ├── vm-create            # the primary tool (preflight → image → cloud-init → create → IP → SSH → info)
│   ├── vm-destroy           # companion teardown (destroy/undefine + volume delete)
│   └── vm-list              # list tool-created VMs with IP + ssh command (vm-pool residents)
├── specification/
│   └── specification.md     # the authoritative POC specification (CLI contract, pipeline, pitfalls, decisions D1–D10)
├── docs/
│   ├── plans/
│   │   └── poc-implementation-plan.md   # vertical-slice plan S1–S7, planner decisions PD1–PD4, environment verification
│   ├── reviews/
│   │   └── poc-review.md                # independent review (Agent 5): verdict PASS, per-slice checklist, gaps G1–G8
│   ├── e2e-acceptance.md                # recorded end-to-end acceptance evidence (all 5 ACs, verbatim outputs)
│   └── POC-Implementation-Documentation.md  # what was done, in what order (this POC's narrative + traceability)
├── .tickets/
│   └── ticket-01..07-*.md     # one self-contained work ticket per slice, in strict execution order
├── README.md
└── CHANGELOG.md
```

## Development process

This project was built by an orchestrated multi-agent pipeline, each phase
committed as it landed:

1. **Specification** (Agent 1) — research doc → authoritative
   [`specification/specification.md`](specification/specification.md)
   (CLI contract, pipeline, verified host facts, pitfalls, decisions D1–D10).
2. **Planning** (Agent 2) →
   [`docs/plans/poc-implementation-plan.md`](docs/plans/poc-implementation-plan.md)
   — 7 strictly sequential **vertical slices** (S1→S7), planner decisions
   PD1–PD4, testing strategy, risk table, read-only environment verification.
3. **Ticketing** (Agent 3) → [`.tickets/`](.tickets/README.md) — one
   self-contained ticket per slice, each with deliverables, safety rules,
   checkable acceptance criteria, and its exact commit message.
4. **Implementation** (worker agents) — one ticket at a time, strictly in
   order; each slice was verified on the real host (fixture tests first,
   real libvirt objects from S4, real guest boots from S5, full E2E in S7)
   and committed individually.
5. **Review & verification** (Agent 5) →
   [`docs/reviews/poc-review.md`](docs/reviews/poc-review.md) — independent
   re-runs of every safe check; verdict **PASS**.
6. **Final documentation** (Agent 6) — this README,
   [`CHANGELOG.md`](CHANGELOG.md), and
   [`docs/POC-Implementation-Documentation.md`](docs/POC-Implementation-Documentation.md).

## Status

**POC accepted — verdict PASS** (review 2026-08-25). All five spec
acceptance criteria are satisfied with recorded, same-day end-to-end
evidence on the real host using the real Ubuntu 26.04 cloud image:
unattended clean-state creation, single-download cache reuse,
SSH-gated success output (including the negative leg), autostart +
orphan-free teardown, and fail-fast name collision (18 ms). Remaining
items are documented gaps, not blockers — see
[`docs/reviews/poc-review.md`](docs/reviews/poc-review.md) §3 (G1–G8) and
the follow-ups section of
[`docs/POC-Implementation-Documentation.md`](docs/POC-Implementation-Documentation.md).

Version **v0.1.0** — see [`CHANGELOG.md`](CHANGELOG.md).
