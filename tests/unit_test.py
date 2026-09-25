"""Pure-logic checks (no libvirt, no network). Run: uv run tests/unit_test.py"""

from virt_runner.profiles import PROFILES


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

    print("unit checks ok")


if __name__ == "__main__":
    main()
