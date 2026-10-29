"""The repair_changelog_conflict recipe: CHANGELOG-only conflicts (#2771).

``CHANGELOG.md merge=union`` (``.gitattributes``) means a local rebase never
conflicts on CHANGELOG.md, while GitHub (which ignores merge drivers) reports
the PR ``CONFLICTING`` / ``DIRTY``. Every feat/fix PR adds an ``[Unreleased]``
bullet, so an open pipeline PR goes conflicting whenever another PR lands
first, and a textual rebase after a release silently files bullets under the
released version.

This recipe repairs such a PR when its ONLY conflict is CHANGELOG.md:

- **Detect** (pure, write-free): ``merge_blocked`` rows whose PR is
  ``CONFLICTING`` + ``DIRTY`` and whose queue row is ``COMPLETED`` (shipped,
  no worker that could push concurrently). The same row is also an
  ``escalate_merge_block`` candidate: that page still fires, by design.
- **Act** (under ``dev_queue_lock``, no subprocess): re-validate, check the
  cross-repo guard over the pre-captured slug, snapshot the fence inputs and
  return at most one :class:`_RepairJob` per tick. Emits no success event and
  saves nothing; the only state is the in-process :data:`_LATCH`.
- **Dispatch** (post-lock, :func:`_dispatch_changelog_repair`): fetch, derive
  the conflict set, rebuild CHANGELOG.md structurally
  (:func:`cw.changelog_rebuild.rebuild_changelog`), rebase the rest in a
  detached scratch tree, verify the non-CHANGELOG diff is unchanged, run the
  freeze gate, disarm auto-merge if armed, force-push with a lease, emit
  ``PR_ACTION_TAKEN`` (proof of the push), then re-arm pinned to the new head
  only if the review-approval and tip fence passes; otherwise leave the PR
  unarmed and emit a ``session.needs_attention`` page.

Every non-success exit emits ``PR_ACTION_FAILED`` with a
:class:`~cw.changelog_rebuild.ChangelogRepairDisposition`; the row's status is
never changed. Not in ``RECIPE_ATTENTION_STATES`` / ``RECIPE_FIRED_AT_GETTERS``:
it has no persisted ``*_fired_at`` latch for the doctor liveness check to read.
No LLM session is spawned, so usage limits do not apply.

Leaf module: depends only on ``_shared``, the two ``changelog_repair_*``
modules and ``cw.changelog_rebuild`` (plus ``cw`` infrastructure). Module-level
imports only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from cw import gh
from cw.changelog_rebuild import (
    ChangelogRebuildError,
    ChangelogRepairDisposition,
    rebuild_changelog,
)
from cw.dev_queue import dev_queue_lock, load_dev_queue
from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus
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
    RECIPE_REPAIR_CHANGELOG_CONFLICT,
    ReviewRecipeCandidate,
    _detect_by_attention_state,
    _emit_pr_action_failed,
    _find_review_task,
    _record_pr_action_taken,
    _repo_guard_allows,
    _review_payload_base,
)
from cw.reconcile.review_recipes.changelog_repair_fence import (
    FenceSnapshot,
    FenceVerdict,
    build_fence_snapshot,
    evaluate_repair_fence,
)
from cw.reconcile.review_recipes.changelog_repair_git import (
    _T_DISARM,
    _T_GH,
    RepairStopError,
    _commit_rebuilt,
    _conflict_set,
    _fetch_and_resolve,
    _head_sha,
    _ls_remote_tip,
    _probe_arm_script,
    _push_with_lease,
    _read_three_blobs,
    _rebase_onto,
    _run_arm,
    _run_changelog_gate,
    _scratch_worktree,
    _verify_non_changelog_identical,
    scratch_path_for,
)
from cw.worktree import _git_dir, resolve_worktree_base

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig, TicketTask
    from cw.reconcile.review_recipes._shared import RepoSlugs

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
_CONFLICT_KIND = "changelog_only_candidate"
_UNARMED_ATTENTION_REASON = "changelog_repair_unarmed"
_UNARMED_DISPOSITION = ChangelogRepairDisposition.UNARMED_REVIEW_FENCE.value
_REVIEW_TIP_STALE = "stale"
_ARM_OK = 0
_ARM_DISALLOWED = 3
_ARM_STATUS_MERGED = "merged"

_PAYLOAD_KEY_CONFLICT_KIND = "conflict_kind"
_PAYLOAD_KEY_DISPOSITION = "disposition"
_EVIDENCE_MERGE_STATE = "merge_state_status"
_EVIDENCE_MERGEABLE = "mergeable"
_EVIDENCE_EXPECTED_SHA = "expected_sha"
_EVIDENCE_NEW_SHA = "new_sha"
_EVIDENCE_WAS_ARMED = "auto_merge_was_armed"

_TRANSIENT: frozenset[ChangelogRepairDisposition] = frozenset(
    {
        ChangelogRepairDisposition.FETCH_FAILED,
        ChangelogRepairDisposition.SCRATCH_WORKTREE_FAILED,
        ChangelogRepairDisposition.PUSH_REJECTED,
        ChangelogRepairDisposition.DISARM_FAILED,
    }
)


class _AttemptLatch:
    """In-process attempt counter per ``(client, ticket_id, pr_url)``.

    Stands in for a persisted ``*_fired_at`` field (no schema bump): a daemon
    restart permits one more fully fail-closed attempt per still-conflicting
    PR. Successes and deterministic failures saturate it to the cap, so a
    ``pr_state`` that lags the push cannot re-fire the recipe.
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


_LATCH = _AttemptLatch()


def _is_repair_shape(task: TicketTask) -> bool:
    """The row is COMPLETED and its PR is merge_blocked + CONFLICTING + DIRTY."""
    pr_state = task.pr_state
    return (
        pr_state is not None
        and task.status == QueueItemStatus.COMPLETED
        and pr_state.attention_state == _ATTENTION_MERGE_BLOCKED
        and pr_state.mergeable == _MERGEABLE_CONFLICTING
        and pr_state.merge_state_status == _MERGE_STATE_DIRTY
    )


def _detect_repair_changelog_conflict(
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[ReviewRecipeCandidate]:
    """Detect COMPLETED rows whose PR is CONFLICTING/DIRTY (pure, write-free).

    ``BEHIND`` is not a conflict; a non-COMPLETED row could have a worker
    pushing concurrently, or an approved sha a rewrite would invalidate.
    """
    return _detect_by_attention_state(
        [task for task in tasks if _is_repair_shape(task)],
        clients=clients,
        config=config,
        attention_state=_ATTENTION_MERGE_BLOCKED,
        recipe=RECIPE_REPAIR_CHANGELOG_CONFLICT,
    )


class _RepairJob(NamedTuple):
    """One deferred repair, built under the lock and run after it releases."""

    client: str
    ticket_id: str
    lane: str
    pr_url: str
    session_id: str | None
    payload_base: dict[str, object]
    client_cfg: ClientConfig
    fence: FenceSnapshot
    config: OrchestratorConfig | None
    repeat_fire_counts: dict[tuple[str, str, str], int] | None
    latch_key: LatchKey


def _repair_payload_base(task: TicketTask, session_id: str | None) -> dict[str, object]:
    """The PR_ACTION payload every TAKEN/FAILED of this recipe carries."""
    pr_state = task.pr_state
    evidence: dict[str, object] = {
        _EVIDENCE_MERGE_STATE: None
        if pr_state is None
        else pr_state.merge_state_status,
        _EVIDENCE_MERGEABLE: None if pr_state is None else pr_state.mergeable,
        _EVIDENCE_EXPECTED_SHA: None,
        _EVIDENCE_NEW_SHA: None,
        _EVIDENCE_WAS_ARMED: None,
    }
    payload = _review_payload_base(
        task, session_id, RECIPE_REPAIR_CHANGELOG_CONFLICT, evidence
    )
    payload[_PAYLOAD_KEY_CONFLICT_KIND] = _CONFLICT_KIND
    return payload


def _latch_key(task: TicketTask) -> LatchKey:
    return task.client, task.ticket_id, task.pr_url or ""


def _prepare_repair_job(
    task: TicketTask,
    candidate: ReviewRecipeCandidate,
    *,
    clients: dict[str, ClientConfig],
    repo_slugs: RepoSlugs,
    guard_target: GuardTarget,
    config: OrchestratorConfig | None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None,
) -> _RepairJob | None:
    """Re-validate one re-loaded row under the lock and build its job.

    Silent skip when the row no longer has the repair shape, the latch is
    spent, or the client is unresolvable (detect already required the recipe
    to be enabled for it). A cross-repo mismatch emits ``PR_ACTION_FAILED``
    through ``_repo_guard_allows``; a slug miss defers with the latch
    unbumped.
    """
    key = _latch_key(task)
    client_cfg = clients.get(task.client)
    if (
        not _is_repair_shape(task)
        or task.pr_url is None
        or _LATCH.blocked(key)
        or client_cfg is None
    ):
        return None
    payload_base = _repair_payload_base(task, candidate.session_id)
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
    _LATCH.bump(key)
    return _RepairJob(
        client=task.client,
        ticket_id=task.ticket_id,
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


def _act_repair_changelog_conflict(
    candidates: list[ReviewRecipeCandidate],
    *,
    clients: dict[str, ClientConfig],
    repo_slugs: RepoSlugs,
    guard_target: GuardTarget,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> list[_RepairJob]:
    """Act phase: at most ``_MAX_REPAIRS_PER_TICK`` jobs, no subprocess, no save.

    Under one ``dev_queue_lock()``: sweep latch entries whose row left the
    repair shape (the episode-end analogue), then prepare jobs. *guard_target*
    is ``auto_fix_ci._auto_fix_ci_guard_target`` (the client workspace),
    passed in by ``core`` so this leaf module imports no sibling recipe. The
    jobs are queued on ``reconcile()``'s post-lock sink by
    ``run_review_recipes``; the act itself emits no success event.
    """
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    jobs: list[_RepairJob] = []
    with dev_queue_lock():
        store = load_dev_queue()
        _LATCH.sweep({_latch_key(t) for t in store.tasks if _is_repair_shape(t)})
        for candidate in by_key.values():
            if len(jobs) >= _MAX_REPAIRS_PER_TICK:
                break
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            job = _prepare_repair_job(
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


class _RepairState:
    """The working payload of one dispatch and what is known so far."""

    def __init__(self, job: _RepairJob) -> None:
        base = job.payload_base.get(_PAYLOAD_KEY_EVIDENCE_SNAPSHOT)
        self.evidence: dict[str, object] = dict(base) if isinstance(base, dict) else {}
        self.payload: dict[str, object] = {
            **job.payload_base,
            _PAYLOAD_KEY_EVIDENCE_SNAPSHOT: self.evidence,
        }
        self.disarmed = False
        self.pushed = False


class _Prepared(NamedTuple):
    branch: str
    repo: Path
    pr_number: int
    default_branch: str
    default_ref: str
    expected_sha: str
    merge_base: str


class _Staged(NamedTuple):
    rebuilt: str
    main_text: str
    arm_script: Path


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


def _read_head(
    job: _RepairJob, branch: str, expected_sha: str, repo: Path
) -> gh.PrHead:
    """The PR head, required to be *branch* at *expected_sha*."""
    head = gh.fetch_pr_head(job.pr_url, cwd=repo, timeout=_T_GH)
    if head is None:
        raise RepairStopError(
            ChangelogRepairDisposition.PR_HEAD_MISMATCH, "could not read the PR head"
        )
    if head.ref_name != branch or head.oid != expected_sha:
        raise RepairStopError(
            ChangelogRepairDisposition.PR_HEAD_MISMATCH,
            f"PR head is {head.ref_name}@{head.oid}, expected {branch}@{expected_sha}",
        )
    return head


def _prepare_repair(job: _RepairJob, state: _RepairState) -> _Prepared:
    """Steps 1-5: resolve, fetch the pre-image, check the head, derive conflicts."""
    client_cfg = job.client_cfg
    branch = f"{client_cfg.feature_branch_prefix}/{job.ticket_id}"
    repo = _git_dir(client_cfg)
    parsed = _parse_pr_url(job.pr_url)
    if parsed is None:
        raise RepairStopError(
            ChangelogRepairDisposition.PR_HEAD_MISMATCH,
            f"pr_url {job.pr_url!r} is not a GitHub PR url",
        )
    default_branch = client_cfg.default_branch
    default_ref = f"refs/remotes/origin/{default_branch}"
    expected_sha = _fetch_and_resolve(repo, default_branch, branch)
    state.evidence[_EVIDENCE_EXPECTED_SHA] = expected_sha
    head = _read_head(job, branch, expected_sha, repo)
    state.evidence[_EVIDENCE_WAS_ARMED] = head.auto_merge_armed
    conflicts, merge_base = _conflict_set(repo, default_ref, expected_sha)
    if not conflicts:
        raise RepairStopError(
            ChangelogRepairDisposition.CONFLICT_UNEXPLAINED,
            "GitHub reports CONFLICTING but no conflicted file was derived",
        )
    if conflicts != {"CHANGELOG.md"}:
        raise RepairStopError(
            ChangelogRepairDisposition.NON_CHANGELOG_CONFLICT,
            f"conflicted files: {sorted(conflicts)}",
        )
    return _Prepared(
        branch=branch,
        repo=repo,
        pr_number=parsed[1],
        default_branch=default_branch,
        default_ref=default_ref,
        expected_sha=expected_sha,
        merge_base=merge_base,
    )


def _stage_rewrite(prep: _Prepared) -> _Staged:
    """Steps 6-7: rebuild CHANGELOG.md and find the arm script, pre-worktree."""
    base, branch_text, main_text = _read_three_blobs(
        prep.repo, prep.merge_base, prep.expected_sha, prep.default_ref
    )
    try:
        result = rebuild_changelog(base=base, branch=branch_text, main=main_text)
    except ChangelogRebuildError as exc:
        raise RepairStopError(exc.disposition, exc.detail) from exc
    return _Staged(
        rebuilt=result.text,
        main_text=main_text,
        arm_script=_probe_arm_script(prep.repo),
    )


def _disarm_before_push(job: _RepairJob, prep: _Prepared, state: _RepairState) -> None:
    """Step 13: re-check the head, then disarm an armed PR, fail closed."""
    head = _read_head(job, prep.branch, prep.expected_sha, prep.repo)
    state.evidence[_EVIDENCE_WAS_ARMED] = head.auto_merge_armed
    if not head.auto_merge_armed:
        return
    outcome = _ensure_unarmed(job.pr_url, cwd=prep.repo, head=head)
    if not outcome.ok:
        raise RepairStopError(
            ChangelogRepairDisposition.DISARM_FAILED,
            f"could not disarm auto-merge before the force-push; nothing pushed"
            f" ({outcome.detail})",
        )
    state.disarmed = True
    _logger.info(
        "changelog_repair_disarmed client=%s ticket=%s pr=%s expected_sha=%s phase=%s",
        job.client,
        job.ticket_id,
        prep.pr_number,
        prep.expected_sha,
        "pre_push",
    )


def _rewrite_and_push(
    job: _RepairJob,
    prep: _Prepared,
    staged: _Staged,
    scratch: Path,
    state: _RepairState,
) -> str:
    """Steps 9-14 in the scratch tree; returns the pushed head."""
    _rebase_onto(
        scratch, prep.default_ref, staged.main_text, prep.merge_base, prep.expected_sha
    )
    _commit_rebuilt(scratch, staged.rebuilt, job.ticket_id, prep.default_branch)
    new_sha = _head_sha(scratch)
    state.evidence[_EVIDENCE_NEW_SHA] = new_sha
    _verify_non_changelog_identical(
        scratch, prep.merge_base, prep.expected_sha, prep.default_ref, new_sha
    )
    _run_changelog_gate(scratch)
    _disarm_before_push(job, prep, state)
    _push_with_lease(scratch, prep.branch, prep.expected_sha)
    _LATCH.saturate(job.latch_key)
    state.pushed = True
    return new_sha


def _breadcrumbs(
    job: _RepairJob, branch: str, verdict: FenceVerdict, new_sha: str, expected_sha: str
) -> str:
    return (
        f"review_tip_check: stale ({verdict.detail}); repaired head {new_sha} pushed"
        f" over {expected_sha}; auto-merge is NOT armed (it was disarmed first if it"
        f" had been armed). Release: cw dev-queue requeue {job.ticket_id} -c"
        f" {job.client} --from-completed (re-runs finalize on the repaired head; if"
        " the ticket worktree still exists, first run git -C <worktree> fetch origin"
        f" and git -C <worktree> reset --hard origin/{branch}, because finalize will"
        " not reset over local commits origin lacks; if finalize parks again, the"
        " BLOCKED_ON_USER row it leaves is released by its own park command, e.g."
        " requeue --stage review)"
    )


def _emit_unarmed_attention(
    job: _RepairJob,
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
            _PAYLOAD_KEY_RECIPE: RECIPE_REPAIR_CHANGELOG_CONFLICT,
            _PAYLOAD_KEY_SESSION_ID: job.session_id,
            _PAUSED_STATUS_KEY: _UNARMED_ATTENTION_REASON,
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
    job: _RepairJob,
    prep: _Prepared,
    state: _RepairState,
    verdict: FenceVerdict,
    new_sha: str,
) -> str:
    """Step 17, fence failed: verify unarmed, then page; never arm."""
    outcome = _ensure_unarmed(job.pr_url, cwd=prep.repo)
    if not outcome.ok:
        raise RepairStopError(
            ChangelogRepairDisposition.DISARM_FAILED,
            f"PUSHED {prep.expected_sha} -> {new_sha}; auto-merge may still be ARMED"
            f" against an unreviewed head; run `gh pr merge {prep.pr_number}"
            f" --disable-auto` ({outcome.detail}; fence: {verdict.detail})",
        )
    if outcome.was_armed:
        _logger.info(
            "changelog_repair_disarmed client=%s ticket=%s pr=%s expected_sha=%s"
            " phase=%s",
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
        "changelog_repair_pushed_unarmed client=%s ticket=%s pr=%s expected_sha=%s"
        " new_sha=%s was_armed=%s review_tip_check=stale approval=%s detail=%s",
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
    job: _RepairJob,
    prep: _Prepared,
    staged: _Staged,
    scratch: Path,
    state: _RepairState,
    new_sha: str,
) -> str:
    """Step 17, fence passed: a fresh arm pinned to *new_sha*."""
    outcome = _run_arm(staged.arm_script, prep.pr_number, scratch, new_sha)
    if outcome.returncode == _ARM_DISALLOWED:
        _logger.info(
            "changelog_repair_arm_skipped client=%s ticket=%s pr=%s new_sha=%s"
            " reason=pr.auto_merge_false",
            job.client,
            job.ticket_id,
            prep.pr_number,
            new_sha,
        )
        arm_status = "skipped_config"
    elif outcome.returncode == _ARM_OK:
        arm_status = "merged" if outcome.status == _ARM_STATUS_MERGED else "armed"
    else:
        _ensure_unarmed(job.pr_url, cwd=prep.repo)
        raise RepairStopError(
            ChangelogRepairDisposition.REARM_FAILED,
            f"arm-automerge exit {outcome.returncode} after force-push"
            f" {prep.expected_sha} -> {new_sha}; PR repaired but NOT armed:"
            f" {outcome.stderr_tail}",
        )
    _logger.info(
        "changelog_repair_succeeded client=%s ticket=%s pr=%s branch=%s"
        " expected_sha=%s new_sha=%s was_armed=%s arm=%s",
        job.client,
        job.ticket_id,
        prep.pr_number,
        prep.branch,
        prep.expected_sha,
        new_sha,
        state.evidence.get(_EVIDENCE_WAS_ARMED),
        arm_status,
    )
    return job.ticket_id


def _finish_steps(
    job: _RepairJob,
    prep: _Prepared,
    staged: _Staged,
    scratch: Path,
    state: _RepairState,
    new_sha: str,
) -> str:
    """Steps 15-17: PR_ACTION_TAKEN, the fence, then arm or the unarmed page."""
    _record_pr_action_taken(
        state.payload,
        job.client,
        job.ticket_id,
        RECIPE_REPAIR_CHANGELOG_CONFLICT,
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
    job: _RepairJob,
    prep: _Prepared,
    staged: _Staged,
    scratch: Path,
    state: _RepairState,
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
            "changelog_repair_post_push_crashed client=%s ticket=%s",
            job.client,
            job.ticket_id,
        )
        unarm = _ensure_unarmed(job.pr_url, cwd=prep.repo)
        armed_note = (
            "auto-merge is verified unarmed"
            if unarm.ok
            else f"auto-merge may still be ARMED ({unarm.detail})"
        )
        detail = (
            f"PUSHED {prep.expected_sha} -> {new_sha}; the PR is repaired but the"
            f" fence or arm step crashed ({type(exc).__name__}: {exc}); {armed_note}"
        )
        raise RepairStopError(
            ChangelogRepairDisposition.POST_PUSH_FAILED, detail
        ) from exc


def _park(job: _RepairJob, state: _RepairState, stop: RepairStopError) -> None:
    """Emit the one PR_ACTION_FAILED for this attempt; saturate if deterministic."""
    if stop.disposition not in _TRANSIENT:
        _LATCH.saturate(job.latch_key)
    detail = stop.detail
    if stop.disposition == ChangelogRepairDisposition.PUSH_OUTCOME_UNKNOWN:
        detail = (
            f"{detail}; the push may have landed: compare origin/"
            f"{job.client_cfg.feature_branch_prefix}/{job.ticket_id} with"
            f" expected_sha {state.evidence.get(_EVIDENCE_EXPECTED_SHA)}"
        )
    if state.disarmed and not state.pushed:
        detail = f"{detail} (auto-merge was disarmed and is NOT re-armed)"
    _logger.warning(
        "changelog_repair_parked client=%s ticket=%s disposition=%s: %s",
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


def _dispatch_changelog_repair(job: _RepairJob) -> str | None:
    """Repair one PR post-lock; the acted ticket_id, or None when it parked.

    Owns the scratch tree's lifetime: the ``with`` spans steps 9-17, and the
    single ``RepairStopError`` handler sits outside it, so cleanup always runs
    before the failure event. Holds no lock.
    """
    state = _RepairState(job)
    try:
        prep = _prepare_repair(job, state)
        staged = _stage_rewrite(prep)
        scratch = scratch_path_for(resolve_worktree_base(job.client_cfg), job.ticket_id)
        with _scratch_worktree(prep.repo, scratch, prep.expected_sha) as tree:
            new_sha = _rewrite_and_push(job, prep, staged, tree, state)
            return _finish_repair(job, prep, staged, tree, state, new_sha)
    except RepairStopError as stop:
        _park(job, state, stop)
        return None
