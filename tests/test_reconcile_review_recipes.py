"""Tests for cw.reconcile.review_recipes (RFC 0010 P1+P2, GitHub #1096/#1097).

P1 is detect-only: the module classifies dev-queue rows whose PR came back
``changes_requested`` into ``ReviewRecipeCandidate``s. These tests exercise the
detect predicate, the ``_is_candidate`` gating borrowed from ``cw.pr_hydrate``,
and the dual master-switch gate.

P2 (#1097) adds the act phase: ``_act_address_review`` re-validates each
candidate under ``dev_queue_lock()``, emits ``PR_ACTION_TAKEN`` (durably,
before dispatch), and then — outside the lock — dispatches an
``/address-review`` session via ``spawn_create_impl``. A dispatch failure or a
precondition anomaly emits ``PR_ACTION_FAILED`` instead. The act-phase tests
below read the durable event store via ``read_events`` (NOT the ``capture_events``
monkeypatch — ``record_event`` is called from ``cw.reconcile.review_recipes``)
and stub the daemon spawn via the file-local ``stub_spawn`` fixture.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from freezegun import freeze_time
from pydantic import ValidationError

from cw.config import dev_queue_lock as _dev_queue_lock_path
from cw.config import (
    dispatch_loop_lock_file,
    load_effective_clients,
    sessions_lock,
    sessions_lock_file,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events, record_event
from cw.exceptions import (
    CwError,
    HookContextConflictError,
    RemoteRefUnresolvedError,
    SessionsLockReentryError,
    WorktreeOccupiedError,
)
from cw.models import (
    ClientConfig,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.pr_hydrate import PrAttentionState
from cw.reconcile import reconcile
from cw.reconcile.review_recipes import (
    _REPEAT_FIRE_ATTENTION_REASON as _REPEAT_FIRE_REASON,
)
from cw.reconcile.review_recipes import (
    RECIPE_ADDRESS_REVIEW,
    RECIPE_ATTENTION_STATES,
    RECIPE_AUTO_FIX_CI,
    RECIPE_ESCALATE_MERGE_BLOCK,
    RECIPE_FIRED_AT_GETTERS,
    RECIPE_REQUEST_REVIEWER,
    ReviewRecipeCandidate,
    _act_address_review,
    _act_auto_fix_ci,
    _act_escalate_merge_block,
    _act_request_reviewer,
    _detect_address_review,
    _detect_auto_fix_ci,
    _detect_escalate_merge_block,
    _detect_repeat_fire_counts,
    _detect_request_reviewer,
    _record_pr_action_taken,
    _shared,
    resolve_outbound_consent_allowed,
    resolve_review_recipe_enabled,
    run_review_recipes,
)
from cw.reconcile.review_recipes import (
    _detect_repeat_fire_counts as _real_detect_repeat_fire_counts,
)
from cw.review_strategy import ReviewStrategy
from cw.worktree import FetchOutcome, FetchResult, create_worktree, worktree_path_for

# Reuse the sibling test helpers rather than re-deriving TicketTask / PrState
# construction: _make_task accepts **kwargs (pr_url / pr_state / session_id /
# client / lane), _pr_state builds a PrState with sensible OPEN defaults.
# _client_with_lanes builds a ClientConfig with the given lanes (reused by the
# resolve-precedence tests below).
from tests._worktree_helpers import patch_worktree
from tests.conftest import (
    _clean_git_env,
    git_in,
    occupy_worktree,
    push_commit_to_origin,
    tree_fingerprint,
)
from tests.test_pr_hydrate import _pr_state, _watched
from tests.test_reconcile_gate_recipes import _client_with_lanes, _make_task

if typing.TYPE_CHECKING:
    from collections.abc import Callable

_SKILL_PATH = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "skills"
    / "address-review"
    / "SKILL.md"
)


def _config(**kwargs: Any) -> OrchestratorConfig:
    """OrchestratorConfig with the review-recipe master switch defaulted ON."""
    kwargs.setdefault("review_recipes_enabled", True)
    return OrchestratorConfig(**kwargs)


def _cr_task(**kwargs: Any) -> Any:
    """A changes_requested candidate task: pr_url + non-terminal PR state."""
    kwargs.setdefault("pr_url", "https://github.com/acme/widgets/pull/42")
    kwargs.setdefault(
        "pr_state", _pr_state(state="OPEN", attention_state="changes_requested")
    )
    return _make_task(**kwargs)


def _enabling_clients() -> dict[str, ClientConfig]:
    """Clients dict opting the default lane into the address-review recipe.

    The direct ``_detect_address_review`` call sites need per-lane enablement
    (RFC 0010 P3): with the default-off floor, a candidate only surfaces when a
    lane (or ticket) opts the recipe in. ``_make_task`` defaults to client
    ``acme`` on lane ``default``, so this dict resolves those tasks enabled.
    """
    return {
        "acme": _client_with_lanes(
            LaneConfig(name="default", review_recipes={RECIPE_ADDRESS_REVIEW: True})
        )
    }


@pytest.mark.parametrize(
    "attention_state",
    ["ci_failing", "merge_blocked", "no_reviewer", "ready_to_approve", None],
)
def test_detect_address_review_only_changes_requested_negative(
    attention_state: str | None,
) -> None:
    task = _make_task(
        pr_url="https://github.com/acme/widgets/pull/42",
        pr_state=_pr_state(state="OPEN", attention_state=attention_state),
    )
    assert (
        _detect_address_review([task], clients=_enabling_clients(), config=_config())
        == []
    )


def test_detect_address_review_only_changes_requested_positive() -> None:
    task = _cr_task()
    candidates = _detect_address_review(
        [task], clients=_enabling_clients(), config=_config()
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.recipe == RECIPE_ADDRESS_REVIEW
    assert candidate.attention_state == "changes_requested"
    assert candidate.pr_url == "https://github.com/acme/widgets/pull/42"
    assert candidate.ticket_id == task.ticket_id
    assert candidate.client == task.client
    assert candidate.lane == task.lane


def test_detect_address_review_requires_is_candidate() -> None:
    # No pr_url -> _is_candidate False -> not a candidate even if the state
    # would otherwise qualify.
    no_url = _make_task(
        pr_url=None,
        pr_state=_pr_state(state="OPEN", attention_state="changes_requested"),
    )
    assert (
        _detect_address_review([no_url], clients=_enabling_clients(), config=_config())
        == []
    )
    # Terminal PR state -> _is_candidate False regardless of attention_state.
    merged = _make_task(
        pr_url="https://github.com/acme/widgets/pull/42",
        pr_state=_pr_state(state="MERGED", attention_state="changes_requested"),
    )
    assert (
        _detect_address_review([merged], clients=_enabling_clients(), config=_config())
        == []
    )


def test_detect_address_review_pr_state_none_guard() -> None:
    # pr_url set but pr_state None: _is_candidate is True (hydratable), but the
    # detect phase requires an actual pr_state to read attention_state from.
    task = _make_task(pr_url="https://github.com/acme/widgets/pull/42", pr_state=None)
    assert (
        _detect_address_review([task], clients=_enabling_clients(), config=_config())
        == []
    )


def test_run_review_recipes_master_switch_off_is_noop() -> None:
    config = OrchestratorConfig()  # review_recipes_enabled defaults False
    assert config.review_recipes_enabled is False
    assert run_review_recipes(config=config) == []
    # Dual gating: _detect_address_review gates on the switch itself too, so a
    # direct call with the switch off returns [] even given a live candidate.
    assert (
        _detect_address_review([_cr_task()], clients=_enabling_clients(), config=config)
        == []
    )


def test_run_review_recipes_loads_from_dev_queue(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
) -> None:
    # Exercises the actual wiring core.py calls: run_review_recipes's own
    # detect → act path, not just the pure _detect_address_review helper.
    # Ticket-level override opts the recipe in (highest tier) so the candidate
    # surfaces; a resolvable client + a real worktree let the act phase dispatch.
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("run-review-recipes")
    task = _cr_task(
        review_recipes={RECIPE_ADDRESS_REVIEW: True}, worktree_path=worktree
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    acted = run_review_recipes(config=_config())

    # P2 now acts: run_review_recipes returns the acted ticket_ids.
    assert acted == [task.ticket_id]
    assert stub_spawn.calls[0]["prompt"] == "/address-review 42"
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    assert any(e.correlation_id == task.ticket_id for e in taken)
    # GitHub #1206: the row's address_review_fired_at latch is now stamped, so
    # the on-disk snapshot is unchanged EXCEPT for that field.
    after_task = load_dev_queue().tasks[0]
    assert after_task.address_review_fired_at is not None
    assert after_task.model_copy(update={"address_review_fired_at": None}) == task


def test_draft_pr_never_a_candidate() -> None:
    # Draft PRs derive attention_state None (Row 0), which never qualifies.
    task = _make_task(
        pr_url="https://github.com/acme/widgets/pull/42",
        pr_state=_pr_state(state="OPEN", attention_state=None),
    )
    assert (
        _detect_address_review([task], clients=_enabling_clients(), config=_config())
        == []
    )


def test_closed_pr_never_a_candidate() -> None:
    # Ported wiki lesson "Abandoned PR auto-completion" (session:94a665a5): a PR
    # closed on GitHub without merge is terminal, so _is_candidate is False and
    # no review recipe ever fires on it — even with a stale changes_requested
    # attention_state left on the row. cw's analogue of review_monitor auto-
    # completing an abandoned PR out of the monitored queue.
    task = _make_task(
        pr_url="https://github.com/acme/widgets/pull/42",
        pr_state=_pr_state(state="CLOSED", attention_state="changes_requested"),
    )
    assert (
        _detect_address_review([task], clients=_enabling_clients(), config=_config())
        == []
    )


def test_detect_address_review_surfaces_sessionless_candidate() -> None:
    task = _cr_task(session_id=None)
    candidates = _detect_address_review(
        [task], clients=_enabling_clients(), config=_config()
    )
    assert len(candidates) == 1
    assert candidates[0].session_id is None


def test_address_review_skill_file_exists() -> None:
    assert _SKILL_PATH.is_file()
    assert _SKILL_PATH.read_text(encoding="utf-8").strip() != ""


class TestResolveReviewRecipeEnabled:
    """3-tier precedence for resolve_review_recipe_enabled (RFC 0010 P3)."""

    def test_resolve_review_recipe_enabled_ticket_overrides_lane(self) -> None:
        """A ticket-level override beats an enabling lane map (True) and a
        disabling lane map (False), in both directions."""
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(name="default", review_recipes={RECIPE_ADDRESS_REVIEW: True})
            )
        }
        task_off = _make_task(review_recipes={RECIPE_ADDRESS_REVIEW: False})
        assert (
            resolve_review_recipe_enabled(task_off, clients, RECIPE_ADDRESS_REVIEW)
            is False
        )
        clients_off = {
            "acme": _client_with_lanes(
                LaneConfig(
                    name="default", review_recipes={RECIPE_ADDRESS_REVIEW: False}
                )
            )
        }
        task_on = _make_task(review_recipes={RECIPE_ADDRESS_REVIEW: True})
        assert (
            resolve_review_recipe_enabled(task_on, clients_off, RECIPE_ADDRESS_REVIEW)
            is True
        )

    def test_resolve_review_recipe_enabled_lane_overrides_default(self) -> None:
        """With no ticket override, the lane map wins over the default floor."""
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(name="default", review_recipes={RECIPE_ADDRESS_REVIEW: True})
            )
        }
        task = _make_task()  # no ticket-level override
        assert (
            resolve_review_recipe_enabled(task, clients, RECIPE_ADDRESS_REVIEW) is True
        )

    def test_resolve_review_recipe_enabled_default_off(self) -> None:
        """No override anywhere falls through to the hardcoded default-off floor."""
        task = _make_task()
        # No client at all.
        assert resolve_review_recipe_enabled(task, {}, RECIPE_ADDRESS_REVIEW) is False
        # Lane present but carries no review_recipes map -> default floor.
        clients = {"acme": _client_with_lanes(LaneConfig(name="default"))}
        assert (
            resolve_review_recipe_enabled(task, clients, RECIPE_ADDRESS_REVIEW) is False
        )

    def test_resolve_review_recipe_enabled_missing_client_or_lane_no_raise(
        self,
    ) -> None:
        """Missing client, missing lane, and an unrecognized recipe_name all
        fall through to the default with no exception."""
        # Client absent from the map.
        ghost = _make_task(client="ghost")
        assert resolve_review_recipe_enabled(ghost, {}, RECIPE_ADDRESS_REVIEW) is False
        # Client present but the task's lane is not among its lanes.
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(name="other", review_recipes={RECIPE_ADDRESS_REVIEW: True})
            )
        }
        assert (
            resolve_review_recipe_enabled(_make_task(), clients, RECIPE_ADDRESS_REVIEW)
            is False
        )
        # Unrecognized recipe_name -> .get fallback False, not KeyError.
        assert (
            resolve_review_recipe_enabled(_make_task(), {}, "nonexistent_recipe")
            is False
        )

    def test_unrecognized_review_recipe_key_rejected(self) -> None:
        """All four recipe names are accepted; an unrecognized key fails loud
        on both TicketTask and LaneConfig (RFC 0010 P4 extends the set)."""
        all_four = {
            RECIPE_ADDRESS_REVIEW: True,
            RECIPE_AUTO_FIX_CI: True,
            RECIPE_REQUEST_REVIEWER: False,
            RECIPE_ESCALATE_MERGE_BLOCK: True,
        }
        assert (
            TicketTask(
                ticket_id="X", client="acme", review_recipes=all_four
            ).review_recipes
            == all_four
        )
        assert (
            LaneConfig(name="default", review_recipes=all_four).review_recipes
            == all_four
        )
        with pytest.raises(ValidationError):
            TicketTask(ticket_id="X", client="acme", review_recipes={"bogus": True})
        with pytest.raises(ValidationError):
            LaneConfig(name="default", review_recipes={"bogus": True})


def test_config_reference_documents_review_recipes() -> None:
    """CONFIG_REFERENCE.md documents every review recipe + the strategy section."""
    doc = (
        Path(__file__).resolve().parent.parent / "config" / "CONFIG_REFERENCE.md"
    ).read_text(encoding="utf-8")
    assert "review_recipes" in doc
    assert "Review Recipe Enablement" in doc
    for recipe in (
        RECIPE_ADDRESS_REVIEW,
        RECIPE_AUTO_FIX_CI,
        RECIPE_REQUEST_REVIEWER,
        RECIPE_ESCALATE_MERGE_BLOCK,
    ):
        assert recipe in doc
    assert "Review Strategy" in doc


# --- RFC 0010 P2 act-phase (#1097) -----------------------------------------


class _SpawnRecorder:
    """Records spawn_create_impl calls and applies an optional side effect.

    File-local (only the P2 act-phase tests need it) — honours PYTHON-PATTERNS
    "never add features to a global fixture if only a subset needs them". The
    patch target is ``cw.spawn.spawn_create_impl`` because ``_act_address_review``
    imports it function-locally, resolving the name from ``cw.spawn`` at call
    time.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.side_effect: Any = None

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.side_effect is not None:
            self.side_effect(**kwargs)
        return "spawned-session-id"


@pytest.fixture
def stub_spawn(monkeypatch: pytest.MonkeyPatch) -> _SpawnRecorder:
    """Stub ``cw.spawn.spawn_create_impl`` with a recording fake."""
    recorder = _SpawnRecorder()
    monkeypatch.setattr("cw.spawn.spawn_create_impl", recorder)
    return recorder


def _write_acme_clients_yaml(tmp_config_dir: Path) -> None:
    """Write a minimal clients.yaml so ``load_effective_clients`` resolves acme.

    The act phase resolves ``clients.get(task.client)`` to build the spawn's
    ``ClientConfig``; without an on-disk entry the row would anomaly-skip with a
    "missing client" PR_ACTION_FAILED.
    """
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  acme:\n    workspace_path: {tmp_config_dir}\n"
        "    default_branch: main\n"
    )


def _candidate_for(task: TicketTask) -> ReviewRecipeCandidate:
    """Build the detect-phase candidate matching *task* (act tests skip detect)."""
    assert task.pr_url is not None
    assert task.pr_state is not None
    return ReviewRecipeCandidate(
        ticket_id=task.ticket_id,
        client=task.client,
        lane=task.lane,
        recipe=RECIPE_ADDRESS_REVIEW,
        attention_state="changes_requested",
        pr_url=task.pr_url,
        evidence={"review_decision": task.pr_state.review_decision},
        session_id=task.session_id,
    )


def test_pr_action_taken_emitted_before_mutation(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
) -> None:
    """PR_ACTION_TAKEN is durably recorded BEFORE spawn_create_impl runs.

    The stub, when invoked, reads the durable store and asserts the
    PR_ACTION_TAKEN for this ticket already exists — proving strict
    emit-before-dispatch ordering (the event fires inside the lock; the spawn
    strictly afterward, outside it).
    """
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("action-taken")
    task = _cr_task(
        worktree_path=worktree,
        pr_state=_pr_state(
            state="OPEN",
            attention_state="changes_requested",
            review_decision="CHANGES_REQUESTED",
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    def _assert_taken_recorded(**_kwargs: Any) -> None:
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert any(e.correlation_id == task.ticket_id for e in taken)

    stub_spawn.side_effect = _assert_taken_recorded

    acted = _act_address_review(
        [_candidate_for(task)], clients=load_effective_clients()
    )

    assert acted == [task.ticket_id]
    assert len(stub_spawn.calls) == 1
    call = stub_spawn.calls[0]
    assert call["prompt"] == "/address-review 42"
    assert call["headless"] is True
    assert call["label"] == "address-review-42"
    assert call["ticket_id"] == task.ticket_id
    assert call["lane"] == task.lane
    # P2 dispatches with NO dev-queue correlation (Resolution 6): no task kwarg.
    assert "task" not in call
    # The PR_ACTION_TAKEN payload carries the 8 keys off the re-loaded row.
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    payload = taken[-1].payload
    assert payload["ticket_id"] == task.ticket_id
    assert payload["client"] == "acme"
    assert payload["recipe"] == RECIPE_ADDRESS_REVIEW
    assert payload["pr_url"] == task.pr_url
    assert payload["attention_state"] == "changes_requested"
    # evidence_snapshot carries the exact field that licensed changes_requested.
    assert payload["evidence_snapshot"] == {"review_decision": "CHANGES_REQUESTED"}
    # No PR_ACTION_FAILED on the happy path.
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []
    # GitHub #1206: the row's address_review_fired_at latch is now stamped, so
    # the on-disk snapshot is unchanged EXCEPT for that field.
    after_task = load_dev_queue().tasks[0]
    assert after_task.address_review_fired_at is not None
    assert after_task.model_copy(update={"address_review_fired_at": None}) == task


def test_lane_in_needs_attention_payload_matches_task_lane(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
) -> None:
    """Integration (#1333): the ``lane`` threaded through ``_prepare_dispatch_job``
    into ``_record_pr_action_taken`` (representative call site, address_review
    recipe) survives end-to-end through ``_act_address_review``'s repeat-fire
    escalation into the SESSION_NEEDS_ATTENTION payload, and matches the
    triggering task's own ``lane`` — not the default."""
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("lane-integration")
    task = _cr_task(
        worktree_path=worktree,
        lane="bugs",
        pr_state=_pr_state(
            state="OPEN",
            attention_state="changes_requested",
            review_decision="CHANGES_REQUESTED",
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    cfg = _config(
        review_recipe_repeat_fire_threshold=1,
        review_recipe_repeat_fire_window_minutes=20,
    )

    acted = _act_address_review(
        [_candidate_for(task)],
        clients=load_effective_clients(),
        config=cfg,
        repeat_fire_counts={},
    )

    assert acted == [task.ticket_id]
    attn = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(attn) == 1
    assert attn[0].payload["lane"] == task.lane == "bugs"


@pytest.mark.parametrize("stale_state", ["ready_to_approve", None])
def test_stale_attention_state_skips_silently(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
    stale_state: str | None,
) -> None:
    """A re-loaded row no longer at changes_requested is a SILENT skip.

    The detect-time candidate said changes_requested, but the re-loaded row's
    pr_state has moved on (or vanished) — no spawn, no PR_ACTION_* event, [].
    """
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("stale")
    if stale_state is None:
        task = _make_task(
            pr_url="https://github.com/acme/widgets/pull/42",
            pr_state=None,
            worktree_path=worktree,
        )
    else:
        task = _make_task(
            pr_url="https://github.com/acme/widgets/pull/42",
            pr_state=_pr_state(state="OPEN", attention_state=stale_state),
            worktree_path=worktree,
        )
    save_dev_queue(DevQueueStore(tasks=[task]))
    # The candidate is stale — it claims changes_requested.
    candidate = ReviewRecipeCandidate(
        ticket_id=task.ticket_id,
        client=task.client,
        lane=task.lane,
        recipe=RECIPE_ADDRESS_REVIEW,
        attention_state="changes_requested",
        pr_url="https://github.com/acme/widgets/pull/42",
        evidence={"review_decision": "CHANGES_REQUESTED"},
        session_id=task.session_id,
    )

    assert _act_address_review([candidate], clients=load_effective_clients()) == []
    assert stub_spawn.calls == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


def test_no_self_deadlock_under_dev_queue_lock(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
) -> None:
    """The spawn runs OUTSIDE dev_queue_lock() — no self-deadlock.

    The stub takes a non-blocking probe lock (LOCK_EX | LOCK_NB) on the
    dev-queue lock file at spawn time. fcntl.flock locks are held per open
    file description, not per-process, so a still-held lock from the act
    phase would deny this second acquisition even from the same process. If
    the act phase ever re-entered dev_queue_lock() around the dispatch (a
    self-deadlock regression), the probe raises BlockingIOError instead of
    hanging — this repo has no pytest-timeout/CI job timeout, so a blocking
    re-acquire here would hang the whole CI job rather than fail fast.
    """
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("no-deadlock")
    task = _cr_task(worktree_path=worktree)
    save_dev_queue(DevQueueStore(tasks=[task]))

    def _probe_lock_released(**_kwargs: Any) -> None:
        fd = _dev_queue_lock_path().open("w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail(
                "dev_queue_lock() still held during dispatch — self-deadlock regression"
            )
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()

    stub_spawn.side_effect = _probe_lock_released

    assert _act_address_review(
        [_candidate_for(task)], clients=load_effective_clients()
    ) == [task.ticket_id]


def test_reconcile_reentry_guard_fires_and_is_swallowed(
    tmp_config_dir: Path,
    make_git_repo: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFC 0010 P4's act phase re-entering reconcile() raises, not hangs.

    Stands in for the real chain (GitHub #1228): reconcile() holds
    sessions_lock() -> ... -> run_review_recipes -> _act_auto_fix_ci ->
    _dispatch_auto_fix_ci -> run_dispatch_loop -> a nested reconcile() /
    sessions_lock() acquisition on the same thread. The outer
    ``with sessions_lock():`` below stands in for reconcile()'s own lock
    hold. Before the #1228 fix this scenario hangs forever in flock(); after
    the fix, SessionsLockReentryError propagates out of the inner
    reconcile() call, into _dispatch_auto_fix_ci's ``except CwError``, and is
    converted to a logged PR_ACTION_FAILED correction instead of a call-site
    change.
    """
    _write_acme_clients_yaml(tmp_config_dir)
    task = _cr_task(
        pr_state=_pr_state(
            state="OPEN", attention_state="ci_failing", failing_checks=["lint"]
        )
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    probed = {"lock_held": False}
    captured: list[BaseException] = []

    def _fake_dispatch_loop(**_kwargs: Any) -> None:
        # Non-blocking probe proves the outer sessions_lock() is genuinely
        # held (not just assumed) before exercising the real reentry path.
        fd = sessions_lock_file().open("w")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            probed["lock_held"] = True
        finally:
            fd.close()
        try:
            reconcile()
        except SessionsLockReentryError as exc:
            # Record the exact exception raised (not just "some CwError")
            # before letting it propagate into _dispatch_auto_fix_ci's
            # `except CwError` handler, so the outer assertions below can
            # confirm the guard — not some other failure — fired.
            captured.append(exc)
            raise

    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", _fake_dispatch_loop)

    with sessions_lock():
        acted = _act_auto_fix_ci(
            [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
            clients=load_effective_clients(),
        )

    assert acted == []
    assert probed["lock_held"] is True
    assert len(captured) == 1
    assert isinstance(captured[0], SessionsLockReentryError)
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id


def test_action_failure_emits_pr_action_failed(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A spawn CwError emits PR_ACTION_FAILED; a sibling candidate still fires.

    PR_ACTION_TAKEN is recorded (emit-before-dispatch), then the spawn raises,
    so PR_ACTION_FAILED is emitted with the error and correlation_id. The loop
    continues: a second candidate whose spawn succeeds still dispatches.
    """
    _write_acme_clients_yaml(tmp_config_dir)
    wt1 = make_git_repo("fail-1")
    wt2 = make_git_repo("fail-2")
    task1 = _cr_task(
        ticket_id="GEN-1",
        pr_url="https://github.com/acme/widgets/pull/42",
        worktree_path=wt1,
    )
    task2 = _cr_task(
        ticket_id="GEN-2",
        pr_url="https://github.com/acme/widgets/pull/99",
        worktree_path=wt2,
    )
    save_dev_queue(DevQueueStore(tasks=[task1, task2]))

    boom_msg = "boom"

    def _raise_for_gen1(**kwargs: Any) -> None:
        if kwargs["ticket_id"] == "GEN-1":
            raise CwError(boom_msg)

    stub_spawn.side_effect = _raise_for_gen1

    with caplog.at_level("WARNING"):
        acted = _act_address_review(
            [_candidate_for(task1), _candidate_for(task2)],
            clients=load_effective_clients(),
        )

    # GEN-2 still dispatched despite GEN-1 failing.
    assert acted == ["GEN-2"]
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    assert {e.correlation_id for e in taken} == {"GEN-1", "GEN-2"}
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == "GEN-1"
    assert failed[0].payload["ticket_id"] == "GEN-1"
    assert boom_msg in failed[0].payload["error"]
    assert any("dispatch_failed" in r.message for r in caplog.records)
    # GitHub #1206: both rows get address_review_fired_at stamped during the
    # prepare phase (which runs for both before either dispatch is attempted)
    # — including task1, whose dispatch subsequently failed. Narrowed
    # store-unchanged assertion: the on-disk snapshot is unchanged except for
    # that field, for both tasks.
    reloaded = {t.ticket_id: t for t in load_dev_queue().tasks}
    after_task1 = reloaded["GEN-1"]
    after_task2 = reloaded["GEN-2"]
    assert after_task1.address_review_fired_at is not None
    assert after_task2.address_review_fired_at is not None
    assert after_task1.model_copy(update={"address_review_fired_at": None}) == task1
    assert after_task2.model_copy(update={"address_review_fired_at": None}) == task2


def test_unparseable_pr_url_emits_pr_action_failed(
    tmp_config_dir: Path,
    stub_spawn: _SpawnRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unparseable pr_url anomaly emits PR_ACTION_FAILED (not silent)."""
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(
        pr_url="not-a-github-url",
        pr_state=_pr_state(state="OPEN", attention_state="changes_requested"),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    with caplog.at_level("WARNING"):
        assert (
            _act_address_review(
                [_candidate_for(task)], clients=load_effective_clients()
            )
            == []
        )

    assert stub_spawn.calls == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id


def test_missing_client_emits_pr_action_failed(
    tmp_config_dir: Path,
    stub_spawn: _SpawnRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A row whose client is unresolvable emits PR_ACTION_FAILED (not silent)."""
    _write_acme_clients_yaml(tmp_config_dir)  # defines acme, not ghost
    task = _make_task(
        client="ghost",
        pr_url="https://github.com/ghost/widgets/pull/42",
        pr_state=_pr_state(state="OPEN", attention_state="changes_requested"),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    with caplog.at_level("WARNING"):
        assert (
            _act_address_review(
                [_candidate_for(task)], clients=load_effective_clients()
            )
            == []
        )

    assert stub_spawn.calls == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].payload["client"] == "ghost"


def test_missing_worktree_emits_pr_action_failed(
    tmp_config_dir: Path,
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A row whose worktree_path does not exist emits PR_ACTION_FAILED."""
    _write_acme_clients_yaml(tmp_config_dir)
    missing = tmp_path / "never-created"
    task = _cr_task(worktree_path=missing)
    save_dev_queue(DevQueueStore(tasks=[task]))

    with caplog.at_level("WARNING"):
        assert (
            _act_address_review(
                [_candidate_for(task)], clients=load_effective_clients()
            )
            == []
        )

    assert stub_spawn.calls == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id


def test_address_review_fires_once_per_episode(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
) -> None:
    """The address_review latch blocks a re-dispatch within the same episode
    (GitHub #1206) — mirrors test_auto_fix_ci_fires_once_per_episode."""
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("address-review-fires-once")
    task = _cr_task(worktree_path=worktree)
    save_dev_queue(DevQueueStore(tasks=[task]))
    candidate = _candidate_for(task)

    acted1 = _act_address_review([candidate], clients=load_effective_clients())
    assert acted1 == [task.ticket_id]
    assert load_dev_queue().tasks[0].address_review_fired_at is not None

    # Hold the hydrated row at changes_requested across N further ticks
    # (simulating hydration lag): detect still yields a candidate every tick,
    # but the latch blocks a re-fire.
    for _ in range(5):
        acted_n = _act_address_review([candidate], clients=load_effective_clients())
        assert acted_n == []
    taken = [
        e
        for e in read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        if e.correlation_id == task.ticket_id
    ]
    assert len(taken) == 1
    assert len(stub_spawn.calls) == 1


def test_address_review_latch_clears_on_episode_end(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
) -> None:
    """The latch re-arms once pr_state leaves changes_requested (GitHub #1206)
    — mirrors test_auto_fix_ci_latch_clears_on_episode_end."""
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("address-review-clears-on-end")
    task = _cr_task(worktree_path=worktree)
    save_dev_queue(DevQueueStore(tasks=[task]))
    candidate = _candidate_for(task)

    assert _act_address_review([candidate], clients=load_effective_clients()) == [
        task.ticket_id
    ]

    # Episode ends: hydration moves the PR off changes_requested.
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(attention_state="ready_to_approve")
    save_dev_queue(store)

    # Clear pass runs even with zero candidates.
    assert _act_address_review([], clients=load_effective_clients()) == []
    assert load_dev_queue().tasks[0].address_review_fired_at is None

    # Genuine re-entry into changes_requested fires again (episode semantics).
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(
        state="OPEN", attention_state="changes_requested"
    )
    save_dev_queue(store)
    assert _act_address_review([candidate], clients=load_effective_clients()) == [
        task.ticket_id
    ]


# --- cross-repo dispatch guard, address_review (GitHub #1198) ---------------


def _set_origin(repo: Path, url: str) -> None:
    """Point *repo*'s ``origin`` remote at *url* (raw subprocess, no fixture)."""
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", url],
        capture_output=True,
        check=True,
    )


def test_repo_mismatch_emits_pr_action_failed(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
) -> None:
    """A worktree whose origin repo differs from the PR's repo skips + fails."""
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("repo-mismatch")
    _set_origin(worktree, "https://github.com/other/repo.git")
    task = _cr_task(worktree_path=worktree)  # pr_url -> acme/widgets
    save_dev_queue(DevQueueStore(tasks=[task]))

    acted = _act_address_review(
        [_candidate_for(task)], clients=load_effective_clients()
    )

    assert acted == []
    assert stub_spawn.calls == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id


def test_repo_match_dispatches_normally(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
) -> None:
    """A worktree whose origin repo matches the PR's repo dispatches normally."""
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("repo-match")
    _set_origin(worktree, "https://github.com/acme/widgets.git")
    task = _cr_task(worktree_path=worktree)
    save_dev_queue(DevQueueStore(tasks=[task]))

    acted = _act_address_review(
        [_candidate_for(task)], clients=load_effective_clients()
    )

    assert acted == [task.ticket_id]
    assert len(stub_spawn.calls) == 1
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


def test_repo_unresolvable_dispatches_normally(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
) -> None:
    """A worktree with no origin remote fails open (R5) -> dispatches normally."""
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("repo-unresolvable")  # no origin set
    task = _cr_task(worktree_path=worktree)
    save_dev_queue(DevQueueStore(tasks=[task]))

    acted = _act_address_review(
        [_candidate_for(task)], clients=load_effective_clients()
    )

    assert acted == [task.ticket_id]
    assert len(stub_spawn.calls) == 1
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


def test_repo_mismatch_override_dispatches_and_logs(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """cross_repo_override=True dispatches despite the mismatch and logs a WARN."""
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("repo-override")
    _set_origin(worktree, "https://github.com/other/repo.git")
    task = _cr_task(worktree_path=worktree, cross_repo_override=True)
    save_dev_queue(DevQueueStore(tasks=[task]))

    with caplog.at_level("WARNING"):
        acted = _act_address_review(
            [_candidate_for(task)], clients=load_effective_clients()
        )

    assert acted == [task.ticket_id]
    assert len(stub_spawn.calls) == 1
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []
    assert "review_recipe_repo_mismatch_override" in caplog.text
    assert task.ticket_id in caplog.text
    assert "pr_repo=acme/widgets" in caplog.text
    assert "client_repo=other/repo" in caplog.text


# --- RFC 0010 P4 recipes (#1099) -------------------------------------------


def _enabling_clients_for(*recipes: str) -> dict[str, ClientConfig]:
    """Clients dict opting the default lane into each named review recipe."""
    return {
        "acme": _client_with_lanes(
            LaneConfig(name="default", review_recipes=dict.fromkeys(recipes, True))
        )
    }


def _candidate(
    task: TicketTask, recipe: str, attention_state: str
) -> ReviewRecipeCandidate:
    assert task.pr_url is not None
    return ReviewRecipeCandidate(
        ticket_id=task.ticket_id,
        client=task.client,
        lane=task.lane,
        recipe=recipe,
        attention_state=attention_state,
        pr_url=task.pr_url,
        evidence={},
        session_id=task.session_id,
    )


_PR_URL = "https://github.com/acme/widgets/pull/42"


# --- auto_fix_ci -----------------------------------------------------------


def test_detect_auto_fix_ci_fires_on_ci_failing() -> None:
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="ci_failing"))
    cands = _detect_auto_fix_ci(
        [task], clients=_enabling_clients_for(RECIPE_AUTO_FIX_CI), config=_config()
    )
    assert len(cands) == 1
    assert cands[0].recipe == RECIPE_AUTO_FIX_CI
    assert cands[0].attention_state == "ci_failing"


def test_detect_auto_fix_ci_master_switch_off_returns_empty() -> None:
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="ci_failing"))
    off = OrchestratorConfig()  # review_recipes_enabled False
    assert (
        _detect_auto_fix_ci(
            [task], clients=_enabling_clients_for(RECIPE_AUTO_FIX_CI), config=off
        )
        == []
    )


def test_act_auto_fix_ci_requeues_completed_row_and_dispatches_once(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub #2100: a COMPLETED row is requeued in place, never a sibling add.

    This is the terminal_sibling park's exact root-cause scenario: a ticket
    whose real dev-queue row already reached COMPLETED goes ci_failing again
    (a post-merge PR, or hydration lag). The fix must mutate that same row
    (status -> PENDING, stage unchanged) rather than mint a second, PLAN-stage
    row alongside it.
    """
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(
        status=QueueItemStatus.COMPLETED,
        pr_url=_PR_URL,
        pr_state=_pr_state(
            attention_state="ci_failing", failing_checks=["lint", "mypy"]
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    dispatched: list[dict[str, Any]] = []

    def _fake_dispatch(**kwargs: Any) -> None:
        # emit-before-dispatch: PR_ACTION_TAKEN is durable before the requeue
        # + tick run.
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert any(e.correlation_id == task.ticket_id for e in taken)
        dispatched.append(kwargs)

    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", _fake_dispatch)

    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert acted == [task.ticket_id]
    assert dispatched == [{"once": True, "client": task.client, "emit": None}]
    # Never mints a sibling row (#2100) -- exactly one row survives, requeued
    # in place at its original stage.
    store_after = load_dev_queue()
    assert len(store_after.tasks) == 1
    after_task = store_after.tasks[0]
    assert after_task.ticket_id == task.ticket_id
    assert after_task.status == QueueItemStatus.PENDING
    assert after_task.stage == task.stage
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    # auto_fix_ci's evidence is the failing checks, not the (meaningless-here)
    # review_decision field the address_review recipe uses.
    assert taken[-1].payload["evidence_snapshot"] == {
        "failing_checks": ["lint", "mypy"]
    }
    # #2100 provenance: the row's status at fire time lands in the durably
    # recorded PR_ACTION_TAKEN payload.
    assert taken[-1].payload["queue_row_status"] == "completed"
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


def test_act_auto_fix_ci_existing_blocked_row_noop_dispatches_once(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub #2100: a row that already owns the ticket is left alone.

    BLOCKED_ON_USER (like PENDING/RUNNING/AWAITING_OPERATOR_SIGNOFF) already
    occupies the ticket -- auto_fix_ci must not add a sibling OR requeue it;
    only the follow-up dispatch tick runs.
    """
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(
        pr_url=_PR_URL,
        pr_state=_pr_state(attention_state="ci_failing", failing_checks=["lint"]),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    store_before = load_dev_queue()
    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "cw.dispatch.run_dispatch_loop", lambda **kw: dispatched.append(kw)
    )

    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert acted == [task.ticket_id]
    assert dispatched == [{"once": True, "client": task.client, "emit": None}]
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    assert taken[-1].payload["queue_row_status"] == "blocked_on_user"
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []
    # The latch is the ONLY mutation this act phase makes -- no sibling row,
    # no status change on the already-parked row.
    store_after = load_dev_queue()
    assert len(store_after.tasks) == 1
    after_task = store_after.tasks[0]
    assert (
        after_task.model_copy(update={"auto_fix_ci_fired_at": None})
        == store_before.tasks[0]
    )


def test_act_auto_fix_ci_dispatch_loop_locked_elsewhere_fails_open(
    tmp_config_dir: Path,
) -> None:
    """A genuinely EXTERNAL dispatch-loop lock holder degrades gracefully (#1362).

    Uses the REAL ``dispatch_loop_lock`` file, held via a raw fd exactly as a
    second ``cw`` process would (fcntl.flock is per-open-file-description, so
    this denies acquisition even from this same test process). Proves
    ``_dispatch_auto_fix_ci`` does NOT bypass a genuinely-held external lock
    (there is no ``force=True`` at this call site) -- it fails open via a
    ``DispatchLoopLockedError``-specific ``PR_ACTION_FAILED`` (GitHub #2100),
    a sibling posture to the ``except CwError`` path already used for the
    analogous ``SessionsLockReentryError`` case (GitHub #1228). The row was
    already requeued in place (``requeue_ticket`` ran, for real, before the
    tick); only the "trigger a tick right now" nicety is lost, deferred to
    whichever process holds the lock on its own next regular tick.
    """
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(
        status=QueueItemStatus.COMPLETED,
        pr_url=_PR_URL,
        pr_state=_pr_state(attention_state="ci_failing", failing_checks=["lint"]),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    lock_path = dispatch_loop_lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    fd = lock_path.open("r+")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        acted = _act_auto_fix_ci(
            [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
            clients=load_effective_clients(),
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()

    assert acted == []
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    matching = [e for e in failed if e.correlation_id == task.ticket_id]
    assert len(matching) == 1
    # Pin the failure to the lock-contention path specifically -- not just
    # "some CwError" -- mirroring test_reconcile_reentry_guard_fires_and_is_swallowed's
    # exact-exception check for the analogous #1228 SessionsLockReentryError case.
    # The message explicitly says the row is already requeued/current (#2100)
    # rather than the stale "already re-enqueued" claim.
    error_text = matching[0].payload["error"]
    assert "dispatch loop already running" in error_text
    assert "already requeued/current" in error_text
    # The row itself WAS requeued to PENDING despite the tick failing -- the
    # mutation and the tick are independent failure domains (#2100). Never
    # mints a sibling: exactly one row survives.
    store_after = load_dev_queue()
    assert len(store_after.tasks) == 1
    after_task = store_after.tasks[0]
    assert after_task.status == QueueItemStatus.PENDING
    # Latch stays stamped even on dispatch failure (no retry storm).
    assert after_task.auto_fix_ci_fired_at is not None


def test_act_auto_fix_ci_stale_row_silent_skip(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    # Re-loaded row is no longer ci_failing -> silent skip.
    task = _make_task(
        pr_url=_PR_URL, pr_state=_pr_state(attention_state="ready_to_approve")
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    called: list[Any] = []
    monkeypatch.setattr("cw.dev_queue.add_ticket", lambda t: called.append(t) or True)
    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", lambda **_kw: called.append(1))

    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert acted == []
    assert called == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


def test_act_auto_fix_ci_requeue_raises_emits_pr_action_failed(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GitHub #2100: a requeue_ticket failure corrects via PR_ACTION_FAILED.

    Mirrors the old add_ticket-raises coverage, updated for the row-mutation
    call this recipe now makes for a terminal row.
    """
    from cw.exceptions import RequeueStateError

    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(
        status=QueueItemStatus.COMPLETED,
        pr_url=_PR_URL,
        pr_state=_pr_state(attention_state="ci_failing"),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    row_gone_msg = "row moved on"

    def _boom(*_args: object, **_kwargs: object) -> dict[str, str | bool | int]:
        raise RequeueStateError(row_gone_msg)

    monkeypatch.setattr("cw.dev_queue.requeue_ticket", _boom)
    monkeypatch.setattr(
        "cw.dispatch.run_dispatch_loop",
        lambda **_kw: pytest.fail("dispatch must not run when requeue_ticket raises"),
    )

    with caplog.at_level("WARNING"):
        acted = _act_auto_fix_ci(
            [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
            clients=load_effective_clients(),
        )

    assert acted == []
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    assert any(e.correlation_id == task.ticket_id for e in taken)
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id
    assert "row moved on" in failed[0].payload["error"]
    # No sibling minted despite the failure -- the row is untouched, still
    # COMPLETED (the mocked requeue_ticket never actually mutated it).
    store_after = load_dev_queue()
    assert len(store_after.tasks) == 1
    assert store_after.tasks[0].status == QueueItemStatus.COMPLETED


def _raise_live_session(
    *_args: object, **_kwargs: object
) -> dict[str, str | bool | int]:
    from cw.exceptions import RequeueLiveSessionError

    msg = "Cannot requeue: a session for this ticket is live: 'stray1'"
    raise RequeueLiveSessionError(msg, session_ids=("stray1",))


def _seed_ci_failing_completed_row(tmp_config_dir: Path) -> TicketTask:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(
        status=QueueItemStatus.COMPLETED,
        pr_url=_PR_URL,
        pr_state=_pr_state(attention_state="ci_failing"),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    return task


def test_auto_fix_ci_live_session_refusal_clears_latch(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2275: a live-session refusal is transient, so the one-shot latch is
    rolled back and PR_ACTION_FAILED carries a distinguishable reason."""
    task = _seed_ci_failing_completed_row(tmp_config_dir)
    monkeypatch.setattr("cw.dev_queue.requeue_ticket", _raise_live_session)
    monkeypatch.setattr(
        "cw.dispatch.run_dispatch_loop",
        lambda **_kw: pytest.fail("dispatch must not run when requeue is refused"),
    )

    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert acted == []
    assert load_dev_queue().tasks[0].auto_fix_ci_fired_at is None
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].payload["redispatch_mode"] == "skipped_live_session"
    assert failed[0].payload["live_session_ids"] == ["stray1"]
    assert "stray1" in failed[0].payload["error"]


def test_auto_fix_ci_roster_unreadable_refusal_clears_latch(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2275 review round 1: an unreadable roster refuses through the real
    requeue_ticket guard, takes the same latch rollback as a live-session
    refusal, and tags PR_ACTION_FAILED with a distinct reason."""
    from cw.native_daemon import FakeNativeDaemonClient

    task = _seed_ci_failing_completed_row(tmp_config_dir)
    roster = FakeNativeDaemonClient()
    roster.roster_unreadable = True
    monkeypatch.setattr("cw.dev_queue.requeue.get_native_daemon_client", lambda: roster)
    monkeypatch.setattr(
        "cw.dispatch.run_dispatch_loop",
        lambda **_kw: pytest.fail("dispatch must not run when requeue is refused"),
    )

    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert acted == []
    row = load_dev_queue().tasks[0]
    assert row.auto_fix_ci_fired_at is None
    assert row.status == QueueItemStatus.COMPLETED
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].payload["redispatch_mode"] == "skipped_roster_unreadable"
    assert failed[0].payload["live_session_ids"] == []
    assert (
        f"daemon roster unreadable at {roster.roster_path};"
        f" cannot rule out a live session for #{task.ticket_id}"
    ) in failed[0].payload["error"]


def test_auto_fix_ci_live_session_refusal_keeps_concurrently_changed_latch(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2275: the rollback is compare-and-clear -- a latch another actor
    re-stamped between the two lock transactions is left untouched."""
    task = _seed_ci_failing_completed_row(tmp_config_dir)
    other_stamp = datetime(2031, 1, 1, tzinfo=UTC)

    def _race_then_refuse(
        *args: object, **kwargs: object
    ) -> dict[str, str | bool | int]:
        store = load_dev_queue()
        store.tasks[0].auto_fix_ci_fired_at = other_stamp
        save_dev_queue(store)
        return _raise_live_session(*args, **kwargs)

    monkeypatch.setattr("cw.dev_queue.requeue_ticket", _race_then_refuse)
    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", lambda **_kw: None)

    _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert load_dev_queue().tasks[0].auto_fix_ci_fired_at == other_stamp


def test_auto_fix_ci_live_session_refusal_row_vanished_is_noop(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2275: a row removed between the two transactions is not resurrected;
    PR_ACTION_FAILED is still emitted."""
    task = _seed_ci_failing_completed_row(tmp_config_dir)

    def _remove_then_refuse(
        *args: object, **kwargs: object
    ) -> dict[str, str | bool | int]:
        save_dev_queue(DevQueueStore(tasks=[]))
        return _raise_live_session(*args, **kwargs)

    monkeypatch.setattr("cw.dev_queue.requeue_ticket", _remove_then_refuse)
    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", lambda **_kw: None)

    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert acted == []
    assert load_dev_queue().tasks == []
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1


def test_auto_fix_ci_fires_again_after_live_session_latch_cleared(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2275: the cleared latch genuinely re-arms the row for the next tick."""
    task = _seed_ci_failing_completed_row(tmp_config_dir)
    candidate = _candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")
    monkeypatch.setattr("cw.dev_queue.requeue_ticket", _raise_live_session)
    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", lambda **_kw: None)

    first = _act_auto_fix_ci([candidate], clients=load_effective_clients())
    assert first == []

    requeued: list[str] = []

    def _requeue_ok(ticket_id: str, *_a: object, **_kw: object) -> dict[str, object]:
        requeued.append(ticket_id)
        return {"from_completed_applied": True}

    monkeypatch.setattr("cw.dev_queue.requeue_ticket", _requeue_ok)

    second = _act_auto_fix_ci([candidate], clients=load_effective_clients())

    assert second == [task.ticket_id]
    assert requeued == [task.ticket_id]
    assert load_dev_queue().tasks[0].auto_fix_ci_fired_at is not None


def test_auto_fix_ci_non_live_session_cwerror_still_latches(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2275 non-interference: any other CwError keeps the latch stamped."""
    from cw.exceptions import RequeueStateError

    task = _seed_ci_failing_completed_row(tmp_config_dir)
    row_gone_msg = "row moved on"

    def _boom(*_args: object, **_kwargs: object) -> dict[str, str | bool | int]:
        raise RequeueStateError(row_gone_msg)

    monkeypatch.setattr("cw.dev_queue.requeue_ticket", _boom)
    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", lambda **_kw: None)

    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert acted == []
    assert load_dev_queue().tasks[0].auto_fix_ci_fired_at is not None
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert "redispatch_mode" not in failed[0].payload
    assert "live_session_ids" not in failed[0].payload


def test_auto_fix_ci_fires_once_per_episode(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="ci_failing"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", lambda **_kw: None)
    candidate = _candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")

    acted1 = _act_auto_fix_ci([candidate], clients=load_effective_clients())
    assert acted1 == [task.ticket_id]
    assert load_dev_queue().tasks[0].auto_fix_ci_fired_at is not None

    # Hold the hydrated row at ci_failing across N further ticks (simulating
    # hydration lag, per the ticket's acceptance criterion): detect still
    # yields a candidate every tick, but the latch blocks a re-fire.
    for _ in range(5):
        acted_n = _act_auto_fix_ci([candidate], clients=load_effective_clients())
        assert acted_n == []
    taken = [
        e
        for e in read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        if e.correlation_id == task.ticket_id
    ]
    assert len(taken) == 1


def test_auto_fix_ci_latch_clears_on_episode_end(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="ci_failing"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", lambda **_kw: None)
    candidate = _candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")

    assert _act_auto_fix_ci([candidate], clients=load_effective_clients()) == [
        task.ticket_id
    ]

    # Episode ends: hydration moves the PR off ci_failing.
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(attention_state="ready_to_approve")
    save_dev_queue(store)

    # Clear pass runs even with zero candidates.
    assert _act_auto_fix_ci([], clients=load_effective_clients()) == []
    assert load_dev_queue().tasks[0].auto_fix_ci_fired_at is None

    # Genuine re-entry into ci_failing fires again (episode semantics).
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(attention_state="ci_failing")
    save_dev_queue(store)
    assert _act_auto_fix_ci([candidate], clients=load_effective_clients()) == [
        task.ticket_id
    ]


# --- cross-repo dispatch guard, auto_fix_ci (GitHub #1198) ------------------


def _write_acme_clients_yaml_with_repo(
    tmp_config_dir: Path, make_git_repo: Any, remote_url: str
) -> Path:
    """clients.yaml whose acme workspace_path is a git repo with *remote_url*.

    Unlike ``_write_acme_clients_yaml`` (which points acme at a non-git dir),
    the auto_fix_ci guard resolves the client's ``workspace_path`` origin remote,
    so it needs a real repo with a settable origin.
    """
    repo = make_git_repo("acme-ws")
    _set_origin(repo, remote_url)
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  acme:\n    workspace_path: {repo}\n    default_branch: main\n"
    )
    return repo


def test_auto_fix_ci_repo_mismatch_emits_pr_action_failed(
    tmp_config_dir: Path, make_git_repo: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Client workspace repo != PR repo -> skip + PR_ACTION_FAILED, no dispatch."""
    _write_acme_clients_yaml_with_repo(
        tmp_config_dir, make_git_repo, "https://github.com/other/repo.git"
    )
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="ci_failing"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    monkeypatch.setattr(
        "cw.dev_queue.add_ticket",
        lambda _t: pytest.fail("no re-dispatch on repo mismatch"),
    )
    monkeypatch.setattr(
        "cw.dispatch.run_dispatch_loop",
        lambda **_kw: pytest.fail("no dispatch on repo mismatch"),
    )

    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert acted == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id


def test_auto_fix_ci_repo_match_dispatches_normally(
    tmp_config_dir: Path, make_git_repo: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Client workspace repo == PR repo -> re-dispatch proceeds as today."""
    _write_acme_clients_yaml_with_repo(
        tmp_config_dir, make_git_repo, "https://github.com/acme/widgets.git"
    )
    task = _make_task(
        pr_url=_PR_URL,
        pr_state=_pr_state(attention_state="ci_failing", failing_checks=["lint"]),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "cw.dispatch.run_dispatch_loop", lambda **kw: dispatched.append(kw)
    )

    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert acted == [task.ticket_id]
    assert len(dispatched) == 1
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []
    # No sibling minted (#2100) -- the (default BLOCKED_ON_USER) row already
    # owns the ticket, so the guard's success path is a no-op beyond the tick.
    assert len(load_dev_queue().tasks) == 1


def test_auto_fix_ci_repo_mismatch_override_dispatches_and_logs(
    tmp_config_dir: Path,
    make_git_repo: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """cross_repo_override=True re-dispatches despite the mismatch and logs WARN."""
    _write_acme_clients_yaml_with_repo(
        tmp_config_dir, make_git_repo, "https://github.com/other/repo.git"
    )
    task = _make_task(
        pr_url=_PR_URL,
        pr_state=_pr_state(attention_state="ci_failing"),
        cross_repo_override=True,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "cw.dispatch.run_dispatch_loop", lambda **kw: dispatched.append(kw)
    )

    with caplog.at_level("WARNING"):
        acted = _act_auto_fix_ci(
            [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
            clients=load_effective_clients(),
        )

    assert acted == [task.ticket_id]
    assert len(dispatched) == 1
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []
    assert "review_recipe_repo_mismatch_override" in caplog.text
    assert task.ticket_id in caplog.text
    assert "pr_repo=acme/widgets" in caplog.text
    assert "client_repo=other/repo" in caplog.text


def test_auto_fix_ci_unparseable_pr_url_fails_open(
    tmp_config_dir: Path, make_git_repo: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unparseable pr_url fails open (no mismatch) -> re-dispatch proceeds."""
    _write_acme_clients_yaml_with_repo(
        tmp_config_dir, make_git_repo, "https://github.com/other/repo.git"
    )
    task = _make_task(
        pr_url="https://example.com/not-a-pr",
        pr_state=_pr_state(attention_state="ci_failing"),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "cw.dispatch.run_dispatch_loop", lambda **kw: dispatched.append(kw)
    )

    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")],
        clients=load_effective_clients(),
    )

    assert acted == [task.ticket_id]
    assert len(dispatched) == 1
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


def test_auto_fix_ci_unresolvable_client_fails_open(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client absent from the clients dict fails open -> re-dispatch proceeds."""
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="ci_failing"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "cw.dispatch.run_dispatch_loop", lambda **kw: dispatched.append(kw)
    )

    # clients={} -> client_cfg is None -> guard fails open, dispatch proceeds.
    acted = _act_auto_fix_ci(
        [_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")], clients={}
    )

    assert acted == [task.ticket_id]
    assert len(dispatched) == 1
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


# --- request_reviewer ------------------------------------------------------


def test_detect_request_reviewer_fires_on_no_reviewer() -> None:
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="no_reviewer"))
    cands = _detect_request_reviewer(
        [task],
        clients=_enabling_clients_for(RECIPE_REQUEST_REVIEWER),
        config=_config(),
    )
    assert len(cands) == 1
    assert cands[0].recipe == RECIPE_REQUEST_REVIEWER
    assert cands[0].attention_state == "no_reviewer"


def _stub_strategy(monkeypatch: pytest.MonkeyPatch, strategy: ReviewStrategy) -> None:
    monkeypatch.setattr(
        "cw.reconcile.review_recipes.request_reviewer.resolve_review_strategy",
        lambda _root: strategy,
    )


def test_act_request_reviewer_ci_mode_silent_skip(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="no_reviewer"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    _stub_strategy(monkeypatch, ReviewStrategy("ci", None))
    calls: list[Any] = []
    monkeypatch.setattr("cw.gh.add_pr_reviewer", lambda *a, **kw: calls.append((a, kw)))

    acted = _act_request_reviewer(
        [_candidate(task, RECIPE_REQUEST_REVIEWER, "no_reviewer")],
        clients=load_effective_clients(),
    )

    assert acted == []
    assert calls == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


@pytest.mark.parametrize(
    ("mode", "handle"),
    [("repo_owner", "alice"), ("reviewer_team", "acme/reviewers")],
)
def test_act_request_reviewer_configured_mode_calls_gh_helper(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    handle: str,
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="no_reviewer"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    _stub_strategy(monkeypatch, ReviewStrategy(mode, handle))  # type: ignore[arg-type]
    calls: list[tuple[str, str]] = []
    store_before = load_dev_queue()

    def _fake_add(
        pr_ref: str, reviewer: str, **_kw: Any
    ) -> subprocess.CompletedProcess[bytes]:
        # emit-before-action: PR_ACTION_TAKEN is durable before the gh call.
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert any(e.correlation_id == task.ticket_id for e in taken)
        calls.append((pr_ref, reviewer))
        return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr("cw.gh.add_pr_reviewer", _fake_add)

    acted = _act_request_reviewer(
        [_candidate(task, RECIPE_REQUEST_REVIEWER, "no_reviewer")],
        clients=load_effective_clients(),
    )

    assert acted == [task.ticket_id]
    assert calls == [(_PR_URL, handle)]
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    payload = taken[-1].payload
    assert payload["review_strategy_mode"] == mode
    assert payload["reviewer_handle"] == handle
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []
    # request_reviewer's only dev-queue mutation is the one-shot
    # request_reviewer_fired_at latch (GitHub #1197); everything else on the
    # row must stay byte-for-byte unchanged.
    after_task = load_dev_queue().tasks[0]
    assert after_task.request_reviewer_fired_at is not None
    assert (
        after_task.model_copy(update={"request_reviewer_fired_at": None})
        == store_before.tasks[0]
    )


def test_act_request_reviewer_scopes_gh_call_to_client_cwd(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1279: the deferred add_pr_reviewer gh call is cwd-scoped to the
    client's repo (via _ReviewerJob.cwd), not the ambient CWD."""
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="no_reviewer"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    _stub_strategy(monkeypatch, ReviewStrategy("repo_owner", "alice"))
    cwds: list[object] = []

    def _fake_add(
        _pr_ref: str, _reviewer: str, **kw: Any
    ) -> subprocess.CompletedProcess[bytes]:
        cwds.append(kw.get("cwd"))
        return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr("cw.gh.add_pr_reviewer", _fake_add)

    acted = _act_request_reviewer(
        [_candidate(task, RECIPE_REQUEST_REVIEWER, "no_reviewer")],
        clients=load_effective_clients(),
    )

    assert acted == [task.ticket_id]
    # _git_dir(acme) == workspace_path == tmp_config_dir (no repo_path set).
    assert cwds == [tmp_config_dir]


def test_act_request_reviewer_gh_call_fails_emits_failed(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="no_reviewer"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    _stub_strategy(monkeypatch, ReviewStrategy("repo_owner", "alice"))
    monkeypatch.setattr(
        "cw.gh.add_pr_reviewer",
        lambda *_a, **_kw: subprocess.CompletedProcess(
            args=[], returncode=1, stderr=b"permission denied"
        ),
    )

    acted = _act_request_reviewer(
        [_candidate(task, RECIPE_REQUEST_REVIEWER, "no_reviewer")],
        clients=load_effective_clients(),
    )

    assert acted == []
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    assert len(taken) == 1  # optimistic PR_ACTION_TAKEN still recorded pre-call
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id
    assert "permission denied" in failed[0].payload["error"]


def test_act_request_reviewer_gh_call_errors_emits_failed(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``add_pr_reviewer`` returns None on a subprocess error/timeout — a
    distinct failure shape from a non-zero returncode, exercised separately."""
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="no_reviewer"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    _stub_strategy(monkeypatch, ReviewStrategy("repo_owner", "alice"))
    monkeypatch.setattr("cw.gh.add_pr_reviewer", lambda *_a, **_kw: None)

    acted = _act_request_reviewer(
        [_candidate(task, RECIPE_REQUEST_REVIEWER, "no_reviewer")],
        clients=load_effective_clients(),
    )

    assert acted == []
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    assert len(taken) == 1  # optimistic PR_ACTION_TAKEN still recorded pre-call
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id
    assert "gh call failed" in failed[0].payload["error"]


def test_act_request_reviewer_misconfigured_mode_missing_handle_emits_failed(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="no_reviewer"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    _stub_strategy(monkeypatch, ReviewStrategy("repo_owner", None))
    monkeypatch.setattr(
        "cw.gh.add_pr_reviewer",
        lambda *_a, **_kw: pytest.fail(
            "gh must not be called for a misconfigured mode"
        ),
    )

    with caplog.at_level("WARNING"):
        acted = _act_request_reviewer(
            [_candidate(task, RECIPE_REQUEST_REVIEWER, "no_reviewer")],
            clients=load_effective_clients(),
        )

    assert acted == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id


def test_request_reviewer_fires_once_per_episode(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="no_reviewer"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    _stub_strategy(monkeypatch, ReviewStrategy("repo_owner", "alice"))
    monkeypatch.setattr(
        "cw.gh.add_pr_reviewer",
        lambda *_a, **_kw: subprocess.CompletedProcess(args=[], returncode=0),
    )
    clients = load_effective_clients()
    candidate = _candidate(task, RECIPE_REQUEST_REVIEWER, "no_reviewer")

    acted1 = _act_request_reviewer([candidate], clients=clients)
    assert acted1 == [task.ticket_id]
    assert load_dev_queue().tasks[0].request_reviewer_fired_at is not None

    # Hold the hydrated row at reviewer_count == 0 across N further ticks
    # (simulating hydration lag, per the ticket's acceptance criterion): detect
    # still yields a candidate every tick, but the latch blocks a re-fire.
    for _ in range(5):
        acted_n = _act_request_reviewer([candidate], clients=clients)
        assert acted_n == []
    taken = [
        e
        for e in read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        if e.correlation_id == task.ticket_id
    ]
    assert len(taken) == 1


def test_request_reviewer_latch_clears_on_episode_end(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="no_reviewer"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    _stub_strategy(monkeypatch, ReviewStrategy("repo_owner", "alice"))
    monkeypatch.setattr(
        "cw.gh.add_pr_reviewer",
        lambda *_a, **_kw: subprocess.CompletedProcess(args=[], returncode=0),
    )
    clients = load_effective_clients()
    candidate = _candidate(task, RECIPE_REQUEST_REVIEWER, "no_reviewer")

    assert _act_request_reviewer([candidate], clients=clients) == [task.ticket_id]

    # Episode ends: hydration moves the PR off no_reviewer.
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(attention_state="ready_to_approve")
    save_dev_queue(store)

    # Clear pass runs even with zero candidates.
    assert _act_request_reviewer([], clients=clients) == []
    assert load_dev_queue().tasks[0].request_reviewer_fired_at is None

    # Genuine re-entry into no_reviewer fires again (episode semantics).
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(attention_state="no_reviewer")
    save_dev_queue(store)
    assert _act_request_reviewer([candidate], clients=clients) == [task.ticket_id]


# --- escalate_merge_block --------------------------------------------------


def test_detect_escalate_merge_block_fires_on_merge_blocked() -> None:
    task = _make_task(
        pr_url=_PR_URL, pr_state=_pr_state(attention_state="merge_blocked")
    )
    cands = _detect_escalate_merge_block(
        [task],
        clients=_enabling_clients_for(RECIPE_ESCALATE_MERGE_BLOCK),
        config=_config(),
    )
    assert len(cands) == 1
    assert cands[0].recipe == RECIPE_ESCALATE_MERGE_BLOCK
    assert cands[0].attention_state == "merge_blocked"


def _detect_escalate(clients: dict[str, ClientConfig]) -> list[ReviewRecipeCandidate]:
    return _detect_escalate_merge_block(
        load_dev_queue().tasks, clients=clients, config=_config()
    )


def test_escalate_merge_block_fires_once_per_episode(tmp_config_dir: Path) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(
        pr_url=_PR_URL, pr_state=_pr_state(attention_state="merge_blocked")
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    clients = _enabling_clients_for(RECIPE_ESCALATE_MERGE_BLOCK)

    acted1 = _act_escalate_merge_block(_detect_escalate(clients))
    assert acted1 == [task.ticket_id]
    assert load_dev_queue().tasks[0].escalate_merge_block_fired_at is not None

    # Second tick, state unchanged: detect still yields a candidate, but the
    # latch blocks a re-fire.
    acted2 = _act_escalate_merge_block(_detect_escalate(clients))
    assert acted2 == []
    taken = [
        e
        for e in read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        if e.correlation_id == task.ticket_id
    ]
    assert len(taken) == 1


def test_escalate_merge_block_latch_clears_on_episode_end(
    tmp_config_dir: Path,
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(
        pr_url=_PR_URL, pr_state=_pr_state(attention_state="merge_blocked")
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    clients = _enabling_clients_for(RECIPE_ESCALATE_MERGE_BLOCK)

    assert _act_escalate_merge_block(_detect_escalate(clients)) == [task.ticket_id]

    # Episode ends: hydration moves the PR off merge_blocked.
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(attention_state="ready_to_approve")
    save_dev_queue(store)

    cands = _detect_escalate(clients)
    assert cands == []
    _act_escalate_merge_block(cands)  # clear pass runs even with no candidates
    assert load_dev_queue().tasks[0].escalate_merge_block_fired_at is None

    # Genuine re-entry into merge_blocked fires again (episode semantics).
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(attention_state="merge_blocked")
    save_dev_queue(store)
    assert _act_escalate_merge_block(_detect_escalate(clients)) == [task.ticket_id]


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("attention_state", "expected_recipe"),
    [
        ("changes_requested", RECIPE_ADDRESS_REVIEW),
        ("ci_failing", RECIPE_AUTO_FIX_CI),
        ("no_reviewer", RECIPE_REQUEST_REVIEWER),
        ("merge_blocked", RECIPE_ESCALATE_MERGE_BLOCK),
        ("ready_to_approve", None),
    ],
)
def test_attention_state_routes_to_exactly_one_recipe(
    attention_state: str, expected_recipe: str | None
) -> None:
    task = _make_task(
        pr_url=_PR_URL, pr_state=_pr_state(attention_state=attention_state)
    )
    clients = _enabling_clients_for(
        RECIPE_ADDRESS_REVIEW,
        RECIPE_AUTO_FIX_CI,
        RECIPE_REQUEST_REVIEWER,
        RECIPE_ESCALATE_MERGE_BLOCK,
    )
    cfg = _config()
    detects = {
        RECIPE_ADDRESS_REVIEW: _detect_address_review,
        RECIPE_AUTO_FIX_CI: _detect_auto_fix_ci,
        RECIPE_REQUEST_REVIEWER: _detect_request_reviewer,
        RECIPE_ESCALATE_MERGE_BLOCK: _detect_escalate_merge_block,
    }
    firing = {
        recipe
        for recipe, fn in detects.items()
        if fn([task], clients=clients, config=cfg)
    }
    assert firing == (set() if expected_recipe is None else {expected_recipe})


def test_ready_to_approve_adds_no_action() -> None:
    task = _make_task(
        pr_url=_PR_URL, pr_state=_pr_state(attention_state="ready_to_approve")
    )
    clients = _enabling_clients_for(
        RECIPE_ADDRESS_REVIEW,
        RECIPE_AUTO_FIX_CI,
        RECIPE_REQUEST_REVIEWER,
        RECIPE_ESCALATE_MERGE_BLOCK,
    )
    cfg = _config()
    for fn in (
        _detect_address_review,
        _detect_auto_fix_ci,
        _detect_request_reviewer,
        _detect_escalate_merge_block,
    ):
        assert fn([task], clients=clients, config=cfg) == []


def test_run_review_recipes_wires_escalate_merge_block(tmp_config_dir: Path) -> None:
    """run_review_recipes drives the new escalate_merge_block recipe end-to-end."""
    _write_acme_clients_yaml(tmp_config_dir)
    # Opt the ticket into escalate_merge_block via the highest tier.
    task = _make_task(
        pr_url=_PR_URL,
        pr_state=_pr_state(attention_state="merge_blocked"),
        review_recipes={RECIPE_ESCALATE_MERGE_BLOCK: True},
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    acted = run_review_recipes(config=_config())

    assert task.ticket_id in acted
    assert load_dev_queue().tasks[0].escalate_merge_block_fired_at is not None
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    assert any(e.correlation_id == task.ticket_id for e in taken)


# --- anomaly / stale / vanished branches -----------------------------------


def _orphan_candidate(recipe: str, attention_state: str) -> ReviewRecipeCandidate:
    """A candidate whose (ticket_id, client) row is absent from the store."""
    return ReviewRecipeCandidate(
        ticket_id="GONE",
        client="acme",
        lane="default",
        recipe=recipe,
        attention_state=attention_state,
        pr_url=_PR_URL,
        evidence={},
        session_id=None,
    )


def test_act_auto_fix_ci_vanished_row_silent_skip(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    save_dev_queue(DevQueueStore(tasks=[]))  # row deleted between detect and act
    monkeypatch.setattr(
        "cw.dev_queue.add_ticket", lambda _t: pytest.fail("no dispatch for a gone row")
    )
    assert (
        _act_auto_fix_ci(
            [_orphan_candidate(RECIPE_AUTO_FIX_CI, "ci_failing")], clients={}
        )
        == []
    )
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []


def test_act_request_reviewer_vanished_row_silent_skip(tmp_config_dir: Path) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    save_dev_queue(DevQueueStore(tasks=[]))
    acted = _act_request_reviewer(
        [_orphan_candidate(RECIPE_REQUEST_REVIEWER, "no_reviewer")],
        clients=load_effective_clients(),
    )
    assert acted == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []


def test_act_request_reviewer_stale_row_silent_skip(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(
        pr_url=_PR_URL, pr_state=_pr_state(attention_state="ready_to_approve")
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    monkeypatch.setattr(
        "cw.gh.add_pr_reviewer", lambda *_a, **_kw: pytest.fail("no gh for a stale row")
    )
    acted = _act_request_reviewer(
        [_candidate(task, RECIPE_REQUEST_REVIEWER, "no_reviewer")],
        clients=load_effective_clients(),
    )
    assert acted == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


def test_act_request_reviewer_missing_client_emits_failed(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)  # defines acme, not ghost
    task = _make_task(
        client="ghost",
        pr_url="https://github.com/ghost/widgets/pull/42",
        pr_state=_pr_state(attention_state="no_reviewer"),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    monkeypatch.setattr(
        "cw.gh.add_pr_reviewer",
        lambda *_a, **_kw: pytest.fail("no gh for an unresolvable client"),
    )
    with caplog.at_level("WARNING"):
        acted = _act_request_reviewer(
            [_candidate(task, RECIPE_REQUEST_REVIEWER, "no_reviewer")],
            clients=load_effective_clients(),
        )
    assert acted == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].payload["client"] == "ghost"


def test_act_escalate_merge_block_vanished_row_silent_skip(
    tmp_config_dir: Path,
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    save_dev_queue(DevQueueStore(tasks=[]))
    acted = _act_escalate_merge_block(
        [_orphan_candidate(RECIPE_ESCALATE_MERGE_BLOCK, "merge_blocked")]
    )
    assert acted == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []


def test_act_escalate_merge_block_stale_row_silent_skip(tmp_config_dir: Path) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    # Re-loaded row moved off merge_blocked -> _fire returns False, no event.
    task = _make_task(
        pr_url=_PR_URL, pr_state=_pr_state(attention_state="ready_to_approve")
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    acted = _act_escalate_merge_block(
        [_candidate(task, RECIPE_ESCALATE_MERGE_BLOCK, "merge_blocked")]
    )
    assert acted == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    assert load_dev_queue().tasks[0].escalate_merge_block_fired_at is None


# --- outbound consent gate (RFC 0011 B2, #1159) -----------------------------


class TestResolveOutboundConsentAllowed:
    """Two-party consent gate for outbound acting toward another's PR.

    Party 1 (operator): ``config.review_recipes_enabled``, the existing
    review-recipes master switch (RFC 0010 P3). Party 2 (target): an active
    ``WatchedPr`` for the queried ``pr_url`` (RFC 0011 S2). See R1-R4.
    """

    _PR_URL = "https://github.com/acme/widgets/pull/42"

    def test_switch_off_returns_false_regardless_of_watched_pr(self) -> None:
        """The master switch off gates outbound action shut, even with an
        active WatchedPr match present."""
        assert (
            resolve_outbound_consent_allowed(
                self._PR_URL,
                config=_config(review_recipes_enabled=False),
                watched_prs=[_watched(pr_number=42)],
            )
            is False
        )

    def test_switch_on_active_match_returns_true(self) -> None:
        """Switch on + an active WatchedPr for this pr_url -> True."""
        assert (
            resolve_outbound_consent_allowed(
                self._PR_URL,
                config=_config(),
                watched_prs=[_watched(pr_number=42)],
            )
            is True
        )

    def test_switch_on_no_match_returns_false(self) -> None:
        """Switch on but no WatchedPr for this pr_url -> False."""
        assert (
            resolve_outbound_consent_allowed(
                self._PR_URL,
                config=_config(),
                watched_prs=[_watched(pr_number=99)],
            )
            is False
        )
        assert (
            resolve_outbound_consent_allowed(
                self._PR_URL,
                config=_config(),
                watched_prs=[],
            )
            is False
        )

    def test_switch_on_dismissed_watched_pr_returns_false(self) -> None:
        """A dismissed WatchedPr matching this pr_url does not open the
        channel -- only an active one does."""
        assert (
            resolve_outbound_consent_allowed(
                self._PR_URL,
                config=_config(),
                watched_prs=[_watched(pr_number=42, status="dismissed")],
            )
            is False
        )


# ---------------------------------------------------------------------------
# #1201 — repeat-fire burst detector + fired-at getters (anomaly layer)
# ---------------------------------------------------------------------------


def _record_taken(ticket_id: str, recipe: str, *, client: str = "acme") -> None:
    """Seed one PR_ACTION_TAKEN event for (ticket_id, recipe) at the frozen now."""
    record_event(
        OrchestratorEventType.PR_ACTION_TAKEN,
        {
            "ticket_id": ticket_id,
            "recipe": recipe,
            "client": client,
        },
        correlation_id=ticket_id,
    )


def _repeat_fire_payload_base(
    ticket_id: str = "GEN-1",
    recipe: str = RECIPE_ADDRESS_REVIEW,
    *,
    client: str = "acme",
) -> dict[str, object]:
    return {
        "client": client,
        "lane": "default",
        "recipe": recipe,
        "ticket_id": ticket_id,
        "pr_url": "https://github.com/acme/widgets/pull/42",
        "attention_state": "changes_requested",
        "session_id": "sess-1",
        "evidence_snapshot": {},
    }


class TestDetectRepeatFireCounts:
    """_detect_repeat_fire_counts: stateless per-(client, ticket, recipe) count."""

    def test_empty_inbox_returns_empty_dict(self, tmp_config_dir: Path) -> None:
        assert _detect_repeat_fire_counts(config=_config()) == {}

    def test_counts_within_window_grouped_by_client_ticket_and_recipe(
        self, tmp_config_dir: Path
    ) -> None:
        base = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
        with freeze_time(base):
            _record_taken("GEN-1", RECIPE_ADDRESS_REVIEW)
            _record_taken("GEN-1", RECIPE_ADDRESS_REVIEW)
            _record_taken("GEN-1", RECIPE_AUTO_FIX_CI)
            _record_taken("GEN-2", RECIPE_ADDRESS_REVIEW)
        counts = _detect_repeat_fire_counts(
            config=_config(), now=base + timedelta(minutes=1)
        )
        assert counts[("acme", "GEN-1", RECIPE_ADDRESS_REVIEW)] == 2
        assert counts[("acme", "GEN-1", RECIPE_AUTO_FIX_CI)] == 1
        assert counts[("acme", "GEN-2", RECIPE_ADDRESS_REVIEW)] == 1

    def test_events_outside_window_excluded(self, tmp_config_dir: Path) -> None:
        base = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
        with freeze_time(base - timedelta(minutes=30)):
            _record_taken("GEN-1", RECIPE_ADDRESS_REVIEW)  # 30 min ago — excluded
        with freeze_time(base):
            _record_taken("GEN-1", RECIPE_ADDRESS_REVIEW)  # inside — counted
        counts = _detect_repeat_fire_counts(
            config=_config(), now=base + timedelta(minutes=1)
        )
        assert counts[("acme", "GEN-1", RECIPE_ADDRESS_REVIEW)] == 1

    def test_event_exactly_at_cutoff_included(self, tmp_config_dir: Path) -> None:
        """The cutoff boundary is inclusive: an event at exactly now-window counts."""
        base = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
        window = _config().review_recipe_repeat_fire_window_minutes
        with freeze_time(base - timedelta(minutes=window)):
            _record_taken("GEN-1", RECIPE_ADDRESS_REVIEW)  # exactly at cutoff
        counts = _detect_repeat_fire_counts(config=_config(), now=base)
        assert counts[("acme", "GEN-1", RECIPE_ADDRESS_REVIEW)] == 1

    def test_read_events_failure_returns_empty_dict(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(**_kwargs: Any) -> list[Any]:
            msg = "inbox unreadable"
            raise OSError(msg)

        monkeypatch.setattr("cw.reconcile.review_recipes.core.read_events", _boom)
        assert _detect_repeat_fire_counts(config=_config()) == {}

    def test_malformed_payload_missing_keys_skipped(self, tmp_config_dir: Path) -> None:
        """A PR_ACTION_TAKEN with no/non-str client+ticket_id+recipe is not counted."""
        record_event(
            OrchestratorEventType.PR_ACTION_TAKEN,
            {"client": "acme"},  # no ticket_id / recipe
        )
        record_event(
            OrchestratorEventType.PR_ACTION_TAKEN,
            {"client": "acme", "ticket_id": 123, "recipe": None},  # non-str values
        )
        record_event(
            OrchestratorEventType.PR_ACTION_TAKEN,
            {"ticket_id": "GEN-1", "recipe": RECIPE_ADDRESS_REVIEW},  # no client
        )
        assert _detect_repeat_fire_counts(config=_config()) == {}

    def test_isolates_by_client_same_ticket_id(self, tmp_config_dir: Path) -> None:
        """Two clients whose numeric ticket_id collides don't share a count."""
        base = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
        with freeze_time(base):
            for _ in range(4):
                _record_taken("42", RECIPE_ADDRESS_REVIEW, client="acme")
            _record_taken("42", RECIPE_ADDRESS_REVIEW, client="widgetco")
        counts = _detect_repeat_fire_counts(
            config=_config(), now=base + timedelta(minutes=1)
        )
        assert counts[("acme", "42", RECIPE_ADDRESS_REVIEW)] == 4
        assert counts[("widgetco", "42", RECIPE_ADDRESS_REVIEW)] == 1


class TestRecordPrActionTaken:
    """_record_pr_action_taken: always records, escalates on exact crossing."""

    def test_records_event_regardless_of_count(self, tmp_config_dir: Path) -> None:
        _record_pr_action_taken(
            _repeat_fire_payload_base(),
            "acme",
            "GEN-1",
            RECIPE_ADDRESS_REVIEW,
            config=_config(),
            repeat_fire_counts={},
            lane="default",
        )
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert len(taken) == 1
        assert taken[0].correlation_id == "GEN-1"

    def test_exact_crossing_emits_session_needs_attention(
        self, tmp_config_dir: Path
    ) -> None:
        cfg = _config(
            review_recipe_repeat_fire_threshold=5,
            review_recipe_repeat_fire_window_minutes=20,
        )
        counts = {("acme", "GEN-1", RECIPE_ADDRESS_REVIEW): 4}  # prior 4 + this = 5
        _record_pr_action_taken(
            _repeat_fire_payload_base(),
            "acme",
            "GEN-1",
            RECIPE_ADDRESS_REVIEW,
            config=cfg,
            repeat_fire_counts=counts,
            lane="default",
        )
        attn = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
        assert len(attn) == 1
        assert attn[0].payload["paused_status"] == _REPEAT_FIRE_REASON
        assert attn[0].payload["ticket_id"] == "GEN-1"
        assert attn[0].payload["recipe"] == RECIPE_ADDRESS_REVIEW
        assert attn[0].payload["client"] == "acme"
        assert attn[0].payload["repeat_fire_count"] == 5
        assert attn[0].payload["window_minutes"] == 20
        assert attn[0].payload["lane"] == "default"

    def test_below_threshold_no_attention_event(self, tmp_config_dir: Path) -> None:
        cfg = _config(review_recipe_repeat_fire_threshold=5)
        counts = {("acme", "GEN-1", RECIPE_ADDRESS_REVIEW): 2}  # prior 2 + this = 3
        _record_pr_action_taken(
            _repeat_fire_payload_base(),
            "acme",
            "GEN-1",
            RECIPE_ADDRESS_REVIEW,
            config=cfg,
            repeat_fire_counts=counts,
            lane="default",
        )
        assert (
            read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
            == []
        )

    def test_past_threshold_no_re_fire(self, tmp_config_dir: Path) -> None:
        cfg = _config(review_recipe_repeat_fire_threshold=5)
        counts = {("acme", "GEN-1", RECIPE_ADDRESS_REVIEW): 5}  # prior 5 + this = 6 > 5
        _record_pr_action_taken(
            _repeat_fire_payload_base(),
            "acme",
            "GEN-1",
            RECIPE_ADDRESS_REVIEW,
            config=cfg,
            repeat_fire_counts=counts,
            lane="default",
        )
        assert (
            read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
            == []
        )

    def test_missing_key_defaults_to_zero(self, tmp_config_dir: Path) -> None:
        cfg = _config(review_recipe_repeat_fire_threshold=1)  # 0 + this = 1 == 1
        _record_pr_action_taken(
            _repeat_fire_payload_base(),
            "acme",
            "GEN-1",
            RECIPE_ADDRESS_REVIEW,
            config=cfg,
            repeat_fire_counts={},
            lane="default",
        )
        attn = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
        assert len(attn) == 1

    def test_none_config_records_without_burst_check(
        self, tmp_config_dir: Path
    ) -> None:
        """A direct _act_* call (no burst wiring) still records PR_ACTION_TAKEN."""
        _record_pr_action_taken(
            _repeat_fire_payload_base(),
            "acme",
            "GEN-1",
            RECIPE_ADDRESS_REVIEW,
            config=None,
            repeat_fire_counts=None,
            lane="default",
        )
        assert (
            len(read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])) == 1
        )
        assert (
            read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
            == []
        )

    def test_different_client_same_ticket_id_isolated_count(
        self, tmp_config_dir: Path
    ) -> None:
        """Tenant A's fire count is unaffected by tenant B's fires on the same id.

        Guards two distinct regression classes: (a) ``client`` dropped from the
        key entirely — a legacy ``(ticket_id, recipe)`` 2-tuple lookup would hit
        tenant B's count — and (b) ``client`` swapped/hardcoded to another
        tenant's value. A dict-typed regression on (a) is caught at the *type*
        level by mypy --strict on ``src/`` (the parameter is
        ``dict[tuple[str, str, str], int]``); this test additionally proves the
        *runtime* isolation a type annotation alone can't verify.
        """
        cfg = _config(review_recipe_repeat_fire_threshold=5)
        counts: dict[tuple[str, ...], int] = {
            ("42", RECIPE_ADDRESS_REVIEW): 4,  # legacy 2-tuple shape (regression a)
            ("widgetco", "42", RECIPE_ADDRESS_REVIEW): 4,  # tenant B, 1 short
        }
        _record_pr_action_taken(
            _repeat_fire_payload_base("42", RECIPE_ADDRESS_REVIEW, client="acme"),
            "acme",
            "42",
            RECIPE_ADDRESS_REVIEW,
            config=cfg,
            repeat_fire_counts=counts,
            lane="default",
        )
        # Tenant A's own count (absent from the ("acme", "42", recipe) key)
        # starts at 0 + this = 1, nowhere near the threshold — neither the
        # legacy 2-tuple shape nor tenant B's 3-tuple entry must leak in.
        assert (
            read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
            == []
        )

    def test_lane_present_in_attention_payload(self, tmp_config_dir: Path) -> None:
        """The new ``lane`` kwarg (#1333) lands in the SESSION_NEEDS_ATTENTION
        payload distinctly from ``_repeat_fire_payload_base``'s own pre-existing
        ``"lane": "default"`` key, which feeds the unrelated PR_ACTION_TAKEN
        payload only (via ``_review_payload_base``) and is never read here."""
        cfg = _config(
            review_recipe_repeat_fire_threshold=5,
            review_recipe_repeat_fire_window_minutes=20,
        )
        counts = {("acme", "GEN-1", RECIPE_ADDRESS_REVIEW): 4}  # prior 4 + this = 5
        _record_pr_action_taken(
            _repeat_fire_payload_base(),
            "acme",
            "GEN-1",
            RECIPE_ADDRESS_REVIEW,
            config=cfg,
            repeat_fire_counts=counts,
            lane="bugs",
        )
        attn = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
        assert len(attn) == 1
        assert attn[0].payload["lane"] == "bugs"


class TestRecipeFiredAtGetters:
    """RECIPE_FIRED_AT_GETTERS reads the correct per-recipe latch field."""

    def test_getters_cover_all_four_recipes(self) -> None:
        assert set(RECIPE_FIRED_AT_GETTERS) == {
            RECIPE_ADDRESS_REVIEW,
            RECIPE_AUTO_FIX_CI,
            RECIPE_REQUEST_REVIEWER,
            RECIPE_ESCALATE_MERGE_BLOCK,
        }
        # 1:1 with the attention-state routing map.
        assert set(RECIPE_FIRED_AT_GETTERS) == set(RECIPE_ATTENTION_STATES)

    @pytest.mark.parametrize(
        ("recipe", "field"),
        [
            (RECIPE_ADDRESS_REVIEW, "address_review_fired_at"),
            (RECIPE_AUTO_FIX_CI, "auto_fix_ci_fired_at"),
            (RECIPE_REQUEST_REVIEWER, "request_reviewer_fired_at"),
            (RECIPE_ESCALATE_MERGE_BLOCK, "escalate_merge_block_fired_at"),
        ],
    )
    def test_getters_read_the_correct_field(self, recipe: str, field: str) -> None:
        stamp = datetime(2026, 7, 17, tzinfo=UTC)
        task = _make_task(**{field: stamp})
        assert RECIPE_FIRED_AT_GETTERS[recipe](task) == stamp
        # A row with no latch set reads None (catches a copy-paste field mixup).
        assert RECIPE_FIRED_AT_GETTERS[recipe](_make_task()) is None


class TestRunReviewRecipesRepeatFire:
    """Integration: run_review_recipes wires the burst detector once per tick."""

    def _enqueue_cr_task(self, worktree: Path) -> TicketTask:
        task = _cr_task(
            review_recipes={RECIPE_ADDRESS_REVIEW: True}, worktree_path=worktree
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        return task

    def _rearm_latch(self) -> None:
        """Simulate a fresh changes_requested episode by clearing the latch."""
        store = load_dev_queue()
        store.tasks[0].address_review_fired_at = None
        save_dev_queue(store)

    def test_run_review_recipes_repeat_fire_triggers_attention_on_fifth_tick(
        self, tmp_config_dir: Path, make_git_repo: Any, stub_spawn: _SpawnRecorder
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir)
        self._enqueue_cr_task(make_git_repo("repeat-fire"))
        base = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
        for i in range(5):
            with freeze_time(base + timedelta(minutes=i)):
                self._rearm_latch()
                run_review_recipes(config=_config())
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert len(taken) == 5
        attn = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
        assert len(attn) == 1
        assert attn[0].payload["recipe"] == RECIPE_ADDRESS_REVIEW
        assert attn[0].payload["repeat_fire_count"] == 5

    def test_run_review_recipes_threshold_configurable(
        self, tmp_config_dir: Path, make_git_repo: Any, stub_spawn: _SpawnRecorder
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir)
        self._enqueue_cr_task(make_git_repo("threshold"))
        base = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
        for i in range(2):
            with freeze_time(base + timedelta(minutes=i)):
                self._rearm_latch()
                run_review_recipes(
                    config=_config(review_recipe_repeat_fire_threshold=2)
                )
        attn = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
        assert len(attn) == 1
        assert attn[0].payload["repeat_fire_count"] == 2

    def test_run_review_recipes_repeat_fire_counts_computed_once_per_tick(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        stub_spawn: _SpawnRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir)
        self._enqueue_cr_task(make_git_repo("once-per-tick"))
        calls: list[dict[str, Any]] = []

        def _spy(**kwargs: Any) -> dict[tuple[str, str, str], int]:
            calls.append(kwargs)
            return _real_detect_repeat_fire_counts(**kwargs)

        monkeypatch.setattr(
            "cw.reconcile.review_recipes.core._detect_repeat_fire_counts", _spy
        )
        run_review_recipes(config=_config())
        # One detector call per tick — NOT once per recipe (four recipes run).
        assert len(calls) == 1

    def test_run_review_recipes_repeat_fire_isolated_per_recipe(
        self, tmp_config_dir: Path, make_git_repo: Any, stub_spawn: _SpawnRecorder
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir)
        task = self._enqueue_cr_task(make_git_repo("isolated"))
        base = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
        with freeze_time(base):
            # 4 prior address_review fires + 4 unrelated auto_fix_ci fires.
            for _ in range(4):
                _record_taken(task.ticket_id, RECIPE_ADDRESS_REVIEW)
            for _ in range(4):
                _record_taken(task.ticket_id, RECIPE_AUTO_FIX_CI)
        with freeze_time(base + timedelta(minutes=1)):
            run_review_recipes(config=_config())
        attn = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
        # Only address_review crossed its threshold; auto_fix_ci counts don't
        # leak into the address_review key.
        assert len(attn) == 1
        assert attn[0].payload["recipe"] == RECIPE_ADDRESS_REVIEW


class TestAttentionConstantsTypedAsPrAttentionState:
    """GitHub #1613 -- drift guard: _shared.py's four _ATTENTION_* constants
    must be annotated PrAttentionState (not bare str), so a typo in any of
    them is a type error, not a silently-never-matching runtime comparison.
    """

    def test_attention_constant_values_are_pr_attention_state_members(self) -> None:
        """Value-level guard (mirrors test_board.py's set-comparison shape).
        Catches PrAttentionState losing/renaming a member (mutation a). Does
        NOT catch a reverted-to-str/dropped annotation (mutation b) -- see
        the annotation-level guard below, per GitHub #1613's R4.
        """
        values = {
            _shared._ATTENTION_CHANGES_REQUESTED,
            _shared._ATTENTION_CI_FAILING,
            _shared._ATTENTION_NO_REVIEWER,
            _shared._ATTENTION_MERGE_BLOCKED,
        }
        assert values <= set(get_args(PrAttentionState))

    @pytest.mark.parametrize(
        "name",
        [
            "_ATTENTION_CHANGES_REQUESTED",
            "_ATTENTION_CI_FAILING",
            "_ATTENTION_NO_REVIEWER",
            "_ATTENTION_MERGE_BLOCKED",
        ],
    )
    def test_attention_constant_annotated_as_pr_attention_state(
        self, name: str
    ) -> None:
        """Annotation-level guard: catches mutation (b) -- a reverted-to-str
        or dropped annotation -- which the value-level guard above cannot
        see (the runtime string value is unchanged either way).

        _shared.py carries `from __future__ import annotations`, so its raw
        __annotations__ values are unevaluated source strings, not type
        objects. `typing.get_type_hints(_shared)` can't be called on the
        whole module directly: other module-level names (e.g.
        RECIPE_FIRED_AT_GETTERS) are annotated with TYPE_CHECKING-only
        forward refs (Callable, datetime, TicketTask) that are genuinely
        undefined at runtime, so a whole-module resolution raises NameError
        before it reaches our four names. Scoping resolution to a throwaway
        stub carrying only *name*'s raw annotation string, resolved against
        the real module's namespace, resolves exactly the annotation we
        need without requiring those unrelated forward refs.
        """
        raw = _shared.__annotations__
        assert name in raw, f"{name} has no annotation at all (bare constant)"
        stub = type("_AnnotationStub", (), {"__annotations__": {name: raw[name]}})
        resolved = typing.get_type_hints(stub, globalns=vars(_shared))[name]
        assert resolved == PrAttentionState, (
            f"{name} must be annotated PrAttentionState, resolved {resolved!r}"
        )


# --- fix_agent recipe (#2017) ----------------------------------------------


def _make_fix_client(
    make_git_repo: Callable[..., Path], tmp_path: Path, name: str = "acme"
) -> ClientConfig:
    """ClientConfig whose workspace_path is a real git repo.

    ``create_worktree`` actually walks this repo with git, so ``_make_client``'s
    bare-mkdir workspace (tests/test_spawn.py) cannot stand in here. Combines
    that helper's shape with the ``ClientConfig(worktree_base=...)`` pattern
    precedented in tests/test_worktree_lifecycle.py.
    """
    repo = make_git_repo(f"{name}-main")
    return ClientConfig(
        name=name,
        workspace_path=repo,
        default_branch="main",
        worktree_base=tmp_path / "wt",
    )


def _seed_bare_origin_with_main(client: ClientConfig) -> None:
    """Give *client*'s repo a real bare origin carrying just ``main``.

    The preamble every fix_agent git fixture starts from (#2209): init the bare
    origin, wire it up as ``origin``, and land ``shared.txt`` on ``main`` so a
    later main-side edit of the same file can conflict for real. Factored out of
    ``_seed_origin``/``_seed_origin_renamed_upstream`` rather than copied a
    fourth time; ``tests/test_reconcile_fix_dispatch.py`` imports it directly
    for its own end-to-end fixtures.
    """
    repo = client.workspace_path
    origin = repo.parent / f"{repo.name}-origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_git_env(),
    )
    git_in(repo, "remote", "add", "origin", str(origin))
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    git_in(repo, "add", "shared.txt")
    git_in(repo, "commit", "-m", "base file")
    git_in(repo, "push", "origin", "main")


def _seed_origin(client: ClientConfig, branch: str) -> None:
    """Give *client*'s repo a real bare origin carrying main and *branch*.

    Reproduces the ONLY git state ``dispatch_fix_agent`` can legitimately
    observe: its caller contract (auto-dev-review.md Step 3b) guarantees the
    implementation branch is already pushed, and Step 1's cleanup deletes only
    the *local* ref. So local ``refs/heads/<branch>`` is absent while
    ``origin/<branch>`` carries real history.
    """
    repo = client.workspace_path
    _seed_bare_origin_with_main(client)

    git_in(repo, "checkout", "-b", branch)
    (repo / "shared.txt").write_text("branch side\n", encoding="utf-8")
    git_in(repo, "add", "shared.txt")
    git_in(repo, "commit", "-m", "impl commit")
    git_in(repo, "push", "origin", branch)

    git_in(repo, "checkout", "main")
    git_in(repo, "branch", "-D", branch)
    git_in(repo, "fetch", "origin")


def _advance_origin_main(client: ClientConfig, relpath: str, content: str) -> None:
    """Land one more commit on origin/main after ``_seed_origin``."""
    repo = client.workspace_path
    (repo / relpath).write_text(content, encoding="utf-8")
    git_in(repo, "add", relpath)
    git_in(repo, "commit", "-m", f"main advances {relpath}")
    git_in(repo, "push", "origin", "main")
    git_in(repo, "fetch", "origin")


_FIX_PROMPT_TEXT = "fix the MUST_FIX items\n"


def _seed_fix_parent_session(client: ClientConfig, session_id: str) -> None:
    """Seed a real Session so dispatch_fix_agent's find_session_by_id(parent)
    resolution (#2149) resolves *session_id* instead of silently falling back
    to the unresolvable-parent path (parent=None + friction note).
    """
    from cw.config import save_state
    from cw.models import CwState, Session

    save_state(
        CwState(
            sessions=[
                Session(
                    id=session_id,
                    name=f"{client.name}/review/2017",
                    client=client.name,
                    purpose=SessionPurpose.IMPL,
                    workspace_path=client.workspace_path,
                    status=SessionStatus.COMPLETED,
                )
            ]
        )
    )


def test_dispatch_fix_agent_provisions_worktree_and_spawns(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """Happy path: local ref absent, origin/<branch> present -> provision + spawn."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")

    session_id = dispatch_fix_agent(
        client=client,
        branch=branch,
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2017",
        ticket_id="2017",
        lane="default",
        parent="parent-session",
    )

    assert session_id == "spawned-session-id"
    assert len(stub_spawn.calls) == 1
    call = stub_spawn.calls[0]
    assert call["headless"] is False
    assert call["purpose"] is SessionPurpose.FIX
    assert call["ticket_id"] == "2017"
    assert call["lane"] == "default"
    assert call["parent"] == "parent-session"
    assert call["label"] == "fix-2017"
    assert call["prompt"] == "fix the MUST_FIX items\n"
    assert call["worktree"] == worktree_path_for(client, branch)
    assert call["worktree"].exists()


def test_dispatch_fix_agent_no_task_kwarg(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """No ``task=`` kwarg: task.attempts and lane occupancy stay untouched."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")

    dispatch_fix_agent(
        client=client,
        branch=branch,
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2017",
        ticket_id="2017",
        lane="default",
        parent="parent-session",
    )

    assert "task" not in stub_spawn.calls[0]


def test_dispatch_fix_agent_resumes_pushed_branch(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """Re-dispatch reuses the existing worktree, never StaleWorktreeError."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")
    kwargs: dict[str, Any] = {
        "client": client,
        "branch": branch,
        "prompt": _FIX_PROMPT_TEXT,
        "label": "fix-2017",
        "ticket_id": "2017",
        "lane": "default",
        "parent": "parent-session",
    }

    dispatch_fix_agent(**kwargs)
    wt = worktree_path_for(client, branch)
    assert wt.exists()

    dispatch_fix_agent(**kwargs)

    assert len(stub_spawn.calls) == 2
    assert stub_spawn.calls[1]["worktree"] == wt


def test_dispatch_fix_agent_fast_forwards_behind_worktree(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """#2213: a reused worktree behind an already-fetched tracking ref is
    fast-forwarded by ``create_worktree`` instead of tripping the HEAD check."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")
    worktree = create_worktree(client, branch, allow_dirty_reuse=True)
    old_sha = git_in(worktree, "rev-parse", "HEAD")

    origin = Path(git_in(client.workspace_path, "remote", "get-url", "origin"))
    new_sha = push_commit_to_origin(origin, branch, tmp_path / "side", "upstream.txt")
    # Load-bearing: without this fetch the workspace's tracking ref still equals
    # the worktree's HEAD, ``_resolve_remote_ref`` resolves to that stale ref,
    # the HEAD-equals-remote check passes today, and this test is vacuous.
    git_in(client.workspace_path, "fetch", "origin")
    assert new_sha != old_sha
    assert git_in(worktree, "rev-parse", "HEAD") == old_sha

    dispatch_fix_agent(
        client=client,
        branch=branch,
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2017",
        ticket_id="2017",
        lane="default",
        parent="parent-session",
    )

    assert len(stub_spawn.calls) == 1
    assert git_in(worktree, "rev-parse", "HEAD") == new_sha
    # A refresh that worked leaves no friction note behind.
    assert "Friction note" not in str(stub_spawn.calls[0]["prompt"])
    # Round 6: the fast-forward left exactly one audit event, attributed to the
    # dispatching ticket (the fix-agent path threads its ``ticket_id`` through).
    (event,) = read_events(event_types=[OrchestratorEventType.WORKTREE_FAST_FORWARDED])
    assert event.correlation_id == "2017"
    assert event.payload["ticket_id"] == "2017"
    assert event.payload["branch"] == branch
    assert event.payload["old_sha"] == old_sha
    assert event.payload["new_sha"] == new_sha


@pytest.mark.parametrize("worktree_is", ["in-sync", "behind"])
@pytest.mark.parametrize("source", ["roster", "state", "unreadable-roster"])
def test_dispatch_fix_agent_refuses_an_occupied_worktree_and_mutates_nothing(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
    source: str,
    worktree_is: str,
) -> None:
    """#2213 round 4: a refresh refusal for a live occupant must stop EVERY
    later mutation on the fix-agent path, not just the fast-forward.

    Before, ``create_worktree`` declined to fast-forward and dispatch then went
    on regardless: ``git fetch origin``, a merge of ``origin/main`` INTO the
    occupied worktree, and a spawn onto it. ``origin/main`` has moved here, so
    any of those is observable: the worktree fingerprint (HEAD, index, every
    file's bytes) and the workspace's ``origin/main`` tracking ref must all be
    exactly as they were, and nothing is spawned.
    """
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")
    worktree = create_worktree(client, branch, allow_dirty_reuse=True)
    origin = Path(git_in(client.workspace_path, "remote", "get-url", "origin"))
    if worktree_is == "behind":
        push_commit_to_origin(origin, branch, tmp_path / "side", "upstream.txt")
        git_in(client.workspace_path, "fetch", "origin")
    # origin/main moves on AFTER the workspace last fetched: a dispatch-level
    # ``git fetch origin`` would advance the tracking ref, and the merge that
    # follows it would put a merge commit on the worktree's HEAD.
    push_commit_to_origin(origin, "main", tmp_path / "side-main", "main-only.txt")
    occupy_worktree(client, worktree, source)
    main_ref = "refs/remotes/origin/main"
    main_before = git_in(client.workspace_path, "rev-parse", main_ref)
    before = tree_fingerprint(worktree)

    with pytest.raises(HookContextConflictError) as excinfo:
        dispatch_fix_agent(
            client=client,
            branch=branch,
            prompt=_FIX_PROMPT_TEXT,
            label="fix-2017",
            ticket_id="2017",
            lane="default",
            parent="parent-session",
        )

    assert tree_fingerprint(worktree) == before
    assert git_in(client.workspace_path, "rev-parse", main_ref) == main_before
    assert stub_spawn.calls == []
    # Round 5: the transient conflict is raised FROM the typed refusal that
    # ``create_worktree`` now raises, not from a flag the caller remembered to
    # check -- so no later step of this function can run without it.
    cause = excinfo.value.__cause__
    assert isinstance(cause, WorktreeOccupiedError)
    assert cause.path == worktree
    message = str(excinfo.value)
    assert str(worktree) in message
    assert branch in message
    assert "worktree was not touched" in message
    # The reason is named: which occupant, or that it could not be ruled out.
    assert {
        "roster": "live daemon worker",
        "state": "live session",
        "unreadable-roster": "roster unreadable",
    }[source] in message


def test_dispatch_fix_agent_proceeds_past_unsaved_work_alone(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """Control for the refusal test: only a LIVE occupant stops the dispatch.
    Unsaved work refuses the fast-forward, but this path legitimately reuses a
    worktree carrying a prior stage's churn (``allow_dirty_reuse``), so the
    dispatch goes ahead and the churn survives."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")
    worktree = create_worktree(client, branch, allow_dirty_reuse=True)
    churn = worktree / "uv.lock"
    churn.write_text("churn from the prior stage\n", encoding="utf-8")

    dispatch_fix_agent(
        client=client,
        branch=branch,
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2017",
        ticket_id="2017",
        lane="default",
        parent="parent-session",
    )

    assert len(stub_spawn.calls) == 1
    assert churn.read_text(encoding="utf-8") == "churn from the prior stage\n"


def test_dispatch_fix_agent_reports_failed_refresh_fetch_in_friction_note(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2213 round 3: unlike ``create_worktree``, this caller has a friction
    surface (the prompt prefix), so a failed refresh fetch is named there --
    worktree and reason -- alongside the log line, and the dispatch proceeds."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")
    worktree = create_worktree(client, branch, allow_dirty_reuse=True)
    # In sync with origin, so the HEAD check passes even though the refresh
    # fetch (patched) fails; only the dispatch's own real ``git fetch`` runs.
    patch_worktree(
        monkeypatch,
        "fetch_feature_branch",
        lambda _c, _b: FetchResult(
            FetchOutcome.FAILED, "rc=128: fatal: Could not read from remote repository."
        ),
    )

    dispatch_fix_agent(
        client=client,
        branch=branch,
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2017",
        ticket_id="2017",
        lane="default",
        parent="parent-session",
    )

    assert len(stub_spawn.calls) == 1
    prompt = str(stub_spawn.calls[0]["prompt"])
    note = next(
        line for line in prompt.splitlines() if line.startswith("_Friction note:")
    )
    assert str(worktree) in note
    assert f"origin/{branch}" in note
    assert "fetch" in note
    # Round 5: the note says WHY, not just that the fetch failed.
    assert "Could not read from remote repository" in note
    assert prompt.endswith(_FIX_PROMPT_TEXT)


def test_dispatch_fix_agent_verifies_head_before_merge(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worktree whose HEAD matches no candidate ref aborts before any spawn.

    Since #2209 there is no standalone post-hoc HEAD check to trip — the ladder
    IS the check. The detached ``stale`` worktree has no upstream, and the only
    candidate that exists (``origin/dev/2017``, pushed by ``_seed_origin``) has
    a tip that is not this HEAD, so it is skipped rather than raised on and the
    ladder exhausts.
    """
    from cw.reconcile.review_recipes import fix_agent as fix_agent_mod

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)

    stale = tmp_path / "stale-wt"
    git_in(client.workspace_path, "worktree", "add", "--detach", str(stale), "main")
    monkeypatch.setattr(
        fix_agent_mod, "create_worktree", lambda *_args, **_kwargs: stale
    )

    with pytest.raises(
        RemoteRefUnresolvedError, match="no upstream configured"
    ) as excinfo:
        fix_agent_mod.dispatch_fix_agent(
            client=client,
            branch=branch,
            prompt=_FIX_PROMPT_TEXT,
            label="fix-2017",
            ticket_id="2017",
            lane="default",
            parent="parent-session",
        )

    assert "origin/dev/2017" in str(excinfo.value)
    assert stub_spawn.calls == []


def test_dispatch_fix_agent_merge_conflict_blocks(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """A conflicting origin/main merge aborts, leaves the worktree clean, raises."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")
    _advance_origin_main(client, "shared.txt", "main side\n")

    with pytest.raises(CwError, match=r"shared\.txt"):
        dispatch_fix_agent(
            client=client,
            branch=branch,
            prompt=_FIX_PROMPT_TEXT,
            label="fix-2017",
            ticket_id="2017",
            lane="default",
            parent="parent-session",
        )

    assert stub_spawn.calls == []
    wt = worktree_path_for(client, branch)
    status = subprocess.run(
        ["git", "-C", str(wt), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_git_env(),
    )
    assert status.stdout.strip() == ""
    merge_head = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--verify", "-q", "MERGE_HEAD"],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_git_env(),
    )
    assert merge_head.returncode != 0


def test_dispatch_fix_agent_merge_clean_succeeds(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """A non-conflicting origin/main commit merges in and the spawn still runs."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")
    _advance_origin_main(client, "sibling.txt", "merged sibling\n")

    dispatch_fix_agent(
        client=client,
        branch=branch,
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2017",
        ticket_id="2017",
        lane="default",
        parent="parent-session",
    )

    assert len(stub_spawn.calls) == 1
    wt = worktree_path_for(client, branch)
    assert (wt / "sibling.txt").read_text(encoding="utf-8") == "merged sibling\n"
    assert (wt / "shared.txt").read_text(encoding="utf-8") == "branch side\n"


def test_dispatch_fix_agent_propagates_spawn_cwerror(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """A spawn CwError propagates (deliberate deviation from address_review)."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")

    def _boom(**_kwargs: Any) -> None:
        msg = "daemon never adopted the worker"
        raise CwError(msg)

    stub_spawn.side_effect = _boom

    with pytest.raises(CwError, match="daemon never adopted the worker"):
        dispatch_fix_agent(
            client=client,
            branch=branch,
            prompt=_FIX_PROMPT_TEXT,
            label="fix-2017",
            ticket_id="2017",
            lane="default",
            parent="parent-session",
        )


def _seed_live_session_context(client: ClientConfig, branch: str) -> Path:
    """Provision the fix branch's worktree and point its hook context at a live
    session, reproducing what a resident REVIEW session leaves behind.

    Fixture shape mirrors tests/test_spawn.py's own
    ``test_daemon_overwrite_raises_when_context_references_live_session``: a
    real ACTIVE Session in cw state plus a hand-written ``cw-context.json``
    naming it.
    """
    from cw.config import save_state
    from cw.models import CwState, Session, SessionOrigin, SessionStatus
    from cw.worktree import create_worktree

    save_state(
        CwState(
            sessions=[
                Session(
                    id="live1234",
                    name=f"{client.name}/auto-dev/2017",
                    client=client.name,
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.ACTIVE,
                    workspace_path=client.workspace_path,
                )
            ]
        )
    )
    worktree = create_worktree(client, branch, allow_dirty_reuse=True)
    claude_dir = worktree / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "cw-context.json").write_text(
        json.dumps(
            {
                "session_id": "live1234",
                "session_name": f"{client.name}/auto-dev/2017",
                "client": client.name,
                "purpose": "impl",
                "ticket_id": "2017",
                "headless": True,
            }
        ),
        encoding="utf-8",
    )
    return worktree


def test_dispatch_fix_agent_refuses_live_worktree_before_mutating(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    tmp_config_dir: Path,
) -> None:
    """R22/R23: the live-session refusal happens before any worktree mutation.

    Deliberately does NOT stub ``spawn_create_impl`` -- the whole point is to
    exercise the real ``_write_hook_context`` guard that every other test in
    this block stubs past, which is the coverage gap #2017's own fix loop fell
    into. ``origin/main`` is advanced first so a merge that *did* run would be
    visible in the worktree; asserting HEAD and the working tree are untouched
    is what proves the refusal preceded the fetch/merge.
    """
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _advance_origin_main(client, "sibling.txt", "merged sibling\n")
    worktree = _seed_live_session_context(client, branch)
    head_before = git_in(worktree, "rev-parse", "HEAD")
    log_before = git_in(worktree, "log", "--oneline")
    status_before = git_in(worktree, "status", "--porcelain")

    with pytest.raises(HookContextConflictError, match="live1234"):
        dispatch_fix_agent(
            client=client,
            branch=branch,
            prompt=_FIX_PROMPT_TEXT,
            label="fix-2017",
            ticket_id="2017",
            lane="default",
            parent="live1234",
        )

    assert git_in(worktree, "rev-parse", "HEAD") == head_before
    assert git_in(worktree, "log", "--oneline") == log_before
    assert git_in(worktree, "status", "--porcelain") == status_before
    assert not (worktree / "sibling.txt").exists()


@pytest.mark.parametrize(
    ("context_text", "session_status"),
    [
        pytest.param(
            '{"session_id": "live1234"}',
            SessionStatus.COMPLETED,
            id="writer_went_terminal",
        ),
        pytest.param('{"session_id": "live1234"}', None, id="session_unknown_to_cw"),
        pytest.param('{"client": "acme"}', None, id="context_names_no_session"),
        pytest.param("{not json", None, id="context_unparseable"),
    ],
)
def test_dispatch_fix_agent_pre_check_lets_a_free_worktree_through(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    tmp_config_dir: Path,
    stub_spawn: _SpawnRecorder,
    context_text: str,
    session_status: SessionStatus | None,
) -> None:
    """The pre-check refuses only a genuinely live holder.

    The happy path of the R21 handoff IS this: the REVIEW session named in the
    hook context has gone terminal by the time the reconcile tick dispatches. A
    pre-check that refused any of these would deadlock the fix loop outright, so
    each tolerance branch is pinned rather than left to the guard's own reading.
    """
    from cw.config import save_state
    from cw.models import CwState, Session, SessionOrigin
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent
    from cw.worktree import create_worktree

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    sessions = (
        []
        if session_status is None
        else [
            Session(
                id="live1234",
                name=f"{client.name}/auto-dev/2017",
                client=client.name,
                purpose=SessionPurpose.IMPL,
                origin=SessionOrigin.DAEMON,
                status=session_status,
                workspace_path=client.workspace_path,
            )
        ]
    )
    save_state(CwState(sessions=sessions))
    worktree = create_worktree(client, branch, allow_dirty_reuse=True)
    claude_dir = worktree / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "cw-context.json").write_text(context_text, encoding="utf-8")

    dispatch_fix_agent(
        client=client,
        branch=branch,
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2017",
        ticket_id="2017",
        lane="default",
        parent="review-sess",
    )

    assert len(stub_spawn.calls) == 1


def test_dispatch_fix_agent_merge_abort_failure_raises_distinct_message(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R24 SHOULD_FIX: a failed ``merge --abort`` must not claim a clean tree."""
    from cw.reconcile.review_recipes import fix_agent as fix_agent_mod

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")
    _advance_origin_main(client, "shared.txt", "main side\n")

    real_run_git = fix_agent_mod._run_git

    def _abort_fails(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("merge", "--abort"):
            return subprocess.CompletedProcess(
                args=list(args), returncode=128, stdout=""
            )
        return real_run_git(*args, **kwargs)

    monkeypatch.setattr(fix_agent_mod, "_run_git", _abort_fails)

    with pytest.raises(CwError, match="NOT verified clean") as excinfo:
        fix_agent_mod.dispatch_fix_agent(
            client=client,
            branch=branch,
            prompt=_FIX_PROMPT_TEXT,
            label="fix-2017",
            ticket_id="2017",
            lane="default",
            parent="parent-session",
        )

    assert "128" in str(excinfo.value)
    assert "worktree left clean" not in str(excinfo.value)
    assert stub_spawn.calls == []


def test_dispatch_fix_agent_records_session_spawned_event(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """R24 MUST_FIX: a successful dispatch leaves a durable spawn audit trail."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2017"
    _seed_origin(client, branch)
    _seed_fix_parent_session(client, "parent-session")

    session_id = dispatch_fix_agent(
        client=client,
        branch=branch,
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2017",
        ticket_id="2017",
        lane="default",
        parent="parent-session",
    )

    events = read_events(event_types=[OrchestratorEventType.SESSION_SPAWNED])
    assert len(events) == 1
    assert events[0].correlation_id == "2017"
    assert events[0].payload == {
        "ticket_id": "2017",
        "client": client.name,
        "session_id": session_id,
        "lane": "default",
    }
    # Guards the exact regression R24 names: a ClientConfig object here would
    # break event JSON serialization and this package's identity convention.
    assert events[0].payload["client"] == "acme"


def _seed_origin_renamed_upstream(
    client: ClientConfig, local_branch: str, remote_branch: str
) -> None:
    """Like ``_seed_origin``, but the pushed branch and the local tracking
    branch have different names (#2145) -- reproducing a worktree whose
    checked-out branch was renamed locally after being pushed under another
    name.

    ``git branch <local_branch> --track origin/<remote_branch>`` is used
    instead of ``git checkout -b`` because ``git worktree add`` refuses a
    branch already checked out elsewhere (the main checkout stays on
    ``main``); a plain ``git branch --track`` only creates the ref and its
    upstream config without moving HEAD.
    """
    repo = client.workspace_path
    _seed_bare_origin_with_main(client)

    git_in(repo, "checkout", "-b", remote_branch)
    (repo / "shared.txt").write_text("branch side\n", encoding="utf-8")
    git_in(repo, "add", "shared.txt")
    git_in(repo, "commit", "-m", "impl commit")
    git_in(repo, "push", "origin", remote_branch)

    git_in(repo, "checkout", "main")
    git_in(repo, "branch", "-D", remote_branch)
    git_in(repo, "fetch", "origin")
    git_in(repo, "branch", local_branch, "--track", f"origin/{remote_branch}")


def test_dispatch_fix_agent_resolves_differently_named_upstream(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """#2145: local branch name differs from the pushed remote branch name --
    dispatch_fix_agent resolves via the branch's configured upstream (@{u})
    instead of guessing origin/<local branch name>, which does not exist."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    local_branch = "dev/2145-local"
    remote_branch = "dev/2145-remote"
    _seed_origin_renamed_upstream(client, local_branch, remote_branch)
    _seed_fix_parent_session(client, "parent-session")

    session_id = dispatch_fix_agent(
        client=client,
        branch=local_branch,
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2145",
        ticket_id="2145",
        lane="default",
        parent="parent-session",
    )

    assert session_id == "spawned-session-id"
    assert len(stub_spawn.calls) == 1


def test_dispatch_fix_agent_ref_resolution_failure_names_upstream_in_message(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """#2145: when neither the branch's upstream nor a guessed origin/<branch>
    resolves, the error names what was checked instead of leaking a raw
    rev-parse failure, and never invents a ref that isn't in the repo."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2145-never-pushed"
    repo = client.workspace_path
    _seed_bare_origin_with_main(client)
    # create_worktree starts a brand-new branch from origin/<default_branch>
    # (the only path reachable here, since neither a local nor a remote ref
    # for `branch` exists yet) and git's default branch.autoSetupMerge would
    # otherwise configure @{u} = origin/main for it -- that's a resolvable
    # upstream, just not the one this test needs absent. Disabling it
    # reproduces the genuine "nothing resolves" case: a brand-new branch
    # with no tracking config and no pushed history of its own.
    git_in(repo, "config", "branch.autoSetupMerge", "false")

    with pytest.raises(
        RemoteRefUnresolvedError, match="no upstream configured"
    ) as excinfo:
        dispatch_fix_agent(
            client=client,
            branch=branch,
            prompt=_FIX_PROMPT_TEXT,
            label="fix-2145",
            ticket_id="2145",
            lane="default",
            parent="parent-session",
        )

    assert f"origin/{branch}" in str(excinfo.value)
    assert stub_spawn.calls == []


def _seed_worktree_with_unpushed_commit(
    client: ClientConfig, branch: str, *, auto_setup_merge: bool = True
) -> Path:
    """Bare origin + a real worktree on *branch* carrying one commit of its own.

    Models the git state a cw dispatch worktree actually reaches (#2209): the
    impl agent commits directly on the session worktree's local branch, which
    carries the templated ``<prefix>/<ticket>`` name whatever name it was later
    pushed under. With *auto_setup_merge* False the branch gets no ``@{u}`` at
    all; left True, git's default configures ``@{u} = origin/main`` — a ref
    that resolves but whose tip is not this worktree's HEAD.
    """
    from cw.worktree import create_worktree

    repo = client.workspace_path
    _seed_bare_origin_with_main(client)
    if not auto_setup_merge:
        git_in(repo, "config", "branch.autoSetupMerge", "false")
    worktree = create_worktree(client, branch)
    (worktree / "impl.txt").write_text("impl work\n", encoding="utf-8")
    git_in(worktree, "add", "impl.txt")
    git_in(worktree, "commit", "-m", "impl commit")
    return worktree


def test_dispatch_fix_agent_uses_reported_remote_branch_when_no_upstream(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """#2209 regression: the impl pushed under a slug name and set no upstream.

    Nothing in the repo names ``origin/dev/2209``; only the sentinel-reported
    ``origin/dev/2209-short-description`` carries HEAD. The worktree stays keyed
    by the templated local branch, which is what every other pipeline stage
    provisions.
    """
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2209"
    worktree = _seed_worktree_with_unpushed_commit(
        client, branch, auto_setup_merge=False
    )
    git_in(worktree, "push", "origin", "HEAD:refs/heads/dev/2209-short-description")
    _seed_fix_parent_session(client, "parent-session")

    session_id = dispatch_fix_agent(
        client=client,
        branch=branch,
        remote_branch="dev/2209-short-description",
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2209",
        ticket_id="2209",
        lane="default",
        parent="parent-session",
    )

    assert session_id == "spawned-session-id"
    assert len(stub_spawn.calls) == 1
    assert stub_spawn.calls[0]["worktree"] == worktree_path_for(client, branch)


def test_dispatch_fix_agent_reported_branch_outranks_default_branch_upstream(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """The ticket's core repro: default ``autoSetupMerge`` + a reported branch.

    ``@{u}`` is ``origin/main`` here — it resolves, so the pre-#2209 tree picked
    it and then died on the post-hoc HEAD check, looping review -> failed
    dispatch -> review forever. Under the HEAD-matching ladder ``origin/main``
    simply fails the tip test and the reported branch wins.
    """
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2209"
    worktree = _seed_worktree_with_unpushed_commit(client, branch)
    git_in(worktree, "push", "origin", "HEAD:refs/heads/dev/2209-short-description")
    _seed_fix_parent_session(client, "parent-session")

    dispatch_fix_agent(
        client=client,
        branch=branch,
        remote_branch="dev/2209-short-description",
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2209",
        ticket_id="2209",
        lane="default",
        parent="parent-session",
    )

    assert len(stub_spawn.calls) == 1


def test_dispatch_fix_agent_stale_reported_branch_falls_through_to_templated_guess(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """An existing-but-stale reported ref is skipped, not raised on.

    ``origin/dev/2209-old-slug`` resolves, but its tip is the earlier commit A;
    HEAD has since advanced to B, which was pushed under the templated name.
    "First existing" was fatal here with no rescue from any later rung — the
    exact gap the tip-matching ladder closes.
    """
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2209"
    worktree = _seed_worktree_with_unpushed_commit(
        client, branch, auto_setup_merge=False
    )
    git_in(worktree, "push", "origin", "HEAD:refs/heads/dev/2209-old-slug")
    (worktree / "impl.txt").write_text("more impl work\n", encoding="utf-8")
    git_in(worktree, "add", "impl.txt")
    git_in(worktree, "commit", "-m", "second impl commit")
    git_in(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
    _seed_fix_parent_session(client, "parent-session")

    dispatch_fix_agent(
        client=client,
        branch=branch,
        remote_branch="dev/2209-old-slug",
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2209",
        ticket_id="2209",
        lane="default",
        parent="parent-session",
    )

    assert len(stub_spawn.calls) == 1


def test_dispatch_fix_agent_raises_when_every_candidate_exists_but_head_diverged(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """Existence alone is never sufficient, at any rung.

    Both candidates resolve, both sit at commit A, and HEAD has moved on to an
    unpushed commit B. Nothing describes the tree that would ship, so the
    dispatch refuses rather than merging onto a branch state no remote holds.
    """
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2209"
    worktree = _seed_worktree_with_unpushed_commit(
        client, branch, auto_setup_merge=False
    )
    git_in(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
    git_in(worktree, "push", "origin", "HEAD:refs/heads/dev/2209-old-slug")
    (worktree / "impl.txt").write_text("unpushed work\n", encoding="utf-8")
    git_in(worktree, "add", "impl.txt")
    git_in(worktree, "commit", "-m", "unpushed impl commit")
    _seed_fix_parent_session(client, "parent-session")

    with pytest.raises(RemoteRefUnresolvedError) as excinfo:
        dispatch_fix_agent(
            client=client,
            branch=branch,
            remote_branch="dev/2209-old-slug",
            prompt=_FIX_PROMPT_TEXT,
            label="fix-2209",
            ticket_id="2209",
            lane="default",
            parent="parent-session",
        )

    assert "reported remote branch origin/dev/2209-old-slug does not resolve; " in str(
        excinfo.value
    )
    assert stub_spawn.calls == []


def test_dispatch_fix_agent_absent_reported_branch_falls_back_to_existing_ladder(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """A reported branch that was never pushed costs the #2145 upstream path nothing."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    local_branch = "dev/2145-local"
    remote_branch = "dev/2145-remote"
    _seed_origin_renamed_upstream(client, local_branch, remote_branch)
    _seed_fix_parent_session(client, "parent-session")

    dispatch_fix_agent(
        client=client,
        branch=local_branch,
        remote_branch="dev/never-pushed",
        prompt=_FIX_PROMPT_TEXT,
        label="fix-2145",
        ticket_id="2145",
        lane="default",
        parent="parent-session",
    )

    assert len(stub_spawn.calls) == 1


def test_dispatch_fix_agent_unresolvable_reported_and_templated_ref_raises(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """Nothing resolves at all: the message names every rung that was tried."""
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2209-never-pushed"
    _seed_bare_origin_with_main(client)
    git_in(client.workspace_path, "config", "branch.autoSetupMerge", "false")

    with pytest.raises(RemoteRefUnresolvedError) as excinfo:
        dispatch_fix_agent(
            client=client,
            branch=branch,
            remote_branch="dev/2209-also-never-pushed",
            prompt=_FIX_PROMPT_TEXT,
            label="fix-2209",
            ticket_id="2209",
            lane="default",
            parent="parent-session",
        )

    message = str(excinfo.value)
    assert (
        "reported remote branch origin/dev/2209-also-never-pushed does not resolve; "
        in message
    )
    assert "no upstream configured" in message
    assert f"origin/{branch}" in message
    assert stub_spawn.calls == []


def test_dispatch_fix_agent_names_configured_upstream_when_ladder_exhausts(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    stub_spawn: _SpawnRecorder,
) -> None:
    """The upstream-configured message variant: ``@{u}`` resolves but is stale.

    Default ``autoSetupMerge`` leaves ``@{u} = origin/main``; the branch itself
    was never pushed under any name. The error must name the upstream it
    actually consulted rather than claiming none was configured.
    """
    from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2209-unpushed"
    _seed_worktree_with_unpushed_commit(client, branch)
    _seed_fix_parent_session(client, "parent-session")

    with pytest.raises(RemoteRefUnresolvedError) as excinfo:
        dispatch_fix_agent(
            client=client,
            branch=branch,
            prompt=_FIX_PROMPT_TEXT,
            label="fix-2209",
            ticket_id="2209",
            lane="default",
            parent="parent-session",
        )

    message = str(excinfo.value)
    assert "configured upstream is 'origin/main'" in message
    assert f"origin/{branch} does not resolve either" in message
    assert stub_spawn.calls == []
