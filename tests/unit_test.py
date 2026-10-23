"""Pure-logic checks (no libvirt, no network). Run: uv run tests/unit_test.py"""

import json

import virt_runner.cmd_modify as cm
import virt_runner.virtualizer as vz
from virt_runner import images
from virt_runner.cmd_create import _validate_mounts, _validate_user
from virt_runner.errors import VirtError
from virt_runner.images import _newest_match, _resolve_glob, _sums_entries
from virt_runner.profiles import PROFILES, _same_dir
from virt_runner.virtualizer import (
    Virtualizer,
    _pick_no_secboot_loader,
    render_meta_data,
    render_user_data,
    script_command,
)

# `modify` reads a domain through these three calls; one XML sample is enough
# to check the parser without a host.
VM_XML = """<domain type='kvm' id='3'>
  <name>t1</name>
  <uuid>6f6da4d2-9d67-4d6a-9c1a-0f0f0f0f0f0f</uuid>
  <memory unit='KiB'>2097152</memory>
  <currentMemory unit='KiB'>2097152</currentMemory>
  <vcpu placement='static' current='2'>4</vcpu>
  <os>
    <type arch='x86_64' machine='pc-q35-9.0'>hvm</type>
  </os>
  <devices>
    <disk type='file' device='disk'>
      <source file='/var/lib/libvirt/images/t1_vda.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='network'>
      <mac address='52:54:00:AA:BB:CC'/>
      <source network='default'/>
      <model type='virtio'/>
    </interface>
  </devices>
</domain>
"""


def vm_spec(**overrides):
    """A running 2 vCPU / 2 GiB / 8 GiB single-NIC guest, bent per test."""
    fields = {
        "name": "t1",
        "uuid": "6f6da4d2-9d67-4d6a-9c1a-0f0f0f0f0f0f",
        "state": "running",
        "vcpu": 2,
        "vcpu_max": 4,
        "ram_kib": 2 * 1024 * 1024,
        "ram_max_kib": 2 * 1024 * 1024,
        "disk_target": "vda",
        "disk_gib": 8,
        "interfaces": (vz.Interface("network", "default", "52:54:00:aa:bb:cc"),),
    }
    fields.update(overrides)
    return vz.VmSpec(**fields)


class FakeVz:
    """A `Virtualizer` that only remembers what libvirt was asked to do.

    The live answers follow libvirt's own rules (a change above the ceiling is
    not live, a guest that is off does not balloon), so the plan/apply split is
    exercised for real; `net_error`/`fs_code` inject the failures.
    """

    POOL = "vm-pool"
    DEFAULT_USER = "ubuntu"

    def __init__(
        self,
        *,
        running=True,
        spec=None,
        net_error=None,
        fs_code=0,
        fs_out="virt-runner: guest filesystem grown",
    ):
        self.running = running
        self.spec = spec or vm_spec(state="running" if running else "shut off")
        self.net_error = net_error
        self.fs_code = fs_code
        self.fs_out = fs_out
        self.vcpu_calls = []
        self.ram_calls = []
        self.disks = []
        self.grown = []
        self.ifaces = []
        self.reboots = 0

    @staticmethod
    def private_key_path(pub_path):
        return "/tmp/vr-test/id_ed25519"

    # --- identity (the command guards with these before changing anything) ---

    def require_libvirt(self):
        pass

    def require_pool_defined(self):
        pass

    def domain_exists(self, name):
        return name != "ghost"

    def owns_domain(self, name):
        return name != "foreign"

    def domain_meta(self, name):
        return {"user": "ci", "identity": "/tmp/vr-test/id_ed25519"}

    def disk_volume(self, name):
        return f"{name}_vda.qcow2"

    def domain_spec(self, name):
        return self.spec

    def network_defined(self, network):
        return network in ("default", "mgmt")

    def vm_ip(self, name, lease_file):
        return "192.168.122.11" if self.running else None

    # --- changes ---

    def set_vcpu(self, name, vcpu, *, live, ceiling):
        self.vcpu_calls.append((vcpu, live, ceiling))
        return live and self.running and vcpu <= ceiling

    def set_ram_gib(self, name, ram_gib, *, live, ceiling):
        self.ram_calls.append((ram_gib, live))
        return live and self.running and ram_gib * 1024 * 1024 <= ceiling

    def grow_disk(self, name, target, volume, gib):
        self.disks.append((name, target, volume, gib))

    def grow_guest_filesystem(self, user, ip, target, identity=None):
        self.grown.append((user, ip, target))
        return self.fs_code, self.fs_out

    def attach_interface(self, name, iface, *, live):
        self._iface("attach", name, iface, live)

    def detach_interface(self, name, iface, *, live):
        self._iface("detach", name, iface, live)

    def _iface(self, action, name, iface, live):
        if self.net_error:
            raise VirtError(
                f"failed to {action} {iface} NIC of '{name}': {self.net_error}",
                "net-change-failed",
            )
        self.ifaces.append((action, iface.type, iface.source, iface.mac, live))

    def reboot(self, name):
        self.reboots += 1

    def wait_for_ip(self, name, mac, lease_file, timeout_s=120):
        return "192.168.122.99"

    def verify_ssh_reachable(self, name, user, ip, identity=None):
        pass


def modify_cli(fake, argv):
    """Run `virt-runner modify` against *fake* and return (exit, parsed JSON)."""
    from click.testing import CliRunner

    from virt_runner.cli import main as cli_main
    from virt_runner.output import set_json_mode

    real = cm.Virtualizer
    cm.Virtualizer = lambda: fake
    try:
        res = CliRunner().invoke(cli_main, ["modify", *argv])
    finally:
        cm.Virtualizer = real
        set_json_mode(False)
    body = json.loads(res.stdout) if res.stdout.strip() else {}
    return res.exit_code, body


def main() -> None:
    # 1.1 — Fedora sums_url resolves the compose name, not the image name.
    # One regex covers both arch tokens.
    fed = PROFILES["fedora"]
    img = (
        "https://example/pub/fedora/linux/releases/44/Cloud/x86_64/images/"
        "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
    )
    assert fed.sums_url("x86_64", img).endswith(
        "/Fedora-Cloud-44-1.7-x86_64-CHECKSUM"
    ), fed.sums_url("x86_64", img)
    img64 = (
        "https://example/pub/fedora/linux/releases/44/Cloud/aarch64/images/"
        "Fedora-Cloud-Base-Generic-44-1.7.aarch64.qcow2"
    )
    assert fed.sums_url("aarch64", img64).endswith(
        "/Fedora-Cloud-44-1.7-aarch64-CHECKSUM"
    ), fed.sums_url("aarch64", img64)

    # 1.2 — ubuntu/arch sums_url outputs.
    ubu = PROFILES["ubuntu"]
    arch = PROFILES["arch"]
    ubu_img = "https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img"
    assert _same_dir(ubu_img, "SHA256SUMS") in ubu.sums_url("x86_64", ubu_img)
    assert ubu.sums_url("x86_64", ubu_img).endswith("/SHA256SUMS")
    assert ubu.sums_url("aarch64", ubu_img).endswith("/SHA256SUMS")
    assert arch.sums_url("x86_64", "https://example/arch.img").endswith(".SHA256")

    # image_url — per-arch templates (spec G16).
    assert (
        PROFILES["ubuntu"]
        .image_url("x86_64", "resolute")
        .endswith("resolute-server-cloudimg-amd64.img")
    )
    assert (
        PROFILES["ubuntu"]
        .image_url("aarch64", "resolute")
        .endswith("resolute-server-cloudimg-arm64.img")
    )
    assert "Cloud/x86_64/images/" in PROFILES["fedora"].image_url("x86_64", "44")
    assert "Cloud/aarch64/images/" in PROFILES["fedora"].image_url("aarch64", "44")
    assert (
        PROFILES["arch"]
        .image_url("x86_64", "latest")
        .endswith("Arch-Linux-x86_64-cloudimg.qcow2")
    )
    # arch-boxes is x86_64-only: a missing arch is a clear error.
    for bad_call in (
        lambda: PROFILES["arch"].image_url("aarch64", "latest"),
        lambda: PROFILES["arch"].sums_url("aarch64", "https://example/arch.img"),
    ):
        try:
            bad_call()
            raise AssertionError("expected VirtError")
        except VirtError as e:
            assert "distro 'arch' has no aarch64 image" in str(e), str(e)
            assert e.code == "image-arch-unavailable", e.code

    # _sums_entries — sha256sum line.
    entries = _sums_entries(
        "aabbccdd00112233445566778899aabbccddeeff00112233445566778899aabb  file.img\n"
    )
    assert entries == [
        ("file.img", "aabbccdd00112233445566778899aabbccddeeff00112233445566778899aabb")
    ]

    # _sums_entries — *binary marker.
    entries = _sums_entries(
        "ddeeff00112233445566778899aabbccddeeff00112233445566778899aabbcc *binary.img\n"
    )
    assert entries == [
        (
            "binary.img",
            "ddeeff00112233445566778899aabbccddeeff00112233445566778899aabbcc",
        )
    ]

    # _sums_entries — PGP-wrapped Fedora block (header/signature ignored).
    entries = _sums_entries(
        "-----BEGIN PGP SIGNED MESSAGE-----\n"
        "Hash: SHA256\n\n"
        "SHA256 (Fedora-Cloud-44-1.7-x86_64-CHECKSUM) = aabbccdd00112233445566778899aabbccddeeff00112233445566778899aabb\n"
        "-----BEGIN PGP SIGNATURE-----\n"
        "...\n"
        "-----END PGP SIGNATURE-----\n"
    )
    assert len(entries) == 1
    assert entries[0][0] == "Fedora-Cloud-44-1.7-x86_64-CHECKSUM"

    # _sums_entries — junk lines ignored.
    entries = _sums_entries("not a hash\n\nmore junk\n")
    assert entries == []

    # _newest_match — 1.10 beats 1.7.
    names = [
        "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2",
        "Fedora-Cloud-Base-Generic-44-1.10.x86_64.qcow2",
    ]
    result = _newest_match(names, "Fedora-Cloud-Base-Generic-44-*.x86_64.qcow2")
    assert result == "Fedora-Cloud-Base-Generic-44-1.10.x86_64.qcow2", result

    # _newest_match — no match returns None.
    assert _newest_match(["foo.txt"], "*.qcow2") is None

    # _resolve_glob — newest wins online; offline falls back to cache or raises.
    import subprocess
    import tempfile
    from pathlib import Path

    def fake(rc, out):
        return lambda *a, **k: subprocess.CompletedProcess(a, rc, out, "")

    gurl = "https://x/images/Fedora-Cloud-Base-Generic-44-*.x86_64.qcow2"
    html = (
        '<a href="Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2">'
        '<a href="Fedora-Cloud-Base-Generic-44-1.10.x86_64.qcow2">'
    )
    real_spawn = images._spawn
    try:
        images._spawn = fake(0, html)
        assert _resolve_glob(gurl).endswith("44-1.10.x86_64.qcow2")
        images._spawn = fake(6, "")
        for cdir in (Path("/nonexistent/x"), Path(tempfile.mkdtemp())):
            try:
                _resolve_glob(gurl, cdir)
                raise AssertionError("expected VirtError")
            except VirtError as e:
                assert e.code == "image-download-failed", e.code
        warm = Path(tempfile.mkdtemp())
        (warm / "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2").touch()
        assert _resolve_glob(gurl, warm).endswith("44-1.7.x86_64.qcow2")
    finally:
        images._spawn = real_spawn

    # _glob_candidates — newest first; empty for non-glob or no match.
    try:
        images._spawn = fake(0, html)
        cands = images._glob_candidates(gurl)
        assert [u.rsplit("/", 1)[-1] for u in cands] == [
            "Fedora-Cloud-Base-Generic-44-1.10.x86_64.qcow2",
            "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2",
        ], cands
        assert images._glob_candidates("https://x/images/plain.img") == []
        images._spawn = fake(6, "")
        assert images._glob_candidates(gurl, Path(tempfile.mkdtemp())) == []
    finally:
        images._spawn = real_spawn

    # fetch_and_verify_image — dead newest candidate (404) falls back to
    # the next-newest name.
    import os

    dead = "https://x/images/Fedora-Cloud-Base-Generic-44-1.10.x86_64.qcow2"
    live = "https://x/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
    listing2 = (
        f'<a href="{dead.rsplit("/", 1)[-1]}"><a href="{live.rsplit("/", 1)[-1]}">'
    )
    cache = Path(tempfile.mkdtemp(prefix="vr-img-"))
    downloads: list[str] = []
    real_fetch_sums = images._fetch_sums

    def fake_fetch(cmd):
        if "-o" in cmd:
            target, src = cmd[cmd.index("-o") + 1], cmd[-1]
            downloads.append(src)
            if src == dead:
                return subprocess.CompletedProcess(cmd, 22)  # 404
            Path(target).write_bytes(b"img")
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0, stdout=listing2)

    os.environ["VM_CREATE_CACHE_DIR"] = str(cache)
    try:
        images._spawn = fake_fetch
        images._fetch_sums = lambda url, dest: False
        fetched = images.fetch_and_verify_image(
            fed, "x86_64", "44", False, gurl, False, False
        )
        assert fetched.path.endswith("44-1.7.x86_64.qcow2"), fetched.path
        assert downloads == [dead, live], downloads
    finally:
        images._spawn = real_spawn
        images._fetch_sums = real_fetch_sums
        os.environ.pop("VM_CREATE_CACHE_DIR", None)

    # fetch_and_verify_image — an unverified newest candidate (checksums
    # missing on a partially synced mirror) falls back to the next-newest
    # verifiable build; when none verifies, the newest unverified image is
    # returned (D1's skip).
    import hashlib

    h = hashlib.sha256(b"img").hexdigest()
    cache2 = Path(tempfile.mkdtemp(prefix="vr-img2-"))
    downloads2: list[str] = []

    def fake_fetch2(cmd):
        if "-o" in cmd:
            target, src = cmd[cmd.index("-o") + 1], cmd[-1]
            downloads2.append(src)
            Path(target).write_bytes(b"img")
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0, stdout=listing2)

    def fake_sums2(url, dest):
        if "1.7" in url:
            dest.write_text(
                f"SHA256 (Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2) = {h}\n"
            )
            return True
        return False

    os.environ["VM_CREATE_CACHE_DIR"] = str(cache2)
    try:
        images._spawn = fake_fetch2
        images._fetch_sums = fake_sums2
        fetched = images.fetch_and_verify_image(
            fed, "x86_64", "44", False, gurl, False, False
        )
        assert fetched.path.endswith("44-1.7.x86_64.qcow2"), fetched.path
        assert fetched.verified, fetched
        assert downloads2 == [dead, live], downloads2

        images._fetch_sums = lambda url, dest: False
        os.environ["VM_CREATE_CACHE_DIR"] = tempfile.mkdtemp(prefix="vr-img3-")
        fetched = images.fetch_and_verify_image(
            fed, "x86_64", "44", False, gurl, False, False
        )
        assert fetched.path.endswith("44-1.10.x86_64.qcow2"), fetched.path
        assert not fetched.verified, fetched
    finally:
        images._spawn = real_spawn
        images._fetch_sums = real_fetch_sums
        os.environ.pop("VM_CREATE_CACHE_DIR", None)

    # render_user_data — key line appears JSON-quoted, output starts with #cloud-config.
    user_data = render_user_data("testuser", "sudo", "ssh-ed25519 # comment: key")
    assert user_data.startswith("#cloud-config")
    assert '"ssh-ed25519 # comment: key"' in user_data

    # render_meta_data — produces valid YAML.
    meta = render_meta_data("abc-123", "myvm")
    assert "id: abc-123" in meta
    assert "local-hostname: myvm" in meta

    # 1.8 — preflight accepts x86_64 and aarch64, refuses anything else.
    v = Virtualizer()
    v.host_arch = lambda: "riscv64"
    try:
        v.preflight_check()
        raise AssertionError("expected VirtError")
    except VirtError as e:
        assert e.code == "unsupported-arch", e.code

    # aarch64 passes the arch gate (any later failure is a different code —
    # or nothing at all, when libvirt/pool/net are all up on this host).
    v2 = Virtualizer()
    v2.host_arch = lambda: "aarch64"
    try:
        v2.preflight_check()
    except VirtError as e:
        assert e.code != "unsupported-arch", e.code

    # _validate_user — good value passes through.
    assert _validate_user(None, None, "ubuntu") == "ubuntu"

    # _validate_user — bad value raises BadParameter.
    import click

    try:
        _validate_user(None, None, "Bad User")
        raise AssertionError("expected BadParameter")
    except click.BadParameter:
        pass

    # _validate_mounts — default guest path, explicit guest path, tags by position.
    share = Path(tempfile.mkdtemp(prefix="vr-share-"))
    got = _validate_mounts(None, None, (str(share), f"{share}:/work/src"))
    assert got == [
        {
            "source": str(share.resolve()),
            "target": f"/mnt/{share.name}",
            "tag": "mount0",
        },
        {"source": str(share.resolve()), "target": "/work/src", "tag": "mount1"},
    ], got
    assert _validate_mounts(None, None, ()) == []

    # _validate_mounts — each bad input is a usage error.
    comma = Path(tempfile.mkdtemp(prefix="vr,share-"))
    for bad in (
        ("/nonexistent/dir",),  # missing host dir
        (f"{share}:relative",),  # guest not absolute
        (f"{share}:/mnt/a b",),  # unsafe char
        (f"{share}:/mnt/..",),  # dot-dot component
        (f"{share}:/x", f"{share}:/x"),  # duplicate guest path
        (str(comma),),  # comma in host path
    ):
        try:
            _validate_mounts(None, None, bad)
            raise AssertionError(f"expected BadParameter for {bad}")
        except click.BadParameter:
            pass

    # render_user_data — no mounts: no bootcmd; with mounts: idempotent virtiofs line.
    assert "bootcmd" not in render_user_data("u", "sudo", "k")
    ud = render_user_data("u", "sudo", "k", got)
    assert "bootcmd:\n" in ud
    assert "mount -t virtiofs mount1 /work/src" in ud
    assert "mountpoint -q /work/src" in ud
    # fstab fallback: the NoCloud seed is first-boot-only, so later reboots
    # mount from fstab (cloud-init may be disabled then, e.g. Ubuntu 26.04).
    assert "/etc/fstab" in ud
    assert "mount1 /work/src virtiofs defaults,nofail 0 0" in ud

    # create_vm — mounts add shared memory + one --filesystem per mount;
    # the NoCloud seed goes in as a plain --disk CDROM (never --cloud-init,
    # whose ISO virt-install deletes while the guest is still booting).
    captured = {}
    real_pt = vz._passthrough
    try:
        vz._passthrough = lambda cmd: (
            captured.setdefault("cmd", cmd),
            subprocess.CompletedProcess(cmd, 0),
        )[1]
        v_cv = Virtualizer()
        v_cv.uefi_no_secboot_loader = lambda: None  # hermetic: no virsh
        v_cv.create_vm(
            "n",
            1024,
            1,
            "vol",
            "52:54:00:00:00:01",
            "n-cloudinit.iso",
            "generic",
            no_boot=False,
            mounts=got,
        )
    finally:
        vz._passthrough = real_pt
    cmd = captured["cmd"]
    assert "source.type=memfd,access.mode=shared" in cmd
    assert sum(a == "--filesystem" for a in cmd) == 2
    assert f"source.dir={share.resolve()},target.dir=mount0,driver.type=virtiofs," in (
        " ".join(cmd)
    )
    assert "--cloud-init" not in cmd
    assert "vol=vm-pool/n-cloudinit.iso,device=cdrom" in cmd

    # _pick_no_secboot_loader — only an explicit no-secboot marker is
    # trusted: the first entry may be the Secure Boot build, and hosts
    # without such a firmware yield None (keep virt-install's default).
    assert (
        _pick_no_secboot_loader(
            [
                "/usr/share/AAVMF/AAVMF_CODE.ms.fd",
                "/usr/share/AAVMF/AAVMF_CODE.secboot.fd",
                "/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd",
            ]
        )
        == "/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd"
    )
    assert _pick_no_secboot_loader(["/usr/share/AAVMF/AAVMF_CODE.ms.fd"]) is None
    assert _pick_no_secboot_loader([]) is None

    # uefi_no_secboot_loader — parses the domcapabilities loader list
    # (absolute paths only: enum values like rom/pflash are ignored).
    real_stdout = vz._stdout
    try:
        v_ld = Virtualizer()
        v_ld.host_arch = lambda: "aarch64"
        vz._stdout = lambda cmd: (
            "<loader supported='yes'>\n"
            "  <value>/usr/share/AAVMF/AAVMF_CODE.ms.fd</value>\n"
            "  <value>/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd</value>\n"
            "  <enum name='type'><value>rom</value><value>pflash</value></enum>\n"
            "</loader>"
        )
        assert (
            v_ld.uefi_no_secboot_loader() == "/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd"
        )
        vz._stdout = lambda cmd: ""
        assert v_ld.uefi_no_secboot_loader() is None
    finally:
        vz._stdout = real_stdout

    # create_vm — aarch64 opts out of Secure Boot via the no-secboot UEFI
    # firmware (Fedora's aarch64 image ships an unsigned GRUB, which the
    # Secure Boot build rejects at firmware level); no --boot when the host
    # offers no such firmware, and no override on x86_64 at all.
    real_pt2 = vz._passthrough
    real_arch = Virtualizer.host_arch
    try:

        def run_create(name: str, loader: str | None, arch: str) -> list:
            cap: dict = {}
            vz._passthrough = lambda c: (
                cap.setdefault("cmd", c),
                subprocess.CompletedProcess(c, 0),
            )[1]
            Virtualizer.host_arch = staticmethod(lambda: arch)
            v_sb = Virtualizer()
            v_sb.uefi_no_secboot_loader = lambda: loader
            v_sb.create_vm(
                name,
                512,
                1,
                "vol",
                f"52:54:00:00:00:{name[1]}",
                f"{name}-cloudinit.iso",
                "generic",
                no_boot=False,
            )
            return cap["cmd"]

        cmd = run_create("sb1", "/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd", "aarch64")
        assert "--boot" in cmd
        assert "uefi=on,loader=/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd" in cmd
        assert "--boot" not in run_create("sb2", None, "aarch64")
        assert "--boot" not in run_create(
            "sb3", "/usr/share/OVMF/OVMF_CODE.secboot.fd", "x86_64"
        )
    finally:
        vz._passthrough = real_pt2
        Virtualizer.host_arch = real_arch

    # render_user_data — first boot disables cloud-init for later boots
    # (the old --cloud-init disable=on effect) even while the seed stays in.
    assert (
        'runcmd:\n  - echo "Disabled by virt-runner" > /etc/cloud/cloud-init.disabled'
        in ud
    )

    # build_cloud_init_iso — builds with xorrisofs and uploads a pool
    # volume (returns the volume name); xorrisofs failure is
    # cloud-init-failed.
    import shutil

    ud_dir = Path(tempfile.mkdtemp(prefix="vr-cid-"))
    (ud_dir / "user-data").write_text("#cloud-config\n")
    (ud_dir / "meta-data").write_text("id: x\n")
    real_spawn_iso = vz._spawn
    real_which = shutil.which
    try:
        v3 = Virtualizer()
        # Hermetic: pretend the host has xorrisofs ...
        shutil.which = lambda name: "/usr/bin/xorrisofs"
        vz._spawn = lambda cmd: subprocess.CompletedProcess(cmd, 1, stderr="boom")
        try:
            v3.build_cloud_init_iso(
                "iso-vm", str(ud_dir / "user-data"), str(ud_dir / "meta-data")
            )
            raise AssertionError("expected VirtError")
        except VirtError as e:
            assert e.code == "cloud-init-failed", e.code

        # ... and a missing xorrisofs is a clean cloud-init-failed, not a
        # traceback.
        shutil.which = lambda name: None
        try:
            v3.build_cloud_init_iso(
                "iso-vm", str(ud_dir / "user-data"), str(ud_dir / "meta-data")
            )
            raise AssertionError("expected VirtError")
        except VirtError as e:
            assert e.code == "cloud-init-failed", e.code
            assert "xorriso" in str(e)
        shutil.which = lambda name: "/usr/bin/xorrisofs"

        def fake_spawn(cmd):
            if cmd[0] == "xorrisofs":
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"iso")
            return subprocess.CompletedProcess(cmd, 0)

        vz._spawn = fake_spawn
        vol = v3.build_cloud_init_iso(
            "iso-vm", str(ud_dir / "user-data"), str(ud_dir / "meta-data")
        )
        assert vol == "iso-vm-cloudinit.iso", vol
        assert v3.cloud_init_iso_volume("iso-vm") == "iso-vm-cloudinit.iso"
    finally:
        vz._spawn = real_spawn_iso
        shutil.which = real_which
        shutil.rmtree(ud_dir, ignore_errors=True)

    # require_virtiofsd — missing binary is virtiofsd-missing.
    v2 = Virtualizer()
    v2.VIRTIOFSD = "/nonexistent/virtiofsd"
    try:
        v2.require_virtiofsd()
        raise AssertionError("expected VirtError")
    except VirtError as e:
        assert e.code == "virtiofsd-missing", e.code

    # script_command — #! wins, else .py -> python3, else bash; waits for cloud-init.
    sdir = Path(tempfile.mkdtemp())
    (sdir / "a.py").write_text("#!/bin/sh\necho hi\n")
    (sdir / "b.py").write_text("print('hi')\n")
    (sdir / "c.sh").write_text("echo hi\n")
    assert '; "$f" </dev/null' in script_command(sdir / "a.py")
    assert 'python3 "$f"' in script_command(sdir / "b.py")
    assert 'bash "$f"' in script_command(sdir / "c.sh")
    assert "cloud-init status --wait" in script_command(sdir / "c.sh")

    # ------------------------------------------------------------------
    # modify — sizes, domain spec, planning, applying (all pure / faked)
    # ------------------------------------------------------------------

    # parse_size_kib — a bare number is KiB (domain XML); suffixed values take
    # both IEC (vol-info) and SI (domblkinfo) spellings; junk stays None so a
    # caller says "unknown" instead of resizing against a guessed ceiling.
    assert vz.parse_size_kib("2097152") == 2097152
    assert vz.parse_size_kib("2097152", "KiB") == 2097152
    assert vz.parse_size_kib("2.0 GiB") == 2 * 1024 * 1024
    assert vz.parse_size_kib("1 MiB") == 1024
    assert vz.parse_size_kib("1 GiB") == 1024 * 1024
    assert vz.parse_size_kib("1 GB") < vz.parse_size_kib("1 GiB")  # SI is smaller
    assert vz.parse_size_kib("1 T") == 1024**3
    assert vz.parse_size_kib("") is None
    assert vz.parse_size_kib("n/a") is None
    assert vz.parse_size_kib("8 XB") is None

    # parse_capacity_gib — the Capacity row only, allocation/available ignored.
    assert (
        vz.parse_capacity_gib(
            " Volume      t1_vda.qcow2\n Pool      vm-pool\n"
            " Capacity: 8.0 GiB\n Allocation: 1.2 GiB\n"
        )
        == 8
    )
    assert vz.parse_capacity_gib(" Capacity: 16.0 GiB") == 16
    assert vz.parse_capacity_gib(" Capacity: 8589934592 B") == 8  # domblkinfo bytes
    assert vz.parse_capacity_gib("no capacity here") is None
    assert vz.parse_capacity_gib("Capacity: n/a") is None

    # parse_vm_spec — the facts `modify` plans from; disk_gib stays None because
    # the size lives in the volume, and state stays empty for the caller to fill.
    spec = vz.parse_vm_spec(VM_XML)
    assert spec is not None
    assert (spec.name, spec.uuid) == ("t1", "6f6da4d2-9d67-4d6a-9c1a-0f0f0f0f0f0f")
    assert (spec.vcpu, spec.vcpu_max) == (2, 4)
    assert (spec.ram_kib, spec.ram_max_kib, spec.ram_gib) == (
        2 * 1024 * 1024,
        2 * 1024 * 1024,
        2,
    )
    assert spec.disk_target == "vda" and spec.disk_gib is None
    assert spec.state == "" and spec.running is False
    assert [i.document() for i in spec.interfaces] == [
        {"type": "network", "source": "default", "mac": "52:54:00:aa:bb:cc"}
    ]
    assert spec.primary is spec.interfaces[0]
    assert spec.document()["interfaces"][0]["mac"] == "52:54:00:aa:bb:cc"
    # maxMemory (a guest with hot-plug headroom) is the live ceiling.
    headroom = vz.parse_vm_spec(
        VM_XML.replace(
            "<currentMemory unit='KiB'>2097152</currentMemory>",
            "<maxMemory slots='4' unit='KiB'>4194304</maxMemory>",
        )
    )
    assert headroom.ram_kib == 2 * 1024 * 1024
    assert headroom.ram_max_kib == 4 * 1024 * 1024
    assert headroom.ram_gib == 2
    # Foreign or broken input is not a VM, it is None.
    assert vz.parse_vm_spec("") is None
    assert vz.parse_vm_spec("<domain><name>x</name></domain>") is None
    assert vz.parse_vm_spec("not xml at all") is None

    # parse_net / parse_net_add / parse_mac — the accepted spellings, and the
    # junk that must be a usage error before libvirt is ever called.
    assert cm.parse_net("default") == vz.Interface("network", "default", "")
    assert cm.parse_net("bridge=br0") == vz.Interface("bridge", "br0", "")
    assert cm.parse_net_add("network=iso,mac=52:54:00:00:00:01") == vz.Interface(
        "network", "iso", "52:54:00:00:00:01"
    )
    for bad in ("network=", "type=default", "", "network=default,mtu=1200"):
        try:
            (cm.parse_net_add if "," in bad else cm.parse_net)(bad)
            raise AssertionError(f"expected UsageError for {bad!r}")
        except click.UsageError:
            pass
    assert cm.parse_mac("52:54:00:AA:BB:CC") == "52:54:00:aa:bb:cc"
    for bad_mac in ("52:54:00:aa:bb", "aa:bb:cc:dd:ee:ff:00", "zz:54:00:aa:bb:cc"):
        try:
            cm.parse_mac(bad_mac)
            raise AssertionError(f"expected UsageError for {bad_mac!r}")
        except click.UsageError:
            pass

    # plan_changes — vCPU/RAM are only live when libvirt allows it, so the
    # ceiling decides `effective` before anything is executed.
    ch, warn = cm.plan_changes(
        vm_spec(), cm.Request(vcpu=3), live=False, auto_grow_fs=True
    )
    assert [(c.field, c.from_, c.to, c.effective) for c in ch] == [
        ("vcpu", 2, 3, "on-boot")
    ]
    assert warn == ["vCPU change applies on next boot — consider --reboot"]
    # `--reboot` makes the hint pointless, so it is not repeated in the plan.
    _, warn = cm.plan_changes(
        vm_spec(), cm.Request(vcpu=3), live=False, auto_grow_fs=True, reboot=True
    )
    assert warn == []
    ch, _ = cm.plan_changes(vm_spec(), cm.Request(vcpu=3), live=True, auto_grow_fs=True)
    assert ch[0].effective == "now"  # 3 <= <vcpu max> 4, domain running
    ch, _ = cm.plan_changes(vm_spec(), cm.Request(vcpu=8), live=True, auto_grow_fs=True)
    assert ch[0].effective == "on-boot"  # above the ceiling: needs a reboot
    ch, _ = cm.plan_changes(
        vm_spec(state="shut off"), cm.Request(vcpu=3), live=True, auto_grow_fs=True
    )
    assert ch[0].effective == "on-boot"
    # RAM: 2 GiB <memory> and no <maxMemory> -> a grow cannot be live, a shrink can.
    ch, _ = cm.plan_changes(
        vm_spec(), cm.Request(ram_gib=4), live=True, auto_grow_fs=True
    )
    assert ch[0].effective == "on-boot"
    ch, _ = cm.plan_changes(
        vm_spec(), cm.Request(ram_gib=1), live=True, auto_grow_fs=True
    )
    assert ch[0].effective == "now"
    ch, _ = cm.plan_changes(
        vm_spec(ram_max_kib=4 * 1024 * 1024),
        cm.Request(ram_gib=4),
        live=True,
        auto_grow_fs=True,
    )
    assert ch[0].effective == "now"

    # Disk: grow only, and the guest filesystem is part of the same change.
    ch, warn = cm.plan_changes(
        vm_spec(), cm.Request(disk_gib=16), live=False, auto_grow_fs=True
    )
    assert ch[0].field == "disk_gib" and ch[0].to == 16
    assert [op for op, _ in ch[0].ops] == ["disk", "fs-grow"]
    assert ch[0].extra == {"fs_grown": None}
    ch, warn = cm.plan_changes(
        vm_spec(), cm.Request(disk_gib=16), live=False, auto_grow_fs=False
    )
    assert [op for op, _ in ch[0].ops] == ["disk"]
    assert any("--no-auto-grow-fs" in w for w in warn), warn
    # The same size is not a noop when the guest filesystem still has to grow:
    # re-running `--disk 8` after an off-domain grow is how that finishes.
    retry = cm.plan_changes(
        vm_spec(), cm.Request(disk_gib=8), live=False, auto_grow_fs=True
    )[0]
    assert [(c.field, c.from_, c.to) for c in retry] == [("disk_gib", 8, 8)]
    assert [op for op, _ in retry[0].ops] == ["fs-grow"]
    try:
        cm.plan_changes(
            vm_spec(), cm.Request(disk_gib=8), live=False, auto_grow_fs=False
        )
        raise AssertionError("expected modify-noop")
    except VirtError as e:
        assert e.code == "modify-noop", e.code

    # Shrinking and no-ops are refused with the documented codes.
    try:
        cm.plan_changes(
            vm_spec(), cm.Request(disk_gib=4), live=False, auto_grow_fs=True
        )
        raise AssertionError("expected disk-shrink-unsupported")
    except VirtError as e:
        assert e.code == "disk-shrink-unsupported", e.code
    try:
        cm.plan_changes(vm_spec(), cm.Request(vcpu=2), live=True, auto_grow_fs=True)
        raise AssertionError("expected modify-noop")
    except VirtError as e:
        assert e.code == "modify-noop", e.code
    # A size we cannot read is never resized blindly.
    try:
        cm.plan_changes(
            vm_spec(disk_gib=None),
            cm.Request(disk_gib=16),
            live=False,
            auto_grow_fs=True,
        )
        raise AssertionError("expected runtime-error")
    except VirtError as e:
        assert e.code == "runtime-error", e.code

    # Networking: the primary NIC is re-attached, NICs are added/removed, and
    # the requests that would strand a VM are usage errors.
    ch, _ = cm.plan_changes(
        vm_spec(),
        cm.Request(net=vz.Interface("bridge", "br0", "")),
        live=False,
        auto_grow_fs=True,
    )
    assert ch[0].field == "network" and ch[0].from_ == "default" and ch[0].to == "br0"
    assert ch[0].effective == "now"  # a running guest is rewired live
    old, new = ch[0].ops[0][1]
    assert (old.source, new.source, new.mac, new.model) == (
        "default",
        "br0",
        "52:54:00:aa:bb:cc",
        "virtio",
    )
    ch, _ = cm.plan_changes(
        vm_spec(), cm.Request(mac="52:54:00:00:00:09"), live=False, auto_grow_fs=True
    )
    assert [(c.field, c.to) for c in ch] == [("mac", "52:54:00:00:00:09")]
    assert ch[0].ops[0][0] == "nic-replace"
    # --net and --mac together: one rewiring, two reported fields, one op.
    ch, _ = cm.plan_changes(
        vm_spec(),
        cm.Request(net=vz.Interface("network", "lan", ""), mac="52:54:00:00:00:09"),
        live=False,
        auto_grow_fs=True,
    )
    assert [c.field for c in ch] == ["network", "mac"]
    assert [op for op, _ in ch[0].ops] == ["nic-replace"]
    assert ch[1].ops == ()
    # An unchanged network/MAC reports nothing, so a no-op is still a no-op.
    try:
        cm.plan_changes(
            vm_spec(),
            cm.Request(net=vz.Interface("network", "default", "")),
            live=False,
            auto_grow_fs=True,
        )
        raise AssertionError("expected modify-noop")
    except VirtError as e:
        assert e.code == "modify-noop", e.code
    # Add + remove in one call, and a shut-off guest changes on-boot.
    two = vm_spec(
        interfaces=(
            vz.Interface("network", "default", "52:54:00:aa:bb:cc"),
            vz.Interface("network", "mgmt", "52:54:00:aa:bb:dd"),
        )
    )
    ch, _ = cm.plan_changes(
        two,
        cm.Request(
            remove=("52:54:00:aa:bb:dd",), add=(vz.Interface("bridge", "br1", ""),)
        ),
        live=False,
        auto_grow_fs=True,
    )
    assert [(c.field, c.to) for c in ch] == [
        ("interface", None),
        ("interface", {"type": "bridge", "source": "br1", "mac": ""}),
    ]
    assert ch[1].to["mac"] == ""
    ch, _ = cm.plan_changes(
        vm_spec(state="shut off"),
        cm.Request(net=vz.Interface("bridge", "br0", "")),
        live=False,
        auto_grow_fs=True,
    )
    assert ch[0].effective == "on-boot"
    for bad_req, needle in (
        (cm.Request(remove=("52:54:00:aa:bb:cc",)), "last interface"),
        (cm.Request(remove=("52:54:00:ff:ff:ff",)), "no interface with MAC"),
        (
            cm.Request(add=(vz.Interface("network", "default", "52:54:00:aa:bb:cc"),)),
            "already exists",
        ),
    ):
        try:
            cm.plan_changes(vm_spec(), bad_req, live=False, auto_grow_fs=True)
            raise AssertionError(f"expected UsageError: {needle}")
        except click.UsageError as e:
            assert needle in str(e), str(e)

    # apply_changes — a live refusal degrades to the config with a warning and
    # effective on-boot; a failed NIC change is fatal, never silent.
    fake = FakeVz()
    ctx = cm.ApplyContext(
        v=fake,
        spec=vm_spec(),
        volume="t1_vda.qcow2",
        live=True,
        user="ci",
        identity="/id",
        ip="192.168.122.11",
    )
    changes, _ = cm.plan_changes(
        vm_spec(),
        cm.Request(vcpu=3, ram_gib=4, disk_gib=16),
        live=True,
        auto_grow_fs=True,
    )
    cm.apply_changes(ctx, changes)
    assert [c.effective for c in changes] == ["now", "on-boot", "now"], changes
    assert changes[1].to == 4 and fake.ram_calls == [(4, True)]  # asked live
    assert any("RAM change applies on next boot" in w for w in ctx.warnings), (
        ctx.warnings
    )
    assert changes[0].extra == {}  # only a disk change reports fs_grown
    assert changes[2].extra["fs_grown"] is True
    assert fake.grown == [("ci", "192.168.122.11", "vda")]

    # A powered-off guest: the volume grows, the filesystem does not, and the
    # warning says how to finish the job.
    off = FakeVz(running=False)
    ctx_off = cm.ApplyContext(
        v=off,
        spec=vm_spec(state="shut off"),
        volume="t1_vda.qcow2",
        live=True,
        user="ci",
        identity="/id",
        ip=None,
    )
    changes, _ = cm.plan_changes(
        vm_spec(state="shut off"), cm.Request(disk_gib=16), live=True, auto_grow_fs=True
    )
    cm.apply_changes(ctx_off, changes)
    assert off.disks == [("t1", "vda", "t1_vda.qcow2", 16)]
    assert ctx_off.warnings and "boot it" in ctx_off.warnings[0], ctx_off.warnings
    assert changes[0].extra["fs_grown"] is None

    # A guest whose tools cannot grow: warning with the guest's own words.
    stubborn = FakeVz(fs_code=1, fs_out="growpart is not installed")
    ctx_stub = cm.ApplyContext(
        v=stubborn,
        spec=vm_spec(),
        volume="t1_vda.qcow2",
        live=False,
        user="ci",
        identity="/id",
        ip="192.168.122.11",
    )
    changes, _ = cm.plan_changes(
        vm_spec(), cm.Request(disk_gib=16), live=False, auto_grow_fs=True
    )
    cm.apply_changes(ctx_stub, changes)
    assert changes[0].extra["fs_grown"] is False
    assert any("growpart is not installed" in w for w in ctx_stub.warnings), (
        ctx_stub.warnings
    )

    # Networking failures surface as net-change-failed with libvirt's reason.
    broken = FakeVz(net_error="target device name was not found")
    ctx_net = cm.ApplyContext(
        v=broken,
        spec=vm_spec(),
        volume=None,
        live=True,
        user="ci",
        identity="/id",
        ip=None,
    )
    changes, _ = cm.plan_changes(
        vm_spec(),
        cm.Request(net=vz.Interface("bridge", "nope", "")),
        live=True,
        auto_grow_fs=True,
    )
    try:
        cm.apply_changes(ctx_net, changes)
        raise AssertionError("expected net-change-failed")
    except VirtError as e:
        assert e.code == "net-change-failed", e.code
        assert "target device name was not found" in str(e), str(e)

    # The command itself, against the fake: one JSON document, the documented
    # codes, and nothing executed under --plan.
    fake = FakeVz()
    code, body = modify_cli(fake, ["t1", "--vcpu", "3", "--plan", "--json"])
    assert code == 0 and body["status"] == "success", body
    assert body["tool"] == "vm-modify" and body["error"] is None
    assert body["planned"] is True and body["changes"][0]["field"] == "vcpu"
    assert fake.vcpu_calls == [] and body["rebooted"] is False
    assert body["vm"]["name"] == "t1" and body["vm"]["state"] == "running"
    assert body["vm"]["ram_gib"] == 2 and body["vm"]["disk_gib"] == 8
    # `ip` is the guest's current address (state, not change); without
    # `--live` a running guest only picks the new count up on its next boot.
    assert body["ip"] == "192.168.122.11", body
    assert body["changes"][0]["effective"] == "on-boot"
    assert body["warnings"] == ["vCPU change applies on next boot — consider --reboot"]

    code, body = modify_cli(fake, ["t1", "--vcpu", "3", "--live", "--json"])
    assert code == 0, body
    assert fake.vcpu_calls == [(3, True, 4)]
    assert body["changes"][0] == {
        "field": "vcpu",
        "from": 2,
        "to": 3,
        "effective": "now",
    }

    # Identity of the target comes first: nothing else is worth trying after.
    assert modify_cli(FakeVz(), ["ghost", "--vcpu", "3", "--json"])[1]["error"][
        "code"
    ] == ("vm-not-found")
    code, body = modify_cli(FakeVz(), ["foreign", "--ram", "4", "--json"])
    assert code == 1 and body["error"]["code"] == "not-a-virt-runner-vm", body
    assert modify_cli(FakeVz(), ["t1", "--disk", "4", "--json"])[1]["error"][
        "code"
    ] == ("disk-shrink-unsupported")
    assert (
        modify_cli(FakeVz(), ["t1", "--net-remove", "52:54:00:aa:bb:cc", "--json"])[1][
            "error"
        ]["code"]
        == "usage"
    )
    code, body = modify_cli(FakeVz(), ["t1", "--vcpu", "2", "--json"])
    assert code == 2 and body["error"]["code"] == "modify-noop", body
    assert (
        modify_cli(FakeVz(), ["t1", "--mac", "nope", "--json"])[1]["error"]["code"]
        == "usage"
    )
    assert modify_cli(FakeVz(), ["t1", "--json"])[1]["error"]["code"] == "usage"
    assert (
        modify_cli(FakeVz(), ["t1", "--wait", "--json"])[1]["error"]["code"] == "usage"
    )
    assert (
        modify_cli(FakeVz(), ["t1", "--vcpu", "0", "--json"])[1]["error"]["code"]
        == "usage"
    )

    # --net with a name that is not a virtual network is retried as a bridge;
    # --reboot --wait reports the fresh IP.
    fake = FakeVz()
    code, body = modify_cli(
        fake, ["t1", "--net", "br-lan", "--reboot", "--wait", "--json"]
    )
    assert code == 0, body
    assert ("detach", "network", "default", "52:54:00:aa:bb:cc", True) in fake.ifaces
    assert ("attach", "bridge", "br-lan", "52:54:00:aa:bb:cc", True) in fake.ifaces
    assert body["rebooted"] is True and body["ip"] == "192.168.122.99"
    assert [c["field"] for c in body["changes"]] == ["network"]

    # A shut-off guest: config-only, disk grows through the volume, and the
    # warning tells the operator how to finish the filesystem.
    off = FakeVz(running=False)
    code, body = modify_cli(off, ["t1", "--disk", "16", "--ram", "4", "--json"])
    assert code == 0, body
    assert off.disks == [("t1", "vda", "t1_vda.qcow2", 16)]
    assert {c["field"]: c["effective"] for c in body["changes"]} == {
        "ram_gib": "on-boot",
        "disk_gib": "now",
    }
    assert body["changes"][1]["fs_grown"] is None
    assert any("is shut off" in w for w in body["warnings"]), body["warnings"]

    # `--no-auto-grow-fs` really turns the guest-side grow off (a negative flag
    # is easy to wire to its own default): the volume/block device still grows,
    # no SSH runs, `fs_grown` stays null and the warning carries the manual
    # commands.
    fake = FakeVz()
    code, body = modify_cli(fake, ["t1", "--disk", "16", "--no-auto-grow-fs", "--json"])
    assert code == 0, body
    assert fake.disks == [("t1", "vda", "t1_vda.qcow2", 16)], fake.disks
    assert fake.grown == [], fake.grown
    assert body["changes"][0]["fs_grown"] is None, body["changes"]
    assert any("--no-auto-grow-fs" in w for w in body["warnings"]), body["warnings"]
    # Without the flag the same command does SSH into the guest to grow the fs.
    fake = FakeVz()
    code, body = modify_cli(fake, ["t1", "--disk", "16", "--json"])
    assert code == 0, body
    assert fake.grown == [("ci", "192.168.122.11", "vda")], fake.grown
    assert body["changes"][0]["fs_grown"] is True, body["changes"]

    # Text mode stays readable and says nothing about JSON.
    from click.testing import CliRunner as _Runner

    from virt_runner.cli import main as _cli

    real = cm.Virtualizer
    cm.Virtualizer = FakeVz
    try:
        res = _Runner().invoke(_cli, ["modify", "t1", "--vcpu", "3", "--live"])
    finally:
        cm.Virtualizer = real
    assert res.exit_code == 0, res.output
    assert "Modified: t1 (running)" in res.output and "vcpu: 2 -> 3 (now)" in res.output
    assert "{" not in res.output

    # Usage errors raised inside the command body still honour --json (exit 2).
    from click.testing import CliRunner

    from virt_runner.cli import main as cli_main
    from virt_runner.output import set_json_mode

    for argv in (
        ["create", "--no-boot", "--script", str(sdir / "c.sh"), "x", "--json"],
        ["create", "--distro", "arch", "--release", "99", "x", "--json"],
    ):
        res = CliRunner().invoke(cli_main, argv)
        assert res.exit_code == 2, (argv, res.output)
        assert json.loads(res.stdout)["error"]["code"] == "usage", res.output
    set_json_mode(False)

    # --version reports the installed package version.
    from importlib.metadata import version

    res = CliRunner().invoke(cli_main, ["--version"])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == f"virt-runner, version {version('virt-runner')}"

    # Short aliases (c/d/ls/s) dispatch to the canonical command, help still
    # lists each command once, and a non-alias name is still a usage error.
    from virt_runner.cli import ALIASES

    assert ALIASES == {
        "c": "create",
        "d": "destroy",
        "ls": "list",
        "s": "ssh",
        "m": "modify",
    }
    for alias, canonical in ALIASES.items():
        assert cli_main.get_command(None, alias) is cli_main.get_command(
            None, canonical
        ), alias
    res = CliRunner().invoke(cli_main, ["ls", "--help"])
    assert res.exit_code == 0 and "User name shown" in res.output, res.output
    assert res.output.count("Options:") == 1
    for bad in ("l", "lst", "creat"):
        res = CliRunner().invoke(cli_main, [bad])
        assert res.exit_code == 2, (bad, res.output)

    # `setup` runs the bundled host installer. In a checkout the path resolves to
    # the repository root; in a wheel it resolves inside the package. Either way
    # it must be the real script, and the command must never execute it here.
    from virt_runner.cmd_setup import SCRIPT_NAME, prerequisite_script

    script = prerequisite_script()
    assert script is not None and script.is_file(), "installer script not found"
    assert script.name == SCRIPT_NAME
    assert "--check" in script.read_text(), "wrong installer script resolved"

    res = CliRunner().invoke(cli_main, ["setup", "--help"])
    assert res.exit_code == 0, res.output
    assert "--with-virt-manager" in res.output
    # Registered as a command, but deliberately not a VM command and not aliased.
    assert cli_main.get_command(None, "setup") is not None
    assert "setup" not in ALIASES

    print("unit checks ok")


if __name__ == "__main__":
    main()
