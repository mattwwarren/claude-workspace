"""Dev-queue ``approve`` command: plan/review/operator-signoff gate clearing,
the ``--post-marker`` audit comment, the ``--scope-drift`` grant (#2337), and
the ``--override-must-fix`` codex MUST_FIX override (#2205), which also
releases a ``review_confirm_must_fix`` park (#3042).

Split out of ``crud`` (#2337) so the approve surface -- which carries its own
tracker-comment machinery -- lives apart from the plain queue mutations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from cw.cli._base import handle_errors
from cw.cli._worker_guard import refuse_inside_dispatch_worker
from cw.cli.routed_orphan_close import close_routed_result_sessions_for_ticket
from cw.config import get_client, load_orchestrator_config
from cw.dev_queue import (
    BODY_DRIFT_WARNING_KEY,
    approve_must_fix_override_ticket,
    approve_scope_drift_ticket,
    approve_ticket,
    resolve_client,
    revoke_plan_approval,
)
from cw.dev_queue.approval import (
    AUDIT_EVENT_RECORDED_KEY,
    REVIEW_APPROVED_SHA_KEY,
    REVIEW_APPROVER_KIND_KEY,
    REVIEW_LESS_RELEASE_KEY,
)
from cw.dev_queue.next_commands import human_gate_settled_notice, is_human_kept_gate
from cw.events import record_event
from cw.exceptions import ApproveGateError, CwError
from cw.models import (
    PLAN_APPROVED_FINGERPRINT_KEY,
    PLAN_PROMOTED_KEY,
    OrchestratorEventType,
)
from cw.outbox_producer import (
    SKIP_ALREADY_POSTED,
    enqueue_prose,
    entry_state_label,
)
from cw.plan_fingerprint import is_plan_draft_fingerprint
from cw.pr_hydrate import _resolve_repo_slug
from cw.worktree import _git_dir

from ._group import dev_queue
from ._loop_warning import warn_if_dispatch_loop_stale
from ._plan_marker import (
    _plan_approved_marker,
    _plan_approved_outbox_marker,
)
from ._settle import SettleOutcome, settle_lagging_row
from .show import echo_resulting_row

if TYPE_CHECKING:
    from cw.models import OrchestratorConfig


@dev_queue.command(name="revoke-plan-approval")
@click.argument("ticket_id")
@click.option("--client", "client", "-c", default=None, help="Client name.")
@handle_errors
def revoke_plan_approval_command(ticket_id: str, client: str | None) -> None:
    """Durably revoke a PLAN approval before applying new resolutions."""
    config = load_orchestrator_config()
    resolved = resolve_client(ticket_id, config, client)
    # bounded=True (#2501): nothing precedes the lock. The headless plan-stage
    # worker also runs this command; a timeout makes it exit blocked (fail
    # closed) instead of hanging behind a wedged queue lock.
    result = revoke_plan_approval(ticket_id, resolved, bounded=True)
    if result["cleared"]:
        click.echo(
            f"Revoked plan approval for {ticket_id} ({resolved}); "
            "plan_approved_at and plan_approved_fingerprint cleared."
        )
    else:
        click.echo(f"No durable plan approval to revoke for {ticket_id} ({resolved}).")


# The plan-approved marker strings themselves live in `_plan_marker` (#2194),
# alongside their shape validator -- and are distinct from lifecycle.py's
# `_PLAN_SPEC_MARKER`/`_PLAN_SOUNDNESS_MARKER` pair (the coded
# plan-quality-review gate). See GitHub #1419.

# Refusal shown when `approve` runs inside a headless dispatch worker (#2776).
_APPROVE_IN_WORKER_MSG = (
    "Refusing to approve a gate from inside a dispatch worker "
    "(.claude/cw-context.json reports headless). An approval is the evidence "
    "a downstream plan or ship gate trusts, so a worker must not mint its "
    "own. Run `cw dev-queue approve` from the orchestrator session."
)

# How much of the bound head SHA the --scope-drift success message shows.
_HEAD_SHA_DISPLAY_LEN = 12


def _bound_fingerprint(result: dict[str, str | bool | None]) -> str | None:
    """The approval's plan-draft fingerprint, or None when there is none.

    ``_stamp_plan_approval`` records only a well-formed digest or None
    (#2382: a malformed agent-produced value is stamped as None with a
    warning that reports its length, never the value), so the shape check
    here is a boundary guard on the value that lands in a tracker comment,
    where a ``-->`` fragment would break out of the HTML comment. A
    non-string (no approval bound a draft) is None.
    """
    raw = result[PLAN_APPROVED_FINGERPRINT_KEY]
    return raw if isinstance(raw, str) and is_plan_draft_fingerprint(raw) else None


def _post_plan_approved_marker(
    ticket_id: str, resolved: str, result: dict[str, str | bool | None]
) -> bool:
    """Queue the plan-approved marker for ``--post-marker`` on the outbox.

    PLAN-stage only -- warns and returns False on any other stage. Returns
    True iff an entry for the marker exists after this call (just queued, or
    already queued); False on the warn and enqueue-failure paths. The marker
    is queued, never posted here: the outbox drain delivers it on any tracker
    (RFC 0019 O3b). See GitHub #1419.

    The marker is bound to the approval's draft fingerprint when the recorded
    fingerprint is well formed (#2194), and dedups on that entry marker: re-
    approving a changed draft queues a second entry, the same draft does not.
    The marker is audit-only: nothing reads it back as approval evidence.
    """
    if result["from_stage"] != "plan":
        click.echo(
            f"--post-marker is PLAN-stage-only; ticket is at"
            f" {result['from_stage']!r} — marker not posted.",
            err=True,
        )
        return False

    fingerprint = _bound_fingerprint(result)
    draft = f" for draft {fingerprint[:12]}" if fingerprint else ""
    # operator_authored=True (#2097): this marker records the operator's own
    # `cw dev-queue approve --post-marker` invocation, so it must NOT carry the
    # agent-authored provenance marker every pipeline-written comment gets.
    outcome = enqueue_prose(
        resolved,
        ticket_id,
        "approve-post-marker",
        _plan_approved_outbox_marker(fingerprint),
        (_plan_approved_marker(fingerprint),),
        operator_authored=True,
    )
    if outcome.created:
        click.echo(
            f"--post-marker: queued the plan-approved marker comment for"
            f" {ticket_id} ({resolved}){draft} (outbox entry {outcome.entry_id})."
        )
        return True
    if outcome.skipped_reason == SKIP_ALREADY_POSTED:
        state = entry_state_label(resolved, outcome.entry_id or "")
        click.echo(
            f"--post-marker: the plan-approved marker comment for {ticket_id}"
            f" ({resolved}){draft} is already queued (entry {outcome.entry_id},"
            f" state {state}) — no duplicate queued."
        )
        return True
    click.echo(
        f"--post-marker: could not queue the plan-approved marker comment for"
        f" {ticket_id} ({resolved}){draft} ({outcome.skipped_reason}); the"
        " approval is recorded on the row regardless.",
        err=True,
    )
    return False


def _close_orphans_after_approve(
    ticket_id: str, resolved: str, config: OrchestratorConfig, *, command: str
) -> None:
    """Close the ticket's orphaned routed-result sessions after the approval.

    Runs after the transition succeeded (#2517), so nothing irreversible
    precedes it; a close that cannot be confirmed raises a ``CwError`` that
    says the ticket is already approved.
    """
    close_routed_result_sessions_for_ticket(
        ticket_id, resolved, command=command, config=config, after_transition=True
    )


def _grant_repo_slug(client_name: str) -> str | None:
    """The origin ``owner/repo`` a scope-drift grant binds to, resolved here.

    Resolved in the CLI, before the queue lock: ``cw.dev_queue`` cannot import
    ``cw.pr_hydrate`` (import cycle), and the slug is a git read (#3772). ``None``
    when the client is unknown or the origin is not a GitHub remote; the
    approval then records an unbound grant that the carry will not reuse.
    """
    try:
        return _resolve_repo_slug(_git_dir(get_client(client_name)))
    except (CwError, OSError, ValueError):
        return None


def _approve_scope_drift(
    ticket_id: str, resolved: str, scope_drift: str, config: OrchestratorConfig
) -> None:
    """The ``--scope-drift`` branch of ``dev_queue_approve`` (#2337)."""
    extra_files = [path.strip() for path in scope_drift.split(",") if path.strip()]
    # bounded=True (#2501): operator command. settle_lagging_row (#2692) may
    # first route this row's recorded sentinel under its OWN bounded lock (the
    # routing the dispatch loop would apply); a timeout here leaves the row
    # parked where the loop would have put it, a clean retry. The routed-orphan
    # close (#2517) runs after the transition.
    result = approve_scope_drift_ticket(
        ticket_id,
        resolved,
        extra_files,
        bounded=True,
        repo=_grant_repo_slug(resolved),
    )
    click.echo(
        f"Approved scope drift for {ticket_id} ({resolved}):"
        f" {result['from_stage']} -> {result['to_stage']}. Allowed extra files:"
        f" {', '.join(result['extra_files'])}. Bound to branch head"
        f" {result['approved_head'][:_HEAD_SHA_DISPLAY_LEN]}; the next dispatch"
        " re-runs the scope-conformance gate with them allowed."
    )
    dropped = result["dropped_review_feedback_sha"]
    if dropped is not None:
        click.echo(
            f"Dropped stale review feedback pinned to {dropped[:_HEAD_SHA_DISPLAY_LEN]}"
            " (the branch head moved past it). The re-run implements the plan"
            " without it; for a fresh report, requeue with"
            f" `cw dev-queue requeue {ticket_id} -c {resolved} --stage impl"
            " --review-feedback-file <file>`."
        )
    echo_resulting_row(ticket_id, resolved)
    _close_orphans_after_approve(
        ticket_id, resolved, config, command="approve --scope-drift"
    )


def _echo_review_approval(
    ticket_id: str, resolved: str, result: dict[str, str | bool | None]
) -> None:
    """Say which reviewed head a REVIEW-stage approval was bound to (#2767).

    Prints only when this call recorded a review approval. The two keys are
    read with ``.get`` (A12): a result without them recorded none.
    """
    kind = result.get(REVIEW_APPROVER_KIND_KEY)
    if kind is None:
        return
    sha = result.get(REVIEW_APPROVED_SHA_KEY)
    if isinstance(sha, str):
        tail = (
            f"reviewed head {sha[:_HEAD_SHA_DISPLAY_LEN]}. Finalize ships the"
            " branch only if that commit is still in its history and nothing"
            " but merges of the default branch lands after it; any other later"
            " commit parks it at finalize, to be returned to review for"
            " re-approval."
        )
    else:
        tail = (
            "reviewed head <not reported by the review sentinel>. Finalize"
            " cannot match a head to it and will park the ticket at finalize,"
            " to be returned to review for re-approval."
        )
    click.echo(
        f"Review approval recorded for {ticket_id} ({resolved}):"
        f" approver_kind={kind}, {tail}"
    )


def _echo_review_less_release(
    ticket_id: str, resolved: str, result: dict[str, str | bool | None]
) -> None:
    """Report a #2779 review-less release and show the row.

    The library recorded the release's own ``TICKET_APPROVED`` event, so no
    generic CLI event is added, and the row stays parked, so there is no
    routed-result orphan to close.
    """
    head = str(result[REVIEW_APPROVED_SHA_KEY])[:_HEAD_SHA_DISPLAY_LEN]
    click.echo(
        f"Review-less release recorded for {ticket_id} ({resolved}):"
        f" approver_kind=operator, branch head {head}. This lane has no review"
        " stage, so nothing reviewed that head and you are vouching for it;"
        " inspect it first (run `git log --oneline` in the ticket's worktree)."
        " The row is still parked; run `cw dev-queue requeue"
        f" {ticket_id} --client {resolved} --stage finalize` to ship it."
        " Finalize ships only while that commit is still in its history and"
        " nothing but merges of the default branch lands after it; any other"
        " later commit parks it again, to be re-approved with this command."
    )
    echo_resulting_row(ticket_id, resolved)


def _approve_must_fix_override(ticket_id: str, resolved: str, reason: str) -> None:
    """The ``--override-must-fix`` branch of ``dev_queue_approve`` (#2205)."""
    # bounded=True (#2501): operator command. settle_lagging_row (#2692) may
    # first route this row's recorded sentinel under its OWN bounded lock (the
    # routing the dispatch loop would apply); a timeout here leaves the row
    # parked where the loop would have put it, a clean retry.
    result = approve_must_fix_override_ticket(ticket_id, resolved, reason, bounded=True)
    count = len(result["finding_ids"])
    noun = "finding" if count == 1 else "findings"
    actor = result["actor"] or "<unresolved operator>"
    message = (
        f"Recorded MUST_FIX override for {ticket_id} ({resolved}) at"
        f" {result['stage']}: {count} {noun} on reviewed commit"
        f" {result['reviewed_sha'][:_HEAD_SHA_DISPLAY_LEN]}, by {actor}. The row"
        " is unchanged; run `cw dev-queue requeue"
        f" {ticket_id} --client {resolved} --stage finalize` to ship it."
        " The override also records a review approval bound to that commit, so"
        " a large ticket passes finalize's approval check while HEAD stays on"
        " it. If the ticket or lane has a finalize hold, operator signoff or"
        " scope_hint: large, this step is the human's."
    )
    overridden_round = result.get("ledger_overridden_round")
    if overridden_round is not None:
        # #2834: the override converged the clean-mode ledger itself.
        message += (
            " The override also converged the clean-mode convergence ledger"
            f" past Claude confirm round {overridden_round}: its findings are"
            " settled as operator-deferred, and `cw review converge"
            " history-comment` shows the audited override."
        )
    click.echo(message)
    echo_resulting_row(ticket_id, resolved)


def _raise_on_exclusive_flags(
    *, post_marker: bool, scope_drift: str | None, override_must_fix: bool
) -> None:
    """Refuse the mutually exclusive approve flags (PLR0912 extraction)."""
    if scope_drift is not None and post_marker:
        msg = "--scope-drift and --post-marker are mutually exclusive"
        raise click.UsageError(msg)
    if override_must_fix and (post_marker or scope_drift is not None):
        msg = (
            "--override-must-fix is mutually exclusive with --post-marker and"
            " --scope-drift"
        )
        raise click.UsageError(msg)


def _raise_if_settled_into_human_gate(outcome: SettleOutcome) -> None:
    """Stop a plain approve whose settle just routed the row to a human gate.

    #2692 R8: the caller never saw the parked row, so releasing it in the
    same invocation would let one blind ``approve`` ship past a gate the
    human opted into. An explicit second ``approve`` releases it.
    """
    if outcome.routed and outcome.row is not None and is_human_kept_gate(outcome.row):
        raise ApproveGateError(human_gate_settled_notice(outcome.row))


@dev_queue.command(name="approve")
@click.argument("ticket_id")
@click.option("--client", "-c", default=None, help="Client name.")
@click.option(
    "--post-marker",
    "post_marker",
    is_flag=True,
    default=False,
    help=(
        "Queue an audit-only plan-approved marker comment on the ticket through"
        " the outbox (any tracker; the drain posts it), binding it to the"
        " approved draft's fingerprint:"
        " <!-- auto-dev-plan-approved: <sha> --> (the unbound"
        " <!-- auto-dev-plan-approved --> when no valid fingerprint is"
        " recorded). PLAN-stage only (warns and skips on other stages)."
        " Approving a changed draft posts a fresh marker; re-approving the"
        " same draft does not duplicate it. Nothing reads the marker back"
        " as approval evidence. Distinct from `add --signoff`, which"
        " requires operator signoff before a ticket ships, and from this"
        " command's own REVIEW-stage operator-signoff gate (see docstring"
        " above) — this flag only queues an audit-trail comment."
    ),
)
@click.option(
    "--scope-drift",
    "scope_drift",
    default=None,
    help=(
        "Approve plan_scope_drift for a ticket parked at IMPL: a"
        " comma-separated list of exact repo-relative paths (no globs or"
        " directories) the operator allows beyond the plan's Files Modified."
        " Recorded on the row as a grant bound to the branch head, repository,"
        " review policy and plan of record. A later repair dispatch re-uses"
        " the grant while it still verifies, and only new paths are"
        " re-presented. Mutually exclusive with --post-marker."
    ),
)
@click.option(
    "--override-must-fix",
    "override_must_fix",
    is_flag=True,
    default=False,
    help=(
        "Ship past a codex MUST_FIX park (#2205) or a"
        " review_confirm_must_fix park (#3042, the confirm round after a"
        " repo review engine pass; its saved round verdict is the one"
        " overridden): records a durable"
        " operator override on the ticket, bound to the reviewed commit"
        " and the exact MUST_FIX findings on that verdict. The override"
        " is recorded, audited (actor, reason, reviewed SHA, finding"
        " identities), and does not itself advance the stage -- run"
        " `cw dev-queue requeue --stage finalize` afterward. A later"
        " review round or a moved HEAD invalidates it; FINALIZE"
        " re-checks both before shipping. Requires --reason. Mutually"
        " exclusive with --post-marker and --scope-drift."
    ),
)
@click.option(
    "--reason",
    "override_reason",
    default=None,
    help=(
        "Operator justification for --override-must-fix, recorded"
        " verbatim on the override record and rendered into the PR"
        " body's Operator override section. Required when"
        " --override-must-fix is passed; ignored otherwise."
    ),
)
@handle_errors
def dev_queue_approve(
    ticket_id: str,
    client: str | None,
    post_marker: bool,
    scope_drift: str | None,
    override_must_fix: bool,
    override_reason: str | None,
) -> None:
    """Approve a plan/review gate, or clear an operator-signoff gate.

    The ticket must be BLOCKED_ON_USER with last_result status of
    plan_pending_approval or review_pending_approval, or already parked
    AWAITING_OPERATOR_SIGNOFF (RFC 0007 Phase 3). Approving a REVIEW-stage
    gate on a ticket with signoff configured re-routes it to
    AWAITING_OPERATOR_SIGNOFF instead of advancing -- run `approve` again
    to clear it.

    A REVIEW-stage approval is recorded on the row, bound to the head the
    review ran against, and the command prints that head. Finalize ships a
    large ticket only while that commit is in its history with nothing but
    merges of the default branch after it; otherwise it parks at finalize and
    is returned to review (`requeue --stage review`) for re-approval. A
    ticket parked at finalize itself is refused on a lane with a review
    stage: `approve` never releases the terminal stage there. On a lane with
    no review stage, approving a large ticket parked at finalize's approval
    check records an operator approval bound to the branch head as it stands
    (nothing reviewed it, so you vouch for it) and prints that head; the row
    stays parked until `requeue --stage finalize` ships it (#2779).

    On success, a leftover routed-result session for the ticket -- one that
    already routed its result but was never completed because background
    work was still running -- is closed once its worker is confirmed gone
    from the daemon roster. A session that an occupied row still binds, or
    whose background work is still draining, is left running and named on
    stderr. If the stop cannot be confirmed the command exits non-zero AFTER
    the approval took effect: the ticket is approved and its row released,
    so do not re-run `approve` (the row is already PENDING and a retry never
    reaches the close); close the session with the `cw spawn close
    --confirmed-dead <id>` line the message prints once the worker is gone.
    Any caller that is not a dispatch worker -- the orchestrator session
    included -- closes such a session; the unattended approvals (auto-adopt
    gate recipe, `drain`, `auto_fix_ci`) do not.

    Refused inside a dispatch worker (the check `cw dev-queue resolve` and
    `cw review settle` apply): an approval is evidence a worker must not mint
    for its own run (#2776). `revoke-plan-approval` stays callable by the
    plan-stage worker.

    Pass --post-marker to also queue the plan-approved audit marker on a
    PLAN-stage ticket through the outbox, for any tracker (unrelated to the
    operator-signoff gate above or to `add --signoff`). The marker embeds the
    approved draft's fingerprint (<!-- auto-dev-plan-approved: <sha> -->) and
    is audit-only: nothing reads it back as approval evidence, which lives on
    the dev-queue row. Approving a changed draft queues a fresh marker;
    re-approving the same draft does not.

    A PLAN-stage approve warns on stderr when the GitHub ticket body changed
    after the plan stage last evaluated it. The warning is advisory and never
    blocks. The safe order for a body edit is: edit the body, run `requeue
    --stage plan` while the ticket is parked, then approve.

    --scope-drift approves operator-directed scope growth that the IMPL-stage
    scope-conformance gate (check_plan_scope_conformance.py) would otherwise
    block as plan_scope_drift — typically files a review-round direction asked
    for. The approval records a grant of exact paths, bound to the branch head,
    the repository, the review policy and the plan of record, and applies only
    to a row parked with blocked_reason plan_scope_drift. A later IMPL dispatch
    re-uses the grant only while it still verifies (the head is still an
    ancestor of HEAD, and the binding has not changed); only paths the grant
    does not cover are presented again. A regress into PLAN drops the grants.

    --override-must-fix records an operator decision to ship past a codex
    background review's MUST_FIX park (blocked_reason codex_must_fix_findings),
    or past the confirm round after a repo review engine pass (blocked_reason
    review_confirm_must_fix, #3042), whose saved round verdict it reads and
    whose convergence ledger it converges. It is bound to the verdict's
    reviewed commit and its exact MUST_FIX findings, audited with --reason,
    and stamp-only: run `cw dev-queue requeue --stage finalize` afterward.
    It also records an operator review approval bound to that commit, so a
    large ticket passes finalize's approval check while HEAD stays on it.
    FINALIZE refuses it if a new review round or a moved HEAD no longer
    matches. Distinct from `cw review settle`, which only suppresses a finding
    in a future review round.
    """
    refuse_inside_dispatch_worker(_APPROVE_IN_WORKER_MSG)
    _raise_on_exclusive_flags(
        post_marker=post_marker,
        scope_drift=scope_drift,
        override_must_fix=override_must_fix,
    )
    # #2729: before any of the three write paths below.
    warn_if_dispatch_loop_stale()
    if override_must_fix:
        if override_reason is None:
            msg = "--override-must-fix requires --reason"
            raise click.UsageError(msg)
        resolved = resolve_client(ticket_id, load_orchestrator_config(), client)
        settle_lagging_row(ticket_id, resolved)
        _approve_must_fix_override(ticket_id, resolved, override_reason)
        return
    config = load_orchestrator_config()
    resolved = resolve_client(ticket_id, config, client)
    outcome = settle_lagging_row(ticket_id, resolved)
    if scope_drift is not None:
        _approve_scope_drift(ticket_id, resolved, scope_drift, config)
        return
    _raise_if_settled_into_human_gate(outcome)
    # bounded=True (#2501): operator command. settle_lagging_row (#2692) may
    # first route this row's recorded sentinel under its OWN bounded lock (the
    # routing the dispatch loop would apply); a timeout here leaves the row
    # parked where the loop would have put it, a clean retry. The routed-orphan
    # close (#2517) runs after the transition.
    result = approve_ticket(ticket_id, resolved, bounded=True)
    if result.get(REVIEW_LESS_RELEASE_KEY):
        _echo_review_less_release(ticket_id, resolved, result)
        return
    # #2311: advisory only -- the approval already landed. approve_ticket
    # recorded the audit event itself; the CLI just surfaces the warning.
    body_drift_warning = result[BODY_DRIFT_WARNING_KEY]
    if body_drift_warning:
        click.echo(body_drift_warning, err=True)
    record_event(
        OrchestratorEventType.TICKET_APPROVED,
        {
            "ticket_id": ticket_id,
            "client": resolved,
            "from_stage": result["from_stage"],
            "to_stage": result["to_stage"],
            "awaiting_signoff": result["awaiting_signoff"],
            "plan_requeued": result["plan_requeued"],
        },
    )
    marker_queued = False
    if post_marker:
        marker_queued = _post_plan_approved_marker(
            ticket_id=ticket_id, resolved=resolved, result=result
        )
    if result["awaiting_signoff"]:
        click.echo(
            f"Approved {ticket_id} ({resolved}): parked at"
            f" {result['from_stage']} awaiting operator signoff before it ships."
            " Run 'approve' again to clear the gate."
        )
    elif result["plan_requeued"]:
        click.echo(
            f"Approved {ticket_id} ({resolved}): plan not yet quality-reviewed"
            " — re-queued at plan stage to run Plan Quality Review."
            " Re-run auto-dev-plan (or dispatch) to proceed. The approval"
            " is recorded on the dev-queue row (plan_approved_at +"
            " plan_approved_fingerprint), bound to the draft as it stands now;"
            " the re-dispatched plan stage accepts it while that draft is"
            " unchanged, or while no operator comment postdates the approval."
            " A changed draft with a newer operator comment re-parks"
            " plan_pending_approval, quoting both fingerprints."
        )
        if not marker_queued:
            click.echo(
                "Pass --post-marker to also queue the plan-approved audit"
                " marker comment on this ticket. The marker is audit-only;"
                " nothing reads it back as approval evidence."
            )
    else:
        promoted_note = (
            " (promoted the approved .cw/plan-draft.md to .cw/plan.md)"
            if result[PLAN_PROMOTED_KEY]
            else ""
        )
        click.echo(
            f"Approved {ticket_id} ({resolved}):"
            f" {result['from_stage']} -> {result['to_stage']}{promoted_note}"
        )
    _echo_review_approval(ticket_id, resolved, result)
    if result.get(AUDIT_EVENT_RECORDED_KEY) is False:
        click.echo(
            f"Approval recorded for {ticket_id} ({resolved}), but its audit"
            " event was not written (see the log); the approval stands.",
            err=True,
        )
    # After the whole confirmation chain (incl. the --post-marker tip) and
    # before the orphan close, so a close failure still shows the row.
    echo_resulting_row(ticket_id, resolved)
    _close_orphans_after_approve(ticket_id, resolved, config, command="approve")
