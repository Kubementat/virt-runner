"""Exceptions carrying a stable JSON error code (spec §5.2).

``VirtError`` derives from ``RuntimeError`` so existing ``except RuntimeError``
handlers keep working; the CLI layer reads ``.code``/``.stage`` to build the
error envelope instead of string-matching messages.

Codes are the stable set of ``specification/python-rewrite.md`` §5.2. Adding
one means adding it to that table first.
"""

from __future__ import annotations


class VirtError(RuntimeError):
    """Runtime/preflight failure with a machine-readable ``code``.

    Args:
        message: Human-readable text — identical to the text-mode stderr line
            minus the ``vm-<command>: `` prefix.
        code: Stable kebab-case code from spec §5.2.
        stage: ``vm-create`` pipeline stage (``preflight``, ``image``,
            ``cloud-init``, ``create``, ``wait-ip``, ``ssh-verify``) when the
            failure belongs to one.
    """

    def __init__(self, message: str, code: str, stage: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage


def code_of(exc: BaseException, default: str) -> str:
    """Error code carried by *exc*, or *default* for a plain ``RuntimeError``."""
    return getattr(exc, "code", None) or default
