"""The shared engine of the conflict-repair review recipes (#2771, #2917).

Two recipes repair a shipped PR that GitHub reports ``CONFLICTING`` /
``DIRTY``: ``repair_changelog_conflict`` (``changelog_repair``) rebuilds a
CHANGELOG-only conflict, and ``repair_mechanical_conflict``
(``mechanical_repair``) runs an operator-declared command over conflicts
confined to declared paths. Everything except "is this conflict set mine" and
"rewrite the scratch tree" is the same, and lives here:

- **Act** (:func:`act_repairs`, under ``dev_queue_lock``, no subprocess):
  re-validate, check the cross-repo guard over the pre-captured slug,
  snapshot the fence inputs and return at most one :class:`RepairJob` per
  tick. Emits no success event and saves nothing; the only state is the
  recipe's in-process :class:`AttemptLatch`.
- **Dispatch** (:func:`dispatch_repair`, post-lock): fetch, check the PR
  head, derive the conflict set and hand it to the recipe's
  :attr:`RepairSpec.check_conflicts`; stage pre-worktree
  (:attr:`RepairSpec.stage`); rewrite in a detached scratch tree
  (:attr:`RepairSpec.rewrite`); then the shared tail: disarm auto-merge if
  armed, force-push with a lease, emit ``PR_ACTION_TAKEN`` (proof of the
  push), and re-arm pinned to the new head only if the review-approval and
  tip fence passes; otherwise leave the PR unarmed and emit a
  ``session.needs_attention`` page.

Every non-success exit emits ``PR_ACTION_FAILED`` with a
:class:`~cw.repair_disposition.RepairDisposition`; the row's status is never
changed. Log tokens are ``<RepairSpec.label>_<step>``, so the CHANGELOG
recipe keeps its ``changelog_repair_*`` text.

Imports ``_shared``, ``repair_git`` and ``repair_fence`` only, never a recipe
module: each recipe module builds its :class:`RepairSpec` and calls in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from cw import gh
from cw.dev_queue import dev_queue_lock, load_dev_queue
from cw.dev_queue.adoption import advance_pin_after_repair
from cw.events import record_event
from cw.exceptions import CwError
from cw.gh import _GH_PR_STATE_OPEN
from cw.models import OrchestratorEventType, QueueItemStatus, resolve_feature_branch
from cw.pr_hydrate import _parse_pr_url
from cw.reconcile._shared import _PAUSED_STATUS_KEY
from cw.reconcile.review_recipes._shared import (
    _ATTENTION_MERGE_BLOCKED,
    _PAYLOAD_KEY_CLIENT,
    _PAYLOAD_KEY_EVIDENCE_SNAPSHOT,
    _PAYLOAD_KEY_LANE,
    _PAYLOAD_KEY_RECIPE,
    _PAYLOAD_KEY_SESSION_ID,
    _PAYLOAD_KEY_TICKET_ID,
    _emit_pr_action_failed,
    _find_review_task,
    _record_pr_action_taken,
    _repo_guard_allows,
    _review_payload_base,
)
from cw.reconcile.review_recipes.repair_fence import (
    build_fence_snapshot,
    evaluate_repair_fence,
)
from cw.reconcile.review_recipes.repair_git import (
    _T_DISARM,
    _T_GH,
    RepairStopError,
    _conflict_set,
    _fetch_and_resolve,
    _ls_remote_tip,
    _push_with_lease,
    _run_arm,
    _scratch_worktree,
    scratch_path_for,
)
from cw.repair_disposition import RepairDisposition
from cw.worktree import _git_dir, resolve_worktree_base

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig, TicketTask
    from cw.reconcile.review_recipes._shared import RepoSlugs, ReviewRecipeCandidate
    from cw.reconcile.review_recipes.repair_fence import FenceSnapshot, FenceVerdict

    # auto_fix_ci._auto_fix_ci_guard_target's shape: (pr_repo, guarded git dir).
    GuardTarget = Callable[
        [TicketTask, dict[str, ClientConfig]], tuple[str, Path] | None
    ]

_logger = logging.getLogger("cw.reconcile.review_recipes")

LatchKey = tuple[str, str, str]

_MAX_ATTEMPTS = 3
_MAX_REPAIRS_PER_TICK = 1
_MERGEABLE_CONFLICTING = "CONFLICTING"
_MERGE_STATE_DIRTY = "DIRTY"
_UNARMED_DISPOSITION = RepairDisposition.UNARMED_REVIEW_FENCE.value
_REVIEW_TIP_STALE = "stale"
# Mirrors of .claude/scripts/prep_pr_finalize.py's EXIT_ARM_DISALLOWED and
# ARM_STATUS_ARMED / ARM_STATUS_MERGED. The script is excluded from the wheel,
# so it cannot be imported; tests/test_reconcile_changelog_repair.py pins them.
_ARM_OK = 0
_ARM_DISALLOWED = 3
_ARM_STATUS_ARMED = "armed"
_ARM_STATUS_MERGED = "merged"
# The recipe's own log token for exit 3; the script reports "skipped" there.
_ARM_STATUS_SKIPPED_CONFIG = "skipped_config"

_PAYLOAD_KEY_CONFLICT_KIND = "conflict_kind"
_PAYLOAD_KEY_DISPOSITION = "disposition"
_EVIDENCE_MERGE_STATE = "merge_state_status"
_EVIDENCE_MERGEABLE = "mergeable"
_EVIDENCE_CONFLICTED_PATHS = "conflicted_paths"
_EVIDENCE_EXPECTED_SHA = "expected_sha"
_EVIDENCE_NEW_SHA = "new_sha"
_EVIDENCE_WAS_ARMED = "auto_merge_was_armed"

_TRANSIENT: frozenset[RepairDisposition] = frozenset(
    {
        RepairDisposition.FETCH_FAILED,
        RepairDisposition.SCRATCH_WORKTREE_FAILED,
        RepairDisposition.PUSH_REJECTED,
        RepairDisposition.DISARM_FAILED,
    }
)


class AttemptLatch:
    """In-process attempt counter per ``(client, ticket_id, pr_url)``.

    Stands in for a persisted ``*_fired_at`` field (no schema bump): a daemon
    restart permits one more fully fail-closed attempt per still-conflicting
    PR. Successes and deterministic failures saturate it to the cap, so a
    ``pr_state`` that lags the push cannot re-fire the recipe. Each recipe
    owns its own instance.
    """

    def __init__(self) -> None:
        self._attempts: dict[LatchKey, int] = {}

    def attempts(self, key: LatchKey) -> int:
        return self._attempts.get(key, 0)

    def blocked(self, key: LatchKey) -> bool:
        return self.attempts(key) >= _MAX_ATTEMPTS

    def bump(self, key: LatchKey) -> None:
        self._attempts[key] = self.attempts(key) + 1

    def saturate(self, key: LatchKey) -> None:
        self._attempts[key] = _MAX_ATTEMPTS

    def sweep(self, live_keys: set[LatchKey]) -> None:
        """Drop every entry whose row is no longer a repair candidate."""
        for key in [k for k in self._attempts if k not in live_keys]:
            del self._attempts[key]

    def clear(self) -> None:
        self._attempts.clear()


class Prepared(NamedTuple):
    """What the shared prepare steps established about the PR."""

    branch: str
    repo: Path
    pr_number: int
    default_branch: str
    default_ref: str
    expected_sha: str
    merge_base: str
    conflicts: frozenset[str]


class Staged[T](NamedTuple):
    """A recipe's pre-worktree output: the arm script, a log summary, its data."""

    arm_script: Path
    summary: str
    data: T


@dataclass(frozen=True)
class RepairSpec[T]:
    """One repair recipe: its constants and its three strategy callables.

    *check_conflicts* raises :class:`RepairStopError` unless the derived
    conflict set is the recipe's to repair. *stage* runs pre-worktree, makes
    no repo change and returns the :class:`Staged` input of *rewrite*.
    *rewrite* owns the scratch tree from "it exists" up to, but excluding, the
    pre-push disarm, and returns the new head; it must store that head in
    ``state.evidence["new_sha"]`` before any check that can park, so the park
    still carries it.
    """

    recipe: str
    label: str
    conflict_kind: str
    unarmed_reason: str
    scratch_prefix: str
    unexpected: RepairDisposition
    latch: AttemptLatch
    hidden_changelog_conflict: bool
    record_conflicted_paths: bool
    check_conflicts: Callable[[frozenset[str], ClientConfig], None]
    stage: Callable[[RepairJob, Prepared], Staged[T]]
    rewrite: Callable[[RepairJob, Prepared, Staged[T], Path, RepairState], str]


class RepairJob(NamedTuple):
    """One deferred repair, built under the lock and run after it releases."""

    spec: RepairSpec[Any]
    client: str
    ticket_id: str
    # The row's resolved branch (#2948), fixed when the job is built under the
    # lock so every later step fetches, checks and pushes the same ref.
    branch: str
    lane: str
    pr_url: str
    session_id: str | None
    payload_base: dict[str, object]
    client_cfg: ClientConfig
    fence: FenceSnapshot
    config: OrchestratorConfig | None
    repeat_fire_counts: dict[tuple[str, str, str], int] | None
    latch_key: LatchKey


def is_repair_shape(task: TicketTask) -> bool:
    """The row is COMPLETED and its PR is merge_blocked + CONFLICTING + DIRTY."""
    pr_state = task.pr_state
    return (
        pr_state is not None
        and task.status == QueueItemStatus.COMPLETED
        and pr_state.attention_state == _ATTENTION_MERGE_BLOCKED
        and pr_state.mergeable == _MERGEABLE_CONFLICTING
        and pr_state.merge_state_status == _MERGE_STATE_DIRTY
    )


def repair_payload_base(
    spec: RepairSpec[Any], task: TicketTask, session_id: str | None
) -> dict[str, object]:
    """The PR_ACTION payload every TAKEN/FAILED of *spec*'s recipe carries."""
    pr_state = task.pr_state
    evidence: dict[str, object] = {
        _EVIDENCE_MERGE_STATE: None
        if pr_state is None
        else pr_state.merge_state_status,
        _EVIDENCE_MERGEABLE: None if pr_state is None else pr_state.mergeable,
    }
    if spec.record_conflicted_paths:
        evidence[_EVIDENCE_CONFLICTED_PATHS] = None
    evidence[_EVIDENCE_EXPECTED_SHA] = None
    evidence[_EVIDENCE_NEW_SHA] = None
    evidence[_EVIDENCE_WAS_ARMED] = None
    payload = _review_payload_base(task, session_id, spec.recipe, evidence)
    payload[_PAYLOAD_KEY_CONFLICT_KIND] = spec.conflict_kind
    return payload


def repair_latch_key(task: TicketTask) -> LatchKey:
    return task.client, task.ticket_id, task.pr_url or ""


def prepare_repair_job(
    spec: RepairSpec[Any],
    task: TicketTask,
    candidate: ReviewRecipeCandidate,
    *,
    clients: dict[str, ClientConfig],
    repo_slugs: RepoSlugs,
    guard_target: GuardTarget,
    config: OrchestratorConfig | None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None,
) -> RepairJob | None:
    """Re-validate one re-loaded row under the lock and build its job.

    Silent skip when the row no longer has the repair shape, the latch is
    spent, or the client is unresolvable (detect already required the recipe
    to be enabled for it). A cross-repo mismatch emits ``PR_ACTION_FAILED``
    through ``_repo_guard_allows``; a slug miss defers with the latch
    unbumped.
    """
    key = repair_latch_key(task)
    client_cfg = clients.get(task.client)
    if (
        not is_repair_shape(task)
        or task.pr_url is None
        or spec.latch.blocked(key)
        or client_cfg is None
    ):
        return None
    payload_base = repair_payload_base(spec, task, candidate.session_id)
    target = guard_target(task, clients)
    if target is not None and not _repo_guard_allows(
        task,
        payload_base,
        repo_slugs,
        pr_repo=target[0],
        git_dir=target[1],
        location="client workspace origin",
    ):
        return None
    spec.latch.bump(key)
    return RepairJob(
        spec=spec,
        client=task.client,
        ticket_id=task.ticket_id,
        branch=resolve_feature_branch(task.ticket_id, client_cfg, task),
        lane=task.lane,
        pr_url=task.pr_url,
        session_id=candidate.session_id,
        payload_base=payload_base,
        client_cfg=client_cfg,
        fence=build_fence_snapshot(task, client_cfg),
        config=config,
        repeat_fire_counts=repeat_fire_counts,
        latch_key=key,
    )


def act_repairs(
    spec: RepairSpec[Any],
    candidates: list[ReviewRecipeCandidate],
    *,
    clients: dict[str, ClientConfig],
    repo_slugs: RepoSlugs,
    guard_target: GuardTarget,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
    exclude: frozenset[tuple[str, str]] = frozenset(),
) -> list[RepairJob]:
    """Act phase: at most ``_MAX_REPAIRS_PER_TICK`` jobs, no subprocess, no save.

    Under one ``dev_queue_lock()``: sweep latch entries whose row left the
    repair shape (the episode-end analogue), then prepare jobs. A candidate
    whose ``(client, ticket_id)`` is in *exclude* (another recipe's job for
    the same row this tick) is skipped silently, latch unbumped.
    *guard_target* is ``auto_fix_ci._auto_fix_ci_guard_target`` (the client
    workspace), passed in by ``core`` so no recipe module imports a sibling.
    The jobs are queued on ``reconcile()``'s post-lock sink by
    ``run_review_recipes``; the act itself emits no success event.
    """
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    jobs: list[RepairJob] = []
    with dev_queue_lock():
        store = load_dev_queue()
        spec.latch.sweep(
            {repair_latch_key(t) for t in store.tasks if is_repair_shape(t)}
        )
        for candidate in by_key.values():
            if len(jobs) >= _MAX_REPAIRS_PER_TICK:
                break
            if (candidate.client, candidate.ticket_id) in exclude:
                continue
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            job = prepare_repair_job(
                spec,
                task,
                candidate,
                clients=clients,
                repo_slugs=repo_slugs,
                guard_target=guard_target,
                config=config,
                repeat_fire_counts=repeat_fire_counts,
            )
            if job is not None:
                jobs.append(job)
    return jobs


# --- post-lock dispatch ---------------------------------------------------------


class RepairState:
    """The working payload of one dispatch and what is known so far."""

    def __init__(self, job: RepairJob) -> None:
        base = job.payload_base.get(_PAYLOAD_KEY_EVIDENCE_SNAPSHOT)
        self.evidence: dict[str, object] = dict(base) if isinstance(base, dict) else {}
        self.payload: dict[str, object] = {
            **job.payload_base,
            _PAYLOAD_KEY_EVIDENCE_SNAPSHOT: self.evidence,
        }
        self.disarmed = False
        self.pushed = False


class UnarmOutcome(NamedTuple):
    """Whether the PR is verified unarmed, whether it was armed, and why."""

    ok: bool
    was_armed: bool | None
    detail: str


def _ensure_unarmed(
    pr_ref: str, *, cwd: Path, head: gh.PrHead | None = None
) -> UnarmOutcome:
    """Read the PR's armed state, disarm if armed, verify by reading back.

    *head* reuses a read the caller already made. The read-back wins over
    the disarm exit code (the ``arm_automerge`` rule, #1140): a non-zero exit
    with an unarmed read-back is success. Fails closed (``ok`` False) on an
    unreadable PR, a disarm that could not run or timed out, or a read-back
    that is unreadable or still armed. Never raises.
    """
    current = (
        head if head is not None else gh.fetch_pr_head(pr_ref, cwd=cwd, timeout=_T_GH)
    )
    if current is None:
        return UnarmOutcome(False, None, "PR head unreadable; armed state unknown")
    if not current.auto_merge_armed:
        return UnarmOutcome(True, False, "auto-merge not armed")
    result = gh.disarm_pr_auto_merge(pr_ref, cwd=cwd, timeout=_T_DISARM)
    if result is None:
        return UnarmOutcome(
            False,
            True,
            "gh pr merge --disable-auto did not run or timed out; still ARMED",
        )
    after = gh.fetch_pr_head(pr_ref, cwd=cwd, timeout=_T_GH)
    if after is None:
        return UnarmOutcome(
            False,
            True,
            f"disarm exit {result.returncode}; read-back unreadable,"
            " may still be ARMED",
        )
    if after.auto_merge_armed:
        return UnarmOutcome(
            False, True, f"disarm exit {result.returncode}; read-back still ARMED"
        )
    return UnarmOutcome(True, True, f"disarmed (exit {result.returncode})")


def _armed_note(outcome: UnarmOutcome) -> str:
    """What is known of the PR's armed state after a best-effort unarm."""
    if outcome.ok:
        return "auto-merge is verified unarmed"
    return f"auto-merge may still be ARMED ({outcome.detail})"


def _read_head(job: RepairJob, branch: str, expected_sha: str, repo: Path) -> gh.PrHead:
    """The PR head, required OPEN on the default branch, at *branch*@*expected_sha*.

    A PR stacked on another base is conflicting against that base, not main,
    and a closed or merged PR is a human decision: the repair rewrites neither.
    """
    mismatch = RepairDisposition.PR_HEAD_MISMATCH
    head = gh.fetch_pr_head(job.pr_url, cwd=repo, timeout=_T_GH)
    if head is None:
        raise RepairStopError(mismatch, "could not read the PR head")
    default_branch = job.client_cfg.default_branch
    if head.state != _GH_PR_STATE_OPEN or head.base_ref_name != default_branch:
        raise RepairStopError(
            mismatch,
            f"PR is state={head.state} base={head.base_ref_name}, expected"
            f" state={_GH_PR_STATE_OPEN} base={default_branch}",
        )
    if head.ref_name != branch or head.oid != expected_sha:
        raise RepairStopError(
            mismatch,
            f"PR head is {head.ref_name}@{head.oid}, expected {branch}@{expected_sha}",
        )
    return head


def prepare_repair(job: RepairJob, state: RepairState) -> Prepared:
    """Steps 1-5: resolve, fetch the pre-image, check the head, derive conflicts.

    The derived set goes to the recipe's ``check_conflicts``; a recipe that
    records it (``record_conflicted_paths``) stores it in the evidence first,
    so every later park carries it.
    """
    spec = job.spec
    client_cfg = job.client_cfg
    branch = job.branch
    repo = _git_dir(client_cfg)
    parsed = _parse_pr_url(job.pr_url)
    if parsed is None:
        raise RepairStopError(
            RepairDisposition.PR_HEAD_MISMATCH,
            f"pr_url {job.pr_url!r} is not a GitHub PR url",
        )
    default_branch = client_cfg.default_branch
    default_ref = f"refs/remotes/origin/{default_branch}"
    expected_sha = _fetch_and_resolve(repo, default_branch, branch)
    state.evidence[_EVIDENCE_EXPECTED_SHA] = expected_sha
    head = _read_head(job, branch, expected_sha, repo)
    state.evidence[_EVIDENCE_WAS_ARMED] = head.auto_merge_armed
    conflicts, merge_base = _conflict_set(
        repo,
        default_ref,
        expected_sha,
        hidden_changelog_conflict=spec.hidden_changelog_conflict,
    )
    if spec.record_conflicted_paths:
        state.evidence[_EVIDENCE_CONFLICTED_PATHS] = sorted(conflicts)
    spec.check_conflicts(conflicts, client_cfg)
    return Prepared(
        branch=branch,
        repo=repo,
        pr_number=parsed[1],
        default_branch=default_branch,
        default_ref=default_ref,
        expected_sha=expected_sha,
        merge_base=merge_base,
        conflicts=conflicts,
    )


def _disarm_before_push(job: RepairJob, prep: Prepared, state: RepairState) -> None:
    """Step 13: re-check the head, then disarm an armed PR, fail closed."""
    head = _read_head(job, prep.branch, prep.expected_sha, prep.repo)
    state.evidence[_EVIDENCE_WAS_ARMED] = head.auto_merge_armed
    if not head.auto_merge_armed:
        return
    outcome = _ensure_unarmed(job.pr_url, cwd=prep.repo, head=head)
    if not outcome.ok:
        raise RepairStopError(
            RepairDisposition.DISARM_FAILED,
            f"could not disarm auto-merge before the force-push; nothing pushed"
            f" ({outcome.detail})",
        )
    state.disarmed = True
    _logger.info(
        "%s_disarmed client=%s ticket=%s pr=%s expected_sha=%s phase=%s",
        job.spec.label,
        job.client,
        job.ticket_id,
        prep.pr_number,
        prep.expected_sha,
        "pre_push",
    )


def _rewrite_and_push(
    job: RepairJob,
    prep: Prepared,
    staged: Staged[Any],
    scratch: Path,
    state: RepairState,
) -> str:
    """The recipe's rewrite in the scratch tree, then steps 13-14; the new head."""
    new_sha = job.spec.rewrite(job, prep, staged, scratch, state)
    _disarm_before_push(job, prep, state)
    # Logged before the push so the force-push pre-image is durable even if the
    # daemon dies before PR_ACTION_TAKEN is recorded.
    _logger.info(
        "%s_pushing client=%s ticket=%s pr=%s branch=%s expected_sha=%s new_sha=%s",
        job.spec.label,
        job.client,
        job.ticket_id,
        prep.pr_number,
        prep.branch,
        prep.expected_sha,
        new_sha,
    )
    _push_with_lease(scratch, prep.branch, prep.expected_sha, new_sha)
    job.spec.latch.saturate(job.latch_key)
    state.pushed = True
    return new_sha


def _breadcrumbs(
    job: RepairJob, branch: str, verdict: FenceVerdict, new_sha: str, expected_sha: str
) -> str:
    return (
        f"review_tip_check: stale ({verdict.detail}); repaired head {new_sha} pushed"
        f" over {expected_sha}; auto-merge is NOT armed (it was disarmed first if it"
        f" had been armed). Release: cw dev-queue requeue {job.ticket_id} -c"
        f" {job.client} --from-completed (re-runs finalize on the repaired head; if"
        " the ticket worktree still exists, first run git -C <worktree> fetch origin"
        f" and then git -C <worktree> reset --hard origin/{branch} only if git -C"
        " <worktree> status --porcelain prints nothing and git -C <worktree> log"
        f" --oneline {expected_sha}..HEAD is empty, otherwise stop and inspect first,"
        " because finalize will not reset over local commits origin lacks and the"
        " reset would destroy them; if finalize parks again, the"
        " BLOCKED_ON_USER row it leaves is released by its own park command, e.g."
        " requeue --stage review)"
    )


def _emit_unarmed_attention(
    job: RepairJob,
    verdict: FenceVerdict,
    *,
    branch: str,
    expected_sha: str,
    new_sha: str,
    was_armed: object,
) -> None:
    """The repaired-but-unarmed page (open-enum ``paused_status``)."""
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            _PAYLOAD_KEY_CLIENT: job.client,
            _PAYLOAD_KEY_TICKET_ID: job.ticket_id,
            _PAYLOAD_KEY_RECIPE: job.spec.recipe,
            _PAYLOAD_KEY_SESSION_ID: job.session_id,
            _PAUSED_STATUS_KEY: job.spec.unarmed_reason,
            _PAYLOAD_KEY_LANE: job.lane,
            "review_tip_check": _REVIEW_TIP_STALE,
            "review_approval_check": verdict.approval_token,
            _PAYLOAD_KEY_DISPOSITION: _UNARMED_DISPOSITION,
            _EVIDENCE_EXPECTED_SHA: expected_sha,
            _EVIDENCE_NEW_SHA: new_sha,
            _EVIDENCE_WAS_ARMED: was_armed,
            "breadcrumbs": _breadcrumbs(job, branch, verdict, new_sha, expected_sha),
        },
        correlation_id=job.ticket_id,
    )


def _leave_unarmed(
    job: RepairJob,
    prep: Prepared,
    state: RepairState,
    verdict: FenceVerdict,
    new_sha: str,
) -> str:
    """Step 17, fence failed: verify unarmed, then page; never arm."""
    outcome = _ensure_unarmed(job.pr_url, cwd=prep.repo)
    if not outcome.ok:
        raise RepairStopError(
            RepairDisposition.DISARM_FAILED,
            f"PUSHED {prep.expected_sha} -> {new_sha}; auto-merge may still be ARMED"
            f" against an unreviewed head; run `gh pr merge {prep.pr_number}"
            f" --disable-auto` ({outcome.detail}; fence: {verdict.detail})",
        )
    if outcome.was_armed:
        _logger.info(
            "%s_disarmed client=%s ticket=%s pr=%s expected_sha=%s phase=%s",
            job.spec.label,
            job.client,
            job.ticket_id,
            prep.pr_number,
            prep.expected_sha,
            "post_push_fence",
        )
    was_armed = state.evidence.get(_EVIDENCE_WAS_ARMED)
    _emit_unarmed_attention(
        job,
        verdict,
        branch=prep.branch,
        expected_sha=prep.expected_sha,
        new_sha=new_sha,
        was_armed=was_armed,
    )
    _logger.warning(
        "%s_pushed_unarmed client=%s ticket=%s pr=%s expected_sha=%s"
        " new_sha=%s was_armed=%s review_tip_check=stale approval=%s detail=%s",
        job.spec.label,
        job.client,
        job.ticket_id,
        prep.pr_number,
        prep.expected_sha,
        new_sha,
        was_armed,
        verdict.approval_token,
        verdict.detail,
    )
    return job.ticket_id


def _arm(
    job: RepairJob,
    prep: Prepared,
    staged: Staged[Any],
    scratch: Path,
    state: RepairState,
    new_sha: str,
) -> str:
    """Step 17, fence passed: a fresh arm pinned to *new_sha*."""
    outcome = _run_arm(staged.arm_script, prep.pr_number, scratch, new_sha)
    if outcome.returncode == _ARM_DISALLOWED:
        _logger.info(
            "%s_arm_skipped client=%s ticket=%s pr=%s new_sha=%s"
            " reason=pr.auto_merge_false",
            job.spec.label,
            job.client,
            job.ticket_id,
            prep.pr_number,
            new_sha,
        )
        arm_status = _ARM_STATUS_SKIPPED_CONFIG
    elif outcome.returncode == _ARM_OK:
        arm_status = (
            _ARM_STATUS_MERGED
            if outcome.status == _ARM_STATUS_MERGED
            else _ARM_STATUS_ARMED
        )
    else:
        unarm = _ensure_unarmed(job.pr_url, cwd=prep.repo)
        raise RepairStopError(
            RepairDisposition.REARM_FAILED,
            f"arm-automerge exit {outcome.returncode} after force-push"
            f" {prep.expected_sha} -> {new_sha}; PR repaired but not re-armed:"
            f" {outcome.stderr_tail}; {_armed_note(unarm)}",
        )
    _logger.info(
        "%s_succeeded client=%s ticket=%s pr=%s branch=%s"
        " expected_sha=%s new_sha=%s was_armed=%s arm=%s %s",
        job.spec.label,
        job.client,
        job.ticket_id,
        prep.pr_number,
        prep.branch,
        prep.expected_sha,
        new_sha,
        state.evidence.get(_EVIDENCE_WAS_ARMED),
        arm_status,
        staged.summary,
    )
    return job.ticket_id


def _finish_steps(
    job: RepairJob,
    prep: Prepared,
    staged: Staged[Any],
    scratch: Path,
    state: RepairState,
    new_sha: str,
) -> str:
    """Steps 15-17: PR_ACTION_TAKEN, the fence, then arm or the unarmed page.

    First, an RFC 0016 adopted row's pin follows the lease push (#2943): the
    push replaced ``prep.expected_sha`` with *new_sha* on origin, and it is
    cw's own, so the drift gate must not park it. A compare-and-set under a
    brief ``dev_queue_lock`` with no I/O; a non-adopted row is a no-op. Inside
    the ``_finish_repair`` guard, so a failure here is ``post_push_failed``.
    """
    advance_pin_after_repair(
        job.client, job.ticket_id, expected_sha=prep.expected_sha, new_sha=new_sha
    )
    _record_pr_action_taken(
        state.payload,
        job.client,
        job.ticket_id,
        job.spec.recipe,
        config=job.config,
        repeat_fire_counts=job.repeat_fire_counts,
        lane=job.lane,
    )
    origin_tip = _ls_remote_tip(prep.repo, prep.branch)
    verdict = evaluate_repair_fence(
        job.fence, repo=prep.repo, new_sha=new_sha, origin_tip=origin_tip
    )
    if verdict.passed:
        return _arm(job, prep, staged, scratch, state, new_sha)
    return _leave_unarmed(job, prep, state, verdict, new_sha)


def _finish_repair(
    job: RepairJob,
    prep: Prepared,
    staged: Staged[Any],
    scratch: Path,
    state: RepairState,
    new_sha: str,
) -> str:
    """Steps 15-17 behind one guard: any crash after the push is post_push_failed."""
    try:
        return _finish_steps(job, prep, staged, scratch, state, new_sha)
    except RepairStopError:
        raise
    except Exception as exc:
        # Why a broad catch: the push has already happened, so ANY failure here
        # (a record_event error, a fence bug) must still be reported as a
        # repaired-but-unfinished PR, never escape as if nothing was pushed.
        # Logged with the traceback; the latch is already saturated, so it is
        # never retried. Paired test: test_crash_after_push_is_post_push_failed.
        _logger.exception(
            "%s_post_push_crashed client=%s ticket=%s",
            job.spec.label,
            job.client,
            job.ticket_id,
        )
        unarm = _ensure_unarmed(job.pr_url, cwd=prep.repo)
        detail = (
            f"PUSHED {prep.expected_sha} -> {new_sha}; the PR is repaired but the"
            f" fence or arm step crashed ({type(exc).__name__}: {exc});"
            f" {_armed_note(unarm)}"
        )
        raise RepairStopError(RepairDisposition.POST_PUSH_FAILED, detail) from exc


def _park(job: RepairJob, state: RepairState, stop: RepairStopError) -> None:
    """Emit the one PR_ACTION_FAILED for this attempt; saturate if deterministic."""
    if stop.disposition not in _TRANSIENT:
        job.spec.latch.saturate(job.latch_key)
    detail = stop.detail
    if stop.disposition == RepairDisposition.PUSH_OUTCOME_UNKNOWN:
        detail = (
            f"{detail}; the push may have landed: compare origin/{job.branch}"
            f" with expected_sha {state.evidence.get(_EVIDENCE_EXPECTED_SHA)}"
        )
    if state.disarmed and not state.pushed:
        detail = f"{detail} (auto-merge was disarmed and is NOT re-armed)"
    _logger.warning(
        "%s_parked client=%s ticket=%s disposition=%s: %s",
        job.spec.label,
        job.client,
        job.ticket_id,
        stop.disposition.value,
        detail,
    )
    _emit_pr_action_failed(
        {**state.payload, _PAYLOAD_KEY_DISPOSITION: stop.disposition.value},
        error=detail,
        ticket_id=job.ticket_id,
    )


def dispatch_repair(job: RepairJob) -> str | None:
    """Repair one PR post-lock; the acted ticket_id, or None when it parked.

    Owns the scratch tree's lifetime: the ``with`` spans the rewrite through
    step 17, and the single ``RepairStopError`` handler sits outside it, so
    cleanup always runs before the failure event. Holds no lock.
    """
    spec = job.spec
    state = RepairState(job)
    try:
        prep = prepare_repair(job, state)
        staged = spec.stage(job, prep)
        scratch = scratch_path_for(
            resolve_worktree_base(job.client_cfg), job.ticket_id, spec.scratch_prefix
        )
        with _scratch_worktree(
            prep.repo, scratch, prep.expected_sha, spec.label
        ) as tree:
            new_sha = _rewrite_and_push(job, prep, staged, tree, state)
            return _finish_repair(job, prep, staged, tree, state, new_sha)
    except RepairStopError as stop:
        _park(job, state, stop)
        return None
    except (OSError, CwError) as exc:
        # Why: nothing before the push may escape the one-FAILED-with-disposition
        # contract. A stray I/O or cw error is deterministic, so park it (which
        # saturates the latch) under the recipe's catch-all disposition instead
        # of letting _run_isolated emit a bare FAILED that repeats on up to
        # three ticks. Post-push failures never get here: _finish_repair
        # converts them to post_push_failed.
        _park(
            job,
            state,
            RepairStopError(
                spec.unexpected,
                f"unexpected {type(exc).__name__} before the push: {exc}",
            ),
        )
        return None
