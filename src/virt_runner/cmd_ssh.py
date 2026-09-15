"""``ssh`` subcommand — interactive shell session on a VM."""

from __future__ import annotations

import os

import click

from virt_runner import output
from virt_runner.args import command
from virt_runner.virtualizer import Virtualizer


@command(name="ssh")
@click.argument("name")
@click.option(
    "--user",
    default=Virtualizer.DEFAULT_USER,
    help="User name to log in as [default: ubuntu]",
)
def cmd_ssh(name: str, user: str, as_json: bool) -> None:
    """Open an interactive SSH shell on VM <NAME>."""
    output.set_json_mode(as_json)
    v = Virtualizer()
    identity = Virtualizer.private_key_path(Virtualizer.DEFAULT_SSH_KEY)

    try:
        v.libvirt_reachable()
        if not v.domain_exists(name):
            output.fail(
                "ssh",
                f"VM '{name}' is not defined",
                code="vm-not-defined",
                text=f"VM '{name}' is not defined",
                fields={"name": name},
            )
        state = v.get_domain_state(name)
        if state != "running":
            output.fail(
                "ssh",
                f"VM '{name}' is {state} — start it first: virsh start {name}",
                code="vm-not-running",
                fields={"name": name, "state": state},
            )
        lease_file = os.environ.get(
            "VM_SSH_LEASE_FILE",
            os.environ.get(
                "VM_CREATE_LEASE_FILE",
                Virtualizer.DEFAULT_LEASE_FILE,
            ),
        )
        ip = v.vm_ip(name, lease_file)
        if not ip:
            output.fail(
                "ssh",
                f"no IP found for VM '{name}' — check: virsh domifaddr {name}",
                code="no-ip",
                fields={"name": name},
            )
    except RuntimeError as exc:
        output.fail_with("ssh", exc, "libvirt-unreachable", fields={"name": name})

    code = v.ssh_shell(user, ip, identity)

    if as_json:
        output.emit(
            "ssh",
            {
                "name": name,
                "ip": ip,
                "ssh_user": user,
                "exit_code": code,
                **output.access_commands(name, user, ip, identity),
            },
        )
    if code != 0:
        raise SystemExit(code)
