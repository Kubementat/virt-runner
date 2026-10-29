"""Virtualizer — all libvirt / VM operations via subprocess (virsh, virt-install, etc.).

No libvirt Python bindings. Every method calls the same CLI tools the original
bash scripts use, and every one of those calls goes through one of the five
helpers below so that ``check`` is always explicit and no call site repeats
``capture_output=True, text=True``:

=============  =====================================================
``_run``       :class:`RuntimeError` on non-zero exit
``_stdout``    stdout of the command, empty string when it failed
``_spawn``     the :class:`subprocess.CompletedProcess`, whatever the code
``_quiet``     best-effort cleanup: exit code and output both ignored
``_passthrough``  inherited stdio — the child's own output reaches the user
=============  =====================================================
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from virt_runner import output
from virt_runner.errors import VirtError

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _child_env() -> dict[str, str]:
    """Environment for subprocesses, with our own venv's ``bin`` off ``PATH``.

    Host tools may carry a ``#!/usr/bin/env python3`` shebang
    (``virt-install`` does on Ubuntu 26.04). Started through ``uv run``,
    ``PATH`` leads with the venv's isolated python, from which system
    packages such as ``gi`` are invisible — so spawned tools must resolve
    interpreters from the host PATH, never from ours.
    """
    env = dict(os.environ)
    if sys.prefix != sys.base_prefix:
        venv_bin = os.path.join(sys.prefix, "bin")
        env["PATH"] = os.pathsep.join(
            p for p in env.get("PATH", "").split(os.pathsep) if p != venv_bin
        )
    return env


_CHILD_ENV = _child_env()


def _spawn(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, returning the result whatever its exit code."""
    return subprocess.run(
        cmd, capture_output=True, text=True, check=False, env=_CHILD_ENV, **kwargs
    )


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, raising ``RuntimeError`` on non-zero exit.

    Callers that own a spec'd message and code re-raise it as
    :class:`~virt_runner.errors.VirtError` with ``raise ... from exc``, so the
    traceback keeps the raw ``stderr`` for debugging while the user-facing text
    stays the contractual one.
    """
    result = _spawn(cmd, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def _stdout(cmd: list[str], **kwargs: Any) -> str:
    """Return the stdout of *cmd* — empty string if the command failed.

    Every caller parses a ``virsh`` table or field out of the result, for
    which "no output" and "no match" are the same outcome; the failure itself
    is reported by the caller's own ``VirtError``.
    """
    return _spawn(cmd, **kwargs).stdout


def _quiet(cmd: list[str]) -> None:
    """Run *cmd* for effect only — exit code and output are both ignored."""
    _spawn(cmd)


def _passthrough(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd* with inherited stdio so the user sees the child's own output.

    Used for ``virt-install``, whose failure message tells the user to look
    "above": capturing its output would leave nothing above to look at. Under
    ``--json`` stdout carries only the result document, so the child's stdout
    is steered to stderr there; its stderr is always inherited.
    """
    return subprocess.run(
        cmd,
        stdout=sys.stderr if output.is_json_mode() else None,
        stderr=None,
        check=False,
        env=_CHILD_ENV,
    )


def _ssh_args(identity: str | None) -> list[str]:
    """Host-key policy for throwaway DHCP guests + the injected key only."""
    return [
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        *(["-i", identity, "-o", "IdentitiesOnly=yes"] if identity else []),
    ]


def _non_empty_file(path: Path) -> bool:
    """True when *path* is a regular file with content."""
    return path.is_file() and path.stat().st_size > 0


# ---------------------------------------------------------------------------
# Sizes and the `modify` effective-state vocabulary
# ---------------------------------------------------------------------------

#: ``modify`` ``effective`` values: a change is either live in the hypervisor
#: right now, or written to the persisted config and applied on next boot.
EFFECTIVE_NOW = "now"
EFFECTIVE_ON_BOOT = "on-boot"

#: ``virsh domstate`` value of a powered-on domain.
STATE_RUNNING = "running"

#: Multipliers that turn a sized libvirt value into KiB. Domain XML uses
#: ``KiB``; ``virsh vol-info`` prints IEC units and ``domblkinfo`` SI ones.
_SIZE_UNITS_KIB = {
    "B": 1 / 1024,
    "KB": 1000 / 1024,
    "K": 1,
    "KIB": 1,
    "MB": 1000**2 / 1024,
    "M": 1024,
    "MIB": 1024,
    "GB": 1000**3 / 1024,
    "G": 1024**2,
    "GIB": 1024**2,
    "TB": 1000**4 / 1024,
    "T": 1024**3,
    "TIB": 1024**3,
}

_SIZE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)")


def parse_size_kib(value: str | None, unit: str | None = None) -> int | None:
    """Parse a libvirt size into KiB: ``("4194304", "KiB")`` or ``"8.0 GiB"``.

    A bare number is KiB (what domain XML stores); suffixed values accept both
    IEC (``GiB``) and SI (``GB``) spellings. ``None`` when unparseable, so a
    caller reports "unknown" rather than a wrong size.
    """
    if not value:
        return None
    match = _SIZE_RE.fullmatch(value.strip())
    if match is None:
        return None
    factor = _SIZE_UNITS_KIB.get((match.group(2) or unit or "KiB").upper())
    if factor is None:
        return None
    return int(round(float(match.group(1)) * factor))


def parse_capacity_gib(info: str) -> int | None:
    """The GiB capacity of a ``virsh vol-info`` / ``domblkinfo`` table.

    Only the ``Capacity:`` row matters, and the ``--disk`` contract speaks whole
    GiB, so the value is rounded. A bare number in such a table is *bytes*
    (``domblkinfo`` without ``--human``), unlike the bare KiB of domain XML.
    """
    for line in info.splitlines():
        key, sep, rest = line.partition(":")
        if not sep or key.strip().lower() != "capacity":
            continue
        kib = parse_size_kib(rest, "B")
        return None if kib is None else int(round(kib / 1024 / 1024))
    return None


def parse_vol_info_bytes(info: str) -> dict[str, int]:
    """The sized rows of ``virsh vol-info --bytes``: ``{"capacity": …, "allocation": …}``."""
    rows: dict[str, int] = {}
    for line in info.splitlines():
        key, sep, rest = line.partition(":")
        match = re.fullmatch(r"\s*([0-9]+) bytes\s*", rest)
        if sep and match:
            rows[key.strip().lower()] = int(match.group(1))
    return rows


def volume_name(name: str) -> str:
    """The disk volume of a VM: ``<NAME>_vda.qcow2``.

    Single source for the name contract — a VM is identified by its volume in
    ``vm-list`` (pool-membership test) and torn down by it in ``vm-destroy``.
    """
    return f"{name}_vda.qcow2"


SNAPSHOT_SUFFIX = ".snapshot.qcow2"
SNAPSHOT_META_SUFFIX = ".snapshot.json"
#: Version of the sidecar document; a reader refuses any other value.
SNAPSHOT_SCHEMA = 1


def snapshot_volume(snap: str) -> str:
    """The disk volume of a snapshot: ``<SNAP>.snapshot.qcow2``.

    The suffix cannot collide with a VM's ``_vda.qcow2``/``-cloudinit.iso``, so
    snapshots and VMs keep separate name spaces in the one pool.
    """
    return f"{snap}{SNAPSHOT_SUFFIX}"


def snapshot_meta_volume(snap: str) -> str:
    """The metadata sidecar of a snapshot: ``<SNAP>.snapshot.json`` (raw)."""
    return f"{snap}{SNAPSHOT_META_SUFFIX}"


def snapshot_volume_xml(name: str, capacity_bytes: int) -> str:
    """``vol-create-from`` definition of a snapshot volume.

    ``<compat>1.1</compat>`` is not optional: without it libvirt 10.0 writes a
    qcow2 v2 image (``compat 0.10``). Mode ``0644`` keeps the base readable by
    the hypervisor user and by ``qemu-img info`` without sudo.
    """
    vol = ET.Element("volume")
    ET.SubElement(vol, "name").text = name
    ET.SubElement(vol, "capacity", unit="bytes").text = str(capacity_bytes)
    target = ET.SubElement(vol, "target")
    ET.SubElement(target, "format", type="qcow2")
    ET.SubElement(target, "compat").text = "1.1"
    ET.SubElement(ET.SubElement(target, "permissions"), "mode").text = "0644"
    return ET.tostring(vol, encoding="unicode")


def backing_path_of(vol_xml: str) -> str | None:
    """``<backingStore><path>`` of a ``virsh vol-dumpxml``, ``None`` if flat.

    libvirt keeps reporting the path after the backing file is gone, which is
    exactly what lets the dependents scan find orphaned overlays.
    """
    try:
        root = ET.fromstring(vol_xml)
    except ET.ParseError:
        return None
    return root.findtext("backingStore/path") or None


def lease_file(command: str) -> str:
    """``VM_<CMD>_LEASE_FILE`` > ``VM_CREATE_LEASE_FILE`` > the dnsmasq default."""
    return os.environ.get(f"VM_{command.upper()}_LEASE_FILE") or os.environ.get(
        "VM_CREATE_LEASE_FILE", Virtualizer.DEFAULT_LEASE_FILE
    )


# ---------------------------------------------------------------------------
# Result records (rendered verbatim by the ``--json`` documents, spec §5.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeardownResult:
    """Outcome of :meth:`Virtualizer.destroy_vm` (spec §5.3.2)."""

    was_running: bool
    volume_deleted: bool
    volume_already_absent: bool
    confirmation: str
    warning: str | None


@dataclass(frozen=True)
class SnapshotInfo:
    """A snapshot's validated sidecar (schema v1, see the README).

    The fields are the ones ``create --from-snapshot`` builds a clone from;
    ``meta`` is the whole document, which ``snapshot show`` prints verbatim.
    """

    name: str
    user: str
    sudo_group: str
    os_variant: str
    distro: str | None
    release: str | None
    guest_hostname: str | None
    virtual_gib: int
    defaults: dict[str, int]
    meta: dict[str, Any]


#: Keys a schema-v1 sidecar must carry for a clone to be built from it.
_SNAPSHOT_REQUIRED = ("name", "user", "sudo_group", "os_variant", "virtual_gib")


def parse_snapshot_meta(text: str) -> SnapshotInfo:
    """Parse and validate a sidecar document.

    Raises:
        VirtError: ``snapshot-incomplete`` for bad JSON, a schema other than
            :data:`SNAPSHOT_SCHEMA` or a missing required key.
    """
    try:
        meta = json.loads(text)
    except json.JSONDecodeError:
        meta = None
    problem = None
    if not isinstance(meta, dict):
        problem = "is not a JSON object"
    elif meta.get("schema") != SNAPSHOT_SCHEMA:
        problem = f"has schema {meta.get('schema')!r}, expected {SNAPSHOT_SCHEMA}"
    else:
        missing = [k for k in _SNAPSHOT_REQUIRED if not meta.get(k)]
        if missing:
            problem = f"lacks {', '.join(missing)}"
    if problem:
        raise VirtError(
            f"snapshot metadata {problem} — delete and re-create the snapshot",
            "snapshot-incomplete",
        )
    return SnapshotInfo(
        name=meta["name"],
        user=meta["user"],
        sudo_group=meta["sudo_group"],
        os_variant=meta["os_variant"],
        distro=meta.get("distro"),
        release=meta.get("release"),
        guest_hostname=meta.get("guest_hostname"),
        virtual_gib=int(meta["virtual_gib"]),
        defaults=dict(meta.get("defaults") or {}),
        meta=meta,
    )


@dataclass(frozen=True)
class VmInfo:
    """One pool-resident domain, as reported by ``vm-list`` (spec §11.3).

    Pure fact: the §4.6 text block and the §5.3.3 JSON entry are both rendered
    from these fields by :mod:`virt_runner.cmd_list`, never here.
    """

    name: str
    uuid: str
    state: str
    mac: str
    ip: str
    ip_status: str
    vcpu: int | None = None
    ram_gib: int | None = None
    disk_gib: int | None = None
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        """True when the domain is in the ``running`` state."""
        return self.state == "running"


@dataclass(frozen=True)
class Interface:
    """One vNIC of a domain (``modify`` reads and re-writes these)."""

    type: str
    source: str
    mac: str
    model: str = "virtio"

    def document(self) -> dict[str, str]:
        """JSON form of the NIC (``interface`` change entries report it)."""
        return {"type": self.type, "source": self.source, "mac": self.mac}


@dataclass(frozen=True)
class VmSpec:
    """Sizing, disk and NIC facts of a domain, as ``modify`` needs them.

    ``ram_max_kib``/``vcpu_max`` are the ceilings libvirt refuses to cross
    without a second call (``<memory>`` and ``<vcpu max>"); they are what
    decides whether a live change is possible at all. ``disk_gib`` is read from
    the backing volume, which is the only place libvirt stores a size.
    """

    name: str
    uuid: str
    state: str
    vcpu: int
    vcpu_max: int
    ram_kib: int
    ram_max_kib: int
    disk_target: str
    disk_gib: int | None
    interfaces: tuple[Interface, ...] = ()
    #: What the **persisted config** says, as opposed to what the guest runs
    #: (`vcpu`/`ram_kib` above). Zero = unknown (a shut-off domain, where both
    #: views are one domain). A running domain can be behind its own config, and
    #: comparing a request to the running value alone would call that "already
    #: set" while the next boot gets something else entirely.
    config_vcpu: int = 0
    config_ram_kib: int = 0

    @property
    def running(self) -> bool:
        """True when the domain is powered on."""
        return self.state == STATE_RUNNING

    @property
    def config_ram_gib(self) -> int:
        """The RAM the next boot gets, in GiB (falls back to the live size)."""
        return int(round((self.config_ram_kib or self.ram_kib) / 1024 / 1024))

    @property
    def primary(self) -> Interface | None:
        """The NIC that ``--net``/``--mac`` act on (the first one)."""
        return self.interfaces[0] if self.interfaces else None

    def document(self) -> dict[str, Any]:
        """The ``vm`` section of the ``vm-modify`` document."""
        return {
            "name": self.name,
            "uuid": self.uuid,
            "state": self.state,
            "vcpu": self.vcpu,
            "ram_gib": self.ram_gib,
            "disk_gib": self.disk_gib,
            "interfaces": [i.document() for i in self.interfaces],
        }

    @property
    def ram_gib(self) -> int:
        """RAM in GiB (the unit the CLI speaks); 0 when the XML had none."""
        return int(round(self.ram_kib / 1024 / 1024))


def parse_vm_spec(xml_text: str) -> VmSpec | None:
    """Parse ``virsh dumpxml`` output into a :class:`VmSpec` (``None`` if foreign).

    Pure: ``disk_gib`` is left ``None`` because the size lives in the volume,
    not in the XML — :meth:`Virtualizer.domain_spec` fills it in.
    """
    if not xml_text.strip():
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    vcpu_el = root.find("vcpu")
    if vcpu_el is None:
        return None
    vcpu_max = _int(vcpu_el.text)
    vcpu = _int(vcpu_el.get("current")) or vcpu_max

    memory_kib = parse_size_kib(root.findtext("memory"), "KiB") or 0
    # The *current* size of a running domain is <currentMemory>; <memory> is what
    # the next boot gets. Reading <memory> here is how a config-only RAM change
    # reads back as "already set" while the guest still runs the old size.
    ram_kib = parse_size_kib(root.findtext("currentMemory"), "KiB") or memory_kib
    ram_max_kib = parse_size_kib(root.findtext("maxMemory"), "KiB") or memory_kib

    interfaces: list[Interface] = []
    for el in root.iterfind("devices/interface"):
        mac = el.find("mac")
        source = el.find("source")
        model = el.find("model")
        src_type = el.get("type", "network")
        interfaces.append(
            Interface(
                type=src_type,
                source=(source.get("network") or source.get("bridge") or "")
                if source is not None
                else "",
                mac=mac.get("address", "").lower() if mac is not None else "",
                model=model.get("type", "virtio") if model is not None else "virtio",
            )
        )

    disk_target = ""
    disk = root.find("devices/disk[@device='disk']")
    if disk is not None:
        target = disk.find("target")
        disk_target = target.get("dev", "") if target is not None else ""

    return VmSpec(
        name=root.findtext("name", ""),
        uuid=root.findtext("uuid", ""),
        state="",
        vcpu=vcpu,
        vcpu_max=vcpu_max,
        ram_kib=ram_kib,
        ram_max_kib=ram_max_kib,
        disk_target=disk_target,
        disk_gib=None,
        interfaces=tuple(interfaces),
    )


def _int(value: str | None) -> int:
    """Integer attribute/text value, ``0`` when absent or malformed."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


#: ``vm-list`` ``ip_status`` values (spec §5.3.3).
IP_STATUS_LEASE = "lease"
IP_STATUS_NO_MAC = "no-mac"
IP_STATUS_NOT_RUNNING = "not-running"
IP_STATUS_RUNNING_NO_LEASE = "running-no-lease"


def ip_status_for(state: str, mac: str, ip: str) -> str:
    """Classify why a VM does or does not have an address.

    A domain with no parseable MAC is degenerate, so ``no-mac`` outranks the
    state split; otherwise the running/not-running split comes first and an
    address is only reported as ``lease``.
    """
    if not mac:
        return IP_STATUS_NO_MAC
    if state != "running":
        return IP_STATUS_NOT_RUNNING
    return IP_STATUS_LEASE if ip else IP_STATUS_RUNNING_NO_LEASE


def _pick_no_secboot_loader(paths: list[str]) -> str | None:
    """The UEFI firmware path with Secure Boot disabled, or ``None``.

    Only an explicit ``no-secboot`` marker is trusted: the first/default
    entry may be the Secure Boot build, whose enrolled keys reject unsigned
    guests (see :meth:`Virtualizer.uefi_no_secboot_loader`).
    """
    for path in paths:
        if "no-secboot" in path:
            return path
    return None


# ---------------------------------------------------------------------------
# Cloud-init renderers (pure functions for unit testing)
# ---------------------------------------------------------------------------


def render_user_data(
    user_name: str, group: str, key_line: str, mounts: list[dict[str, str]] = ()
) -> str:
    """Generate cloud-init user-data YAML with JSON-quoted scalars.

    Each mount becomes an idempotent ``bootcmd`` line. bootcmd runs on the
    first boot, before sshd, so the share is mounted by the time SSH answers.
    It also records an fstab entry: the first boot disables cloud-init for
    all later boots (``runcmd`` below), so bootcmd never runs again and
    fstab is what keeps the mount across later reboots.
    """
    bootcmd = "".join(
        "  - "
        + json.dumps(
            f"mkdir -p {m['target']} && "
            f"(grep -Fq ' {m['target']} ' /etc/fstab || "
            f"echo '{m['tag']} {m['target']} virtiofs defaults,nofail 0 0' >> /etc/fstab) && "
            f"(mountpoint -q {m['target']} || mount -t virtiofs {m['tag']} {m['target']})"
        )
        + "\n"
        for m in mounts
    )
    return (
        "#cloud-config\n"
        "manage_etc_hosts: true\n"
        "users:\n"
        f"  - name: {json.dumps(user_name)}\n"
        f"    groups: [{group}]\n"
        '    sudo: ["ALL=(ALL) NOPASSWD:ALL"]\n'
        "    ssh_authorized_keys:\n"
        f"      - {json.dumps(key_line)}\n"
        "package_update: false\n"
        # The NoCloud seed CDROM stays attached after the first boot, so the
        # first boot disables cloud-init for all later boots (the same effect
        # virt-install's `--cloud-init disable=on` used to give).
        'runcmd:\n  - echo "Disabled by virt-runner" > /etc/cloud/cloud-init.disabled\n'
        + (f"bootcmd:\n{bootcmd}" if mounts else "")
    )


def script_command(script: Path) -> str:
    """Remote shell line that runs *script*, piped in on ssh's stdin.

    A ``#!`` line wins; otherwise ``.py`` runs under ``python3`` and anything
    else under ``bash``. ``cloud-init status --wait`` comes first: sshd answers
    before cloud-init finishes, and package managers hold their locks until
    then. Its exit code is ignored (``degraded`` still means "done").
    """
    with script.open("rb") as f:
        shebang = f.read(2) == b"#!"
    runner = "" if shebang else "python3 " if script.suffix == ".py" else "bash "
    return (
        'f=$(mktemp) && cat >"$f" && chmod +x "$f" || exit 1; '
        "cloud-init status --wait >/dev/null 2>&1; "
        f'{runner}"$f" </dev/null; rc=$?; rm -f "$f"; exit $rc'
    )


def render_meta_data(id_val: str, name: str, hostname: str | None = None) -> str:
    """Generate cloud-init meta-data YAML.

    *hostname* defaults to the VM name; a ``--keep-hostname`` clone passes the
    hostname recorded when its snapshot was sealed.
    """
    host = hostname or name
    return (
        f"id: {id_val}\n"
        f"local-hostname: {host}\n"
        "hostnames:\n"
        f"  local: {host}\n"
        f"  host: {host}\n"
    )


# ---------------------------------------------------------------------------
# Virtualizer
# ---------------------------------------------------------------------------


def _poll(check, timeout_s: float, interval: float):
    """Call *check* until it returns truthy or *timeout_s* elapses; return its last value."""
    deadline = time.monotonic() + timeout_s
    while True:
        result = check()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def _live(cmd: list[str]) -> tuple[bool, str]:
    """Attempt one live libvirt change: ``(ok, reason)`` with libvirt's own words.

    libvirt refuses a live change for reasons that all look the same from the
    exit code — no hot-pluggable vCPUs, a ceiling the guest cannot cross, an
    older libvirt — and the distinction decides whether the caller retries, asks
    for a reboot, or reports a limitation. Whitespace is collapsed so a
    multi-line ``error:`` block stays one warning.
    """
    result = _spawn(cmd)
    if result.returncode == 0:
        return True, ""
    err = " ".join((result.stderr or result.stdout or "").split())
    return False, err or f"exit {result.returncode}"


class Virtualizer:
    """Encapsulates every libvirt / VM operation.

    Every public method mirrors a bash-script pipeline step. Failures raise
    :class:`~virt_runner.errors.VirtError` (a ``RuntimeError`` carrying a
    stable code) where the spec defines one, plain ``RuntimeError`` elsewhere;
    the CLI layer translates either into exit code 1.
    """

    POOL = "vm-pool"
    NET = "default"
    #: How long a cooperative ``virsh shutdown`` gets before ``destroy`` does it.
    REBOOT_GRACE_S = 60
    # Namespace of the per-VM metadata recorded at create time (libvirt's
    # `virsh metadata`; the old setmeta/dommetadata are gone in libvirt 10).
    METADATA_URI = "https://virt-runner.local/vm"
    METADATA_KEY = "virt-runner"
    DEFAULT_USER = "ubuntu"
    DEFAULT_SSH_KEY = os.path.expanduser("~/.ssh/virt_runner_key.pub")
    DEFAULT_LEASE_FILE = "/var/lib/libvirt/dnsmasq/virbr0.status"

    #: virtiofs daemon started by libvirt for `create --mount` (Debian/Ubuntu path).
    VIRTIOFSD = "/usr/libexec/virtiofsd"

    #: Poll cadence for the lease and SSH windows (PI-13).
    POLL_INTERVAL_S = 2

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    @staticmethod
    def _table_has(table: str, name: str, state: str | None = None) -> bool:
        """True if a ``virsh ...-list`` table has a row named *name*.

        When *state* is given the second column must match it as well. The
        first two lines (header + dashes) are skipped.
        """
        for line in table.splitlines()[2:]:
            fields = line.split()
            if len(fields) >= 2 and fields[0] == name and state in (None, fields[1]):
                return True
        return False

    @staticmethod
    def private_key_path(pub_path: str) -> str:
        """The private half of a keypair: *pub_path* without ``.pub``."""
        return pub_path.removesuffix(".pub")

    def domain_exists(self, name: str) -> bool:
        """True if *name* is a defined domain (``virsh dominfo`` succeeds)."""
        return _spawn(["virsh", "dominfo", name]).returncode == 0

    def require_libvirt(self) -> None:
        """Raise unless ``virsh list`` succeeds.

        ``libvirtd`` may be socket-activated — never check ``systemctl``.
        """
        if not _spawn(["virsh", "list"]).returncode == 0:
            raise VirtError(
                "preflight failed: libvirt not reachable ('virsh list' failed) "
                "— run 'virt-runner setup --check', then log out and back in",
                "libvirt-unreachable",
            )

    def require_pool_defined(self) -> None:
        """Raise unless the storage pool is defined (any state).

        Used by ``list``, which can report VMs in an inactive pool.
        """
        listing = _stdout(["virsh", "pool-list", "--all"])
        if not self._table_has(listing, self.POOL):
            raise VirtError(
                f"preflight failed: storage pool '{self.POOL}' not found "
                "— run 'virt-runner setup' to define and start it",
                "pool-not-found",
            )

    def preflight_check(self) -> None:
        """Validate all preconditions. Raises ``VirtError`` on failure."""
        # 0. x86_64 / aarch64 only (the profiles carry images for those archs).
        if self.host_arch() not in ("x86_64", "aarch64"):
            raise VirtError(
                f"preflight failed: host arch {self.host_arch()} unsupported "
                "(x86_64 and aarch64 hosts only)",
                "unsupported-arch",
                "preflight",
            )

        # 1. libvirt reachable
        self.require_libvirt()

        # 2. storage pool active
        pools = _stdout(["virsh", "pool-list", "--all"])
        if not self._table_has(pools, self.POOL, "active"):
            raise VirtError(
                f"preflight failed: storage pool '{self.POOL}' is not active",
                "pool-not-active",
                "preflight",
            )

        # 3. network active
        nets = _stdout(["virsh", "net-list", "--all"])
        if not self._table_has(nets, self.NET, "active"):
            raise VirtError(
                f"preflight failed: network '{self.NET}' is not active",
                "network-not-active",
                "preflight",
            )

    def require_domain_absent(self, name: str) -> None:
        """Raise if *name* is already a defined domain."""
        if self.domain_exists(name):
            raise VirtError(
                f"VM '{name}' already defined", "vm-already-defined", "preflight"
            )

    def require_virtiofsd(self) -> None:
        """Raise unless the virtiofs daemon needed by ``--mount`` is installed."""
        if not os.access(self.VIRTIOFSD, os.X_OK):
            raise VirtError(
                f"preflight failed: {self.VIRTIOFSD} not found (needed by --mount) "
                "— install the 'virtiofsd' package or run 'virt-runner setup'",
                "virtiofsd-missing",
                "preflight",
            )

    def ensure_ssh_key(self, path: str) -> None:
        """Ensure *path* is a usable public key, generating the keypair if absent.

        A missing key is created with ``ssh-keygen`` (ed25519, no passphrase),
        so a first run on a fresh host needs no manual key setup. The private
        half is *path* without the ``.pub`` suffix; when only that half
        exists, the public key is re-derived from it with ``ssh-keygen -y``.
        Empty placeholder files at either path are replaced.

        Args:
            path: Public key path (``--ssh-key``, default
                :attr:`DEFAULT_SSH_KEY`).

        Returns:
            ``None``; the caller keeps using *path* throughout.

        Raises:
            VirtError: ``ssh-key-missing`` when no key can be prepared at
                *path*: it does not end in ``.pub`` (the private half would be
                ambiguous), its directory cannot be created, or ``ssh-keygen``
                failed — and no half-written keypair is left behind.
        """
        key = Path(path)
        if _non_empty_file(key):
            return None

        if not path.endswith(".pub"):
            raise VirtError(
                f"ssh key not found: {path} — cannot generate a keypair from "
                "a path that does not end in .pub",
                "ssh-key-missing",
                "preflight",
            )
        private = Path(path[: -len(".pub")])

        key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        if _non_empty_file(private):
            derived = _spawn(["ssh-keygen", "-y", "-f", str(private)])
            if derived.returncode != 0 or not derived.stdout.strip():
                raise VirtError(
                    f"failed to derive the public key from {private}: "
                    f"{(derived.stderr or derived.stdout).strip()}",
                    "ssh-key-missing",
                    "preflight",
                )
            key.write_text(f"{derived.stdout.strip()}\n")
            output.progress(f"Derived public key from {private}: {path}")
            return None

        # Empty placeholders are replaced, not refused.
        for stale in (private, key):
            if stale.is_file() and stale.stat().st_size == 0:
                stale.unlink()
        private_existed = private.exists()
        # stdin closed: ssh-keygen must never stop on an overwrite prompt.
        generated = _spawn(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"virt-runner@{platform.node()}",
                "-f",
                str(private),
            ],
            input="",
        )
        if generated.returncode != 0 or not _non_empty_file(key):
            if not private_existed:
                # No half-written keypair behind (ssh-keygen writes priv first).
                with suppress(OSError):
                    private.unlink()
                with suppress(OSError):
                    key.unlink()
            raise VirtError(
                f"failed to generate ssh key {path}: "
                f"{(generated.stderr or generated.stdout).strip()}",
                "ssh-key-missing",
                "preflight",
            )
        output.progress(f"Generated SSH keypair: {path}")
        return None

    @staticmethod
    def host_arch() -> str:
        """Normalised host architecture (``uname -m`` aliases folded)."""
        machine = platform.machine()
        return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)

    # ------------------------------------------------------------------
    # Cloud-init seed ISO
    # ------------------------------------------------------------------

    @staticmethod
    def cloud_init_iso_volume(name: str) -> str:
        """The NoCloud seed ISO volume of *name* in the storage pool.

        The ISO must outlive the ``create`` process (the guest reads it on
        first boot) and be readable by the hypervisor user — a pool volume
        satisfies both (the daemon owns the pool's permissions; a file under
        the user's home dir is not traversable by ``libvirt-qemu``).
        """
        return f"{name}-cloudinit.iso"

    def delete_cloud_init_iso_quiet(self, name: str) -> None:
        """Best-effort seed volume removal; an absent volume is fine."""
        _quiet(
            [
                "virsh",
                "--quiet",
                "vol-delete",
                self.cloud_init_iso_volume(name),
                self.POOL,
            ]
        )

    def build_cloud_init_iso(self, name: str, user_data: str, meta_data: str) -> str:
        """Build the NoCloud seed ISO (volume label ``cidata``) for *name*.

        Both seed files sit at the ISO root, as cloud-init's NoCloud data
        source expects. The ISO is uploaded to the storage pool as a
        volume; returns the volume name (attach as ``vol=pool/<vol>``).

        Raises:
            VirtError: ``cloud-init-failed`` when ``xorrisofs`` or the
                volume upload fails — no volume or partial file is left
                behind.
        """
        if not shutil.which("xorrisofs"):
            raise VirtError(
                f"failed to build NoCloud seed ISO for '{name}': xorrisofs "
                "not found — install the 'xorriso' package",
                "cloud-init-failed",
                "cloud-init",
            )
        volume = self.cloud_init_iso_volume(name)
        fd, tmp = tempfile.mkstemp(prefix="virt-runner-cidata-", suffix=".iso")
        os.close(fd)
        try:
            # user-data and meta-data share the temp dir — xorrisofs takes the dir.
            src_dir = Path(user_data).parent
            result = _spawn(
                [
                    "xorrisofs",
                    "-o",
                    tmp,
                    "-J",
                    "-input-charset",
                    "utf8",
                    "-rational-rock",
                    "-V",
                    "cidata",
                    str(src_dir),
                ]
            )
            if result.returncode != 0 or not _non_empty_file(Path(tmp)):
                raise VirtError(
                    f"failed to build NoCloud seed ISO for '{name}': "
                    f"{(result.stderr or result.stdout).strip()}",
                    "cloud-init-failed",
                    "cloud-init",
                )
            try:
                self._upload_raw_volume(volume, tmp)
            except RuntimeError as exc:
                self.delete_cloud_init_iso_quiet(name)
                raise VirtError(
                    f"failed to upload NoCloud seed ISO for '{name}' to pool "
                    f"'{self.POOL}'",
                    "cloud-init-failed",
                    "cloud-init",
                ) from exc
        finally:
            Path(tmp).unlink(missing_ok=True)
        output.progress(f"NoCloud seed ISO ready: {self.POOL}/{volume}")
        return volume

    def _upload_raw_volume(self, volume: str, path: str) -> None:
        """Create a raw pool volume the size of *path* and upload *path* into it.

        The way to put a host file into the root-owned pool without sudo.

        Raises:
            RuntimeError: from either ``virsh`` call; the caller cleans up.
        """
        _run(
            [
                "virsh",
                "vol-create-as",
                self.POOL,
                volume,
                "--capacity",
                str(Path(path).stat().st_size),
                "--format",
                "raw",
            ]
        )
        _run(["virsh", "vol-upload", "--pool", self.POOL, volume, path])

    # ------------------------------------------------------------------
    # Cloud-init
    # ------------------------------------------------------------------

    def generate_cloud_init_files(
        self,
        name: str,
        user_name: str,
        sudo_group: str,
        ssh_key: str,
        mounts: list[dict[str, str]] = (),
        hostname: str | None = None,
    ) -> tuple[str, str]:
        """Generate cloud-init user-data and meta-data files.

        *hostname* overrides the guest hostname (default: *name*).

        Returns:
            Tuple of ``(user_data_path, meta_data_path)``.
            The temp dir is cleaned up on process exit via ``atexit``.
        """
        tmp_dir = tempfile.mkdtemp(prefix="virt-runner-cloud-init-")

        # Fresh lowercase UUID (meta-data `id:`).
        new_id = str(uuid.uuid4())

        try:
            ssh_key_line = Path(ssh_key).read_text().strip()
        except OSError as exc:
            raise VirtError(
                f"ssh key not found: {ssh_key}", "ssh-key-missing", "cloud-init"
            ) from exc

        # meta-data and user-data via pure renderers.
        meta_data = render_meta_data(new_id, name, hostname)
        user_data = render_user_data(user_name, sudo_group, ssh_key_line, mounts)

        (Path(tmp_dir) / "meta-data").write_text(meta_data)
        (Path(tmp_dir) / "user-data").write_text(user_data)

        # Test-only dump hook.
        dump_dir = os.environ.get("VM_CREATE_CLOUD_INIT_DUMP")
        if dump_dir:
            dump_path = Path(dump_dir)
            dump_path.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(tmp_dir) / "meta-data", dump_path / "meta-data")
            shutil.copyfile(Path(tmp_dir) / "user-data", dump_path / "user-data")

        # The files must outlive this function (virt-install reads them), so
        # cleanup is registered for interpreter exit — the bash EXIT trap.
        atexit.register(shutil.rmtree, tmp_dir, True)

        output.progress(
            f"cloud-init files generated: {tmp_dir} (meta-data, user-data; id={new_id})"
        )
        return (
            str(Path(tmp_dir) / "user-data"),
            str(Path(tmp_dir) / "meta-data"),
        )

    # ------------------------------------------------------------------
    # Storage volumes
    # ------------------------------------------------------------------

    def create_volume(self, name: str, disk_gib: int) -> str:
        """Create a qcow2 volume in the pool. Returns the volume name.

        Raises:
            VirtError: ``volume-create-failed`` (§4.4 wording; the raw
                ``virsh`` stderr is kept on the chained cause, as the bash tool
                discarded it too).
        """
        volume = volume_name(name)
        try:
            _run(
                [
                    "virsh",
                    "vol-create-as",
                    self.POOL,
                    volume,
                    "--capacity",
                    f"{disk_gib}G",
                    "--format",
                    "qcow2",
                ]
            )
        except RuntimeError as exc:
            raise VirtError(
                f"failed to create pool volume '{volume}' in pool '{self.POOL}'",
                "volume-create-failed",
                "create",
            ) from exc
        return volume

    def upload_image(self, volume: str, image_path: str) -> None:
        """Upload an image into a pool volume.

        Raises:
            VirtError: ``volume-import-failed``.
        """
        try:
            _run(["virsh", "vol-upload", "--pool", self.POOL, volume, image_path])
        except RuntimeError as exc:
            raise VirtError(
                f"failed to import image into pool volume '{volume}'",
                "volume-import-failed",
                "create",
            ) from exc

    def resize_volume(self, volume: str, disk_gib: int) -> None:
        """Restore the contracted capacity after upload.

        Mandatory (PI-1): ``vol-upload`` silently resizes the destination to
        the source's virtual size and libvirt 10.0's ``virsh`` has no
        ``--resize=no`` — see ``docs/lessons-learned/001``.

        Raises:
            VirtError: ``volume-resize-failed``.
        """
        try:
            _run(["virsh", "vol-resize", volume, f"{disk_gib}G", self.POOL])
        except RuntimeError as exc:
            raise VirtError(
                f"failed to resize pool volume '{volume}' to {disk_gib}G",
                "volume-resize-failed",
                "create",
            ) from exc

    def delete_volume_quiet(self, volume: str) -> None:
        """Best-effort volume delete used by failure cleanup (PI-12)."""
        _quiet(["virsh", "--quiet", "vol-delete", volume, self.POOL])

    def provision_volume(self, name: str, disk_gib: int, image_path: str) -> str:
        """Create the pool volume, upload *image_path* into it, restore capacity.

        A failed upload/resize leaves no volume behind (PI-12).

        Raises:
            VirtError: ``volume-create-failed``, ``volume-import-failed`` or
                ``volume-resize-failed`` (the code each step already raises).
        """
        volume = self.create_volume(name, disk_gib)
        try:
            self.upload_image(volume, image_path)
            self.resize_volume(volume, disk_gib)
        except RuntimeError:
            self.delete_volume_quiet(volume)
            raise
        return volume

    # ------------------------------------------------------------------
    # VM lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def generate_mac() -> str:
        """Random MAC: the QEMU OUI plus 3 random bytes (PI-4).

        Colon-separated per byte pair — the bare-hex tail form
        ``52:54:00:24d2bd`` is rejected by libvirt (ticket-04 deviation).
        """
        tail = os.urandom(3).hex()
        return f"52:54:00:{tail[0:2]}:{tail[2:4]}:{tail[4:6]}"

    def uefi_no_secboot_loader(self) -> str | None:
        """UEFI firmware without Secure Boot for aarch64 domains, or ``None``.

        aarch64 domains always boot UEFI, and libvirt's default firmware on
        some hosts has Secure Boot enabled (enrolled Microsoft keys). That
        rejects guests with unsigned bootloaders — e.g. Fedora's aarch64
        cloud image — with a firmware ``Security Violation`` dialog: the VM
        never reaches the kernel and ``create`` times out in wait-ip with no
        console output (the dialog predates any console attach). Booting the
        same image from the ``no-secboot`` firmware works. ``None`` (no
        override) when the host offers no such firmware — e.g. hosts whose
        only UEFI build is already non-secure.
        """
        listing = _stdout(["virsh", "domcapabilities", "--arch", self.host_arch()])
        return _pick_no_secboot_loader(re.findall(r"<value>(/[^<]+)</value>", listing))

    def create_vm(
        self,
        name: str,
        ram_mib: int,
        vcpus: int,
        volume: str,
        mac: str,
        cloud_init_iso: str,
        os_variant: str,
        no_boot: bool,
        mounts: list[dict[str, str]] = (),
    ) -> str:
        """Create a VM via virt-install.

        *cloud_init_iso* is the NoCloud seed ISO built by
        :meth:`build_cloud_init_iso`, attached as a plain CDROM — NOT
        virt-install's own ``--cloud-init`` flag, whose ISO virt-install's
        exit cleanup deletes while the guest is still booting (fatal on
        aarch64; see ``docs/lessons-learned/008``).

        If ``no_boot`` is True, stops the domain immediately (D5).

        Returns:
            The domain state after creation (``"running"`` or ``"shut off"``).

        Raises:
            VirtError: ``vm-create-failed``, after removing the domain and the
                volume — no VM or volume is left behind (PI-12).
        """
        cmd = [
            "virt-install",
            "--name",
            name,
            "--ram",
            str(ram_mib),
            "--vcpu",
            str(vcpus),
            "--disk",
            f"vol={self.POOL}/{volume},bus=virtio",
            "--network",
            f"network={self.NET},model=virtio,mac={mac}",
            "--os-variant",
            os_variant,
            "--import",
            # --noautoconsole keeps virt-install from hanging on the console
            # while still creating the serial console device that the
            # `virsh console` recommendation points at.
            "--noautoconsole",
            # NoCloud seed as a plain --disk CDROM from the pool volume
            # (see the docstring): virt-runner owns the volume and its
            # lifetime, virt-install does not delete it on exit. cloud-init
            # stays enabled only for the first boot via the runcmd in
            # render_user_data (the old `--cloud-init disable=on`
            # equivalent; a bare --cloud-init would also generate a root
            # password, PI-9).
            "--disk",
            f"vol={self.POOL}/{cloud_init_iso},device=cdrom",
            # PI-11: autostart always.
            "--autostart",
        ]

        # aarch64 domains always boot UEFI; opt out of Secure Boot so every
        # profile's image can boot (Fedora's aarch64 image ships an unsigned
        # GRUB — see uefi_no_secboot_loader). None on hosts without a
        # no-secboot firmware: virt-install keeps its default there.
        if self.host_arch() == "aarch64":
            loader = self.uefi_no_secboot_loader()
            if loader:
                cmd += ["--boot", f"uefi=on,loader={loader}"]

        if mounts:
            # virtiofs requires guest RAM shared with the virtiofsd process.
            cmd += ["--memorybacking", "source.type=memfd,access.mode=shared"]
        for m in mounts:
            cmd += [
                "--filesystem",
                f"source.dir={m['source']},target.dir={m['tag']},driver.type=virtiofs,binary.path={self.VIRTIOFSD}",
            ]

        if _passthrough(cmd).returncode != 0:
            # Clean up orphan resources (--nvram: aarch64 UEFI domains,
            # see destroy_vm; a failed undefine here would leave the domain).
            _quiet(["virsh", "--quiet", "destroy", name])
            _quiet(["virsh", "--quiet", "undefine", "--nvram", name])
            self.delete_volume_quiet(volume)
            self.delete_cloud_init_iso_quiet(name)
            raise VirtError(
                f"virt-install failed for '{name}' (see errors above); "
                f"no VM left behind",
                "vm-create-failed",
                "create",
            )

        output.progress(f"VM '{name}' created (assigned MAC: {mac})")

        # --no-boot (D5): virt-install auto-starts the domain, so guard the
        # destroy with a domstate check; either way the reported state is the
        # stopped one.
        if no_boot:
            if self.get_domain_state(name) == "running":
                _quiet(["virsh", "--quiet", "destroy", name])
            return "shut off"

        return "running"

    def domain_xml_text(self, name: str) -> tuple[ET.Element | None, str]:
        """Both halves of ``virsh dumpxml``: the parsed tree *and* its text.

        Callers that need the tree and a parser that takes text — like
        :func:`parse_vm_spec` — get one subprocess instead of two.
        """
        text = _stdout(["virsh", "dumpxml", name])
        try:
            return (ET.fromstring(text) if text.strip() else None), text
        except ET.ParseError:
            return None, text

    def domain_xml(self, name: str) -> ET.Element | None:
        """Parsed ``virsh dumpxml`` of *name*, or ``None`` when unavailable."""
        return self.domain_xml_text(name)[0]

    def get_domain_uuid(self, name: str) -> str:
        """Return the domain UUID (PI-6: last field of the ``UUID:`` line)."""
        root = self.domain_xml(name)
        return root.findtext("uuid", "") if root is not None else ""

    def get_domain_state(self, name: str) -> str:
        """Return the domain state string (``running``, ``shut off``, …)."""
        return _stdout(["virsh", "domstate", name]).strip()

    def set_domain_metadata(
        self, name: str, distro: str, user: str, identity: str, **extra: str | None
    ) -> None:
        """Record the guest distro/user/identity in the domain's metadata.

        Read back by ``ssh``/``list`` via :meth:`domain_meta`, so both log in
        as the user that was actually created on the guest. *extra* adds the
        optional ``release``/``image_url``/``mounts`` (JSON)/``snapshot``
        attributes that ``snapshot create`` copies into a snapshot; ``None``
        values are left out, so metadata written before them stays valid.
        """
        attrs = {"distro": distro, "user": user, "identity": identity}
        attrs.update({k: v for k, v in extra.items() if v is not None})
        xml = ET.tostring(ET.Element("virt-runner", attrs), encoding="unicode")
        _run(
            [
                "virsh",
                "metadata",
                name,
                self.METADATA_URI,
                "--key",
                self.METADATA_KEY,
                "--set",
                xml,
            ]
        )

    def domain_meta(self, name: str) -> dict[str, str]:
        """Parse the ``virt-runner`` metadata block and return its attributes."""
        root = self.domain_xml(name)
        if root is None:
            return {}
        meta = root.find(f"metadata/{{{self.METADATA_URI}}}{self.METADATA_KEY}")
        return dict(meta.attrib) if meta is not None else {}

    # ------------------------------------------------------------------
    # IP discovery + SSH verification
    # ------------------------------------------------------------------

    def find_ip_for_mac(self, lease_file: str, mac: str) -> str | None:
        """Look up IP for *mac* in the dnsmasq lease file.

        Supports JSON format (primary, PI-7) and legacy column format
        (fallback). Returns ``None`` if no match, a missing file or a
        malformed one.
        """
        mac_lower = mac.lower()

        try:
            content = Path(lease_file).read_text()
        except OSError:
            return None

        if not content.strip():
            return None

        # JSON format (starts with '[').
        if content.strip().startswith("["):
            return self._parse_json_lease(content, mac_lower)

        # Legacy column format: <ip> <mac> [<hostname>].
        return self._parse_legacy_lease(content, mac_lower)

    @staticmethod
    def _parse_json_lease(content: str, mac_lower: str) -> str | None:
        """Parse JSON-format dnsmasq lease file.

        A MAC can have several entries: dnsmasq records a renewal as a new line
        and keeps the previous one until it expires, and the two can carry
        different client-ids (the plain MAC, plus the DUID cloud-init sends).

        **The freshest lease wins** — picking the first means reporting an
        address the guest gave up, which surfaces as an SSH timeout, not as a
        wrong answer.
        """
        try:
            leases = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(leases, list):
            return None
        best: str | None = None
        best_expiry = -1
        for lease in leases:
            if not isinstance(lease, dict):
                continue
            if str(lease.get("mac-address", "")).lower() != mac_lower:
                continue
            ip = lease.get("ip-address")
            if not ip:
                continue
            try:
                expiry = int(lease.get("expiry-time") or 0)
            except (TypeError, ValueError):
                expiry = 0
            if expiry >= best_expiry:
                best, best_expiry = str(ip), expiry
        return best

    @staticmethod
    def _parse_legacy_lease(content: str, mac_lower: str) -> str | None:
        """Parse legacy space-separated dnsmasq lease file.

        The MAC is matched case-insensitively at any field >= 2 and the field
        immediately before it (the IP) is returned; a line whose *first* field
        is the MAC is degenerate and yields no match.
        """
        for line in content.splitlines():
            fields = line.split()
            for i in range(1, len(fields)):
                if fields[i].lower() == mac_lower:
                    return fields[i - 1]
        return None

    def wait_for_ip(
        self,
        name: str,
        mac: str,
        lease_file: str,
        timeout_s: int = 120,
    ) -> str:
        """Poll the lease file for the IP. Raises on timeout.

        ``domifaddr``/``arp`` are best-effort fallbacks used ONLY when the
        lease file is absent, unreadable or empty — they are unreliable as a
        primary source and must not extend the polling window (PI-8).
        """
        interval = self.POLL_INTERVAL_S
        lease = Path(lease_file)
        lease_usable = lease.is_file() and os.access(lease, os.R_OK)

        def _lease_ip() -> str | None:
            ip = self.find_ip_for_mac(lease_file, mac)
            # Belt-and-braces: a lease is accepted only while the domain
            # actually runs.
            return ip if ip and self.get_domain_state(name) == "running" else None

        if lease_usable:
            ip = _poll(_lease_ip, timeout_s, interval)
            if ip:
                return ip

        if not lease_usable or lease.stat().st_size == 0:
            # Fallbacks, in order: `virsh domifaddr` (retried up to 60 s), then
            # a single `arp -n` sweep matched by MAC.
            ip = self._fallback_ip_domifaddr(name) or self._fallback_ip_arp(mac)
            if ip:
                return ip

        raise VirtError(
            f"no DHCP lease after {timeout_s}s — check: virsh console {name}",
            "lease-timeout",
            "wait-ip",
        )

    def _fallback_ip_domifaddr(self, name: str) -> str | None:
        """Best-effort IP via ``virsh domifaddr`` (retried up to 60 s)."""

        def _domifaddr_ip() -> str | None:
            listing = _stdout(["virsh", "domifaddr", name])
            for line in listing.splitlines():
                fields = line.split()
                if len(fields) >= 3 and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", fields[2]):
                    return fields[2]
            return None

        return _poll(_domifaddr_ip, 30 * self.POLL_INTERVAL_S, self.POLL_INTERVAL_S)

    @staticmethod
    def _fallback_ip_arp(mac: str) -> str | None:
        """Best-effort IP via ``arp -n`` (matched by MAC, case-insensitive)."""
        mac_lower = mac.lower()
        table = _stdout(["arp", "-n"])
        for line in table.splitlines():
            fields = line.split()
            for i, value in enumerate(fields):
                if i > 0 and value.lower() == mac_lower:
                    return fields[i - 1]
        return None

    def verify_ssh_reachable(
        self,
        name: str,
        user: str,
        ip: str,
        identity: str | None = None,
        timeout_s: int = 90,
    ) -> None:
        """Verify SSH connectivity (PI-13: success requires a round-trip).

        *identity* is the private key injected into the guest — without it the
        probe falls back to the caller's default identities and cannot
        authenticate against a freshly created VM. Host keys are checked
        against ``/dev/null``: DHCP hands these IPs out to throwaway guests,
        so a reused address legitimately presents a new host key and must not
        fail verification (nor pollute the user's ``known_hosts``).

        Raises:
            VirtError: ``ssh-timeout`` when no attempt succeeds.
        """
        probe = [
            "ssh",
            "-o",
            "ConnectTimeout=2",
            "-o",
            "BatchMode=yes",
            *_ssh_args(identity),
            f"{user}@{ip}",
            "exit",
        ]
        if not _poll(
            lambda: _spawn(probe).returncode == 0, timeout_s, self.POLL_INTERVAL_S
        ):
            raise VirtError(
                f"SSH to {user}@{ip} not reachable yet — check: virsh console {name}",
                "ssh-timeout",
                "ssh-verify",
            )

    def run_script(
        self, user: str, ip: str, script: Path, identity: str | None = None
    ) -> int:
        """Run a host-side *script* in the guest as *user*; return its exit code.

        The file travels on ssh's stdin (no scp round-trip). Its output streams
        to stderr in both modes so ``--json`` stdout stays one document.
        """
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "LogLevel=ERROR",
            *_ssh_args(identity),
            f"{user}@{ip}",
            script_command(script),
        ]
        with script.open("rb") as f:
            return subprocess.run(
                cmd, stdin=f, stdout=sys.stderr, check=False, env=_CHILD_ENV
            ).returncode

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    def vm_ip(self, name: str, lease_file: str) -> str | None:
        """Current address of a running domain, or ``None``.

        Non-blocking counterpart of :meth:`wait_for_ip` — used by ``ssh``
        against VMs that booted long ago: the DHCP lease first, then a
        single ``domifaddr`` sweep (which may report the address with a
        ``/prefix`` suffix, so it is matched anywhere in the line and
        stripped).
        """
        root = self.domain_xml(name)
        mac = self._mac_of(root) if root is not None else ""
        ip = self.find_ip_for_mac(lease_file, mac) if mac else None
        if ip:
            return ip
        listing = _stdout(["virsh", "domifaddr", name])
        for line in listing.splitlines():
            match = re.search(r"\b\d+\.\d+\.\d+\.\d+\b", line)
            if match:
                return match.group(0)
        return None

    def ssh_shell(self, user: str, ip: str, identity: str | None = None) -> int:
        """Open an interactive ssh session; return ssh's exit code.

        stdio is inherited so the user gets a real shell; under ``--json``
        — like :func:`_passthrough` — stdout is steered to stderr so the
        result document stays the only thing on stdout. Host keys are
        handled like :meth:`verify_ssh_reachable`: DHCP recycles these
        addresses between throwaway guests.
        """
        cmd = ["ssh", *_ssh_args(identity), f"{user}@{ip}"]
        return _passthrough(cmd).returncode

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def get_pool_path(self) -> str:
        """Return the pool's directory path from its XML definition."""
        xml = _stdout(["virsh", "pool-dumpxml", self.POOL])
        match = re.search(r"<path>([^<]*)</path>", xml)
        if match:
            return match.group(1)
        raise VirtError(
            f"failed to determine path of pool '{self.POOL}'",
            "pool-path-unknown",
        )

    def _has_disk_in_pool(self, root: ET.Element, pool_path: str) -> bool:
        """True if any of the domain's disk sources lives under *pool_path*.

        Compared on path components: a bare prefix match would also accept a
        sibling directory such as ``<pool_path>-evil``.
        """
        return any(
            (s.get("file") or "").startswith(f"{pool_path}/")
            for s in root.iterfind("devices/disk/source")
        )

    @staticmethod
    def _mac_of(root: ET.Element) -> str:
        """MAC of the domain's first interface, or ``""`` when absent."""
        mac = root.find("devices/interface/mac")
        return mac.get("address", "") if mac is not None else ""

    def list_vms(self, lease_file: str) -> list[VmInfo]:
        """Return one :class:`VmInfo` per domain whose disk lives in the pool.

        Domains without a pool-resident disk are not ours and are skipped.
        Rendering (text block or JSON entry) happens in
        :mod:`virt_runner.cmd_list`.
        """
        pool_path = self.get_pool_path().rstrip("/")
        listing = _stdout(["virsh", "list", "--all", "--name"])
        domain_names = [n.strip() for n in listing.strip().splitlines() if n.strip()]

        results = []
        for name in domain_names:
            root, xml = self.domain_xml_text(name)
            if root is None or not self._has_disk_in_pool(root, pool_path):
                continue

            state = self.get_domain_state(name)
            mac = self._mac_of(root)
            ip = (self.find_ip_for_mac(lease_file, mac) or "") if mac else ""
            meta_el = root.find(f"metadata/{{{self.METADATA_URI}}}{self.METADATA_KEY}")
            # Sizing comes from the live XML because that is what the guest runs,
            # not what its next boot gets; a config-only change therefore reads
            # back as the old value until someone reboots. The disk is the
            # exception: its size is only in the volume, never in the XML.
            spec = parse_vm_spec(xml)
            volume = self._volume_of(root, pool_path)

            results.append(
                VmInfo(
                    name=name,
                    uuid=root.findtext("uuid", ""),
                    state=state,
                    mac=mac,
                    ip=ip,
                    ip_status=ip_status_for(state, mac, ip),
                    vcpu=spec.vcpu if spec else None,
                    ram_gib=spec.ram_kib // (1024 * 1024) if spec else None,
                    disk_gib=self.disk_capacity_gib(name, "", volume),
                    meta=dict(meta_el.attrib) if meta_el is not None else {},
                )
            )

        return results

    # ------------------------------------------------------------------
    # Destroy
    # ------------------------------------------------------------------

    def destroy_vm(self, name: str) -> TeardownResult:
        """Destroy and undefine a domain, delete its volume.

        Returns:
            :class:`TeardownResult` — whether ``virsh destroy`` was needed, how
            the volume delete went, and the confirmation/warning lines.

        Raises:
            RuntimeError: from the destroy/undefine calls (fatal: a teardown
                must not report success after the domain survived).
            VirtError: ``volume-delete-failed``.
        """
        # 1. Destroy if running, then undefine. Ordering is mandatory:
        #    vol-delete fails on a still-attached disk.
        was_running = self.get_domain_state(name) == "running"
        try:
            if was_running:
                _run(["virsh", "--quiet", "destroy", name])
            # --nvram: aarch64 UEFI domains carry an NVRAM variable store
            # that libvirt refuses to leave behind on a plain undefine;
            # no-op for NVRAM-less (x86_64 SeaBIOS) domains.
            _run(["virsh", "--quiet", "undefine", "--nvram", name])
        except RuntimeError as exc:
            raise VirtError(
                f"failed to destroy/undefine VM '{name}'", "vm-destroy-failed"
            ) from exc

        # 2. Delete volume. An already-absent volume is a warning, not a
        #    failure (D7): the teardown's contract is "nothing left behind".
        volume = volume_name(name)
        volume_absent = self._delete_volume(volume)

        # 3. Best-effort: the NoCloud seed ISO volume. It stays attached for
        #    the VM's lifetime — the domain XML pins its file path, so a
        #    deleted seed would break `virsh start` (and --autostart) — and
        #    is removed here (absent file is fine).
        self.delete_cloud_init_iso_quiet(name)

        if volume_absent:
            warning = (
                f"Warning: volume '{volume}' in pool '{self.POOL}' was "
                f"already absent; nothing to delete."
            )
            confirm = f"VM '{name}' destroyed and disk removed (volume already absent)."
        else:
            warning = None
            confirm = f"VM '{name}' destroyed and disk removed."

        return TeardownResult(
            was_running=was_running,
            volume_deleted=not volume_absent,
            volume_already_absent=volume_absent,
            confirmation=confirm,
            warning=warning,
        )

    def _delete_volume(self, volume: str) -> bool:
        """``vol-delete`` *volume*; return True when it was already absent.

        Raises:
            VirtError: ``volume-delete-failed`` for any other failure.
        """
        result = _spawn(["virsh", "vol-delete", volume, self.POOL])
        err = result.stderr.lower()
        absent = result.returncode != 0 and any(
            kw in err for kw in ("not found", "no such volume", "does not exist")
        )
        if result.returncode != 0 and not absent:
            raise VirtError(
                f"failed to delete volume '{volume}' in pool '{self.POOL}': "
                f"{result.stderr.strip()}",
                "volume-delete-failed",
            )
        return absent

    # ------------------------------------------------------------------
    # Snapshots and clones
    # ------------------------------------------------------------------

    def seal_guest(self, user: str, ip: str, identity: str | None = None) -> str:
        """Run :data:`_SEAL_SCRIPT` in the guest as root; return its hostname.

        Raises:
            VirtError: ``snapshot-seal-failed`` (stage ``seal``) on a non-zero
                exit; the guest keeps running, possibly half-sealed.
        """
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=5",
            *_ssh_args(identity),
            f"{user}@{ip}",
            "sudo",
            "-n",
            "bash",
            "-s",
        ]
        result = _spawn(cmd, input=_SEAL_SCRIPT)
        if result.returncode != 0:
            detail = " ".join((result.stderr or result.stdout).split())[-300:]
            raise VirtError(
                f"seal script failed in {user}@{ip} (exit {result.returncode}): "
                f"{detail} — the VM is kept running and may be half-sealed; "
                "re-run snapshot create, or destroy the VM",
                "snapshot-seal-failed",
                "seal",
            )
        lines = result.stdout.strip().splitlines()
        return lines[0].strip() if lines else ""

    def start_domain(self, name: str) -> None:
        """``virsh start`` *name*.

        Raises:
            RuntimeError: when libvirt refuses to start it.
        """
        _run(["virsh", "start", name])

    def shutdown_and_wait(self, name: str, timeout_s: int) -> None:
        """Power the guest off cooperatively and wait until it is off.

        Unlike :meth:`reboot` there is no ``destroy`` fallback: a power cut
        right after the seal would put an unclean filesystem into the snapshot.

        Raises:
            VirtError: ``vm-shutdown-timeout`` (stage ``shutdown``).
        """
        if self.get_domain_state(name) != STATE_RUNNING:
            return
        _spawn(["virsh", "shutdown", name])
        if not _poll(
            lambda: self.get_domain_state(name) != STATE_RUNNING,
            timeout_s,
            self.POLL_INTERVAL_S,
        ):
            raise VirtError(
                f"VM '{name}' did not power off within {timeout_s}s; no forced "
                "power-off was done — stop its workload or raise --shutdown-timeout",
                "vm-shutdown-timeout",
                "shutdown",
            )

    def volume_bytes(self, volume: str) -> dict[str, int]:
        """``capacity``/``allocation`` of a pool volume in bytes (``{}`` if absent)."""
        return parse_vol_info_bytes(
            _stdout(["virsh", "vol-info", "--bytes", "--pool", self.POOL, volume])
        )

    def copy_to_snapshot(self, src_volume: str, snap: str) -> None:
        """Copy *src_volume* into a new flat ``<SNAP>.snapshot.qcow2``.

        libvirtd runs ``qemu-img convert``, which flattens a backing chain, so
        a snapshot of a clone is standalone too.

        Raises:
            VirtError: ``snapshot-copy-failed``; a half-written volume is
                removed (but never a snapshot that already existed).
        """
        volume = snapshot_volume(snap)
        capacity = self.volume_bytes(src_volume).get("capacity")
        if not capacity:
            raise VirtError(
                f"cannot read the size of source volume '{src_volume}'",
                "snapshot-copy-failed",
                "copy",
            )
        with tempfile.NamedTemporaryFile(
            "w", prefix="virt-runner-snap-", suffix=".xml"
        ) as xml:
            xml.write(snapshot_volume_xml(volume, capacity))
            xml.flush()
            result = _spawn(
                [
                    "virsh",
                    "vol-create-from",
                    self.POOL,
                    xml.name,
                    src_volume,
                    "--inputpool",
                    self.POOL,
                ]
            )
        if result.returncode != 0:
            if "already in use" not in result.stderr:
                self.delete_volume_quiet(volume)
            raise VirtError(
                f"failed to copy '{src_volume}' to '{volume}': {result.stderr.strip()}",
                "snapshot-copy-failed",
                "copy",
            )

    def write_snapshot_meta(self, snap: str, meta: dict[str, Any]) -> None:
        """Upload *meta* as the raw sidecar volume ``<SNAP>.snapshot.json``.

        Raises:
            VirtError: ``snapshot-metadata-failed``; no sidecar is left.
        """
        volume = snapshot_meta_volume(snap)
        with tempfile.NamedTemporaryFile(
            "w", prefix="virt-runner-snap-", suffix=".json"
        ) as f:
            json.dump(meta, f, indent=2)
            f.flush()
            try:
                self._upload_raw_volume(volume, f.name)
            except RuntimeError as exc:
                self.delete_volume_quiet(volume)
                raise VirtError(
                    f"failed to write snapshot metadata '{volume}'",
                    "snapshot-metadata-failed",
                    "metadata",
                ) from exc

    def pool_volumes(self) -> list[str]:
        """Names of every volume in the pool (``vol-list`` has no ``--name``)."""
        listing = _stdout(["virsh", "vol-list", self.POOL])
        return [line.split()[0] for line in listing.splitlines()[2:] if line.split()]

    def snapshot_names(self) -> list[str]:
        """Every snapshot with a disk *or* a sidecar volume, so neither half hides."""
        names = set()
        for vol in self.pool_volumes():
            for suffix in (SNAPSHOT_SUFFIX, SNAPSHOT_META_SUFFIX):
                if vol.endswith(suffix) and len(vol) > len(suffix):
                    names.add(vol[: -len(suffix)])
        return sorted(names)

    def read_snapshot_meta(self, snap: str) -> SnapshotInfo:
        """Download and validate a snapshot's sidecar.

        Raises:
            VirtError: ``snapshot-not-found`` when neither volume exists,
                ``snapshot-incomplete`` when only one does or the sidecar is
                invalid.
        """
        volumes = set(self.pool_volumes())
        has_disk = snapshot_volume(snap) in volumes
        has_meta = snapshot_meta_volume(snap) in volumes
        if not (has_disk or has_meta):
            raise VirtError(f"snapshot '{snap}' not found", "snapshot-not-found")
        if not (has_disk and has_meta):
            missing = "metadata" if has_disk else "disk volume"
            raise VirtError(
                f"snapshot '{snap}' has no {missing} — delete and re-create it",
                "snapshot-incomplete",
            )
        with tempfile.TemporaryDirectory(prefix="virt-runner-snap-") as tmp:
            path = Path(tmp) / "meta.json"
            result = _spawn(
                [
                    "virsh",
                    "vol-download",
                    "--pool",
                    self.POOL,
                    snapshot_meta_volume(snap),
                    str(path),
                ]
            )
            text = path.read_text() if result.returncode == 0 else ""
        return parse_snapshot_meta(text)

    def backing_users(self) -> dict[str, list[str]]:
        """Backing path -> the pool volumes layered on it (one dumpxml each).

        libvirt itself happily deletes a backing file under a running clone, so
        this scan is the only thing standing between ``snapshot delete`` and
        corrupted clone disks. Dangling paths are reported as well.
        """
        users: dict[str, list[str]] = {}
        for vol in self.pool_volumes():
            path = backing_path_of(_stdout(["virsh", "vol-dumpxml", vol, self.POOL]))
            if path:
                users.setdefault(path, []).append(vol)
        return users

    def snapshot_path(self, snap: str) -> str:
        """Absolute path of the snapshot disk, as overlays record it."""
        return f"{self.get_pool_path().rstrip('/')}/{snapshot_volume(snap)}"

    def snapshot_dependents(
        self, snap: str, users: dict[str, list[str]] | None = None
    ) -> list[str]:
        """Clones (VM names, else bare volume names) backed by *snap*."""
        users = self.backing_users() if users is None else users
        return sorted(
            vol.removesuffix("_vda.qcow2")
            for vol in users.get(self.snapshot_path(snap), [])
        )

    def delete_snapshot(self, snap: str) -> list[dict[str, Any]]:
        """Delete both snapshot volumes; the caller has checked for dependents.

        Returns:
            One ``{name, deleted, already_absent}`` entry per volume.

        Raises:
            VirtError: ``volume-delete-failed``.
        """
        report = []
        for volume in (snapshot_volume(snap), snapshot_meta_volume(snap)):
            absent = self._delete_volume(volume)
            report.append(
                {"name": volume, "deleted": not absent, "already_absent": absent}
            )
        return report

    def create_overlay_volume(self, name: str, disk_gib: int, snap: str) -> str:
        """Thin qcow2 overlay ``<NAME>_vda.qcow2`` backed by the snapshot.

        Raises:
            VirtError: ``overlay-create-failed`` (stage ``create``).
        """
        volume = volume_name(name)
        try:
            _run(
                [
                    "virsh",
                    "vol-create-as",
                    self.POOL,
                    volume,
                    "--capacity",
                    f"{disk_gib}G",
                    "--format",
                    "qcow2",
                    "--backing-vol",
                    snapshot_volume(snap),
                    "--backing-vol-format",
                    "qcow2",
                ]
            )
        except RuntimeError as exc:
            raise VirtError(
                f"failed to create overlay '{volume}' on snapshot '{snap}'",
                "overlay-create-failed",
                "create",
            ) from exc
        return volume

    # ------------------------------------------------------------------
    # Modify (vCPU / RAM / disk / networking)
    # ------------------------------------------------------------------

    def owns_domain(self, name: str) -> bool:
        """True when *name* is a domain virt-runner created (or owns the disk of).

        The membership test is identical to ``vm-list``'s: a disk under the pool
        path, or the per-VM metadata. The first half is what does the work — a
        hand-defined domain that points at a pool volume is *not* recognized as
        foreign, because it is a guest whose storage this tool owns. Use
        ``virsh`` for anything outside the pool.
        """
        if self.domain_meta(name):
            return True
        root = self.domain_xml(name)
        if root is None:
            return False
        return self._has_disk_in_pool(root, self.get_pool_path().rstrip("/"))

    @staticmethod
    def _volume_of(root: ET.Element, pool: str) -> str | None:
        """Pool volume referenced by *root*'s disks, or ``None`` when outside."""
        for source in root.iterfind("devices/disk/source"):
            path = source.get("file") or ""
            if path.startswith(f"{pool}/") and path != pool:
                return path[len(pool) + 1 :]
        return None

    def disk_volume(self, name: str) -> str | None:
        """Pool volume backing *name*'s first disk, or ``None`` when outside."""
        root = self.domain_xml(name)
        if root is None:
            return None
        return self._volume_of(root, self.get_pool_path().rstrip("/"))

    def disk_capacity_gib(
        self, name: str, target: str, volume: str | None
    ) -> int | None:
        """Current disk size in GiB: the pool volume first, ``domblkinfo`` next.

        ``None`` when neither reports a size — callers must then refuse to
        resize rather than guess at a ceiling.
        """
        if volume:
            gib = parse_capacity_gib(
                _stdout(["virsh", "vol-info", "--pool", self.POOL, volume])
            )
            if gib is not None:
                return gib
        if not target:
            return None
        return parse_capacity_gib(_stdout(["virsh", "domblkinfo", name, target]))

    def domain_spec(self, name: str) -> VmSpec | None:
        """Full :class:`VmSpec` of *name* (XML + backing volume), ``None`` if absent."""
        spec = parse_vm_spec(_stdout(["virsh", "dumpxml", name]))
        if spec is None:
            return None
        volume = self.disk_volume(name)
        state = self.get_domain_state(name)
        spec = replace(
            spec,
            name=name,
            state=state,
            disk_gib=self.disk_capacity_gib(name, spec.disk_target, volume),
        )
        if state == STATE_RUNNING:
            # A running domain is two documents: what it runs and what its next
            # boot gets. Both are needed to tell "already set" apart from
            # "already in the config, waiting for a reboot".
            cfg = parse_vm_spec(_stdout(["virsh", "dumpxml", "--inactive", name]))
            if cfg is not None:
                # `ram_max_kib` of the *config* document is `<memory>` for every
                # virt-runner guest, i.e. the size that gets reserved on the next
                # boot — the number that tells us whether the config really says
                # what we think it does.
                spec = replace(
                    spec,
                    config_vcpu=cfg.vcpu,
                    config_ram_kib=cfg.ram_max_kib,
                )
        return spec

    def network_defined(self, network: str) -> bool:
        """True when libvirt knows a virtual network of that name."""
        return _spawn(["virsh", "net-dumpxml", network]).returncode == 0

    def interface_target_exists(self, iface: Interface) -> bool:
        """True when *iface*'s source is usable on this host right now.

        Checked **before** a NIC is given up: libvirt only notices a missing
        network/bridge at attach time, i.e. after the detach already happened.
        """
        if iface.type == "network":
            return self.network_defined(iface.source)
        return _spawn(["ip", "link", "show", "dev", iface.source]).returncode == 0

    @staticmethod
    def _flags(live: bool) -> list[str]:
        """``--live --config`` when a change must land on the running domain too."""
        return ["--live", "--config"] if live else ["--config"]

    def set_vcpu(
        self, name: str, vcpu: int, *, live: bool, ceiling: int
    ) -> tuple[bool, str]:
        """Persist the vCPU count; hot-plug first when *live*.

        Returns:
            ``(live_ok, reason)``: whether the running domain changed, plus
            libvirt's reason when it refused. ``reason`` is empty when the change
            worked or no live change was asked for.

        Raises:
            RuntimeError: the persisted change failed (a plain ``RuntimeError``
                keeps the generic ``runtime-error`` code — libvirt's own message
                is the useful part here).
        """
        live_ok, why = (
            _live(["virsh", "setvcpus", name, str(vcpu), "--live"])
            if live
            else (False, "")
        )
        self._write_vcpu_config(name, vcpu, ceiling)
        return live_ok, why

    def _write_vcpu_config(self, name: str, vcpu: int, ceiling: int) -> None:
        """``virsh setvcpus --config``, raising ``<vcpu max>`` first when growing.

        libvirt rejects a config vCPU count above the configured maximum, so a
        grow past the ceiling is a two-step change: ``--maximum`` lifts the
        persisted ceiling (config-only — a live domain's maximum is fixed until
        it reboots), then the count is set.
        """
        try:
            _run(["virsh", "setvcpus", name, str(vcpu), "--config"])
        except RuntimeError:
            if vcpu <= ceiling:
                raise
            _run(["virsh", "setvcpus", name, str(vcpu), "--maximum", "--config"])
            _run(["virsh", "setvcpus", name, str(vcpu), "--config"])

    def set_ram_gib(
        self,
        name: str,
        ram_gib: int,
        *,
        live: bool,
        ceiling: int,
        config_ceiling: int = 0,
    ) -> bool:
        """Persist the RAM size in GiB; balloon first when *live*.

        Args:
            ceiling: The live domain's ceiling — what the running guest can be
                ballooned to without a reboot.
            config_ceiling: ``<memory>`` of the persisted config, which is what the
                config write is compared against. A guest that is behind its own
                config has two different ceilings, and the wrong one silently
                skips the ceiling write.

        Returns:
            ``(live_ok, reason)``: whether the running domain changed, plus
            libvirt's reason when the balloon refused. ``reason`` is empty when it
            worked or no live change was asked for.

        Raises:
            RuntimeError: the persisted change failed, or the guest is bigger than
                the host — libvirt writes such a config without complaint and the
                domain then fails to boot.
        """
        kib = str(ram_gib * 1024 * 1024)
        cap = self.host_memory_kib()
        if cap is not None and int(kib) > cap:
            raise RuntimeError(
                f"refusing to give '{name}' {ram_gib} GiB of RAM: the libvirt host "
                f"has {cap // 1024 // 1024} GiB total, and libvirt would write the "
                "config anyway — the domain would not start (no change was made)"
            )
        live_ok, why = (
            _live(["virsh", "setmem", name, kib, "--live"]) if live else (False, "")
        )
        self._write_ram_config(name, kib, config_ceiling or ceiling)
        return live_ok, why

    def host_memory_kib(self) -> int | None:
        """Total RAM of the libvirt host, or ``None`` when it cannot be read.

        ``virsh setmem --config`` validates a size against nothing: a guest bigger
        than the machine is accepted into ``<memory>``/``<currentMemory>`` and only
        fails on the next start. The ceiling virt-runner needs a real number for.
        """
        stdout = _spawn(["virsh", "nodeinfo"]).stdout
        for line in stdout.splitlines():
            if line.lower().startswith("memory size"):
                digits = "".join(c for c in line.split(":", 1)[-1] if c.isdigit())
                return int(digits) if digits else None
        return None

    def _write_ram_config(self, name: str, kib: str, ceiling: int) -> None:
        """Write both halves of the domain's RAM: ``<memory>`` and ``<currentMemory>``.

        Growing past ``<memory>`` fails with "cannot set memory higher than max
        memory", and the ceiling call is a *separate* command: ``virsh setmem``
        has no ``--maximum`` (that spelling belongs to ``setvcpus``), so the
        ceiling goes up through ``virsh setmaxmem --config`` — which writes
        ``<memory>`` when the domain has no ``<maxMemory>`` element, as every
        virt-runner guest has. ``setmaxmem`` alone is not enough: it leaves
        ``<currentMemory>`` at the old value and the guest boots with the old
        amount of RAM, so the boot size has to follow in a second call.

        The reverse half is the one that bites. A *lower* target makes
        ``setmem --config`` succeed on the first try — it moves
        ``<currentMemory>`` and leaves ``<memory>`` at the old, higher size, which
        is what qemu allocates for the next boot. libvirt does not care, so does
        the tool, and the guest simply does not start (see
        ``docs/lessons-learned/013-virsh-option-spelling.md``). So when the target
        is below the ceiling, lower the ceiling too.
        """
        try:
            _run(["virsh", "setmem", name, kib, "--config"])
        except RuntimeError:
            if int(kib) <= ceiling:
                raise
            _run(["virsh", "setmaxmem", name, kib, "--config"])
            _run(["virsh", "setmem", name, kib, "--config"])
            return
        if int(kib) < ceiling:
            _run(["virsh", "setmaxmem", name, kib, "--config"])

    def grow_disk(self, name: str, target: str, volume: str | None, gib: int) -> None:
        """Grow the domain's first disk to *gib* (never shrinks — checked upstream).

        ``virsh blockresize`` is the live primitive, but libvirt only resizes an
        *active* domain, so a shut-off one grows by its pool volume — which
        virt-runner owns for every VM it creates.

        Raises:
            RuntimeError: live resize failed.
            VirtError: ``volume-resize-failed`` when the volume grow failed.
        """
        result = _spawn(["virsh", "blockresize", name, target, f"{gib}G"])
        if result.returncode == 0:
            return
        if self.get_domain_state(name) == STATE_RUNNING or volume is None:
            raise RuntimeError(
                f"failed to grow disk '{target}' of '{name}' to {gib}G: "
                f"{result.stderr.strip() or 'virsh blockresize failed'}"
            )
        self.grow_volume(volume, gib)

    def grow_volume(self, volume: str, disk_gib: int) -> None:
        """Grow a pool volume. Shrinking is libvirt's own refusal, not ours.

        ``virsh vol-resize`` errors on a smaller capacity unless ``--shrink`` is
        passed explicitly (there is no ``--grow`` option), and this call never
        passes it — plus :func:`virt_runner.cmd_modify.plan_changes` refuses a
        smaller ``--disk`` before it gets here.

        Raises:
            VirtError: ``volume-resize-failed``.
        """
        try:
            _run(
                [
                    "virsh",
                    "vol-resize",
                    volume,
                    "--pool",
                    self.POOL,
                    "--capacity",
                    f"{disk_gib}G",
                ]
            )
        except RuntimeError as exc:
            raise VirtError(
                f"failed to grow pool volume '{volume}' to {disk_gib}G",
                "volume-resize-failed",
            ) from exc

    def attach_interface(self, name: str, iface: Interface, *, live: bool) -> None:
        """Attach a NIC; the MAC is kept when the caller carries one.

        Raises:
            VirtError: ``net-change-failed`` with libvirt's reason.
        """
        cmd = [
            "virsh",
            "attach-interface",
            name,
            iface.type,
            iface.source,
            "--model",
            iface.model or "virtio",
        ]
        if iface.mac:
            cmd += ["--mac", iface.mac]
        self._change_interface(cmd + self._flags(live), name, f"attach {iface}")

    def detach_interface(self, name: str, iface: Interface, *, live: bool) -> None:
        """Detach the NIC with *iface*'s MAC.

        Raises:
            VirtError: ``net-change-failed`` with libvirt's reason.
        """
        cmd = [
            "virsh",
            "detach-interface",
            name,
            iface.type,
            "--mac",
            iface.mac,
            *self._flags(live),
        ]
        self._change_interface(cmd, name, f"detach {iface}")

    @staticmethod
    def _change_interface(cmd: list[str], name: str, what: str) -> None:
        """Run one interface change and turn any failure into ``net-change-failed``."""
        result = _spawn(cmd)
        if result.returncode != 0:
            raise VirtError(
                f"failed to {what} NIC of '{name}': "
                f"{result.stderr.strip() or 'virsh failed'}",
                "net-change-failed",
            )

    def reboot(self, name: str) -> None:
        """Power-cycle the domain, letting the guest sync first.

        ``virsh shutdown`` is the cooperative half: the guest sees the ACPI event
        and powers itself off. ``destroy`` is only the fallback for a guest that
        ignored it — a hard power cut is what corrupts a filesystem that was just
        resized, so it gets no first turn. A domain that is already off is simply
        started.

        Raises:
            RuntimeError: from destroy/start — a half-rebooted VM must not
                report success.
        """
        if self.get_domain_state(name) != STATE_RUNNING:
            _run(["virsh", "start", name])
            return
        _spawn(["virsh", "shutdown", name])
        off = _poll(
            lambda: self.get_domain_state(name) != STATE_RUNNING,
            self.REBOOT_GRACE_S,
            self.POLL_INTERVAL_S,
        )
        if not off:
            _quiet(["virsh", "destroy", name])
        _run(["virsh", "start", name])

    def grow_guest_filesystem(
        self, user: str, ip: str, target: str, identity: str | None = None
    ) -> tuple[int, str]:
        """Grow the guest's root partition/filesystem into the resized disk.

        Best-effort by contract: the return value is ``(exit_code, output)`` and
        the caller turns anything but 0 into a warning. The script stays distro
        neutral — util-linux (`findmnt`/`growpart`) plus the filesystem's own
        grow tool — and prints a ``virt-runner:`` line explaining any skip.

        Raises:
            RuntimeError: ssh itself could not run (the guest is unreachable).
        """
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=5",
            *_ssh_args(identity),
            f"{user}@{ip}",
            "sudo",
            "-n",
            "bash",
            "-s",
            "--",
            target,
        ]
        result = _spawn(cmd, input=_FS_GROW_SCRIPT)
        return result.returncode, (result.stdout + result.stderr).strip()


#: Grows the root partition of the resized disk and then the filesystem on it.
#: Any non-zero exit is a warning for `modify`, and every skip prints the exact
#: manual commands, because the layout (`growpart` missing, LVM, exotic fs) is
#: the guest's business and virt-runner does not guess its way through it.
_FS_GROW_SCRIPT = r"""set -u
disk=$1
dev=/dev/$disk
root=$(findmnt -no SOURCE /) || { echo "virt-runner: findmnt failed"; exit 1; }

if [ ! -b "$dev" ]; then
  echo "virt-runner: $dev not found in the guest — filesystem left alone"
  exit 0
fi

case $root in
  "$dev")   part="" ;;
  "$dev"p[0-9]*) part=${root#"$dev"p} ;;
  "$dev"[0-9]*)  part=${root#"$dev"} ;;
  /dev/mapper/*|/dev/dm-*)
    echo "virt-runner: root is on LVM ($root) — grow manually: sudo pvresize <disk of $root> && sudo lvextend -l +100%FREE --resizefs $root"
    exit 0 ;;
  *)
    echo "virt-runner: root device $root is not on $dev — filesystem left alone"
    exit 0 ;;
esac

if [ -n "$part" ]; then
  if ! command -v growpart >/dev/null 2>&1; then
    echo "virt-runner: growpart is not installed in the guest — grow manually: sudo growpart $dev $part, then the resize below"
    exit 0
  fi
  growpart "$dev" "$part"
  rc=$?
  # growpart's own exit status (see its man page): 0 = grown, 1 = there was no
  # space left to grow into, i.e. already as large as it can be, 2 = error. So 1
  # is a *success* for us — treating it as failure is how a perfectly healthy
  # guest gets told its filesystem was "not grown".
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
    echo "virt-runner: growpart $dev $part failed (exit $rc)"
    exit 1
  fi
fi

fstype=$(blkid -o value -s TYPE "$root" | head -n 1)
case $fstype in
  ext2|ext3|ext4) resize2fs "$root" ;;
  xfs) xfs_growfs / ;;
  btrfs) btrfs filesystem resize max / ;;
  *) echo "virt-runner: unsupported guest filesystem '$fstype' — grow it manually"; exit 0 ;;
esac
echo "virt-runner: guest filesystem grown"
"""


#: Turns a provisioned guest back into "next boot is a first boot" before its
#: disk is copied into a snapshot: virt-runner's own ``cloud-init.disabled``
#: marker would make every clone ignore its new seed, and a shared machine-id
#: makes networkd send the same DHCP client-id from every clone. SSH host keys
#: stay: cloud-init rotates them on the new instance, and deleting them here
#: would leave a clone whose cloud-init failed unreachable. ``uname -n``, not
#: ``hostname`` (Arch ships no such binary). The first stdout line is the guest
#: hostname, recorded for ``--keep-hostname``.
_SEAL_SCRIPT = r"""set -eu
cloud-init status --wait >/dev/null 2>&1 || true
uname -n
rm -f /etc/cloud/cloud-init.disabled
cloud-init clean --logs
if [ -d /run/systemd/system ]; then
  printf 'uninitialized\n' > /etc/machine-id
else rm -f /etc/machine-id; fi
[ -L /var/lib/dbus/machine-id ] || rm -f /var/lib/dbus/machine-id
rm -f /var/lib/systemd/random-seed
rm -f /var/lib/NetworkManager/*.lease /var/lib/dhcp/*.leases 2>/dev/null || true
sed -i -E '/^mount[0-9]+[[:space:]]+[^[:space:]]+[[:space:]]+virtiofs[[:space:]]/d' /etc/fstab
sync
"""
