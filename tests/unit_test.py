"""Pure-logic checks (no libvirt, no network). Run: uv run tests/unit_test.py"""

import virt_runner.virtualizer as vz
from virt_runner import images
from virt_runner.cmd_create import _validate_mounts, _validate_user
from virt_runner.errors import VirtError
from virt_runner.images import _newest_match, _resolve_glob, _sums_entries
from virt_runner.profiles import PROFILES, _same_dir
from virt_runner.virtualizer import Virtualizer, render_meta_data, render_user_data


def main() -> None:
    # 1.1 — Fedora sums_url resolves the compose name, not the image name.
    fed = PROFILES["fedora"]
    img = (
        "https://example/pub/fedora/linux/releases/44/Cloud/x86_64/images/"
        "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
    )
    assert fed.sums_url(img).endswith("/Fedora-Cloud-44-1.7-x86_64-CHECKSUM"), (
        fed.sums_url(img)
    )

    # 1.2 — ubuntu/arch sums_url outputs.
    ubu = PROFILES["ubuntu"]
    arch = PROFILES["arch"]
    ubu_img = "https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img"
    assert _same_dir(ubu_img, "SHA256SUMS") in ubu.sums_url(ubu_img)
    assert ubu.sums_url(ubu_img).endswith("/SHA256SUMS")
    assert arch.sums_url("https://example/arch.img").endswith(".SHA256")

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
        fetched = images.fetch_and_verify_image(fed, "44", False, gurl, False, False)
        assert fetched.path.endswith("44-1.7.x86_64.qcow2"), fetched.path
        assert downloads == [dead, live], downloads
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

    # 1.8 — preflight refuses non-x86_64 hosts.
    v = Virtualizer()
    v.host_arch = lambda: "aarch64"
    try:
        v.preflight_check()
        raise AssertionError("expected VirtError")
    except VirtError as e:
        assert e.code == "unsupported-arch", e.code

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

    # create_vm — mounts add shared memory + one --filesystem per mount.
    captured = {}
    real_pt = vz._passthrough
    try:
        vz._passthrough = lambda cmd: (
            captured.setdefault("cmd", cmd),
            subprocess.CompletedProcess(cmd, 0),
        )[1]
        Virtualizer().create_vm(
            "n",
            1024,
            1,
            "vol",
            "52:54:00:00:00:01",
            "u",
            "m",
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

    # require_virtiofsd — missing binary is virtiofsd-missing.
    v2 = Virtualizer()
    v2.VIRTIOFSD = "/nonexistent/virtiofsd"
    try:
        v2.require_virtiofsd()
        raise AssertionError("expected VirtError")
    except VirtError as e:
        assert e.code == "virtiofsd-missing", e.code

    print("unit checks ok")


if __name__ == "__main__":
    main()
