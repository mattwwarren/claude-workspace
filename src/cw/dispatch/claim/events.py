"""Dispatch-claim event emitters.

Per-task ``dispatch.tick`` and ``SESSION_NEEDS_ATTENTION`` payload builders
shared by the claim screen (:mod:`cw.dispatch.claim.screening`) and the spawn
path's occupancy deferrals (:mod:`cw.dispatch.claim.spawn`). Extracted verbatim
from the historical flat ``cw.dispatch.claim`` module by the package split
(#2378).
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from cw._text import redact
from cw.dev_queue import (
    REVIEW_APPROVAL_GATE_DISPOSITION,
    STALE_DISPATCH_GATE_DISPOSITION,
)
from cw.dev_queue.dispositions import (
    REVIEW_CAPABILITY_RELANE_STEPS,
    REVIEW_CAPABILITY_UNMET_DISPOSITION,
)
from cw.dev_queue.next_commands import dev_queue_command
from cw.events import record_event
from cw.models import DispatchSkipReason, OrchestratorEventType
from cw.models.bindings import Role

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.dispatch.claim.outcome import _SpawnOutcome
    from cw.dispatch.claim.preflight_reach import BindingHit
    from cw.dispatch.claim.review_capability import CapabilityGap
    from cw.models import TicketTask

# Hard cap on the spawn-failure text carried on a dispatch.tick (#1679). Same
# 500-char convention as spawn.py's _PRIOR_ATTEMPT_DETAILS_MAX_LEN.
_SPAWN_ERROR_TEXT_MAX_CHARS = 500


def _sanitize_spawn_error_text(error: str) -> str:
    """Collapse *error* to one line, redact it, then cap it (#1679, #2696).

    Collapsed first (same idiom as ``lane.paused``'s ``last_error``, #2034),
    redacted, and only then truncated to :data:`_SPAWN_ERROR_TEXT_MAX_CHARS`
    plus a trailing ``…`` -- redact first so a secret straddling the cap cannot
    be cut into an unmatchable fragment. Shared by the ``dispatch.tick``
    ``last_error`` and the lane override's persisted ``recent_spawn_errors``.
    """
    text = redact(" ".join(error.split()))
    if len(text) > _SPAWN_ERROR_TEXT_MAX_CHARS:
        text = text[:_SPAWN_ERROR_TEXT_MAX_CHARS] + "…"
    return text


def _spawn_error_tick_fields(outcome: _SpawnOutcome | None) -> dict[str, str]:
    """Return the ``dispatch.tick`` payload fragment for a spawn failure (#1679).

    *outcome* is the ``spawn_error=True`` :class:`_SpawnOutcome` seen during the
    client's lane walk, or ``None`` when no spawn failure occurred -- in which
    case the fragment is empty and the ``last_error`` key stays absent.

    The text goes through :func:`_sanitize_spawn_error_text`. An empty
    exception message yields ``""``, never a missing key.
    """
    if outcome is None:
        return {}
    return {"last_error": _sanitize_spawn_error_text(outcome.error)}


def _emit_attempt_cap_blocked_event(
    client_name: str, ticket_id: str, ceiling: int
) -> None:
    """Emit a dispatch.tick event when a task is parked at the attempt ceiling.

    Per-task event (not per-client-per-tick) for operator observability: a quiet
    loop after several failures should be distinguishable from a healthy idle loop.
    See GitHub #786.

    ``attempt_ceiling`` carries the *resolved* number that actually fired
    (#1751). Since the ceiling became lane-scoped, an operator reading this
    event can no longer infer it from ``global_attempt_ceiling`` — the lane may
    have overridden it. Additive: this payload is a plain untyped dict, so no
    consumer schema migration is involved.
    """
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client_name,
            "claimed": 0,
            "skip_reason": DispatchSkipReason.ATTEMPT_CAP_BLOCKED,
            "ticket_id": ticket_id,
            "attempt_ceiling": ceiling,
        },
    )


def _record_pre_dispatch_attention_event(
    task: TicketTask,
    client_name: str,
    lane: str,
    *,
    paused_status: str,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Record a pre-dispatch ``SESSION_NEEDS_ATTENTION`` park (single owner).

    Owns the nine-field shape every claim-time park shares. No session ran for
    these parks, so ``claude_session_id`` is ``None`` and ``breadcrumbs`` is
    empty. ``session_id`` is the task's pinned session or ``""``.
    ``paused_status`` passes through unconverted: callers hand in the
    gate-suffixed disposition, never the ``Status``-derived value (#1729).

    *extra* is additive and appended after the common fields in its own
    insertion order. It may not shadow a common key: a clash raises
    ``ValueError`` before anything is recorded.
    """
    payload: dict[str, object] = {
        "session_id": task.session_id or "",
        "session_name": "",
        "client": client_name,
        "ticket_id": task.ticket_id,
        "claude_session_id": None,
        "paused_status": paused_status,
        "breadcrumbs": "",
        "crashed": False,
        "lane": lane,
    }
    if extra:
        clash = sorted(payload.keys() & extra.keys())
        if clash:
            msg = f"extra fields shadow common attention fields: {clash}"
            raise ValueError(msg)
        payload.update(extra)
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        payload,
        correlation_id=task.ticket_id,
    )


def _emit_attempt_cap_attention_event(
    task: TicketTask, client_name: str, lane: str, ceiling: int
) -> None:
    """Emit SESSION_NEEDS_ATTENTION when a task is parked at the attempt ceiling.

    Sibling of :func:`_emit_attempt_cap_blocked_event` (#1257) -- that helper
    emits a DISPATCH_TICK event (operator-visible tick summary); this one
    emits the SESSION_NEEDS_ATTENTION event the attention-monitor/board
    surfaces consume. No session/breadcrumb detail exists at this pre-spawn
    ceiling check (the task never spawned a session this attempt).

    ``attempt_ceiling`` is the resolved lane-or-global number that fired, for
    the same reason its DISPATCH_TICK sibling carries it (#1751). The canonical
    9-field shape already varies per caller elsewhere (see ``renotify_marker``
    in reconcile/liveness.py), so one extra field here is additive.
    """
    _record_pre_dispatch_attention_event(
        task,
        client_name,
        lane,
        paused_status=DispatchSkipReason.ATTEMPT_CAP_BLOCKED,
        extra={"attempt_ceiling": ceiling},
    )


def _emit_stale_dispatch_blocked_event(client_name: str, ticket_id: str) -> None:
    """Emit a dispatch.tick event when the pre-dispatch open-PR gate parks a task.

    Sibling of :func:`_emit_attempt_cap_blocked_event` (#1862): per-task, not
    per-client-per-tick, so an operator can tell a loop that is quietly holding
    a stale row apart from a healthy idle loop.
    """
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client_name,
            "claimed": 0,
            "skip_reason": DispatchSkipReason.STALE_PR_BLOCKED,
            "ticket_id": ticket_id,
        },
    )


def _emit_worktree_occupied_skip_event(client_name: str, ticket_id: str) -> None:
    """Emit a dispatch.tick event when a ticket's worktree/hook-context is
    held by a live session or daemon worker (#2077).

    Sibling of _emit_stale_dispatch_blocked_event: per-task, outside the
    precedence chain, no SESSION_NEEDS_ATTENTION sibling -- a genuinely
    working session resolves itself; a routed-but-never-completed orphan
    (#2517) does not and is closed by the explicit ``cw dev-queue
    approve`` / ``requeue`` or ``cw spawn close --confirmed-dead`` command
    (the orchestrator session counts, ADR-0020). Shared by the
    pre-claim occupancy screen, the genuinely-live HookContextConflictError
    release, and the pre-existing WorktreeOccupiedError release.
    """
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client_name,
            "claimed": 0,
            "skip_reason": DispatchSkipReason.WORKTREE_OCCUPIED,
            "ticket_id": ticket_id,
        },
    )


def _emit_ticket_closed_cancel_event(
    client_name: str, ticket_id: str, cancel_reason: str
) -> None:
    """Emit a dispatch.tick event when the closed-ticket guard cancels a row (#1605).

    Same per-task shape as :func:`_emit_worktree_occupied_skip_event`, outside
    the precedence chain. The row loses ``disposition`` on CANCELLED, so the
    reason rides here as ``cancel_reason``. A cap deferral emits nothing.
    """
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client_name,
            "claimed": 0,
            "skip_reason": DispatchSkipReason.TICKET_CLOSED,
            "ticket_id": ticket_id,
            "cancel_reason": cancel_reason,
        },
    )


def _emit_stale_dispatch_attention_event(
    task: TicketTask, client_name: str, lane: str
) -> None:
    """Emit SESSION_NEEDS_ATTENTION for a pre-dispatch open-PR gate park (#1862).

    ``paused_status`` is the *gate-suffixed*
    :data:`~cw.dev_queue.STALE_DISPATCH_GATE_DISPOSITION`, never the
    ``Status``-derived ``"stale_dispatch"``: the literal must stay outside
    ``BREADCRUMB_ELIGIBLE_PAUSED_STATUSES`` (#1729 convention).
    """
    _record_pre_dispatch_attention_event(
        task,
        client_name,
        lane,
        paused_status=STALE_DISPATCH_GATE_DISPOSITION,
    )


def _emit_binding_blocked_event(client_name: str, ticket_id: str) -> None:
    """Emit a dispatch.tick event when a binding reachability park fires (#2442).

    Sibling of :func:`_emit_stale_dispatch_blocked_event`: per-task, outside
    the per-client-tick precedence chain. ``skip_reason`` is
    ``binding_blocked`` (``tracker_mcp_gate_blocked`` before #3744).
    """
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client_name,
            "claimed": 0,
            "skip_reason": DispatchSkipReason.BINDING_BLOCKED,
            "ticket_id": ticket_id,
        },
    )


def _binding_attention_extra(hit: BindingHit) -> dict[str, str]:
    """The park's additive SESSION_NEEDS_ATTENTION keys, in payload order.

    A plugin hit (claude-native) keeps ``expected_plugin`` and its pre-#3055
    ``details`` string byte for byte, plus ``backend``. A server hit (RFC 0018
    S3) names the MCP server as ``expected_server`` instead. A tool-missing
    hit appends ``; missing_tools=<a,b>`` to ``details``. ``need`` (the role,
    #3744) is appended last.
    """
    if hit.expected_kind == "plugin":
        extra = {
            "branch": hit.branch,
            "file_inspected": hit.file_inspected,
            "expected_plugin": hit.expected,
            "backend": hit.backend,
            "details": (
                f"branch={hit.branch}; file_inspected={hit.file_inspected};"
                f" expected_plugin={hit.expected}"
            ),
        }
    else:
        extra = {
            "branch": hit.branch,
            "file_inspected": hit.file_inspected,
            "backend": hit.backend,
            "expected_server": hit.expected,
            "details": (
                f"branch={hit.branch}; file_inspected={hit.file_inspected};"
                f" backend={hit.backend}; expected_server={hit.expected}"
            ),
        }
    if hit.missing_tools:
        extra["details"] += f"; missing_tools={','.join(hit.missing_tools)}"
    extra["need"] = Role.TICKETS.value
    return extra


def _emit_binding_attention_event(
    task: TicketTask, client_name: str, lane: str, hit: BindingHit
) -> None:
    """Emit SESSION_NEEDS_ATTENTION for a binding reachability park (#2442, #3744).

    ``paused_status`` is the hit's reason. ``branch``, ``file_inspected``, the
    expected plugin (claude-native) or MCP server and ``backend`` name exactly
    what the orchestrator must fix, ``details`` composes them, and ``need``
    names the role. This detail is transient and event-only by design --
    ``TicketTask.advisory_note`` is cleared by the very
    ``transition_task_status`` call that performs the park.
    """
    _record_pre_dispatch_attention_event(
        task,
        client_name,
        lane,
        paused_status=hit.reason.value,
        extra=_binding_attention_extra(hit),
    )


def _emit_review_approval_gate_attention_event(
    task: TicketTask,
    client_name: str,
    lane: str,
    *,
    branch: str,
    token: str,
    detail: str,
) -> None:
    """Emit SESSION_NEEDS_ATTENTION for a claim-time review-approval park (#2773).

    ``details`` names the Step 4.0 token and the one release,
    ``requeue --stage review`` (a plain finalize requeue re-parks).
    Event-only: ``advisory_note`` is cleared by the park itself.
    """
    release = dev_queue_command("requeue", task, "--stage", "review")
    _record_pre_dispatch_attention_event(
        task,
        client_name,
        lane,
        paused_status=REVIEW_APPROVAL_GATE_DISPOSITION,
        extra={
            "branch": branch,
            "approval_token": token,
            "details": (
                f"Step 4.0 would park this large-tier ticket ({token}: {detail});"
                " the claim was refused before any worker started. Fix any"
                " environment fault named in the detail first, then release with"
                f" `{release}` to obtain a fresh review approval. A plain finalize"
                " requeue re-parks."
            ),
        },
    )


def _emit_review_capability_attention_event(
    task: TicketTask,
    client_name: str,
    lane: str,
    gap: CapabilityGap,
) -> None:
    """Emit SESSION_NEEDS_ATTENTION for a claim-time review-capability park (#3790).

    ``details`` names the missing capabilities, the lane, the compatible lanes
    and the config fix. The release is ``requeue``; re-laning is the shared
    ``REVIEW_CAPABILITY_RELANE_STEPS`` (pause the incompatible lane, requeue,
    move, resume: move needs PENDING and a tick would re-park a requeued row).
    Nothing was dispatched and nothing was attested.
    """
    requeue = dev_queue_command("requeue", task)
    target = gap.compatible_lanes[0] if gap.compatible_lanes else "<lane>"
    move = dev_queue_command("move", task, "--to", target)
    lane_cmd = f"-c {shlex.quote(task.client)} {shlex.quote(task.lane)}"
    relane = REVIEW_CAPABILITY_RELANE_STEPS.format(
        pause=f"cw lane pause {lane_cmd}",
        requeue=requeue,
        move=move,
        resume=f"cw lane resume {lane_cmd}",
    )
    compatible = ", ".join(gap.compatible_lanes) or "none"
    _record_pre_dispatch_attention_event(
        task,
        client_name,
        lane,
        paused_status=REVIEW_CAPABILITY_UNMET_DISPOSITION,
        extra={
            "backend": gap.backend,
            "required_capabilities": list(gap.required),
            "declared_capabilities": list(gap.declared),
            "missing_capabilities": list(gap.missing),
            "compatible_lanes": list(gap.compatible_lanes),
            "details": (
                f"Lane '{gap.lane}' runs REVIEW on backend={gap.backend}, which"
                f" declares {list(gap.declared)}; the client's review.engine requires"
                f" {list(gap.required)} and lacks {list(gap.missing)}. The claim was"
                " refused before any worker started: no repair was dispatched and"
                " nothing was attested. Fix the config in clients.yaml: declare the"
                " missing capabilities on this lane's"
                " pipeline.executors.review.runtime_capabilities (a lane's"
                " executors replace the client's"
                " wholesale), or remove them from"
                " review.engine.requires_runtime_capabilities. Compatible lanes: "
                f"{compatible}. Release with `{requeue}`. {relane}"
            ),
        },
    )


def emit_adoption_attention_event(
    task: TicketTask,
    client_name: str,
    lane: str,
    *,
    paused_status: str,
    branch: str,
    observed_head_sha: str | None,
    commit_range: str,
    details: str,
) -> None:
    """Emit SESSION_NEEDS_ATTENTION for an RFC 0016 adoption park (#2943).

    Shared by the claim-time ``adopted_head_drift`` park and the intake park
    ``adopt_pr_state_unknown`` (a non-green PR enters at impl with repair
    feedback instead of parking since #2944). ``adopted_head_sha`` is the row's
    pin, ``observed_head_sha`` the head the park saw, ``commit_range`` the
    capped ``git log --oneline pin..observed`` (empty for an intake park),
    ``branch`` the resolved branch, and ``details`` the human-readable cause.
    Event-only, like the tracker-MCP detail: ``transition_task_status`` clears
    ``advisory_note`` on the park.
    """
    _record_pre_dispatch_attention_event(
        task,
        client_name,
        lane,
        paused_status=paused_status,
        extra={
            "branch": branch,
            "pr_url": task.adopted_pr_url,
            "adopted_head_sha": task.adopted_head_sha,
            "observed_head_sha": observed_head_sha,
            "commit_range": commit_range,
            "details": details,
        },
    )
