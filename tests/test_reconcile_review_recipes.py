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

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    TicketTask,
)
from cw.reconcile.review_recipes import (
    RECIPE_ADDRESS_REVIEW,
    ReviewRecipeCandidate,
    _act_address_review,
    _detect_address_review,
    resolve_review_recipe_enabled,
    run_review_recipes,
)

# Reuse the sibling test helpers rather than re-deriving TicketTask / PrState
# construction: _make_task accepts **kwargs (pr_url / pr_state / session_id /
# client / lane), _pr_state builds a PrState with sensible OPEN defaults.
# _client_with_lanes builds a ClientConfig with the given lanes (reused by the
# resolve-precedence tests below).
from tests.test_pr_hydrate import _pr_state
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
    # P2 performs NO dev-queue mutation: the on-disk snapshot is untouched.
    assert load_dev_queue().tasks == [task]


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
        """Both TicketTask and LaneConfig fail loud on an unrecognized key."""
        with pytest.raises(ValidationError):
            TicketTask(ticket_id="X", client="acme", review_recipes={"bogus": True})
        with pytest.raises(ValidationError):
            LaneConfig(name="default", review_recipes={"bogus": True})


def test_config_reference_documents_review_recipes() -> None:
    """CONFIG_REFERENCE.md documents the review_recipes field + prose section."""
    doc = (
        Path(__file__).resolve().parent.parent / "config" / "CONFIG_REFERENCE.md"
    ).read_text(encoding="utf-8")
    assert "review_recipes" in doc
    assert "Review Recipe Enablement" in doc


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
    task = _cr_task(worktree_path=worktree)
    save_dev_queue(DevQueueStore(tasks=[task]))

    def _assert_taken_recorded(**_kwargs: Any) -> None:
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert any(e.correlation_id == task.ticket_id for e in taken)

    stub_spawn.side_effect = _assert_taken_recorded

    acted = _act_address_review([_candidate_for(task)])

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
    assert "evidence_snapshot" in payload
    # No PR_ACTION_FAILED on the happy path.
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


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

    assert _act_address_review([candidate]) == []
    assert stub_spawn.calls == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


def test_no_self_deadlock_under_dev_queue_lock(
    tmp_config_dir: Path,
    make_git_repo: Any,
    stub_spawn: _SpawnRecorder,
) -> None:
    """The spawn runs OUTSIDE dev_queue_lock() — no self-deadlock.

    The stub acquires dev_queue_lock() at spawn time. Because the act phase
    holds the flock for a consistent READ only and dispatches after releasing
    it, this acquisition succeeds immediately. A re-entrant/nested acquisition
    would self-deadlock (fresh fd + LOCK_EX per call), so the test completing
    (no hang) is the deadlock guard.
    """
    _write_acme_clients_yaml(tmp_config_dir)
    worktree = make_git_repo("no-deadlock")
    task = _cr_task(worktree_path=worktree)
    save_dev_queue(DevQueueStore(tasks=[task]))

    def _acquire_lock(**_kwargs: Any) -> None:
        with dev_queue_lock():
            pass

    stub_spawn.side_effect = _acquire_lock

    assert _act_address_review([_candidate_for(task)]) == [task.ticket_id]


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
        acted = _act_address_review([_candidate_for(task1), _candidate_for(task2)])

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
        assert _act_address_review([_candidate_for(task)]) == []

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
        assert _act_address_review([_candidate_for(task)]) == []

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
        assert _act_address_review([_candidate_for(task)]) == []

    assert stub_spawn.calls == []
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
    failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
    assert len(failed) == 1
    assert failed[0].correlation_id == task.ticket_id
