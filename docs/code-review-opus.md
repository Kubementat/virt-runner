# Code Review — virt-runner (full repository)

**Date:** 2026-09-25 · **Reviewer:** Claude (Opus 5.5) · **Branch/commit:** `develop` @ `88967c5`
**Scope:** every tracked file under `src/virt_runner/`, `tests/integration_test.py`,
`pyproject.toml`, `README.md`, `CHANGELOG.md`, `AGENTS.md`, plus spot checks of
`specification/features/generalize-vm-creation-specification.md` and
`install-prerequisites.sh`.

Companion plan with concrete, ordered changes: [`code-review-opus-plan.md`](code-review-opus-plan.md).

---

## 1. Summary

virt-runner is a small, readable CLI (~2,000 lines of Python) with a clear
layering (`cli → cmd_* → virtualizer → output/errors`), a disciplined
JSON-output contract, and unusually good in-code rationale (every pitfall is
explained where it is handled). The subprocess helper set (`_run`/`_stdout`/
`_spawn`/`_quiet`/`_passthrough`) and the `VirtError(code, stage)` pattern are
solid foundations.

Main problems:

1. **One real security/correctness bug.** Fedora images are never checksum-verified,
   and the JSON output then says they were (§3.1, §3.2). I confirmed this live
   on this host.
2. **Identity/user handling breaks outside the default path.** `ssh`/`list` ignore
   a non-default `--ssh-key` (§3.3). `--user` goes unvalidated into YAML and XML (§3.5).
3. **Error codes and messages that mislead** in `destroy`: a stopped libvirt
   is reported as "VM not defined" (§3.4), and a failed undefine as
   `volume-delete-failed` (§3.6).
4. **Structural weight in two places.** `virtualizer.py` (1,347 lines) mixes five
   concerns. `cmd_create.py` repeats a 10-line try/except block ten times. `list`
   spawns ~5 `virsh` processes per VM where one `dumpxml` would do.
5. **No unit tests.** The pure parsers (sums, leases, virsh tables, URL
   derivation) are only exercised by a multi-minute test against real VMs,
   which is how bug §3.1 got through.
6. **Doc drift.** The README links a spec file that doesn't exist, user-facing
   strings still say `vm-create`, docstrings count distros wrong, and the
   lint/format checks currently fail.

| Severity | Count |
|---|---|
| High | 3 |
| Medium | 9 |
| Low / cleanup | 20+ |

---

## 2. Architecture assessment

**What works**

- A single subprocess choke point (`_spawn`) with an explicit `check=False`
  and a sanitized child `PATH` (`_child_env`, lesson 003). That's the right call.
- `VirtError(message, code, stage)` plus `output.fail_with()` keeps error codes
  stable without matching on message strings.
- `JsonCommand.make_context` turns usage errors into JSON too. That's thoughtful.
- `DistroProfile` data keeps distro conditionals out of the pipeline, except
  for the `sums_kind` switch (see §4.3).
- Result dataclasses (`ImageFetch`, `TeardownResult`, `VmInfo`) separate facts
  from rendering.

**What is heavy**

- `Virtualizer` is a class with **no instance state**. Every method could be a
  module function. The class adds `self.` noise and a `Virtualizer()` per command,
  but no polymorphism, injection or caching. This is fine to keep. The bigger
  issue is that one 1,347-line file holds five concerns:
  1. subprocess helpers
  2. image fetch/verify with three sums parsers and glob resolution
  3. cloud-init rendering
  4. libvirt domain/volume/pool operations
  5. IP discovery/SSH

  The image and lease code is pure and belongs in its own testable modules.
- `cmd_create.py` is ~60% exception plumbing. Nearly all of it is redundant,
  because the `VirtError`s raised by the `Virtualizer` already carry `code` and
  `stage`.
- Three separate fetch mechanisms:
  - `curl` for images
  - `urllib` for sums, the only one that sends `USER_AGENT`
  - `curl -s -L` for directory listings

  Each has its own `file://` handling.

---

## 3. Findings — correctness & security

### 3.1 [HIGH] Fedora images are never checksum-verified
`src/virt_runner/virtualizer.py:463-470`

For `sums_kind == "block"`, the sums URL is derived by string replacement on the
image basename:

```
Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2  →  Fedora-Cloud-Base-Generic-44-1.7-x86_64-CHECKSUM
```

The real file is `Fedora-Cloud-44-1.7-x86_64-CHECKSUM`. The spec, §5.3,
states the correct name. I checked this live on 2026-09-25:

- the derived URL returns **HTTP 404**
- the directory listing contains `Fedora-Cloud-44-1.7-x86_64-CHECKSUM`
- `~/vm-images/fedora/44/` contains the image but **no sums file**, which proves the
  verify step was skipped on the real run

`_fetch_sums` returns `False`, and the code takes the "no same-directory
SHA256SUMS; skipping verification" warning path. Under `--json` that warning
is dropped, so the only trace is `"verification": "skipped"` in the document.
The integration test doesn't assert on it.

The unit-testable `_sums_block` parser itself is fine. The sums URL is simply
wrong. Fix: derive the CHECKSUM name from the resolved listing (spec §5.3 says
"the sums file is the source of truth"), or template it as
`Fedora-Cloud-{release}-{point}-x86_64-CHECKSUM`, where `{point}` is parsed out
of the resolved image name.

### 3.2 [HIGH] A cache hit claims verification that never happened
`virtualizer.py:494-511`, docstring `:151-154`

On a cache hit, `verification = "sha256-sums" if verifiable else "skipped"`.
The docstring justifies this with "the cache is only written by a verified
download". That's false: the skip-verification path (`:577-583`) also leaves the
image in the cache.

So the second Fedora create, and any release whose sums were unreachable on
first download, reports `"verified": true`. The JSON contract lies about trust.

Fix: persist the verification outcome next to the image, e.g. a
`<image>.verified` marker holding the digest or mode, written only after a
successful compare. On a cache hit, read it back, or re-verify against the
cached `SHA256SUMS` (cheap relative to boot time).

### 3.3 [MEDIUM-HIGH] `ssh`/`list` ignore the key the VM was created with
`cmd_ssh.py:26`, `cmd_list.py:80`, `virtualizer.py:1199-1209`

`create --ssh-key ~/.ssh/other.pub` injects `other.pub`. But `ssh` and `list`
always use `private_key_path(DEFAULT_SSH_KEY)`, and `ssh_shell` passes
`-o IdentitiesOnly=yes`. So `virt-runner ssh NAME` offers only the default key,
won't fall back to the agent or other keys, and fails with "Permission denied".

`list` prints a wrong `ssh_command` for such VMs. The user is already
recorded in domain metadata (`set_domain_metadata`), but the identity isn't.

Fix: record `identity="<private key path>"` in the same metadata element and
read it back. Keep `--user`-style precedence (flag > metadata > default).

### 3.4 [MEDIUM] `destroy` reports "not defined" when libvirt is unreachable
`cmd_destroy.py:20-28`

`domain_exists()` is `virsh dominfo` returncode == 0. `destroy` never calls
`libvirt_reachable()` first, unlike `ssh` and `list`. With libvirtd down or
the user missing from the `libvirt` group, `destroy myvm` prints
`VM 'myvm' is not defined` with code `vm-not-defined`. That's misleading, and
a script treating `vm-not-defined` as "already gone" will wrongly consider the
teardown done. The integration test's cleanup loop relies on exactly that
tolerance.

### 3.5 [MEDIUM] `--user` goes unvalidated into YAML and XML
`cmd_create.py:120-124`, `virtualizer.py:744-754`, `:964`

The user name is interpolated raw into:

- cloud-init YAML: `- name: {user_name}`. A value with `:`, a newline or `#`
  produces invalid or different YAML.
- the metadata XML: `user="{user}"`. A `"` or `<` breaks `virsh metadata --set`.
  The failure is then swallowed into a warning, and `ssh` falls back to `ubuntu`.

It's local and self-inflicted, so this isn't a privilege issue. It is a silent
misconfiguration, though. Validate with a POSIX user-name regex
(`^[a-z_][a-z0-9_-]{0,31}$`) as a click callback, like `_validate_name`.

`--ssh-key` contents also go into YAML unquoted. A key comment containing
`: ` or `#` could break parsing. Emit user-data with JSON-quoted scalars
(`json.dumps(value)` is valid YAML).

### 3.6 [MEDIUM] Wrong error code when destroy/undefine fails
`cmd_destroy.py:30-33`, `virtualizer.py:1311-1314`

`destroy_vm` raises a plain `RuntimeError` from `_run(["virsh", "destroy"|"undefine", …])`.
`cmd_destroy` maps every non-`VirtError` to `volume-delete-failed`. A domain
that failed to undefine is reported as a volume problem, and the message is the
raw "Command failed (exit 1): virsh --quiet undefine …" text.

Wrap both in `VirtError(..., "vm-destroy-failed")`. Add the code to the codes
table.

### 3.7 [MEDIUM] Fedora glob picks the first match, not the newest, and needs the network on cache hits
`virtualizer.py:452-453`, `:670-692`

- `_resolve_glob` returns the **first** `href` matching the pattern in listing
  order. Directory listings sort by name, so `44-1.10` sorts before `44-1.7`.
  After a respin the tool could pick the older build, or whatever the mirror
  lists first. The spec asks for "the CHECKSUM of the newest build".
- Resolution runs **before** the cache lookup. Every Fedora `create` needs the
  network even with a warm cache. If the listing fetch fails, the literal
  `*` flows into `img_basename` and the cache path
  (`~/vm-images/fedora/44/Fedora-Cloud-Base-Generic-44-*.x86_64.qcow2`). The
  download then fails with a confusing URL. `--keep-going` can never help
  here, because the cache key contains the glob.
- Only `.qcow2` hrefs are considered. That's fine for Fedora, but it's an
  undocumented coupling.

### 3.8 [MEDIUM] Poll loops overshoot their stated timeouts
`virtualizer.py:1070-1081`, `:1155-1159`, `:1098-1105`

The loops count iterations (`timeout_s // interval`) instead of measuring
elapsed time. `verify_ssh_reachable` runs 45 × (up to 2 s `ConnectTimeout` +
2 s sleep), so up to **~180 s** for a "90 s" window. `wait_for_ip` adds a
`virsh` call per iteration. `_fallback_ip_domifaddr` adds up to 60 s on top of
the 120 s. The error text says "no DHCP lease after 120s". Use a
`time.monotonic()` deadline.

### 3.9 [MEDIUM] Custom-image cache collides on basename
`virtualizer.py:477-482`

A bare `--image URL` is cached at `custom/<basename>`. Two different URLs
ending in `disk.qcow2` (very common) share one cache entry. The second create
silently boots the first image, and reports it as a cache hit. Key the cache
by a hash of the URL: `custom/<sha256(url)[:12]>/<basename>`.

### 3.10 [MEDIUM] Arch and aarch64 are reported but not honored
`cmd_create.py:184`, `profiles.py`

`run_state["arch"] = v.host_arch()` is reported in JSON, but every image URL
is hard-coded x86_64/amd64. On an aarch64 host the tool downloads an x86 image
and virt-install fails late, after a multi-hundred-MB download. Until arm64
is implemented (spec §2.4), preflight should refuse non-x86_64 hosts with a
clear error.

### 3.11 [LOW-MEDIUM] Mutable "latest"/"current" caches never refresh
Ubuntu `…/current/…` and Arch `images/latest/…` are cached by release name
forever. A months-old "latest" gets booted without notice. At minimum, document
it and add a `--refresh-image` flag. The cheap check: re-fetch the sums file
and compare the digest of the cached file.

### 3.12 [LOW] Other correctness nits

- `cmd_create.py:149`: `release: str` should be `str | None`.
- `cmd_list.py:23`: `vm.ip or None if leased else None` parses as intended
  (`(vm.ip or None) if leased else None`), but it reads as a precedence bug.
  Parenthesize or simplify.
- `cmd_list.py:100-102`: `display_user()` runs `virsh metadata` for every VM,
  even when the result is thrown away (non-leased → `ssh_user=None`).
- Preflight JSON error documents for `create` (`cmd_create.py:198-212`) omit
  `booted`, while every later failure includes it. The schema is inconsistent
  depending on stage.
- `ensure_ssh_key` and `generate_cloud_init_files` return values nobody uses
  (`path` echo; `tmp_dir`).
- `os.chmod(tmp_dir, 0o700)` is redundant; `mkdtemp` already creates 0700.
- `_passthrough` and `ssh_shell` duplicate the stdout-to-stderr steering, and
  `verify_ssh_reachable`/`ssh_shell` duplicate the ssh option list.
- `virt-install` is called with **both** full `user-data` **and**
  `clouduser-ssh-key`. `clouduser-ssh-key` targets the image's *default* cloud
  user, which the user-data `users:` list suppresses (spec §2.2), so it's
  redundant at best. Verify, then drop it. The spec already says it should only
  be used when `default_user` is non-empty.
- `--console none` (`virtualizer.py:907`): check on a virt-runner-created domain
  that `virsh console NAME` still attaches, since every output line recommends
  it. If it doesn't, replace with `--noautoconsole`. *Unverified; no
  virt-runner VM existed on the host during review.*
- `--keep-going` is nearly a no-op. The cache hit already short-circuits before
  any download, so the flag only changes a warning and one error code (noted as
  G1 in `docs/reviews/poc-review.md`, still true).
- `curl` download timeout of 600 s kills slow but healthy downloads of the
  ~600 MB images on links under ~1 MB/s.

---

## 4. Findings — simplification & design

### 4.1 `cmd_create.py`: ten near-identical try/except blocks
Lines 198-372. Each block is:

```python
try:
    x = v.step(...)
except RuntimeError as exc:
    output.fail_with(
        "create", exc, "<code>", stage="<stage>", fields=_create_document(run_state)
    )
```

Every `Virtualizer` method already raises `VirtError` with the right code, and
nearly all with the right stage. Two exceptions: `libvirt_reachable` has no stage,
and `set_domain_metadata` raises a plain `RuntimeError`. One outer
`try/except RuntimeError` around the pipeline, with a `stage` tracker for
plain RuntimeErrors, collapses ~120 lines into ~15.

Volume cleanup after an upload or resize failure should move into the
`Virtualizer`, e.g. `provision_volume(name, disk, image)` that cleans up after
itself, mirroring how `create_vm` already cleans up.

### 4.2 `list` is N×5 subprocesses
`list_vms` runs `domblklist`, `domstate`, `domiflist`, `dominfo` and (via
`display_user`) `metadata` per domain, plus text-table parsing for each. One
`virsh dumpxml NAME` parsed with `xml.etree.ElementTree` yields uuid, disk
sources, first MAC and the `<virt-runner:…>` metadata element. `virsh domstate`
is still needed, because dumpxml doesn't carry the state. That removes
`_has_pool_disk`, `_first_mac`, `get_domain_uuid`'s table parsing and
`domain_user`'s regex. `vm_ip` (ssh) can reuse the same helper.

### 4.3 Sums handling belongs in the profile
`fetch_and_verify_image` branches on `sums_kind` twice: once to derive the URL
(`:459-470`), once to pick the parser (`:552-559`). The bare `--image` path
duplicates the "dir" derivation. Put a small `SumsSource` strategy (or two
callables, `sums_url(image_url)` and `parse(text)`) on the profile, as the spec's
`sums_url_template` intends. The pipeline then has no `sums_kind` conditional,
which honors the `profiles.py` promise ("Pipeline code stays free of
distro-name conditionals").

### 4.4 One place for lease-file resolution
Three copies of the env-var cascade exist: `cmd_create.py:340`,
`cmd_list.py:89-95`, `cmd_ssh.py:46-52`, using `VM_CREATE_LEASE_FILE`,
`VM_LIST_LEASE_FILE` and `VM_SSH_LEASE_FILE`. Replace them with one
`virtualizer.lease_file()` helper. Keeping the per-command overrides is fine,
but consider collapsing to a single `VIRT_RUNNER_LEASE_FILE` (keeping the old
names as fallbacks).

### 4.5 Split `virtualizer.py`
Suggested modules, each well under 400 lines:

| Module | Contents |
|---|---|
| `host.py` | `_child_env`, `_spawn`/`_run`/`_stdout`/`_quiet`/`_passthrough`, `ssh_base_args()` |
| `images.py` | `ImageFetch`, `fetch_and_verify_image`, sums fetch and parsers, glob resolve, sha256 |
| `cloudinit.py` | `render_user_data`, `render_meta_data`, `write_seed_dir` (pure rendering + one writer) |
| `leases.py` | `find_ip_for_mac`, JSON/legacy parsers, `ip_status_for`, IP_STATUS_* |
| `virtualizer.py` | libvirt domain/volume/pool ops, `wait_for_ip`, `verify_ssh_reachable`, `ssh_shell`, `list_vms`, `destroy_vm` |

Keep `Virtualizer` as the facade the `cmd_*` modules call, so the `cmd_*` side
barely changes.

**Recommendation: defer the split.** The deletions in §4.1-4.3 (dumpxml, one
sums parser, provision_volume) shrink the file by roughly 250 lines. Split only
if it's still above ~1,000 lines afterwards, and then extract just
`images.py`, the part with real standalone logic. More files isn't a goal by
itself.

### 4.6 Text vs JSON rendering duplication
The "Console:" / "Teardown:" lines are formatted three times: create booted,
create no-boot, and list. They should come from `output.access_commands()` (or
a sibling `text_access_lines(name)`), so the text and JSON forms can't drift,
as the `cmd_list` docstring already promises.

### 4.7 Small API tidy-ups

- `pool_exists`, `libvirt_reachable`, `domain_not_defined` and
  `preflight_check` are "raise unless" checks named like predicates. Rename
  them `require_pool_defined`, `require_libvirt` and so on.
- `domain_exists` should distinguish "libvirt error" from "no such domain".
  `virsh dominfo` stderr contains `failed to get domain` for the latter. Better:
  call `require_libvirt()` first in every command (see §3.4).
- `_table_has` and `get_pool_path` loop line by line over text that a single
  `re.search`/ElementTree call handles.
- `args._wants_json` does `list(args or [])` on an already-list argument.

---

## 5. Findings — tests

- **No unit tests at all.** Everything is covered by one sequential
  integration run that creates four real VMs, takes minutes and needs
  KVM. The following are pure and trivially testable, and none are tested:
  - `_sums_entries`, `_sums_block`
  - `_parse_json_lease`, `_parse_legacy_lease`
  - `ip_status_for`, `_table_has`
  - `access_commands`, `document`
  - `_validate_name`, `generate_mac`
  - the cloud-init renderers
  - sums-URL derivation
  - `JsonCommand` usage-error JSON

  A 20-line pytest over the sums-URL derivation would have caught §3.1.
- The integration test never asserts `image.verified`. Adding
  `check(doc["image"]["verified"], …)` per create leg would have caught §3.1
  and §3.2 immediately.
- On failure the integration test leaves VMs behind: `sys.exit` runs
  mid-flow and there's no `try/finally`. The next run cleans up, but a CI run
  would leak. Wrap the flow in `try/finally` with the destroy loop.
- Test docstrings and comments are stale ("2 Ubuntu + 1 Arch", "our three
  VMs") after the Fedora leg was added.
- Negative paths are uncovered: duplicate name, `destroy` of an unknown VM
  (only used as cleanup, never asserted), usage errors under `--json`, and
  `--no-boot`.

---

## 6. Findings — tooling, docs, hygiene

- **Lint is red:** `uv run ruff check .` reports I001 (unsorted imports:
  `fnmatch` in `virtualizer.py:25`). `ruff format --check` wants to reformat 3
  files. Nothing enforces these checks; there's no CI and no pre-commit.
- **README points to a missing spec:** `specification/python-rewrite.md`
  (README lines 56, 89, 95). `errors.py:7` and `output.py:3` reference it too.
  `CHANGELOG.md` references `specification/specification.md` and `bin/vm-*`
  scripts, neither of which exists anymore. The JSON contract (§5 codes table)
  therefore has **no in-repo source of truth**.
- **Stale user-facing strings:**
  - `cmd_list.py:120` says "(vm-create <name> to create one)".
  - `cmd_list.py:77` says "List VMs configured by vm-create".
  - `--user` help (`cmd_create.py:123`) says "ubuntu/arch", missing fedora.
  - `output.fail` docstring lists `create, destroy, list`, missing `ssh`.
- **Stale docstrings:**
  - `profiles.py:5` says "this slice lands exactly two" (three exist).
  - `output.py:12-13` shows version `0.2.0` (package is 0.3.0).
- **CHANGELOG:** Arch/Fedora sit under *Unreleased* although `pyproject.toml`
  is already `0.3.0`, and 0.2.0/0.3.0 entries are missing.
- `install-prerequisites.sh` usage text says `./bin/install-prerequisites.sh`,
  but the script lives at the repo root.
- `AGENTS.md` asks for the `ponytail` skill and a `codegraph` MCP tool. Neither
  was available in this review session (the codegraph MCP server failed to
  start: executable not on `PATH`).

---

## 7. Findings ranked

| # | Sev | Finding | Location |
|---|---|---|---|
| 1 | High | Fedora sums URL wrong → image never verified | `virtualizer.py:463-470` |
| 2 | High | Cache hit reports `verified: true` for unverified images | `virtualizer.py:494-511` |
| 3 | High (test gap) | No unit tests; integration test doesn't assert `verified` | `tests/` |
| 4 | Med-High | `ssh`/`list` ignore non-default `--ssh-key` (+`IdentitiesOnly`) | `cmd_ssh.py:26`, `cmd_list.py:80` |
| 5 | Med | `destroy` says "not defined" when libvirt is down | `cmd_destroy.py:20` |
| 6 | Med | `--user`/key unvalidated into YAML and XML | `virtualizer.py:744`, `:964` |
| 7 | Med | Undefine failure mapped to `volume-delete-failed` | `cmd_destroy.py:33` |
| 8 | Med | Glob picks first, not newest; resolution defeats cache | `virtualizer.py:453`, `:670` |
| 9 | Med | Poll loops overshoot timeouts (up to 2×) | `virtualizer.py:1070`, `:1155` |
| 10 | Med | `custom/` cache keyed by basename only | `virtualizer.py:482` |
| 11 | Med | Non-x86_64 host not refused up front | `cmd_create.py:184` |
| 12 | Med | Lint/format red; spec referenced by README missing | repo |
| 13 | Low | Stale latest/current caches; nits in §3.12, §4.7, §6 | various |
