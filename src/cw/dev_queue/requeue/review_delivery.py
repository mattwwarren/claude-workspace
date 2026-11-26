"""Review re-entry deliverability for a requeue (#3739)."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.models import Stage

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask


class _ReviewDeliverability(NamedTuple):
    """Verdict from :func:`_review_reentry_deliverable` (GitHub #1730).

    A named 4-tuple rather than a bare one so a future reorder of fields is a
    mypy-catchable error at every call/unpack site instead of a silent bug.
    """

    deliverable: bool
    reason: str
    backend: str
    tracker: str | None


def _review_reentry_deliverable(
    task: TicketTask, client_cfg: ClientConfig
) -> _ReviewDeliverability:
    """True iff a REVIEW-stage re-entry can deliver operator tracker context.

    Returns a :class:`_ReviewDeliverability`. ``backend`` and ``tracker`` are
    always populated (even when ``deliverable`` is True, or when ``tracker``
    was never consulted because ``backend`` alone already settled the
    verdict) so the caller can fold them into the
    ``REQUEUE_REVIEW_DELIVERY_DEGRADED`` payload for machine consumption
    (GitHub #1730) without a second, possibly-drifting resolution call.
    ``tracker`` is ``None`` when unresolvable (no ``tracking.primary.system``
    in ``.claude/project-config.yaml`` -- see ``cw.tracker.resolve_tracker``'s
    own contract) or not meaningful for the resolved backend (``claude-native``
    never consults it). ``resolve_tracker`` is called unconditionally rather
    than only on the codex branch: it is a local, read-only config-file read
    with no network I/O, so a consistent payload key set is worth more than
    skipping it.

    Deferred import of ``resolve_executor_config`` to break the ``dev_queue ->
    executor -> codex_background -> dev_queue`` cycle (same precedent as
    ``unblock_ticket``'s deferred ``cw.config`` import, see module docstring).
    NOTE for test authors: because these imports are function-local, tests must
    monkeypatch ``cw.executor.resolve_executor_config`` /
    ``cw.tracker.resolve_tracker`` at their origin modules, not this module --
    there is no module-level attribute of either name here to patch.

    Unlike an #1681-style guard, this NEVER blocks the transition (see
    ``requeue/stage_gate.py``'s "review and finalize degrade and are deliberately not
    guarded" comment in ``_apply_requeue_stage``, and #1730/#1717 comment 6):
    the caller emits a loud event and proceeds regardless.
    """
    from cw.executor import resolve_executor_config
    from cw.models.orchestrator_config import CLAUDE_NATIVE_BACKEND, CODEX_BACKEND
    from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

    backend = resolve_executor_config(Stage.REVIEW, task, client_cfg).backend
    tracker = resolve_tracker(client_cfg.workspace_path)
    if backend == CLAUDE_NATIVE_BACKEND:
        return _ReviewDeliverability(True, "", backend, tracker)
    if backend == CODEX_BACKEND:
        if tracker == TRACKER_GITHUB_ISSUES:
            return _ReviewDeliverability(True, "", backend, tracker)
        return _ReviewDeliverability(
            False,
            (
                f"REVIEW-stage backend 'codex' for client {client_cfg.name!r} can only"
                " deliver operator tracker comments on a github-issues tracker"
            ),
            backend,
            tracker,
        )
    return _ReviewDeliverability(
        False,
        f"REVIEW-stage backend {backend!r} has no operator-comment delivery path",
        backend,
        tracker,
    )
