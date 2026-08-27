# Ticket 03 — S3: Cloud-init user-data / meta-data generation

**Slice:** S3 (plan §2)
**Goal:** Extend `bin/vm-create` (committed by ticket 02) with `generate_cloud_init_files()`: write correct NoCloud `user-data` and `meta-data` YAML files (hostname, fresh lowercase UUID, `--user` propagation, injected SSH key, `manage_etc_hosts: true`, `package_update: false`, **no** `network:` section) into a `mktemp -d` dir with `chmod 600` and trap cleanup, per spec §4 step 3 + decisions D2/D3. The stub after cloud-init now reports the temp dir, then exits 1.

## Context

- **Spec sections:** §4 step 3 (file contents, temp-dir handling, rationale bullets), §3.1 (`--user` default `ubuntu`), decisions **D2** (`--user` is honored end-to-end: user-data `users:` block, SSH verification, printed output) and **D3** (YAML indentation: `local:` and `host:` at the *same* level under `hostnames:` — the research doc had a stray-indent bug; do not repeat it), §6 pitfalls 4 & 5 (no bare `--cloud-init` later; no hardcoded interface names / no network-config ever).
- **Plan sections:** §2 slice S3 (scope, verification via a test-only dump hook), §3 (commit message), §4 safety rules.
- **Environment facts:** `python3` and PyYAML are available on this host for YAML validation (`python3 -c 'import yaml'` — if PyYAML is actually missing, fall back to a structural grep check and record that in the commit message). `uuidgen` is at `/usr/bin/uuidgen`.
- The SSH key file for tests: use `~/.ssh/id_rsa.pub` (the default `~/.ssh/id_ed25519.pub` does not exist yet; ticket 05 generates it).

## Deliverables

- **Modify** `bin/vm-create`:
  - New function `generate_cloud_init_files()` producing, in a fresh `TMP="$(mktemp -d)"` directory:
    - `meta-data`:
      ```yaml
      id: <random lowercase uuid>
      hostnames:
        local: <NAME>
        host: <NAME>
      ```
      (`id` = `uuidgen | tr A-Z a-z`, fresh per invocation; `local:`/`host:` at identical indentation per D3.)
    - `user-data`:
      ```yaml
      #cloud-config
      manage_etc_hosts: true
      users:
        - name: <USER>            # the --user value, default ubuntu
          groups: [sudo]
          sudo: ["ALL=(ALL) NOPASSWD:ALL"]
          ssh_authorized_keys:
            - <contents of --ssh-key file, single line>
      package_update: false
      ```
    - **No `network:` key anywhere; no network-config passed to anything.**
  - Temp-dir hygiene: `mktemp -d` + `chmod 700` the dir, `chmod 600` both files, `trap 'rm -rf "$TMP"' EXIT`. Never a static path.
  - A **test-only dump hook** so the files can be inspected before the trap deletes them: pick one and document it in the script header — either `VM_CREATE_CLOUD_INIT_DUMP=/tmp/path` (copy both files into that dir before exit) or a `--debug` flag. Record which you chose.
  - The stub after cloud-init now prints the temp dir / "cloud-init files generated" info, then exits 1 (virt-install arrives in ticket 04).

## Implementation requirements

1. `--user` (default `ubuntu`) must flow into `users: - name:` exactly; a non-default `--user` (e.g. `tester`) must appear in the file (D2).
2. The `ssh_authorized_keys` entry is the full single-line content of the `--ssh-key` file (read it with `cat`/`$(<file)`; the file is guaranteed non-empty by ticket 01's preflight).
3. `id` must be a fresh lowercase UUID on **every** run — two consecutive runs must produce different `id:` values.
4. `manage_etc_hosts: true` and `package_update: false` must be present verbatim.
5. Files must be valid YAML (parseable by `yaml.safe_load`).
6. The dump hook copies the files *before* the exit trap fires; without the hook, no temp dir may survive.
7. Keep `set -euo pipefail`, the exit-code contract (0/1/2), and ticket 01/02 behavior unchanged.

## Safety rules

- Any VM name used in tests: `poc-` prefix (e.g. `poc-s3`). No VM or disk is created by this ticket (no virt-install yet).
- All fixtures/dumps live under `/tmp/virt-test-s3/` and are removed (`rm -rf /tmp/virt-test-s3`) before finishing.
- Never touch `setup-test-vm`, the pre-existing Noble volume, `/var/lib/libvirt/`, or `~/vm-images`.
- No `sudo`.

## Acceptance criteria

Use the dump hook (assuming the chosen hook is `VM_CREATE_CLOUD_INIT_DUMP`; adapt if you chose `--debug`) with a fixture key file so key-content assertions are deterministic:

```bash
mkdir -p /tmp/virt-test-s3
echo 'ssh-ed25519 AAAA_TEST_KEY poc-test' > /tmp/virt-test-s3/testkey.pub
```

1. **YAML validity:** generate once (`VM_CREATE_CLOUD_INIT_DUMP=/tmp/virt-test-s3/run1 bin/vm-create poc-s3 --ssh-key /tmp/virt-test-s3/testkey.pub`), then:
   `python3 -c 'import yaml,sys; yaml.safe_load(open(sys.argv[1]))' /tmp/virt-test-s3/run1/meta-data` → exit 0, and the same for `user-data`.
2. **meta-data content:** `id:` matches `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` (lowercase UUID); both `hostnames.local` and `hostnames.host` equal `poc-s3`.
3. **user-data content:** starts with `#cloud-config`; contains `name: ubuntu` (default) and the exact line `- ssh-ed25519 AAAA_TEST_KEY poc-test`; contains `sudo: ["ALL=(ALL) NOPASSWD:ALL"]` and `package_update: false`; and `grep -c 'network:' user-data` → **0** (no network section at all).
4. **`--user` propagation (D2):** re-run with `--user tester` into `run2` → `name: tester` present in `user-data`.
5. **Fresh UUID per run:** compare `id:` values from `run1/meta-data` and `run2/meta-data` → different.
6. **No leaked temp dirs:** capture `ls /tmp` (or `ls -d /tmp/tmp.*` count) before and after a run **without** the dump hook → no new temp dirs survive (trap works).
7. Exit code of every generation run is **1** (stub after cloud-init) with the not-implemented message.

## Verification (run in this order)

1. Create the fixture key file as in the "Acceptance criteria" setup block.
2. Run criteria 1 → 5 (two generation runs into `run1`/`run2`, then grep/python assertions).
3. Run criterion 6 (temp-dir leak check).
4. Teardown: `rm -rf /tmp/virt-test-s3` and confirm gone.
5. `git status --short` shows only `bin/vm-create` modified.
6. **No real VM creation is required for this ticket.**
7. If all pass: `git add -A && git commit -m "feat(vm-create): generate cloud-init user-data and meta-data (ticket 03)"`

## Definition of done

- All acceptance criteria pass; verification sequence ran successfully; `/tmp/virt-test-s3` removed.
- Working tree committed with exactly: `git add -A && git commit -m "feat(vm-create): generate cloud-init user-data and meta-data (ticket 03)"`

## Pitfalls (spec §6 / plan §2, S3-relevant)

- **YAML indentation (D3):** `hostnames:` children at one consistent indent; the research doc's stray indent produced invalid NoCloud — this is the classic failure for this step.
- **No `network:` section / no network-config (pitfall 5):** Ubuntu 26.04 uses predictable `ens*`/`enp*` names and has a known rename bug (canonical/cloud-init#6887). Rely on the image's default DHCP-on-first-NIC. Never hardcode interface names.
- **`--user` propagation (D2):** the user-data `users:` block, the later SSH probe (ticket 05), and the printed output must all agree on `$USER_NAME`.
- **SSH key as YAML content:** the key line goes verbatim as a single-line YAML scalar item under `ssh_authorized_keys:` — multiline or malformed key content breaks the file; preflight already guarantees a non-empty single-line `.pub` file.
- **Trap vs dump hook ordering:** the dump copy must happen before the EXIT trap removes `$TMP`.
