#!/usr/bin/env bash
# shellcheck shell=bash
# =============================================================================
# install-prerequisites.sh — Install the host prerequisites for virt-runner
# =============================================================================
#
# Description:
#   Installs (or upgrades) the KVM + libvirt + virt-install toolchain that
#   virt-runner drives, and brings the host into the state the tool expects:
#
#     * KVM kernel modules + /dev/kvm access
#     * libvirt reachable at qemu:///system WITHOUT sudo
#       (user in the `libvirt` and `kvm` groups)
#     * the `default` NAT network defined, autostarted and active
#     * the `vm-pool` dir storage pool defined, autostarted and active
#       (all tool-created disks live there)
#
#   Idempotent: safe to re-run — existing packages are upgraded, existing
#   networks/pools are left alone (only started/autostarted when needed).
#
# Supports: Ubuntu / Debian (apt-get). Uses sudo internally (or runs as root).
#
# Usage:
#   ./bin/install-prerequisites.sh                 # install + configure
#   ./bin/install-prerequisites.sh --check         # audit only, change nothing
#   ./bin/install-prerequisites.sh --with-virt-manager
#
# Environment variables (all optional):
#   VIRT_USERNAME    User to add to libvirt/kvm (default: $SUDO_USER, else $USER)
#   VIRT_POOL_NAME   Storage pool the tool uses (default: vm-pool)
#   VIRT_POOL_DIR    Directory backing that pool
#                    (default: /var/lib/libvirt/images/vms)
#   VIRT_NET_NAME    NAT network the tool uses (default: default)
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Output helpers (self-contained — no shared library dependency)
# -----------------------------------------------------------------------------
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; RESET=$'\033[0m'
  RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[34m'
else
  BOLD=""; RESET=""; RED=""; GREEN=""; YELLOW=""; BLUE=""
fi

step()    { printf '\n%s==> %s%s\n' "${BOLD}${BLUE}" "$*" "${RESET}"; }
info()    { printf '    %s\n' "$*"; }
success() { printf '%s    + %s%s\n' "${GREEN}" "$*" "${RESET}"; }
warn()    { printf '%s    ! %s%s\n' "${YELLOW}" "$*" "${RESET}" >&2; }
error()   { printf '%sERROR: %s%s\n' "${RED}${BOLD}" "$*" "${RESET}" >&2; exit 1; }

# Interactive yes/no prompt. Reads from the controlling terminal so it works
# even when stdout is piped, and answers "no" (returns 1) when no terminal is
# available — a non-interactive run must never install anything by itself.
ask_yes_no() {
  local reply=""
  # 2>/dev/null goes FIRST: redirections apply left to right, so the open of
  # /dev/tty failing (no controlling terminal) stays silent this way.
  printf '%s [y/N] ' "$1" 2>/dev/null > /dev/tty || return 1
  read -r reply < /dev/tty 2>/dev/null || return 1
  [[ "${reply}" =~ ^[Yy]$ ]]
}

# -----------------------------------------------------------------------------
# Configuration (resolved before argument parsing so --help shows defaults)
# -----------------------------------------------------------------------------
: "${VIRT_USERNAME:="${SUDO_USER:-${USER:-}}"}"
: "${VIRT_POOL_NAME:=vm-pool}"
: "${VIRT_POOL_DIR:=/var/lib/libvirt/images/vms}"
: "${VIRT_NET_NAME:=default}"

export DEBIAN_FRONTEND=noninteractive

usage() {
  cat <<EOF
${BOLD}Usage:${RESET} $0 [OPTIONS]

Installs the KVM + libvirt + virt-install toolchain virt-runner drives and
brings the host to its expected state: qemu:///system reachable without sudo,
active '${VIRT_NET_NAME}' network, active '${VIRT_POOL_NAME}' storage pool.
Uses sudo internally; re-runnable.

${BOLD}Options:${RESET}
  --with-virt-manager   Also install the virt-manager GUI
  --check               Audit only: report what is missing and what would be
                        installed/configured; change nothing (exit 0)
  -h, --help            Show this help and exit

${BOLD}Environment variables${RESET} (all optional):
  VIRT_USERNAME     User for libvirt/kvm groups (default: ${SUDO_USER:-$USER})
  VIRT_POOL_NAME    Pool name (default: vm-pool)   = Virtualizer.POOL
  VIRT_POOL_DIR     Pool directory (default: /var/lib/libvirt/images/vms)
  VIRT_NET_NAME     Network name (default: default) = Virtualizer.NET
EOF
}

# -----------------------------------------------------------------------------
# Arguments
# -----------------------------------------------------------------------------
WITH_VIRT_MANAGER=false
CHECK_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-virt-manager) WITH_VIRT_MANAGER=true ;;
    --check)             CHECK_ONLY=true ;;
    -h|--help)           usage; exit 0 ;;
    *) error "Unknown option: $1 (see --help)" ;;
  esac
  shift
done

# -----------------------------------------------------------------------------
# Packages
# -----------------------------------------------------------------------------
# What src/virt_runner/virtualizer.py shells out to: virsh, virt-install,
# curl, uuidgen, ssh — plus qemu tools and jq for --json consumers.

# Concrete KVM host package per architecture. 'qemu-kvm' is not a portable
# name: it only exists on x86 and, since Ubuntu 26.04, it is a purely virtual
# package offered by BOTH qemu-system-x86 and qemu-system-x86-hwe — apt-get
# then refuses to choose and fails with "no installation candidate". Unknown
# architectures fall back to 'qemu-kvm' plus resolve_package()'s provider
# selection below.
case "$(uname -m)" in
  x86_64)  QEMU_PKG=qemu-system-x86 ;;
  aarch64) QEMU_PKG=qemu-system-arm ;;
  ppc64le) QEMU_PKG=qemu-system-ppc ;;
  s390x)   QEMU_PKG=qemu-system-s390x ;;
  riscv64) QEMU_PKG=qemu-system-riscv64 ;;
  *)       QEMU_PKG=qemu-kvm ;;
esac

declare -a BASE_PACKAGES=(
  # KVM / qemu (the system emulator that runs guests on the KVM accelerator)
  "${QEMU_PKG}"
  qemu-utils
  cpu-checker            # kvm-ok
  # libvirt
  libvirt-daemon-system
  libvirt-clients
  bridge-utils
  # virt-install (drives --import + --cloud-init)
  virtinst
  # UEFI firmware: unused by the BIOS-only Ubuntu path, required for aarch64 /
  # --boot uefi guests (generalize-vm-creation feature spec).
  ovmf
  # Called directly by the pipeline
  curl
  ca-certificates
  uuid-runtime           # uuidgen
  openssh-client         # ssh reachability check
  jq                     # `virt-runner list --json | jq`
)

declare -a PACKAGES=("${BASE_PACKAGES[@]}")
if [[ "${WITH_VIRT_MANAGER}" == true ]]; then
  PACKAGES+=(virt-manager)
fi

# -----------------------------------------------------------------------------
# Privilege helpers
# -----------------------------------------------------------------------------
if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 || error "sudo not found and not running as root"
  SUDO="sudo"
fi

run_as_root() {
  if [[ -n "${SUDO}" ]]; then ${SUDO} "$@"; else "$@"; fi
}

# Every mutating command goes through apply_as_root / vir_apply so that
# --check previews it instead of running it.
apply_as_root() {
  if [[ "${CHECK_ONLY}" == true ]]; then
    info "[check] would run: ${SUDO:+${SUDO} }$*"
    return 0
  fi
  run_as_root "$@"
}

# Mutating virsh call. The privilege decision lives inside vir(), NOT in
# apply_as_root: sudo execs an external binary, so `apply_as_root vir …` looks
# for a program named "vir" and dies with "sudo: vir: command not found".
# --quiet also silences stderr, for calls whose failure is expected (already
# active). Suppression happens here and NOT as a `>/dev/null` on the call site:
# that would swallow the --check preview too.
vir_apply() {
  local quiet=false
  if [[ "${1:-}" == "--quiet" ]]; then quiet=true; shift; fi

  if [[ "${CHECK_ONLY}" == true ]]; then
    local preview="virsh -c ${VIRSH_URI} $*"
    if [[ "${VIRSH_PRIV}" == root ]]; then preview="sudo ${preview}"; fi
    info "[check] would run: ${preview}"
    return 0
  fi

  if [[ "${quiet}" == true ]]; then
    vir "$@" >/dev/null 2>&1
  else
    vir "$@" >/dev/null
  fi
}

# Every virsh call targets the SYSTEM instance explicitly: the default URI of a
# non-root user is qemu:///session, which would silently configure the wrong
# libvirt. Privilege is probed once — while the caller's `libvirt` group
# membership is not active in this shell yet, the privileged path is used.
VIRSH_URI="qemu:///system"
VIRSH_PRIV=""   # "" = unprivileged virsh works, "root" = via sudo

vir() {
  if [[ "${VIRSH_PRIV}" == root ]]; then
    run_as_root virsh -c "${VIRSH_URI}" "$@"
  else
    virsh -c "${VIRSH_URI}" "$@"
  fi
}

# Pick the working privilege level; returns 1 when neither reaches libvirt.
probe_virsh() {
  if virsh -c "${VIRSH_URI}" list >/dev/null 2>&1; then
    VIRSH_PRIV=""
  elif run_as_root virsh -c "${VIRSH_URI}" list >/dev/null 2>&1; then
    VIRSH_PRIV="root"
  else
    return 1
  fi
  return 0
}

# A missing prerequisite is fatal while installing, a finding while auditing.
finding() {
  if [[ "${CHECK_ONLY}" == true ]]; then warn "$*"; return 0; fi
  error "$*"
}

is_package_installed() {
  dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null | grep -q '^installed$'
}

package_version() {
  dpkg-query -W -f='${Version}' "$1" 2>/dev/null || echo "unknown"
}

# Map a requested package name to a concrete installable one. Handles names
# that are virtual in the current release — e.g. qemu-kvm on Ubuntu 26.04,
# where qemu-system-x86 and qemu-system-x86-hwe both Provide it and apt-get
# refuses to pick among several providers. Rules: a name with its own
# candidate maps to itself; a virtual name maps to a provider — an
# already-installed one first (re-runs never churn or double-install), else
# the alphabetically first, which prefers 'base' over 'base-hwe'-style
# variants. Returns 1 when nothing installable exists.
resolve_package() {
  local pkg="$1" candidate provider
  local -a providers=()

  candidate=$(apt-cache policy "${pkg}" 2>/dev/null | awk '/^  Candidate:/{print $2}')
  if [[ -n "${candidate}" && "${candidate}" != "(none)" ]]; then
    printf '%s\n' "${pkg}"
    return 0
  fi

  mapfile -t providers < <(apt-cache showpkg "${pkg}" 2>/dev/null \
    | sed -n '/^Reverse Provides:/,$p' \
    | awk 'NR > 1 && $1 != "" && !seen[$1]++ { print $1 }')
  [[ ${#providers[@]} -gt 0 ]] || return 1

  for provider in "${providers[@]}"; do
    if is_package_installed "${provider}"; then
      printf '%s\n' "${provider}"
      return 0
    fi
  done
  printf '%s\n' "${providers[@]}" | sort | head -n1
}

user_in_group() {
  id -nG "$1" 2>/dev/null | tr ' ' '\n' | grep -qx "$2"
}

# Is *item* the first field of some row of a `virsh …-list` table?
# (awk ignores the leading blank of each row, so $1 is always the name.)
table_has() {
  local table="$1" item="$2"
  # shellcheck disable=SC2086  # $table is a fixed internal word list
  vir ${table} 2>/dev/null | awk -v item="${item}" '$1 == item { found = 1 } END { exit !found }'
}

# -----------------------------------------------------------------------------
# Host check
# -----------------------------------------------------------------------------
step "Checking host"

if [[ "${CHECK_ONLY}" == true ]]; then
  info "audit mode (--check): nothing will be installed or changed"
fi

[[ -f /etc/debian_version ]] \
  || error "Not a Debian/Ubuntu host — install the package list in --help with your distro's package manager"
command -v apt-get >/dev/null 2>&1 || error "apt-get not found"

if [[ -c /dev/kvm ]]; then
  success "/dev/kvm present (hardware virtualization available)"
else
  warn "/dev/kvm missing — enable VT-x/AMD-V (firmware) and load the kvm modules; install continues"
fi

# -----------------------------------------------------------------------------
# Packages
# -----------------------------------------------------------------------------
step "Updating apt package lists"
apply_as_root apt-get update -qq

declare -a TO_INSTALL=()
declare -a TO_UPGRADE=()
for pkg in "${PACKAGES[@]}"; do
  if is_package_installed "${pkg}"; then
    TO_UPGRADE+=("${pkg}")
    info "installed: ${pkg} ($(package_version "${pkg}"))"
    continue
  fi
  # Not installed under its own name: it may be a virtual name whose provider
  # is already present (idempotent re-runs) — resolve before deciding.
  concrete=$(resolve_package "${pkg}") || concrete=""
  if [[ -z "${concrete}" ]]; then
    finding "no installation candidate for '${pkg}' on this release/architecture"
    continue
  fi
  via=""
  [[ "${concrete}" != "${pkg}" ]] && via=" -> ${concrete}"
  if is_package_installed "${concrete}"; then
    TO_UPGRADE+=("${concrete}")
    info "installed: ${pkg}${via} ($(package_version "${concrete}"))"
  else
    TO_INSTALL+=("${concrete}")
    info "missing:   ${pkg}${via}"
  fi
done

if [[ ${#TO_INSTALL[@]} -gt 0 ]]; then
  step "Installing ${#TO_INSTALL[@]} package(s)"
  info "${TO_INSTALL[*]}"
  apply_as_root apt-get install -y -qq "${TO_INSTALL[@]}"
  success "Packages installed"
else
  step "All required packages are present"
fi

if [[ ${#TO_UPGRADE[@]} -gt 0 ]]; then
  step "Upgrading ${#TO_UPGRADE[@]} existing package(s)"
  if [[ "${CHECK_ONLY}" == true ]]; then
    info "[check] would run: ${SUDO:+${SUDO} }apt-get install -y --only-upgrade ${TO_UPGRADE[*]}"
  else
    run_as_root apt-get install -y -qq --only-upgrade "${TO_UPGRADE[@]}" \
      || warn "upgrade failed for some packages (continuing; existing versions are adequate)"
    success "Packages upgraded"
  fi
fi

# -----------------------------------------------------------------------------
# uv — the Python package manager virt-runner itself is launched with. It is
# a per-user tool (~/.local/bin), so it is handled after the apt phase and
# installed as the invoking user, never as root. The prompt is skipped in
# --check mode and whenever no terminal is attached.
# -----------------------------------------------------------------------------

# Official astral installer. When the script itself was started through sudo,
# drop back to the real user so uv lands in their ~/.local/bin, not /root's.
install_uv() {
  if [[ "${EUID}" -eq 0 && -n "${SUDO_USER:-}" ]]; then
    sudo -Hu "${SUDO_USER}" sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
}

step "uv (required for 'uv run virt-runner')"

if command -v uv >/dev/null 2>&1; then
  success "uv present ($(uv --version 2>/dev/null | head -1))"
elif [[ "${CHECK_ONLY}" == true ]]; then
  warn "uv is not installed: curl -LsSf https://astral.sh/uv/install.sh | sh"
elif ask_yes_no "uv is not installed. Install it now from astral.sh (official installer)?"; then
  if install_uv; then
    success "uv installed into ~/.local/bin"
    info "if 'uv' is not on PATH yet: open a new shell, or run  . \"\$HOME/.local/bin/env\""
  else
    warn "uv installer failed — install manually: curl -LsSf https://astral.sh/uv/install.sh | sh"
  fi
else
  info "skipped — install later: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# -----------------------------------------------------------------------------
# libvirt daemon — socket activation is normal, so the only test that matters
# is whether virsh reaches qemu:///system.
# -----------------------------------------------------------------------------
step "Ensuring libvirt answers on qemu:///system"

apply_as_root systemctl enable --now libvirtd >/dev/null 2>&1 \
  || warn "could not enable libvirtd (socket-activated setups still work)"

if probe_virsh; then
  success "virsh reaches ${VIRSH_URI} (${VIRSH_PRIV:-unprivileged})"
else
  apply_as_root systemctl restart libvirtd >/dev/null 2>&1 || true
  if probe_virsh; then
    success "virsh reaches ${VIRSH_URI} after a restart (${VIRSH_PRIV:-unprivileged})"
  else
    finding "virsh cannot reach ${VIRSH_URI} — inspect: systemctl status libvirtd.socket, journalctl -u libvirtd"
  fi
fi

# -----------------------------------------------------------------------------
# NAT network
# -----------------------------------------------------------------------------
step "Ensuring the '${VIRT_NET_NAME}' network"

if table_has "net-list --all" "${VIRT_NET_NAME}"; then
  info "network '${VIRT_NET_NAME}' already defined"
else
  [[ -f /usr/share/libvirt/networks/default.xml ]] \
    || finding "network '${VIRT_NET_NAME}' is missing and /usr/share/libvirt/networks/default.xml is unavailable"
  vir_apply net-define /usr/share/libvirt/networks/default.xml
  [[ "${CHECK_ONLY}" == true ]] || success "defined network '${VIRT_NET_NAME}'"
fi

vir_apply --quiet net-autostart "${VIRT_NET_NAME}" || true
vir_apply --quiet net-start "${VIRT_NET_NAME}" || true

if table_has "net-list" "${VIRT_NET_NAME}"; then
  success "network '${VIRT_NET_NAME}' active"
else
  finding "network '${VIRT_NET_NAME}' is not active — inspect: virsh net-dumpxml ${VIRT_NET_NAME}"
fi

# -----------------------------------------------------------------------------
# Storage pool for tool-created disks
# -----------------------------------------------------------------------------
step "Ensuring the '${VIRT_POOL_NAME}' storage pool"

if table_has "pool-list --all" "${VIRT_POOL_NAME}"; then
  pool_path=$(vir pool-dumpxml "${VIRT_POOL_NAME}" 2>/dev/null \
    | sed -n 's:.*<path>\(.*\)</path>.*:\1:p' | head -1)
  info "pool '${VIRT_POOL_NAME}' already defined at ${pool_path:-unknown}"
else
  apply_as_root install -d -m 0755 "${VIRT_POOL_DIR}"
  # Explicit flags: the positional form of pool-define-as is
  # name type source-host source-path source-dev source-name target, so the
  # bare "dir - - <dir> ''" form put the directory into source-dev and failed
  # on the empty source-name argument.
  vir_apply pool-define-as --name "${VIRT_POOL_NAME}" --type dir --target "${VIRT_POOL_DIR}"
  vir_apply --quiet pool-build "${VIRT_POOL_NAME}" || true
  [[ "${CHECK_ONLY}" == true ]] || success "defined pool '${VIRT_POOL_NAME}' at ${VIRT_POOL_DIR}"
fi

vir_apply --quiet pool-autostart "${VIRT_POOL_NAME}" || true
vir_apply --quiet pool-start "${VIRT_POOL_NAME}" || true

if table_has "pool-list" "${VIRT_POOL_NAME}"; then
  success "pool '${VIRT_POOL_NAME}' active"
else
  finding "pool '${VIRT_POOL_NAME}' is not active — inspect: virsh pool-dumpxml ${VIRT_POOL_NAME}"
fi

# -----------------------------------------------------------------------------
# Passwordless access for the invoking user
# -----------------------------------------------------------------------------
step "Configuring user permissions for '${VIRT_USERNAME:-<unset>}'"

if [[ -z "${VIRT_USERNAME}" ]]; then
  warn "No username resolved — add it manually: usermod -aG libvirt,kvm <user>"
else
  for group in libvirt kvm; do
    getent group "${group}" >/dev/null 2>&1 \
      || apply_as_root groupadd --system "${group}" 2>/dev/null || true
    if user_in_group "${VIRT_USERNAME}" "${group}"; then
      success "user '${VIRT_USERNAME}' already in group '${group}'"
    else
      apply_as_root usermod -aG "${group}" "${VIRT_USERNAME}"
      success "user '${VIRT_USERNAME}' added to group '${group}'"
    fi
  done
  if ! user_in_group "${VIRT_USERNAME}" libvirt || ! user_in_group "${VIRT_USERNAME}" kvm; then
    warn "Group membership applies at the next login — run 'newgrp libvirt' or log out/in"
  fi
fi

# -----------------------------------------------------------------------------
# Verification
# -----------------------------------------------------------------------------
step "Verification"

check_cmd() {
  local label="$1" version_cmd="$2"
  if command -v "${label}" >/dev/null 2>&1; then
    success "${label}: $(${version_cmd} 2>&1 | head -1)"
  else
    warn "${label} not found in PATH"
  fi
}

check_cmd virsh "virsh --version"
check_cmd virt-install "virt-install --version"
check_cmd curl "curl --version"
check_cmd jq "jq --version"

# kvm-ok is the authoritative check: it covers both loaded and built-in kvm,
# so no separate `lsmod` probe (a built-in kvm shows no lsmod row and would
# raise a false alarm).
if command -v kvm-ok >/dev/null 2>&1; then
  if kvm-ok >/dev/null 2>&1; then
    success "kvm-ok: KVM acceleration usable"
  else
    warn "kvm-ok: $(kvm-ok 2>&1 | tail -1) — virt-runner needs real KVM (no TCG emulation)"
  fi
elif [[ ! -c /dev/kvm ]]; then
  warn "neither kvm-ok nor /dev/kvm available — cannot verify hardware virtualization"
fi

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
pool_state=$(vir pool-list 2>/dev/null | awk -v p="${VIRT_POOL_NAME}" '$1 == p {print $2; exit}')
net_state=$(vir net-list 2>/dev/null | awk -v n="${VIRT_NET_NAME}" '$1 == n {print $2; exit}')

if [[ "${CHECK_ONLY}" == true ]]; then
  printf '\n%saudit complete (--check): nothing was changed%s\n' "${BOLD}${BLUE}" "${RESET}"
  printf '  pool:    %s — %s\n' "${VIRT_POOL_NAME}" "${pool_state:-inactive}"
  printf '  network: %s — %s\n' "${VIRT_NET_NAME}" "${net_state:-inactive}"
  printf '  user:    %s — libvirt:%s kvm:%s\n' "${VIRT_USERNAME:-<unset>}" \
    "$( [[ -n "${VIRT_USERNAME}" ]] && user_in_group "${VIRT_USERNAME}" libvirt && echo yes || echo no )" \
    "$( [[ -n "${VIRT_USERNAME}" ]] && user_in_group "${VIRT_USERNAME}" kvm && echo yes || echo no )"
  printf '\nRe-run without --check to install and configure what is missing.\n'
  exit 0
fi

printf '\n%svirt-runner prerequisites ready%s\n\n' "${BOLD}${GREEN}" "${RESET}"
printf '  pool:    %s — %s\n' "${VIRT_POOL_NAME}" "${pool_state:-inactive}"
printf '  network: %s — %s\n' "${VIRT_NET_NAME}" "${net_state:-inactive}"
printf '  user:    %s — libvirt,kvm (activated at next login)\n' "${VIRT_USERNAME}"
echo
printf 'Next steps:\n'
printf '  1. newgrp libvirt            # or log out and back in\n'
printf '  2. uv run virt-runner list\n'
printf '  3. uv run virt-runner create test123\n'
