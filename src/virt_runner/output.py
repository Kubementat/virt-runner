"""Output rendering for the subcommands: human text (default) or ``--json``.

Contract: ``specification/python-rewrite.md`` §5 (JSON Output Contract).

``--json`` makes stdout carry **exactly one** JSON document — the result
envelope below — written at the end of the run, after success or failure has
been decided. Nothing else is printed at all on that path: progress lines and
warnings are dropped (they are represented by the document), so
``virt-runner <cmd> --json | jq .`` is clean even though a terminal merges
stderr into the display.

    {"tool": "vm-create", "version": "0.2.0", "status": "success", "error": null, ...}
    {"tool": "vm-create", "version": "0.2.0", "status": "error", "error": {...}}

``VM_JSON_TRACE=1`` re-sends the otherwise-dropped progress/warning lines to
**stderr**, which keeps stdout parseable while debugging a long run that would
otherwise sit silent for minutes (spec §5.1.2 describes exactly that
redirect; the default here is silence).

Exit codes never change with ``--json``: ``0`` success, ``1`` runtime or
preflight failure, ``2`` usage error (:mod:`virt_runner.args` JSON-ifies those
too). Because stdout is the document's channel, human text inside the package
must go through :func:`progress`/:func:`warn` — a bare ``print()`` would
corrupt it. (``Virtualizer`` captures all subprocess output, so subprocess
noise can never reach stdout.)
"""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from typing import Any, NoReturn

import click

from virt_runner.errors import code_of  # re-exported for CLI call sites

#: Restore progress/warning lines on stderr under ``--json``.
TRACE_ENV = "VM_JSON_TRACE"

_STATE: dict[str, bool] = {"json": False}


# ---------------------------------------------------------------------------
# Mode plumbing
# ---------------------------------------------------------------------------


def set_json_mode(enabled: bool) -> None:
    """Record the output mode for the running command.

    Called by :class:`virt_runner.args.JsonCommand` right after parsing (so
    usage errors are covered) and again at the top of every command body, so a
    callback invoked directly still knows its mode.
    """
    _STATE["json"] = bool(enabled)


def is_json_mode() -> bool:
    """True when ``--json`` was passed for the running command."""
    return _STATE["json"]


def _trace_enabled() -> bool:
    return bool(os.environ.get(TRACE_ENV))


def progress(message: str) -> None:
    """Human-readable progress line — silent under ``--json``."""
    if _STATE["json"]:
        if _trace_enabled():
            _line(sys.stderr, message)
        return
    _line(sys.stdout, message)


def warn(message: str) -> None:
    """Warning line (stderr) — silent under ``--json``, see :func:`progress`.

    Callers whose warnings matter to machines put them in the document's
    ``warnings`` array as well.
    """
    if _STATE["json"]:
        if _trace_enabled():
            _line(sys.stderr, message)
        return
    _line(sys.stderr, message)


def _line(stream: Any, message: str) -> None:
    """One flushed line to *stream* (``click.echo`` has no ``flush``)."""
    click.echo(message, file=stream)
    stream.flush()


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def version() -> str:
    """Installed package version for the envelope's ``version`` field."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _version

    try:
        return _version("virt-runner")
    except PackageNotFoundError:  # run straight from a source tree
        return "0.0.0+unknown"


def tool_name(command: str) -> str:
    """Spec ``tool`` id for a subcommand (``create`` → ``vm-create``)."""
    return f"vm-{command}"


def document(
    command: str,
    *,
    error: dict[str, Any] | None,
    fields: dict[str, Any] | None = None,
) -> str:
    """Render the result envelope: ``tool``/``version``/``status``/``error``
    first, then the command-specific sections in the order given (spec §5.3).
    """
    payload: dict[str, Any] = {
        "tool": tool_name(command),
        "version": version(),
        "status": "error" if error else "success",
        "error": error,
    }
    payload.update(fields or {})
    return json.dumps(payload, indent=2, ensure_ascii=False)


def emit(command: str, fields: dict[str, Any]) -> None:
    """Write the success document to stdout; no-op in text mode.

    Text-mode callers print their human-readable block instead.
    """
    if _STATE["json"]:
        click.echo(document(command, error=None, fields=fields))


def fail(
    command: str,
    message: str,
    *,
    code: str | None = None,
    stage: str | None = None,
    exit_code: int = 1,
    text: str | None = None,
    fields: dict[str, Any] | None = None,
) -> NoReturn:
    """Report a failure — error envelope under ``--json`` — and exit.

    Args:
        command: Subcommand name (``create``, ``destroy``, ``list``).
        message: Error text; also the ``error.message`` value (spec §5.2:
            identical to the stderr line without the ``vm-…: `` prefix).
        code: Stable code (spec §5.2). ``code_of(exc, default)`` keeps the
            code raised by the ``Virtualizer`` when it has one.
        stage: ``vm-create`` stage, when the failure belongs to one.
        exit_code: ``1`` runtime/preflight, ``2`` usage.
        text: Overrides the human-readable line (default
            ``vm-<command>: <message>``).
        fields: Already-known command sections (e.g. ``vm``, ``image``) to
            include so a late failure still reports the created resources.
    """
    if _STATE["json"]:
        error: dict[str, Any] = {
            "code": code or "runtime-error",
            "message": message,
        }
        if stage:
            error["stage"] = stage
        click.echo(document(command, error=error, fields=fields))
    else:
        click.echo(
            text if text is not None else f"{tool_name(command)}: {message}",
            err=True,
        )
    raise SystemExit(exit_code)


def access_commands(name: str, user_name: str | None, ip: str | None) -> dict[str, Any]:
    """The ``ssh``/``console``/``teardown`` command trio of a VM (spec §5.3).

    ``ssh_command`` is ``None`` when there is no IP to reach the guest on or no
    user to log in as — a null in JSON beats a "(unavailable)" string to parse.
    """
    return {
        "ssh_command": f"ssh {user_name}@{ip}" if (user_name and ip) else None,
        "console_command": f"virsh console {name}",
        "teardown_command": f"virt-runner destroy {name}",
    }


def fail_with(
    command: str,
    exc: BaseException,
    default_code: str,
    **kwargs: Any,
) -> NoReturn:
    """:func:`fail` with the code carried by *exc* (or *default_code*)."""
    stage = kwargs.pop("stage", None) or getattr(exc, "stage", None)
    return fail(
        command, str(exc), code=code_of(exc, default_code), stage=stage, **kwargs
    )
