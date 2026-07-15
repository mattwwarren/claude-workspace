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
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cw.config import dev_queue_lock as _dev_queue_lock_path
from cw.config import load_effective_clients
from cw.dev_queue import load_dev_queue, save_dev_queue
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
    RECIPE_AUTO_FIX_CI,
    RECIPE_ESCALATE_MERGE_BLOCK,
    RECIPE_REQUEST_REVIEWER,
    ReviewRecipeCandidate,
    _act_address_review,
    _act_auto_fix_ci,
    _act_escalate_merge_block,
    _act_request_reviewer,
    _detect_address_review,
    _detect_auto_fix_ci,
    _detect_escalate_merge_block,
    _detect_request_reviewer,
    resolve_review_recipe_enabled,
    run_review_recipes,
)
from cw.review_strategy import ReviewStrategy

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
    # Resolution 6 (no dev-queue mutation): the on-disk snapshot is untouched.
    assert load_dev_queue().tasks == [task]


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
    # Resolution 6 (no dev-queue mutation): the on-disk snapshot is untouched,
    # including for the candidate whose dispatch failed.
    assert load_dev_queue().tasks == [task1, task2]


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
