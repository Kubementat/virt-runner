# Security Policy

This document describes the security-relevant behaviour of `virt-runner`, what it does and does
not protect, and how to report a vulnerability.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

1. Use [GitHub's "Report a vulnerability"](https://github.com/Kubementat/virt-runner/security/advisories/new)
   form to open a private advisory.
2. If that is unavailable, mail `julianweberdev@gmail.com` with the word `virt-runner` in the subject.

| Step | Target |
|---|---|
| Acknowledgement | 7 days |
| Fix released or a reasoned refusal given | 90 days from acknowledgement |

Advisories, credits, and fixed-version information are published as GitHub security advisories;
the fix appears in `CHANGELOG.md` like any other entry.

### What to include

`virt-runner --version`, the full `--json` output (or the `VM_JSON_TRACE=1` trace), how to reproduce
it, and whether your host is x86_64/aarch64. Never post a private key, a guest IP, or a token you
actually use — a key that only ever protected throwaway VMs is still a key.

## Supported versions

The latest release is the only supported version. `virt-runner` is `0.x`:
fixes land on `main` and ship in the next tagged release, without backporting. There are no
extended-support branches.

## What `virt-runner` actually is

One unprivileged user's tool for creating throwaway, single-tenant KVM guests through libvirt.
Its design goal is *"one command to an SSH-reachable VM"*, not *"strong workload isolation for
untrusted code"*. Read [Threat model and accepted trade-offs](#threat-model-and-accepted-trade-offs)
before trusting it with anything beyond development and testing.

## Attack surface

| Surface | Notes |
|---|---|
| CLI arguments | VM name (`^[A-Za-z][A-Za-z0-9._-]*$`), `--user` (`[a-z_][a-z0-9_-]{0,31}`), and guest mount paths (`[A-Za-z0-9._-]`, no `.`/`..`) are validated by regex before they reach any YAML, XML, or argv (RCE/privilege-escalation surface closed at the boundary). The guest's `--user` has passwordless `sudo`; passing an unintended user is a guest-side privilege issue, not host-side. |
| Subprocess execution | Everything is invoked with `subprocess.run` + `argv` lists, never a shell, so there is no `eval`-style surface. |
| Network | `virt-runner` itself binds no ports. It talks to `libvirtd` over the local libvirt socket and to guests over the `default` NAT network. |
| Privileges | It runs entirely as your user, but that user must be in the `libvirt` and `kvm` groups, which is effectively root-equivalent on the host (see below). `create`/`destroy`/`list`/`ssh` need no sudo; `setup` is the exception — its whole job is running the privileged host installer. |

## Threat model and accepted trade-offs

These are deliberate decisions, not bugs. If they are incompatible with your threat model,
`virt-runner` is the wrong tool.

### 1. SSH host keys are never verified

Every SSH call `virt-runner` makes — both the `create` verification probe and an interactive
`virt-runner ssh NAME` — uses `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null`.

- **Why:** libvirt's `default` network reuses DHCP addresses between guests, so a permanently
  recorded host key is guaranteed to collide with the next guest (see
  [lesson 002](docs/lessons-learned/002-ssh-verify-identity-and-host-key-reuse.md)). The goal is a
  working probe and a clean `~/.ssh/known_hosts`, not persistent trust.
- **Residual risk:** a host that can position itself on the guest network (the host itself, another
  guest, or anything bridged into that segment) can terminate SSH and read everything you send into
  the guest, including credentials and `sudo`-capable commands.
- **Practical guidance:** only use `virt-runner` on hosts you control and on a NAT network you do
  not expose. `curl`/`ssh` traffic inside a guest is not protected by this tool at all.

### 2. Image verification is best-effort

Cloud images are fetched over HTTPS from the distro's official mirror and SHA256-verified against
the mirror's own checksum file. Two weaknesses follow:

- Trust is delegated to TLS plus the mirror. There is no detached GPG signature or transparency log,
  so a compromised mirror that controls both the image and the checksum file is indistinguishable
  from a legitimate one.
- When the checksum entry for the newest build is missing (a mirror mid-sync — a real, observed
  failure mode), verification falls back to an *older* build and, as a last resort, uses the
  newest **unverified** image with a `no verifiable checksums` warning.

**Guidance:** treat that warning as a real finding. Do not use `virt-runner` to provision images you
would not otherwise trust, and consider pre-populating `~/vm-images/` from a known-good source on
sensitive hosts. `--image` widens the surface: it fetches whatever URL you give and is only
checksummed if the source publishes a checksum file.

### 3. One SSH keypair for every guest

`create` generates a passphrase-less ed25519 keypair (`~/.ssh/virt_runner_key`, mode `0600`) and
injects the same public key into every guest. That is one credential for all of them:

- Anyone who reads the private key can log in to **every** guest created with the default key, on
  any address the guest is reachable on.
- The key is not revoked by `destroy` — it stays in `~/.ssh/`. Rotating it means deleting both files
  and passing `--ssh-key` deliberately.

**Guidance:** do not put `~/.ssh/virt_runner_key` in a shared folder, dotfiles repo, or container
image. For anything long-lived, use a dedicated key.

### 4. `--mount` grants the guest write access to your host filesystem

A virtiofs share is read-write and passes UIDs through 1:1. Root inside the guest behaves like root
on the shared host directory: it can modify, delete, and `chown` files you own there, and it can
exhaust host disk from inside a guest that you considered disposable.

**Guidance:** share only directories you accept being fully writable by the guest. Never mount
`$HOME`, your dotfiles, or a repository working tree you have not committed.

### 5. `--script` executes your code with the guest's privileges

`--script` uploads the file, makes it executable, and runs it over SSH as the cloud user, who has
passwordless `sudo`. Whatever you pass is executed with no sandbox and no review. A malicious script
has root in the guest and write access to every mount you attached.

**Guidance:** same discipline as `curl | bash` — only run scripts you wrote or reviewed.

### 6. The libvirt/KVM group membership is a root-equivalent grant

The host setup adds your user to `libvirt` and `kvm` so that `virsh` works without sudo. That is the
price of "no sudo at run time": the ability to create, delete, read, and connect to *all* VMs on the
host. On a machine where other users exist, this is a privilege boundary you are deliberately
opening.

**Guidance:** do not run `install-prerequisites.sh` on shared or multi-user hosts without thinking
it through, and do not assume a guest's `ubuntu`/`fedora` account is isolated from other users of
the host.

## Not a vulnerability

- Destroying or listing a VM you do not own is limited to VMs whose disk lives in the `vm-pool`
  storage pool.
- The `vm-already-defined`, `vm-not-defined`, and similar errors are checks, not access control.
  Any user with the group membership can destroy any pool-owned VM.
- Cloud images are downloaded from the URLs hard-coded in the distro profiles; there is no allowlist
  feature to complain about.
- A `--mount` path or `--image` URL that resolves outside your expectations is only reachable by
  someone who already controls your command line.

## Hardening

If you need stronger guarantees than the defaults give you:

- Run `virt-runner` inside a dedicated, single-purpose account (its own `~/vm-images`, its own
  `~/.ssh/virt_runner_key`) so a stray VM cannot see your data.
- Pass `--ssh-key` explicitly per project so key rotation is a deliberate act.
- Prefer `--mount` of an empty scratch directory over sharing real data.
- Keep the `default` network NAT-only; do not bridge guests into a network you use for real traffic.
- Review `install-prerequisites.sh` before running it with sudo, like any installer.
