# 15. A copied virt-runner disk ignores its new seed — seal before copying

Found while designing `snapshot create` / `create --from-snapshot` (verified on real VMs,
2026-10-28, see `docs/archive/plans/snapshot-generation-and-loading-research.md` §9).

## Symptom

Copy the disk of a running-and-provisioned VM, boot it as a new domain with a new MAC and a new
NoCloud seed, and the "clone":

- keeps the source's hostname, `/etc/machine-id` and SSH host keys,
- ignores the new seed completely (new hostname, new `--mount` lines: nothing applied),
- on Arch gets **no DHCP lease at all**; on Ubuntu 26.04 it does get one, through dracut's
  catch-all `zzzz-dracut-default.network`, so "no network" is not even a reliable symptom.

## Why

Three things, each enough on its own:

1. **virt-runner's own marker.** `render_user_data`'s `runcmd` writes
   `/etc/cloud/cloud-init.disabled` on the first boot, so the attached seed ISO is not re-read on
   every later boot. On a copy, that marker stops cloud-init before it ever compares
   instance-ids. A new instance-id alone does nothing.
2. **MAC-matched network config.** cloud-init's fallback config matches the first NIC by
   `macaddress` (netplan `match: macaddress` + `set-name`, networkd `[Match] MACAddress=`, NM
   keyfile). With a new MAC, nothing matches.
3. **machine-id → DHCP client-id.** systemd-networkd hashes the machine-id into its DUID, and
   dnsmasq prefers the client-id over the MAC. Two clones with one machine-id ask for the same
   lease.

## Fix

Seal the guest *before* the copy (`_SEAL_SCRIPT`, run with `sudo -n bash -s` over SSH): remove
the marker, `cloud-init clean --logs`, write `uninitialized` to `/etc/machine-id` (the same as
`cloud-init clean --machine-id`, which older cloud-init lacks), and strip virt-runner's
`mountN … virtiofs` fstab lines. Each clone's new seed then gets a full first boot: new host
keys, network config for the new MAC, hostname, growpart, and systemd derives a fresh machine-id
from the new domain's SMBIOS UUID. The clone's own `runcmd` writes the marker again, so it ends
up in exactly the state of a freshly created VM.

Two traps inside the fix:

- Use `uname -n`, not `hostname`, to record the hostname: the Arch cloud image ships no
  `hostname` binary, and under `set -e` the seal died with exit 127 before changing anything.
  Keep the script to coreutils, `sed` and `cloud-init`.
- Do not delete the SSH host keys in the seal. cloud-init rotates them on the new instance; if
  cloud-init ever failed on a clone, a key-less sshd would make it unreachable.

The seal mutates the source: its next boot also re-runs first-boot setup (new host keys; the
machine-id and IP stay, because its SMBIOS UUID did not change). That is why `snapshot create`
leaves the source shut off and says so.
