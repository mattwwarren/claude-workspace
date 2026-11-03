"""The stale-dispatch-loop stderr line for row-writing commands (#2729).

``cw dev-queue resolve`` and ``approve`` write row fields an older dispatch
loop does not know, and that loop's next queue save drops them. Both call
:func:`warn_if_dispatch_loop_stale` before they write. The comparison and its
wording live in :mod:`cw.dispatch_loop_identity`, shared with the
``dispatch-loop-code`` doctor check. Advisory only: it never raises, never
changes an exit code and never blocks the write.
"""

from __future__ import annotations

import contextlib

import click

from cw.dispatch_loop_identity import LoopStatus, assess_dispatch_loop

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
