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
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time
from pydantic import ValidationError

from cw.config import dev_queue_lock as _dev_queue_lock_path
from cw.config import load_effective_clients, sessions_lock, sessions_lock_file
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events, record_event
from cw.exceptions import CwError, SessionsLockReentryError
from cw.models import (
    ClientConfig,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    TicketTask,
)
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
    resolve_outbound_consent_allowed,
    resolve_review_recipe_enabled,
    run_review_recipes,
)
from cw.reconcile.review_recipes import (
    _detect_repeat_fire_counts as _real_detect_repeat_fire_counts,
)
from cw.review_strategy import ReviewStrategy

# Reuse the sibling test helpers rather than re-deriving TicketTask / PrState
# construction: _make_task accepts **kwargs (pr_url / pr_state / session_id /
# client / lane), _pr_state builds a PrState with sensible OPEN defaults.
# _client_with_lanes builds a ClientConfig with the given lanes (reused by the
# resolve-precedence tests below).
from tests.test_pr_hydrate import _pr_state, _watched
from tests.test_reconcile_gate_recipes import _client_with_lanes, _make_task

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

    monkeypatch.setattr("cw.dev_queue.add_ticket", lambda _t: True)

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
        acted = _act_auto_fix_ci([_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")])

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


def test_act_auto_fix_ci_calls_add_ticket_and_dispatch_once(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(
        pr_url=_PR_URL,
        pr_state=_pr_state(
            attention_state="ci_failing", failing_checks=["lint", "mypy"]
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    store_before = load_dev_queue()
    added: list[TicketTask] = []
    dispatched: list[dict[str, Any]] = []

    def _fake_add_ticket(t: TicketTask) -> bool:
        # emit-before-dispatch: PR_ACTION_TAKEN is durable before re-enqueue.
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert any(e.correlation_id == task.ticket_id for e in taken)
        added.append(t)
        return True

    def _fake_dispatch(**kwargs: Any) -> None:
        dispatched.append(kwargs)

    monkeypatch.setattr("cw.dev_queue.add_ticket", _fake_add_ticket)
    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", _fake_dispatch)

    acted = _act_auto_fix_ci([_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")])

    assert acted == [task.ticket_id]
    assert len(added) == 1
    assert added[0].ticket_id == task.ticket_id
    assert added[0].client == task.client
    assert added[0].lane == task.lane
    assert dispatched == [{"once": True, "client": task.client, "emit": None}]
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    # auto_fix_ci's evidence is the failing checks, not the (meaningless-here)
    # review_decision field the address_review recipe uses.
    assert taken[-1].payload["evidence_snapshot"] == {
        "failing_checks": ["lint", "mypy"]
    }
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []
    # The latch is the ONLY mutation this act phase makes to the row.
    after_task = load_dev_queue().tasks[0]
    assert (
        after_task.model_copy(update={"auto_fix_ci_fired_at": None})
        == store_before.tasks[0]
    )


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

    acted = _act_auto_fix_ci([_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")])

    assert acted == []
    assert called == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


def test_act_auto_fix_ci_add_ticket_raises_emits_pr_action_failed(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cw.dev_queue import LaneNotFoundError

    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="ci_failing"))
    save_dev_queue(DevQueueStore(tasks=[task]))

    lane_gone_msg = "lane gone"

    def _boom(_t: TicketTask) -> bool:
        raise LaneNotFoundError(lane_gone_msg)

    monkeypatch.setattr("cw.dev_queue.add_ticket", _boom)
    monkeypatch.setattr(
        "cw.dispatch.run_dispatch_loop",
        lambda **_kw: pytest.fail("dispatch must not run when add_ticket raises"),
    )

    with caplog.at_level("WARNING"):
        acted = _act_auto_fix_ci([_candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")])

    assert acted == []
    taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
    assert any(e.correlation_id == task.ticket_id for e in taken)
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id
    assert "lane gone" in failed[0].payload["error"]


def test_auto_fix_ci_fires_once_per_episode(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_acme_clients_yaml(tmp_config_dir)
    task = _make_task(pr_url=_PR_URL, pr_state=_pr_state(attention_state="ci_failing"))
    save_dev_queue(DevQueueStore(tasks=[task]))
    monkeypatch.setattr("cw.dev_queue.add_ticket", lambda _t: True)
    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", lambda **_kw: None)
    candidate = _candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")

    acted1 = _act_auto_fix_ci([candidate])
    assert acted1 == [task.ticket_id]
    assert load_dev_queue().tasks[0].auto_fix_ci_fired_at is not None

    # Hold the hydrated row at ci_failing across N further ticks (simulating
    # hydration lag, per the ticket's acceptance criterion): detect still
    # yields a candidate every tick, but the latch blocks a re-fire.
    for _ in range(5):
        acted_n = _act_auto_fix_ci([candidate])
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
    monkeypatch.setattr("cw.dev_queue.add_ticket", lambda _t: True)
    monkeypatch.setattr("cw.dispatch.run_dispatch_loop", lambda **_kw: None)
    candidate = _candidate(task, RECIPE_AUTO_FIX_CI, "ci_failing")

    assert _act_auto_fix_ci([candidate]) == [task.ticket_id]

    # Episode ends: hydration moves the PR off ci_failing.
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(attention_state="ready_to_approve")
    save_dev_queue(store)

    # Clear pass runs even with zero candidates.
    assert _act_auto_fix_ci([]) == []
    assert load_dev_queue().tasks[0].auto_fix_ci_fired_at is None

    # Genuine re-entry into ci_failing fires again (episode semantics).
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(attention_state="ci_failing")
    save_dev_queue(store)
    assert _act_auto_fix_ci([candidate]) == [task.ticket_id]


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
        "cw.reconcile.review_recipes.resolve_review_strategy",
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
    assert _act_auto_fix_ci([_orphan_candidate(RECIPE_AUTO_FIX_CI, "ci_failing")]) == []
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

        monkeypatch.setattr("cw.reconcile.review_recipes.read_events", _boom)
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
        )
        attn = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
        assert len(attn) == 1
        assert attn[0].payload["paused_status"] == _REPEAT_FIRE_REASON
        assert attn[0].payload["ticket_id"] == "GEN-1"
        assert attn[0].payload["recipe"] == RECIPE_ADDRESS_REVIEW
        assert attn[0].payload["client"] == "acme"
        assert attn[0].payload["repeat_fire_count"] == 5
        assert attn[0].payload["window_minutes"] == 20

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
        """Tenant A's fire count is unaffected by tenant B's fires on the same id."""
        cfg = _config(review_recipe_repeat_fire_threshold=5)
        counts = {
            ("widgetco", "42", RECIPE_ADDRESS_REVIEW): 4,  # tenant B: 1 short
        }
        _record_pr_action_taken(
            _repeat_fire_payload_base("42", RECIPE_ADDRESS_REVIEW, client="acme"),
            "acme",
            "42",
            RECIPE_ADDRESS_REVIEW,
            config=cfg,
            repeat_fire_counts=counts,
        )
        # Tenant A's own count (absent from `counts`) starts at 0 + this = 1,
        # nowhere near the threshold — tenant B's count must not leak in.
        assert (
            read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
            == []
        )


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
            "cw.reconcile.review_recipes._detect_repeat_fire_counts", _spy
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
