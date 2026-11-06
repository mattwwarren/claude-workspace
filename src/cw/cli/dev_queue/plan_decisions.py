"""``cw dev-queue record-plan-decisions`` (GitHub #2984).

Records the approved plan's ``## Adopted Assumptions``, ``## Non-Goals`` and
``## Decisions`` on the ticket's dev-queue row as decided constraints for the
review lane (:func:`cw.dev_queue.operator_constraints.record_plan_constraints`).
The plan stage runs it once, at Step 1g.1, right after it persists
``.cw/plan.md``.

Deliberately not worker-guarded: the headless plan-stage worker is the
intended caller. Its guard is the recorder's row-stage check, which refuses
unless the dispatcher-owned row ``stage`` is ``plan``, so a review or impl
worker cannot record decided input for the lane that reviews it.

Idempotent: an item already on the row records nothing, so a re-run, or a
re-plan that rewords one bullet, appends only what is new. Exit codes: 0 when
every item is recorded or already present; 1 when nothing could be recorded
(unreadable plan, unsafe dispatch loop, missing row, row not at the plan
stage); 3 when a size cap left some items unrecorded (listed on stderr).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw.cli._base import handle_errors
from cw.cli.dev_queue._group import dev_queue
from cw.cli.dev_queue._loop_warning import require_dispatch_loop_safe
from cw.config import load_effective_config
from cw.dev_queue import resolve_client
from cw.dev_queue.operator_constraints import record_plan_constraints
from cw.exceptions import CwError
from cw.plan_decisions import parse_plan_decisions
from cw.plan_fingerprint import compute_plan_draft_fingerprint

if TYPE_CHECKING:
    from cw.dev_queue.operator_constraints import PlanRecordResult
    from cw.models import TicketTask

_EXIT_UNRECORDED = 3
# How much of an unrecorded item's first line the stderr list shows.
_ITEM_PREVIEW_CHARS = 80


def _read_plan(path: str) -> str:
    """The UTF-8 text at *path*; a failure is a ``CwError``."""
    try:
        return Path(path).read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = (
            f"Cannot read plan file {path}: it is not valid UTF-8"
            f" ({exc.reason} at byte {exc.start}). Nothing was recorded."
        )
        raise CwError(msg) from exc
    except OSError as exc:
        msg = (
            f"Cannot read plan file {path}: could not read it"
            f" ({exc.__class__.__name__}: {exc}). Nothing was recorded."
        )
        raise CwError(msg) from exc


def _preview(text: str) -> str:
    first = text.splitlines()[0] if text else ""
    if len(first) > _ITEM_PREVIEW_CHARS:
        return first[:_ITEM_PREVIEW_CHARS] + "…"
    return first


def _validate_plan_source(plan_path: str, text: str, task: TicketTask) -> None:
    """Bind recording to the task's persisted plan artifact and its token."""
    root = task.worktree_path if task.worktree_path is not None else Path.cwd()
    canonical = (root / ".cw" / "plan.md").resolve()
    supplied = Path(plan_path).resolve()
    if supplied != canonical:
        msg = (
            "Cannot record plan decisions from an arbitrary file: --plan must"
            f" name the canonical persisted plan {canonical}. Nothing was"
            " recorded."
        )
        raise CwError(msg)

    expected = task.plan_approved_fingerprint
    if expected is None:
        msg = (
            "Cannot record plan decisions: the row has no queue-owned approved"
            " plan fingerprint. Approval must be recorded before plan decisions"
            " can be persisted. Nothing was recorded."
        )
        raise CwError(msg)
    actual = compute_plan_draft_fingerprint(text)
    if actual != expected:
        msg = (
            "Cannot record plan decisions: the canonical plan fingerprint"
            f" does not match the approved plan (expected {expected}, got"
            f" {actual}). Nothing was recorded."
        )
        raise CwError(msg)


def _echo_result(ticket: str, client: str, result: PlanRecordResult) -> None:
    """The counts, then any unrecorded items, on stderr."""
    click.echo(
        f"Recorded {len(result['recorded'])} plan decision(s) for {ticket}"
        f" ({client}); {result['duplicates']} already recorded. Plan decisions"
        " are append-only; retraction is tracked in #2968.",
        err=True,
    )
    unrecorded = result["unrecorded"]
    if not unrecorded:
        return
    click.echo(
        f"{len(unrecorded)} plan decision(s) were not recorded:"
        f" {result['unrecorded_reason']}",
        err=True,
    )
    for kind, text in unrecorded:
        click.echo(f"- [{kind}] {_preview(text)}", err=True)


@dev_queue.command(name="record-plan-decisions")
@click.argument("ticket")
@click.option("--client", "-c", default=None, help="Client that owns the row.")
@click.option(
    "--plan",
    "plan_path",
    required=True,
    type=str,
    help="Path to the approved plan, normally .cw/plan.md.",
)
@handle_errors
def record_plan_decisions_command(
    ticket: str, client: str | None, plan_path: str
) -> None:
    """Record the approved plan's decided sections on the row (#2984).

    Run by the plan stage only (Step 1g.1). Each top-level item of the plan's
    Adopted Assumptions, Non-Goals and Decisions sections is appended to the
    row's constraint list as a decided constraint the review lane reads. It
    refuses unless the row is at the plan stage, and when the dispatch loop is
    stale or its code identity is unverifiable. Exit 3 when a size cap left
    items unrecorded.
    """
    resolved = resolve_client(ticket, load_effective_config(), client)
    require_dispatch_loop_safe(subject="plan decisions")
    text = _read_plan(plan_path)
    items = parse_plan_decisions(text)
    try:
        # bounded=True (#2501): the headless plan-stage worker runs this in
        # the foreground; a wedged queue lock must fail it, not hang it.
        result = record_plan_constraints(
            ticket,
            resolved,
            items,
            bounded=True,
            validate_source=lambda task: _validate_plan_source(plan_path, text, task),
        )
    except OSError as exc:
        msg = (
            f"Cannot record plan decisions for ticket '{ticket}': writing the"
            f" dev-queue failed ({exc.__class__.__name__}: {exc}). Re-run the"
            " command; already-recorded items are skipped."
        )
        raise CwError(msg) from exc
    if not items:
        click.echo(
            f"Plan {plan_path} has no decided sections with items; nothing recorded.",
            err=True,
        )
        return
    _echo_result(ticket, resolved, result)
    if result["unrecorded"]:
        raise SystemExit(_EXIT_UNRECORDED)
