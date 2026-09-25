# 006 — Fedora image downloads can 404 due to mirror index inconsistency

Date: 2026-07-08 · Found while merging `feature/mount-directories`

## Symptom

`virt-runner create --distro fedora` intermittently fails with
`image-download-failed` (stage `image`): the resolved URL
`.../Fedora-Cloud-Base-Generic-44-20260424.n.0.x86_64.qcow2` returns HTTP 404,
even though the same directory listing shows it. Retries against different
mirrors succeed with `Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2`.

## Root cause

`download.fedoraproject.org` is a geo/round-robin redirector to public
mirrors. During the Fedora 44 → 44-1.7 point release some mirrors kept a
stale autoindex that still lists the old dated build while the file itself
is gone (404 on the mirror that served the download).
`images._resolve_glob` fetches the listing from one mirror and
`_newest_match` then prefers the dated name (`20260424` > `1` in natural
sort), which can 404 on the mirror that serves the actual download. The
listing content also differed between consecutive fetches (one mirror
returned an index with no matching files at all).

## Fix (fix/fedora-glob-fallback)

`images.py` now resolves the glob to an ordered candidate list
(`_glob_candidates`, newest first) and re-tries the next-newest name when a
candidate's download fails. Verification failures (SHA mismatch) do NOT
fall back — only `image-download-failed` does. Covered by a unit test in
`tests/unit_test.py` (dead newest 404 → next-newest succeeds).

Unfixed (acceptable): the listing is fetched from whichever mirror
`download.fedoraproject.org` redirects to, so a *consistent* mirror that
lists only dead files would still fail — in practice mirrors diverge, so
at least one candidate usually downloads. A warm local cache
(`~/vm-images/fedora/<release>/`) also bypasses the download entirely.

Not a virt-runner regression: the identical failure occurred without
`--mount` on a plain `develop` baseline.
