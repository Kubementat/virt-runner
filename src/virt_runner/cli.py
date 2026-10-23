"""CLI entry point — click-based subcommand router.

Usage:
    uv run virt-runner create <NAME> [OPTIONS] [--json]
    uv run virt-runner destroy <NAME> [--json]
    uv run virt-runner list [OPTIONS] [--json]
    uv run virt-runner ssh <VM_NAME> [--json]
    uv run virt-runner setup [--check] [--with-virt-manager]
    uv run virt-runner --version

Short aliases: ``c`` = create, ``d`` = destroy, ``ls`` = list, ``s`` = ssh.

``setup`` is the odd one out: it is not a VM command, it is not covered by the
``--json`` contract, and it is the only subcommand that needs sudo (see
:mod:`virt_runner.cmd_setup`).

``--json`` (all subcommands) makes stdout carry exactly one JSON document and
prints nothing else; see :mod:`virt_runner.output` and spec §5.

Exit codes: 0 = success, 1 = runtime/preflight failure, 2 = usage error.
"""

from __future__ import annotations

import click

from virt_runner.cmd_create import cmd_create
from virt_runner.cmd_destroy import cmd_destroy
from virt_runner.cmd_list import cmd_list
from virt_runner.cmd_modify import cmd_modify
from virt_runner.cmd_setup import cmd_setup
from virt_runner.cmd_ssh import cmd_ssh

#: Short forms accepted instead of the full subcommand name.
ALIASES = {
    "c": "create",
    "d": "destroy",
    "ls": "list",
    "s": "ssh",
    "m": "modify",
}


class AliasGroup(click.Group):
    """Group that also dispatches on :data:`ALIASES`.

    Only name *lookup* is aliased: ``--help`` still lists every command once,
    and the JSON envelope keeps reporting the canonical ``tool`` id.
    """

    def get_command(self, ctx: click.Context, cmd_name: str):
        return super().get_command(ctx, ALIASES.get(cmd_name, cmd_name))


@click.group(cls=AliasGroup)
@click.version_option(package_name="virt-runner", prog_name="virt-runner")
def main():
    """virt-runner — one-command KVM VM creation (Ubuntu, Arch, Fedora) via libvirt + cloud-init.

    Short aliases: c=create, d=destroy, ls=list, s=ssh. Run `setup` once per host
    to install and configure the KVM/libvirt prerequisites.
    """


main.add_command(cmd_create, "create")
main.add_command(cmd_destroy, "destroy")
main.add_command(cmd_list, "list")
main.add_command(cmd_modify, "modify")
main.add_command(cmd_ssh, "ssh")
main.add_command(cmd_setup, "setup")


if __name__ == "__main__":
    main()
