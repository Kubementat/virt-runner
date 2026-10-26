# 14 — What a live libvirt refusal looks like, and what a report field is for

Three doc-accurate-looking statements were each confirmed wrong by running them once — and a fourth
died on the smoke test before the commit.

## "live hot-plug works only up to `<vcpu max>`"

True, and incomplete in a way that misleads. Two live vCPU attempts on one guest, both after
reading that sentence:

```
virsh setvcpus NAME 2 --live      # 4 -> 2, XML was <vcpu>4</vcpu>   -> refused
error: unsupported configuration: failed to find appropriate hotpluggable vcpus
        to reach the desired target vcpu count

virt-runner modify NAME --vcpu 2 --live    # 1 -> 2, XML was <vcpu current='1'>4</vcpu> -> "now"
```

The difference is the live XML's `max`, not the direction and not a ceiling check: when `max`
equals `current` libvirt has no `hotpluggable='yes'` entries to work with and **no** live change is
possible. After a config write has raised the ceiling (`current='1'>4`), a live grow works. A live
shrink of the same domain stays refused, because the CPUs libvirt would remove are in use.

The trap that made the sentence wrong in practice: I concluded from the one refusal that vCPUs
can *never* change live on these guests, and wrote that down before testing the other direction.
The lesson is about the report, too. `set_vcpu` kept only `returncode`, so every refusal produced
the same `on-boot` plus "consider --reboot" and no way to tell a deliberate refusal from a missed
target. `virsh setvcpus NAME N --live` is the diagnostic that carries the sentence. Fixed by
returning `(ok, reason)` from the live attempt and recording it as `changes[].live_refused` and in
the warning.

And `"now"` lies for this axis in a second way. That successful live grow from 1→2 reported `now`,
the live XML said `current='2'`, and `nproc` stayed 1: the device was present
(`/sys/devices/system/cpu/present` = `0-1`) but `/sys/devices/system/cpu/cpu1/online` was `0` — the
guest kernel onlines CPUs while booting, not when one appears. So `"now"` for vCPU means
"libvirt enabled it", which is one step short of "the guest has it". Everything else the tool
reports the same way is accurate; this axis needs `--reboot --wait` plus `nproc`.

## "the MAC comes from the result document"

`--net-add` without `,mac=` leaves the address to libvirt, which picks it at attach time — so the
plan **and** the success document both said `"mac": ""`. The documented follow-up
(`jq -r '.changes[0].to.mac'`) returns an empty string, and the next `--net-remove` then fails on
a malformed MAC. `virsh domiflist` was the only place the address existed.

Fixed by generating the MAC while planning (`52:54:00` + 3 random bytes, same OUI as `create`) and
passing it to `attach-interface`. Lesson for the rest of the tool: **a field that a caller has to
act on must be known before the change is described**, otherwise the document describes a fact it
has not yet produced.

## "`list` reports state, MAC and disk"

`vm-list`'s document had no sizing keys at all. `jq '{vcpu, ram_gib, disk_gib}'` printed
`null` for each — and I reported that as "`list` returns nulls", which was a *second*, self-inflicted
error: **jq invents absent keys as null**, so a query can appear to verify a field that was never
in the document. The first query against an unknown document is `| keys`.

Same class of mistake as grepping `<memory` and reading the live XML: a live domain is two
documents, and a balloon change moves `<currentMemory>` while `<memory>` stays as the ceiling.
`virsh dumpxml` shows the live one; `--inactive` is what the next boot gets. Both are worth
checking before telling anyone a change has landed.
