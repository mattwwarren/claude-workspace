"""The stale-dispatch-loop guard for row-writing commands (#2729).

``cw dev-queue resolve`` and ``approve`` write row fields an older dispatch
loop does not know, and that loop's next queue save drops them. They call
:func:`warn_if_dispatch_loop_stale` before they write, which prints a line and
never refuses the write. ``cw review constrain``, ``cw dev-queue
record-plan-decisions`` and ``cw dev-queue prerequisite`` use
:func:`require_dispatch_loop_safe`, which refuses a write when the loop is
stale or its identity cannot be verified.

For a stale loop run by ``cw dev-queue serve --supervised`` (#3726) both first
wait up to 90s for its supervisor to restart it onto the checkout; an
unsupervised stale loop is handled at once, with no wait. The comparison and
its wording live in :mod:`cw.dispatch_loop_identity`, shared with the
``dispatch-loop-code`` doctor check.
"""

from __future__ import annotations

import contextlib
import time

import click

from cw.dispatch_loop_identity import LoopAssessment, LoopStatus, assess_dispatch_loop
from cw.exceptions import CwError

# MATCH and NO_LOOP print nothing.
_PREFIXES: dict[LoopStatus, str] = {
    LoopStatus.STALE: "warning",
    LoopStatus.IDENTITY_UNKNOWN: "note",
}
# How long a guarded command waits for a supervised loop's restart, and how
# often it re-assesses meanwhile (#3726).
_SUPERVISED_WAIT_SECONDS = 90.0
_SUPERVISED_POLL_SECONDS = 2.0


def _awaiting_restart(assessment: LoopAssessment) -> bool:
    return assessment.status is LoopStatus.STALE and assessment.supervised


def _await_supervised_restart() -> LoopAssessment:
    """Assess the loop; for a supervised STALE loop, wait for its restart.

    Returns the first assessment that is no longer a supervised STALE one, or
    the last one when the wait budget runs out. An unsupervised loop returns
    the first assessment, with no wait.
    """
    assessment = assess_dispatch_loop()
    if not _awaiting_restart(assessment):
        return assessment
    click.echo(
        "note: waiting for the supervised loop to restart..."
        f" (pid {assessment.pid}, up to {_SUPERVISED_WAIT_SECONDS:g}s)",
        err=True,
    )
    deadline = time.monotonic() + _SUPERVISED_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_SUPERVISED_POLL_SECONDS)
        assessment = assess_dispatch_loop()
        if not _awaiting_restart(assessment):
            break
    return assessment


def warn_if_dispatch_loop_stale() -> None:
    """Print a ``warning:`` (stale) or ``note:`` (unknown) line on stderr."""
    # Advisory: the shared assessment already degrades an unreadable lock path
    # to NO_LOOP (silent) and corrupt package metadata to an unknown version;
    # this guard drops the line on any other failure rather than aborting the
    # write command.
    with contextlib.suppress(Exception):
        assessment = _await_supervised_restart()
        prefix = _PREFIXES.get(assessment.status)
        if prefix is not None:
            click.echo(f"{prefix}: {assessment.message}", err=True)


def require_dispatch_loop_safe(subject: str = "an operator constraint") -> None:
    """Refuse constraint writes when a live loop may overwrite the row.

    *subject* names what would be recorded in both refusals; ``cw dev-queue
    record-plan-decisions`` passes ``"plan decisions"`` (#2984).
    """
    try:
        assessment = _await_supervised_restart()
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
    if assessment.supervised:
        msg = (
            f"Cannot safely record {subject} while {assessment.message}. The"
            " supervised loop did not restart within"
            f" {_SUPERVISED_WAIT_SECONDS:g}s; send SIGHUP to its supervisor or"
            " restart `cw dev-queue serve --supervised`, then retry."
        )
        raise CwError(msg)
    if assessment.status in {LoopStatus.STALE, LoopStatus.IDENTITY_UNKNOWN}:
        msg = (
            f"Cannot safely record {subject} while "
            f"{assessment.message}. Restart `cw dev-queue serve` and retry."
        )
        raise CwError(msg)
