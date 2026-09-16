# Feature Specification — Generalizing `vm-create` Beyond Ubuntu Cloud Images

**Version:** 1.1 (draft)
**Status:** Ready for planning — "verified" facts come from three passes: (a) direct `curl` probes of every image/sums URL and checksum-file content (2026-08-26), (b) a documentation research pass (official wikis/docs/kickstarts, cited inline in §5), and (c) an **aarch64/arm64 research pass (2026-08-26)**: live `curl` probes of every aarch64 image tree, alias, and sidecar listed in §5.10, followed by an **independent web-verification pass (2026-08-26, worker agents)** that re-probed all §5.10 image/sums facts, firmware package contents, and boot claims; corrections from that pass are folded in inline (notably the CentOS `latest`-alias finding in pitfall 3, the aarch64 firmware package/path correction in pitfall 18, and the per-arch cadence evidence in pitfall 20). Rows marked **verify-on-first-run** must still be confirmed on real guests during the implementation tickets.
**Project root:** `~/virt-runner`
**Supersedes:** nothing — this is an additive feature on top of `specification/specification.md` (v1.0, the POC contract, which remains authoritative for the unchanged pipeline steps).
**Companion:** `.tickets/README.md` conventions (self-contained tickets, one commit each, `poc-` test names, full teardown).

## Table of Contents

1. [Purpose & Goals](#1-purpose--goals)
2. [Design: The Distro Profile](#2-design-the-distro-profile) (incl. §2.4 Architecture: x86_64 + arm64)
3. [CLI Changes](#3-cli-changes)
4. [Pipeline Changes (per step)](#4-pipeline-changes-per-step)
5. [Image Source Registry (verified facts)](#5-image-source-registry-verified-facts)
6. [Pitfalls (new, distro-specific)](#6-pitfalls-new-distro-specific)
7. [Acceptance Criteria](#7-acceptance-criteria)
8. [Out of Scope / Follow-ups](#8-out-of-scope--follow-ups)
9. [Suggested Ticket Breakdown](#9-suggested-ticket-breakdown)
10. [Decisions Log (G1–G12)](#10-decisions-log)

---

## 1. Purpose & Goals

### 1.1 What exists today

`vm-create` (POC v0.1.0) hardcodes Ubuntu in four places:

1. **Image URLs** — `https://cloud-images.ubuntu.com/<rel>/current/<rel>-server-cloudimg-amd64.img` + same-directory `SHA256SUMS` (sha256sum-compatible format), in `fetch_and_verify_image`.
2. **Cloud user / sudo group** — default `--user ubuntu`, and the generated `user-data` always uses `groups: [sudo]`.
3. **OS variant** — `--os-variant ubuntu-lts-latest` (osinfo-db gap workaround) passed verbatim to `virt-install`.
4. **Cloud-init key injection** — `clouduser-ssh-key=` targets the Ubuntu default user.

Everything else in the pipeline (preflight, NoCloud `user-data`/`meta-data`, the
`vol-create-as` → `vol-upload` → `vol-resize` import sequence, fixed-MAC +
dnsmasq-lease IP discovery, SSH verification, `vm-destroy`) is already
distro-agnostic.

### 1.2 Goals

1. `vm-create <name> --distro <d> --release <r>` brings up a running,
   SSH-verified KVM guest for any distro in the support matrix (§5.1),
   fully unattended, no sudo.
2. **Full backward compatibility**: `vm-create <name>` with no distro
   options behaves byte-for-byte as today (Ubuntu `resolute`, same cache
   path `~/vm-images/resolute/`, same output block — plus the new
   `Distro:` line, decision G9).
3. **Data-driven, not code-driven**: all distro-specific knowledge lives in
   a single profile table (§2); the pipeline code contains zero
   distro-name conditionals. Adding a distro = adding one profile row
   (+ verification, §5).
4. **Verification never silently skipped**: every profile declares its
   checksum kind (§2.2); `--image-sha256` (new) allows direct digest
   verification for any `--image` URL (decision G8).
5. The existing five POC acceptance criteria (spec §7) keep passing
   unchanged.
6. **Multi-architecture: arm64 (aarch64) support in addition to
   x86_64.** The tool runs natively on **both** host architectures:
   `vm-create` on an x86_64 host creates x86_64 guests (today's
   behavior, unchanged) and the same command on an arm64 host creates
   arm64 guests — same CLI, same unattended pipeline, same SSH/sudo/
   hostname acceptance bar, no manual per-arch configuration.
   Concretely:
   - All §2.3 distros have an arm64 cloud image in the source matrix
     (§5.10, all URL/sums facts probed and independently re-verified
     2026-08-26); image selection is architecture-aware via per-arch
     URL templates (§2.4).
   - Guest architecture always equals the **host** architecture
     (`uname -m`); `--arch` states it explicitly. Cross-arch guests
     (TCG emulation, e.g. arm64 guest on an x86_64 host) are rejected
     with a clear error (§4.1, decision G13) — emulated boots take
     minutes, breaking the wait windows and every acceptance timing.
   - On arm64 hosts the effective boot mode is **always UEFI**
     (QEMU's aarch64 machine has no legacy BIOS at all); the profile
     `boot` field applies only to x86_64 (§2.4, decision G14). The UEFI
     preflight (§4.1) is arch-aware and checks the aarch64 firmware
     paths, which differ by host distro (pitfall 18).
   - Image caching keeps x86_64 paths byte-identical to v1.0; arm64
     images go into an `aarch64/` subdirectory (decision G15).

### 1.3 Non-goals

- No GPG signature verification of checksum files (TLS to the official
  origin is the trust anchor; see §8).
- No image formats beyond uncompressed `raw`/`qcow2` downloads (no
  `.tar.xz`/`.raw.xz` extraction — every profile in §5 has an
  uncompressed variant; verified where noted).
- No per-VM metadata store (vm-list cannot know a VM's distro — §4.6).
- **Cross-architecture guests.** No x86_64 guest on an arm64 host or
  vice versa (QEMU TCG emulation). Out of scope for both v1 and the
  arm64 extension: emulated boots take minutes (breaks
  `--wait-lease`/`--wait-ssh`), `qemu-system-<other-arch>` is usually
  not installed, and it is not what "run on an arm64 machine" means
  (decision G13).
- Host architectures other than `{x86_64, aarch64}` (ppc64le, s390x,
  riscv64, …): unsupported — preflight exits 1 with a clear message.

---

## 2. Design: The Distro Profile

### 2.1 Where profiles live

A new shared shell file **`config/distro-profiles.sh`**, sourced by
`vm-create`. It defines a
bash function `get_profile_field <distro> <field>` backed by `declare -A`
associative arrays (bash 5.2 on this host supports this; POC already
requires bash). One logical profile per distro; per-release values are
expressed via URL *templates* with a `{release}` placeholder, not one row
per release.

Supported distros are discoverable: `get_supported_distros` prints the
key list (used by `--list-distros` and by usage-error messages).

### 2.2 Profile fields

| Field | Meaning | Example values |
|---|---|---|
| `display_name` | Human name for output/messages | `Fedora` |
| `default_release` | Release used when `--distro` given without `--release` | `44`, `trixie`, `10`, `resolute` |
| `image_url_template` | Download URL with `{release}` | see §5 |
| `sums_kind` | How to verify: `sums-url` (same-dir sums file), `block-url` (PGP block-format file, template given), `sidecar` (fetch `<image-url>.sha256`), `none` | `sums-url` |
| `sums_url_template` | Sums file URL (for `sums-url`/`block-url`) | see §5 |
| `image_glob` | Filename glob for rotating-name resolution (§4.2.1); **per-arch values** `image_glob[arch]` (§2.4). Unused for `latest-alias` profiles | `CentOS-Stream-GenericCloud-10-*.aarch64.qcow2` |
| `sums_algorithm` | `sha256` or `sha512` (Debian publishes **SHA512SUMS only** — verified) | `sha512` |
| `url_stability` | `latest-alias` = the templated URL is stable and cacheable by release; `rotating` = filename changes per build, the *sums file is the name source of truth* (§4.2, G7) | `rotating` |
| `default_user` | User **baked into the image** (empty = none; the cloud-init `users:` block is then the only login). Distros whose cloud-init `system_info` defines a first-boot default (Fedora `fedora`, CentOS Stream `cloud-user`, SUSE `opensuse`) count as **none** here, because any user-data `users:` list suppresses that default (cloud-init semantics — verified). Non-empty values also enable `clouduser-ssh-key` (G4) | `ubuntu`, `almalinux`, `` (others) |
| `suggested_user` | Default `--user` when `default_user` is empty | `fedora`, `debian` |
| `sudo_group` | Group granting sudo in the image | `sudo`, `wheel` |
| `boot` | `bios` or `uefi` (auto-applied as `virt-install --boot`; §4.4). All v1 profiles are `bios` (Fedora/Alma images are hybrid); `uefi` is for UEFI-only images (e.g. Fedora UEFI-UKI variant). **Applies to x86_64 only** — aarch64 guests are always UEFI (§2.4) | `bios` |
| `osinfo_candidates` | Ordered osinfo ids to try against the host DB, ending in `generic` (always present — verified) | `fedora44 fedora43 fedora42 generic` |
| `serial_console` | `yes` = image ships a ttyS0 getty, so `virsh console` is a valid debug hint; `no` = hint must change (§4.5) | `yes` |
| `notes` | Free-form, shown by `--list-distros` | |

The effective user is: `--user` (explicit) > `default_user` >
`suggested_user`. For all distros the `user-data` `users:` block
*creates or reinforces* the user, so a pre-existing default user and a
cloud-init-created user both work with the same template.

### 2.3 Profile table (initial)

Release strings accepted per distro (first listed = `default_release`);
all others accepted via `--release` as long as the template resolves —
the tool does **not** maintain an allowlist of releases (release strings
flow into the URL template; a 404 fails with a clear error).

| Distro | default_release | releases known-good | default/suggested user | sudo group | boot |
|---|---|---|---|---|---|
| `ubuntu` | `resolute` | `resolute`, `noble`, `mantic`, `jammy` | `ubuntu` | `sudo` | bios |
| `fedora` | `44` | `42`, `43`, `44` (numeric release) | – / `fedora` | `wheel` | bios (hybrid image) |
| `debian` | `trixie` | `trixie`, `bookworm` | – / `debian` | `sudo` | bios |
| `centos-stream` | `10` | `9`, `10` | – / `centos` | `wheel` | bios |
| `almalinux` | `10` | `9`, `10` | `almalinux` | `wheel` | bios (hybrid image) |
| `rocky` | `10` | `9`, `10` | – / `rocky` | `wheel` | bios |
| `opensuse` | `twee` | `twee` (Tumbleweed alias) | – / `opensuse` | `wheel`² | bios¹ |

¹ openSUSE stable alias is the *Minimal-VM* kvm-and-xen appliance
(verified §5.6); UEFI variants exist (`-sdboot`, `-grub-bls`) and can be
added as separate profile releases later.
² openSUSE `wheel`/sudo is the intended target, but SUSE cloud.cfg
default groups are `cdrom, users` (no `wheel`) and TW's stock sudo asks
for the root password — the e2e ticket must prove `sudo -n true` and,
if the Minimal-VM appliance lacks `sudo`, fall back to a `runcmd:` that
installs/configures it (see §5.6 and pitfall 16).

³ Profiles whose URL templates contain **no** `{release}` placeholder
(openSUSE) accept only `--release <default_release>` or none; any other
value exits 2 (`distro <d> has no release-scoped images; release '<r>'
not supported`).

### 2.4 Architecture: x86_64 and arm64 (goal 6)

Two architectures are first-class: `x86_64` and `aarch64` (spelled
`arm64` in most upstream filenames and in the CLI; normalized to
`aarch64` internally).

**Architecture is not a profile row; it parameterizes profiles.**
Each profile's arch-dependent fields are *per-arch templates* (G16),
not one template with a `{arch}` placeholder, because the differences
are sometimes more than the filename:

- **`image_url_template` / `sums_url_template`**: one template per
  arch, as separate fields (`image_url_template[x86_64]`,
  `image_url_template[aarch64]`, …). The x86_64 templates are exactly
  the v1.0 values (§5.2–5.8); the aarch64 templates are the §5.10
  values. Six of seven distros are mechanical path/filename swaps
  (`x86_64/`→`aarch64/` in the path, `amd64`/`x86_64`→`arm64`/`aarch64`
  in the basename, same sums file or a per-arch CHECKSUM); **openSUSE
  is the exception** — its aarch64 tree lives under a completely
  different URL root (`/ports/aarch64/tumbleweed/appliances/` vs
  `/tumbleweed/appliances/`) with a different stable-alias name
  (`…-Minimal-VM.aarch64-Cloud.qcow2` vs
  `…-Minimal-VM.x86_64-kvm-and-xen.qcow2`), verified §5.10 (pitfall 21).
  A single `{arch}`-substituted template cannot express that. The
  per-arch set also includes `image_glob[arch]` — the filename glob the
  rotating-name resolution (§4.2.1) selects against (unused for
  `latest-alias` profiles, but present in the schema for uniformity).
- **`boot`**: the profile `boot` field is the x86_64 value. For
  aarch64 the effective boot mode is **always `uefi`**, overriding the
  profile: `--boot bios` with aarch64 is a usage error (exit 2: `aarch64
  guests cannot boot BIOS`), while an explicit `--boot uefi` is accepted
  as a no-op (UEFI is already forced). Rationale: QEMU's aarch64 machine type
  has no legacy-bios firmware at all; every aarch64 cloud image in the
  matrix (Ubuntu, Fedora, Debian, CentOS Stream, AlmaLinux, Rocky,
  openSUSE) is UEFI/GPT (§5.10). The x86_64 "hybrid" claims of
  Fedora/Alma do not extend to their aarch64 builds.
- Everything else (user/sudo group, osinfo candidates, `sums_kind`,
  `sums_algorithm`, `serial_console`, cloud-init generation, MAC/
  dnsmasq scheme, vol steps, `vm-destroy`) is architecture-agnostic —
  no per-arch values needed.

**`--arch` (new CLI option, §3.1)**: `auto` (default) resolves to the
host's `uname -m` mapped into the supported set. Effective guest arch
= host arch, always (G13). Preflight errors:

- `--arch` not `auto|x86_64|aarch64|arm64` → exit 2 (usage).
- requested arch ≠ host arch → exit 1: `guest arch <a> does not match
  host arch <b>; cross-architecture emulation (TCG) is not supported —
  run virt-runner on native <a> hardware`.
- host arch outside `{x86_64, aarch64}` → exit 1: `virt-runner supports
  host architectures x86_64 and aarch64 only (detected <host-arch>)`.

**UEFI preflight becomes arch-aware (§4.1).** For effective boot mode
`uefi` the check looks for *that architecture's* firmware:

- x86_64: `ls /usr/share/OVMF/OVMF_CODE*.fd` (unchanged from v1.0).
- aarch64: probe, in order, `/usr/share/AAVMF/AAVMF_CODE*.fd` or
  `/usr/share/qemu-efi-aarch64/QEMU_EFI*.fd` (Debian/Ubuntu
  `qemu-efi-aarch64` package), then `/usr/share/edk2/aarch64/QEMU_EFI*.fd`
  (Fedora/RHEL `edk2-aarch64` package). **Note:** the Debian/Ubuntu
  `ovmf` package is `Architecture: all` containing **x86_64 firmware
  only** — `/usr/share/OVMF/OVMF_AARCH64*` exists on no current distro
  (verified by package-content inspection in the 2026-08-26
  verification pass; pitfall 18). If all file probes miss, the
  authoritative check is `virsh domcapabilities --arch aarch64`
  advertising a `<firmware>` (virt-install's `--boot uefi` resolves
  firmware through libvirt domcapabilities, per its man page —
  confirmed). Failure → exit 1: `UEFI boot requires aarch64 firmware
  (AAVMF / QEMU_EFI.fd); install the 'qemu-efi-aarch64' (Debian/Ubuntu)
  or 'edk2-aarch64' (Fedora/RHEL) package`.

**virt-install on aarch64**: no `--arch` flag is needed (guest arch =
host arch is the default); osinfo ids map automatically to
`-machine virt` and the virt CPU model for aarch64, and the existing
virtio network model + fixed-MAC/dnsmasq lease discovery are
arch-agnostic. Serial on the `virt` machine is PL011 (`ttyAMA0`), not
`ttyS0` — images ship the correct `console=` for their arch, so
`virsh console` remains a valid debug hint; per-distro
`serial_console=yes` claims on aarch64 are **verify-on-first-run**
(§5.10 checklist).

**Cache layout (§4.2)**: x86_64 keeps the v1.0 layout byte-identically
(incl. legacy `~/vm-images/resolute/`); aarch64 downloads go to the
same path plus an `aarch64/` component — `~/vm-images/resolute/aarch64/`,
`~/vm-images/fedora/44/aarch64/`, `~/vm-images/custom/aarch64/`
(G15).

**Excluded distros** (must fail with a clear "not supported" usage error,
not a broken image):

- `rhel` — cloud images require subscription/registration access
  (see §8 follow-up; do not ship a half-working profile).
- `alpine`, `arch` — default images do not ship cloud-init/NoCloud;
  out of the feature's design (would need initramfs/first-boot-script
  injection, a different mechanism).

The list and its reasons live in `config/distro-profiles.sh` as an
`excluded_distros` map (`get_excluded_distros` prints `name<TAB>reason`);
the §4.1 preflight uses it for the explicit "not supported: <reason>"
text required by AC-G2 (excluded names are distinct from merely
unknown ones).

---

## 3. CLI Changes

### 3.1 `vm-create`

```
vm-create <NAME> [OPTIONS]
```

New/changed options (existing options unchanged unless noted):

| Option | Meaning | Default |
|---|---|---|
| `--arch A` | Guest architecture: `auto` \| `x86_64` \| `aarch64` (alias `arm64`, normalized). `auto` = host arch; must match host arch (no TCG cross-arch, §2.4, G13). | `auto` |
| `--distro D` | Distro profile name (`ubuntu`, `fedora`, `debian`, `centos-stream`, `almalinux`, `rocky`, `opensuse`). Selects image URL template, checksum handling, user/sudo-group, boot mode, osinfo candidates. | `ubuntu` |
| `--release REL` | Now **distro-scoped**: release/codename for the active `--distro`. Backward compatible: without `--distro`, a bare `--release` still means an Ubuntu release (G1). | profile's `default_release` (Ubuntu: `resolute`) |
| `--image-sha256 HASH` | 64-hex digest of the image to enforce. Applies to both `--image` and profile-resolved URLs. When present it is the **authoritative** verification and supersedes sums-file logic (G8). | off |
| `--boot MODE` | `auto` \| `bios` \| `uefi`. `auto` = profile's `boot` field (x86_64). `uefi` requires the host firmware (preflight check, §4.1). On effective arch aarch64: `bios` → exit 2 (G14, no BIOS exists), explicit `uefi` → accepted no-op. | `auto` |
| `--wait-lease SECS` | Lease-file polling window (scales the 2 s cadence). Some distros first-boot slower (SELinux relabel etc.). | `120` |
| `--wait-ssh SECS` | SSH verification window. | `90` |
| `--list-distros` | Print the profile table: one line per distro with **distro, default release, default user, boot (as the `x86_64/aarch64` pair, e.g. `bios/uefi`)**; exit 0 (AC-G2). | — |

Precedence (extends POC decision D1):

1. `--image` (with optional `--image-sha256`) overrides the profile's
   image URL **for the effective arch**; the *distro profile still
   governs* user/sudo-group/boot/osinfo (the image is assumed to be a
   cloud image of that distro *for that arch*).
2. `--distro` + `--release` select the template URLs.
3. `--release` without `--distro` ⇒ `--distro ubuntu` (G1).
4. `--os-variant` is **not** a new CLI flag; osinfo resolution is
   automatic from the profile (§4.4). Escape hatch for the future only —
   keep the CLI small. (Decision G5.)

Errors: unknown `--distro` → exit 2, usage, plus the supported-distro
list. `--release` values that don't resolve to a downloadable file →
download failure at step 2 (exit 1) with the attempted URL printed.

### 3.2 Output block change

The §3.1 success block gains exactly one line (G9), inserted after `Name:`:

```
VM created and running.
Name:    poc-fedora   (UUID 6f1c2b3a-...)
Distro:  fedora 44 [aarch64]
IP:      192.168.122.52   (also reachable as poc-fedora.default)
SSH:     ssh fedora@192.168.122.52
Console: virsh console poc-fedora     (Ctrl-] to detach)
Teardown: vm-destroy poc-fedora   (or: virsh destroy poc-fedora && virsh undefine poc-fedora)
```

`Distro:` = `<distro> <effective-release>`, plus ` [aarch64]` **only
when the effective arch is aarch64** — on x86_64 the line is
byte-identical to v1.0's `fedora 44` (protects AC-G1). For bare
`--image` runs: `<distro> custom:<url-basename>` (same arch-suffix
rule). The `--no-boot` variant gains the same
line. Everything else (field alignment, IP-before-SSH rule) unchanged.

### 3.3 `vm-destroy` / `vm-list`

- `vm-destroy`: **no changes** — it is already distro-agnostic
  (domain + `<NAME>_vda.qcow2` in `vm-pool`).
- `vm-list`: no functional change. Documented limitation: the distro/user
  used at creation is not recorded in the domain XML, so the `SSH:` line
  keeps its `--user` display override (default `ubuntu`). Follow-up §8:
  optional sidecar metadata file.

---

## 4. Pipeline Changes (per step)

Steps 1, 4 (mostly), 5 and 6 of the POC pipeline keep their structure;
only the marked parts change.

### 4.1 Step 1 — Preflight

Add, after the existing checks (all existing checks unchanged):

1. **Profile resolution**: `--distro` must be a known profile (exit 2
   otherwise, listing supported distros). Names in `excluded_distros`
   (§2.4) are rejected with their recorded reason (`not supported:
   <reason>`, AC-G2) rather than the generic unknown-distro text.
   Compute the effective release (G1 precedence, incl. the §2.3 fn ³
   no-placeholder rule) and all derived values (URLs, user, group, boot,
   osinfo).
2. **Arch resolution** (§2.4, G13): read host arch from `uname -m`;
   unsupported host arch → exit 1. Normalize `--arch` (`arm64` →
   `aarch64`); requested ≠ host → exit 1 (TCG message). Select the
   profile's per-arch URL/sums templates and the effective boot mode
   (aarch64 ⇒ `uefi`, G14; `--boot bios` + aarch64 ⇒ exit 2).
3. **UEFI check** (only when effective boot mode is `uefi`), against the
   *effective arch's* firmware (§2.4): x86_64 probes
   `ls /usr/share/OVMF/OVMF_CODE*.fd` (verified present on this host:
   `OVMF_CODE_4M.fd`, `OVMF_VARS_4M.fd`, …) with failure message `UEFI
   boot requires OVMF (not found in /usr/share/OVMF); install the
   'ovmf' package or use --bios`; aarch64 probes the
   `qemu-efi-aarch64`/`edk2-aarch64` paths with the §2.4 failure
   message (no `--bios` out — there is no BIOS fallback).
4. `--image-sha256`, when given, must match `^[0-9a-fA-F]{64}$` (exit 2).

### 4.2 Step 2 — Image download + verify (generalized)

Cache layout (G3):

```
~/vm-images/
  resolute/                                  # Ubuntu x86_64: legacy layout PRESERVED
    resolute-server-cloudimg-amd64.img
    SHA256SUMS
  resolute/aarch64/                          # + aarch64/ component for arm64 (G15)
    resolute-server-cloudimg-arm64.img
    SHA256SUMS
  fedora/44/                                 # other distros: <distro>/<release>/ (x86_64)
    Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
    Fedora-Cloud-44-1.7-x86_64-CHECKSUM
  fedora/44/aarch64/
    Fedora-Cloud-Base-Generic-44-1.7.aarch64.qcow2
    Fedora-Cloud-44-1.7-aarch64-CHECKSUM
  debian/trixie/
    debian-13-genericcloud-amd64.qcow2
    SHA512SUMS
  custom/                                    # bare --image (unchanged); + custom/aarch64/
```

Ubuntu keeps the POC's exact `<release>/` paths so the existing ~820 MiB
cache remains valid with zero re-download. The `aarch64/` suffix rule
is additive: x86_64 paths never change, so an existing x86_64 cache is
untouched by the arch extension (G15).

Flow changes inside `fetch_and_verify_image`:

1. **URL resolution.**
   - `latest-alias` profiles: URL = template with `{release}`; the cached
     file is `<cache-dir>/<image-basename>`.
   - `rotating` profiles (currently: `centos-stream`): **fetch the sums
     file first**; parse it per `sums_kind`; select the entry whose
     filename matches the profile's **per-arch** image *glob* (e.g.
     `CentOS-Stream-GenericCloud-10-*.x86_64.qcow2` on x86_64,
     `…*.aarch64.qcow2` on aarch64) and whose date component is the
     newest; download **that** file; verify against the same sums line
     (G7). Cache under the *resolved* filename, so a build released on
     a later day downloads once and is then cached. "Newest" is always
     per-arch within the same sums file — never mix architectures in
     one glob (aarch64 builds lag x86_64, §5.10/pitfall 20).
2. **Verification** — one code path, parameterized by `sums_kind` +
   `sums_algorithm`:
   - `sums-url` (Ubuntu, AlmaLinux, Debian): existing
     `grep -F "<name>" <sums> | sha256sum -c -` (Debian uses
     `sha512sum -c -`). The sums file lines are sha256sum/
     sha512sum-compatible — verified for Ubuntu (2-space/`*name`),
     AlmaLinux (2-space), Debian (2-space).
   - `block-url` (Fedora, CentOS Stream, Rocky): PGP-signed file whose
     payload lines are `SHA256 (<name>) = <hash>`. Parser: skip lines
     until the PGP body (or simply regex every line), extract the line
     matching the image filename with
     `^SHA256 \((<name>)\) = ([0-9a-f]{64})$`, compare against local
     `sha256sum`. The PGP wrapper is **parsed around, not verified** (§8).
   - `sidecar` (openSUSE): fetch `<image-url>.sha256` (single line
     `<hash>  <name>`, sha256sum-compatible — verified), verify in place.
   - `none`: existing D1 behavior (warn, skip).
3. **`--image-sha256` (G8)**: when set, compute the local digest with
   `sha256sum` and compare (case-insensitive) *instead of* the
   sums-file step; mismatch → same failure path (delete file, exit 1 /
   `--keep-going`).
4. Cache-hit / `--keep-going` / partial-file cleanup semantics:
   unchanged.
5. `file://` URLs: unchanged fast-path; `--image-sha256` works with
   `file://` too (useful for fixture tests — keep the existing
   `VM_CREATE_CACHE_DIR` hook).

### 4.3 Step 3 — cloud-init generation (parameterized)

`user-data` template becomes:

```yaml
#cloud-config
manage_etc_hosts: true
users:
  - name: <EFFECTIVE_USER>
    groups: [<PROFILE.sudo_group>]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    ssh_authorized_keys:
      - <ssh key line>
package_update: false
```

- `groups:` is the profile's `sudo_group` (`sudo` for ubuntu/debian,
  `wheel` for fedora/centos-stream/almalinux/rocky/opensuse).
- `meta-data` (incl. the NoCloud `local-hostname:` fix from ticket 05)
  is unchanged.
- No `network:` section — unchanged, applies to all distros.

`virt-install` `--cloud-init` sub-options:

- Always: `user-data=`, `meta-data=`, `disable=on` (Pitfall 3/4
  unchanged).
- `clouduser-ssh-key=<SSHKEY>`: included **only when the profile's
  `default_user` is non-empty** (G4). Rationale: it injects the key into
  the image's pre-created cloud user; on images with no default user
  (Fedora, Debian) the `users:` block is the only injection path, and the
  sub-option's effect under a `generic` osinfo is undefined — omit it
  there. (Verify on the first Fedora/Debian boot that SSH works with the
  block alone; if `clouduser-ssh-key` proves harmless and useful,
  revisit — but the safe default is omit.)

### 4.4 Step 4 — virt-install (osinfo + boot)

Two flag changes, everything else (vol-create/vol-upload/vol-resize,
MAC scheme, `--import`, `--console none`, `--autostart`, cleanup-on-fail)
unchanged:

1. **`--os-variant`**: resolve from the profile's `osinfo_candidates` —
   first id present in `virt-install --osinfo list` (host DB verified to
   contain e.g. `fedora42`, `debian13`, `centos-stream10`, `almalinux10`,
   `opensuse42.3`/`15.6`, and `generic`; it does **not** contain
   `fedora43`/`fedora44` or `rocky10` — so the `generic` fallback is a
   live path, not a theory). Ubuntu keeps `ubuntu-lts-latest` as its
   first candidate (preserving POC behavior) with `generic` last.
   Candidates are a **static** string list per profile (no release
   arithmetic at runtime); a non-default `--release` whose id is absent
   from the list simply falls through to the next candidate, ending at
   `generic`.
2. **Boot mode**: effective `--boot` (G6), resolved per §2.4 —
   aarch64 ⇒ always `uefi` (G14), x86_64 ⇒ profile value/`--boot`.
   `uefi` ⇒ add `virt-install --boot uefi`. `bios` ⇒ no flag (current
   behavior). No `--arch` flag is passed to virt-install: on supported
   hosts the guest arch equals the host arch, which is the default.

### 4.5 Step 5 — IP wait + SSH verify

- Polling/verification logic unchanged.
- On aarch64 the QEMU serial port is PL011 (`ttyAMA0`), not `ttyS0`;
  images carry the correct `console=`, so the same `virsh console`
  debug hint is valid (per-distro first-boot check, §5.10).
- Wait windows: `--wait-lease` (default 120 s) and `--wait-ssh`
  (default 90 s) scale the 2 s cadence (attempts = window/2, min 1).
- Error messages: the `virsh console` hint is printed **only when the
  profile's `serial_console` is `yes`**. For `no`-serial profiles the
  timeout message is: `no DHCP lease after Ns — the image may not expose
  a serial console; attach VNC: virsh vncdisplay <NAME>`.
  (All §5 profiles are `yes` **except openSUSE (`no`, §5.6)** —
  verify-on-first-run per row.)

### 4.6 Step 6 — output

Per §3.2 (adds `Distro:` line). `vm-list` unchanged; the
distro-per-VM limitation is documented there (G12, §8).

---

## 5. Image Source Registry (verified facts)

All facts in this section were **probed live from this host on
2026-08-26** with `curl` against the official origins (redirects
followed), unless a row is explicitly marked **verify-on-first-run**.
Re-verify at ticket time; release numbers move.

### 5.1 Support matrix summary

| Distro | Phase | URL stability | Sums kind | Algo | Boot (x86_64 / aarch64) |
|---|---|---|---|---|---|
| ubuntu | (existing) | latest-alias | sums-url | sha256 | bios / uefi |
| fedora | 1 | latest-alias¹ | block-url | sha256 | bios (hybrid) / uefi |
| debian | 1 | latest-alias | sums-url | **sha512** | bios / uefi |
| centos-stream | 1 | **rotating** | block-url | sha256 | bios / uefi |
| almalinux | 1 | latest-alias | sums-url | sha256 | bios (hybrid) / uefi |
| rocky | 2 | latest-alias | block-url | sha256 | bios / uefi |
| opensuse | 2 | latest-alias | sidecar | sha256 | bios / uefi |

Every distro in the matrix has **both** x86_64 and aarch64 images
(§5.2–5.8 x86_64, §5.10 aarch64). aarch64 boot is uniformly `uefi` —
no legacy-BIOS firmware exists for QEMU's aarch64 machine (G14).

¹ per *build* (e.g. `44-1.7`); the build number is part of the filename
but the URL is only re-resolved when the cache is cold, so a point
release refresh costs one re-download — acceptable, documented. The
aarch64 tree uses the *same scheme* (`44-1.7` at probe, verified
§5.10), but the `{point}` is resolved **independently per arch** from
that arch's directory listing (an aarch64 tree may lag — pitfall 20).

### 5.2 Ubuntu (baseline — unchanged)

| Fact | Value (verified 2026-08-26) |
|---|---|
| Image URL | `https://cloud-images.ubuntu.com/{release}/current/{release}-server-cloudimg-amd64.img` — HTTP 200 for `resolute` |
| Sums | same-dir `SHA256SUMS`, sha256sum-compatible; contains entry `9dc7c536… *resolute-server-cloudimg-amd64.img` |
| default user / sudo group | `ubuntu` / `sudo` (POC-verified, stable across releases) |
| Boot / serial | bios / yes (POC-verified: serial getty on ttyS0) |
| osinfo | `ubuntu-lts-latest` first (POC workaround), `generic` last |

### 5.3 Fedora

| Fact | Value |
|---|---|
| Current release (verified) | **44** (`releases/` index lists 40–44; 44 is the top). Point release at probe time: `44-1.7` |
| Image URL (verified 200 tree) | `https://download.fedoraproject.org/pub/fedora/linux/releases/{release}/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-{release}-*.x86_64.qcow2` — **layout moved**: 42 and earlier used `Cloud/x86_64/fedora-cloud-<rel>-…qcow2`; 43+/44 use `Cloud/x86_64/images/Fedora-Cloud-Base-*`. The profile must handle both layouts (glob the dir or keep two templates keyed by release ≥ 43). |
| Sums (verified) | Per-directory `Fedora-Cloud-{release}-{point}-x86_64-CHECKSUM` (fetched live: PGP-signed, payload lines `SHA256 (<file>) = <hash>`) — **not** sha256sum-compatible. The build/point number is in the filename, so the sums URL is itself versioned: resolve it from the small directory listing (~10 entries at `Cloud/<arch>/images/`) and use the CHECKSUM of the newest build, downloading the image **of that same build** (G7-style: sums file is the source of truth for both name and hash). Exact resolution decided at ticket time. **Per-arch:** the `{point}`/build number comes from *that arch's* listing (an aarch64 tree may lag x86_64 — pitfall 20); a resolved x86_64 `{point}` must never be reused for aarch64. |
| Boot mode (docs-verified) | **Hybrid BIOS+UEFI** — the `FedoraCloudHybridBoot` change (fedoraproject.org wiki) is implemented: cloud base images are GPT-partitioned and "equipped to boot either with legacy BIOS or UEFI" (kickstart commit 611edda149, fedora-kickstarts). Profile is `boot=bios`; `--boot uefi` also works. The separate `Fedora-Cloud-Base-UEFI-UKI-…qcow2` (verified in the 44 tree) *is* UEFI-only — candidate for a follow-up profile release. OVMF is installed on this host (`/usr/share/OVMF/`, verified), so the UEFI path is testable. |
| default user / sudo group (docs-verified) | **No user is baked into the image.** cloud-init `system_info` defines default user `fedora` (cloud-init-fedora.cfg: `default_user: name: fedora, lock_passwd`); the kickstart states "Cloud-init creates a user account named 'fedora' with passwordless sudo access. The root password is empty and locked." Our user-data `users:` list suppresses that default, so our created user is the only login; sudo via `wheel` + NOPASSWD (verify `sudo -n true` in e2e). |
| Serial console | yes expected (historical Fedora cloud images ship `console=ttyS0,115200`) — verify-on-first-run via `virsh console` during first boot |
| osinfo candidates | `fedora{release} fedora{release-1} … fedora42 generic` (DB verified to stop at fedora42) |

### 5.4 Debian

| Fact | Value |
|---|---|
| Current stable (verified) | **trixie** = 13. `latest/` alias is a stable redirect (index + files verified). |
| Image URL (verified) | `https://cloud.debian.org/images/cloud/{release}/latest/debian-{NN}-genericcloud-amd64.qcow2` (NN = release number: trixie→13, bookworm→12). Bookworm alias also verified (348 MiB). |
| Sums (verified) | **`SHA512SUMS` only — there is NO `SHA256SUMS`** (404 verified). Lines are sha512sum-compatible: `<64-hex-sha512-double… 128-hex>  debian-13-genericcloud-amd64.qcow2`. ⇒ `sums_algorithm=sha512`, verify with `sha512sum -c -`. |
| default user / sudo group (docs-verified) | **No default credentials** — "There are no default credentials with these images. You're expected to provide an ssh public key via … cloud-init" (debian-cloud list). The users block is mandatory (it always is). Sudo group `sudo`. |
| cloud-init / NoCloud / serial (docs-verified) | NoCloud datasource supported; **serial console confirmed**: Debian cloud images set `console=tty0 console=ttyS0,115200 earlyprintk=ttyS0,115200` (Debian bug #1098874). Note: trixie's new first-boot flow shows a root-password prompt on the serial console at first boot — non-interactive for our SSH path ("just press enter", debian-cloud 2026); confirm it does not block the SSH path on the first e2e boot. |
| Boot | bios (genericcloud is MBR+BIOS and UEFI capable) |
| osinfo candidates | `debian13 debian12 generic` (debian13/debiantrixie verified in DB) |

### 5.5 CentOS Stream

| Fact | Value |
|---|---|
| Current (verified) | 10 (and 9) available. |
| Image URL (verified) | `https://cloud.centos.org/centos/{release}-stream/x86_64/images/` with **date-versioned** names `CentOS-Stream-GenericCloud-{release}-<YYYYMMDD>.<build>.x86_64.qcow2`. **The `CentOS-Stream-{N}-genericcloud-latest.x86_64.qcow2` alias returns HTTP 502 (does not resolve)** on both 9 and 10 ⇒ `url_stability=rotating`; newest-build resolution from the CHECKSUM file (G7). Newest 10-stream build at probe time: `20260810.0`. **Re-probe 2026-08-26 (verification pass):** the *correctly-cased* alias `CentOS-Stream-GenericCloud-{N}-latest.<arch>.qcow2` **does** resolve (HTTP 206; `latest` entries present in both archs' CHECKSUMs) — only the lowercase name probed above is dead. G7 (sums-pinned, hash-pinned cache) remains primary; the alias is a fallback, not the source of truth (pitfall 3). |
| Sums (verified) | Same-dir `CHECKSUM`, PGP-signed, block format `SHA256 (<file>) = <hash>`, listing **all** builds (date parse for "newest" = `YYYYMMDD` field, tie-break on the `<build>` int). |
| default user / sudo group (docs-verified) | **No baked-in user.** cloud-init's `system_info` default for Stream 9/10 is **`cloud-user`** (cloud-init PR #1639; RHEL bug 2115576; EC2 AMIs use `ec2-user`) — but our user-data `users:` list suppresses it, so the profile's suggested user is `centos`, created by us. Sudo group `wheel` (verify `sudo -n true` in e2e). |
| Serial console | yes expected (RHEL-family cloud images) — verify-on-first-run |
| Checksum sidecars (verified) | 9-stream also publishes per-image `.SHA256SUM` sidecars (seen in the 9-stream index) — usable as a fallback verification path; the aggregate block-format `CHECKSUM` remains primary (G7 needs it for name resolution anyway) |
| Boot | bios (genericcloud is BIOS+UEFI) |
| osinfo candidates | `centos-stream{release} generic` (centos-stream9/10 verified in DB) |

### 5.6 openSUSE (Tumbleweed)

| Fact | Value |
|---|---|
| Stable alias (verified 206 range-probe) | `https://download.opensuse.org/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.x86_64-kvm-and-xen.qcow2` — **appliance** image (Minimal-VM), the only *dateless* Tumbleweed kvm qcow2 alias (full TW images exist only as `…-Snapshot<date>` files). |
| Sums (verified) | Sidecar `<name>.sha256`, single line `<hash>  <name>`, sha256sum-compatible (`.asc` sidecar also published). |
| cloud-init (docs-verified) | The Minimal-VM **Cloud** qcow2 variants "include the cloud-init tool necessary for automatic virtual machine configuration" (SUSE Virtualization docs); NoCloud is in the datasource order (LocalDisk, NoCloud, OpenStack, None — Portal:MicroOS/cloud-init). |
| default user / sudo group (docs-verified, risky) | SUSE cloud.cfg `system_info` default is `default_user: name: opensuse, lock_passwd: True` — created by cloud-init, suppressed by our `users:` list. **Risk:** SUSE cloud.cfg default groups are `cdrom, users` (no `wheel`), and TW's stock sudo asks for the *root* password — the Minimal-VM appliance may not even ship `sudo`. e2e must run `sudo -n true`; fallback is a `runcmd:` installing/configuring sudo (footnote ², §2.3). |
| Serial console | **no** — SDB:SerialConsole documents *enabling* serial on TW via `update-bootloader`, i.e. it is not on by default; the appliance may differ (verify). This profile exercises the §4.5 alternate-hint path (`virsh vncdisplay`). |
| Boot | bios for the plain `-kvm-and-xen` alias; `-sdboot`/`-grub-bls` aliases are the UEFI variants (not in v1 scope) |
| osinfo candidates | `opensuse-tumbleweed generic` (DB has versioned ids like `opensuse42.3`; plain `tumbleweed` id likely absent ⇒ `generic` is the real path — verify) |
| Note | Appliance is read-only-root (Btrfs subvolumes); fine for test VMs, `--disk` resize semantics unchanged (qcow2). Document in `notes`. |

### 5.7 AlmaLinux

| Fact | Value |
|---|---|
| Current (verified) | 10 (10.2 point at probe time). |
| Image URL (verified) | `https://repo.almalinux.org/almalinux/{release}/cloud/x86_64/images/` with **stable alias** `AlmaLinux-{release}-GenericCloud-latest.x86_64.qcow2` (date-versioned `…-10.2-20260526.0.…` also present). |
| Sums (verified) | Same-dir `CHECKSUM`, **plain sha256sum-compatible** (`<hash>  <name>`, 2-space, includes the `-latest` name — verified), GPG signature in separate `CHECKSUM.asc`. Existing `grep -F \| sha256sum -c -` pipeline works **unchanged**. |
| default user / sudo group (docs-verified) | **`almalinux`, baked into the image and locked** — "the default user … is `almalinux` and it's locked. Therefore we need to inject … a public ssh key" (AlmaLinux wiki). `clouduser-ssh-key` enabled (G4); sudo group `wheel` (verify `sudo -n true` in e2e). |
| Serial console | yes expected (RHEL-family cloud images ship `console=ttyS0`) — verify-on-first-run |
| Boot (docs-verified) | bios — GenericCloud images have **unified (hybrid) BIOS+UEFI boot since 8.9** (AlmaLinux cloud changelog + 2023-12-26 release blog); a `-UEFI-latest` compatibility symlink exists. |
| osinfo candidates | `almalinux{release} generic` (almalinux9/10 verified in DB) |

### 5.8 Rocky

| Fact | Value |
|---|---|
| Current (verified) | 10 (10.2 point at probe time). |
| Image URL (verified) | `https://dl.rockylinux.org/pub/rocky/{release}/images/x86_64/` with **stable alias** `Rocky-{release}-GenericCloud-Base.latest.x86_64.qcow2` (also `…-GenericCloud.latest`, `-LVM`, cloud-hypervisor variants; use `Base`). |
| Sums (verified) | Two options, both block format `SHA256 (<file>) = <hash>`: aggregate same-dir `CHECKSUM` (contains the `.latest` entries — verified) or per-image `<name>.CHECKSUM`. Use the aggregate (one fetch). |
| default user / sudo group (docs-verified, drifted) | **Unstable across builds** — older 9.x images baked in a locked `rocky` user; a 2023 bug ("should use cloud-init instead of rocky user") and current forum guidance say recent GenericCloud images have **no default creds** and rely on cloud-init. Treat as *no baked-in user* (users block authoritative, suggested `rocky`); e2e must prove login + `sudo -n true`. |
| Serial console | yes expected — verify-on-first-run |
| Boot | bios |
| osinfo candidates | `rocky{release} rocky9 generic` (DB verified to have rocky9 but **no rocky10**) |

### 5.9 RHEL — excluded (follow-up)

RHEL cloud images are **not publicly downloadable** — they live behind
the Red Hat Customer Portal login ("login required", confirmed in the
diskimage-builder `rhel` element docs and Red Hat's own download flow),
and even obtained they are expected to register. A profile would either
401 or ship a half-functional guest. Kept out of this feature; see §8.

### 5.10 arm64 (aarch64) image sources — all probed live 2026-08-26

Facts below were `curl`-probed from the x86_64 dev host on 2026-08-26
(redirects followed) against the same official origins as §5.2–5.8, and
re-verified by an independent web-verification pass the same day; its
corrections are folded in inline (CentOS cadence, openSUSE snapshot,
firmware facts in the checklist). Release numbers and build dates move;
re-verify at ticket time.

| Distro | aarch64 image URL (verified) | Sums (verified) | Boot | Notes |
|---|---|---|---|---|
| ubuntu | `https://cloud-images.ubuntu.com/{release}/current/{release}-server-cloudimg-arm64.img` (HTTP 200 for `resolute`) | same-dir `SHA256SUMS` already contains the arm64 entry (verified: `3e113fdd… *resolute-server-cloudimg-arm64.img`) — **no** per-arch sums file | uefi | Pure basename swap (`-amd64`→`-arm64`); sums path unchanged per arch |
| fedora | `https://download.fedoraproject.org/pub/fedora/linux/releases/{release}/Cloud/aarch64/images/Fedora-Cloud-Base-Generic-{release}-*.aarch64.qcow2` — dir listing verified: `Fedora-Cloud-Base-Generic-44-1.7.aarch64.qcow2` (+ `Fedora-Cloud-Base-UEFI-UKI-44-1.7.aarch64.qcow2`) | Per-dir `Fedora-Cloud-{release}-{point}-aarch64-CHECKSUM` (present in the same listing) — same PGP block format as x86_64 | uefi | Layout mirrors x86_64 exactly incl. the 43+/44 `images/` move (pitfall 4 applies per arch). `{point}` (build number) is resolved **per-arch** from that arch's listing — never reuse an x86_64 build number for aarch64 (pitfall 20) |
| debian | `https://cloud.debian.org/images/cloud/{release}/latest/debian-{NN}-genericcloud-arm64.qcow2` (HTTP 206 partial probe; `debian-13-genericcloud-arm64.qcow2` in the `latest/` listing) | same-dir `SHA512SUMS` covers both archs (one file, entries per filename) | uefi | Pure basename swap (`-amd64`→`-arm64`); arm64 images are UEFI/GPT (cloud-image docs) |
| centos-stream | `https://cloud.centos.org/centos/{release}-stream/aarch64/images/CentOS-Stream-GenericCloud-{release}-<YYYYMMDD>.<build>.aarch64.qcow2` (dir listing verified; 10-stream builds `20250904.0` … `20260825.0` at re-probe) | same-dir aggregate `CHECKSUM` (HTTP 200; PGP block format, aarch64 entries verified) + per-image `.SHA256SUM` sidecars; same G7 newest-build resolution, glob `*.aarch64.qcow2` | uefi | **Per-arch cadence drift observed 2026-08-26**: first probe — newest aarch64 = `20260427.0` vs x86_64 `20260810.0`; same-day re-probe — both archs at `20260825.0` (parity). Builds *can* drift per-arch: G7 newest-build resolution stays per-arch, and an old-but-present aarch64 build is **not** an error (pitfall 20) |
| almalinux | `https://repo.almalinux.org/almalinux/{release}/cloud/aarch64/images/AlmaLinux-{release}-GenericCloud-latest.aarch64.qcow2` (listing verified: stable alias + date-versioned `10.2-20260526.0` + `-ext4` variant) | same-dir `CHECKSUM` (plain sha256sum-compatible, one file serves both archs) | uefi | Same stable-alias pattern as x86_64; the §5.7 hybrid-boot claim is x86_64-only |
| rocky | `https://dl.rockylinux.org/pub/rocky/{release}/images/aarch64/Rocky-{release}-GenericCloud-Base.latest.aarch64.qcow2` (listing verified: `.latest` + `10.2-20260525.0`, each with its own `.CHECKSUM`) | same-dir aggregate `CHECKSUM` (verified) — block format, same as x86_64 | uefi | Same `.latest`-symlink caveat (pitfall 10) per arch |
| opensuse | `https://download.opensuse.org/ports/aarch64/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.aarch64-Cloud.qcow2` (HTTP 206; **different URL root** than x86_64 — the `ports/aarch64/` tree, per the `openSUSE:AArch64` wiki) | sidecar `<name>.sha256` (HTTP 200, single-line sha256sum format verified: `2b609ad8…  openSUSE-Tumbleweed-Minimal-VM.aarch64-Cloud.qcow2`) | uefi | Stable alias but **snapshot lags by weeks**: alias served `Snapshot20260331` at first probe and `Snapshot20260806` at the 2026-08-26 re-probe, vs x86_64 `Snapshot20260822` (aarch64 appliances build less frequently; pitfall 20). No `-sdboot`/`-grub-bls` aarch64 aliases observed in the appliances tree — uniformly UEFI with no alias choice. Same read-only-root Minimal-VM caveats as §5.6. The x86_64 `-kvm-and-xen` alias has no aarch64 twin — aarch64 uses `-Cloud` |

Per-arch verification checklist (**verify-on-first-run**, on an
**aarch64 host**, per e2e ticket):

- UEFI boot with aarch64 firmware (`qemu-efi-aarch64`: AAVMF /
  QEMU_EFI.fd on Debian/Ubuntu; `edk2-aarch64` on Fedora/RHEL —
  pitfall 18) — no BIOS fallback exists; a firmware failure is a hard
  boot failure, not a retry. Record the firmware path actually used
  (evidence line).
- Serial console: aarch64 images use PL011 `ttyAMA0`; confirm
  `virsh console` shows a getty for each distro that claims
  `serial_console=yes` (openSUSE keeps `no`, VNC hint).
- First-boot time on aarch64 (UEFI + cloud-init) — if any distro
  regularly exceeds the 120 s lease window, note it in the profile
  `notes` (per-arch `--wait-lease` default is a follow-up, not v1).

---

## 6. Pitfalls (new, distro-specific)

1. **Fedora checksum format is not sha256sum-compatible.**
   `SHA256 (name) = hash` inside a PGP wrapper. The POC's
   `grep -F | sha256sum -c -` path cannot parse it; a dedicated block
   parser is required (§4.2). Feeding it to `sha256sum -c` fails
   *silently* (`-c` finds no valid lines and exits 0 on some inputs) —
   the block parser must assert exactly one matching line was found.
2. **Debian publishes SHA512SUMS only.** Any code path hardcoding
   `SHA256SUMS`/`sha256sum` 404s or mismatches. `sums_algorithm` is a
   profile field, not a constant.
3. **The CentOS Stream `latest` alias is name-sensitive and historically
   dead.** The lowercase `CentOS-Stream-{N}-genericcloud-latest.<arch>`
   probe returned 502 on 9 and 10 (2026-08-26); a same-day re-probe
   found the *correctly-cased*
   `CentOS-Stream-GenericCloud-{N}-latest.<arch>.qcow2` alias **does**
   resolve (HTTP 206, `latest` entries in both archs' CHECKSUMs). Primary
   resolution stays the sums-pinned G7 newest-build (hash-pinned cache);
   the alias is a fallback only. Rotating names mean the *second* VM on
   a later day downloads a new image — expected, documented.
4. **Fedora moved its cloud image layout at 43/44** (`Cloud/x86_64/`
   → `Cloud/x86_64/images/`, `fedora-cloud-…` →
   `Fedora-Cloud-Base-…`). A single URL template cannot serve 42 and 44;
   key the template on release (two templates in the profile).
5. **Fedora boot mode is hybrid, not UEFI-only (corrected).** Early
   research assumed UEFI-only; the `FedoraCloudHybridBoot` change
   (implemented — fedora-kickstarts commit 611edda149) makes the generic
   cloud image GPT with both BIOS and UEFI boot paths. The profile uses
   `boot=bios`; the **UEFI-UKI** variant image *is* UEFI-only and would
   need `--boot uefi` (OVMF present on this host — verified). Keep the
   OVMF preflight for portability to hosts without it.
6. **The host osinfo DB is stale.** No `fedora43`/`fedora44`, no
   `rocky10` (verified). `generic` fallback is not an edge case — it is
   the expected path for the newest Fedora. `generic` exists in the DB
   (verified) and gives the correct virtio defaults for all profiles
   here.
7. **sudo group divergence.** `groups: [sudo]` on a `wheel`-group distro
   (or vice versa) creates a user that cannot sudo — a silent auth
   regression. Driven by the profile; each e2e ticket must run
   `ssh <user>@<ip> sudo -n true` as an acceptance check.
8. **Images without a pre-created user (Fedora, Debian).** Top-level
   `ssh-authorized-keys:` in user-data does nothing useful there; the
   `users:` block is the only login. (Already the tool's behavior — but
   the e2e must prove it, since today's tests all used Ubuntu.)
9. **openSUSE stable alias is a Minimal appliance**, not the full TW
   server image; full TW is date-versioned only. Profile `notes` must
   say so; do not promise "Tumbleweed server" in output text.
10. **Symlinked `latest` files change content in place** (Alma/Rocky).
    A cached file is a snapshot, not a link — fine, but `--keep-going`
    + "cache hit" after upstream refresh uses the *old* build. Document;
    no code change.
11. **`vol-upload` implicit resize** (existing bug 001, fixed with
    `vol-resize`) applies to *every* distro — the fix must stay in the
    shared step 4 path.
12. **502 ≠ 404 on some mirrors.** The CentOS `latest` probe returned
    502 (origin error), which can also be transient mirror trouble. The
    download step uses `curl -fL --retry 3` (existing) and the error
    message must print the exact URL attempted.
13. **A cloud-init `users:` list suppresses the distro's first-boot
    default user** (Fedora `fedora`, CentOS Stream `cloud-user`, SUSE
    `opensuse`). This is what makes our single `users:` block template
    authoritative everywhere — but it means the image's "documented
    default user" is not what you get; the effective user is always
    `--user`/the profile suggestion.
14. **CentOS Stream's cloud-init default is `cloud-user`, not
    `centos`** (CS9+; AMIs use `ec2-user`). Never assume `centos`
    exists — it exists only because we create it.
15. **Rocky's baked-in user drifted across builds** (locked `rocky` in
    older 9.x, cloud-init-only in newer). Any profile claiming a
    baked-in user for Rocky would rot; treat as none.
16. **openSUSE Minimal-VM sudo is unproven.** SUSE's default cloud.cfg
    groups exclude `wheel` and TW's stock sudo uses the root password;
    the appliance may lack `sudo` entirely. e2e gate: `sudo -n true`;
    fallback per footnote ² (a `runcmd:` installing/configuring sudo).
17. **Debian trixie first boot shows a serial root-password prompt**
    (new first-boot flow). Non-interactive for the SSH path, but a
    console-watcher (or future automation) must not misread it as a
    hang; verify on the first e2e boot.
18. **aarch64 firmware package and paths differ by host distro — and
    the Debian/Ubuntu `ovmf` package does NOT provide it** (corrected by
    the 2026-08-26 verification pass via downloaded package contents).
    Debian/Ubuntu: `ovmf` is `Architecture: all` and contains
    **x86_64 firmware only**; the aarch64 firmware comes from the
    `qemu-efi-aarch64` package → `/usr/share/AAVMF/AAVMF_CODE*.fd` and
    `/usr/share/qemu-efi-aarch64/QEMU_EFI*.fd`. Fedora/RHEL:
    `edk2-aarch64` → `/usr/share/edk2/aarch64/QEMU_EFI*.fd` (RPM payload
    verified). `/usr/share/OVMF/OVMF_AARCH64*` exists on **no** current
    distro — a preflight probing it would false-fail everywhere. Check
    the distro-specific paths above, or better, `virsh domcapabilities
    --arch aarch64` firmware advertising (virt-install's `--boot uefi`
    resolves through it anyway — confirmed in its man page).
19. **No legacy BIOS exists for aarch64 guests.** Any code path that
    assumes `boot=bios` works (error messages, `--boot` validation,
    profile fallbacks) is wrong on arm64. aarch64 ⇒ uefi is a hard
    invariant (G14); `--boot bios` + aarch64 must be a usage error,
    not a silently-broken VM definition.
20. **aarch64 image cadence can drift per-arch.** Observed 2026-08-26:
    CentOS Stream 10 aarch64 was months behind x86_64 in the first probe
    (20260427 vs 20260810) but reached full parity the same day in the
    re-probe (20260825 = 20260825); the openSUSE TW aarch64 alias
    lagged x86_64 by weeks (Snapshot20260331 → 20260806 vs 20260822).
    "Newest build" (G7) and "stable alias" are per-arch; never assume
    an x86_64 build date implies an aarch64 one, and don't treat an
    old-but-present aarch64 build as an error.
21. **openSUSE aarch64 lives under a different URL root**
    (`/ports/aarch64/tumbleweed/appliances/`) with a different alias
    name (`-Cloud` vs `-kvm-and-xen`). A single `{arch}`-substituted
    template 404s; the profile must carry separate per-arch templates
    (§2.4, G16). Same trap potential for future distros.
22. **Serial on aarch64 is `ttyAMA0` (PL011), not `ttyS0`.** Any
    tooling or docs that grep the console for `ttyS0` on arm64 will
    see nothing. `virsh console` itself is arch-agnostic (it attaches
    the first serial device), so the debug hint stays valid; only
    string-matching tooling is at risk.

---

## 7. Acceptance Criteria

Phase 1 (fedora, debian, centos-stream, almalinux) must pass all;
Phase 2 (rocky, opensuse) the same for those two distros.

- **AC-G1 (backward compatibility).** `vm-create poc-regress` with no
  distro options behaves exactly as v0.1.0: same cache dir
  (`~/vm-images/resolute/`), no re-download if cached, same flags to
  virt-install (`ubuntu-lts-latest`, no `--boot`), success block identical
  except the added `Distro: ubuntu resolute` line.
- **AC-G2 (discovery).** `vm-create --list-distros` prints one line per
  supported distro (name, default release, default user, boot as the
  `x86_64/aarch64` pair, e.g. `bios/uefi`) and exits 0. `vm-create poc-x --distro rhel` exits 2 with the supported list and
  an explicit "not supported" reason; `--distro alpine` likewise.
- **AC-G3 (Fedora).** `vm-create poc-fedora --distro fedora` (release
  44) from clean state: BIOS boot of the hybrid image, SSH as `fedora`
  (user created purely by our `users:` block — no baked-in user exists),
  `sudo -n true` exits 0, hostname = `poc-fedora`, `virsh console` shows
  a serial getty (verifies the serial-yes claim), checksum verified via
  the block parser. Optional second VM with `--boot uefi` proves the
  OVMF path. `vm-destroy poc-fedora` leaves `vm-pool` orphan-free.
- **AC-G4 (Debian).** `vm-create poc-debian --distro debian` (trixie):
  image verified with **sha512** (evidence line printed or logged), SSH
  as `debian`, `sudo -n true` OK, hostname correct, destroy clean.
- **AC-G5 (CentOS Stream, rotating).** `vm-create poc-cs --distro
  centos-stream` (10): resolved build name printed (e.g.
  `CentOS-Stream-GenericCloud-10-2026xxxx.0.x86_64.qcow2`), newest build
  chosen from CHECKSUM, SSH as `centos`, `sudo -n true` OK. A second
  `vm-create poc-cs2 --distro centos-stream` on the same day performs
  **no download** (cache hit on the resolved name).
- **AC-G6 (AlmaLinux).** `vm-create poc-almalinux --distro almalinux`
  (10): verified through the plain `CHECKSUM` (sha256sum-compatible path
  reused), SSH as `almalinux`, `sudo -n true` OK, destroy clean.
- **AC-G7 (digest enforcement).** `vm-create poc-digest --image
  file:///… --image-sha256 <correct>` succeeds; with one flipped hex
  digit it exits 1 (`SHA256 mismatch`), removes the cached file, and
  creates no VM/volume. Same for a profile URL (e.g. ubuntu resolute)
  with a wrong digest.
- **AC-G8 (windows).** `--wait-lease 20` fails a real boot attempt within
  ~20 s with the distro-appropriate hint (fixture or real); default
  behavior unchanged at 120 s/90 s.
- **AC-G9 (Phase 2).** Rocky 10 (`Rocky-10-GenericCloud-Base.latest.…`,
  block-format aggregate CHECKSUM) and openSUSE TW (stable Minimal-VM
  alias + `.sha256` sidecar) each pass the same per-distro pattern as
  AC-G3 (SSH, `sudo -n true`, hostname, clean destroy). For openSUSE,
  the timeout-hint path must render the VNC variant
  (`serial_console=no`), and if `sudo` is absent the footnote-² fallback
  is applied in-profile before acceptance.
- **AC-G10 (no regression).** The original five POC acceptance criteria
  (spec v1.0 §7) re-run green after all changes.
- **AC-G11 (arch guard).** On an x86_64 host: `vm-create poc-x
  --arch aarch64` exits 1 with the TCG/cross-arch message and creates
  no VM, volume, or download; `--arch arm64` normalizes the same way;
  `--arch x86_64` and `--arch auto` both behave exactly like omitting
  the flag. Same matrix mirrored on an arm64 host (`--arch x86_64` →
  exit 1). Additionally, `--boot bios` with effective arch aarch64
  exits 2 (usage error, G14) before any download, and explicit
  `--boot uefi` on aarch64 is an accepted no-op.
- **AC-G12 (arm64 e2e — requires an aarch64 host).** On an arm64
  machine with KVM + aarch64 UEFI firmware: (a) default `vm-create
  poc-arm-ubuntu` boots `resolute-server-cloudimg-arm64.img` from
  `~/vm-images/resolute/aarch64/`, verified via the *same* SHA256SUMS
  line, UEFI boot (aarch64 firmware — `qemu-efi-aarch64` AAVMF/QEMU_EFI.fd
  on Debian/Ubuntu hosts, `edk2-aarch64` on Fedora/RHEL; evidence line
  printed), `Distro: ubuntu resolute [aarch64]`, SSH as `ubuntu`,
  `sudo -n true` OK, hostname correct, clean destroy; (b) at least one of fedora/debian/almalinux on
  aarch64 passes the same per-distro pattern as AC-G3/AC-G4 (UEFI
  evidence, sha256/sha512 evidence, sudo, hostname, destroy); (c) the
  x86_64 acceptance suite (AC-G1 … AC-G10) re-runs green on the
  x86_64 host in the same release — the arch extension must be a no-op
  for x86_64 users (cache paths, virt-install flags, and output lines
  unchanged, incl. the *absence* of the `[aarch64]` suffix).

---

## 8. Out of Scope / Follow-ups

- **GPG signature verification** of the PGP-signed CHECKSUM files
  (Fedora/CentOS/Rocky). v1 parses around the signature; TLS to the
  official origin is the trust anchor. Follow-up: `gpg --verify` with the
  distro's release key, `--verify-signatures` opt-in.
- **RHEL** support (subscription-gated; needs `subscription-manager` or a
  user-supplied image → realistically only reachable via `--image`).
- **Per-VM metadata sidecar** (`~/.virt-runner/<name>.env` with distro,
  release, user, **arch**) so `vm-list` can print real per-VM SSH users.
- **Cross-architecture guests** (x86_64-on-arm64 or arm64-on-x86_64
  via QEMU TCG) — deliberately rejected (G13); if ever wanted, it is a
  separate feature with its own wait-window and firmware design.
- Compressed image sources (`.raw.xz`, `.tar.xz`) — none needed for the
  v1 matrix.
- Fedora UEFI-UKI variant (`Fedora-Cloud-Base-UEFI-UKI-…`) as a
  profile release with `boot=uefi` (the hybrid BIOS path is already
  covered by the default profile).
- openSUSE UEFI aliases (`-sdboot`, `-grub-bls`); the aarch64 TW
  appliance is uniformly UEFI with no alias choice (no aarch64
  `-sdboot`/`-grub-bls` observed in the appliances tree, §5.10).
- Other host architectures (ppc64le, s390x, riscv64, loongarch64) —
  preflight rejects them; adding one = per-arch templates (§2.4/G16)
  + an e2e host, not a code change.
- `--clean-cache` (carried over from POC §8).
- Additional distros (SLES, NixOS, Gentoo — mostly NoCloud-incompatible
  or niche; case-by-case later).

---

## 9. Suggested Ticket Breakdown

Following the established convention (self-contained tickets, strict
order, `poc-` names, full teardown, one commit each):

| # | Slice | Goal | Real VM? |
|---|---|---|---|
| G-01 | Profile framework | `config/distro-profiles.sh`, `--distro`/`--release` scoping (incl. fn ³ no-placeholder rule), `--list-distros` with boot-pair column, precedence G1, unknown-distro **and** `excluded_distros` reason-map errors (AC-G2); all 7 profiles registered (URL/sums fields from §5) | no (fixture/logic) |
| G-02 | Verification generalization | `sums_kind`/`sums_algorithm` paths: block parser (with one-match assertion), sha512 path, sidecar, `--image-sha256`; fixture-based tests incl. negative (corrupt file, not-in-sums, wrong digest) | no |
| G-03 | Rotating-name resolution | CENTOS-Stream: fetch CHECKSUM first, newest-build selection, cache under resolved name; fixture CHECKSUM file | no |
| G-04 | cloud-init + osinfo + boot | `sudo_group`/user parameterization, conditional `clouduser-ssh-key` (G4), osinfo candidate resolution (G5), `--boot`/OVMF preflight (G6), `Distro:` output line (G9), `--wait-*` (G10-ish) | no (fixture + optional `--no-boot` + `--boot uefi` dry run to prove the OVMF path) |
| G-05 | Fedora e2e | AC-G3 (BIOS first boot of the hybrid image, serial check, sudo check, block-verify evidence; optional `--boot uefi` second VM) | **yes** |
| G-06 | Debian e2e | AC-G4 (sha512 evidence) | **yes** |
| G-07 | CentOS Stream + Alma e2e | AC-G5, AC-G6 (rotating cache-hit second VM; plain CHECKSUM path) | **yes** |
| G-08 | Phase 2: Rocky + openSUSE e2e | AC-G9 | **yes** |
| G-09 | Architecture core (x86_64 + arm64, §2.4) | `--arch` option + `arm64` normalization; host-arch preflight (G13 error paths); per-arch URL/sums templates in `config/distro-profiles.sh` (aarch64 values from §5.10); aarch64⇒uefi boot resolution (G14) incl. `--boot bios`+aarch64 exit 2; arch-aware UEFI preflight (both firmware paths + domcapabilities probe); `aarch64/` cache layout (G15); ` [aarch64]` output suffix; per-arch rotating glob (G7); x86_64 behavior fixture-verified byte-identical | no (fixture/logic; on the x86_64 dev host: AC-G11 guard paths + dry-run prints of the resolved aarch64 URLs without downloading) |
| G-10 | Regression + docs (x86_64 host) | AC-G1/AC-G7/AC-G8/AC-G10, README/CHANGELOG update, `vm-list` limitation note, this spec's verify-on-first-run rows closed out | yes (ubuntu regression VM) |
| G-11 | arm64 e2e (**aarch64 host**) | AC-G12: arm64 Ubuntu e2e (default arch, UEFI evidence, SHA256SUMS line, `aarch64/` cache) + one of fedora/debian/almalinux; §5.10 checklist per row (UEFI boot, ttyAMA0 serial, first-boot timing) | **yes — must run on an aarch64 machine** (the x86_64 dev box cannot execute this ticket) |

Ground rules (inherited): only `poc-`-prefixed names; every VM/volume
torn down before the ticket finishes; `setup-test-vm` and the pre-existing
`noble-server-cloudimg-amd64.img` volume never touched; no sudo; the
~820 MiB resolute cache is not re-downloaded; large images are downloaded
at most once per ticket.

---

## 10. Decisions Log

- **G1 — Bare `--release` stays Ubuntu.** `--release noble` without
  `--distro` means `--distro ubuntu --release noble`. Preserves every
  existing invocation.
- **G2 — `--image` keeps the distro profile active.** An explicit image
  URL only replaces the *source*; user/sudo-group/boot/osinfo still come
  from `--distro` (default ubuntu). The tool assumes the URL is a
  cloud image of that distro — misuse is the caller's problem, printed
  via `Distro:` on success.
- **G3 — Cache layout.** Ubuntu keeps the POC `<release>/` layout
  (zero re-download); other distros use `<distro>/<release>/`; bare
  `--image` keeps `custom/`.
- **G4 — `clouduser-ssh-key` only for profiles with a *baked-in*
  default user (v1: `ubuntu`, `almalinux`).** Fedora, Debian,
  CentOS Stream, Rocky (drifted) and openSUSE omit it — none ships a
  user the option could target, and cloud-init `system_info` defaults
  are suppressed by our `users:` list anyway. The `users:` block is the
  single source of truth for the login everywhere.
- **G5 — osinfo by candidate list, `generic` last.** No new CLI flag;
  the list lives in the profile. `generic` is verified present in the
  host DB and correct (virtio) for every profile in this feature.
- **G6 — Boot mode is profile-driven with a `--boot` override.**
  `auto` reads the profile; `uefi` triggers the OVMF preflight. All v1
  profiles are `boot=bios` (Fedora and Alma images are hybrid — §5.3,
  §5.7), so UEFI is available but not required by any Phase-1 distro;
  it exists for UEFI-only images (Fedora UEFI-UKI variant, future
  profiles). The OVMF preflight stays for portability.
- **G7 — Rotating names resolve from the sums file.** Sums fetched
  first; newest matching build (date, then build number) selected; that
  exact filename is downloaded, verified, and used as the cache key.
  This makes verification and name resolution share one source of truth
  and keeps the cached file hash-pinned.
- **G8 — `--image-sha256` is authoritative.** A provided digest
  supersedes all sums-file logic (including for profile URLs), enabling
  pinned builds and fixture tests. Validation: 64 hex chars, else
  exit 2.
- **G9 — `Distro:` line added to both success blocks.** The only output
  change; documented in §3.2. Existing `D9` (format authority) applies to
  all other lines.
- **G10 — Wait windows become options** (`--wait-lease` 120,
  `--wait-ssh` 90), defaults identical to today, so no distro gets a
  hidden behavior change.
- **G11 — RHEL/Alpine/Arch are excluded by design**, with explicit
  not-supported errors (exclusion list + reasons in §2.4,
  `excluded_distros` map; §4.1 item 1), not half-supported profiles.
- **G12 — vm-list gets no per-VM distro knowledge in v1.** The
  limitation is documented; the sidecar-metadata follow-up is listed in
  §8. vm-destroy is untouched (already distro-agnostic).
- **G13 — Native-arch only; cross-arch is a hard error.** Guest arch
  always equals host arch; `--arch` states/verifies it, never selects
  emulation. Rationale: TCG boots take minutes (breaks the 120 s/90 s
  windows and the "fully unattended" goal), `qemu-system-<other-arch>`
  is usually absent (verified on this host: no
  `qemu-system-aarch64`, `virsh domcapabilities --arch aarch64` empty),
  and the requirement ("run on an arm64 and an x86_64 machine") means
  native-on-each. Error: exit 1, exact message in §2.4.
- **G14 — aarch64 forces UEFI, unconditionally.** QEMU's aarch64
  machine has no legacy-bios firmware; every aarch64 image in the
  matrix is UEFI/GPT (§5.10). Profile `boot` is x86_64-scoped;
  effective boot = `uefi` when arch = aarch64; `--boot bios` +
  aarch64 ⇒ exit 2. Consequences: the OVMF preflight must probe
  aarch64 firmware paths (pitfall 18), and every aarch64 e2e host
  needs the firmware package (`qemu-efi-aarch64` on Debian/Ubuntu, or
  `edk2-aarch64` on Fedora/RHEL) — documented in AC-G12's host setup.
- **G15 — aarch64 cache = x86_64 path + `aarch64/`.** x86_64 cache
  paths (incl. legacy `~/vm-images/resolute/`) are never modified;
  arm64 downloads sit in a subdirectory of the same per-distro path.
  One uniform rule covering all profiles and `custom/`;
  cross-arch cache confusion is impossible (filenames differ by arch
  anyway, the directory is belt-and-braces + consistent).
- **G16 — Per-arch URL/sums templates, not `{arch}` substitution.**
  Six distros are mechanical path swaps, but openSUSE's aarch64 tree
  is a different URL *root* and alias name (pitfall 21), so the profile
  schema carries `image_url_template[x86_64]`,
  `image_url_template[aarch64]`, `sums_url_template[...]`, and
  `image_glob[...]` as separate fields. Uniform across distros, self-documenting, and adding a third
  arch later = adding fields, not template surgery.
