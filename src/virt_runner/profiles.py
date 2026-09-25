"""Distro profiles — per-distro image/user/boot data for ``create``.

Data-driven profiles per the generalization spec
(``specification/features/generalize-vm-creation-specification.md``); three
profiles: ubuntu, arch, fedora. Pipeline code (``cmd_create`` / ``virtualizer``)
stays free of distro-name conditionals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class DistroProfile:
    """One guest distro's image, user and boot parameters."""

    name: str
    default_release: str
    suggested_user: str
    sudo_group: str
    os_variant: str
    image_url: Callable[[str], str]  # release -> full image URL
    sums_kind: str  # "dir" (same-dir SHA256SUMS) | "sidecar" (<url>.SHA256)
    cache_prefix: str = ""  # extra cache dir under the cache root
    releases: tuple[str, ...] | None = None  # None = any; else allowed set


PROFILES: dict[str, DistroProfile] = {
    "ubuntu": DistroProfile(
        name="ubuntu",
        default_release="resolute",
        suggested_user="ubuntu",
        sudo_group="sudo",
        os_variant="ubuntu-lts-latest",
        image_url=lambda r: (
            f"https://cloud-images.ubuntu.com/{r}/current/{r}-server-cloudimg-amd64.img"
        ),
        sums_kind="dir",
    ),
    "arch": DistroProfile(
        name="arch",
        default_release="latest",
        suggested_user="arch",
        sudo_group="wheel",
        # osinfo-db has no archlinux distro id; `generic` carries the same
        # virtio/x86_64 defaults the image is built for.
        os_variant="generic",
        # Official arch-boxes cloud image (cloud-init/NoCloud preinstalled),
        # x86_64 only; `latest` is the stable mirror alias.
        image_url=lambda r: (
            "https://fastly.mirror.pkgbuild.com/images/latest/"
            "Arch-Linux-x86_64-cloudimg.qcow2"
        ),
        sums_kind="sidecar",
        cache_prefix="arch",
        releases=("latest",),
    ),
    "fedora": DistroProfile(
        name="fedora",
        default_release="44",
        suggested_user="fedora",
        sudo_group="wheel",
        # osinfo-db has fedora42 but NOT fedora43/44; `generic` carries
        # the same virtio/x86_64 defaults the image is built for.
        os_variant="generic",
        # Fedora 43+ cloud image layout: Cloud/x86_64/images/Fedora-Cloud-Base-*.
        # The URL contains a glob; the block-format checksum parser resolves it.
        image_url=lambda r: (
            "https://download.fedoraproject.org/pub/fedora/linux/releases/"
            f"{r}/Cloud/x86_64/images/"
            f"Fedora-Cloud-Base-Generic-{r}-*.x86_64.qcow2"
        ),
        # Fedora publishes PGP-signed CHECKSUM files:
        # Fedora-Cloud-{release}-{point}-x86_64-CHECKSUM
        # Format: "SHA256 (<filename>) = <hash>" inside a PGP wrapper.
        sums_kind="block",
        cache_prefix="fedora",
        releases=None,  # any numeric release works via the URL template
    ),
}
