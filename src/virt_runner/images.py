"""Cloud-image download + verification (moved out of :mod:`virt_runner.virtualizer`).

Everything that fetches a guest image or its checksum file lives here;
``cmd_create`` calls :func:`fetch_and_verify_image` directly.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from virt_runner import output
from virt_runner.errors import VirtError
from virt_runner.profiles import DistroProfile, _same_dir
from virt_runner.virtualizer import _spawn

#: Sent on every image/sums fetch; identifies the tool without leaking anything else.
USER_AGENT = f"virt-runner/{output.version()}"


@dataclass(frozen=True)
class ImageFetch:
    """Outcome of :func:`fetch_and_verify_image`.

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


_SUMS_LINE = re.compile(
    r"^(?:SHA256\s+\((?P<bn>.+?)\)\s+=\s+(?P<bh>[0-9a-fA-F]{64})"
    r"|(?P<h>[0-9a-fA-F]{64})\s+\*?(?P<n>\S.*?))\s*$"
)


def _newest_match(names: list[str], pattern: str) -> str | None:
    """Newest (natural-sort) name matching *pattern*: '44-1.10' > '44-1.7'."""

    def _key(s: str) -> list[int | str]:
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]

    hits = [n for n in set(names) if fnmatch.fnmatch(n, pattern)]
    return max(hits, key=_key) if hits else None


def _resolve_glob(url: str, cache_dir: Path | None = None) -> str:
    """Resolve a URL containing ``*`` by listing the directory.

    Returns a concrete URL when *url* contains ``*``; otherwise returns
    *url* unchanged.  The listing is fetched via ``curl -s -L`` and
    ``.qcow2`` filenames matching the glob are extracted from the HTML.
    """
    if "*" not in url:
        return url
    dir_url = url.rsplit("/", 1)[0] + "/"
    pattern = url.rsplit("/", 1)[-1]
    # Fetch directory listing and extract matching .qcow2 filenames.
    result = _spawn(["curl", "-s", "-L", "-A", USER_AGENT, dir_url])
    if result.returncode != 0:
        # Offline: try the cache.
        if cache_dir is not None:
            base = url.rsplit("/", 1)[-1].rsplit("*", 1)[0]
            candidate = _newest_match(os.listdir(cache_dir), base + "*.qcow2")
            if candidate:
                return dir_url + candidate
        return url  # fall through: download will fail with a clear error
    # Extract .qcow2 filenames from the HTML listing.
    filenames = re.findall(r'href="([^"]+\.qcow2)"', result.stdout)
    # Filter to entries matching the glob pattern.
    for name in filenames:
        if fnmatch.fnmatch(name, pattern):
            return dir_url + name
    # No match found and not offline — raise to avoid literal * in paths.
    if cache_dir is None:
        raise VirtError(
            f"no image matching {pattern} at {dir_url}",
            "image-download-failed",
            "image",
        )
    candidate = _newest_match(os.listdir(cache_dir), pattern)
    if candidate:
        return dir_url + candidate
    raise VirtError(
        f"no image matching {pattern} at {dir_url}", "image-download-failed", "image"
    )


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
        request = urllib.request.Request(sums_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            dest.write_bytes(response.read())
    except (OSError, ValueError):
        # URLError/HTTPError/socket errors are OSErrors; a malformed URL
        # is a ValueError. All of them mean "no sums derivable".
        return False
    return True


def _sums_entries(sums_text: str) -> list[tuple[str, str]]:
    """``(filename, sha256)`` pairs from sha256sum *or* BSD/Fedora ``SHA256 (f) = h`` lines."""
    entries = []
    for line in sums_text.splitlines():
        m = _SUMS_LINE.match(line.strip())
        if m:
            entries.append(((m["bn"] or m["n"]), (m["bh"] or m["h"]).lower()))
    return entries


def _sha256_of(path: Path) -> str:
    """Streamed sha256 hex digest of *path*."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_and_verify_image(
    profile: DistroProfile,
    release: str,
    release_given: bool,
    image_url: str,
    image_given: bool,
    keep_going: bool,
) -> ImageFetch:
    """Download + verify cloud image.

    Args:
        profile: the distro profile (image URL, sums URL, cache dir).
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
        # Standard release path (image_url is the profile's release URL).
        verifiable = True
        if profile.cache_prefix:
            cache_dir = cache_dir / profile.cache_prefix / release
        else:
            cache_dir = cache_dir / release
    else:
        # Bare --image path.
        scheme = image_url.split("://")[0] if "://" in image_url else ""
        verifiable = scheme in ("file", "http", "https")
        cache_dir = (
            cache_dir / "custom" / hashlib.sha256(image_url.encode()).hexdigest()[:12]
        )

    # Resolve any glob in the image URL (Fedora uses rotating filenames).
    image_url = _resolve_glob(image_url, cache_dir)

    # Re-extract basename after glob resolution.
    img_basename = image_url.rsplit("/", 1)[-1]

    # Compute sums_url from the resolved image_url.
    if release_given or not image_given:
        sums_url = profile.sums_url(image_url)
    else:
        sums_url = _same_dir(image_url, "SHA256SUMS")

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
                f"vm-create: warning: reusing cached image (no download): {img_path}"
            )
        output.progress(f"image ready: {img_path}")
        # A cached file is a previously verified artifact (only a verified
        # download writes it), so the verification mode is reported rather
        # than re-run here (PI-14).
        marker = img_path.with_name(img_path.name + ".verified")
        verification = "sha256-sums" if marker.exists() else "skipped"
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
        # curl with retries (matches bash: curl -fL --retry 3). Stall
        # detection replaces a hard timeout: only a transfer that stays
        # under 10 KB/s for 60 s aborts. The partial file is removed below.
        result = _spawn(
            [
                "curl",
                "-fL",
                "--retry",
                "3",
                "--speed-limit",
                "10240",
                "--speed-time",
                "60",
                "-A",
                USER_AGENT,
                "-o",
                str(img_path),
                image_url,
            ]
        )
        if result.returncode != 0:
            img_ok = False
            fail_msg = f"download failed: {image_url}"

    # Verify.
    verification = "skipped"
    if img_ok and verifiable:
        if _fetch_sums(sums_url, sums_path):
            # Verify the file against ITS OWN entry in the sums file.
            # The entry is selected by exact filename, not substring: a
            # substring match also picks up sibling entries (e.g.
            # 'foo.img' inside 'other-foo.img'). The digest is computed
            # locally, so no external sha256sum and no cwd dependency.
            # _sums_entries handles both sha256sum and Fedora PGP-block lines.
            entries = _sums_entries(sums_path.read_text(errors="replace"))
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
            elif _sha256_of(img_path) != expected[0]:
                img_ok = False
                fail_msg = f"SHA256 mismatch for {img_path}"
            else:
                # Compared successfully against its own sums entry.
                verification = "sha256-sums"
                # Write a marker so cache hits can prove verification.
                img_path.with_name(img_path.name + ".verified").write_text(
                    expected[0] + "\n"
                )
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
        marker = img_path.with_name(img_path.name + ".verified")
        img_path.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
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
