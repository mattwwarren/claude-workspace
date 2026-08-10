"""Daemon-side gate recipes: mechanical gate-clearing reactor (RFC 0009).

Gate recipes are the opt-in automation layer that clears an approval gate a
human would otherwise have to clear by hand, but only when a fixed, verifiable
predicate holds. Unlike the concierge reactor (``cw.reconcile.concierge``),
which *recovers* rows stuck behind dead sessions, gate recipes *advance* a live
approval gate — so they gate on their own opt-in master switch
(``OrchestratorConfig.gate_recipes_enabled``, default False) and forward their
audit event to the operator channel, since an auto-advance with no human in the
loop is attention-worthy.

**P1+P2 scope (GitHub #1065):** the ``auto_approve_clean_review`` recipe. It
auto-approves a ``review_pending_approval`` gate when the review came back
completely clean — no MUST_FIX findings, nothing deferred, health
recommendation PROCEED, no forbidden-area touch, and at least one reviewer
agent actually ran (``agents_run > 0``).

**P3 scope (GitHub #1066):** the ``auto_adopt_clean_plan`` recipe. It
auto-adopts a ``plan_pending_approval`` gate when the plan-of-record carries
both signoff markers (``plan-spec-reviewed`` and ``plan-soundness-reviewed``)
appended by ``auto-dev-plan``. The per-lane ``resolve_gate_recipe_enabled``
precedence (P4, #1067) remains out of scope here.

The recipe follows the repo's detect/act split (see ``concierge.py`` for the
closest sibling): a pure ``_detect_auto_approve_review`` classification phase,
then ``_act_auto_approve_review`` which re-validates the predicate under
``dev_queue_lock()`` and mutates. Emit-before-act is a hard requirement:
:class:`OrchestratorEventType.GATE_AUTO_APPROVED` is recorded (durably, to the
append-only events inbox) *before* the approval mutation, so evidence of what
the recipe decided survives even if the subsequent write fails (mirrors the
concierge ``CONCIERGE_RECOVERED`` ordering). If the mutation itself then
raises, :class:`OrchestratorEventType.GATE_AUTO_APPROVE_FAILED` is emitted as
a durable, operator-forwarded correction — without it, ``GATE_AUTO_APPROVED``
would stand alone on the operator channel as an uncorrected false-positive
"approved" signal — and ``TicketTask.gate_recipe_failed_at`` is stamped as a
one-shot latch so the same still-failing episode doesn't re-detect and
re-emit both events every reconcile tick forever. The latch clears itself
the same way the RFC 0008 escalation latch does: unconditionally, on the
next status transition (``dev_queue.transition_task_status``).

The act phase calls the lock-free ``_approve_ticket_locked`` primitive directly
from inside its own ``dev_queue_lock()`` acquisition — never the public
``approve_ticket`` wrapper, which would self-deadlock by re-acquiring the same
flock-based lock (see #1065 and ``dev_queue._approve_ticket_locked``).

**Invariant (GitHub #1199):** cw never grants a GitHub pull-request review
approval — no ``gh pr review --approve``, no GraphQL mutation that adds a
pull-request review with an approving event, and no REST reviews-endpoint
call with an approving event exists anywhere in ``src/``, and this
module's own ``auto_approve_clean_review`` recipe does not touch GitHub
review state at all — it advances only cw's internal dev-queue gate via
``_approve_ticket_locked``. See ADR-0012 and
``tests/test_review_approval_guard.py`` for the exact call shapes this
invariant covers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.config import load_effective_clients, load_state
from cw.dev_queue import (
    _PLAN_SOUNDNESS_MARKER,
    _PLAN_SPEC_MARKER,
    _approve_ticket_locked,
    _marker_version,
    _newest_by_created_at,
    _plan_body_signoff_ok,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.gh import fetch_approved_plan_comment, post_issue_comment
from cw.models import OrchestratorEventType, QueueItemStatus
from cw.reconcile.tasks import _client_cwd, _is_dangling_client

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path
    from typing import Protocol

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        OrchestratorConfig,
        Session,
        TicketTask,
    )

    class _CommentPostFn(Protocol):
        """Shape shared by ``_post_auto_approve_comment``/``_post_auto_adopt_comment``.

        A plain ``Callable[[str, dict[str, object]], None]`` can't express the
        keyword-only ``cwd`` parameter both functions share, so mypy --strict
        wouldn't catch a signature drift at the ``_flush_gate_recipe_comment_jobs``
        call site (GitHub #1570).
        """

        def __call__(
            self,
            ticket_id: str,
            snapshot: dict[str, object],
            *,
            cwd: Path | None = None,
        ) -> None: ...


_log = logging.getLogger(__name__)

# Recipe name constants — the recognised gate-recipe keys. Only the review
# recipe is wired in P1+P2 (#1065); RECIPE_AUTO_ADOPT_PLAN is defined now so
# both keys have one home, but its detect/act land in P3 (#1066).
# NOTE (#1199): "auto_approve" here means cw's internal dispatch gate only —
# never a GitHub PR review approval. See the module docstring and ADR-0012.
RECIPE_AUTO_APPROVE_REVIEW = "auto_approve_clean_review"
RECIPE_AUTO_ADOPT_PLAN = "auto_adopt_clean_plan"

# RFC 0009 P4 (#1067) — tier-3 hardcoded fallback for the per-lane resolver.
# Both recipes default OFF (inverted from concierge's all-True default): a gate
# recipe auto-clears an approval gate with no human in the loop, so nothing
# fires unless an operator opts a lane (or ticket) in. NOT a config field — it
# is the floor the ticket/lane tiers fall through to.
_DEFAULT_GATE_RECIPE_ENABLED: dict[str, bool] = {
    RECIPE_AUTO_APPROVE_REVIEW: False,
    RECIPE_AUTO_ADOPT_PLAN: False,
}


def resolve_gate_recipe_enabled(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    recipe_name: str,
) -> bool:
    """Return whether *recipe_name* is enabled for *task*, per RFC 0009 P4.

    3-tier precedence, highest first (mirrors resolve_signoff's shape and
    resolve_concierge_recipe_enabled's per-recipe .get fallback):

    1. ``task.gate_recipes`` — ticket-level override wins when it names the
       recipe.
    2. ``LaneConfig.gate_recipes`` on the task's lane — the per-lane map.
    3. ``_DEFAULT_GATE_RECIPE_ENABLED`` — the hardcoded default-off floor.

    Robust to a missing client (absent from *clients*) or a missing lane
    (absent from the client's ``effective_lanes``): either falls straight
    through to the default with no exception.
    """
    if task.gate_recipes is not None and recipe_name in task.gate_recipes:
        return task.gate_recipes[recipe_name]
    client_cfg = clients.get(task.client)
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if (
                lane_cfg.name == task.lane
                and lane_cfg.gate_recipes is not None
                and recipe_name in lane_cfg.gate_recipes
            ):
                return lane_cfg.gate_recipes[recipe_name]
    # .get(..., False): a recipe_name outside _DEFAULT_GATE_RECIPE_ENABLED
    # (i.e. not one of the two RECIPE_* constants) falls through to the safe
    # default instead of raising KeyError, matching this function's documented
    # no-exception robustness guarantee for every other unresolved input.
    return _DEFAULT_GATE_RECIPE_ENABLED.get(recipe_name, False)


def _recipe_gate_open(
    config: OrchestratorConfig,
    task: TicketTask,
    clients: dict[str, ClientConfig],
    recipe_name: str,
) -> bool:
    """Return whether *recipe_name* may fire for *task* right now.

    Composes the master switch with the per-lane/per-ticket resolution so
    both ``_detect_*`` functions share one gating check instead of drifting
    copies. Why: redundant with ``run_gate_recipes``'s top-level short-circuit
    on ``config.gate_recipes_enabled`` — needed here too so a caller invoking
    ``_detect_*`` directly (unit tests) still gets correct gating.
    """
    return config.gate_recipes_enabled and resolve_gate_recipe_enabled(
        task, clients, recipe_name
    )


# The only sentinel status the review recipe fires on. A row whose owning
# session's last_result is not at this gate is never a candidate.
_REVIEW_PENDING_APPROVAL = "review_pending_approval"
# The single health recommendation the clean-review predicate accepts.
_RECOMMENDATION_PROCEED = "PROCEED"

_AUTO_APPROVE_COMMENT_TEMPLATE = """\
Auto-approved by gate recipe `{recipe}`.

The review met the clean-review predicate and was approved automatically
(no human review) by RFC 0009 gate-recipe automation:

- must_fix_initial: {must_fix_initial}
- deferred: {deferred}
- recommendation: {recommendation}
- forbidden_touched: {forbidden_touched}
- agents_run: {agents_run}

See event `GATE_AUTO_APPROVED` for the full audit trail.
"""

# The only sentinel status the plan recipe fires on. A row whose owning
# session's last_result is not at this gate is never a candidate.
_PLAN_PENDING_APPROVAL = "plan_pending_approval"

# The two signoff markers auto-dev-plan appends to the plan-of-record body.
# Canonical definition now lives in cw.dev_queue.lifecycle (#1567) — imported
# above rather than redefined here. _PLAN_SPEC_MARKER still mirrors
# gh._PLAN_MARKER, a genuinely separate definition in a different module;
# test_plan_spec_marker_matches_gh_marker continues to guard that drift.

# predicate_snapshot dict keys (R3) — named once so the producer
# (_clean_plan_snapshot) and consumer (_post_auto_adopt_comment) can't drift
# via a typo'd string literal at one site only.
_SNAPSHOT_KEY_SPEC = "plan_spec_reviewed"
_SNAPSHOT_KEY_SOUNDNESS = "plan_soundness_reviewed"

_AUTO_ADOPT_COMMENT_TEMPLATE = """\
Auto-approved by gate recipe `{recipe}`.

The plan met the clean-plan predicate (both signoff markers present on the
plan-of-record) and was approved automatically (no human review) by RFC 0009
gate-recipe automation:

- plan_spec_reviewed: {plan_spec_reviewed}
- plan_soundness_reviewed: {plan_soundness_reviewed}

See event `GATE_AUTO_APPROVED` for the full audit trail.
"""


@dataclass(frozen=True)
class GateRecipeCandidate:
    """Classification result from a gate recipe's detect phase.

    Same shape as :class:`cw.reconcile.concierge.ConciergeCandidate` plus a
    ``lane`` field: the ``GATE_AUTO_APPROVED`` event payload carries the row's
    lane, so the candidate captures it at detect time. ``evidence`` carries the
    ``predicate_snapshot`` — the exact five field values that licensed the fire
    (``must_fix_initial``, ``deferred``, ``recommendation``,
    ``forbidden_touched``, ``agents_run``), read off ``session.last_result``.

    Why ``evidence`` is unlike :class:`ConciergeCandidate`'s: the concierge act
    phase reads ``candidate.evidence`` straight into its event payload, but
    this module's act phase (:func:`_act_auto_approve_review`) deliberately
    re-derives a fresh snapshot instead of trusting this detect-time one — the
    predicate is re-checked under ``dev_queue_lock()`` to close the
    detect-to-act race (a concurrent human approve or new sentinel can
    invalidate it), so acting on a stale ``evidence`` value here would defeat
    that guard. The field is kept for detect-phase introspection/tests only.
    """

    ticket_id: str
    client: str
    lane: str
    recipe: str
    evidence: dict[str, object]
    session_id: str


def _clean_review_snapshot(last_result: object) -> dict[str, object] | None:
    """Extract the clean-review predicate snapshot, or None if not fireable.

    Returns None (fail-closed) unless *last_result* is a dict at the
    ``review_pending_approval`` gate whose ``review``/``health``/``scope``
    sections are all present dicts. The returned snapshot holds the five
    predicate field values verbatim; whether the predicate *holds* is a
    separate check (:func:`_predicate_holds`) so detect and act share both the
    extraction and the decision.
    """
    if not isinstance(last_result, dict):
        return None
    if last_result.get("status") != _REVIEW_PENDING_APPROVAL:
        return None
    review = last_result.get("review")
    health = last_result.get("health")
    scope = last_result.get("scope")
    if not (
        isinstance(review, dict)
        and isinstance(health, dict)
        and isinstance(scope, dict)
    ):
        return None
    return {
        "must_fix_initial": review.get("must_fix_initial"),
        # As of #1805, review.deferred on the Claude-native path is
        # apply_adjudication's real deferral count, not the placeholder it was
        # when every accepted finding carried an unearned disposition="fixed".
        # auto_approve_clean_review's `deferred == 0` check (_predicate_holds
        # below) therefore becomes semantically live once a lane enables the
        # recipe: it will correctly stop auto-approving a review with genuinely
        # deferred findings. That is the intended effect of making the field
        # accurate, not a regression to guard against.
        "deferred": review.get("deferred", 0),
        "recommendation": health.get("recommendation"),
        "forbidden_touched": scope.get("forbidden_touched"),
        "agents_run": review.get("agents_run", 0),
    }


def _predicate_holds(snapshot: dict[str, object]) -> bool:
    """True iff the five-field clean-review predicate is satisfied.

    Every field is compared against its clean value; a missing/None field
    (e.g. a malformed producer payload) fails the comparison and blocks the
    fire — the predicate is fail-closed. ``agents_run`` is guarded with an
    explicit ``isinstance`` check (rather than a bare ``> 0`` comparison)
    since *snapshot* is typed ``dict[str, object]`` — a malformed
    non-int producer value must fail closed, not raise or pass via truthy
    coercion. ``bool`` is excluded explicitly: it is a subclass of ``int``
    in Python, so a malformed ``agents_run: true`` payload would otherwise
    satisfy both ``isinstance(agents_run, int)`` and ``agents_run > 0``.
    """
    agents_run = snapshot["agents_run"]
    return (
        snapshot["must_fix_initial"] == 0
        # See _clean_review_snapshot's note: this comparison is unchanged by
        # #1805, but the value it reads became accurate on the Claude-native
        # path there.
        and snapshot["deferred"] == 0
        and snapshot["recommendation"] == _RECOMMENDATION_PROCEED
        and snapshot["forbidden_touched"] is False
        and isinstance(agents_run, int)
        and not isinstance(agents_run, bool)
        and agents_run > 0
    )


def _plan_of_record_body(task: TicketTask) -> str | None:
    """Return the plan-of-record body, tracker-first with a `.cw/plan.md` fallback.

    Why tracker-first (opposite of local_runner.build_task_message's
    .cw-first order): that function fills a local cache for Stage-2 task
    prompts; this gate checks *current* approval freshness, for which the
    tracker is the authoritative, freshest source. Falls back to the
    worktree's ``.cw/plan.md`` only when the tracker read returns None. A row
    with no materialized worktree has no fallback (returns None rather than
    raising on ``Path(None)``).
    """
    body = fetch_approved_plan_comment(task.ticket_id)
    if body is not None:
        return body
    if task.worktree_path is None:
        return None
    plan_path = task.worktree_path / ".cw" / "plan.md"
    if not plan_path.exists():
        return None
    try:
        return plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Read/decode failure between .exists() and read_text() (deleted,
        # permission error, non-UTF-8 content, etc.) degrades to "no plan
        # body" rather than propagating — an unhandled exception here would
        # abort the entire reconcile tick, including the unrelated
        # auto_approve_clean_review recipe processed in the same
        # run_gate_recipes() call. UnicodeDecodeError is not an OSError
        # subclass, so it must be caught explicitly alongside it.
        return None


def _clean_plan_snapshot(
    last_result: object, task: TicketTask
) -> dict[str, object] | None:
    """Extract the clean-plan predicate snapshot, or None if not fireable.

    Returns None (fail-closed) unless *last_result* is a dict at the
    ``plan_pending_approval`` gate AND the plan-of-record body carries BOTH
    signoff markers. Both markers are read from the SAME body (R2 — there is
    exactly one ``body`` variable in scope, so same-source is structural, not
    a separate check); a union across tracker + `.cw/plan.md` is impossible by
    construction. The returned snapshot holds only the two marker-version
    strings — the raw plan body is never placed in the snapshot, the event
    payload, or the audit comment.
    """
    if not isinstance(last_result, dict):
        return None
    if last_result.get("status") != _PLAN_PENDING_APPROVAL:
        return None
    body = _plan_of_record_body(task)
    if body is None:
        return None
    if not _plan_body_signoff_ok(body):
        return None
    spec_version = _marker_version(body, marker=_PLAN_SPEC_MARKER)
    soundness_version = _marker_version(body, marker=_PLAN_SOUNDNESS_MARKER)
    # Defense-in-depth (#1567): _plan_body_signoff_ok already proved both
    # calls below return non-None, since it composes the identical
    # _marker_version checks over the same body. This guard is kept anyway so
    # mypy sees the narrowed str type and so a future divergence between the
    # two predicates fails closed here rather than raising on a None key.
    if spec_version is None or soundness_version is None:
        return None
    return {
        _SNAPSHOT_KEY_SPEC: spec_version,
        _SNAPSHOT_KEY_SOUNDNESS: soundness_version,
    }


def _detect_auto_approve_review(
    state: CwState,
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[GateRecipeCandidate]:
    """Pure classification phase for auto_approve_clean_review. Zero writes.

    A candidate is produced for every BLOCKED_ON_USER row whose owning session
    (resolved via ``task.session_id``, the same lookup ``approve_ticket``
    performs) sits at the ``review_pending_approval`` gate with a clean-review
    ``last_result``. A row with a non-None ``gate_recipe_failed_at`` latch is
    excluded — a prior act-phase failure for this exact episode already fired
    a correcting ``GATE_AUTO_APPROVE_FAILED`` event, and re-detecting it every
    tick would re-emit both events forever for a condition that hasn't
    changed. The latch clears itself the moment anything about the episode
    does change (``transition_task_status`` unconditionally clears it on
    every status transition, including a same-status re-park by a fresh
    review session).
    """
    candidates: list[GateRecipeCandidate] = []
    for task in tasks:
        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            continue
        if task.gate_recipe_failed_at is not None:
            continue
        if task.session_id is None:
            continue
        session = state.find_by_name_or_id(task.session_id)
        if session is None:
            continue
        snapshot = _clean_review_snapshot(session.last_result)
        if snapshot is None or not _predicate_holds(snapshot):
            continue
        if not _recipe_gate_open(config, task, clients, RECIPE_AUTO_APPROVE_REVIEW):
            continue
        candidates.append(
            GateRecipeCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                lane=task.lane,
                recipe=RECIPE_AUTO_APPROVE_REVIEW,
                evidence=snapshot,
                session_id=task.session_id,
            )
        )
    return candidates


def _detect_auto_adopt_plan(
    state: CwState,
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[GateRecipeCandidate]:
    """Read-only classification phase for auto_adopt_clean_plan. Zero writes.

    Mirrors :func:`_detect_auto_approve_review`'s guard chain (BLOCKED_ON_USER,
    ``gate_recipe_failed_at`` latch None, resolvable session) but swaps the
    clean-review snapshot for a clean-plan snapshot: the row's owning session
    must sit at the ``plan_pending_approval`` gate and the plan-of-record must
    carry both signoff markers. The plan-of-record read (tracker-first) happens
    here at detect time — unlocked — never under the act-phase lock (R5). Not
    a pure function (it makes a ``gh`` subprocess call and may read a file),
    but it performs no writes/mutations.
    """
    candidates: list[GateRecipeCandidate] = []
    for task in tasks:
        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            continue
        if task.gate_recipe_failed_at is not None:
            continue
        if task.session_id is None:
            continue
        session = state.find_by_name_or_id(task.session_id)
        if session is None:
            continue
        snapshot = _clean_plan_snapshot(session.last_result, task)
        if snapshot is None:
            continue
        if not _recipe_gate_open(config, task, clients, RECIPE_AUTO_ADOPT_PLAN):
            continue
        candidates.append(
            GateRecipeCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                lane=task.lane,
                recipe=RECIPE_AUTO_ADOPT_PLAN,
                evidence=snapshot,
                session_id=task.session_id,
            )
        )
    return candidates


def _post_auto_approve_comment(
    ticket_id: str, snapshot: dict[str, object], *, cwd: Path | None = None
) -> None:
    """Post the auto-approve audit comment to the ticket (best-effort, logged).

    A distinct helper from ``codex_background._post_review_comment``: that one
    swallows failures with zero logging, whereas the ticket's OQ2 resolution
    requires a comment-write failure to be logged (the event remains the
    source-of-truth audit trail — a failed comment never undoes the approve).

    *cwd* scopes the gh call to the client's repo (GitHub #1269/#1279).
    """
    body = _AUTO_APPROVE_COMMENT_TEMPLATE.format(
        recipe=RECIPE_AUTO_APPROVE_REVIEW,
        must_fix_initial=snapshot["must_fix_initial"],
        deferred=snapshot["deferred"],
        recommendation=snapshot["recommendation"],
        forbidden_touched=snapshot["forbidden_touched"],
        agents_run=snapshot["agents_run"],
    )
    result = post_issue_comment(ticket_id, body, cwd=cwd)
    if result is None:
        _log.warning("gate_recipe_comment_failed ticket=%s: gh call failed", ticket_id)
        return
    if result.returncode != 0:
        _log.warning(
            "gate_recipe_comment_failed ticket=%s rc=%s: %s",
            ticket_id,
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )


def _post_auto_adopt_comment(
    ticket_id: str, snapshot: dict[str, object], *, cwd: Path | None = None
) -> None:
    """Post the auto-adopt audit comment to the ticket (best-effort, logged).

    Mirrors :func:`_post_auto_approve_comment` exactly (same ``gh issue
    comment`` subprocess call, same best-effort log-on-failure behavior),
    formatting the plan template with the two marker-version strings.

    *cwd* scopes the gh call to the client's repo (GitHub #1269/#1279).
    """
    # Explicit named args, not **snapshot: this writes to a public,
    # append-only GitHub comment, so the fields exposed there must stay
    # grep-able and reviewable at this call site. Unpacking the full
    # snapshot dict would auto-expose any future key added to
    # _clean_plan_snapshot with no code change here to acknowledge the new
    # public disclosure, and would raise TypeError if a future key ever
    # collided with the `recipe=` kwarg.
    body = _AUTO_ADOPT_COMMENT_TEMPLATE.format(
        recipe=RECIPE_AUTO_ADOPT_PLAN,
        plan_spec_reviewed=snapshot[_SNAPSHOT_KEY_SPEC],
        plan_soundness_reviewed=snapshot[_SNAPSHOT_KEY_SOUNDNESS],
    )
    result = post_issue_comment(ticket_id, body, cwd=cwd)
    if result is None:
        _log.warning("gate_recipe_comment_failed ticket=%s: gh call failed", ticket_id)
        return
    if result.returncode != 0:
        _log.warning(
            "gate_recipe_comment_failed ticket=%s rc=%s: %s",
            ticket_id,
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )


def _find_blocked_task(
    store: DevQueueStore, ticket_id: str, client: str
) -> TicketTask | None:
    """Resolve the (ticket_id, client) row this recipe acts on.

    Mirrors :func:`dev_queue._find_ticket`'s tie-break for the BLOCKED_ON_USER
    tier only (newest ``created_at`` wins) — this recipe never needs the
    PENDING/RUNNING/terminal tiers, since it exclusively operates on
    BLOCKED_ON_USER rows. A hand-rolled ``next()`` with no tie-break (the
    original shape of both call sites below) would, on the same duplicate-row
    condition ``_find_ticket`` itself guards against, risk resolving a
    *different* physical row than the one ``_approve_ticket_locked``
    (internally, via the real ``_find_ticket``) just acted on — silently
    latching or re-validating the wrong row. Returns ``None`` (rather than
    ``_find_ticket``'s raise) since every caller here treats a missing row as
    a silent skip, not an error.

    Deliberately NOT full parity with ``_find_ticket``: a duplicate row in a
    *live* status (PENDING/RUNNING) for the same key is out of scope here —
    ``_find_ticket``'s own live-tier precedence would resolve that case
    inside ``_approve_ticket_locked`` instead, which then rejects it with
    ``ApproveGateError`` (status not approvable). That failure is caught by
    this module's own ``except CwError`` and turned into a
    ``GATE_AUTO_APPROVE_FAILED`` correction + latch — fails safe, not silent.
    """
    matches = [
        t
        for t in store.tasks
        if t.ticket_id == ticket_id
        and t.client == client
        and t.status == QueueItemStatus.BLOCKED_ON_USER
    ]
    if not matches:
        return None
    return _newest_by_created_at(matches)


def _stamp_gate_recipe_failure(ticket_id: str, client: str, *, now: datetime) -> None:
    """Persist the one-shot failure latch (GitHub #1065).

    A fresh load/save round-trip, independent of the caller's outer ``store``
    snapshot: the caller (:func:`_act_auto_approve_review`) holds a
    pre-loop-hoisted snapshot that other candidates in the same loop may have
    already made stale via their own successful (separately-persisted)
    approve, so writing through that stale snapshot here would silently
    revert those. Caller MUST already hold ``dev_queue_lock()``, so this
    load-then-save is race-free against any other writer.
    """
    store = load_dev_queue()
    task = _find_blocked_task(store, ticket_id, client)
    if task is None:
        return
    task.gate_recipe_failed_at = now
    save_dev_queue(store)


def _handle_gate_recipe_approve_failure(
    task: TicketTask,
    session: Session,
    recipe: str,
    exc: CwError,
    *,
    now: datetime,
) -> None:
    """Log, emit a durable GATE_AUTO_APPROVE_FAILED correction, and stamp the
    one-shot failure latch (GitHub #1065/#1570).

    Shared body of both act phases' ``except CwError`` blocks. The mutation
    is caught and swallowed here, rather than left to propagate, because an
    uncaught raise would abort the rest of this reconcile tick (including
    ``run_escalation_sweep`` and every other still-valid candidate) and, via
    callers that don't wrap ``reconcile()`` in a broad except (e.g. ``cw
    status``), surface as a crash to unrelated CLI commands.

    The GATE_AUTO_APPROVED event already recorded before the mutation is
    durable, but without this correction it would stand alone on the
    operator channel as an uncorrected false-positive "approved" signal (a
    log line alone isn't queryable via the event stream). Stamping the
    one-shot failure latch then keeps a persisting failure from re-detecting
    and re-emitting both events every reconcile tick forever. Caller keeps
    its own ``continue``; this helper performs no control-flow.
    """
    _log.warning(
        "gate_recipe_approve_failed ticket=%s client=%s",
        task.ticket_id,
        task.client,
        exc_info=True,
    )
    record_event(
        OrchestratorEventType.GATE_AUTO_APPROVE_FAILED,
        {
            "ticket_id": task.ticket_id,
            "client": task.client,
            "lane": task.lane,
            "session_id": session.id,
            "recipe": recipe,
            "error": str(exc),
        },
        correlation_id=task.ticket_id,
    )
    _stamp_gate_recipe_failure(task.ticket_id, task.client, now=now)


def _log_gate_recipe_comment_skipped(ticket_id: str, client: str) -> None:
    """Log a dangling-client audit-comment skip (GitHub #1269/#1279 R7).

    Shared by :func:`_act_auto_approve_review` and :func:`_act_auto_adopt_plan`
    so the two identical skip sites can't drift independently.
    """
    _log.warning(
        "gate_recipe_comment_skipped ticket=%s client=%s: client "
        "missing from clients.yaml (config drift) -- gh call skipped, "
        "GitHub #1269",
        ticket_id,
        client,
    )


def _flush_gate_recipe_comment_jobs(
    comment_jobs: list[tuple[str, str, dict[str, object]]],
    clients: dict[str, ClientConfig] | None,
    post_fn: _CommentPostFn,
) -> None:
    """Post-lock, dangling-client-guarded best-effort comment flush.

    Shared loop body of both act phases' post-``dev_queue_lock()`` comment
    posting loop (GitHub #1570) — see the dangling-client skip rationale on
    :func:`_log_gate_recipe_comment_skipped` (#1269/#1279 R7). Caller keeps
    its own ``return approved``; this helper performs no control-flow beyond
    its own loop.
    """
    for ticket_id, client, snapshot in comment_jobs:
        if _is_dangling_client(client, clients or {}):
            _log_gate_recipe_comment_skipped(ticket_id, client)
            continue
        post_fn(ticket_id, snapshot, cwd=_client_cwd(client, clients or {}))


def _act_auto_approve_review(
    candidates: list[GateRecipeCandidate],
    *,
    now: datetime,
    clients: dict[str, ClientConfig] | None = None,
) -> list[str]:
    """Act phase: re-validate under lock, emit, then approve via the primitive.

    For each candidate the row + session are re-loaded fresh under
    ``dev_queue_lock()`` and the five-field predicate re-checked — a concurrent
    human approve, re-dispatch, or new sentinel between detect and act can have
    invalidated it (the re-check race). Only a still-valid candidate fires:
    :class:`OrchestratorEventType.GATE_AUTO_APPROVED` is emitted BEFORE the
    mutation, then the lock-free :func:`_approve_ticket_locked` advances the
    gate exactly as a human ``approve_ticket`` call would. Event payload
    sources come from the re-loaded row/session, never the (possibly stale)
    detect-time candidate. The audit comment is posted after the lock releases,
    best-effort — a comment-write failure never undoes the approve.

    A third outcome exists alongside "approved" and "raised" (RFC 0011 A3,
    #1160): the row may carry an armed proactive finalize hold, in which case
    :func:`_approve_ticket_locked` — called here WITHOUT ``operator_initiated``,
    i.e. as the automatic caller it is — declines to mutate and returns
    ``finalize_held=True``. That is not a failure, so it does not stamp the
    ``gate_recipe_failed_at`` latch; but ``GATE_AUTO_APPROVED`` is already
    durable by then, so a ``GATE_AUTO_APPROVE_HELD`` correction is emitted and
    the ticket is neither reported approved nor commented on.

    Known noise (deliberate, flagged for a follow-up ticket): a *persistently*
    armed hold — as opposed to one armed in the detect→act race window — re-runs
    this whole path on every reconcile tick, emitting a fresh
    GATE_AUTO_APPROVED/GATE_AUTO_APPROVE_HELD pair each time. No anti-noise latch
    is built here: reusing ``gate_recipe_failed_at`` would conflate a deliberate
    hold with a broken mutation (and would be cleared by the same transitions),
    and a second latch field is out of this ticket's scope.
    """
    if not candidates:
        return []
    # Keyed on (ticket_id, client): ticket_id alone is a per-repo GitHub issue
    # number, not globally unique across this multi-tenant system's clients —
    # keying on ticket_id alone would let two different clients' candidates
    # that happen to share a ticket_id collide and silently drop one.
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    approved: list[str] = []
    # (ticket_id, client, snapshot): client name is carried across the lock
    # boundary so the R7 dangling check runs against this tick's `clients`
    # snapshot at comment-post time rather than the detect-time candidate,
    # matching how every other field here is re-validated post-lock, not
    # detect-time state (GitHub #1279).
    comment_jobs: list[tuple[str, str, dict[str, object]]] = []
    with dev_queue_lock():
        # Loaded once: dev_queue_lock() is the exclusive writer lock for this
        # file, so no concurrent process can change it mid-loop, and every
        # mutation this loop performs goes through _approve_ticket_locked's
        # own internal load/save round-trip rather than this snapshot.
        store = load_dev_queue()
        for candidate in by_key.values():
            state = load_state()
            task = _find_blocked_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            if task.session_id is None:
                continue
            session = state.find_by_name_or_id(task.session_id)
            if session is None:
                continue
            snapshot = _clean_review_snapshot(session.last_result)
            if snapshot is None or not _predicate_holds(snapshot):
                continue
            record_event(
                OrchestratorEventType.GATE_AUTO_APPROVED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "lane": task.lane,
                    "session_id": session.id,
                    "recipe": RECIPE_AUTO_APPROVE_REVIEW,
                    "predicate_snapshot": snapshot,
                    "approved_at": now.isoformat(),
                },
                correlation_id=task.ticket_id,
            )
            try:
                # RFC 0009 / #1083: pin the mutation to THIS validated row's
                # identity so _approve_ticket_locked cannot re-resolve to a
                # newer AWAITING_OPERATOR_SIGNOFF duplicate and clear a signoff
                # gate this recipe never checked.
                result = _approve_ticket_locked(
                    task.ticket_id, task.client, resolved_task=task
                )
            except CwError as exc:
                _handle_gate_recipe_approve_failure(
                    task, session, RECIPE_AUTO_APPROVE_REVIEW, exc, now=now
                )
                continue
            if result["finalize_held"]:
                # RFC 0011 A3 (#1160): the row's proactive finalize hold
                # declined this automatic approve. Nothing was mutated and
                # nothing is broken, so no failure latch is stamped -- but the
                # already-durable GATE_AUTO_APPROVED needs a correction, or it
                # stands alone on the operator channel as a false "approved".
                _log.info(
                    "gate_recipe_approve_held ticket=%s client=%s",
                    task.ticket_id,
                    task.client,
                )
                record_event(
                    OrchestratorEventType.GATE_AUTO_APPROVE_HELD,
                    {
                        "ticket_id": task.ticket_id,
                        "client": task.client,
                        "lane": task.lane,
                        "session_id": session.id,
                        "recipe": RECIPE_AUTO_APPROVE_REVIEW,
                    },
                    correlation_id=task.ticket_id,
                )
                continue
            approved.append(task.ticket_id)
            comment_jobs.append((task.ticket_id, task.client, snapshot))
    _flush_gate_recipe_comment_jobs(comment_jobs, clients, _post_auto_approve_comment)
    return approved


def _act_auto_adopt_plan(
    candidates: list[GateRecipeCandidate],
    *,
    now: datetime,
    clients: dict[str, ClientConfig] | None = None,
) -> list[str]:
    """Act phase for auto_adopt_clean_plan: in-memory re-check, emit, approve.

    Mirrors :func:`_act_auto_approve_review` with one deliberate divergence
    (R5): the re-check under ``dev_queue_lock()`` reads ONLY already-loaded
    in-memory state — the row is still BLOCKED_ON_USER, its session still
    resolves, and ``session.last_result`` is still at the
    ``plan_pending_approval`` gate. It does NOT re-run
    :func:`_plan_of_record_body`/:func:`_clean_plan_snapshot`: the plan-of-
    record read is a ~30s ``gh`` subprocess, and the signoff markers are
    append-only, so cleanliness established at detect cannot regress between
    detect and act. Only task/session state can. ``candidate.evidence`` (the
    detect-time snapshot) is reused directly as the event's
    ``predicate_snapshot`` and the audit-comment source.
    """
    if not candidates:
        return []
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    approved: list[str] = []
    # (ticket_id, client, snapshot): see _act_auto_approve_review for why the
    # client name is deferred across the lock boundary (GitHub #1279 R7).
    comment_jobs: list[tuple[str, str, dict[str, object]]] = []
    with dev_queue_lock():
        # Both loaded once, unlike the sibling _act_auto_approve_review (which
        # reloads state per candidate): that function re-derives a fresh
        # predicate snapshot from session.last_result on every iteration, so a
        # stale state read would matter. This function's R5 recheck only
        # reads already-loaded task/session fields and never re-derives the
        # predicate, so a per-candidate reload buys no correctness benefit
        # while extending the exclusive dev_queue_lock() hold time.
        store = load_dev_queue()
        state = load_state()
        for candidate in by_key.values():
            task = _find_blocked_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            if task.session_id is None:
                continue
            session = state.find_by_name_or_id(task.session_id)
            if session is None:
                continue
            # In-memory re-check only (R5): no plan-of-record re-fetch. The
            # markers are append-only, so only task/session state can have
            # changed since detect.
            last_result = session.last_result
            if (
                not isinstance(last_result, dict)
                or last_result.get("status") != _PLAN_PENDING_APPROVAL
            ):
                continue
            snapshot = candidate.evidence
            record_event(
                OrchestratorEventType.GATE_AUTO_APPROVED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "lane": task.lane,
                    "session_id": session.id,
                    "recipe": RECIPE_AUTO_ADOPT_PLAN,
                    "predicate_snapshot": snapshot,
                    "approved_at": now.isoformat(),
                },
                correlation_id=task.ticket_id,
            )
            try:
                # RFC 0009 / #1083: pin the mutation to THIS validated row's
                # identity so _approve_ticket_locked cannot re-resolve to a
                # newer AWAITING_OPERATOR_SIGNOFF duplicate and clear a signoff
                # gate this recipe never checked. plan_reviewed=True (#968)
                # documents the no-refetch contract explicitly: this recipe's
                # detect phase already proved the clean-plan predicate holds
                # (both signoff markers present -- see _clean_plan_snapshot),
                # so the act phase must not trigger a second, redundant live
                # _plan_is_reviewed() fetch of the plan-of-record.
                _approve_ticket_locked(
                    task.ticket_id, task.client, resolved_task=task, plan_reviewed=True
                )
            except CwError as exc:
                _handle_gate_recipe_approve_failure(
                    task, session, RECIPE_AUTO_ADOPT_PLAN, exc, now=now
                )
                continue
            approved.append(task.ticket_id)
            comment_jobs.append((task.ticket_id, task.client, snapshot))
    _flush_gate_recipe_comment_jobs(comment_jobs, clients, _post_auto_adopt_comment)
    return approved


def run_gate_recipes(*, now: datetime, config: OrchestratorConfig) -> list[str]:
    """Run all enabled gate recipes for one reconcile tick.

    No-op (returns ``[]`` immediately) unless ``config.gate_recipes_enabled``
    is True. Loads fresh state/dev-queue snapshots itself rather than accepting
    them from the caller — by the time reconcile's ``_reconcile_locked`` reaches
    this wiring point several prior sweeps have already mutated and saved both
    files, so a caller-supplied snapshot would be stale (mirrors
    ``run_concierge_recoveries``). Safe to call while the caller already holds
    ``sessions_lock`` — this function only acquires ``dev_queue_lock`` per act
    phase, never ``sessions_lock`` itself.

    Returns the list of ticket IDs auto-approved this tick.
    """
    if not config.gate_recipes_enabled:
        return []

    state = load_state()
    tasks = load_dev_queue().tasks
    # Per-lane enablement (RFC 0009 P4) is resolved against effective clients —
    # load_effective_clients so lane pause/override state is honoured, matching
    # where the scheduler makes per-lane dispatch decisions.
    clients = load_effective_clients()

    # Review-then-plan order (R6), matching the constant declaration order. A
    # task sits at exactly one gate at a time, so review-approved rows are
    # never plan candidates; plan act re-loads under its own lock.
    approved = _act_auto_approve_review(
        _detect_auto_approve_review(state, tasks, clients=clients, config=config),
        now=now,
        clients=clients,
    )
    approved += _act_auto_adopt_plan(
        _detect_auto_adopt_plan(state, tasks, clients=clients, config=config),
        now=now,
        clients=clients,
    )
    return approved
