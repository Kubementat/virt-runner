# Bug 001 — `virsh vol-upload` silently shrinks the VM disk to the source image size

| | |
|---|---|
| **Severity** | High (data-plane contract violation: `--disk N` does not produce an N GiB disk) |
| **Component** | `bin/vm-create`, step 4 (pool volume import) |
| **Discovered** | 2026-08-25 |
| **Fixed** | 2026-08-26 |
| **Status** | Fixed and verified end-to-end |

---

## 1. Symptom

Created VMs with an explicit 10 GiB disk:

```
bin/vm-create test-1 --ram 2 --vcpu 1 --disk 10
bin/vm-create test-2 --ram 2 --vcpu 1 --disk 10
```

Both VMs booted and were reachable via SSH, but inside the guest the disk was
~3.5 GiB, not 10 GiB:

```
$ df -h
Filesystem      Size  Used Avail Use% Mounted on
/dev/vda1       2.3G  2.0G  333M  86% /
/dev/vda13      989M  106M  816M  12% /boot
/dev/vda15      105M  6.3M   99M   7% /boot/efi
```

Nothing failed: the script exited 0, printed success output, and the VM
"worked" — the wrong size was only visible from inside the guest.

## 2. Environment

| Item | Value |
|---|---|
| Host | Ubuntu, KVM, libvirt **10.0.0** (QEMU 8.2.2) |
| `virt-install` | 4.1.0 |
| Storage pool | `vm-pool` (dir pool at `/var/lib/libvirt/images/vms`) |
| Guest image | `resolute-server-cloudimg-amd64.img` from `https://cloud-images.ubuntu.com/resolute/current/` |
| Affected VMs | `test-1`, `test-2` (3.5 GiB disks instead of 10 GiB) |

## 3. Investigation trail

1. **Guest disk size check** — `virsh domblkinfo test-1 vda`:

   ```
   Capacity:  3758096384   (= 3.50 GiB, not 10 GiB)
   ```

   The *host-side* volume is already wrong, so the bug is in `vm-create`, not
   in the guest.

2. **Volume inspection** — `virsh vol-info test-1_vda.qcow2 vm-pool`:

   ```
   Capacity:  3.50 GiB
   ```

   So `virsh vol-create-as ... --capacity 10G` was evidently not the last
   word on the volume size.

3. **Isolating `vol-create-as`** — ran it in isolation:

   ```
   $ virsh vol-create-as vm-pool dbg_vda.qcow2 --capacity 10G --format qcow2
   $ virsh vol-info dbg_vda.qcow2 vm-pool | grep -i cap
   Capacity:  10.00 GiB      ← correct
   ```

   Volume creation is fine.

4. **Reproducing with the actual script** — `bin/vm-create dbg-disk-test
   --ram 2 --vcpu 1 --disk 10 --no-boot` produced a **3.50 GiB** volume.
   The bug is inside the script's create-then-import sequence.

5. **Step-by-step bisection** (the decisive experiment):

   ```
   $ virsh vol-create-as vm-pool step_vda.qcow2 --capacity 10G --format qcow2
   $ virsh vol-info step_vda.qcow2 vm-pool | grep -i capacity
   Capacity:  10.00 GiB
   $ virsh vol-upload --pool vm-pool step_vda.qcow2 ~/vm-images/resolute/resolute-server-cloudimg-amd64.img
   $ virsh vol-info step_vda.qcow2 vm-pool | grep -i capacity
   Capacity:  3.50 GiB        ← shrunk by vol-upload
   ```

   **`virsh vol-upload` itself shrank the volume** from 10.00 GiB to
   3.50 GiB.

6. **Where did 3.5 GiB come from?**

   ```
   $ qemu-img info ~/vm-images/resolute/resolute-server-cloudimg-amd64.img
   image: ...resolute-server-cloudimg-amd64.img
   file format: qcow2
   virtual size: 3.5 GiB (3758096384 bytes)
   disk size:    821 MiB
   ```

   The "raw" cloud image file in the cache is actually a **qcow2 with a
   3.5 GiB virtual size**. `virsh vol-upload`'s default behavior is to
   resize the destination volume to the source size — for a qcow2 source,
   that is its *virtual* size.

   Note: the re-downloaded image (after wiping the cache) is qcow2 as well,
   **and its SHA256 matched the official `SHA256SUMS`** — so the canonical
   URL genuinely serves a qcow2 file in this environment. The cache was not
   "corrupted"; the script's "raw image" assumption was wrong.

## 4. Root cause

Two compounding facts:

1. **`virsh vol-upload` resizes the destination volume to the source size by
   default**, with no flag in libvirt 10.0.0's `virsh` CLI to disable it
   (`virsh vol-upload --help` only shows `--offset`, `--length`, `--sparse`;
   the newer libvirt CLIs add `--resize`):

   ```
   vol-upload <vol> <file> [--pool <string>] [--offset <number>] [--length <number>] [--sparse]
   ```

   The upstream `virt-install` man page documents the equivalent default for
   its own import path ("by default, the volume is resized to match the
   source"), which is another signal this is expected tooling behavior — not
   a libvirt malfunction.

2. **The script never re-checked the volume capacity after import.** The
   pipeline was: `vol-create-as --capacity ${DISK}G` → `vol-upload` →
   `virt-install --disk vol=…` — with no assertion that the volume still has
   the contracted size. Since `virt-install` (per its man page: *`vol`: an
   existing libvirt storage volume to use*) happily consumes the shrunken
   volume, every stage downstream "succeeded" and the wrong size propagated
   silently into the guest.

## 5. The fix

`bin/vm-create`, step 4 — restore the contracted capacity explicitly after
the upload:

```bash
if ! virsh vol-upload --pool "$POOL" "$VOLUME" "$IMAGE_LOCAL_PATH" >/dev/null 2>&1; then
  virsh --quiet vol-delete "$VOLUME" "$POOL" 2>/dev/null || true
  die "failed to import image into pool volume '${VOLUME}'"
fi

# DEVIATION (bugfix, verified live on this host, libvirt 10.0.0): `virsh
# vol-upload` RESIZES the destination volume to the source size by default,
# and this libvirt's virsh has NO --resize flag to disable it (the libvirt
# API supports --resize=no, but the CLI exposes it only in newer versions).
# With a 3.5G qcow2 source image a --disk 10 volume silently shrank to 3.5G.
# Restore the contracted capacity explicitly.
if ! virsh vol-resize "$VOLUME" "${DISK}G" "$POOL" >/dev/null 2>&1; then
  die "failed to resize pool volume '${VOLUME}' to ${DISK}G"
fi
```

Why `vol-resize` after the upload instead of `--resize=no`:

- the installed `virsh` (libvirt 10.0.0) **does not expose `--resize`** on
  `vol-upload`, so the intended one-flag fix was unavailable;
- `vol-resize` to `${DISK}G` is available and yields the identical end
  state, regardless of the source image's format or virtual size (raw,
  qcow2, whatever the URL serves);
- it makes the post-condition explicit: *after this step, the volume is
  exactly `${DISK}G`*.

Side effect worth noting: with a qcow2 source larger than the requested
disk (e.g. 3.5G image, `--disk 1`), the upload itself would already fail
(volume too small) before the resize — a clear, early failure, which is
acceptable for this PoC.

## 6. Verification (end-to-end)

1. Cache reset: `rm ~/vm-images/resolute/{resolute-server-cloudimg-amd64.img,SHA256SUMS}`
   → re-downloaded and SHA256-verified by the script.
2. `bin/vm-create dbg-disk-test --ram 2 --vcpu 1 --disk 10` → exit 0.
3. Host side: `virsh vol-info dbg-disk-test_vda.qcow2 vm-pool` →
   **Capacity: 10.00 GiB** ✓
4. Guest side (SSH):

   ```
   $ lsblk
   vda     253:0    0   10G  0 disk
   ├─vda1  253:1    0 8.9G  0 part /
   ├─vda13 ...      1023M  0 part /boot
   └─vda15 ...      106M  0 part /boot/efi
   $ df -h /
   /dev/vda1   8.6G  2.0G  6.7G  23% /
   ```

   Guest sees a 10G disk and cloud-init/growpart auto-expanded the root
   partition ✓
5. Test VM destroyed and volume removed — no artifacts left behind.

## 7. Remediation of already-affected VMs

Existing VMs (e.g. `test-1`, `test-2`) keep their shrunken disks until
migrated. Two options:

- **Recreate (clean):** `bin/vm-destroy <vm> && bin/vm-create <vm> [same options]`
- **Live resize:** `virsh vol-resize <vm>_vda.qcow2 <N>G vm-pool`, then in
  the guest: `sudo growpart /dev/vda 1 && sudo resize2fs /dev/vda1`

## 8. Lessons learned

1. **Never trust an intermediate artifact's state — assert the post-condition.**
   The script assumed "I created a 10G volume, therefore the volume is 10G"
   after a third-party command that had no contractual obligation to preserve
   it. Any step that hands state to an external tool needs a verification
   step afterward (`virsh vol-info | grep Capacity` compared to `${DISK}G`,
   or — as implemented — an explicit restoring `vol-resize`).

2. **Silent success is the most dangerous failure mode for provisioning
   tools.** Every stage exited 0; the bug was only observable *inside the
   guest*. Contract checks that live at the boundary (host → guest, here:
   disk size visible via `df`/`lsblk`) should be part of acceptance
   testing, not just "VM boots and SSH works".

3. **Read the default behaviors of the tools in the middle of the pipeline,
   not just their happy paths.** `vol-upload`'s default of resizing the
   destination to the source size is documented upstream behavior — but it
   was invisible in our flow because we only tested "did the command
   succeed?". When composing tools, enumerate each one's *side effects*
   (truncate, resize, overwrite, chown…), not just its outputs.

4. **File extensions are a contract with zero enforcement.** A
   `….img` that is actually qcow2 (with a *virtual* size different from its
   on-disk size) broke the "byte copy of a raw image" assumption baked into
   the design comments. Verify formats with `file`/`qemu-img info`, and
   make the pipeline robust to either (the `vol-resize` fix makes disk size
   independent of the source format entirely).

5. **CLI capability varies across libvirt versions.** The "obvious" fix
   (`vol-upload --resize=no`) does not exist in libvirt 10.0.0's `virsh`;
   the flag exists only in newer CLIs. Always check the *installed* tool's
   `--help` before writing a fix, and prefer workarounds (`vol-resize`)
   that work across versions.

6. **Bisect the pipeline, don't blame the script.** The investigation went:
   guest symptom → host volume state → isolate `vol-create-as` (fine) →
   reproduce via full script (fails) → run the two sub-steps manually with
   a state check between them (catches `vol-upload` in the act). Inserting a
   state observation *between* pipeline steps turned a "the script is
   somehow wrong" mystery into a two-minute identification.
