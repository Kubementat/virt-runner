"""``list`` subcommand — list VMs whose disk lives in the vm-pool."""

from __future__ import annotations

import os
from typing import Any

import click

from virt_runner import output
from virt_runner.args import command
from virt_runner.virtualizer import IP_STATUS_LEASE, Virtualizer, VmInfo


def _vm_document(vm: VmInfo, display_user: str) -> dict[str, Any]:
    """JSON view of one ``list_vms`` entry (spec §5.3.3).

    Only a leased address is reported: ``ip``/``ssh_user``/``ssh_command`` are
    null unless ``ip_status`` is ``"lease"``, and ``ip_status`` says why — no
    need to parse a placeholder sentence.
    """
    leased = vm.ip_status == IP_STATUS_LEASE
    ip = vm.ip or None if leased else None
    ssh_user = display_user if leased else None
    return {
        "name": vm.name,
        "uuid": vm.uuid,
        "state": vm.state,
        "mac": vm.mac or None,
        "ip": ip,
        "ip_status": vm.ip_status,
        "ssh_user": ssh_user,
        **output.access_commands(vm.name, ssh_user, ip),
    }


def _text_block(vm: VmInfo, display_user: str) -> list[str]:
    """The §4.6 block of one VM: headline/Name/IP/SSH/Console/Teardown.

    Reads the same :class:`~virt_runner.virtualizer.VmInfo` as
    :func:`_vm_document`, so the two renderings cannot drift apart.
    """
    if vm.running:
        headline = "VM running."
        if vm.ip:
            ip_line = f"IP:      {vm.ip}   (also reachable as {vm.name}.default)"
            ssh_line = f"SSH:     ssh {display_user}@{vm.ip}"
        else:
            ip_line = "IP:      (no DHCP lease found yet)"
            ssh_line = "SSH:     (unavailable — no IP yet)"
    else:
        headline = f"VM {vm.state}."
        ip_line = "IP:      (no DHCP lease — VM not running)"
        ssh_line = f"SSH:     (unavailable — start the VM first: virsh start {vm.name})"

    return [
        headline,
        f"Name:    {vm.name}   (UUID {vm.uuid})",
        ip_line,
        ssh_line,
        f"Console: virsh console {vm.name}     (Ctrl-] to detach)",
        (
            f"Teardown: vm-destroy {vm.name}   "
            f"(or: virsh destroy {vm.name} && virsh undefine {vm.name})"
        ),
    ]


@command(name="list")
@click.option(
    "--user",
    default=Virtualizer.DEFAULT_USER,
    help="User name shown in the SSH command [default: ubuntu]",
)
def cmd_list(user: str, as_json: bool) -> None:
    """List VMs configured by vm-create (disk in the vm-pool)."""
    output.set_json_mode(as_json)
    v = Virtualizer()

    # Preflight, narrowed to what listing needs: libvirt reachable and the
    # pool *defined* (an inactive pool still has listable VMs). The network is
    # irrelevant here, so it is not checked.
    try:
        v.libvirt_reachable()
        v.pool_exists()

        lease_file = os.environ.get(
            "VM_LIST_LEASE_FILE",
            os.environ.get(
                "VM_CREATE_LEASE_FILE",
                Virtualizer.DEFAULT_LEASE_FILE,
            ),
        )
        vms = v.list_vms(lease_file=lease_file)
    except RuntimeError as exc:
        output.fail_with("list", exc, "pool-not-found")

    if as_json:
        # An empty listing is a success; the text-mode hint line is simply
        # not printed (the document carries count: 0).
        output.emit(
            "list",
            {
                "pool": v.POOL,
                "display_user": user,
                "count": len(vms),
                "vms": [_vm_document(vm, user) for vm in vms],
            },
        )
        return

    if not vms:
        click.echo(
            f"No VMs configured in pool '{v.POOL}' (vm-create <name> to create one)."
        )
        return

    for vm in vms:
        for line in _text_block(vm, user):
            click.echo(line)
