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
from dataclasses import dataclass, field
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


def volume_name(name: str) -> str:
    """The disk volume of a VM: ``<NAME>_vda.qcow2``.

    Single source for the name contract — a VM is identified by its volume in
    ``vm-list`` (pool-membership test) and torn down by it in ``vm-destroy``.
    """
    return f"{name}_vda.qcow2"


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
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        """True when the domain is in the ``running`` state."""
        return self.state == "running"


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


def render_meta_data(id_val: str, name: str) -> str:
    """Generate cloud-init meta-data YAML."""
    return (
        f"id: {id_val}\n"
        f"local-hostname: {name}\n"
        "hostnames:\n"
        f"  local: {name}\n"
        f"  host: {name}\n"
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


class Virtualizer:
    """Encapsulates every libvirt / VM operation.

    Every public method mirrors a bash-script pipeline step. Failures raise
    :class:`~virt_runner.errors.VirtError` (a ``RuntimeError`` carrying a
    stable code) where the spec defines one, plain ``RuntimeError`` elsewhere;
    the CLI layer translates either into exit code 1.
    """

    POOL = "vm-pool"
    NET = "default"
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
                "preflight failed: libvirt not reachable ('virsh list' failed)",
                "libvirt-unreachable",
            )

    def require_pool_defined(self) -> None:
        """Raise unless the storage pool is defined (any state).

        Used by ``list``, which can report VMs in an inactive pool.
        """
        listing = _stdout(["virsh", "pool-list", "--all"])
        if not self._table_has(listing, self.POOL):
            raise VirtError(
                f"preflight failed: storage pool '{self.POOL}' not found",
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
                "— install the 'virtiofsd' package",
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
                _run(
                    [
                        "virsh",
                        "vol-create-as",
                        self.POOL,
                        volume,
                        "--capacity",
                        str(Path(tmp).stat().st_size),
                        "--format",
                        "raw",
                    ]
                )
                _run(["virsh", "vol-upload", "--pool", self.POOL, volume, tmp])
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
    ) -> tuple[str, str]:
        """Generate cloud-init user-data and meta-data files.

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
        meta_data = render_meta_data(new_id, name)
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

    def domain_xml(self, name: str) -> ET.Element | None:
        """Parsed ``virsh dumpxml`` of *name*, or ``None`` when unavailable."""
        text = _stdout(["virsh", "dumpxml", name])
        try:
            return ET.fromstring(text) if text.strip() else None
        except ET.ParseError:
            return None

    def get_domain_uuid(self, name: str) -> str:
        """Return the domain UUID (PI-6: last field of the ``UUID:`` line)."""
        root = self.domain_xml(name)
        return root.findtext("uuid", "") if root is not None else ""

    def get_domain_state(self, name: str) -> str:
        """Return the domain state string (``running``, ``shut off``, …)."""
        return _stdout(["virsh", "domstate", name]).strip()

    def set_domain_metadata(
        self, name: str, distro: str, user: str, identity: str
    ) -> None:
        """Record the guest distro/user/identity in the domain's metadata.

        Read back by ``ssh``/``list`` via :meth:`domain_meta`, so both log in
        as the user that was actually created on the guest.
        """
        xml = ET.tostring(
            ET.Element("virt-runner", distro=distro, user=user, identity=identity),
            encoding="unicode",
        )
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
        """Parse JSON-format dnsmasq lease file."""
        try:
            leases = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(leases, list):
            return None
        for lease in leases:
            if not isinstance(lease, dict):
                continue
            if str(lease.get("mac-address", "")).lower() != mac_lower:
                continue
            ip = lease.get("ip-address")
            if ip:
                return ip
        return None

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
            root = self.domain_xml(name)
            if root is None or not self._has_disk_in_pool(root, pool_path):
                continue

            state = self.get_domain_state(name)
            mac = self._mac_of(root)
            ip = (self.find_ip_for_mac(lease_file, mac) or "") if mac else ""
            meta_el = root.find(f"metadata/{{{self.METADATA_URI}}}{self.METADATA_KEY}")

            results.append(
                VmInfo(
                    name=name,
                    uuid=root.findtext("uuid", ""),
                    state=state,
                    mac=mac,
                    ip=ip,
                    ip_status=ip_status_for(state, mac, ip),
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
        result = _spawn(["virsh", "vol-delete", volume, self.POOL])
        err = result.stderr.lower()
        volume_absent = result.returncode != 0 and any(
            kw in err for kw in ("not found", "no such volume", "does not exist")
        )
        if result.returncode != 0 and not volume_absent:
            raise VirtError(
                f"failed to delete volume '{volume}' in pool '{self.POOL}': "
                f"{result.stderr.strip()}",
                "volume-delete-failed",
            )

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
