"""CLI entry point — click-based subcommand router.

Usage:
    uv run virt-runner create <NAME> [OPTIONS] [--json]
    uv run virt-runner destroy <NAME> [--json]
    uv run virt-runner list [OPTIONS] [--json]
    uv run virt-runner ssh <VM_NAME> [--json]

``--json`` (all subcommands) makes stdout carry exactly one JSON document and
prints nothing else; see :mod:`virt_runner.output` and spec §5.

Exit codes: 0 = success, 1 = runtime/preflight failure, 2 = usage error.
"""

from __future__ import annotations

import click

from virt_runner.cmd_create import cmd_create
from virt_runner.cmd_destroy import cmd_destroy
from virt_runner.cmd_list import cmd_list
from virt_runner.cmd_ssh import cmd_ssh


@click.group()
def main():
    """virt-runner — one-command Ubuntu KVM VM creation via libvirt + cloud-init."""


main.add_command(cmd_create, "create")
main.add_command(cmd_destroy, "destroy")
main.add_command(cmd_list, "list")
main.add_command(cmd_ssh, "ssh")


if __name__ == "__main__":
    main()
