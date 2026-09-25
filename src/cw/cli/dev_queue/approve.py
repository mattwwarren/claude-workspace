"""Dev-queue ``approve`` command: plan/review/operator-signoff gate clearing,
the ``--post-marker`` audit comment, the ``--scope-drift`` grant (#2337), and
the ``--override-must-fix`` codex MUST_FIX override (#2205).

Split out of ``crud`` (#2337) so the approve surface -- which carries its own
tracker-comment machinery -- lives apart from the plain queue mutations.
"""

from __future__ import annotations

import click

from cw.cli._base import handle_errors
from cw.config import get_client, load_orchestrator_config
from cw.dev_queue import (
    approve_must_fix_override_ticket,
    approve_scope_drift_ticket,
    approve_ticket,
    resolve_client,
    revoke_plan_approval,
)
from cw.events import record_event
from cw.gh import FETCH_COMMENTS_TIMEOUT, fetch_issue_comments, post_issue_comment
from cw.models import (
    PLAN_APPROVED_FINGERPRINT_KEY,
    PLAN_PROMOTED_KEY,
    OrchestratorEventType,
)
from cw.plan_fingerprint import is_plan_draft_fingerprint
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.worktree import _git_dir

from ._group import dev_queue
from ._plan_marker import (
    _marker_present,
    _plan_approved_marker,
)


@dev_queue.command(name="revoke-plan-approval")
@click.argument("ticket_id")
@click.option("--client", "client", "-c", default=None, help="Client name.")
@handle_errors
def revoke_plan_approval_command(ticket_id: str, client: str | None) -> None:
    """Durably revoke a PLAN approval before applying new resolutions."""
    config = load_orchestrator_config()
    resolved = resolve_client(ticket_id, config, client)
    result = revoke_plan_approval(ticket_id, resolved)
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

# How much of the bound head SHA the --scope-drift success message shows.
_HEAD_SHA_DISPLAY_LEN = 12


def _bound_fingerprint(result: dict[str, str | bool | None]) -> str | None:
    """The approval's plan-draft fingerprint, or None when there is none.

    Shape-validates at the marker boundary (#2194): ``_stamp_plan_approval``
    records whatever string the sentinel emitted, unvalidated, and that value
    is agent-produced and lands in a tracker comment, where a ``-->`` fragment
    would break out of the HTML comment. A non-string (no approval bound a
    draft) is silently None; a malformed string warns and falls back to the
    unbound marker, which is the pre-#2194 behavior.

    The warning reports the value's length, never the value: it is untrusted
    text that could carry terminal control sequences, the length diagnoses the
    common truncation/padding failures, and the raw value stays inspectable on
    the session's ``last_result``.
    """
    raw = result[PLAN_APPROVED_FINGERPRINT_KEY]
    if not isinstance(raw, str):
        return None
    if is_plan_draft_fingerprint(raw):
        return raw
    click.echo(
        "--post-marker: the approval's plan-draft fingerprint is not a"
        f" 64-character lowercase hex digest (got {len(raw)} characters)"
        " — posting the unbound marker instead.",
        err=True,
    )
    return None


def _post_plan_approved_marker(
    ticket_id: str, resolved: str, result: dict[str, str | bool | None]
) -> bool:
    """Post (or dedup-skip) the plan-approved marker for ``--post-marker``.

    PLAN-stage only -- warns and returns False on any other stage. Returns
    True iff the marker is recorded on the issue after this call (either
    found already present, or just posted successfully); False on every
    warn / fail-closed / gh-failure path. See GitHub #1419.

    GitHub-only by construction (``gh issue comment``): when the client's
    tracker is positively known to be non-GitHub (e.g. ``linear``), the
    ``gh`` calls can only fail against that ticket id, so they are skipped
    and the operator is told the approval already lives on the dev-queue
    row (``plan_approved_at``), which the plan stage reads on re-dispatch.
    Fail-open on an unresolvable tracker, matching ``requeue.py``'s gate.

    The marker is bound to the approval's draft fingerprint
    (``<!-- auto-dev-plan-approved: <sha> -->``) when the recorded
    fingerprint is a 64-character lowercase hex digest, and is the bare
    ``<!-- auto-dev-plan-approved -->`` otherwise (no fingerprint, or a
    malformed one, which also warns). Dedup is exact-string on that marker,
    so re-approving a changed draft posts a fresh marker and re-approving the
    same draft does not (#2194). The marker is audit-only: nothing reads it
    back as approval evidence.
    """
    if result["from_stage"] != "plan":
        click.echo(
            f"--post-marker is PLAN-stage-only; ticket is at"
            f" {result['from_stage']!r} — marker not posted.",
            err=True,
        )
        return False

    client_cfg = get_client(resolved)
    tracker = resolve_tracker(client_cfg.workspace_path)
    if tracker is not None and tracker != TRACKER_GITHUB_ISSUES:
        click.echo(
            f"--post-marker: tracker is {tracker!r}, not GitHub — the"
            f" marker comment is not posted to {ticket_id} ({resolved})."
            " The approval is recorded on the dev-queue row"
            " (plan_approved_at) and the plan stage honors it on"
            " re-dispatch."
        )
        return False

    repo_cwd = _git_dir(client_cfg)
    comments = fetch_issue_comments(
        ticket_id, timeout=FETCH_COMMENTS_TIMEOUT, cwd=repo_cwd
    )
    if comments is None:
        click.echo(
            "--post-marker: could not verify existing comments — marker"
            " not posted (dedup check failed)."
        )
        return False

    fingerprint = _bound_fingerprint(result)
    marker = _plan_approved_marker(fingerprint)
    draft = f" for draft {fingerprint[:12]}" if fingerprint else ""

    if _marker_present(comments, marker):
        click.echo(
            f"--post-marker: plan-approved marker already present on"
            f" {ticket_id} ({resolved}){draft} — skipped (no duplicate"
            " posted)."
        )
        return True

    # operator_authored=True (#2097): this marker records the operator's own
    # `cw dev-queue approve --post-marker` invocation, so it must NOT carry the
    # agent-authored provenance marker every pipeline-written comment gets. It
    # is the single operator-decision channel through this choke point.
    post_result = post_issue_comment(
        ticket_id, marker, cwd=repo_cwd, operator_authored=True
    )
    if post_result is not None and post_result.returncode == 0:
        click.echo(
            f"--post-marker: posted the plan-approved marker comment"
            f" to {ticket_id} ({resolved}){draft}."
        )
        return True

    click.echo(
        f"--post-marker: failed to post the plan-approved marker"
        f" comment to {ticket_id} ({resolved}) — see gh error"
        " above.",
        err=True,
    )
    return False


def _tracker_is_github_or_unknown(client_name: str) -> bool:
    """True unless *client_name*'s tracker is positively non-GitHub.

    Gates the ``--post-marker`` hint printed after a #968 plan re-queue: the
    GitHub audit comment is meaningless advice on a Linear-tracked client.
    """
    tracker = resolve_tracker(get_client(client_name).workspace_path)
    return tracker is None or tracker == TRACKER_GITHUB_ISSUES


def _approve_scope_drift(ticket_id: str, resolved: str, scope_drift: str) -> None:
    """The ``--scope-drift`` branch of ``dev_queue_approve`` (#2337)."""
    extra_files = [path.strip() for path in scope_drift.split(",") if path.strip()]
    result = approve_scope_drift_ticket(
        ticket_id,
        resolved,
        extra_files,
    )
    click.echo(
        f"Approved scope drift for {ticket_id} ({resolved}):"
        f" {result['from_stage']} -> {result['to_stage']}. Allowed extra files:"
        f" {', '.join(result['extra_files'])}. Bound to branch head"
        f" {result['approved_head'][:_HEAD_SHA_DISPLAY_LEN]}; the next dispatch"
        " re-runs the scope-conformance gate with them allowed."
    )


def _approve_must_fix_override(ticket_id: str, resolved: str, reason: str) -> None:
    """The ``--override-must-fix`` branch of ``dev_queue_approve`` (#2205)."""
    result = approve_must_fix_override_ticket(ticket_id, resolved, reason)
    count = len(result["finding_ids"])
    noun = "finding" if count == 1 else "findings"
    actor = result["actor"] or "<unresolved operator>"
    click.echo(
        f"Recorded MUST_FIX override for {ticket_id} ({resolved}) at"
        f" {result['stage']}: {count} {noun} on reviewed commit"
        f" {result['reviewed_sha'][:_HEAD_SHA_DISPLAY_LEN]}, by {actor}. The row"
        " is unchanged; run `cw dev-queue requeue"
        f" {ticket_id} --client {resolved} --stage finalize` to ship it."
    )


@dev_queue.command(name="approve")
@click.argument("ticket_id")
@click.option("--client", "-c", default=None, help="Client name.")
@click.option(
    "--post-marker",
    "post_marker",
    is_flag=True,
    default=False,
    help=(
        "Post an audit-only plan-approved marker comment to the ticket,"
        " binding it to the approved draft's fingerprint:"
        " <!-- auto-dev-plan-approved: <sha> --> (the unbound"
        " <!-- auto-dev-plan-approved --> when no valid fingerprint is"
        " recorded). PLAN-stage only (warns and skips on other stages)."
        " Approving a changed draft posts a fresh marker; re-approving the"
        " same draft does not duplicate it. Nothing reads the marker back"
        " as approval evidence. Distinct from `add --signoff`, which"
        " requires operator signoff before a ticket ships, and from this"
        " command's own REVIEW-stage operator-signoff gate (see docstring"
        " above) — this flag only posts an audit-trail comment."
    ),
)
@click.option(
    "--scope-drift",
    "scope_drift",
    default=None,
    help=(
        "Approve plan_scope_drift for a ticket parked at IMPL: a"
        " comma-separated list of repo-relative paths the operator allows"
        " beyond the plan's Files Modified. Recorded on the row, bound to"
        " the branch head, and added to the scope-conformance gate's"
        " allowed set on the next dispatch. Mutually exclusive with"
        " --post-marker."
    ),
)
@click.option(
    "--override-must-fix",
    "override_must_fix",
    is_flag=True,
    default=False,
    help=(
        "Ship past a codex MUST_FIX park (#2205): records a durable"
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

    Pass --post-marker to also post the plan-approved audit marker on a
    PLAN-stage ticket (unrelated to the operator-signoff gate above or to
    `add --signoff`). The marker embeds the approved draft's fingerprint
    (<!-- auto-dev-plan-approved: <sha> -->) and is audit-only: nothing
    reads it back as approval evidence, which lives on the dev-queue row.
    Approving a changed draft posts a fresh marker; re-approving the same
    draft does not.

    --scope-drift approves operator-directed scope growth that the IMPL-stage
    scope-conformance gate (check_plan_scope_conformance.py) would otherwise
    block as plan_scope_drift — typically files a review-round direction asked
    for. The approval lists the extra paths, is bound to the branch head it
    was given for, and applies only to a row parked with blocked_reason
    plan_scope_drift. A head that moves before the next dispatch invalidates
    it, and the gate blocks again rather than trusting a stale approval.

    --override-must-fix records an operator decision to ship past a codex
    background review's MUST_FIX park (blocked_reason codex_must_fix_findings).
    It is bound to the worktree verdict's reviewed commit and its exact MUST_FIX
    findings, audited with --reason, and stamp-only: run `cw dev-queue requeue
    --stage finalize` afterward. FINALIZE refuses it if a new review round or a
    moved HEAD no longer matches. Distinct from `cw review settle`, which only
    suppresses a finding in a future review round.
    """
    if scope_drift is not None and post_marker:
        msg = "--scope-drift and --post-marker are mutually exclusive"
        raise click.UsageError(msg)
    if override_must_fix and (post_marker or scope_drift is not None):
        msg = (
            "--override-must-fix is mutually exclusive with --post-marker and"
            " --scope-drift"
        )
        raise click.UsageError(msg)
    if override_must_fix:
        if override_reason is None:
            msg = "--override-must-fix requires --reason"
            raise click.UsageError(msg)
        resolved = resolve_client(ticket_id, load_orchestrator_config(), client)
        _approve_must_fix_override(ticket_id, resolved, override_reason)
        return
    config = load_orchestrator_config()
    resolved = resolve_client(ticket_id, config, client)
    if scope_drift is not None:
        _approve_scope_drift(ticket_id, resolved, scope_drift)
        return
    result = approve_ticket(ticket_id, resolved)
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
    marker_already_recorded = False
    if post_marker:
        marker_already_recorded = _post_plan_approved_marker(
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
            " is recorded on the dev-queue row (plan_approved_at); the"
            " re-dispatched plan stage treats it as operator approval."
        )
        if not marker_already_recorded and _tracker_is_github_or_unknown(resolved):
            click.echo(
                "Pass --post-marker to also post the plan-approved audit"
                " marker comment on this ticket."
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
