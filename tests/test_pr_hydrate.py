"""Tests for cw.pr_hydrate — PR-state hydration in the serve tick (#929)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from freezegun import freeze_time

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    PrState,
    TicketTask,
)
from cw.pr_hydrate import (
    _compute_attention_state,
    _diff_transitions,
    _parse_pr_url,
    _summarize_status_checks,
    hydrate_pr_states,
)

if TYPE_CHECKING:
    import pytest

_URL = "https://github.com/acme/widgets/pull/42"


def _checkrun(status: str, conclusion: str = "", name: str = "check") -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "status": status,
        "conclusion": conclusion,
        "name": name,
        "workflowName": "wf",
        "detailsUrl": "https://ci/1",
    }


def _pr_view_payload(**fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
        "reviewDecision": "",
        "isDraft": False,
        "reviewRequests": [],
    }
    payload.update(fields)
    return payload


class TestSummarizeStatusChecks:
    def test_empty_rollup_is_ok(self) -> None:
        result = _summarize_status_checks([])
        assert result == {"failing": [], "pending_count": 0, "ok": True}

    def test_failed_checkrun_is_not_ok(self) -> None:
        rollup = [_checkrun("COMPLETED", "FAILURE", name="lint")]
        result = _summarize_status_checks(rollup)
        assert result["ok"] is False
        assert result["failing"][0]["name"] == "lint"
        assert result["failing"][0]["conclusion"] == "FAILURE"

    def test_pending_checkrun_does_not_block_ok(self) -> None:
        rollup = [_checkrun("IN_PROGRESS", name="build")]
        result = _summarize_status_checks(rollup)
        assert result["ok"] is True
        assert result["pending_count"] == 1

    def test_passing_checkrun_is_ok(self) -> None:
        rollup = [_checkrun("COMPLETED", "SUCCESS", name="test")]
        result = _summarize_status_checks(rollup)
        assert result["ok"] is True
        assert result["failing"] == []

    def test_legacy_status_context_failure(self) -> None:
        rollup = [{"__typename": "StatusContext", "state": "FAILURE", "context": "ci"}]
        result = _summarize_status_checks(rollup)
        assert result["ok"] is False
        assert result["failing"][0]["name"] == "ci"

    def test_legacy_status_context_pending(self) -> None:
        rollup = [{"__typename": "StatusContext", "state": "PENDING", "context": "ci"}]
        result = _summarize_status_checks(rollup)
        assert result["ok"] is True
        assert result["pending_count"] == 1


class TestAttentionState:
    def test_row0_draft_returns_none(self) -> None:
        # Draft gates the entire function even with a blocking merge state.
        assert (
            _compute_attention_state(
                ci_ok=False,
                merge_state_status="DIRTY",
                review_decision="CHANGES_REQUESTED",
                is_draft=True,
                reviewer_count=0,
            )
            is None
        )

    def test_row1_merge_blocked_dirty(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                merge_state_status="DIRTY",
                review_decision="",
                is_draft=False,
                reviewer_count=1,
            )
            == "merge_blocked"
        )

    def test_row1_merge_blocked_behind(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                merge_state_status="BEHIND",
                review_decision="",
                is_draft=False,
                reviewer_count=1,
            )
            == "merge_blocked"
        )

    def test_row2_ci_failing(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=False,
                merge_state_status="CLEAN",
                review_decision="",
                is_draft=False,
                reviewer_count=1,
            )
            == "ci_failing"
        )

    def test_row3_changes_requested(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                merge_state_status="CLEAN",
                review_decision="CHANGES_REQUESTED",
                is_draft=False,
                reviewer_count=1,
            )
            == "changes_requested"
        )

    def test_row4_no_reviewer(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                merge_state_status="CLEAN",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=0,
            )
            == "no_reviewer"
        )

    def test_row4_review_required_with_reviewer_is_not_no_reviewer(self) -> None:
        # REVIEW_REQUIRED but a reviewer is assigned -> falls through to default.
        assert (
            _compute_attention_state(
                ci_ok=True,
                merge_state_status="CLEAN",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=2,
            )
            == "ready_to_approve"
        )

    def test_row0_gates_row4_draft_zero_reviewers(self) -> None:
        # Explicit amendment case: draft + zero reviewers + REVIEW_REQUIRED -> None,
        # NOT no_reviewer (row 0 gates row 4).
        assert (
            _compute_attention_state(
                ci_ok=True,
                merge_state_status="CLEAN",
                review_decision="REVIEW_REQUIRED",
                is_draft=True,
                reviewer_count=0,
            )
            is None
        )

    def test_same_inputs_non_draft_returns_no_reviewer(self) -> None:
        # Same inputs as the gated case but non-draft -> no_reviewer.
        assert (
            _compute_attention_state(
                ci_ok=True,
                merge_state_status="CLEAN",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=0,
            )
            == "no_reviewer"
        )

    def test_row5_blocked_ready_to_approve(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                merge_state_status="BLOCKED",
                review_decision="APPROVED",
                is_draft=False,
                reviewer_count=1,
            )
            == "ready_to_approve"
        )

    def test_row6_default_ready_to_approve(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                merge_state_status="CLEAN",
                review_decision="APPROVED",
                is_draft=False,
                reviewer_count=1,
            )
            == "ready_to_approve"
        )


class TestParsePrUrl:
    def test_parses_owner_repo_and_number(self) -> None:
        assert _parse_pr_url(_URL) == ("acme/widgets", 42)

    def test_returns_none_for_garbage(self) -> None:
        assert _parse_pr_url("not-a-url") is None

    def test_returns_none_for_non_pull_url(self) -> None:
        assert _parse_pr_url("https://github.com/acme/widgets/issues/42") is None


def _pr_state(**fields: Any) -> PrState:
    defaults: dict[str, Any] = {
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "ci_ok": True,
        "review_decision": "",
        "attention_state": "ready_to_approve",
    }
    defaults.update(fields)
    return PrState(**defaults)


_BASE = {
    "repo": "acme/widgets",
    "pr_number": 42,
    "ticket_id": "GEN-9",
    "client": "acme",
}


class TestTransitions:
    def test_merged_transition_emits_base_payload(self) -> None:
        old = _pr_state(state="OPEN")
        new = _pr_state(state="MERGED")
        events = _diff_transitions(old, new, base=dict(_BASE))
        types = {t for t, _ in events}
        assert OrchestratorEventType.PR_MERGED in types
        payload = next(p for t, p in events if t == OrchestratorEventType.PR_MERGED)
        assert payload == _BASE

    def test_merged_first_seen_emits(self) -> None:
        new = _pr_state(state="MERGED")
        events = _diff_transitions(None, new, base=dict(_BASE))
        assert OrchestratorEventType.PR_MERGED in {t for t, _ in events}

    def test_ci_failed_transition_true_to_false(self) -> None:
        old = _pr_state(ci_ok=True)
        new = _pr_state(ci_ok=False, failing_checks=["lint", "test-unit"])
        events = _diff_transitions(old, new, base=dict(_BASE))
        payload = next(p for t, p in events if t == OrchestratorEventType.PR_CI_FAILED)
        assert payload == {**_BASE, "failing_checks": ["lint", "test-unit"]}

    def test_ci_failed_not_emitted_without_prior_true(self) -> None:
        # First-seen failing (old is None) does not emit ci_failed.
        new = _pr_state(ci_ok=False, failing_checks=["lint"])
        events = _diff_transitions(None, new, base=dict(_BASE))
        assert OrchestratorEventType.PR_CI_FAILED not in {t for t, _ in events}

    def test_ci_failed_deduped_when_still_failing(self) -> None:
        old = _pr_state(ci_ok=False, failing_checks=["lint"])
        new = _pr_state(ci_ok=False, failing_checks=["lint"])
        events = _diff_transitions(old, new, base=dict(_BASE))
        assert OrchestratorEventType.PR_CI_FAILED not in {t for t, _ in events}

    def test_review_received_on_change(self) -> None:
        old = _pr_state(review_decision="REVIEW_REQUIRED")
        new = _pr_state(review_decision="CHANGES_REQUESTED")
        events = _diff_transitions(old, new, base=dict(_BASE))
        payload = next(
            p for t, p in events if t == OrchestratorEventType.PR_REVIEW_RECEIVED
        )
        assert payload == {**_BASE, "review_decision": "CHANGES_REQUESTED"}

    def test_review_received_deduped_when_unchanged(self) -> None:
        old = _pr_state(review_decision="APPROVED")
        new = _pr_state(review_decision="APPROVED")
        events = _diff_transitions(old, new, base=dict(_BASE))
        assert OrchestratorEventType.PR_REVIEW_RECEIVED not in {t for t, _ in events}

    def test_mergeable_transition_into_clean(self) -> None:
        old = _pr_state(merge_state_status="DIRTY")
        new = _pr_state(merge_state_status="CLEAN")
        events = _diff_transitions(old, new, base=dict(_BASE))
        payload = next(p for t, p in events if t == OrchestratorEventType.PR_MERGEABLE)
        assert payload == {**_BASE, "mergeStateStatus": "CLEAN"}

    def test_mergeable_not_emitted_when_already_clean(self) -> None:
        old = _pr_state(merge_state_status="CLEAN")
        new = _pr_state(merge_state_status="CLEAN")
        events = _diff_transitions(old, new, base=dict(_BASE))
        assert OrchestratorEventType.PR_MERGEABLE not in {t for t, _ in events}


class TestCandidateSelection:
    def test_hydrates_task_with_pr_url_and_no_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="OPEN"),
        )
        hydrate_pr_states(OrchestratorConfig())
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.state == "OPEN"

    def test_skips_task_without_pr_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_dev_queue(
            DevQueueStore(tasks=[TicketTask(ticket_id="GEN-1", client="acme")])
        )
        calls = []
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *a, **_k: calls.append(a) or _pr_view_payload(),
        )
        hydrate_pr_states(OrchestratorConfig())
        assert calls == []
        assert load_dev_queue().tasks[0].pr_state is None

    def test_skips_terminal_pr_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=PrState(
                            state="MERGED",
                            hydrated_at=datetime(2000, 1, 1, tzinfo=UTC),
                        ),
                    )
                ]
            )
        )
        calls = []
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *a, **_k: calls.append(a) or _pr_view_payload(),
        )
        hydrate_pr_states(OrchestratorConfig())
        assert calls == []


class TestThrottle:
    def test_second_pass_within_interval_is_throttled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
            )
        )
        calls: list[Any] = []
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *a, **_k: calls.append(a) or _pr_view_payload(state="OPEN"),
        )
        config = OrchestratorConfig(pr_hydration_interval_seconds=150)
        with freeze_time("2026-07-04 12:00:00") as frozen:
            hydrate_pr_states(config)
            assert len(calls) == 1
            frozen.tick(delta=timedelta(seconds=60))
            hydrate_pr_states(config)
            assert len(calls) == 1  # throttled — no second fetch
            frozen.tick(delta=timedelta(seconds=120))
            hydrate_pr_states(config)
            assert len(calls) == 2  # interval elapsed — fetched again

    def test_hydrated_at_stamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="OPEN"),
        )
        with freeze_time("2026-07-04 12:00:00"):
            hydrate_pr_states(OrchestratorConfig())
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.hydrated_at == datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


class TestTransientFailure:
    def test_fetch_none_leaves_prior_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prior = PrState(
            state="OPEN",
            merge_state_status="CLEAN",
            hydrated_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1", client="acme", pr_url=_URL, pr_state=prior
                    )
                ]
            )
        )
        monkeypatch.setattr("cw.pr_hydrate.fetch_pr_view", lambda *_a, **_kw: None)
        hydrate_pr_states(OrchestratorConfig())
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.hydrated_at == datetime(2000, 1, 1, tzinfo=UTC)


class TestHydrateEmitsEvents:
    def test_merged_emits_pr_merged_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=PrState(
                            state="OPEN",
                            hydrated_at=datetime(2000, 1, 1, tzinfo=UTC),
                        ),
                    )
                ]
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="MERGED"),
        )
        hydrate_pr_states(OrchestratorConfig())
        events = read_events(event_types=[OrchestratorEventType.PR_MERGED])
        assert len(events) == 1
        assert events[0].payload == _BASE | {"ticket_id": "GEN-1"}
        assert events[0].payload["repo"] == "acme/widgets"
        assert events[0].payload["pr_number"] == 42
        assert events[0].correlation_id == "GEN-1"


class TestMultiTaskRouting:
    """Two candidates in the same pass route to the correct task, no cross-talk."""

    _URL_A = "https://github.com/acme/widgets/pull/1"
    _URL_B = "https://github.com/acme/gadgets/pull/2"

    def test_two_candidates_fetch_correct_urls_and_persist_to_correct_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(ticket_id="GEN-A", client="acme", pr_url=self._URL_A),
                    TicketTask(ticket_id="GEN-B", client="acme", pr_url=self._URL_B),
                ]
            )
        )

        def _fake_fetch(pr_ref: str, **_kw: object) -> dict[str, Any]:
            if pr_ref == self._URL_A:
                return _pr_view_payload(state="OPEN", reviewDecision="APPROVED")
            if pr_ref == self._URL_B:
                return _pr_view_payload(state="OPEN", mergeStateStatus="DIRTY")
            msg = f"unexpected pr_ref: {pr_ref}"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.pr_hydrate.fetch_pr_view", _fake_fetch)
        hydrate_pr_states(OrchestratorConfig())

        tasks_by_id = {t.ticket_id: t for t in load_dev_queue().tasks}
        state_a = tasks_by_id["GEN-A"].pr_state
        state_b = tasks_by_id["GEN-B"].pr_state
        assert state_a is not None
        assert state_b is not None
        assert state_a.review_decision == "APPROVED"
        assert state_a.merge_state_status == "CLEAN"
        assert state_b.merge_state_status == "DIRTY"
        assert state_b.review_decision == ""

    def test_two_candidates_emit_independent_transitions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-A",
                        client="acme",
                        pr_url=self._URL_A,
                        pr_state=PrState(
                            state="OPEN", hydrated_at=datetime(2000, 1, 1, tzinfo=UTC)
                        ),
                    ),
                    TicketTask(
                        ticket_id="GEN-B",
                        client="acme",
                        pr_url=self._URL_B,
                        pr_state=PrState(
                            state="OPEN", hydrated_at=datetime(2000, 1, 1, tzinfo=UTC)
                        ),
                    ),
                ]
            )
        )

        def _fake_fetch(pr_ref: str, **_kw: object) -> dict[str, Any]:
            if pr_ref == self._URL_A:
                return _pr_view_payload(state="MERGED")
            return _pr_view_payload(state="OPEN")

        monkeypatch.setattr("cw.pr_hydrate.fetch_pr_view", _fake_fetch)
        hydrate_pr_states(OrchestratorConfig())

        events = read_events(event_types=[OrchestratorEventType.PR_MERGED])
        assert len(events) == 1
        assert events[0].correlation_id == "GEN-A"
        assert events[0].payload["repo"] == "acme/widgets"
