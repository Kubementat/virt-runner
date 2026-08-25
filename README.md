# virt-runner

POC for Linux virtualization research (see specification/).

- `bin/vm-create` — download a cached Ubuntu cloud image (SHA256-verified,
  reused on later runs), generate cloud-init files, and create+boot a KVM
  VM on the libvirt `default` NAT network with its disk in the `vm-pool`
  storage pool. Waits for the DHCP lease, verifies SSH, then prints the
  access info.
- `bin/vm-destroy` — destroy+undefine the VM and delete its disk volume
  (`<NAME>_vda.qcow2` from `vm-pool`). No prompt; idempotent if the
  volume is already gone.

## How to run

Both scripts are plain bash; make sure the user is in the `libvirt` group
and that `~/vm-images` is writable (image cache). Examples:

```console
# Create a 2 GiB / 1 vCPU / 10 GiB VM named poc-1 from the default release:
bin/vm-create poc-1 --ram 2 --vcpu 1 --disk 10

# A different release, explicit image, custom user:
bin/vm-create poc-2 --release noble --user ubuntu --ssh-key ~/.ssh/id_ed25519.pub

# Tear it down (VM + disk):
bin/vm-destroy poc-1
```

See `specification/specification.md` §3 for the full CLI contract and
`docs/e2e-acceptance.md` for the full end-to-end acceptance evidence.
