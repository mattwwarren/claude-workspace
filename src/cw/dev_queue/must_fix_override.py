"""Operator override of a codex MUST_FIX park (GitHub #2205).

``cw dev-queue approve --override-must-fix --reason ...`` records a durable,
audited decision to ship a branch past a codex background review's BLOCKING
verdict. The record lives on :attr:`TicketTask.must_fix_override`, a field the
PENDING reset of ``requeue --stage finalize`` does not clear (unlike
``blocked_reason``), and is bound to the verdict it was given for: its
``reviewed_sha`` and the :func:`cw.review_debt.fingerprint_v1` identity of every
MUST_FIX finding on it. FINALIZE's ``check_must_fix_override.py`` honors it only
while the live verdict and HEAD still match.

Stamp-only: the row keeps its status and stage, and the operator still runs
``cw dev-queue requeue --stage finalize`` (a human step when the ticket or lane
carries a finalize hold, operator signoff or scope_hint: large). The same save
also stamps the large-tier review approval (#2779): ``approver_kind`` is
``"operator"``, bound to the verdict's ``reviewed_sha`` with the override's
``recorded_at``, so a large ticket passes finalize Step 4.0 while HEAD stays on
the overridden commit. A REVIEW-targeting regress or same-stage REVIEW requeue
clears both together. Every refusal
names the command legal from the row's state (#2692): the park refusals append
the ``next_commands`` suffix, the verdict refusals name a review re-run (never
the finalize jump or the override flag). Split out of ``approval.py`` beside
``plan_promotion``/``requeue`` to keep that module under the size ceiling.

Under ``review.convergence: clean`` (#2791 G3) the override also needs a
convergence ledger for this ticket in the worktree that finalize's ``cw review
converge verify-tip`` accepts at the verdict's ``reviewed_sha``. A ledger
already converged there passes as is. A ledger whose last round is a dirty
Claude confirm round at that sha, whose saved round verdict raised every
overridden MUST_FIX, is converged by the override itself (#2834): each finding
settles as an operator ``defer`` entry, the round is marked clean and the
ledger audits the override in ``confirm_overrides``. A missing (the codex loop
parked before any ledger was written), unreadable, foreign, other-tip or any
other non-converged ledger is refused before any write, naming the review
requeue and the ledger-side exits.

The same command releases a ``review_confirm_must_fix`` park (#3042, RFC 0017
I3): the confirm round after a repo review engine pass raised a MUST_FIX the
engine's verdict leaves to the operator. That park has no
``.claude/review-verdict.json``; the verdict is the parked round's saved
``round-<n>-verdict.json`` (:func:`_load_confirm_park`), the ledger must be this
ticket's, its last round a confirm round, and the override converges it through
the same ``override_confirm_round``. A round verdict with no MUST_FIX, or one
whose MUST_FIX or SHOULD_FIX carries a never-deferrable ``coverage.*`` /
``acceptance.*`` rule, is refused naming ``requeue --stage impl --regress``. A
MUST_FIX with no diff anchor (``file == "N/A"``) is legal on this park and is
bound as ``("N/A", normalize_summary(summary))``. Still stamp-only, with the same
two-step operator sequence (override, then ``requeue --stage finalize``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn, NotRequired, TypedDict

from pydantic import ValidationError

from cw.auto_dev_result import REVIEW_CONFIRM_MUST_FIX_BLOCKER_REASON
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.config import get_client
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.ledger_race import row_started_running
from cw.dev_queue.lifecycle import _record_review_approval, _review_approval_snapshot
from cw.dev_queue.next_commands import dev_queue_command
from cw.dev_queue.refusal import refusal_hint
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import ApproveGateError, CwError
from cw.models import MustFixOverride, OrchestratorEventType, QueueItemStatus, Stage
from cw.operator_identity import resolve_actor_or_empty
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    LEDGER_STATUS_CONVERGED,
    confirm_override_applies,
    finding_identity,
    load_ledger,
    load_round_verdict,
    operator_settle_event_payload,
    override_confirm_round,
    save_ledger,
)
from cw.review_debt import fingerprint_v1, normalize_summary
from cw.review_engine.route import is_non_deferrable_rule
from cw.review_findings import REVIEW_VERDICT_JSON_RELATIVE_PATH, ReviewVerdictEnvelope
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask
    from cw.review_convergence import (
        ConfirmOverride,
        ConvergenceLedger,
        FindingIdentity,
    )
    from cw.review_findings import ReviewVerdict


_log = logging.getLogger(__name__)

#: The ledger's timestamp format (``cw review``'s ``_utc_now_iso``), derived
#: here from the override's own ``now`` rather than imported from the CLI.
_LEDGER_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_LEDGER_ROW_STARTED_MESSAGE = (
    "a worker started on this ticket while the MUST_FIX override was written, so"
    " the convergence ledger write may have been lost. Re-run the same override"
    " once the ticket parks again (idempotent: a rerun after success takes the"
    " converged path and adds no second confirm override)."
)


class MustFixOverrideApproval(TypedDict):
    """What :func:`approve_must_fix_override_ticket` stamped.

    ``ledger_overridden_round`` is present only when the override converged
    the clean-mode ledger past that dirty Claude confirm round (#2834).
    """

    ticket_id: str
    client: str
    stage: str
    actor: str
    reviewed_sha: str
    finding_ids: list[tuple[str, str]]
    ledger_overridden_round: NotRequired[int]


@dataclass(frozen=True)
class _LedgerOverridePlan:
    """A validated plan to converge the ledger at *path* past its confirm round."""

    path: Path
    ledger: ConvergenceLedger
    identities: tuple[FindingIdentity, ...]
    round_identities: tuple[FindingIdentity, ...]


def approve_must_fix_override_ticket(
    ticket_id: str,
    client_name: str,
    reason: str,
    *,
    bounded: bool = False,
) -> MustFixOverrideApproval:
    """Record an operator override of a ``codex_must_fix_findings`` park.

    A ``review_confirm_must_fix`` park (#3042) is accepted too: its verdict is
    the parked confirm round's saved ``round-<n>-verdict.json``, and a round
    verdict with no MUST_FIX or with a never-deferrable finding is refused,
    naming ``requeue --stage impl --regress``.

    Raises:
        ApproveGateError: if *reason* is blank, the row is not parked
            BLOCKED_ON_USER for ``codex_must_fix_findings``, its worktree or
            ``.claude/review-verdict.json`` cannot be resolved, the verdict
            belongs to another ticket or is not blocking, a MUST_FIX finding
            has no fingerprint, or (clean-mode clients) the worktree holds no
            convergence ledger for this ticket that is converged at the
            verdict's commit or that this override may converge (a dirty
            Claude confirm round there). Nothing is recorded on any of these
            paths.
        CwError: if no matching task is found.
    """
    with _lock(bounded=bounded):
        return _approve_must_fix_override_locked(ticket_id, client_name, reason)


def _approve_must_fix_override_locked(
    ticket_id: str,
    client_name: str,
    reason: str,
) -> MustFixOverrideApproval:
    """Lock-free body of :func:`approve_must_fix_override_ticket`.

    The caller MUST already hold ``dev_queue_lock()`` (``_lock``). Every
    validation runs before the first write, so a refusal leaves no trace.

    Event-first ordering (the ``revoke_plan_approval`` precedent, #2394): the
    override and its review approval are stamped in memory, then the audit
    events are recorded before the row is saved, so a failed audit write leaves
    neither on disk. A save that fails after the events leaves the row
    without an override too, which FINALIZE treats as "still blocked" -- the
    events then truthfully record an attempt, and an ERROR line names it. A
    converged ledger (#2834) is saved last; see :func:`_save_converged_ledger`.
    """
    stripped_reason = reason.strip()
    if not stripped_reason:
        msg = (
            f"Cannot override MUST_FIX for ticket '{ticket_id}': --reason is"
            " blank. State why shipping past the findings is acceptable."
        )
        raise ApproveGateError(msg)

    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client_name)
    _require_codex_must_fix_park(task)
    verdict, plan, finding_ids = _override_target(task, client_name)

    actor = resolve_actor_or_empty(
        get_client(client_name),
        context=f"MUST_FIX override for {client_name}/{ticket_id}",
    )

    stage = task.stage.value
    # #2779: one `now` for the override and the review approval it carries;
    # both are stamped in memory and persist together on the save below.
    # #2834: the ledger's stamps are the same instant, in the ledger format.
    now = datetime.now(UTC)
    converged = (
        None
        if plan is None
        else override_confirm_round(
            plan.ledger,
            plan.identities,
            reviewed_sha=verdict.reviewed_sha,
            round_identities=plan.round_identities,
            actor=actor,
            reason=stripped_reason,
            now=now.strftime(_LEDGER_STAMP_FORMAT),
        )
    )
    before = _review_approval_snapshot(task)
    task.must_fix_override = MustFixOverride(
        actor=actor,
        reason=stripped_reason,
        reviewed_sha=verdict.reviewed_sha,
        finding_ids=finding_ids,
        recorded_at=now,
    )
    _record_review_approval(task, "operator", verdict.reviewed_sha, now)
    payload: dict[str, object] = {
        "ticket_id": ticket_id,
        "client": client_name,
        "from_stage": stage,
        "to_stage": stage,
        "actor": actor,
        "reason": stripped_reason,
        "reviewed_sha": verdict.reviewed_sha,
        "finding_ids": [list(pair) for pair in finding_ids],
        "review_approver_kind": "operator",
        "review_approval_before": before,
        "review_approval_after": _review_approval_snapshot(task),
    }
    if converged is not None:
        payload["ledger_override"] = _ledger_override_payload(converged[1])
    record_event(
        OrchestratorEventType.TICKET_APPROVED, payload, correlation_id=ticket_id
    )
    if converged is not None:
        _record_settled_events(ticket_id, *converged)
    try:
        save_dev_queue(store)
    except Exception:
        _log.error(
            "MUST_FIX override save failed for %s/%s after TICKET_APPROVED was"
            " recorded; the row carries no override, so FINALIZE still blocks",
            client_name,
            ticket_id,
            exc_info=True,
        )
        raise
    result: MustFixOverrideApproval = {
        "ticket_id": ticket_id,
        "client": client_name,
        "stage": stage,
        "actor": actor,
        "reviewed_sha": verdict.reviewed_sha,
        "finding_ids": finding_ids,
    }
    if plan is not None and converged is not None:
        _save_converged_ledger(plan.path, converged[0], client_name, ticket_id)
        result["ledger_overridden_round"] = converged[1].round
        if row_started_running(ticket_id, client_name):
            _log.error(
                "MUST_FIX override for %s/%s: a worker started on the row while"
                " the ledger was written; the ledger write may have been lost",
                client_name,
                ticket_id,
            )
            raise CwError(_LEDGER_ROW_STARTED_MESSAGE)
    return result


def _override_target(
    task: TicketTask, client_name: str
) -> tuple[ReviewVerdict, _LedgerOverridePlan | None, list[tuple[str, str]]]:
    """The verdict, ledger plan and finding ids the override binds to.

    A ``review_confirm_must_fix`` park (#3042) reads the parked round's saved
    verdict and binds unanchored findings; a codex park reads
    ``.claude/review-verdict.json`` and fails closed on them, as before.
    """
    if _is_confirm_park(task):
        verdict, plan = _load_confirm_park(task, client_name)
        return verdict, plan, _finding_ids(task, verdict, allow_unanchored=True)
    verdict = _load_owned_verdict(task, client_name)
    plan = _check_ledger_when_clean(task, client_name, verdict)
    return verdict, plan, _finding_ids(task, verdict)


def _ledger_override_payload(override: ConfirmOverride) -> dict[str, object]:
    """The ``ledger_override`` key of ``ticket.approved`` (an attempt, #2834)."""
    return {
        "round": override.round,
        "prior_status": override.prior_status,
        "prior_stop_reason": override.prior_stop_reason,
        "identities": [identity.audit_key() for identity in override.identities],
    }


def _record_settled_events(
    ticket_id: str, ledger: ConvergenceLedger, override: ConfirmOverride
) -> None:
    """One ``review.finding_settled`` event per overridden identity (#2834).

    Parity with ``cw review converge operator-settle``: the absence of the
    event for a suppression means the suppression did not happen. Recorded
    before any save, so a failure leaves queue and ledger unwritten.
    """
    by_identity = {entry.identity: entry for entry in ledger.settled}
    for identity in override.identities:
        record_event(
            OrchestratorEventType.REVIEW_FINDING_SETTLED,
            operator_settle_event_payload(by_identity[identity]),
            correlation_id=ticket_id,
        )


def _save_converged_ledger(
    path: Path, ledger: ConvergenceLedger, client_name: str, ticket_id: str
) -> None:
    """Persist the override-converged ledger, after the row save (#2834).

    There is no ledger-level lock: ``dev_queue_lock`` does not cover
    ``.cw/review-convergence.json``, and this read-modify-write would race any
    other ledger writer. The guard is :func:`_require_codex_must_fix_park` (the
    row is parked, so no live REVIEW worker owns the worktree), the same
    exposure ``cw review converge operator-settle`` accepts. A failure leaves
    the row stamped and the ledger unconverged: fail-closed (``verify-tip``
    still parks), and a rerun of the same command converges it.
    """
    try:
        save_ledger(path, ledger)
    except Exception:
        _log.error(
            "MUST_FIX override ledger save failed for %s/%s after the row was"
            " stamped; the convergence ledger is not converged, so finalize's"
            " verify-tip still parks. To finish, rerun the same `cw dev-queue"
            " approve --override-must-fix` command",
            client_name,
            ticket_id,
            exc_info=True,
        )
        raise


#: The parks ``--override-must-fix`` releases: a codex background review's
#: MUST_FIX, and the confirm round after a repo review engine pass (#3042).
_OVERRIDABLE_PARKS = (CODEX_MUST_FIX_FINDINGS, REVIEW_CONFIRM_MUST_FIX_BLOCKER_REASON)


def _is_confirm_park(task: TicketTask) -> bool:
    """Whether *task* parked for the confirm round after an engine pass (#3042)."""
    return task.blocked_reason == REVIEW_CONFIRM_MUST_FIX_BLOCKER_REASON


def _require_codex_must_fix_park(task: TicketTask) -> None:
    """Raise unless *task* is parked BLOCKED_ON_USER for an overridable MUST_FIX.

    The name predates #3042: a ``review_confirm_must_fix`` park is accepted
    beside ``codex_must_fix_findings``.
    """
    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': status is"
            f" {task.status.value!r}, expected BLOCKED_ON_USER.{refusal_hint(task)}"
        )
        raise ApproveGateError(msg)
    if task.blocked_reason not in _OVERRIDABLE_PARKS:
        expected = " or ".join(repr(reason) for reason in _OVERRIDABLE_PARKS)
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}':"
            f" blocked_reason is {task.blocked_reason!r}, expected"
            f" {expected}.{refusal_hint(task)}"
        )
        raise ApproveGateError(msg)


def _review_rerun(task: TicketTask) -> str:
    """The sentence a verdict refusal appends: re-run review, never ship."""
    command = dev_queue_command("requeue", task, "--stage", Stage.REVIEW.value)
    return f" Re-run review to produce a current verdict: `{command}`."


def _task_worktree(task: TicketTask, client_name: str) -> Path:
    """*task*'s worktree, or a refusal naming the review re-run."""
    worktree = resolve_task_worktree(task, get_client(client_name))
    if worktree is None:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': its"
            " worktree could not be resolved, so there is no review verdict to"
            f" bind the override to.{_review_rerun(task)}"
        )
        raise ApproveGateError(msg)
    return worktree


def _verdict_path(task: TicketTask, client_name: str) -> Path:
    """Path of *task*'s worktree ``.claude/review-verdict.json``."""
    return _task_worktree(task, client_name) / REVIEW_VERDICT_JSON_RELATIVE_PATH


def _load_owned_verdict(task: TicketTask, client_name: str) -> ReviewVerdict:
    """Read the worktree's verdict; require it to be this ticket's and blocking."""
    path = _verdict_path(task, client_name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': could not"
            f" read {path} ({exc.__class__.__name__}: {exc}).{_review_rerun(task)}"
        )
        raise ApproveGateError(msg) from exc
    try:
        envelope = ReviewVerdictEnvelope.model_validate_json(text)
    except ValidationError as exc:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': {path} is"
            f" not a valid review verdict ({exc.error_count()} validation"
            f" error(s)).{_review_rerun(task)}"
        )
        raise ApproveGateError(msg) from exc
    if envelope.ticket_id != task.ticket_id:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': {path}"
            f" belongs to ticket {envelope.ticket_id!r}, so it is stale or"
            f" foreign.{_review_rerun(task)}"
        )
        raise ApproveGateError(msg)
    if not envelope.verdict.blocking:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': the"
            f" verdict in {path} is not blocking -- there is nothing to"
            f" override.{_review_rerun(task)}"
        )
        raise ApproveGateError(msg)
    return envelope.verdict


def _finding_ids(
    task: TicketTask, verdict: ReviewVerdict, *, allow_unanchored: bool = False
) -> list[tuple[str, str]]:
    """Sorted, deduped fingerprints of *verdict*'s MUST_FIX findings.

    Fails closed on a finding with no fingerprint (``file == "N/A"``): the
    override would otherwise look complete while leaving it uncovered.
    *allow_unanchored* (the confirm park only, #3042) binds such a finding as
    ``(file, normalize_summary(summary))`` instead: a confirm MUST_FIX with no
    diff anchor is exactly the finding only an operator can discharge, its
    ledger identity is defined for ``N/A``, and finalize's override check never
    evaluates the pair (that park writes no ``.claude/review-verdict.json``).
    """
    ids: set[tuple[str, str]] = set()
    for finding in verdict.must_fix:
        fingerprint = fingerprint_v1(finding.file, finding.summary)
        if fingerprint is None and allow_unanchored:
            fingerprint = (finding.file, normalize_summary(finding.summary))
        if fingerprint is None:
            msg = (
                f"Cannot override MUST_FIX for ticket '{task.ticket_id}': finding"
                f" {finding.summary!r} has no diff anchor (file"
                f" {finding.file!r}), so it has no identity to bind the override"
                " to. Resolve it, or re-review once it is anchored."
                f"{_review_rerun(task)}"
            )
            raise ApproveGateError(msg)
        ids.add(fingerprint)
    return sorted(ids)


def _ledger_exits(task: TicketTask) -> str:
    """The exits a clean-mode ledger refusal names, honest about their strength."""
    command = dev_queue_command("requeue", task, "--stage", Stage.REVIEW.value)
    return (
        f" Re-run review so the clean-mode gate converges the ledger: `{command}`"
        " (the loop is not deterministic and can park on the same finding"
        " again), or switch the client to `review.convergence: blocking`."
    )


def _ledger_prefix(task: TicketTask) -> str:
    return f"Cannot override MUST_FIX for ticket '{task.ticket_id}' under clean mode:"


def _check_ledger_when_clean(
    task: TicketTask, client_name: str, verdict: ReviewVerdict
) -> _LedgerOverridePlan | None:
    """Under clean mode, refuse unless finalize's verify-tip can pass (G3, #2834).

    ``verify-tip`` in finalize needs a converged ledger clean at the tip being
    shipped, so an override on a missing, unreadable, foreign or non-converged
    one, or one converged at another sha, would stamp the row and still park.
    The overridden tip is the verdict's ``reviewed_sha`` (what the override
    binds to; FINALIZE separately requires HEAD to still equal it), which this
    module already holds, so no git subprocess runs under the dev-queue lock.

    Returns ``None`` for a blocking-mode client (untouched) and for a ledger
    already converged at that sha. Returns the plan when the ledger's last
    round is a dirty Claude confirm round this override may converge
    (:func:`_override_plan`). Every other case raises. Runs before any write,
    like every other refusal here.
    """
    client_cfg = get_client(client_name)
    if not client_cfg.review.is_clean:
        return None
    path, ledger = _load_owned_ledger(task, client_name)
    if ledger.status == LEDGER_STATUS_CONVERGED:
        if ledger.last_clean_sha != verdict.reviewed_sha:
            msg = (
                f"{_ledger_prefix(task)} the convergence ledger {path} converged at"
                f" {ledger.last_clean_sha!r}, not at the overridden verdict's"
                f" reviewed commit {verdict.reviewed_sha!r}, so finalize's"
                f" verify-tip would still park.{_ledger_exits(task)}"
            )
            raise ApproveGateError(msg)
        return None
    plan = _override_plan(path, ledger, verdict)
    if plan is None:
        _refuse_unconverged(task, path, ledger)
    return plan


def _load_owned_ledger(
    task: TicketTask, client_name: str
) -> tuple[Path, ConvergenceLedger]:
    """This ticket's ledger, or the unreadable, missing or foreign refusal."""
    path = _task_worktree(task, client_name) / LEDGER_RELATIVE_PATH
    prefix = _ledger_prefix(task)
    try:
        ledger = load_ledger(path)
    except (OSError, ValueError) as exc:
        msg = (
            f"{prefix} could not read the convergence ledger {path}"
            f" ({exc.__class__.__name__}).{_ledger_exits(task)}"
        )
        raise ApproveGateError(msg) from exc
    if ledger is None:
        msg = (
            f"{prefix} no convergence ledger exists at {path}. The park came"
            " from the codex fix loop before any ledger was written, so"
            f" finalize's verify-tip would find no clean round.{_ledger_exits(task)}"
        )
        raise ApproveGateError(msg)
    if ledger.ticket_id != task.ticket_id:
        msg = (
            f"{prefix} the convergence ledger {path} belongs to ticket"
            f" {ledger.ticket_id!r}.{_ledger_exits(task)}"
        )
        raise ApproveGateError(msg)
    return path, ledger


def _refuse_unconverged(
    task: TicketTask, path: Path, ledger: ConvergenceLedger
) -> NoReturn:
    """The non-converged refusal: the override cannot converge this ledger."""
    msg = (
        f"{_ledger_prefix(task)} the convergence ledger {path} is"
        f" {ledger.status!r}, not converged, so finalize's verify-tip would"
        " still park. Unless its last round is a dirty Claude confirm round at"
        " the overridden commit whose saved round verdict covers every"
        " overridden finding, an override does not converge it. To accept a"
        " finding as a false positive, `cw review converge operator-settle`"
        " settles it first (a stopped ledger's recurring findings, or a Claude"
        " confirm finding in the ledger's fix memory), then a review requeue"
        " records a clean round; with `review.confirm_reentry` on, a requeue"
        " also feeds the remaining Claude findings back into the codex fix"
        f" loop.{_ledger_exits(task)}"
    )
    raise ApproveGateError(msg)


def _must_fix_identities(verdict: ReviewVerdict) -> tuple[FindingIdentity, ...]:
    """The ledger identity of every MUST_FIX finding on *verdict*."""
    return tuple(
        finding_identity("MUST_FIX", f.file, f.summary, fingerprint=f.fingerprint)
        for f in verdict.must_fix
    )


def _override_plan(
    path: Path, ledger: ConvergenceLedger, verdict: ReviewVerdict
) -> _LedgerOverridePlan | None:
    """The plan to converge *ledger* past its dirty confirm round, or ``None``.

    ``None`` (today's refusal) unless the last round is a ``confirm`` round,
    no MUST_FIX carries a never-deferrable ``coverage.*``/``acceptance.*`` rule
    (RFC 0017 D-6), that round's persisted verdict reads back, and
    :func:`confirm_override_applies` holds against its MUST_FIX identities. A
    missing or unreadable round verdict fails closed.
    """
    if not ledger.rounds or ledger.rounds[-1].kind != "confirm":
        return None
    if any(is_non_deferrable_rule(f.rule_id) for f in verdict.must_fix):
        return None
    try:
        round_verdict = load_round_verdict(path, ledger.rounds[-1].round)
    except (OSError, ValueError):
        return None
    identities = _must_fix_identities(verdict)
    round_identities = _must_fix_identities(round_verdict)
    if not confirm_override_applies(
        ledger,
        identities,
        reviewed_sha=verdict.reviewed_sha,
        round_identities=round_identities,
    ):
        return None
    return _LedgerOverridePlan(
        path=path,
        ledger=ledger,
        identities=identities,
        round_identities=round_identities,
    )


def _regress_exit(task: TicketTask) -> str:
    """The sentence a confirm-park refusal appends: send the branch back to impl."""
    command = dev_queue_command(
        "requeue", task, "--stage", Stage.IMPL.value, "--regress"
    )
    return f" Send it back to implementation instead: `{command}`."


def _refuse_confirm_park(task: TicketTask, detail: str) -> NoReturn:
    msg = (
        f"Cannot override MUST_FIX for ticket '{task.ticket_id}' "
        f"(review_confirm_must_fix): {detail}{_regress_exit(task)}"
    )
    raise ApproveGateError(msg)


def _never_deferrable(verdict: ReviewVerdict) -> set[tuple[str, str]]:
    """``(file, summary)`` of each gating finding no operator can defer (D-6)."""
    gating = [
        item.finding
        for item in verdict.accepted
        if item.finding.severity in {"MUST_FIX", "SHOULD_FIX"}
    ]
    return {
        (f.file, f.summary)
        for f in [*verdict.must_fix, *gating]
        if is_non_deferrable_rule(f.rule_id)
    }


def _confirm_round_verdict(
    task: TicketTask, path: Path, ledger: ConvergenceLedger
) -> ReviewVerdict:
    """The parked confirm round's saved verdict, or a refusal naming the regress."""
    if not ledger.rounds or ledger.rounds[-1].kind != "confirm":
        _refuse_confirm_park(
            task,
            f"the convergence ledger {path} has no confirm round last, so there"
            " is no parked confirm verdict to override.",
        )
    round_number = ledger.rounds[-1].round
    try:
        return load_round_verdict(path, round_number)
    except (OSError, ValueError) as exc:
        _refuse_confirm_park(
            task,
            f"could not read the saved verdict of confirm round {round_number}"
            f" beside {path} ({exc.__class__.__name__}).",
        )


def _load_confirm_park(
    task: TicketTask, client_name: str
) -> tuple[ReviewVerdict, _LedgerOverridePlan | None]:
    """The verdict and ledger plan of a ``review_confirm_must_fix`` park (#3042).

    Requires a clean-mode client and this ticket's ledger whose last round is
    a confirm round with a readable saved verdict that carries at least one
    MUST_FIX and no never-deferrable finding. A ledger already converged at
    that verdict's commit is the idempotent re-run (no plan); otherwise
    :func:`_override_plan` must hold. Every refusal runs before any write.
    """
    if not get_client(client_name).review.is_clean:
        _refuse_confirm_park(
            task,
            "the park is a clean-mode convergence stop, but the client is not"
            " on `review.convergence: clean`.",
        )
    path, ledger = _load_owned_ledger(task, client_name)
    verdict = _confirm_round_verdict(task, path, ledger)
    if not verdict.must_fix:
        _refuse_confirm_park(
            task,
            "the parked confirm round raised no MUST_FIX -- there is nothing to"
            " override (a never-deferrable coverage or acceptance SHOULD_FIX"
            " parked it).",
        )
    blocked = _never_deferrable(verdict)
    if blocked:
        _refuse_confirm_park(
            task,
            f"{len(blocked)} finding(s) carry a never-deferrable coverage or"
            " acceptance rule, which no operator can defer (RFC 0017 D-6).",
        )
    if ledger.status == LEDGER_STATUS_CONVERGED:
        if ledger.last_clean_sha != verdict.reviewed_sha:
            msg = (
                f"{_ledger_prefix(task)} the convergence ledger {path} converged"
                f" at {ledger.last_clean_sha!r}, not at the parked confirm"
                f" round's commit {verdict.reviewed_sha!r}.{_regress_exit(task)}"
            )
            raise ApproveGateError(msg)
        return verdict, None
    plan = _override_plan(path, ledger, verdict)
    if plan is None:
        _refuse_unconverged(task, path, ledger)
    return verdict, plan
