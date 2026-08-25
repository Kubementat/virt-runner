# Ticket 02 — S2: Image download + SHA256 verification + cache reuse

**Slice:** S2 (plan §2)
**Goal:** Extend `bin/vm-create` (committed by ticket 01) with `fetch_and_verify_image()`: download a cloud image into `~/vm-images/<release>/`, verify it against `SHA256SUMS`, never leave partial/corrupt files in the cache, skip the download entirely when the cache is warm (acceptance criterion 2's mechanism), support the `--keep-going` fallback and D1's custom-image / `--release` URL rules. The stub after the download step now reports the resolved image path, then exits 1 (cloud-init is still not implemented).

## Context

- **Spec sections:** §4 step 2 (cache layout, download/verify algorithm, URL patterns), §5 (verified resolute URLs and the fact that `SHA256SUMS` entries use the *final* image name), §7 AC2 (one download per release), §6 pitfalls (see below), decisions **D1** (release/image precedence; bare `--image` caches under `custom/`, verifies only if a same-directory `SHA256SUMS` is derivable, else **skip verification and warn**), **D4** (download to the final name and verify in place; on failure remove the file).
- **Plan sections:** §1 ground rules (testability hook `VM_CREATE_CACHE_DIR` — relocate the cache root for tests; default stays `~/vm-images`), §2 slice S2, §3 (commit message), §4 safety rules, §6 (risk row: SHA256SUMS filename matching; `file://` support; no stray `.part`/corrupt file may survive a failure; **S2 must not trigger the 820 MiB resolute download — fixture files only**).
- **Environment facts:** `curl` and `sha256sum` are at `/usr/bin/`; `~/vm-images` does not exist yet (first real resolute download happens only in ticket 07); 227 GiB free on `/`. The default resolute URLs are:
  - image: `https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img`
  - sums: `https://cloud-images.ubuntu.com/resolute/current/SHA256SUMS`
  - pattern for `--release <rel>`: `https://cloud-images.ubuntu.com/<rel>/current/<rel>-server-cloudimg-amd64.img` (+ `/SHA256SUMS`).

## Deliverables

- **Modify** `bin/vm-create`:
  - New function `fetch_and_verify_image()` implementing spec §4 step 2 + D1 + D4.
  - Cache layout (root overridable by `VM_CREATE_CACHE_DIR`, default `$HOME/vm-images`):
    - `--release` path (or default release): `<cache>/<release>/{<release>-server-cloudimg-amd64.img, SHA256SUMS}`
    - bare `--image URL` path (no `--release`): image cached under `<cache>/custom/` using the URL's basename; sums fetched only if a same-directory `SHA256SUMS` is derivable (for `file://` and for the standard https pattern), otherwise verification is **skipped with a printed warning** (D1).
  - The stub after the download step now prints the resolved image path (e.g. `image ready: <path>`), then keeps exiting 1 with the not-implemented message (cloud-init arrives in ticket 03).

## Implementation requirements

1. **Download (D4):** if the final image file is **absent**:
   1. `curl -fL --retry 3 -o <img> <IMAGE_URL>` — download directly to the **final name** in the cache dir (the sums file lists the final name; a `.part` name would not match).
   2. `curl -fsL <SUMS_URL> -o <cache-dir>/SHA256SUMS` (skip for the no-derivable-sums `--image` case, per D1).
   3. Verify: `(cd <cache-dir> && grep -F "$(basename <img>)" SHA256SUMS | sha256sum -c -)`.
   4. On any curl failure or mismatch: `rm -f` the (partial or corrupt) image file, then — if `--keep-going` is set **and** a previously cached image exists at the final path, print a warning and continue using the cache — otherwise exit 1 with a clear one-line error (`SHA256 mismatch for <img>` / `download failed: <url>`).
2. **Cache hit:** if the final image file is **present**, skip download and sums fetch entirely. This must be observable: a second run does zero network activity for the image.
3. **`--keep-going` semantics exactly as spec §3.1:** on download/verification error → continue iff a cached image exists, else fail with a clear error. Without `--keep-going` the same error is fatal (exit 1).
4. **`file://` URLs must work end-to-end** in this ticket (tickets 04–06 depend on it): both image copy and same-directory `SHA256SUMS` fetch must work for `file://` paths (curl handles `file://`; if you find it unreliable, a local `cp` fast-path for `file://` is acceptable — record which branch you implemented).
5. Never leave a `.part`, truncated, or corrupt file in the cache on any failure path (spec §4 step 2; plan S2 risk row).
6. All test fixtures live under `/tmp/virt-test-s2/` and are removed at the end of the slice.
7. No behavior changes to ticket 01's parsing/preflight; keep `set -euo pipefail` and the exit-code contract (0/1/2).

## Safety rules

- Any VM name used in tests: `poc-` prefix only (this ticket may not create VMs at all — no virt-install yet).
- Every fixture file/dir created in `/tmp/virt-test-s2/` must be deleted (`rm -rf /tmp/virt-test-s2`) before finishing.
- **Do not perform the real 820 MiB resolute download** — fixtures only.
- Never touch `setup-test-vm`, the pre-existing `noble-server-cloudimg-amd64.img` volume, or anything under `/var/lib/libvirt/` or `~/vm-images` (use `VM_CREATE_CACHE_DIR` to keep tests in `/tmp`).
- No `sudo`.

## Acceptance criteria

Setup (once):

```bash
mkdir -p /tmp/virt-test-s2
dd if=/dev/zero of=/tmp/virt-test-s2/demo-server-cloudimg-amd64.img bs=1024 count=1
cd /tmp/virt-test-s2 && sha256sum demo-server-cloudimg-amd64.img > SHA256SUMS
```

1. **Download + verify path:** `VM_CREATE_CACHE_DIR=/tmp/virt-test-s2/cache bin/vm-create poc-s2 --ssh-key ~/.ssh/id_rsa.pub --image file:///tmp/virt-test-s2/demo-server-cloudimg-amd64.img` → image lands at `/tmp/virt-test-s2/cache/custom/demo-server-cloudimg-amd64.img` together with `SHA256SUMS`; the script prints `image ready: <path>` then the stub exit-1 message; **exit 1** (stub), and the download+verify branch clearly ran (record which branch: `file://` copy vs curl).
2. **Corruption rejection:** corrupt the fixture (e.g. `truncate -s 512 /tmp/virt-test-s2/demo-server-cloudimg-amd64.img`, or edit `SHA256SUMS` to a wrong hash) and remove the cached copy → re-run → **exit 1**, `SHA256 mismatch`-style error, and `ls /tmp/virt-test-s2/cache/custom/` shows **no** corrupt/partial image file remains (restore the fixture for the next test).
3. **Cache reuse:** with a valid cache present (criterion 1's output), delete the *source* fixture (`rm /tmp/virt-test-s2/demo-server-cloudimg-amd64.img`); re-run the identical command → it still succeeds to the stub exit 1 **from cache with no download** (proving the presence-check skip; if the source were still required, the run would have failed).
4. **`--keep-going` with warm cache:** point `--image` at a dead URL (e.g. `file:///tmp/virt-test-s2/does-not-exist.img`) **with** a valid image already cached at the final path **and** `--keep-going` → warning printed, run continues to the stub exit 1 using the cached image.
5. **`--keep-going` with cold cache:** same dead URL, empty cache dir, `--keep-going` → **exit 1**, clear error (no cached image to continue with).
6. **`~/vm-images` untouched:** after all tests, `ls ~/vm-images 2>/dev/null || echo absent` → absent (or unchanged from before the ticket).

## Verification (run in this order)

1. Create the fixture exactly as in the "Setup" block above.
2. Run acceptance criteria 1 → 5 in order, recording each exit code and the relevant message; after criterion 2 run `ls -la /tmp/virt-test-s2/cache/custom/` and confirm no corrupt file survives.
3. For criterion 3, capture the output and confirm no download/verify lines appear (cache-skip path).
4. Run criterion 6 (`~/vm-images` untouched).
5. Teardown: `rm -rf /tmp/virt-test-s2` and confirm it is gone.
6. Confirm `git status --short` shows only `bin/vm-create` modified (and `.gitignore` if you added entries).
7. **No real VM creation is required for this ticket.**
8. If all pass: `git add -A && git commit -m "feat(vm-create): add image download, sha256 verification, and cache reuse (ticket 02)"`

## Definition of done

- All acceptance criteria pass; verification sequence ran successfully; `/tmp/virt-test-s2` removed; `~/vm-images` uncreated/unchanged.
- Working tree committed with exactly: `git add -A && git commit -m "feat(vm-create): add image download, sha256 verification, and cache reuse (ticket 02)"`

## Pitfalls (spec §6 / plan §2, S2-relevant)

- **D4 filename subtlety:** `SHA256SUMS` entries name the *final* image file — verifying a `.part` file would always fail. Download to the final name and verify in place (or rename before verify); never build a flow where the name can mismatch.
- **No orphan artifacts:** a failed run must leave neither `.part` files nor a truncated final file in the cache (plan S2 key risk).
- **`file://` sums derivation:** the same-directory `SHA256SUMS` fetch must work for `file://` (or you must take the D1 warn-and-skip branch and *record* that it ran) — ticket 05's Noble `file://` boot relies on the documented warn path.
- **820 MiB discipline:** S2 is fixture-only; accidentally running the default (resolute) download here wastes bandwidth and blurs ticket 07's "first real download" evidence.
- `set -e` vs `grep -F` (a non-matching `grep` exits non-zero): guard the verify pipeline so "not in sums" is reported as a clear mismatch error, not a bare shell abort.
