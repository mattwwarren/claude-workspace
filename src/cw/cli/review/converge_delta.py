"""``cw review converge delta-confirm`` -- the cw-recorded delta round (#2918).

A clean-mode ledger converges at ``last_clean_sha``. When HEAD then moves by a
merge of ``origin/<default>`` that overlaps the branch's own files, the
default-branch merge tolerance rejects the move and ``verify-tip`` parks the
tip. For a client that opts in (``review.default_merge_delta_confirm``), this
command records the move with a much smaller round instead of a full one: one
review pass on the ledger's independent confirm backend over only what the
merge brought into the overlapping files, with the branch's own hunks in those
files as context. cw runs the reviewer and writes the ledger itself, as the
codex lane's Claude confirm does (``codex_clean_confirm._record_confirm_round``):
a worker can never self-report a ``delta`` round through ``record-round``.

Eligibility is :func:`~cw.review_convergence.assess_delta_round`, with the
tolerance read from the LIVE client config (``tolerance_for_client``), never
from the worker-writable cw-context: a context that claims the flag while the
client does not is refused (``delta_disabled`` / ``tolerance_off``). Any
refusal is a one-line ``CwError`` (exit 1) before the reviewer runs, and
nothing is written.

The failure contract is ``cw review confirm``'s, through the shared
:func:`~cw.cli.review.confirm.verified_verdict`: exit 3 the backend is down,
exit 4 the round failed on content (including a worktree mutated during the
call), exit 1 a ``CwError`` (including HEAD moving during the review or a
verdict for another sha); nothing is recorded on any of them. On success the
verdict goes through ``suppress_settled`` and ``decide_verdict`` (SHOULD_FIX is
deferred, non-gating), then ``from_verdict`` and ``record_delta_round``; the
round's verdict file is written first and the ledger last, as the commit
point.

The command fetches nothing (a stale ``origin/<default>`` ref only refuses
more), emits no event of its own (the inner review emits its audit events), and
imposes no deadline on the review (ADR-0014).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw.cli._base import handle_errors
from cw.cli.review.commands import _utc_now_iso
from cw.cli.review.confirm import (
    _resolve_round_inputs,
    require_backend_binary,
    run_backend_round,
    verified_verdict,
)
from cw.cli.review.converge import (
    converge,
    emit_json,
    ledger_option,
    require_ledger,
)
from cw.config import get_client
from cw.exceptions import CwError
from cw.review_convergence import (
    OVERLAPPING_FILES_KEY,
    assess_delta_round,
    decide_verdict,
    from_verdict,
    record_delta_round,
    rev_parse_or_none,
    save_ledger,
    save_round_verdict,
    shard_tally_from_verdict,
    suppress_settled,
    tolerance_for_client,
    worktree_state,
)

if TYPE_CHECKING:
    from cw.cli.review.confirm import _RoundOutcome
    from cw.review_convergence import ConvergenceLedger, DeltaPlan, RoundReport
    from cw.review_findings import ReviewVerdict

_REFUSED = "delta-confirm refused"


def delta_defer_rationale(backend: str) -> str:
    """The rationale a delta round's deferred SHOULD_FIX findings carry."""
    return f"SHOULD_FIX from the {backend} delta confirm round; non-gating (#2918)"


class DeltaRefusedError(CwError):
    """A ``delta-confirm`` refusal: one line, exit 1, nothing written."""

    __slots__ = ()

    def __init__(self, reason: str) -> None:
        super().__init__(f"{_REFUSED}: {reason}")


def _resolve_plan(
    *, client: str, ticket_id: str, worktree: Path, ledger_path: Path
) -> tuple[ConvergenceLedger, str, DeltaPlan]:
    """The ledger, HEAD and the delta plan, or a one-line refusal ``CwError``."""
    tolerance = tolerance_for_client(get_client(client))
    head = rev_parse_or_none(worktree, "HEAD")
    if head is None:
        unreadable = "unreadable git state"
        raise DeltaRefusedError(unreadable)
    ledger = require_ledger(ledger_path)
    if ledger.ticket_id != ticket_id:
        msg = (
            f"convergence ledger at {ledger_path} belongs to ticket "
            f"{ledger.ticket_id!r}, not {ticket_id!r}"
        )
        raise CwError(msg)
    assessment = assess_delta_round(
        ledger, worktree=worktree, head_sha=head, tolerance=tolerance
    )
    if assessment.plan is None:
        raise DeltaRefusedError(assessment.refusal or "ineligible")
    return ledger, head, assessment.plan


def _validated(
    *,
    worktree: Path,
    head: str,
    backend: str,
    before: tuple[str, str],
    outcome: _RoundOutcome,
) -> ReviewVerdict:
    """The verdict to record, after the G4 and sha checks (exit 1/3/4 otherwise).

    HEAD moving during the review is a ``CwError`` checked first, so it is
    never reported as a content failure; any other worktree change is exit 4.
    """
    if rev_parse_or_none(worktree, "HEAD") != head:
        moved = f"HEAD moved during the review (was {head}); nothing recorded"
        raise DeltaRefusedError(moved)
    verdict, _reason = verified_verdict(
        backend=backend, worktree=worktree, before=before, outcome=outcome
    )
    if verdict.reviewed_sha != head:
        other = (
            f"the verdict reviewed {verdict.reviewed_sha}, not HEAD {head}; "
            "nothing recorded"
        )
        raise DeltaRefusedError(other)
    return verdict


def _record(
    ledger_path: Path,
    ledger: ConvergenceLedger,
    verdict: ReviewVerdict,
    *,
    backend: str,
    head: str,
) -> RoundReport:
    """Record the delta round; the verdict file first, the ledger last."""
    settled, _ = suppress_settled(ledger, verdict)
    decided = decide_verdict(settled, delta_defer_rationale(backend))
    findings, unread = from_verdict(decided)
    shard_count, shards_reviewed = shard_tally_from_verdict(decided)
    updated, report = record_delta_round(
        ledger,
        backend=backend,
        reviewed_sha=head,
        findings=findings,
        roster=[record.reviewer_role for record in decided.agents_run],
        now=_utc_now_iso(),
        unread=unread,
        shard_count=shard_count,
        shards_reviewed=shards_reviewed,
    )
    save_round_verdict(ledger_path, report.round, decided)
    save_ledger(ledger_path, updated)
    return report


@converge.command(name="delta-confirm")
@click.option(
    "--ticket-id", required=True, help="Ticket whose dev-queue row to review."
)
@click.option("--client", "-c", required=True, help="Client the ticket belongs to.")
@click.option(
    "--worktree",
    default=".",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help="Worktree holding the branch under review.",
)
@ledger_option
@click.option(
    "--model",
    default=None,
    help="Reviewer model. Default: the backend's own configured model.",
)
@click.option(
    "--reasoning-effort",
    default=None,
    help="Codex reasoning effort. Default: the codex default (high). Codex only.",
)
@handle_errors
def converge_delta_confirm(
    ticket_id: str,
    client: str,
    worktree: Path,
    ledger_rel: Path,
    model: str | None,
    reasoning_effort: str | None,
) -> None:
    """Run a cw-recorded delta confirm round over a default-branch merge that
    overlaps the branch's files.

    Reviews only what the merge brought into the overlapping files (clean sha
    to HEAD), with the branch's own hunks in those files as context. A clean
    round records a `delta` round and moves `last_clean_sha` to HEAD; findings
    reopen the loop. Refused (exit 1, nothing written) unless the client is in
    clean mode with `default_merge_tolerance` and `default_merge_delta_confirm`
    on, the ledger is converged with a claude-native loop and an independent
    confirm, and every merge since the clean sha is a textually clean two-parent
    merge of the default branch. Exit 3: backend unavailable. Exit 4: the round
    failed; nothing is recorded in either case.
    """
    task, default_branch = _resolve_round_inputs(
        ticket_id=ticket_id, client=client, worktree=worktree, default_branch=None
    )
    ledger_path = worktree / ledger_rel
    ledger, head, plan = _resolve_plan(
        client=client, ticket_id=ticket_id, worktree=worktree, ledger_path=ledger_path
    )
    backend = ledger.confirm_backend
    require_backend_binary(backend)
    before = worktree_state(worktree)
    outcome = run_backend_round(
        backend=backend,
        task=task,
        worktree=worktree,
        default_branch=default_branch,
        base_ref=default_branch,
        model=model,
        reasoning_effort=reasoning_effort,
        delta=plan.spec,
    )
    verdict = _validated(
        worktree=worktree, head=head, backend=backend, before=before, outcome=outcome
    )
    report = _record(ledger_path, ledger, verdict, backend=backend, head=head)
    body = report.model_dump(mode="json")
    body[OVERLAPPING_FILES_KEY] = list(plan.spec.paths)
    emit_json(body)
