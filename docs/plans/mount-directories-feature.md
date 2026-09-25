# Plan: share host directories into guests (`create --mount`)

Status: ready to implement · Written 2026-09-25 against branch `refactoring-1` (`4ecf53c`)

## 1. Goal

```bash
uv run virt-runner create myvm --mount ~/src/project:/work --mount /data/datasets
```

Each `--mount HOST[:GUEST]` shares a host directory into the guest. It is mounted at
`GUEST` (default `/mnt/<basename of HOST>`), read-write, and stays mounted across guest
reboots. When `create` reports success, the mount is already usable over SSH.

## 2. Research summary (why the design looks like this)

| Question | Finding |
|---|---|
| Mechanism | **virtiofs**. It is the libvirt-native way to share a host dir and is much faster than 9p. It works with this host's stack: libvirt 10.0.0, QEMU 8.2.2, virt-install 4.1.0 (Ubuntu 24.04). |
| virt-install syntax | Verified with `--print-xml`: `--memorybacking source.type=memfd,access.mode=shared` plus `--filesystem source.dir=HOST,target.dir=TAG,driver.type=virtiofs,binary.path=/usr/libexec/virtiofsd` produces the right `<memoryBacking>` and `<filesystem type="mount">` XML. |
| Shared memory | virtiofs **requires** shared guest memory (`memfd` + `access mode="shared"`). Without it, the domain fails to start. Add the flag only when at least one mount is requested. |
| virtiofsd binary | **Not installed on this host.** The Ubuntu 24.04+ package is `virtiofsd` (1.10), which installs `/usr/libexec/virtiofsd`. We pass `binary.path` explicitly so libvirt does not need to find a vhost-user descriptor. |
| AppArmor | `/etc/apparmor.d/usr.sbin.libvirtd` already allows `/usr/{lib,lib64,lib/qemu,libexec}/virtiofsd PUx`, and the `virtiofsd` profile is `unconfined`. **No AppArmor changes needed.** |
| Sandboxing | `libvirtd`/`virtqemud` run with `PrivateTmp=no`, `ProtectHome=no`, so host dirs under `/tmp` and `/home` are visible to virtiofsd. |
| Privileges | Under `qemu:///system`, libvirt starts virtiofsd as **root**, so it can read any host dir. UIDs pass straight through: guest user `ubuntu`/`arch`/`fedora` is uid 1000, and so is the usual host user, so ownership matches. Files the guest creates as root are root-owned on the host. |
| Guest-side mounting | Use a cloud-init **`bootcmd`**, not the `mounts:` module. `cc_mounts` rewrites a bare tag such as `mount0` to `/dev/mount0`, so a virtiofs tag is not a safe `fs_spec` there. `bootcmd` runs on **every boot**, which makes the mount persist without fstab, and it runs in `cloud-init.service`, which is `Before=sshd.service` and `Before=sysinit.target` (checked in the host's unit file). So once `ssh-verify` passes, the mount is already in place and **no extra wait stage is needed**. |
| Guest kernels | virtiofs is a module (`virtiofs.ko`) in the Ubuntu generic, Arch, and Fedora kernels. `mount -t virtiofs` autoloads it. The integration test confirms this for all three distros. |
| Tag length | The virtio-fs mount tag is limited to 36 bytes. Tags are generated as `mount0`, `mount1`, … so they are always short and never contain user input. |

## 3. Design decisions (final — do not re-litigate)

- CLI: repeatable `--mount HOST[:GUEST]` on `create` only.
  - `HOST` is expanded (`~`), resolved to an absolute path, and must be an existing directory.
  - `HOST` must not contain `,` (virt-install splits sub-options on commas). The first
    `:` separates HOST from GUEST, so HOST cannot contain `:` either.
  - `GUEST` defaults to `/mnt/<basename(HOST)>`. It must match
    `/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*` and contain no `.`/`..` component. This keeps it
    safe to put unquoted into the `bootcmd` shell line.
  - The same GUEST path must not appear twice.
  - Invalid input raises `click.BadParameter`, which gives exit 2 and error code `usage`
    (existing mechanism in `args.py`).
- Tags are `mount<i>`, where `i` is the position of the `--mount` flag.
- Mounts are computed **once** in `cmd_create` as a list of dicts
  `{"source": str, "target": str, "tag": str}`. The same list feeds cloud-init, virt-install
  and the JSON output.
- Preflight: when mounts are requested, `/usr/libexec/virtiofsd` must be executable.
  Otherwise the new error code is `virtiofsd-missing` (stage `preflight`).
- JSON: `vm.mounts` is always present (an empty list when there are no mounts).
  Text: one `Mount:   HOST -> GUEST` line per mount.
- `destroy` does not change. virtiofsd exits with the domain, and the host directory is
  **never** touched.

### Out of scope (say so in the README, do not build)

- Read-only mounts. libvirt 10 has no `<readonly/>` for virtiofs. A guest-side `ro`
  option would not be a security boundary. Add it when the host has a newer libvirt.
- Adding or removing mounts on an existing VM, and showing mounts in `list`.
- UID/GID remapping (`<idmap>`) and older hosts where virtiofsd lives at
  `/usr/lib/qemu/virtiofsd` (Ubuntu 22.04).

## 4. Git workflow

The code in this plan is from `refactoring-1`, which is 30 commits ahead of `develop`.
Branch from `develop` if `refactoring-1` has been merged into it by then. Otherwise
branch from `refactoring-1`.

```bash
git worktree add ../virt-runner-mount -b feature/mount-directories develop   # or refactoring-1
cd ../virt-runner-mount
uv sync
```

## 5. Implementation steps

Do them in order. After each step run `uv run tests/unit_test.py` and `uv run ruff check .`.

### Step 1 — host prerequisite: install virtiofsd via `install-prerequisites.sh`

`virtiofsd` is a hard requirement for `--mount`, and this host does not have it yet. It
goes into the script's normal package list. The script's existing machinery then handles
it with no new logic: `--check` reports it as missing, a normal run installs it, and
re-runs upgrade it.

**1a. Header comment** (the bullet list around lines 11–16). Add a bullet:

```bash
#     * virtiofsd, the daemon libvirt starts for `create --mount` host-directory
#       shares (/usr/libexec/virtiofsd)
```

**1b. `usage()` text** (line ~80). Change the first sentence to
`Installs the KVM + libvirt + virt-install + virtiofsd toolchain virt-runner drives and`.

**1c. Package list.** In the comment above the packages (line ~118), add `virtiofsd` to the
list of tools. Then add it to `BASE_PACKAGES` after `virtinst`:

```bash
  # virtiofs daemon: libvirt starts it per shared dir for `create --mount`
  # (installs /usr/libexec/virtiofsd = Virtualizer.VIRTIOFSD)
  virtiofsd
```

Leave it unconditional, with no flag. Every host that `virt-runner` supports (Ubuntu 24.04+,
because virt-install must be ≥ 4.1) has the `virtiofsd` package. On a release without it,
the existing `finding "no installation candidate …"` path reports it correctly.

**1d. Verification section**, right after the `check_cmd jq …` line (~513). `virtiofsd` is
not on `PATH` because it lives in libexec, so `check_cmd` can't be reused:

```bash
# virtiofsd lives in libexec, not on PATH; must match Virtualizer.VIRTIOFSD.
VIRTIOFSD=/usr/libexec/virtiofsd
if [[ -x "${VIRTIOFSD}" ]]; then
  success "virtiofsd: $("${VIRTIOFSD}" --version 2>&1 | head -1)"
else
  warn "${VIRTIOFSD} not found — 'virt-runner create --mount' will fail (package: virtiofsd)"
fi
```

No AppArmor, libvirt config or service restart is needed (see §2). libvirt is given the
binary path explicitly on each `virt-install` call.

**1e. Verify the script change:**

```bash
bash -n install-prerequisites.sh
shellcheck install-prerequisites.sh            # if installed; no new warnings
./install-prerequisites.sh --check             # expect: "missing:   virtiofsd", "[check] would run: … apt-get install … virtiofsd"
./install-prerequisites.sh                     # installs it
./install-prerequisites.sh --check             # expect: "installed: virtiofsd (1.10…)" and "+ virtiofsd: …" in Verification
```

### Step 2 — `src/virt_runner/virtualizer.py`

**2a. Cloud-init renderer.** Extend `render_user_data` (line ~217) with an optional
`mounts` argument. Output with no mounts must stay byte-identical.

```python
def render_user_data(
    user_name: str, group: str, key_line: str, mounts: list[dict[str, str]] = ()
) -> str:
    """Generate cloud-init user-data YAML with JSON-quoted scalars.

    Each mount becomes an idempotent ``bootcmd`` line. bootcmd runs on every
    boot, before sshd, so the share is mounted by the time SSH answers.
    """
    bootcmd = "".join(
        "  - "
        + json.dumps(
            f"mkdir -p {m['target']} && (mountpoint -q {m['target']} || "
            f"mount -t virtiofs {m['tag']} {m['target']})"
        )
        + "\n"
        for m in mounts
    )
    return (
        "#cloud-config\n"
        "manage_etc_hosts: true\n"
        "users:\n"
        f"  - name: {json.dumps(user_name)}\n"
        f"    groups: [{group}]\n"
        '    sudo: ["ALL=(ALL) NOPASSWD:ALL"]\n'
        "    ssh_authorized_keys:\n"
        f"      - {json.dumps(key_line)}\n"
        "package_update: false\n" + (f"bootcmd:\n{bootcmd}" if mounts else "")
    )
```

**2b. `generate_cloud_init_files`.** Add the parameter `mounts: list[dict[str, str]] = ()`
and pass it through:
`render_user_data(user_name, sudo_group, ssh_key_line, mounts)`.

**2c. virtiofsd constant and preflight.** In `class Virtualizer`, add next to the other constants:

```python
    #: virtiofs daemon started by libvirt for `create --mount` (Debian/Ubuntu path).
    VIRTIOFSD = "/usr/libexec/virtiofsd"
```

Add this method after `require_domain_absent`:

```python
    def require_virtiofsd(self) -> None:
        """Raise unless the virtiofs daemon needed by ``--mount`` is installed."""
        if not os.access(self.VIRTIOFSD, os.X_OK):
            raise VirtError(
                f"preflight failed: {self.VIRTIOFSD} not found (needed by --mount) "
                "— install the 'virtiofsd' package",
                "virtiofsd-missing",
                "preflight",
            )
```

**2d. `create_vm`.** Add the parameter `mounts: list[dict[str, str]] = ()`, placed last. Build
`cmd` as today, then just before `if _passthrough(cmd).returncode != 0:` add:

```python
        if mounts:
            # virtiofs requires guest RAM shared with the virtiofsd process.
            cmd += ["--memorybacking", "source.type=memfd,access.mode=shared"]
        for m in mounts:
            cmd += [
                "--filesystem",
                f"source.dir={m['source']},target.dir={m['tag']},"
                f"driver.type=virtiofs,binary.path={self.VIRTIOFSD}",
            ]
```

### Step 3 — `src/virt_runner/cmd_create.py`

**3a. Imports.** Add `from pathlib import Path`.

**3b. Validator.** Put it next to `_validate_user`:

```python
_GUEST_PATH_RE = re.compile(r"/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")


def _validate_mounts(ctx, param, value):
    """Parse repeated ``--mount HOST[:GUEST]`` into source/target/tag dicts."""
    mounts: list[dict[str, str]] = []
    for i, spec in enumerate(value):
        host, _, guest = spec.partition(":")
        source = Path(host).expanduser().resolve()
        if not source.is_dir():
            raise click.BadParameter(f"'{host}' is not an existing directory")
        if "," in str(source):
            raise click.BadParameter(f"host path '{source}' must not contain ','")
        target = guest or f"/mnt/{source.name}"
        if not _GUEST_PATH_RE.fullmatch(target) or {".", ".."} & set(target.split("/")):
            raise click.BadParameter(
                f"invalid guest path '{target}': must be absolute, components "
                "[A-Za-z0-9._-], no '.' or '..'"
            )
        if any(m["target"] == target for m in mounts):
            raise click.BadParameter(f"guest path '{target}' is used twice")
        mounts.append({"source": str(source), "target": target, "tag": f"mount{i}"})
    return mounts
```

**3c. Option.** Add this decorator after `--ssh-key` and before `--no-boot`:

```python
@click.option(
    "--mount",
    "mounts",
    multiple=True,
    callback=_validate_mounts,
    metavar="HOST[:GUEST]",
    help="Share a host directory into the guest (virtiofs, read-write); "
    "repeatable [default GUEST: /mnt/<basename of HOST>]",
)
```

Add `mounts: list[dict[str, str]]` to the `cmd_create` signature, after `ssh_key`.

**3d. Run state.** Add `"mounts": mounts,` to the `run_state` dict.

**3e. Pipeline.**
- Right after `v.require_domain_absent(name)` add:
  ```python
          if mounts:
              v.require_virtiofsd()
  ```
- Pass `mounts=mounts` to `v.generate_cloud_init_files(...)` and to `v.create_vm(...)`.

**3f. JSON.** In `_create_document`, add `"mounts": run_state["mounts"],` to the
`fields["vm"]` dict, after `"disk_gib"`.

**3g. Text output.** Just before `for line in output.text_access_lines(name):` add:

```python
    for m in mounts:
        click.echo(f"Mount:   {m['source']} -> {m['target']}")
```

### Step 4 — `tests/unit_test.py`

Add these cases before `print("unit checks ok")`. Add the needed imports at the top
(`_validate_mounts` from `virt_runner.cmd_create`; `virt_runner.virtualizer as vz`).

```python
# _validate_mounts — default guest path, explicit guest path, tags by position.
share = Path(tempfile.mkdtemp(prefix="vr-share-"))
got = _validate_mounts(None, None, (str(share), f"{share}:/work/src"))
assert got == [
    {"source": str(share.resolve()), "target": f"/mnt/{share.name}", "tag": "mount0"},
    {"source": str(share.resolve()), "target": "/work/src", "tag": "mount1"},
], got
assert _validate_mounts(None, None, ()) == []

# _validate_mounts — each bad input is a usage error.
comma = Path(tempfile.mkdtemp(prefix="vr,share-"))
for bad in (
    ("/nonexistent/dir",),  # missing host dir
    (f"{share}:relative",),  # guest not absolute
    (f"{share}:/mnt/a b",),  # unsafe char
    (f"{share}:/mnt/..",),  # dot-dot component
    (f"{share}:/x", f"{share}:/x"),  # duplicate guest path
    (str(comma),),  # comma in host path
):
    try:
        _validate_mounts(None, None, bad)
        raise AssertionError(f"expected BadParameter for {bad}")
    except click.BadParameter:
        pass

# render_user_data — no mounts: no bootcmd; with mounts: idempotent virtiofs line.
assert "bootcmd" not in render_user_data("u", "sudo", "k")
ud = render_user_data("u", "sudo", "k", got)
assert "bootcmd:\n" in ud
assert "mount -t virtiofs mount1 /work/src" in ud
assert "mountpoint -q /work/src" in ud

# create_vm — mounts add shared memory + one --filesystem per mount.
captured = {}
real_pt = vz._passthrough
try:
    vz._passthrough = lambda cmd: (
        captured.setdefault("cmd", cmd),
        subprocess.CompletedProcess(cmd, 0),
    )[1]
    Virtualizer().create_vm(
        "n",
        1024,
        1,
        "vol",
        "52:54:00:00:00:01",
        "u",
        "m",
        "generic",
        no_boot=False,
        mounts=got,
    )
finally:
    vz._passthrough = real_pt
cmd = captured["cmd"]
assert "source.type=memfd,access.mode=shared" in cmd
assert sum(a == "--filesystem" for a in cmd) == 2
assert f"source.dir={share.resolve()},target.dir=mount0,driver.type=virtiofs," in (
    " ".join(cmd)
)

# require_virtiofsd — missing binary is virtiofsd-missing.
v2 = Virtualizer()
v2.VIRTIOFSD = "/nonexistent/virtiofsd"
try:
    v2.require_virtiofsd()
    raise AssertionError("expected VirtError")
except VirtError as e:
    assert e.code == "virtiofsd-missing", e.code
```

Notes for the implementer:
- `subprocess`, `tempfile`, `Path` and `click` are already imported inside `main()` further
  up. Put the new block **after** those imports, or move the imports to the top of the file.
- A `.` or `..` component can't get past `Path.resolve()` on the host side, but it can on
  the guest side, which is why the guest check exists.

### Step 5 — `tests/integration_test.py` (minimal e2e)

Add the mount to **all four** existing `create` calls. That covers the Ubuntu, Arch and
Fedora kernels and Fedora SELinux, and adds no extra VM boots.

1. At the top of `main()`, before the `try:`, create the share with a marker file:
   ```python
   share = Path(tempfile.mkdtemp(prefix="virt-runner-it-share-"))
   (share / "marker").write_text("from-host\n")
   ```
   (imports: `import shutil, tempfile` and `from pathlib import Path`)
2. Add `"--mount", f"{share}:/mnt/share"` to each of the four `create` invocations
   (NAMES loop, ARCH_NAME, FEDORA_NAME). Do **not** add it to the negative
   "already defined" call.
3. After each successful create, check:
   ```python
   check(
       doc["vm"]["mounts"]
       == [{"source": str(share.resolve()), "target": "/mnt/share", "tag": "mount0"}],
       f"{name} reports its mount",
   )
   ```
4. Replace `ssh_hello` with a small generic runner and keep the hello-world check on top of it:
   ```python
   def ssh_run(ssh_command: str, remote: str) -> str:
       """Run *remote* via the reported ssh_command (non-interactive); return stdout."""
       parts = shlex.split(ssh_command)
       cmd = (
           parts[:1]
           + [
               "-o",
               "BatchMode=yes",
               "-o",
               "StrictHostKeyChecking=no",
               "-o",
               "UserKnownHostsFile=/dev/null",
               "-o",
               "ConnectTimeout=10",
           ]
           + parts[1:]
           + [remote]
       )
       proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
       assert proc.returncode == 0, f"ssh failed ({' '.join(cmd)}): {proc.stderr}"
       return proc.stdout
   ```
   `ssh_hello(world, c)` becomes `assert world in ssh_run(c, f"echo {world}")`.
5. In step 3 of the test (the per-VM ssh loop), add a read and write check for each VM:
   ```python
   out = ssh_run(
       vm["ssh_command"], f"cat /mnt/share/marker && touch /mnt/share/from-{vm['name']}"
   )
   check("from-host" in out, f"{vm['name']} reads the host share")
   check((share / f"from-{vm['name']}").exists(), f"{vm['name']} writes to the host share")
   ```
6. In the existing `finally:` block, add `shutil.rmtree(share, ignore_errors=True)`.
7. Update the module docstring flow line: `... -> ssh hello world + shared-dir read/write in each -> ...`.

### Step 6 — docs

- **README.md**
  - Commands block: add `# --mount HOST[:GUEST] (repeatable; virtiofs share)` to the `create` comment.
  - Requirements line: mention `virtiofsd` (only needed for `--mount`; `install-prerequisites.sh` installs it).
  - Error-code table: add
    `| virtiofsd-missing | --mount given but /usr/libexec/virtiofsd is not installed |`.
    `errors.py` requires the README table to be updated for every new code.
  - Add a short section `## Shared directories` covering: the syntax and default guest path;
    that the mount is ready when `create` returns and persists across reboots; that it is
    read-write, UIDs pass through, and guest root is root on those files; the out-of-scope
    list from §3; and that `destroy` never touches the host dir.
- **CHANGELOG.md**: under `## [Unreleased]`, add an `### Added` entry for `create --mount`,
  `vm.mounts` in JSON, and the `virtiofsd-missing` error code.

### Step 7 — verify end to end

```bash
uv run tests/unit_test.py
uv run ruff check . && uv run ruff format --check .
uv run tests/integration_test.py        # needs virtiofsd (Step 1); takes a few minutes
```

Manual smoke test (also shows the text output):

```bash
mkdir -p ~/vr-share && echo hi > ~/vr-share/hello
uv run virt-runner create smoke --mount ~/vr-share
uv run virt-runner ssh smoke            # then: cat /mnt/vr-share/hello ; findmnt -t virtiofs
uv run virt-runner destroy smoke
uv run virt-runner create bad --mount /nope --json; echo "exit=$?"   # expect code usage, exit 2
```

## 6. Acceptance criteria

- [ ] `create --mount DIR[:GUEST]` (repeatable) boots a VM with each DIR mounted read-write at GUEST, on ubuntu, arch and fedora.
- [ ] The mount is usable as soon as `create` returns, and still mounted after `sudo reboot` in the guest.
- [ ] Bad `--mount` values exit 2 with `error.code == "usage"`. A missing virtiofsd exits 1 with `virtiofsd-missing` / stage `preflight`.
- [ ] `create --json` has `vm.mounts` (an empty list when there are no mounts). Creates without mounts produce the same virt-install command and user-data as before.
- [ ] `install-prerequisites.sh` installs `virtiofsd`, `--check` reports it as missing or installed, and the Verification section confirms `/usr/libexec/virtiofsd`.
- [ ] Unit and integration suites pass. README and CHANGELOG are updated.

## 7. Risks and troubleshooting (read if the integration test fails)

| Symptom | Likely cause → fix |
|---|---|
| `virt-install` fails: "Unable to find a satisfying virtiofsd" / binary not found | `virtiofsd` package not installed → `sudo apt-get install virtiofsd`. |
| Domain fails to start mentioning shared memory / vhost-user | The `--memorybacking` flag is missing or has a typo. Check with `virsh dumpxml NAME \| grep -A3 memoryBacking`. |
| Guest: `mount: unknown filesystem type 'virtiofs'` | The guest kernel lacks the module (`modinfo virtiofs` in the guest). Check `sudo cat /var/log/cloud-init.log \| grep -i bootcmd`. |
| Fedora only: `cat /mnt/share/marker` → Permission denied | Guest SELinux denies the virtiofs label. Confirm with `sudo ausearch -m avc -ts recent` in the guest. Fix: add a SELinux mount option to the bootcmd line, e.g. `-o context=system_u:object_r:nfs_t:s0`. Try it by hand in the guest first. Record the finding in `docs/lessons-learned/`. |
| Guest write → Permission denied on the host dir | The host dir is not writable for the guest's uid (usually 1000). This is ordinary DAC; check `ls -ln` on the host. |
| Mount missing right after `create` | `bootcmd` failed. Look at `/var/log/cloud-init-output.log` in the guest. Test the exact bootcmd line by hand. |

After implementing, write `docs/lessons-learned/005-virtiofs-mounts.md` for anything
non-obvious that came up (AGENTS.md rule), for example the Fedora SELinux result or why
`bootcmd` beats `mounts:`.
