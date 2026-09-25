"""Pure-logic checks (no libvirt, no network). Run: uv run tests/unit_test.py"""

from virt_runner import virtualizer as vz
from virt_runner.profiles import PROFILES, _same_dir


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
    entries = vz.Virtualizer._sums_entries(
        "aabbccdd00112233445566778899aabbccddeeff00112233445566778899aabb  file.img\n"
    )
    assert entries == [
        ("file.img", "aabbccdd00112233445566778899aabbccddeeff00112233445566778899aabb")
    ]

    # _sums_entries — *binary marker.
    entries = vz.Virtualizer._sums_entries(
        "ddeeff00112233445566778899aabbccddeeff00112233445566778899aabbcc *binary.img\n"
    )
    assert entries == [
        (
            "binary.img",
            "ddeeff00112233445566778899aabbccddeeff00112233445566778899aabbcc",
        )
    ]

    # _sums_entries — PGP-wrapped Fedora block (header/signature ignored).
    entries = vz.Virtualizer._sums_entries(
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
    entries = vz.Virtualizer._sums_entries("not a hash\n\nmore junk\n")
    assert entries == []

    print("unit checks ok")


if __name__ == "__main__":
    main()
