"""Argument plumbing shared by the subcommands (spec §5.1.4).

The CLI must know the output mode *before* argument validation, so that a usage
error (exit 2) is reported as a JSON document when ``--json`` was part of the
invocation — a script that asked for JSON always gets JSON.

``JsonCommand`` also injects the shared ``--json`` flag, so a subcommand only
has to accept ``as_json: bool`` and hand it to :func:`virt_runner.output.
set_json_mode`.
"""

from __future__ import annotations

from typing import Any

import click

from virt_runner import output

JSON_HELP = (
    "Print exactly one JSON document on stdout and suppress all human-readable output."
)

#: Param name of the shared flag (``--json`` would shadow nothing but reads
#: poorly as a callback argument).
JSON_PARAM = "as_json"


class JsonCommand(click.Command):
    """Subcommand with a shared ``--json`` flag and JSON usage errors."""

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        """Append ``--json`` unless the command declares it itself."""
        params = list(super().get_params(ctx))
        if not any(p.name == JSON_PARAM for p in params):
            params.append(
                # The second decl names the param: ``--json`` would bind to a
                # callback argument called ``json`` otherwise.
                click.Option(
                    ["--json", JSON_PARAM],
                    is_flag=True,
                    default=False,
                    help=JSON_HELP,
                )
            )
        return params

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        """Parse, and turn parse failures into the JSON error envelope.

        The raw *args* are pre-scanned for ``--json`` because a parse failure
        never produces params to read the flag from. (An invocation where the
        flag itself is misspelled cannot be recognised — spec §5.1.4.)
        """
        # Snapshot first: click's parser consumes/mutates *args* in place, so
        # by the time an error is raised the original list is gone.
        raw_args = list(args or [])
        try:
            ctx = super().make_context(info_name, args, parent, **extra)
        except click.ClickException as exc:
            if not _wants_json(raw_args):
                raise
            output.set_json_mode(True)
            output.fail(
                self.name,
                exc.format_message(),
                code="usage",
                exit_code=exc.exit_code,
            )
        output.set_json_mode(bool(ctx.params.get(JSON_PARAM)))
        return ctx


def command(name: str, **kwargs: Any) -> Any:
    """``click.command`` with :class:`JsonCommand` as the default class."""
    kwargs.setdefault("cls", JsonCommand)
    return click.command(name=name, **kwargs)


def _wants_json(args: list[str]) -> bool:
    """Pre-scan raw arguments for the ``--json`` flag."""
    return "--json" in list(args or [])
