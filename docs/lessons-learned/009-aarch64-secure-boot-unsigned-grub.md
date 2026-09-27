# aarch64 + Secure Boot: unsigned guests hang in a firmware dialog, no console output

aarch64 domains always boot UEFI. On this host libvirt's default firmware is
`/usr/share/AAVMF/AAVMF_CODE.ms.fd` — the **Secure Boot** build (enrolled
Microsoft CA keys; `<feature enabled='yes' name='secure-boot'/>` in the
domain XML). Guests whose bootloader is not signed for that key database do
not fail — they **hang**:

```
BdsDxe: loading Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x2,0x0)
BdsDxe: starting Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x2,0x0)
Verification failed: (0x1A) Security Violation
```

…followed by a "Press OK to continue" dialog that waits forever for a
keypress. Symptoms that made this devious:

- the domain stays `running` and burns almost no CPU (firmware idle-wait),
- `virsh console` shows **nothing** — the dialog was printed before any
  console attach, so attaching later sees a silent session,
- no DHCP lease ever appears → `create` ends in `lease-timeout` at
  `wait-ip`, which points the user at `virsh console` for an empty console.

Fedora's aarch64 cloud image ships an **unsigned GRUB** (Fedora's Secure
Boot support is x86_64-only), so it hits this on any Secure-Boot-enabled
firmware. Ubuntu's cloud image carries a Canonical-signed GRUB whose key is
in Microsoft's CA database, so it boots fine — which is why the first real
aarch64 run (Ubuntu legs) masked the problem until the Fedora leg.

Repro that revealed it: boot the extracted image directly with
`qemu-system-aarch64 -pflash AAVMF_CODE.ms.fd …` — the Security Violation
dialog appears on serial immediately. The same image boots and gets a DHCP
lease within a minute from `AAVMF_CODE.no-secboot.fd`.

Fix: virt-runner creates aarch64 domains with Secure Boot disabled —
`virt-install --boot uefi=on,loader=<no-secboot firmware>` (the firmware
path is looked up in `virsh domcapabilities --arch aarch64` and must contain
the `no-secboot` marker; `None` keeps virt-install's default, which is the
right behavior on hosts whose only UEFI build is already non-secure). These
are throwaway dev VMs from generic cloud images; a per-VM Secure Boot knob
is YAGNI. Note the virt-install 4.1.0 `--boot` parser rejects the
`firmware.feature.N.*` form, and a bare `loader=` (without `uefi=on`)
produces old-style UEFI XML that libvirt refuses on aarch64 ("ACPI requires
UEFI on this architecture") — the `uefi=on,loader=…` combination is what
emits `firmware='efi'` plus the paired non-secure NVRAM template.

Discovered while bringing up aarch64 host support (2026-09-27): the Fedora
leg of the integration suite timed out in wait-ip with a silent,
near-idle domain, while both Ubuntu legs passed.
