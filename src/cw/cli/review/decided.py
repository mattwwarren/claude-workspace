"""``cw review decided`` -- print the row's decided constraints (#2984).

The read-only source of the Claude-native reviewers' ``## Decided
Constraints`` prompt block: every constraint entry on the ticket's dev-queue
row, verbatim, grouped by provenance. Operator entries (``cw review
constrain``) come first, then the plan-derived ones (``cw dev-queue
record-plan-decisions``). The reviewer wording is the same constant the codex
``## Decided Scope`` section uses.

Never writes anything and is not worker-guarded: the review stage runs it
from inside its worktree. When ``.cw/plan.md`` (relative to the current
directory) has decided items that are not on the row, it prints one neutral
note saying they are plan context, not binding decided constraints. It names
no command to run, because the review stage never records.
"""

from __future__ import annotations

from pathlib import Path

import click

from cw.cli._base import handle_errors
from cw.cli.review._group import review
from cw.config import load_effective_config
from cw.dev_queue import load_dev_queue, resolve_client
from cw.dev_queue.crud import _find_ticket
from cw.models.operator_constraint import (
    DECIDED_SCOPE_INSTRUCTIONS,
    OPERATOR_CONSTRAIN_COMMAND,
    render_constraint_entries,
    render_decided_entries,
    split_constraints,
)
from cw.plan_decisions import parse_plan_decisions

_HEADING = "## Decided Constraints"
_NONE_RECORDED = "None recorded."
_PLAN_PATH = Path(".cw") / "plan.md"
_NOT_RECORDED_NOTE = (
    "Note: `.cw/plan.md` has decision sections that were not recorded on this"
    " ticket; treat them as plan context, not as binding decided constraints."
)


def _plan_has_unrecorded(recorded: set[str]) -> bool:
    """Whether ``.cw/plan.md`` has a decided item whose text is not *recorded*.

    An absent or unreadable plan has none: this is a courtesy note, never a
    reason to fail the reviewer's prompt assembly.
    """
    try:
        text = _PLAN_PATH.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return any(item.text not in recorded for item in parse_plan_decisions(text))


def render_decided_block(ticket: str, client: str) -> str:
    """The ``## Decided Constraints`` text for *ticket*'s row."""
    entries = _find_ticket(load_dev_queue(), ticket, client).operator_constraints
    operator, plan = split_constraints(entries)
    parts = [
        _HEADING,
        DECIDED_SCOPE_INSTRUCTIONS,
        f"**Operator constraints (`{OPERATOR_CONSTRAIN_COMMAND}`):**",
        render_constraint_entries(operator) or _NONE_RECORDED,
        "**Plan decisions (from the approved plan):**",
        render_decided_entries(plan) or _NONE_RECORDED,
    ]
    if _plan_has_unrecorded({entry.text for entry in entries}):
        parts.append(_NOT_RECORDED_NOTE)
    return "\n\n".join(parts)


@review.command(name="decided")
@click.argument("ticket")
@click.option(
    "--client",
    "-c",
    default=None,
    help="Client that owns the ticket's dev-queue row.",
)
@handle_errors
def review_decided(ticket: str, client: str | None) -> None:
    """Print the ticket's decided constraints for reviewers (#2984).

    Read-only. Prints every constraint entry on the cw dev-queue row,
    verbatim and grouped by provenance, under a ## Decided Constraints
    heading with the reviewer instructions. Safe inside a dispatch worker.
    """
    resolved = resolve_client(ticket, load_effective_config(), client)
    click.echo(render_decided_block(ticket, resolved))
