"""Distro profiles — per-distro image/user/boot data for ``create``.

Data-driven profiles per the generalization spec
(``specification/features/generalize-vm-creation-specification.md``); three
profiles: ubuntu, arch, fedora. Pipeline code (``cmd_create`` / ``virtualizer``)
stays free of distro-name conditionals.

Image/sums URLs are per-arch template maps (spec G16: per-arch templates, not
one template with an ``{arch}`` placeholder): a distro without an image for an
arch simply has no entry, and looking it up fails with a clear error.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from virt_runner.errors import VirtError


def _same_dir(url: str, name: str) -> str:
    """Return ``url``'s directory concatenated with *name*."""
    return url.rsplit("/", 1)[0] + "/" + name


def _fedora_sums_url(url: str) -> str:
    """Fedora CHECKSUM file URL for an image URL (both arch tokens).

    Fedora publishes PGP-signed CHECKSUM files:
    Fedora-Cloud-{release}-{point}-{arch}-CHECKSUM — same naming shape for
    x86_64 and aarch64, so one regex covers both.
    """
    return _same_dir(
        url,
        re.sub(
            r"^Fedora-Cloud-Base-Generic-(.+)\.(x86_64|aarch64)\.qcow2$",
            r"Fedora-Cloud-\1-\2-CHECKSUM",
            url.rsplit("/", 1)[-1],
        ),
    )


@dataclass(frozen=True)
class DistroProfile:
    """One guest distro's per-arch images and its user/boot parameters."""

    name: str
    default_release: str
    suggested_user: str
    sudo_group: str
    os_variant: str
    image_urls: dict[str, Callable[[str], str]]  # arch -> release -> image URL
    sums_urls: dict[str, Callable[[str], str]]  # arch -> image URL -> sums URL
    cache_prefix: str = ""  # extra cache dir under the cache root
    releases: tuple[str, ...] | None = None  # None = any; else allowed set

    def _lookup(self, kind: str, arch: str) -> Callable[[str], str]:
        """Template for *arch*, or a clear error when the distro lacks it."""
        table = self.image_urls if kind == "image" else self.sums_urls
        try:
            return table[arch]
        except KeyError:
            raise VirtError(
                f"distro '{self.name}' has no {arch} image",
                "image-arch-unavailable",
            ) from None

    def image_url(self, arch: str, release: str) -> str:
        """Image URL for *arch*; ``image-arch-unavailable`` when unsupported."""
        return self._lookup("image", arch)(release)

    def sums_url(self, arch: str, url: str) -> str:
        """Checksums file URL for *arch*; same error when unsupported."""
        return self._lookup("sums", arch)(url)


PROFILES: dict[str, DistroProfile] = {
    "ubuntu": DistroProfile(
        name="ubuntu",
        default_release="resolute",
        suggested_user="ubuntu",
        sudo_group="sudo",
        os_variant="ubuntu-lts-latest",
        image_urls={
            "x86_64": lambda r: (
                f"https://cloud-images.ubuntu.com/{r}/current/"
                f"{r}-server-cloudimg-amd64.img"
            ),
            "aarch64": lambda r: (
                f"https://cloud-images.ubuntu.com/{r}/current/"
                f"{r}-server-cloudimg-arm64.img"
            ),
        },
        # The same-dir SHA256SUMS already carries one entry per arch image.
        sums_urls={
            "x86_64": lambda u: _same_dir(u, "SHA256SUMS"),
            "aarch64": lambda u: _same_dir(u, "SHA256SUMS"),
        },
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
        image_urls={
            "x86_64": lambda r: (
                "https://fastly.mirror.pkgbuild.com/images/latest/"
                "Arch-Linux-x86_64-cloudimg.qcow2"
            ),
        },
        sums_urls={"x86_64": lambda u: u + ".SHA256"},
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
        # Fedora 43+ cloud image layout: Cloud/<arch>/images/Fedora-Cloud-Base-*.
        # The URL contains a glob; the block-format checksum parser resolves it.
        image_urls={
            "x86_64": lambda r: (
                "https://download.fedoraproject.org/pub/fedora/linux/releases/"
                f"{r}/Cloud/x86_64/images/"
                f"Fedora-Cloud-Base-Generic-{r}-*.x86_64.qcow2"
            ),
            "aarch64": lambda r: (
                "https://download.fedoraproject.org/pub/fedora/linux/releases/"
                f"{r}/Cloud/aarch64/images/"
                f"Fedora-Cloud-Base-Generic-{r}-*.aarch64.qcow2"
            ),
        },
        sums_urls={"x86_64": _fedora_sums_url, "aarch64": _fedora_sums_url},
        cache_prefix="fedora",
        releases=None,  # any numeric release works via the URL template
    ),
}
