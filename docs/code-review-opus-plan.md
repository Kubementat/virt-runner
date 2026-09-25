# Implementation Plan — fixes & simplifications from the Opus code review

**Source review:** [`code-review-opus.md`](code-review-opus.md), cited below as "R§x.y"
**Baseline:** `develop` @ `88967c5` (2026-09-25)
**Audience:** a later AI coding agent session. Every task stands alone: it
lists the files, the concrete edit, and how to verify it. Line numbers refer to
the baseline and drift as you go, so search for the quoted code if a number
doesn't match.

---

## 0. Ground rules (read first)

- Follow `AGENTS.md`:
  - Branch off `develop` in a git worktree, one branch per phase
    (`fix/review-p1-verification`, `refactor/review-p2-simplify`, …).
  - Use `uv` for everything.
  - Every behavior change adds or extends a check in
    `tests/integration_test.py`.
- Philosophy: **delete before adding.** Use no new dependencies; the stdlib
  (`xml.etree.ElementTree`, `re`, `time.monotonic`) covers everything. Add no
  new classes unless a task says so.
- **The JSON contract is public.** Existing `error.code` values, envelope keys
  and exit codes must not change, except where a task explicitly adds a code
  or fixes a wrong value. Adding keys is fine; renaming or removing them is not.
- After every task, run:
  ```bash
  uv run ruff check . && uv run ruff format --check .
  uv run tests/unit_test.py          # exists after task 1.1
  ```
  Run the full `uv run tests/integration_test.py` (needs KVM, takes minutes) at
  the end of each phase, at minimum.
- Commit per task with a conventional message. End commit messages with the
  attribution line required by the current session.

Phase order matters: P0 → P1 → P2 → P3. P1 fixes a security-relevant bug, so
do it before any refactoring.

---

## Phase 0 — Hygiene (≈15 min)

### 0.1 Make lint and format green
- `uv run ruff check --fix . && uv run ruff format .`
  This fixes I001 (`import fnmatch` order in `virtualizer.py:25`) and
  reformats 3 files.
- **Verify:** both commands exit 0.

### 0.2 Fix stale strings (R§6, R§3.12)
Text-only edits:

| File | Change |
|---|---|
| `src/virt_runner/cmd_list.py:77` | docstring → `"List VMs created by virt-runner (disk in the vm-pool)."` |
| `src/virt_runner/cmd_list.py:120` | `(vm-create <name> to create one)` → `(virt-runner create <name> to create one)` |
| `src/virt_runner/cmd_create.py:123` | help → `"Cloud user name [default: per-distro: ubuntu/arch/fedora]"` |
| `src/virt_runner/cmd_create.py:149` | `release: str` → `release: str \| None` |
| `src/virt_runner/profiles.py:5` | "this slice lands exactly two" → "three profiles: ubuntu, arch, fedora" |
| `src/virt_runner/output.py:12-13` | drop the hard-coded `"version": "0.2.0"` from the example, use `"…"` |
| `src/virt_runner/output.py:161` | `(create, destroy, list)` → `(create, destroy, list, ssh)` |
| `tests/integration_test.py:5-6,125` | describe 2 Ubuntu + 1 Arch + 1 Fedora; "our four VMs" |
| `install-prerequisites.sh` usage block | `./bin/install-prerequisites.sh` → `./install-prerequisites.sh` |

- **Verify:** `grep -rn "vm-create <name>\|exactly two\|bin/install" src tests install-prerequisites.sh` returns nothing.

---

## Phase 1 — Correctness & security fixes

### 1.1 Add a tiny unit-check file first (R§5)
Create `tests/unit_test.py`: plain `assert`s, no pytest, runnable with
`uv run tests/unit_test.py`, in the same style as the integration test. It
must not touch libvirt or the network. Start with the checks that pin the
bugs in 1.2–1.4 (write them to **fail** first, then fix):

```python
"""Pure-logic checks (no libvirt, no network). Run: uv run tests/unit_test.py"""

from virt_runner.profiles import PROFILES
from virt_runner import virtualizer as vz


def main() -> None:
    fed = PROFILES["fedora"]
    img = (
        "https://example/pub/fedora/linux/releases/44/Cloud/x86_64/images/"
        "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
    )
    assert fed.sums_url(img).endswith("/Fedora-Cloud-44-1.7-x86_64-CHECKSUM"), (
        fed.sums_url(img)
    )
    # … more asserts added by each task below
    print("unit checks ok")


if __name__ == "__main__":
    main()
```

Add to `AGENTS.md` under "Integration Testsuite": *"Pure-logic checks:
`uv run tests/unit_test.py` (seconds, no KVM). Add a case for every parser or
branch you touch."*

### 1.2 Fix the Fedora sums URL and put sums derivation on the profile (R§3.1, R§4.3)
**Problem:** `virtualizer.py:463-470` derives
`Fedora-Cloud-Base-Generic-44-1.7-x86_64-CHECKSUM`, which returns 404. The real
file is `Fedora-Cloud-44-1.7-x86_64-CHECKSUM`, so verification is silently
skipped (confirmed live).

**Change: `profiles.py`**
- Replace the `sums_kind: str` field with `sums_url: Callable[[str], str]`,
  which maps the *resolved* image URL to the sums URL.
- Add one module helper:
  ```python
  def _same_dir(url: str, name: str) -> str:
      return url.rsplit("/", 1)[0] + "/" + name
  ```
- Per profile:
  - ubuntu: `sums_url=lambda u: _same_dir(u, "SHA256SUMS")`
  - arch: `sums_url=lambda u: u + ".SHA256"`
  - fedora:
    ```python
    sums_url = lambda u: _same_dir(
        u,
        re.sub(
            r"^Fedora-Cloud-Base-Generic-(.+)\.x86_64\.qcow2$",
            r"Fedora-Cloud-\1-x86_64-CHECKSUM",
            u.rsplit("/", 1)[-1],
        ),
    )
    ```
- Keep the comment explaining Fedora's CHECKSUM naming (spec §5.3).

**Change: one sums parser in `virtualizer.py`.** Delete `_sums_block` and the
`if profile.sums_kind == "block"` dispatch (`:552-559`). Extend
`_sums_entries` so each line matches **either** format:

```python
_SUMS_LINE = re.compile(
    r"^(?:SHA256\s+\((?P<bn>.+?)\)\s+=\s+(?P<bh>[0-9a-fA-F]{64})"
    r"|(?P<h>[0-9a-fA-F]{64})\s+\*?(?P<n>\S.*?))\s*$"
)


def _sums_entries(text: str) -> list[tuple[str, str]]:
    """``(filename, sha256)`` pairs from sha256sum *or* BSD/Fedora ``SHA256 (f) = h`` lines."""
    out = []
    for line in text.splitlines():
        m = _SUMS_LINE.match(line.strip())
        if m:
            out.append(((m["bn"] or m["n"]), (m["bh"] or m["h"]).lower()))
    return out
```

**Change: `fetch_and_verify_image` (`:456-482`).** Replace the three-way
`sums_kind` branch with `sums_url = profile.sums_url(image_url)`. For the
bare `--image` branch, use `_same_dir(image_url, "SHA256SUMS")` (import the
helper from profiles, or duplicate the one-liner).

**Unit checks** (add to `tests/unit_test.py`):
- the Fedora assert from 1.1
- ubuntu/arch `sums_url` outputs
- `_sums_entries` on:
  - a sha256sum line
  - a `*binary` line
  - a PGP-wrapped Fedora block, with header/signature lines ignored
  - junk lines

**Integration check:** after each `create` leg, add
`check(doc["image"]["verified"], f"{name} image verified")`. Delete
`~/vm-images/fedora/44/` before the run so Fedora really downloads and
verifies.

**Lessons learned:** add
`docs/lessons-learned/004-fedora-checksum-filename.md` (3-5 lines): Fedora's
CHECKSUM file is named after the *compose* (`Fedora-Cloud-44-1.7`), not the
image (`Fedora-Cloud-Base-Generic-44-1.7`). The skipped-verification path hid
this because `--json` drops warnings.

### 1.3 Stop claiming verification on cache hits (R§3.2)
**Change: `fetch_and_verify_image`**
- Right after a successful digest compare (`:574-576`), write a marker:
  `img_path.with_name(img_path.name + ".verified").write_text(expected[0] + "\n")`.
- On a cache hit (`:494-511`), set
  `verification = "sha256-sums" if marker.exists() else "skipped"`.
  Drop the `verifiable` shortcut there.
- On any failure path that unlinks `img_path` (`:588-589`), also
  `marker.unlink(missing_ok=True)`.
- Fix the `ImageFetch` docstring (`:151-154`): the cache can hold unverified
  images, and the marker is the proof.
- Existing caches without a marker now honestly report `skipped`. That's
  intended; mention it in the CHANGELOG.

**Unit check:** optional. The integration assert from 1.2 covers it on the
second run. If added, use a tmp dir with a `file://` image and sums via
`VM_CREATE_CACHE_DIR`. `_fetch_sums` and the `file://` copy paths make that
network-free.

### 1.4 Pick the newest glob match; fall back to cache when offline (R§3.7)
**Change: `_resolve_glob` (`virtualizer.py:670-692`)**
- Split out a pure helper and test it:
  ```python
  def _newest_match(names: list[str], pattern: str) -> str | None:
      """Newest (natural-sort) name matching *pattern*: '44-1.10' > '44-1.7'."""
      key = lambda s: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
      hits = [n for n in set(names) if fnmatch.fnmatch(n, pattern)]
      return max(hits, key=key) if hits else None
  ```
- `_resolve_glob(url, cache_dir)`: fetch the listing as today. If curl fails or
  nothing matches, try `_newest_match(os.listdir(cache_dir), pattern)` so a
  warm cache works offline. Otherwise raise
  `VirtError(f"no image matching {pattern} at {dir_url}", "image-download-failed", "image")`
  instead of letting a literal `*` flow into cache paths.
- Move the `_resolve_glob` call in `fetch_and_verify_image` to **after**
  `cache_dir` is computed. It is currently at `:453`, before the cache dir
  exists. The cache dir only depends on profile and release, not the file
  name, so this is safe.

**Unit checks:**
- `_newest_match(["…-44-1.7.x86_64.qcow2", "…-44-1.10.x86_64.qcow2"], "…-44-*.x86_64.qcow2")`
  returns the `1.10` name
- no match returns `None`

### 1.5 `destroy`: check libvirt first; correct error code (R§3.4, R§3.6)
- `cmd_destroy.py`: before `domain_exists`, add
  ```python
  try:
      v.libvirt_reachable()
  except RuntimeError as exc:
      output.fail_with("destroy", exc, "libvirt-unreachable", fields={"name": name})
  ```
- `virtualizer.destroy_vm` (`:1311-1314`): wrap the two `_run` calls:
  ```python
  try:
      ...
  except RuntimeError as exc:
      raise VirtError(
          f"failed to destroy/undefine VM '{name}'", "vm-destroy-failed"
      ) from exc
  ```
- `cmd_destroy.py:33`: keep the default `"volume-delete-failed"`. It now only
  applies to what it names.
- Add `vm-destroy-failed` to the codes table (see 3.2).
- **Verify by hand:** `virt-runner destroy x --json` with libvirt reachable
  still returns `vm-not-defined`. The unreachable case is hard to simulate,
  so a code review of the change is enough.

### 1.6 Record and use the SSH identity per VM (R§3.3)
Do this together with 2.2 (dumpxml). If you do it first, the metadata change
below stands alone.

- `set_domain_metadata(name, distro, user, identity)` (`virtualizer.py:958-976`):
  build the XML safely instead of with an f-string:
  ```python
  xml = ET.tostring(
      ET.Element("virt-runner", distro=distro, user=user, identity=identity),
      encoding="unicode",
  )
  ```
  (`import xml.etree.ElementTree as ET`). This also fixes the quoting half of
  R§3.5.
- `cmd_create.py:316`: pass `run_state["ssh_identity"]`.
- Replace `domain_user(name) -> str | None` with
  `domain_meta(name) -> dict[str, str]`. Parse the `virsh metadata` output
  with `ET.fromstring(...).attrib`, and return `{}` on empty output or a
  `ParseError`.
- `cmd_ssh.py` and `cmd_list.py`:
  - `meta = v.domain_meta(name)`
  - `user = user or meta.get("user") or DEFAULT_USER`
  - `identity = meta.get("identity") or private_key_path(DEFAULT_SSH_KEY)`

  For `list`, compute this per VM inside the loop.
- Older VMs without `identity` fall back to today's behavior.
- **Integration check:** optional (it needs a second keypair). At minimum,
  assert that `list`'s `ssh_command` for `it-vm-a` contains `-i <default private key>`.

### 1.7 Validate `--user`; quote cloud-init scalars (R§3.5)
- `cmd_create.py`: add a callback next to `_validate_name`:
  ```python
  def _validate_user(ctx, param, value):
      if value is not None and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value):
          raise click.BadParameter(
              f"invalid user name '{value}': must match [a-z_][a-z0-9_-]{{0,31}}"
          )
      return value
  ```
  Wire it with `callback=_validate_user` on `--user`. A usage error gives
  exit 2, and it's JSON-ified automatically by `JsonCommand`.
- `generate_cloud_init_files` (`virtualizer.py:744-754`): emit
  `- name: {json.dumps(user_name)}` and `- {json.dumps(ssh_key_line)}`. A JSON
  string is a valid YAML scalar, so a key comment with `#` or `: ` can no
  longer break the document.
- **Unit check:** factor the two f-string blocks into
  `render_user_data(user, group, key_line) -> str` and
  `render_meta_data(id, name) -> str`, both module-level and pure. Assert
  `yaml`-free properties: the key line appears JSON-quoted, and the output
  starts with `#cloud-config`. Also call `_validate_user` with a good and a
  bad value, expecting `click.BadParameter`.

### 1.8 Refuse non-x86_64 hosts up front (R§3.10)
- `preflight_check` (`virtualizer.py:293`), first line:
  ```python
  if self.host_arch() != "x86_64":
      raise VirtError(
          f"preflight failed: host arch {self.host_arch()} unsupported (x86_64 images only)",
          "unsupported-arch",
          "preflight",
      )
  ```
- Add `unsupported-arch` to the codes table (3.2).
- `# ponytail:` comment: remove once profiles carry per-arch URLs (spec §2.4).

### 1.9 Custom-image cache keyed by URL (R§3.9)
- `fetch_and_verify_image:482`:
  `cache_dir = cache_dir / "custom" / hashlib.sha256(image_url.encode()).hexdigest()[:12]`
- Old `custom/<basename>` entries are orphaned, which is harmless. Mention it
  in the CHANGELOG.

**Phase 1 exit:** unit and integration both green, with `image.verified`
true for all four VMs. Update `CHANGELOG.md` under *Unreleased → Fixed*.

---

## Phase 2 — Simplify (net deletion expected: ~250 lines)

### 2.1 Collapse `cmd_create`'s try/except ladder (R§4.1)
**Target shape** (replaces `cmd_create.py:195-372`):

```python
_DEFAULT_CODE = {
    "preflight": "libvirt-unreachable",
    "image": "image-download-failed",
    "cloud-init": "cloud-init-failed",
    "create": "vm-create-failed",
    "wait-ip": "lease-timeout",
    "ssh-verify": "ssh-timeout",
}

stage = "preflight"
try:
    v.preflight_check()
    v.domain_not_defined(name)
    v.ensure_ssh_key(ssh_key)

    stage = "image"
    run_state["image"] = image_fetch = v.fetch_and_verify_image(...)

    stage = "cloud-init"
    user_data, meta_data = v.generate_cloud_init_files(...)

    stage = "create"
    volume = v.provision_volume(name, disk, image_fetch.path)  # 2.1b
    run_state["mac"] = mac = v.generate_mac()
    domain_state = v.create_vm(...)
    run_state.update(
        created=True, domain_state=domain_state, uuid=v.get_domain_uuid(name)
    )
    _record_metadata(
        v, name, distro, effective_user, run_state["ssh_identity"]
    )  # warns, never raises

    if not no_boot:
        stage = "wait-ip"
        run_state["ip"] = ip = v.wait_for_ip(name, mac, lease_file("create"))
        run_state["booted"] = True
        output.progress(f"IP acquired: {ip}")
        stage = "ssh-verify"
        v.verify_ssh_reachable(
            name, effective_user, ip, identity=run_state["ssh_identity"]
        )
except RuntimeError as exc:
    output.fail_with(
        "create",
        exc,
        _DEFAULT_CODE[stage],
        stage=getattr(exc, "stage", None) or stage,
        fields=_create_document(run_state),
    )
```

Then one rendering block for success, text or JSON, including the no-boot
variant.

Notes:
- `fail_with` currently lets an explicit `stage=` override `exc.stage`
  (`output.py:221`). The call above passes the exception's own stage first,
  so behavior is preserved.
- **Behavior change (intended, R§3.12):** preflight failures now include
  `"booted": false` in the JSON document like every other stage. This only
  adds a key. Note it in the CHANGELOG.
- `generate_cloud_init_files` returns 2 values now (drop the unused
  `tmp_dir`).
- **2.1b `provision_volume(name, disk_gib, image_path) -> str`** in
  `virtualizer.py`:
  ```python
  volume = self.create_volume(name, disk_gib)
  try:
      self.upload_image(volume, image_path)
      self.resize_volume(volume, disk_gib)
  except RuntimeError:
      self.delete_volume_quiet(volume)
      raise
  return volume
  ```
  Error codes are unchanged, because `upload_image` and `resize_volume`
  already raise their own `VirtError`s.
- **Verify:** `create` succeeds in the integration run. By hand:
  - `create existingname --json` gives `vm-already-defined` with
    `stage: preflight`.
  - `create x --image https://invalid.example/x.img --json` gives
    `image-download-failed` with `stage: image`.

### 2.2 Replace per-VM table parsing with one `virsh dumpxml` (R§4.2)
In `virtualizer.py`, add:

```python
def domain_xml(self, name: str) -> ET.Element | None:
    """Parsed ``virsh dumpxml`` of *name*, or ``None`` when unavailable."""
    text = _stdout(["virsh", "dumpxml", name])
    try:
        return ET.fromstring(text) if text.strip() else None
    except ET.ParseError:
        return None
```

Derive the fields from it:
- uuid: `root.findtext("uuid", "")`
- first MAC: `(root.find("devices/interface/mac") or {}).get("address", "")`.
  Write it with an explicit `None` check, because `Element` truthiness is
  deprecated.
- disks: `[s.get("file") for s in root.iterfind("devices/disk/source") if s.get("file")]`
- metadata: `root.find(f"metadata/{{{self.METADATA_URI}}}{self.METADATA_KEY}")`,
  then `.attrib` or `{}`.

Then:
- `list_vms` makes one `dumpxml` call plus one `domstate` call per domain, and
  returns `VmInfo` with an added `meta: dict[str, str]` field, so `cmd_list`
  needs no extra `virsh metadata` call.
- **Delete:** `_has_pool_disk`, `_first_mac`, `get_domain_uuid` (or make it a
  2-line wrapper over `domain_xml`), and `domain_meta`'s separate `virsh
  metadata` call (1.6). `vm_ip` gets the MAC from `domain_xml`.
- Keep the pool-path component check: `src.startswith(pool_path + "/")`.
- **Verify:** `list --json` output is identical before and after for the
  integration VMs (diff the documents), and there are no extra keys in the
  JSON. `meta` is internal only.
- Check the metadata namespace once by hand:
  `virsh dumpxml <vm> | grep virt-runner` should show
  `<virt-runner:virt-runner xmlns:virt-runner="https://virt-runner.local/vm" …/>`.

### 2.3 One lease-file resolver (R§4.4)
- In `virtualizer.py`:
  ```python
  def lease_file(command: str) -> str:
      """``VM_<CMD>_LEASE_FILE`` > ``VM_CREATE_LEASE_FILE`` > the dnsmasq default."""
      return os.environ.get(f"VM_{command.upper()}_LEASE_FILE") or os.environ.get(
          "VM_CREATE_LEASE_FILE", Virtualizer.DEFAULT_LEASE_FILE
      )
  ```
- Replace the three inline cascades (`cmd_create.py:340`,
  `cmd_list.py:89-95`, `cmd_ssh.py:46-52`). Env var names don't change.

### 2.4 Deadline-based polling (R§3.8)
Add one helper and use it in `wait_for_ip`, `_fallback_ip_domifaddr` and
`verify_ssh_reachable`:

```python
def _poll(check, timeout_s: float, interval: float):
    """Call *check* until it returns truthy or *timeout_s* elapses; return its last value."""
    deadline = time.monotonic() + timeout_s
    while True:
        result = check()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)
```

The error messages' `after {timeout_s}s` then become true. Keep the
lease-file-absent logic (PI-8) exactly as is. Only the loop mechanics change.

### 2.5 Share ssh options and stdio steering
- Module constant plus helper in `virtualizer.py`:
  ```python
  def _ssh_args(identity: str | None) -> list[str]:
      """Host-key policy for throwaway DHCP guests + the injected key only."""
      return [
          "-o",
          "StrictHostKeyChecking=no",
          "-o",
          "UserKnownHostsFile=/dev/null",
          *(["-i", identity, "-o", "IdentitiesOnly=yes"] if identity else []),
      ]
  ```
  Use it in `verify_ssh_reachable` (plus its `ConnectTimeout`/`BatchMode`) and
  `ssh_shell`.
- `ssh_shell` returns `_passthrough(cmd).returncode`. `_passthrough` already
  does the JSON stdout steering. Drop its `text=True`, which isn't needed for
  inherited stdio.

### 2.6 Shared text access lines (R§4.6)
- `output.py`:
  ```python
  def text_access_lines(name: str) -> list[str]:
      return [
          f"Console: virsh console {name}     (Ctrl-] to detach)",
          f"Teardown: virt-runner destroy {name}   (or: virsh destroy {name} && virsh undefine {name})",
      ]
  ```
- Use it in `cmd_create` (both variants) and `cmd_list._text_block`. The text
  output must stay byte-identical, so compare before and after by hand for
  one VM.

### 2.7 Small deletions (R§3.12, R§4.7)
Each is a one-liner. Do them in one commit:
- `generate_cloud_init_files`: drop `os.chmod(tmp_dir, 0o700)` (`mkdtemp` is
  already 0700).
- `ensure_ssh_key`: return `None`, and update its docstring's "Returns".
- `args._wants_json`: `return "--json" in args`.
- `cmd_list._vm_document`: `ip = (vm.ip or None) if leased else None`.
- `get_pool_path`: one `re.search(r"<path>([^<]*)</path>", xml)` on the
  whole text, or `domain_xml`-style ET parsing of `pool-dumpxml`.
- Rename the raise-unless checks for clarity:
  - `libvirt_reachable` → `require_libvirt`
  - `pool_exists` → `require_pool_defined`
  - `domain_not_defined` → `require_domain_absent`

  Update all callers. This is optional; skip it if it bloats the diff.

### 2.8 Download robustness (R§3.12 curl timeout)
- In the curl download (`virtualizer.py:529-539`), replace the hard 600 s
  `timeout=` with curl's own stall detection:
  `["curl", "-fL", "--retry", "3", "--speed-limit", "10240", "--speed-time", "60", "-A", USER_AGENT, "-o", …]`.
  That aborts only after 60 s under 10 KB/s. Delete the `TimeoutExpired`
  branch.
- Add `-A USER_AGENT` to the listing curl in `_resolve_glob` as well, so all
  fetches identify the tool the same way.

### 2.9 (Verify-then-maybe) virt-install flags (R§3.12)
Both items need a real VM, so do them at the end of the phase:
1. **`clouduser-ssh-key`** (`virtualizer.py:915`): remove it, run the full
   integration suite (all four distros must pass SSH). If all four pass, keep
   the removal and drop the now-unused `ssh_key` parameter of `create_vm`. If
   any fails, restore it and write a lessons-learned note on why it's needed.
2. **`--console none`** (`virtualizer.py:906-908`): on a created VM, run
   `virsh dumpxml NAME | grep -E '<(serial|console)'`.
   - If there is **no** serial/console device, replace `--console none` with
     `--noautoconsole`. Every output line recommends `virsh console NAME`, so
     the device must exist.
   - If a pty serial exists, keep the flag and add a code comment saying so.

**Phase 2 exit:** unit and integration green; `list --json` and
`create --json` documents are key-identical to the baseline, apart from the
documented `booted` addition on preflight errors. `git diff --stat develop`
shows net deletions in `src/`.

### 2.10 (Deferred) splitting `virtualizer.py`
Do **not** split preemptively. After Phase 2, run `wc -l src/virt_runner/virtualizer.py`.
If it's still above ~1,000 lines, move only the image code (`ImageFetch`,
`fetch_and_verify_image`, `_fetch_sums`, `_sums_entries`, `_resolve_glob`,
`_newest_match`, `_sha256_of`) into `src/virt_runner/images.py`. Keep
`Virtualizer.fetch_and_verify_image` as a one-line delegate, or call the
function directly from `cmd_create`.

---

## Phase 3 — Tests, docs, contract

### 3.1 Harden the integration test (R§5)
- Wrap steps 1–4 of `main()` in `try: … finally:` that runs
  `virt_runner("destroy", n, allow_fail=True)` for all names, so a failed
  assertion doesn't leak VMs.
  - Keep `check()` using `sys.exit`, which is fine because `SystemExit` still
    runs `finally`.
- Add cheap negative checks that don't create VMs:
  - `create it-vm-a` while it exists → `status == "error"`,
    `error.code == "vm-already-defined"`, exit code 1
  - `destroy it-never-existed` → `error.code == "vm-not-defined"`
  - `create 1bad` → `error.code == "usage"`, exit code 2

  The `virt_runner()` helper then needs to return `proc.returncode` too.
  Change it to return `(doc, rc)`, or add the rc to the doc under a `_rc` key
  inside the test.
- Add `image.verified` checks (from 1.2) if not done already.

### 3.2 Restore an in-repo source of truth for the JSON contract (R§6)
`specification/python-rewrite.md` is referenced by README, `errors.py` and
`output.py` but doesn't exist.

- **Minimal fix (preferred):** add a "JSON output" section to `README.md`
  containing:
  - the envelope keys (`tool`, `version`, `status`, `error{code,message,stage?}`)
  - the exit codes
  - the error-code table below, one line of meaning each

  Derive the table with `grep -rhoE '"[a-z]+(-[a-z]+)+"' src/virt_runner | sort -u`
  and filter to the codes. It should include at least:
  `usage`, `runtime-error`, `libvirt-unreachable`, `pool-not-active`,
  `pool-not-found`, `pool-path-unknown`, `network-not-active`,
  `vm-already-defined`, `vm-not-defined`, `vm-not-running`, `no-ip`,
  `ssh-key-missing`, `image-download-failed`, `image-verification-failed`,
  `image-no-cache`, `cloud-init-failed`, `volume-create-failed`,
  `volume-import-failed`, `volume-resize-failed`, `vm-create-failed`,
  `lease-timeout`, `ssh-timeout`, `volume-delete-failed`, plus new
  `vm-destroy-failed` and `unsupported-arch`.
- Point `errors.py:7`, `output.py:3` and all README links at that section.
  Remove every reference to `specification/python-rewrite.md`.
- Only if the original spec exists in git history
  (`git log --all --oneline -- specification/python-rewrite.md`), consider
  restoring it instead. Ask the user before restoring a 500+ line document.

### 3.3 CHANGELOG
- Move Arch/Fedora out of *Unreleased* into a `[0.3.0]` section. Check
  `git log` for the version bump dates; add `[0.2.0]` if missing, from git
  history.
- Under *Unreleased*, list the Phase 1 fixes (Fedora verification, cache-hit
  honesty, custom cache key, destroy codes, per-VM identity, `--user`
  validation, arch refusal) and Phase 2 behavior notes (`booted` on preflight
  errors, stall-based download timeout).
- Remove or annotate references to `bin/vm-*` and
  `specification/specification.md` in the 0.1.0 entry. They're historical, so
  add "(since removed)" rather than rewriting history.

### 3.4 README refresh
- The Commands section already lists `--distro`. Add a note that cached
  `latest`/`current` images are never refreshed: *"delete
  `~/vm-images/<distro>/<release>/` to pick up a newer build"*. Also add the
  new `unit_test.py` command under Development.

---

## Explicitly out of scope (with reasons)

| Idea | Why not now |
|---|---|
| Auto-refresh of `latest`/`current` caches (R§3.11) | Needs a design decision (always re-fetch sums? TTL?); the README note (3.4) covers users. Add when someone gets bitten. |
| Rework `--keep-going` | It's a public flag with near-no-op semantics; removing it breaks scripts. Leave it and document the behavior. |
| PGP verification of Fedora CHECKSUM / Ubuntu `SHA256SUMS.gpg` | TLS to the official origin is the documented trust anchor (spec); adding gpg is a new host dependency. |
| pytest / CI pipeline | `tests/unit_test.py` with asserts is enough at this size; add CI when there's a remote runner. |
| Turning `Virtualizer` into module functions | Churn across every call site for no behavior gain. |
| arm64 guests | Spec §2.4 feature, not a fix; 1.8 makes the gap explicit. |

---

## Task checklist (for the implementing agent)

- [ ] 0.1 ruff green
- [ ] 0.2 stale strings
- [ ] 1.1 `tests/unit_test.py` scaffold + AGENTS.md note
- [ ] 1.2 Fedora sums URL via `profile.sums_url` + unified `_sums_entries` + lessons-learned 004
- [ ] 1.3 `.verified` marker; honest cache-hit reporting
- [ ] 1.4 `_newest_match`, offline cache fallback, no literal `*` in paths
- [ ] 1.5 destroy: `libvirt_reachable` + `vm-destroy-failed`
- [ ] 1.6 identity in metadata (ET-built XML); ssh/list use it
- [ ] 1.7 `--user` validation; JSON-quoted cloud-init scalars; pure renderers
- [ ] 1.8 non-x86_64 preflight refusal
- [ ] 1.9 custom cache keyed by URL hash
- [ ] Phase 1 integration run green (all `image.verified == true`)
- [ ] 2.1 cmd_create single try + `provision_volume`
- [ ] 2.2 `domain_xml`; delete table parsers
- [ ] 2.3 `lease_file()`
- [ ] 2.4 `_poll` deadline helper
- [ ] 2.5 `_ssh_args`; `ssh_shell` via `_passthrough`
- [ ] 2.6 `text_access_lines`
- [ ] 2.7 small deletions
- [ ] 2.8 curl stall detection + UA
- [ ] 2.9 verify `clouduser-ssh-key` / `--console none`
- [ ] Phase 2 integration run green; JSON documents key-identical
- [ ] 3.1 integration try/finally + negative checks
- [ ] 3.2 JSON contract section in README; dead spec links removed
- [ ] 3.3 CHANGELOG
- [ ] 3.4 README notes
