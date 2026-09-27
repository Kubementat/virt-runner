"""Pure-logic checks (no libvirt, no network). Run: uv run tests/unit_test.py"""

import json

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

    print("unit checks ok")


if __name__ == "__main__":
    main()
