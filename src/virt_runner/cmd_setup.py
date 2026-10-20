"""``setup`` subcommand — run the host-prerequisite installer without a checkout.

``virt-runner`` needs a KVM/libvirt host that a Python package cannot configure.
The installer script therefore ships inside the wheel as package data, so
``uvx virt-runner create myvm`` and ``uv tool install virt-runner`` users get the
same one-time setup a source checkout gets, pinned to the installed version.

The command is deliberately thin: it locates the script and execs ``bash`` on it
with stdio and environment inherited, then returns the script's exit code. Every
flag the installer understands (`--check`, `--with-virt-manager`) is passed
through, and its `VIRT_USERNAME` / `VIRT_POOL_NAME` / `VIRT_POOL_DIR` /
`VIRT_NET_NAME` variables are read from the environment as usual.

Unlike the VM commands, ``setup`` is not part of the ``--json`` contract: it is a
privileged host operation, not a VM lifecycle operation, and it is meant to be
watched while it runs.
"""

from __future__ import annotations

import subprocess
from importlib import resources
from pathlib import Path

import click

SCRIPT_NAME = "install-prerequisites.sh"


def prerequisite_script() -> Path | None:
    """Absolute path of the bundled installer, or ``None`` when not available.

    Resolution order matters: the wheel keeps the script in the package tree
    (``virt_runner/resources/``), while a source checkout runs against an
    editable install that only maps ``src/virt_runner`` — there the script still
    lives at the repository root, two levels above this file.

    Returns:
        The script path when one exists, else ``None``.
    """
    bundled = resources.files("virt_runner").joinpath("resources", SCRIPT_NAME)
    if bundled.is_file():
        return Path(str(bundled))
    for parent in Path(__file__).resolve().parents:
        candidate = parent / SCRIPT_NAME
        if candidate.is_file():
            return candidate
    return None


@click.command("setup")
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Audit only: report what is missing and what would change. Needs no sudo.",
)
@click.option(
    "--with-virt-manager",
    is_flag=True,
    help="Also install the virt-manager GUI.",
)
def cmd_setup(check_only: bool, with_virt_manager: bool) -> None:
    """Install and configure the host prerequisites (KVM, libvirt, virtiofsd).

    Runs the bundled install-prerequisites.sh, which needs sudo and a
    Debian/Ubuntu host; read it first with --help. On another distro, install the
    packages and define the pool and network yourself — see the README.

    \b
      virt-runner setup --check          # what is missing?
      virt-runner setup                  # install + configure, then log out/in
      virt-runner setup --with-virt-manager
    """
    script = prerequisite_script()
    if script is None:
        raise SystemExit(
            f"virt-runner: the installer script is not available in this installation; "
            f"fetch it from the repository: "
            f"https://raw.githubusercontent.com/Kubementat/virt-runner/main/{SCRIPT_NAME}"
        )

    flags = []
    if check_only:
        flags.append("--check")
    if with_virt_manager:
        flags.append("--with-virt-manager")
    click.echo(f"Running {script} {' '.join(flags)}".rstrip())
    return subprocess.run(["bash", str(script), *flags], check=False).returncode
