"""``destroy`` subcommand — teardown: destroy/undefine + volume delete."""

from __future__ import annotations

import click

from virt_runner import output
from virt_runner.args import command
from virt_runner.virtualizer import Virtualizer, volume_name


@command(name="destroy")
@click.argument("name")
def cmd_destroy(name: str, as_json: bool) -> None:
    """Destroy (if running) and undefine domain, then delete its disk."""
    output.set_json_mode(as_json)
    v = Virtualizer()

    # Preflight: must be a defined domain.
    if not v.domain_exists(name):
        # Exact text-mode wording (spec §4.5): no "vm-destroy:" prefix.
        output.fail(
            "destroy",
            f"VM '{name}' is not defined",
            code="vm-not-defined",
            text=f"VM '{name}' is not defined",
            fields={"name": name},
        )

    try:
        teardown = v.destroy_vm(name)
    except RuntimeError as exc:
        output.fail_with("destroy", exc, "volume-delete-failed", fields={"name": name})

    if teardown.warning:
        output.warn(teardown.warning)

    if as_json:
        # The warning is part of the document too — stderr is silent here.
        output.emit(
            "destroy",
            {
                "name": name,
                "domain": {
                    "was_running": teardown.was_running,
                    "destroyed": True,
                    "undefined": True,
                },
                "volume": {
                    "name": volume_name(name),
                    "pool": v.POOL,
                    "deleted": teardown.volume_deleted,
                    "already_absent": teardown.volume_already_absent,
                },
                "warnings": [teardown.warning] if teardown.warning else [],
            },
        )
        return

    click.echo(teardown.confirmation)
