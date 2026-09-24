"""Distro profiles — per-distro image/user/boot data for ``create``.

Data-driven profiles per the generalization spec
(``specification/features/generalize-vm-creation-specification.md``); this
slice lands exactly two. Pipeline code (``cmd_create`` / ``virtualizer``)
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
}
