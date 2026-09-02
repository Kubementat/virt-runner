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
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from virt_runner import output
from virt_runner.errors import VirtError

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

#: Sent on every sums fetch; identifies the tool without leaking anything else.
USER_AGENT = f"virt-runner/{output.version()}"


def _spawn(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, returning the result whatever its exit code."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kwargs)


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
        text=True,
        check=False,
    )


def _non_empty_file(path: Path) -> bool:
    """True when *path* is a regular file with content."""
    return path.is_file() and path.stat().st_size > 0


def volume_name(name: str) -> str:
    """The disk volume of a VM: ``<NAME>_vda.qcow2``.

    Single source for the name contract — a VM is identified by its volume in
    ``vm-list`` (pool-membership test) and torn down by it in ``vm-destroy``.
    """
    return f"{name}_vda.qcow2"


# ---------------------------------------------------------------------------
# Result records (rendered verbatim by the ``--json`` documents, spec §5.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageFetch:
    """Outcome of :meth:`Virtualizer.fetch_and_verify_image`.

    ``verification``/``verified`` say how the returned file earned trust. A
    cache hit reports the verification that populated the cache — the cache is
    only written by a verified download — while ``"skipped"`` means no sums
    were derivable (warning printed, ``verified`` false).
    """

    path: str
    cache_hit: bool
    downloaded: bool
    verification: str
    verified: bool


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


# ---------------------------------------------------------------------------
# Virtualizer
# ---------------------------------------------------------------------------


class Virtualizer:
    """Encapsulates every libvirt / VM operation.

    Every public method mirrors a bash-script pipeline step. Failures raise
    :class:`~virt_runner.errors.VirtError` (a ``RuntimeError`` carrying a
    stable code) where the spec defines one, plain ``RuntimeError`` elsewhere;
    the CLI layer translates either into exit code 1.
    """

    POOL = "vm-pool"
    NET = "default"
    DEFAULT_IMAGE_BASE = "https://cloud-images.ubuntu.com"
    DEFAULT_RELEASE = "resolute"
    DEFAULT_USER = "ubuntu"
    DEFAULT_SSH_KEY = os.path.expanduser("~/.ssh/virt_runner_key.pub")
    DEFAULT_LEASE_FILE = "/var/lib/libvirt/dnsmasq/virbr0.status"

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

    def domain_exists(self, name: str) -> bool:
        """True if *name* is a defined domain (``virsh dominfo`` succeeds)."""
        return _spawn(["virsh", "dominfo", name]).returncode == 0

    def libvirt_reachable(self) -> None:
        """Raise unless ``virsh list`` succeeds.

        ``libvirtd`` may be socket-activated — never check ``systemctl``.
        """
        if not _spawn(["virsh", "list"]).returncode == 0:
            raise VirtError(
                "preflight failed: libvirt not reachable ('virsh list' failed)",
                "libvirt-unreachable",
            )

    def pool_exists(self) -> None:
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
        # 1. libvirt reachable
        self.libvirt_reachable()

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

    def domain_not_defined(self, name: str) -> None:
        """Raise if *name* is already a defined domain."""
        if self.domain_exists(name):
            raise VirtError(
                f"VM '{name}' already defined", "vm-already-defined", "preflight"
            )

    def ensure_ssh_key(self, path: str) -> str:
        """Return *path* as a usable public key, generating the keypair if absent.

        A missing key is created with ``ssh-keygen`` (ed25519, no passphrase),
        so a first run on a fresh host needs no manual key setup. The private
        half is *path* without the ``.pub`` suffix; when only that half
        exists, the public key is re-derived from it with ``ssh-keygen -y``.
        Empty placeholder files at either path are replaced.

        Args:
            path: Public key path (``--ssh-key``, default
                :attr:`DEFAULT_SSH_KEY`).

        Returns:
            *path* unchanged, so the caller keeps using one value throughout.

        Raises:
            VirtError: ``ssh-key-missing`` when no key can be prepared at
                *path*: it does not end in ``.pub`` (the private half would be
                ambiguous), its directory cannot be created, or ``ssh-keygen``
                failed — and no half-written keypair is left behind.
        """
        key = Path(path)
        if _non_empty_file(key):
            return path

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
            return path

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
        return path

    @staticmethod
    def host_arch() -> str:
        """Normalised host architecture (``uname -m`` aliases folded)."""
        machine = platform.machine()
        return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)

    # ------------------------------------------------------------------
    # Image cache
    # ------------------------------------------------------------------

    def fetch_and_verify_image(
        self,
        release: str,
        release_given: bool,
        image_url: str,
        image_given: bool,
        keep_going: bool,
    ) -> ImageFetch:
        """Download + verify cloud image.

        Args:
            release: release codename (e.g. ``resolute``).
            release_given: whether ``--release`` was explicitly passed.
            image_url: the effective image URL (the release pattern unless
                ``--image`` alone was given, D1).
            image_given: whether ``--image`` was explicitly passed.
            keep_going: whether to continue on error if a cache exists.

        Returns:
            :class:`ImageFetch` for the cached, verified image file.

        Raises:
            VirtError: ``image-download-failed``, ``image-verification-failed``
                or ``image-no-cache`` (``--keep-going`` with nothing to fall
                back on), leaving no partial file behind.
        """
        cache_root = os.environ.get(
            "VM_CREATE_CACHE_DIR", os.path.expanduser("~/vm-images")
        )
        cache_dir = Path(cache_root)

        # Determine image path and verification settings.
        if release_given or not image_given:
            # Standard release path.
            img_basename = f"{release}-server-cloudimg-amd64.img"
            sums_url = f"{self.DEFAULT_IMAGE_BASE}/{release}/current/SHA256SUMS"
            verifiable = True
            cache_dir = cache_dir / release
        else:
            # Bare --image path.
            img_basename = image_url.rsplit("/", 1)[-1]
            sums_url = image_url.rsplit("/", 1)[0] + "/SHA256SUMS"
            scheme = image_url.split("://")[0] if "://" in image_url else ""
            verifiable = scheme in ("file", "http", "https")
            cache_dir = cache_dir / "custom"

        img_path = cache_dir / img_basename
        sums_path = cache_dir / "SHA256SUMS"

        cache_dir.mkdir(parents=True, exist_ok=True)

        # Cache hit — a zero-byte leftover from an interrupted download is not
        # a hit: drop it and re-download rather than booting an empty disk.
        if img_path.exists() and img_path.stat().st_size == 0:
            img_path.unlink(missing_ok=True)

        if img_path.exists():
            if keep_going:
                output.warn(
                    f"vm-create: warning: reusing cached image "
                    f"(no download): {img_path}"
                )
            output.progress(f"image ready: {img_path}")
            # A cached file is a previously verified artifact (only a verified
            # download writes it), so the verification mode is reported rather
            # than re-run here (PI-14).
            verification = "sha256-sums" if verifiable else "skipped"
            return ImageFetch(
                path=str(img_path),
                cache_hit=True,
                downloaded=False,
                verification=verification,
                verified=verification != "skipped",
            )

        # Download (D4): directly to the final name in the cache dir.
        img_ok = True
        fail_msg = ""
        fail_code = "image-download-failed"

        if image_url.startswith("file://"):
            # Local fast-path: a plain copy, reliable where curl's file://
            # handling is not.
            try:
                shutil.copyfile(image_url.removeprefix("file://"), img_path)
            except OSError:
                img_ok = False
                fail_msg = f"download failed: {image_url}"
        else:
            # curl with retries (matches bash: curl -fL --retry 3). A timeout
            # counts as a failed download; the partial file is removed below.
            try:
                result = _spawn(
                    ["curl", "-fL", "--retry", "3", "-o", str(img_path), image_url],
                    timeout=600,
                )
                downloaded = result.returncode == 0
            except subprocess.TimeoutExpired:
                downloaded = False
            if not downloaded:
                img_ok = False
                fail_msg = f"download failed: {image_url}"

        # Verify.
        verification = "skipped"
        if img_ok and verifiable:
            if self._fetch_sums(sums_url, sums_path):
                # Verify the file against ITS OWN entry in the sums file.
                # The entry is selected by exact filename, not substring: a
                # substring match also picks up sibling entries (e.g.
                # 'foo.img' inside 'other-foo.img'). The digest is computed
                # locally, so no external sha256sum and no cwd dependency.
                entries = self._sums_entries(sums_path.read_text(errors="replace"))
                expected = [h for name, h in entries if name == img_basename]
                fail_code = "image-verification-failed"
                if len(expected) != 1:
                    # An image with no entry is NOT verified — accepting that
                    # would silently take a substituted artifact. Fail.
                    img_ok = False
                    fail_msg = (
                        f"SHA256 mismatch for {img_path}: "
                        + ("no entry" if not expected else f"{len(expected)} entries")
                        + f" for '{img_basename}' in SHA256SUMS"
                    )
                elif self._sha256_of(img_path) != expected[0]:
                    img_ok = False
                    fail_msg = f"SHA256 mismatch for {img_path}"
                else:
                    # Compared successfully against its own sums entry.
                    verification = "sha256-sums"
            else:
                # D1: no derivable same-directory SHA256SUMS -> skip
                # verification, warn.
                output.warn(
                    f"vm-create: warning: no same-directory SHA256SUMS for "
                    f"{image_url}; skipping verification"
                )

        # Failure handling. We only reach this when the cache lookup above
        # missed, so there is no previously cached image left to fall back on
        # (the file at this path is the partial/corrupt download we just made).
        if not img_ok:
            img_path.unlink(missing_ok=True)
            if keep_going:
                raise VirtError(
                    f"keep-going: {fail_msg}; no cached image to continue with",
                    "image-no-cache",
                    "image",
                )
            raise VirtError(fail_msg, fail_code, "image")

        output.progress(f"image ready: {img_path}")
        return ImageFetch(
            path=str(img_path),
            cache_hit=False,
            downloaded=True,
            verification=verification,
            verified=verification != "skipped",
        )

    @staticmethod
    def _fetch_sums(sums_url: str, dest: Path) -> bool:
        """Fetch *sums_url* to *dest*; False when it is not derivable."""
        if sums_url.startswith("file://"):
            src = sums_url.removeprefix("file://")
            try:
                shutil.copyfile(src, dest)
            except OSError:
                return False
            return True
        try:
            request = urllib.request.Request(
                sums_url, headers={"User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                dest.write_bytes(response.read())
        except (OSError, ValueError):
            # URLError/HTTPError/socket errors are OSErrors; a malformed URL
            # is a ValueError. All of them mean "no sums derivable".
            return False
        return True

    @staticmethod
    def _sums_entries(sums_text: str) -> list[tuple[str, str]]:
        """Parse sha256sum-format text into ``(filename, hex_digest)`` pairs.

        Accepts ``<hash>  <name>`` and the binary-mode marker
        ``<hash> *<name>``; blank and malformed lines are ignored.
        """
        entries = []
        for line in sums_text.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2:
                continue
            digest, name = fields[0], fields[1].strip()
            if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                continue
            # Optional binary-mode marker.
            entries.append((name.removeprefix("*"), digest.lower()))
        return entries

    @staticmethod
    def _sha256_of(path: Path) -> str:
        """Streamed sha256 hex digest of *path*."""
        digest = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    # ------------------------------------------------------------------
    # Cloud-init
    # ------------------------------------------------------------------

    def generate_cloud_init_files(
        self,
        name: str,
        user_name: str,
        ssh_key: str,
    ) -> tuple[str, str, str]:
        """Generate cloud-init user-data and meta-data files.

        Returns:
            Tuple of ``(temp_dir, user_data_path, meta_data_path)``.
            The temp dir is cleaned up on process exit via ``atexit``.
        """
        tmp_dir = tempfile.mkdtemp(prefix="virt-runner-cloud-init-")
        os.chmod(tmp_dir, 0o700)

        # Fresh lowercase UUID (meta-data `id:`).
        new_id = str(uuid.uuid4())

        try:
            ssh_key_line = Path(ssh_key).read_text().strip()
        except OSError as exc:
            raise VirtError(
                f"ssh key not found: {ssh_key}", "ssh-key-missing", "cloud-init"
            ) from exc

        # meta-data. PI-5: NoCloud applies `local-hostname:`; the `hostnames:`
        # dict alone leaves the image default hostname. Both are emitted.
        meta_data = (
            f"id: {new_id}\n"
            f"local-hostname: {name}\n"
            f"hostnames:\n"
            f"  local: {name}\n"
            f"  host: {name}\n"
        )

        # user-data
        user_data = (
            "#cloud-config\n"
            "manage_etc_hosts: true\n"
            "users:\n"
            f"  - name: {user_name}\n"
            "    groups: [sudo]\n"
            '    sudo: ["ALL=(ALL) NOPASSWD:ALL"]\n'
            "    ssh_authorized_keys:\n"
            f"      - {ssh_key_line}\n"
            "package_update: false\n"
        )

        (Path(tmp_dir) / "meta-data").write_text(meta_data)
        (Path(tmp_dir) / "user-data").write_text(user_data)
        os.chmod(Path(tmp_dir) / "meta-data", 0o600)
        os.chmod(Path(tmp_dir) / "user-data", 0o600)

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
            tmp_dir,
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

    def create_vm(
        self,
        name: str,
        ram_mib: int,
        vcpus: int,
        volume: str,
        mac: str,
        cloud_init_user_data: str,
        cloud_init_meta_data: str,
        ssh_key: str,
        no_boot: bool,
    ) -> str:
        """Create a VM via virt-install.

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
            "ubuntu-lts-latest",
            "--import",
            "--console",
            "none",
            # PI-9: `disable=on` is mandatory; a bare `--cloud-init` makes
            # virt-install generate a root password.
            "--cloud-init",
            (
                f"user-data={cloud_init_user_data},"
                f"meta-data={cloud_init_meta_data},"
                f"clouduser-ssh-key={ssh_key},"
                "disable=on"
            ),
            # PI-11: autostart always.
            "--autostart",
        ]

        if _passthrough(cmd).returncode != 0:
            # Clean up orphan resources.
            _quiet(["virsh", "--quiet", "destroy", name])
            _quiet(["virsh", "--quiet", "undefine", name])
            self.delete_volume_quiet(volume)
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

    def get_domain_uuid(self, name: str) -> str:
        """Return the domain UUID (PI-6: last field of the ``UUID:`` line)."""
        info = _stdout(["virsh", "dominfo", name])
        for line in info.strip().splitlines():
            if line.startswith("UUID"):
                return line.split()[-1]
        return ""

    def get_domain_state(self, name: str) -> str:
        """Return the domain state string (``running``, ``shut off``, …)."""
        return _stdout(["virsh", "domstate", name]).strip()

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
        attempts = max(timeout_s // interval, 1) if lease_usable else 0

        for _ in range(attempts):
            ip = self.find_ip_for_mac(lease_file, mac)
            # Belt-and-braces: a lease is accepted only while the domain
            # actually runs.
            if ip and self.get_domain_state(name) == "running":
                return ip
            time.sleep(interval)

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
        for _ in range(30):
            listing = _stdout(["virsh", "domifaddr", name])
            for line in listing.splitlines():
                fields = line.split()
                if len(fields) >= 3 and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", fields[2]):
                    return fields[2]
            time.sleep(self.POLL_INTERVAL_S)
        return None

    @staticmethod
    def _fallback_ip_arp(mac: str) -> str | None:
        """Best-effort IP via ``arp -n`` (matched by MAC, case-insensitive)."""
        mac_lower = mac.lower()
        table = _stdout(["arp", "-n"])
        for line in table.splitlines():
            fields = line.split()
            for i, field in enumerate(fields):
                if i > 0 and field.lower() == mac_lower:
                    return fields[i - 1]
        return None

    def verify_ssh_reachable(
        self,
        name: str,
        user: str,
        ip: str,
        timeout_s: int = 90,
    ) -> None:
        """Verify SSH connectivity (PI-13: success requires a round-trip).

        Raises:
            VirtError: ``ssh-timeout`` when no attempt succeeds.
        """
        probe = [
            "ssh",
            "-o",
            "ConnectTimeout=2",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"{user}@{ip}",
            "exit",
        ]
        interval = self.POLL_INTERVAL_S
        for _ in range(max(timeout_s // interval, 1)):
            if _spawn(probe).returncode == 0:
                return
            time.sleep(interval)
        raise VirtError(
            f"SSH to {user}@{ip} not reachable yet — check: virsh console {name}",
            "ssh-timeout",
            "ssh-verify",
        )

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def get_pool_path(self) -> str:
        """Return the pool's directory path from its XML definition."""
        xml = _stdout(["virsh", "pool-dumpxml", self.POOL])
        for line in xml.splitlines():
            match = re.search(r"<path>([^<]*)</path>", line)
            if match:
                return match.group(1)
        raise VirtError(
            f"failed to determine path of pool '{self.POOL}'",
            "pool-path-unknown",
        )

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
            if not self._has_pool_disk(name, pool_path):
                continue

            state = self.get_domain_state(name)
            mac = self._first_mac(name)
            ip = (self.find_ip_for_mac(lease_file, mac) or "") if mac else ""

            results.append(
                VmInfo(
                    name=name,
                    uuid=self.get_domain_uuid(name),
                    state=state,
                    mac=mac,
                    ip=ip,
                    ip_status=ip_status_for(state, mac, ip),
                )
            )

        return results

    @staticmethod
    def _has_pool_disk(name: str, pool_path: str) -> bool:
        """True if any of the domain's disk sources lives under *pool_path*.

        Compared on path components: a bare prefix match would also accept a
        sibling directory such as ``<pool_path>-evil``.
        """
        listing = _stdout(["virsh", "domblklist", name])
        for line in listing.strip().splitlines()[2:]:  # skip header
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith(f"{pool_path}/"):
                return True
        return False

    @staticmethod
    def _first_mac(name: str) -> str:
        """MAC of the domain's first interface, or ``""`` when unparseable."""
        listing = _stdout(["virsh", "domiflist", name])
        for line in listing.splitlines():
            fields = line.split()
            if len(fields) >= 5 and re.fullmatch(
                r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}", fields[4]
            ):
                return fields[4]
        return ""

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
        if was_running:
            _run(["virsh", "--quiet", "destroy", name])
        _run(["virsh", "--quiet", "undefine", name])

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
