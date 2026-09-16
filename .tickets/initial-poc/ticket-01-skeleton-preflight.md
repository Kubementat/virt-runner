# Ticket 01 — S1: Project skeleton + `vm-create` preflight

**Slice:** S1 (of S1→S7, plan `docs/plans/poc-implementation-plan.md` §2)
**Goal:** Create the full CLI surface of `bin/vm-create` — shebang, strict mode, constants, usage/help text, argument parsing for every flag (including `--release`/`--image` precedence), name validation, and the complete Step-1 preflight checks — with a clearly marked stub after preflight. By the end of this ticket the CLI contract (flags, precedence, error paths, exit codes) is fully exercisable; later tickets replace the stub feature by feature.

## Context

- **Spec sections:** §1 (purpose), §2 (environment requirements), §3.1 (CLI interface, exit codes, defaults), §4 step 1 (preflight), §9 (reference sketch — starting point only, not the deliverable), §6 pitfall 8 (libvirtd is socket-activated; do NOT check `systemctl is-active libvirtd`), §6 pitfall 9 (`setup-test-vm` must never be touched), decisions **D1** (`--release` overrides `--image` when both given), **D9** (output format authority), **D10** (default key path is `~/.ssh/id_ed25519.pub` — a dot is required).
- **Plan sections:** §1 (ground rules: `#!/usr/bin/env bash` + `set -euo pipefail`; no new dependencies; exit codes 0/1/2; `poc-` prefix for all test VMs; testability hooks `VM_CREATE_CACHE_DIR` and `VM_CREATE_LEASE_FILE` must be documented in the script header even though S1 doesn't use them yet), §2 slice S1, §3 (commit message), §4 (safety rules).
- **Environment facts (host-verified, plan §8):**
  - Current user `user` is in `libvirt` + `kvm` groups; all `virsh` commands work **without sudo** on `qemu:///system`.
  - Exactly one pre-existing domain: `setup-test-vm` (shut off). Pool `vm-pool` is active; network `default` is active. Never touch either beyond read-only checks.
  - `~/.ssh/id_ed25519.pub` **does not exist** on this host (drives a negative test below); `~/.ssh/id_rsa.pub` does exist and is usable as an alternate `--ssh-key` for the positive preflight test.
  - `virt-install` is 4.1.0, `curl`/`sha256sum`/`uuidgen` present; `/dev/kvm` present.
  - `~/vm-images` does not exist yet.

## Deliverables

- **Create** `bin/vm-create` — executable (`chmod +x`), `#!/usr/bin/env bash`, `set -euo pipefail`, containing:
  - All constants (defaults per spec §3.1: `RAM=4`, `VCPUS=2`, `DISK=30`, `RELEASE=resolute`, `USER_NAME=ubuntu`, `SSHKEY=$HOME/.ssh/id_ed25519.pub`, `POOL=vm-pool`, `NET=default`, `LEASE_FILE=/var/lib/libvirt/dnsmasq/virbr0.status`, resolute default image/sums URLs per spec §5).
  - Full usage/help text on stderr for exit-2 paths.
  - Argument parsing for: positional `NAME`, `--ram`, `--vcpu`, `--disk`, `--image`, `--release`, `--user`, `--ssh-key`, `--no-boot`, `--keep-going`. Unknown option → exit 2 + usage. Missing `NAME` → exit 2 + usage.
  - `--release`/`--image` precedence per **D1**: if both given, `--release` wins (the release selects the standard URL pattern `https://cloud-images.ubuntu.com/<rel>/current/<rel>-server-cloudimg-amd64.img` + matching `SHA256SUMS` URL). Parsing and precedence must be in place now even though the download happens in ticket 02.
  - NAME validation: regex `^[A-Za-z][A-Za-z0-9._-]*$`. Invalid NAME is a **usage error → exit 2** with a clear message (planner decision, plan §2 S1).
  - Full Step-1 preflight (spec §4 step 1), each failing check → exit 1 with a one-line message naming the failing check:
    1. `virsh list` succeeds (libvirt reachable, no sudo needed). Do **not** check `systemctl is-active libvirtd` (socket-activated; pitfall 8).
    2. Domain `<NAME>` not already defined — `virsh dominfo <NAME>` must fail; on success print `VM '<NAME>' already defined` (or equivalent clear wording) and exit 1.
    3. `vm-pool` appears in `virsh pool-list --all` as active.
    4. `default` appears in `virsh net-list --all` as active.
    5. `--ssh-key` file exists and is non-empty (`ssh key not found: <path>` style message).
  - After preflight: a stub that prints a clear message such as `not implemented in this slice` to stderr and exits **1**. (So a valid invocation that passes preflight currently ends in exit 1 — expected and correct for S1.)
  - Script header comment documenting the two test-only env hooks `VM_CREATE_CACHE_DIR` (relocate `~/vm-images`) and `VM_CREATE_LEASE_FILE` (relocate the lease file), per plan ground rule 5.
- **Do NOT create** `bin/vm-destroy` in this ticket (that is ticket 06).
- Optionally `.gitignore` entries only if you add temp patterns; test artifacts live in `/tmp`, never in the repo.

## Implementation requirements

1. Both scripts' (here: `vm-create`'s) style: `#!/usr/bin/env bash`, `set -euo pipefail` as the first executable lines.
2. Option-parsing loop must be safe under `set -u` (no unguarded `$2` when a value-taking option is the last argument — a missing value should be a clear exit-2 usage error).
3. Positive-integer validation for `--ram`/`--vcpu`/`--disk` values (non-numeric → exit 2 usage error).
4. Pre-flight order per spec §4 step 1; each failure is a single line on stderr naming the check, exit 1 (except NAME-regex failures which are exit 2).
5. The duplicate-domain check must use `virsh dominfo` (its non-zero exit status = not defined). Note: `virsh dominfo` prints errors to stderr — suppress them in the test so output stays clean.
6. Usage text must list all options with their defaults (mirroring spec §3.1 table).
7. Do not implement download, cloud-init, virt-install, IP-wait, or output-block logic yet — those slices replace the stub. Keep the stub location obvious (a single comment block `# STUB — replaced by ticket 02+`).

## Safety rules

- Any VM name used in tests must start with `poc-` (use `poc-preflight`, `poc-x`, …).
- No VM or disk may actually be created by this ticket (there is no virt-install call yet) — verify with `virsh list --all` and `virsh vol-list vm-pool` before and after your verification that state is unchanged.
- **Never touch** `setup-test-vm` or the pre-existing `noble-server-cloudimg-amd64.img` volume (read-only inspection only).
- No `sudo`, ever. No modification of root-owned files, `~/.ssh` keys, or libvirt config.
- Any `/tmp` fixtures must be removed before finishing.

## Acceptance criteria

Run from the project root (`bin/vm-create` by relative path). Each bullet: command → expected outcome.

1. `bin/vm-create` → **exit 2**, usage printed on stderr.
2. `bin/vm-create --ram 2` → **exit 2**, usage on stderr (missing NAME).
3. `bin/vm-create poc-x --bogus` → **exit 2**, `unknown option` + usage on stderr.
4. `bin/vm-create -bad` → **exit 2**, clear invalid-name message (fails the regex `^[A-Za-z][A-Za-z0-9._-]*$`).
5. `bin/vm-create .bad` → **exit 2**, same invalid-name path.
6. `bin/vm-create setup-test-vm` → **exit 1**, message of the form `VM 'setup-test-vm' already defined` (duplicate-domain preflight proven against the real pre-existing domain; no side effects).
7. `bin/vm-create poc-preflight --ssh-key /nonexistent` → **exit 1**, `ssh key not found` message (real negative case: the default key does not exist on this host, so this also passes even without the flag — but the explicit flag makes the intent clear).
8. `bin/vm-create poc-preflight --ssh-key ~/.ssh/id_rsa.pub` → **passes preflight** (libvirt, duplicate-name, pool, net, key checks all succeed) and hits the stub: stderr `not implemented in this slice`, **exit 1**.
9. `bin/vm-create poc-preflight --ssh-key ~/.ssh/id_rsa.pub --release noble --image file:///tmp/whatever` → parses cleanly, `--release` wins (no crash under `set -u`), reaches the stub exit 1 (precedence plumbing in place).
10. State unchanged: `virsh list --all` shows only `setup-test-vm`; `virsh vol-list vm-pool` shows only `noble-server-cloudimg-amd64.img`.

## Verification (run in this order)

1. `cd ~/virt-runner && chmod +x bin/vm-create`
2. Record baseline state: `virsh list --all && virsh vol-list vm-pool` → only `setup-test-vm` / only `noble-server-cloudimg-amd64.img`.
3. Run acceptance criteria 1–9 above one by one, checking both the exit code (`echo $?`) and the message.
4. Run acceptance criterion 10 (state-unchanged check).
5. Success = all criteria pass and the working tree contains only the intended new/changed files (no test artifacts, nothing in `/tmp` left behind in the repo).
6. **No real VM creation is required for this ticket.** Do not invoke any virt-install or volume-creating command.
7. If all pass: `git add -A && git commit -m "feat(vm-create): add script skeleton, CLI parsing, and preflight checks (ticket 01)"`

## Definition of done

- All acceptance criteria pass; verification sequence ran successfully.
- Working tree committed with exactly: `git add -A && git commit -m "feat(vm-create): add script skeleton, CLI parsing, and preflight checks (ticket 01)"`

## Pitfalls (spec §6 / plan §2, S1-relevant)

- **Pitfall 8:** libvirtd is socket-activated — `systemctl status libvirtd` showing "inactive" is normal. Do not add such a check; only require `virsh list` to succeed.
- **Pitfall 9:** `setup-test-vm` exists and is used here *read-only* to prove the duplicate-name check. Never create, modify, or destroy it.
- **D1:** `--release` beats `--image` when both are given — get the precedence decision right now; ticket 02 builds on it.
- **D10:** default key path is `~/.ssh/id_ed25519.pub` (with the dot). It is absent on this host — that is why the positive preflight test uses `--ssh-key ~/.ssh/id_rsa.pub`.
- `set -u` interactions with the option-parsing loop (missing value as last argument) — plan S1 key risks.
- `virsh dominfo` prints to stderr on failure; don't let that pollute your error output.
