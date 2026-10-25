"""Rule 5 of the staged-routing decision table: the stage-failure statuses.

Owns ``_route_stage_failure`` (the ``STAGE_FAILURE_STATUSES`` branch of
``_route_staged_decision``), its sole reason-keyed override
``_park_must_fix_mechanically_rejected`` (#1714), the breadcrumb renderer
``_breadcrumb_for_reason`` and the paused-status constants Rule 5 writes.

Split out of ``routing/__init__`` (#2719) to bring that module under the
~1000-line ceiling. This relaxes the #1728 monkeypatch-coupling pin for Rule 5
only: Rule 5's free names (``record_event``, ``_stage_regress``,
``resolve_task_worktree``, ``merge_in_progress``) resolve against this module,
so tests patch ``cw.dispatch.routing.stage_failure.<name>``. Rules 1-4 and 6
and ``_stage_advance_unchecked`` stay defined in ``routing/__init__``.

Imports only leaves plus the sibling ``stage_walk`` and ``pr_refs`` submodules,
never ``routing/__init__``, which imports this module at its top and re-exports
``_AWAITING_OPERATOR_REASON`` and ``BREADCRUMB_ELIGIBLE_PAUSED_STATUSES`` for
the ``cw.dispatch`` facade. Logs on the package's shared ``cw.dispatch``
logger.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.auto_dev_result import (
    FINALIZE_REGRESS_BLOCKER_REASONS,
    FINALIZE_REGRESS_CAP,
    OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
    STAGE_FAILURE_STATUSES,
    STALE_DISPATCH_BLOCKER_REASON,
    is_known_blocker_reason,
)
from cw.branch_ahead import merge_in_progress
from cw.codex_review import (
    CODEX_MUST_FIX_MECHANICALLY_REJECTED,
    CODEX_REVIEW_UNPARSEABLE,
)
from cw.dev_queue import (
    REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
    _extract_pr_url_or_info,
    _stage_regress,
    transition_task_status,
)
from cw.dev_queue.gate_backoff import is_gate_block, record_gate_block
from cw.dispatch.routing.pr_refs import _extract_blocked_on_pr
from cw.dispatch.routing.stage_walk import _is_earlier_stage_report
from cw.events import record_event
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from cw.queue_rows import (
    _AUTOMERGE_NOT_ARMED_REASON,
    _PRIOR_PIPELINE_PR_OPEN_REASON,
)
from cw.reconcile.fix_dispatch import FIX_LOOP_PENDING_DISPATCH
from cw.unavailability import FAMILY_PROVIDER_OVERLOAD
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        TicketTask,
    )

_log = logging.getLogger("cw.dispatch")


# paused_status written to SESSION_NEEDS_ATTENTION when Rule 5's blocked
# status carries a blocker reason in OPERATOR_UNAVAILABLE_BLOCKER_REASONS
# (RFC 0011 A1). Deliberately distinct from QueueItemStatus.
# AWAITING_OPERATOR_SIGNOFF -- that enum member is a REVIEW-stage signoff
# gate; this constant is a paused_status string for an operator/dependency
# unavailability, an unrelated concept that happens to share the "awaiting
# operator" phrase. See GitHub #1155.
_AWAITING_OPERATOR_REASON = "awaiting_operator_availability"


# paused_status written to SESSION_NEEDS_ATTENTION when the #1714 gate parks a
# ticket whose blocked sentinel reports that review's only MUST_FIX finding(s)
# were mechanically rejected before adjudication. Shares its literal string
# value with dev_queue.dispositions.REVIEW_MUST_FIX_MECHANICALLY_REJECTED_
# DISPOSITION (task.disposition) on the same _SIGNOFF_GATE_REASON /
# _REVIEW_HEALTH_GATE_REASON precedent above -- still two constants in two
# namespaces, do not collapse them. Distinct in turn from
# codex_review.CODEX_MUST_FIX_MECHANICALLY_REJECTED, which is the *blocker
# reason* the sentinel carries; that one is imported, not re-derived.
_MUST_FIX_MECHANICALLY_REJECTED_REASON = "codex_must_fix_mechanically_rejected"


# Paused-status values that carry a non-empty breadcrumbs string derived
# verbatim from blocker.reason in Rule 5's SESSION_NEEDS_ATTENTION payload
# below (GitHub #1511) -- every STAGE_FAILURE_STATUSES member for which the
# schema allows a non-null blocker (schema.py's #777 exception:
# "blocked"/"merge_gate_blocked" only -- scope_exceeded/forbidden_area never
# carry one, by design), plus two substitutes: _AWAITING_OPERATOR_REASON,
# which the ternary below writes when that blocker's reason is in
# OPERATOR_UNAVAILABLE_BLOCKER_REASONS, and (#1729)
# _MUST_FIX_MECHANICALLY_REJECTED_REASON, stamped by
# _park_must_fix_mechanically_rejected above -- the one gate-class park whose
# breadcrumbs is genuinely populated from blocker.reason rather than a
# hardcoded "" literal. Named + anchored so the attention stream has one
# allowlist to render breadcrumbs against. See #1597, #2250, #2693. A
# gate-block park (merge_gate_blocked, or a prior_pipeline_pr_open blocker)
# additionally appends its #2717 backoff note `occurrence=N
# next_eligible_at=<iso>`, so merge_gate_blocked is non-empty even with no
# blocker.
#
# IMPORTANT: its one runtime reader is cw.attention_watch (``cw orchestrate
# watch``), which imports it directly to decide which pages render their
# breadcrumbs; it only *renders* breadcrumbs that a park already stamped.
# Adding a paused_status here does NOT by itself cause a breadcrumb to be
# emitted for it: the producing _park_* helper must independently stamp
# non-empty breadcrumbs content at its own call site. Every gate-class park other than
# _park_must_fix_mechanically_rejected (review_gates.py's _park_finalize_hold,
# _park_signoff_gate, _park_scope_hint_gate, _park_review_health_gate and
# _park_branch_staleness_gate, plus the _stage_advance_unchecked config-error
# paths here) hardcodes breadcrumbs="" and is deliberately excluded from this
# set -- membership for one of those would be cosmetic, not a fix (#1729).
#
# #1775 reaffirms this for _park_review_health_gate specifically: a degraded
# reviewer's stated reason (ReviewerRunRecord.detail, threaded through
# cw.review_findings.consolidate_verdict and rendered by
# cw.codex_review._verdict._render._render_degraded_roles_note) lives entirely
# inside the review-executor process and is never written onto the
# session.last_result sentinel this dispatch-loop process reads -- there is no
# existing data path from that detail into _park_review_health_gate's
# breadcrumbs/needs_attention payload. Threading one in was considered and
# declined this round: render_verdict_comment already posts the degraded-role
# note to the ticket on every completed review pass, including the
# stage_complete path that reaches this park (codex_background.py posts it
# whenever verdict is not None), so the reason is already a durable,
# operator-visible artifact before the park happens. Adding a second copy via
# breadcrumbs would duplicate that, not fix a silence. See #1607/#1754.
BREADCRUMB_ELIGIBLE_PAUSED_STATUSES: frozenset[str] = (
    STAGE_FAILURE_STATUSES - {"scope_exceeded", "forbidden_area"}
) | {_AWAITING_OPERATOR_REASON, _MUST_FIX_MECHANICALLY_REJECTED_REASON}


# Appended to Rule 5's breadcrumb when the sentinel's blocker.reason is
# neither registered in KNOWN_BLOCKER_REASONS nor declared freeform via the
# `x_` prefix (#2097). The reason itself still travels verbatim -- open enum,
# §4.2 -- but an operator reading `session.needs_attention` can now tell an
# invented reason from a documented routing code without cross-checking the
# contract by hand, which is the failure the #2097 incident turned on.
_UNRECOGNIZED_REASON_SUFFIX = " (unrecognized)"
# Max blocker.details chars appended to an unrecognized-reason breadcrumb
# (#2415). Hard cutoff, no word-boundary awareness -- the breadcrumb is a
# triage hint, not a full transcript.
_UNRECOGNIZED_REASON_DETAILS_MAX_CHARS = 200


def _breadcrumb_for_reason(blocker_reason: str | None, blocker: object | None) -> str:
    """Render Rule 5's SESSION_NEEDS_ATTENTION breadcrumb (#1511, #2097, #2415).

    Verbatim reason for a registered or `x_`-declared one; the same string
    plus :data:`_UNRECOGNIZED_REASON_SUFFIX` otherwise. Empty when the
    sentinel carried no blocker at all, exactly as before #2097.

    #2415: an unrecognized reason additionally appends a bounded slice of
    `blocker.details` (200 chars, hard cutoff, trailing `…` only when actually
    truncated) -- an operator reading the breadcrumb alone can then triage an
    invented reason without opening the transcript.
    """
    if blocker_reason is None:
        return ""
    reason = str(blocker_reason)
    if is_known_blocker_reason(reason):
        return reason
    unrecognized = f"{reason}{_UNRECOGNIZED_REASON_SUFFIX}"
    details = blocker.get("details") if isinstance(blocker, dict) else None
    if isinstance(details, str) and details:
        max_chars = _UNRECOGNIZED_REASON_DETAILS_MAX_CHARS
        if len(details) <= max_chars:
            return f"{unrecognized}: {details[:max_chars]}"
        return f"{unrecognized}: {details[:max_chars]}…"
    return unrecognized


def _gate_block_breadcrumb_note(
    task: TicketTask,
    status: str,
    blocker_reason: str | None,
    blocker: object | None,
) -> str:
    """Stamp the #2717 gate-block backoff and return its breadcrumb note.

    ``""`` (and no stamp) for a non-gate-block park. The blocking PR comes from
    ``blocker.details``, not ``task.blocked_on_pr``: that is only stamped after
    ``transition_task_status``, which clears it, so it is not set yet here.
    Runs before the page and the transition, while ``task.stage_base_ref`` is
    still the spawn-time HEAD the fingerprint keys on.
    """
    if not is_gate_block(status, blocker_reason):
        return ""
    details = blocker.get("details") if isinstance(blocker, dict) else None
    return record_gate_block(
        task,
        status=status,
        blocker_reason=blocker_reason,
        blocking_pr=_extract_blocked_on_pr(details),
        now=datetime.now(UTC),
    )


def _park_must_fix_mechanically_rejected(task: TicketTask) -> None:
    """Park *task* BLOCKED_ON_USER for a mechanically-rejected MUST_FIX (#1714).

    Rule 5's sole reason-keyed override: every other blocker_reason at the
    STAGE_FAILURE_STATUSES branch falls through to the generic
    ``_hold_aware_disposition(status, blocker_reason)`` stamp, which for a
    blocked status yields the verbatim string ``"blocked"``. This reason is
    neither an operator-unavailability hold nor an ordinary block -- the review
    ran and found something, but the finding was mechanically dropped before
    adjudication, which is a quality signal that must stay distinguishable from
    both.

    Field-for-field mirror of ``_park_review_health_gate`` (review_gates.py)
    with one deliberate difference: ``breadcrumbs`` carries the real
    ``blocker.reason`` here, because -- unlike the health gate -- this park
    genuinely originates from a populated blocker dict. That matches Rule 5's
    own generic breadcrumbs semantics further down this module.

    BLOCKED_ON_USER, not a hold disposition:
    ``REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION`` is deliberately
    excluded from ``HOLD_DISPOSITIONS`` -- it clears by re-running review with
    the finding adjudicated, not by an operator saying "proceed anyway",
    mirroring ``REVIEW_HEALTH_GATE_DISPOSITION``'s identical reasoning. Stamping
    it directly here (rather than teaching ``_hold_aware_disposition`` about the
    reason) is what guarantees it can never resolve to a hold.
    """
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _MUST_FIX_MECHANICALLY_REJECTED_REASON,
            "breadcrumbs": CODEX_MUST_FIX_MECHANICALLY_REJECTED,
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
        blocked_reason=CODEX_MUST_FIX_MECHANICALLY_REJECTED,
        # GitHub #1750 R2: hardcoded, NOT evidence-derived. This park means the
        # reviewer emitted MUST_FIX findings that failed mechanical validation
        # — a reviewer malfunction the ticket cannot be blamed for. The payload
        # here carries no commits or countable findings, so evidence-deriving
        # it would charge the ticket for someone else's malfunction.
        unproductive=False,
    )


def _route_stage_failure(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    status: str,
    last_result: dict[str, object] | None,
    blocker: object | None,
    blocker_reason: str | None,
    disposition: str | None,
    claim_unproductive: bool,
) -> None:
    """Rule 5 body: blocked/merge_gate_blocked/scope_exceeded/forbidden_area.

    Extracted from ``_route_staged_decision``'s STAGE_FAILURE_STATUSES branch
    (#1948) to keep that function under CLAUDE.md's PLR0912 branch ceiling --
    mirrors the existing ``_route_stage_success``/``_route_scope_gated_approval``
    per-rule extraction shape, not a new pattern. Every early ``return`` below
    is a same-function early exit (skips the rest of *this* body only); the
    caller's own ``elif status in STAGE_FAILURE_STATUSES: _route_stage_failure(...)``
    unconditionally falls through to ``_route_staged_decision``'s single
    ``return True`` regardless of which branch here fires, so no return value
    is needed here.

    Sub-rule 5a: "blocked" at FINALIZE with a regress-eligible blocker reason
    and attempts below cap -> regress to IMPL for self-heal (#770).
    scope_exceeded/forbidden_area have no blocker field (validator enforces
    this) so they always fall through to BLOCKED_ON_USER. merge_gate_blocked
    MAY carry one (schema.py's #777 exception), but 5a is gated on
    status=="blocked" so it still cannot regress. blocker/blocker_reason are
    read once by the caller, before the rule table (#1254). Also excludes an
    earlier-stage report (GitHub #1676 follow-up, _is_earlier_stage_report):
    "agent_block" reported at, say, stage1_plan never actually failed at
    FINALIZE, so self-healing off it would mask the sentinel's real, earlier
    failure behind a fresh, doomed-to-repeat IMPL dispatch. 5a measures
    ``merge_in_progress`` in the ticket's worktree before regressing and hands
    the result to ``_stage_regress`` (#2421), since ``blocker_reason`` cannot
    tell a merge-conflict cause from any other ``agent_block``.
    """
    if status == "blocked" and blocker_reason == CODEX_MUST_FIX_MECHANICALLY_REJECTED:
        # #1714: dedicated override -- see
        # _park_must_fix_mechanically_rejected's docstring for why this
        # reason cannot use the generic _hold_aware_disposition stamp
        # computed above. The reason is never a member of
        # FINALIZE_REGRESS_BLOCKER_REASONS ({"agent_block"}), so there is
        # no ordering conflict with 5a below, but the branch is placed
        # first and returns immediately so that fact does not have to hold
        # forever.
        _park_must_fix_mechanically_rejected(task)
        return
    if (
        status == "blocked"
        and task.stage == Stage.FINALIZE
        and blocker_reason in FINALIZE_REGRESS_BLOCKER_REASONS
        and task.regress_attempts < FINALIZE_REGRESS_CAP
        and not _is_earlier_stage_report(task, last_result, clients)
    ):
        _log.info(
            "dispatch: finalize gate blocked (%r) — regressing %r to IMPL"
            " (regress attempt %d/%d)",
            blocker_reason,
            task.ticket_id,
            task.regress_attempts + 1,
            FINALIZE_REGRESS_CAP,
        )
        # #2421: measured here, not derived from blocker_reason --
        # "agent_block" covers merge and non-merge causes alike.
        worktree_path = resolve_task_worktree(task, clients.get(task.client))
        _stage_regress(
            task,
            Stage.IMPL,
            merge_conflict_detected=merge_in_progress(worktree_path) is True,
        )
        record_event(
            OrchestratorEventType.TICKET_REQUEUED,
            {
                "ticket_id": task.ticket_id,
                "client": task.client,
                "from_stage": Stage.FINALIZE,
                "to_stage": Stage.IMPL,
                "reason": "finalize_regress",
                "blocker_reason": blocker_reason,
                "regress_attempt": task.regress_attempts,
            },
        )
        return
    if status == "blocked" and blocker_reason == FIX_LOOP_PENDING_DISPATCH:
        # #2017: the REVIEW session recorded its fix-loop action list on the
        # queue row and exited so cw.reconcile.fix_dispatch can issue the spawn
        # from outside the worktree. Routine per-cycle flow, not a terminal
        # outcome -- so no SESSION_NEEDS_ATTENTION (nothing for an operator to
        # act on) and no status transition at all. Leaving task.status at
        # RUNNING is load-bearing, not merely tidy: the generic fallthrough
        # would park it BLOCKED_ON_USER, stranding a ticket the fix loop is
        # mid-way through, and RUNNING is precisely the state claim.py's
        # PENDING-only reclaim ignores, which is what stops a second REVIEW
        # session being dispatched before the fix agent has even spawned.
        return
    if status == "blocked" and blocker_reason == FAMILY_PROVIDER_OVERLOAD:
        # #1948: worker-declared provider overload (API 529) is transient and
        # provider-side; same-stage retry, NOT a _stage_regress -- task.stage
        # is untouched (no pipeline boundary crossed), so none of
        # _stage_regress's re-entry machinery (regress_attempts,
        # regressed_into_stage, pending_operator_comment,
        # finalize_regress_branch_head) applies. Narrow reason-keyed --
        # blocker.retry_eligible is never read (operator's #1923-round-3
        # resolution, carried forward: a generic read would silently start
        # auto-reverting local_main_diverged_from_origin/operator_unavailable,
        # which OPERATOR_UNAVAILABLE_BLOCKER_REASONS deliberately excludes).
        # Bound: the global attempt ceiling (unproductive_attempts /
        # claim.py's resolve_attempt_ceiling), deliberately NOT
        # regress_attempts/FINALIZE_REGRESS_CAP -- that cap exists because
        # _stage_regress already double-bounds against it (lifecycle.py:757);
        # this branch performs no stage regress, so nothing else bounds it.
        # unproductive=True hardcoded (not claim_unproductive): mirrors Rule
        # 4/5's no_op/stale_dispatch hardcodes -- a provider outage killing
        # the RUNNING exit is unproductive by definition regardless of
        # whatever partial evidence landed before the API died.
        prior_session_id = task.session_id
        transition_task_status(task, QueueItemStatus.PENDING, unproductive=True)
        task.session_id = None
        task.stage_base_ref = None
        record_event(
            OrchestratorEventType.TICKET_REQUEUED,
            {
                "ticket_id": task.ticket_id,
                "client": task.client,
                "from_stage": task.stage,
                "to_stage": task.stage,
                "reason": "provider_overload_retry",
                "blocker_reason": blocker_reason,
                "session_id": prior_session_id,
                "unproductive_attempts": task.unproductive_attempts,
            },
        )
        return
    # #2717: a gate-block park appends `occurrence=N next_eligible_at=<iso>`
    # (and stamps the backoff); a bare note when there is no blocker reason.
    base = _breadcrumb_for_reason(blocker_reason, blocker)
    note = _gate_block_breadcrumb_note(task, status, blocker_reason, blocker)
    breadcrumbs = " ".join(part for part in (base, note) if part)
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": (
                _AWAITING_OPERATOR_REASON
                if blocker_reason in OPERATOR_UNAVAILABLE_BLOCKER_REASONS
                else status
            ),
            "breadcrumbs": breadcrumbs,
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )
    # GitHub #1713 Variant A: automerge_not_armed parks a real, already-
    # created PR, but the schema forbids `pr` from being non-null on a
    # `blocked` status -- the producer carries the PR's identity in the
    # unmodeled `pr_info` object instead (see
    # _extract_pr_url_or_info's docstring). Without this, task.pr_url
    # never gets stamped and cw.pr_hydrate._is_candidate permanently
    # skips the row -- it is never polled, so it can never observe its
    # own PR merging.
    gate_pr_url = (
        _extract_pr_url_or_info(last_result)
        if blocker_reason == _AUTOMERGE_NOT_ARMED_REASON
        else None
    )
    # GitHub #1862: a stale_dispatch report legitimately carries zero
    # commits and zero findings -- the session correctly refused to
    # duplicate work already sitting in an open PR. Under the generic
    # evidence-based computation that reads as unproductive, so repeated
    # honest refusals would charge the ceiling and eventually re-park the
    # row at attempt_cap_blocked, burying the specific signal this status
    # exists to surface. Hardcoded False for the same reason Rule 4
    # hardcodes it for no_op: a correct, evidence-producing terminal
    # outcome, not a crashloop.
    #
    # #2280: a codex_review_unparseable park is a harness-side parse
    # failure (timeout, missing output, invalid JSON, schema mismatch --
    # every ExecutorFailureCategory that reason aggregates), not evidence
    # the ticket produced no progress -- same rationale as
    # _park_must_fix_mechanically_rejected's unproductive=False above, for
    # a reason that falls through to this generic branch instead of that
    # dedicated one.
    rule5_unproductive = (
        False
        if status == "stale_dispatch" or blocker_reason == CODEX_REVIEW_UNPARSEABLE
        else claim_unproductive
    )
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=disposition,
        pr_url=gate_pr_url,
        blocked_reason=blocker_reason,
        unproductive=rule5_unproductive,  # #1750 Rule 5
    )
    # GitHub #1713 Variant B: prior_pipeline_pr_open blocks this ticket
    # behind a DIFFERENT ticket's open PR -- this row has no PR of its
    # own yet, so pr_url/pr_hydrate can't help. Stamp the blocking PR's
    # bare number (regex-extracted from blocker.details, the only place
    # the producer emits it -- see routing/pr_refs.py's
    # _extract_blocked_on_pr) directly on task AFTER transition_task_status
    # returns: that call's unconditional latch-clear (mirrors
    # escalation_parked_at) zeroes blocked_on_pr as part of every
    # transition, so stamping before the call would be immediately wiped.
    #
    # GitHub #1902 (fast-follow to #1862): a stale_dispatch sentinel's
    # pr_already_open blocker.details names the blocking PR in the
    # identical "PR #<N>" free-text shape, so the same
    # _extract_blocked_on_pr regex applies unchanged -- second producer,
    # not a new parser. NOTE: unlike prior_pipeline_pr_open (whose
    # blocking PR belongs to a DIFFERENT ticket that already completed
    # and so carries its pr_url on some other store row),
    # stale_dispatch's blocking PR is this ticket's OWN earlier,
    # un-harvested-sentinel dispatch, discovered via a live
    # `gh pr list --head <branch>` query that never writes a pr_url onto
    # any TicketTask row. GitHub #1927 gives that number an independent
    # state source instead of a row to match against: a per-tick rescan
    # (cw.reconcile.stale_dispatch_watch) registers it as a client-tagged
    # WatchedPr, which hydrates on the same serve tick and feeds
    # release_stale_gated_tasks's Variant B cross-reference scan. The
    # rescan is retroactive, so a row stamped here needs no cooperation
    # from this call site beyond the stamp itself.
    if blocker_reason in (
        _PRIOR_PIPELINE_PR_OPEN_REASON,
        STALE_DISPATCH_BLOCKER_REASON,
    ):
        details = blocker.get("details") if isinstance(blocker, dict) else None
        task.blocked_on_pr = _extract_blocked_on_pr(details)
