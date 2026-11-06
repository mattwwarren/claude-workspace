"""The stale-dispatch-loop guard for row-writing commands (#2729).

``cw dev-queue resolve`` and ``approve`` write row fields an older dispatch
loop does not know, and that loop's next queue save drops them. They call
:func:`warn_if_dispatch_loop_stale` before they write. ``cw review constrain``
and ``cw dev-queue record-plan-decisions`` use
:func:`require_dispatch_loop_safe`, which refuses a write when the loop is
stale or its identity cannot be verified. The comparison and its wording
live in :mod:`cw.dispatch_loop_identity`, shared with the
``dispatch-loop-code`` doctor check.
"""

from __future__ import annotations

import contextlib

import click

from cw.dispatch_loop_identity import LoopStatus, assess_dispatch_loop
from cw.exceptions import CwError

# MATCH and NO_LOOP print nothing.
_PREFIXES: dict[LoopStatus, str] = {
    LoopStatus.STALE: "warning",
    LoopStatus.IDENTITY_UNKNOWN: "note",
}


def warn_if_dispatch_loop_stale() -> None:
    """Print a ``warning:`` (stale) or ``note:`` (unknown) line on stderr."""
    # Advisory: the shared assessment already degrades an unreadable lock path
    # to NO_LOOP (silent) and corrupt package metadata to an unknown version;
    # this guard drops the line on any other failure rather than aborting the
    # write command.
    with contextlib.suppress(Exception):
        assessment = assess_dispatch_loop()
        prefix = _PREFIXES.get(assessment.status)
        if prefix is not None:
            click.echo(f"{prefix}: {assessment.message}", err=True)


def require_dispatch_loop_safe(subject: str = "an operator constraint") -> None:
    """Refuse constraint writes when a live loop may overwrite the row.

    *subject* names what would be recorded in both refusals; ``cw dev-queue
    record-plan-decisions`` passes ``"plan decisions"`` (#2984).
    """
    try:
        assessment = assess_dispatch_loop()
    except Exception as exc:
        msg = (
            f"Cannot safely record {subject}: the dispatch loop's "
            "code identity could not be assessed "
            f"({exc.__class__.__name__}: {exc}). Restart `cw dev-queue serve` "
            "and retry."
        )
        raise CwError(msg) from exc

    prefix = _PREFIXES.get(assessment.status)
    if prefix is not None:
        click.echo(f"{prefix}: {assessment.message}", err=True)
    if assessment.status in {LoopStatus.STALE, LoopStatus.IDENTITY_UNKNOWN}:
        msg = (
            f"Cannot safely record {subject} while "
            f"{assessment.message}. Restart `cw dev-queue serve` and retry."
        )
        raise CwError(msg)
