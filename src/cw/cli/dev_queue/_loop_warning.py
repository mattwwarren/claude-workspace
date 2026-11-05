"""The stale-dispatch-loop stderr line for row-writing commands (#2729).

``cw dev-queue resolve``, ``approve`` and ``cw review constrain`` (#2964)
write row fields an older dispatch loop does not know, and that loop's next
queue save drops them. The first two call :func:`warn_if_dispatch_loop_stale`
before they write; ``constrain`` uses :func:`refuse_if_dispatch_loop_unsafe`.
The comparison and its
wording live in :mod:`cw.dispatch_loop_identity`, shared with the
``dispatch-loop-code`` doctor check. Advisory only: it never raises, never
changes an exit code and never blocks the write; the constraint-specific guard
below does block unsafe writes.
"""

from __future__ import annotations

import contextlib

import click

from cw.dispatch_loop_identity import LoopStatus, assess_dispatch_loop
from cw.exceptions import CwError
from cw.models.operator_constraint import OPERATOR_CONSTRAIN_COMMAND

# MATCH and NO_LOOP print nothing.
_PREFIXES: dict[LoopStatus, str] = {
    LoopStatus.STALE: "warning",
    LoopStatus.IDENTITY_UNKNOWN: "note",
}
_UNSAFE_ASSESSMENT_MSG = (
    "Refusing to record an operator constraint because the dispatch loop's code "
    "identity cannot be assessed. Restart `cw dev-queue serve` first, then "
    f"re-run `{OPERATOR_CONSTRAIN_COMMAND}`."
)
_UNSAFE_LOOP_MSG = (
    "Refusing to record an operator constraint while the dispatch loop is stale "
    "or its code identity cannot be assessed. Restart `cw dev-queue serve` "
    f"first, then re-run `{OPERATOR_CONSTRAIN_COMMAND}`."
)


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


def refuse_if_dispatch_loop_unsafe() -> None:
    """Refuse a row write when an old or unverifiable loop could erase it."""
    try:
        assessment = assess_dispatch_loop()
    except Exception as exc:
        raise CwError(_UNSAFE_ASSESSMENT_MSG) from exc
    prefix = _PREFIXES.get(assessment.status)
    if prefix is not None:
        click.echo(f"{prefix}: {assessment.message}", err=True)
    if assessment.status in (LoopStatus.STALE, LoopStatus.IDENTITY_UNKNOWN):
        raise CwError(_UNSAFE_LOOP_MSG)
