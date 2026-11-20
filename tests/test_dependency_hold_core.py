"""Dependency-hold core rules (GitHub #2321): slot predicate, sentinel field,
routing stamp, and the serve-tick release/latch behaviour."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import cw.dev_queue.hold as hold_module
from cw.auto_dev_result import (
    Blocker,
    parse_depends_on_pr,
    parse_depends_on_pr_ref,
)
from cw.config import load_state, save_state
from cw.dev_queue import (
    DEPENDENCY_HOLD_DISPOSITION,
    hold_ticket,
    load_dev_queue,
    save_dev_queue,
)
from cw.dev_queue.hold import (
    is_operator_hold_on,
    parse_github_pull_url,
    record_hold_event,
    resolve_ticket_pr,
    restamp_unvetted_hold_ref,
)
from cw.dispatch.routing.stage_failure import (
    _dependency_park_pr,
    _stamp_blocked_on_pr,
)
from cw.exceptions import CwError
from cw.models import (
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    ReapPolicy,
    SessionStatus,
    TicketTask,
    WatchedPr,
)
from cw.models.pr_ref import PrRef
from cw.models.tasks.store import occupies_lane_slot
from cw.reconcile.tasks import release_stale_gated_tasks
from cw.reconcile.tasks.stale_gate import _is_variant_b_gate_task
from tests._reconcile_helpers import park_via_routing
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    events_of,
    make_task,
    queue_row,
    seed_queue,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_HOLD = DEPENDENCY_HOLD_DISPOSITION
_REF_11 = PrRef(number=11)
_REF_88 = PrRef(number=88)


def _hold(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": _HOLD,
        "blocked_reason": "dependency_unmerged",
        "blocked_on_pr": 42,
    }
    kwargs.update(overrides)
    return make_task(**kwargs)


class TestSlotPredicate:
    def test_dependency_hold_with_pr_id_frees_its_slot(self) -> None:
        assert occupies_lane_slot(_hold()) is False

    def test_dependency_hold_without_pr_id_keeps_its_slot(self) -> None:
        assert occupies_lane_slot(_hold(blocked_on_pr=None)) is True

    def test_dependency_hold_outside_blocked_on_user_keeps_its_slot(self) -> None:
        running = _hold(status=QueueItemStatus.RUNNING)
        assert occupies_lane_slot(running) is True

    def test_merge_gate_blocked_with_pr_id_still_holds_its_slot(self) -> None:
        gated = _hold(
            disposition="merge_gate_blocked", blocked_reason="prior_pipeline_pr_open"
        )
        assert occupies_lane_slot(gated) is True


class TestDependsOnPrValidator:
    @pytest.mark.parametrize("value", [7, 1])
    def test_accepts_positive_int(self, value: int) -> None:
        blocker = Blocker(
            stage="pre_flight", reason="dependency_unmerged", depends_on_pr=value
        )
        assert blocker.depends_on_pr == value

    @pytest.mark.parametrize("value", [True, False, 0, -3, "7", 7.0, [], {}])
    def test_rejects_non_positive_int(self, value: object) -> None:
        with pytest.raises(ValidationError):
            Blocker(
                stage="pre_flight", reason="dependency_unmerged", depends_on_pr=value
            )

    def test_defaults_to_none(self) -> None:
        blocker = Blocker(stage="pre_flight", reason="dependency_unmerged")
        assert blocker.depends_on_pr is None

    def test_helper_matches_model(self) -> None:
        assert parse_depends_on_pr(7) == 7
        assert parse_depends_on_pr(True) is None
        assert parse_depends_on_pr("7") is None


class TestRoutingStamp:
    def test_depends_on_pr_read_only_for_dependency_reason(self) -> None:
        assert (
            _dependency_park_pr("dependency_unmerged", {"depends_on_pr": 88}) == _REF_88
        )

    def test_depends_on_pr_ignored_for_other_reasons(self) -> None:
        assert _dependency_park_pr("agent_block", {"depends_on_pr": 88}) is None

    def test_unparseable_depends_on_pr_yields_none(self) -> None:
        assert (
            _dependency_park_pr("dependency_unmerged", {"depends_on_pr": True}) is None
        )

    def test_stamp_sets_blocked_on_pr_from_dependency(self) -> None:
        task = make_task()
        _stamp_blocked_on_pr(task, "dependency_unmerged", {}, _REF_88)
        assert task.blocked_on_pr == _REF_88


def _watch(pr_number: int, state: str, client: str = "acme") -> WatchedPr:
    return WatchedPr(
        pr_url=f"https://github.com/foo/bar/pull/{pr_number}",
        repo="foo/bar",
        pr_number=pr_number,
        client=client,
        source="dependency_hold",
        pr_state=PrState(state=state),
    )


class TestServeTickRelease:
    def test_operator_hold_releases_on_merge_under_default_policy(self) -> None:
        seed_queue([_hold(session_id=None)])
        save_dev_queue(
            DevQueueStore(
                tasks=load_dev_queue().tasks,
                watched_prs=[_watch(42, "MERGED")],
            )
        )

        released = release_stale_gated_tasks()

        assert released == ["ACM-1"]
        assert queue_row().status == QueueItemStatus.PENDING
        [event] = events_of(OrchestratorEventType.SESSION_REAP_PROPOSED)
        assert event.payload["proposed_action"] == "release_stale_gate_variant_b"

    def test_worker_park_under_signal_only_only_stamps(self) -> None:
        seed_queue([_hold(session_id="sess-1")])
        save_dev_queue(
            DevQueueStore(
                tasks=load_dev_queue().tasks,
                watched_prs=[_watch(42, "MERGED")],
            )
        )

        release_stale_gated_tasks()

        row = queue_row()
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
        assert row.session_id == "sess-1"
        assert row.stale_gate_detected_at is not None

    def test_closed_unmerged_hold_never_releases_and_latches_one_event(self) -> None:
        seed_queue([_hold(session_id=None)])
        save_dev_queue(
            DevQueueStore(
                tasks=load_dev_queue().tasks,
                watched_prs=[_watch(42, "CLOSED")],
            )
        )

        assert release_stale_gated_tasks() == []
        assert release_stale_gated_tasks() == []

        assert queue_row().status == QueueItemStatus.BLOCKED_ON_USER
        [event] = events_of(OrchestratorEventType.SESSION_REAP_PROPOSED)
        assert event.payload["proposed_action"] == "dependency_hold_pr_closed_unmerged"
        assert event.payload["evidence"] == {"blocked_on_pr": 42}

    def test_unknown_pr_state_fails_closed(self) -> None:
        seed_queue([_hold(session_id=None)])
        save_dev_queue(
            DevQueueStore(
                tasks=load_dev_queue().tasks,
                watched_prs=[
                    WatchedPr(
                        pr_url="https://github.com/foo/bar/pull/42",
                        repo="foo/bar",
                        pr_number=42,
                        client="acme",
                        source="dependency_hold",
                    )
                ],
            )
        )

        assert release_stale_gated_tasks() == []
        assert queue_row().stale_gate_detected_at is None
        assert events_of(OrchestratorEventType.SESSION_REAP_PROPOSED) == []


class TestHoldTicketCore:
    def test_liveness_snapshot_is_read_under_the_queue_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session that goes live before the lock is taken still refuses."""
        task = make_task(status=QueueItemStatus.PENDING, session_id="sess-1")
        seed_queue([task], [bound_session_for(task, status=SessionStatus.COMPLETED)])
        real_lock = hold_module._lock

        @contextlib.contextmanager
        def _revive_then_lock(*, bounded: bool) -> Iterator[None]:
            state = load_state()
            [session] = state.sessions
            session.status = SessionStatus.ACTIVE
            save_state(state)
            with real_lock(bounded=bounded):
                yield

        monkeypatch.setattr(hold_module, "_lock", _revive_then_lock)

        with pytest.raises(CwError, match="close the session first"):
            hold_ticket(ACME_TICKET, ACME, PrRef(number=9), repo="foo/bar")
        assert queue_row().status == QueueItemStatus.PENDING
        assert events_of(OrchestratorEventType.TICKET_HELD) == []

    def test_unchanged_hold_records_no_audit_event(self) -> None:
        seed_queue([_hold(session_id=None, blocked_on_pr=9)])

        result = hold_ticket(ACME_TICKET, ACME, PrRef(number=9), repo="foo/bar")

        assert result.outcome == "unchanged"
        assert events_of(OrchestratorEventType.TICKET_HELD) == []

    def test_record_hold_event_skips_unchanged_outcome(self) -> None:
        seed_queue([_hold(session_id=None, blocked_on_pr=9)])
        result = hold_ticket(ACME_TICKET, ACME, PrRef(number=9), repo="foo/bar")

        record_hold_event(result, "foo/bar")

        assert result.outcome == "unchanged"
        assert events_of(OrchestratorEventType.TICKET_HELD) == []

    def test_moved_hold_records_audit_event_with_previous_pr(self) -> None:
        seed_queue([_hold(session_id=None, blocked_on_pr=9)])

        result = hold_ticket(ACME_TICKET, ACME, _REF_11, repo="foo/bar")
        record_hold_event(result, "foo/bar")

        assert result.outcome == "updated"
        assert queue_row().blocked_on_pr == _REF_11
        assert result.previous_disposition == _HOLD
        assert result.previous_blocked_reason == "dependency_unmerged"
        [held] = events_of(OrchestratorEventType.TICKET_HELD)
        assert held.payload["previous_pr"] == 9
        assert held.payload["previous_disposition"] == _HOLD
        assert held.payload["previous_blocked_reason"] == "dependency_unmerged"
        assert held.payload["pr_number"] == 11

    def test_worker_hold_conversion_records_previous_state(self) -> None:
        seed_queue([_hold(session_id="sess-1", blocked_on_pr=9)])

        result = hold_ticket(ACME_TICKET, ACME, _REF_11, repo="foo/bar")
        record_hold_event(result, "foo/bar")

        assert result.outcome == "updated"
        assert result.converted_from_worker_park is True
        assert result.previous_disposition == _HOLD
        assert result.previous_blocked_reason == "dependency_unmerged"
        [held] = events_of(OrchestratorEventType.TICKET_HELD)
        assert held.payload["previous_disposition"] == _HOLD
        assert held.payload["previous_blocked_reason"] == "dependency_unmerged"
        assert queue_row().session_id is None

    def test_audit_failure_keeps_saved_row_and_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # State first, then event: no compensation. A failed ticket.held write
        # leaves the saved row in place and the error propagates to the caller.
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        result = hold_ticket(ACME_TICKET, ACME, _REF_11, repo="foo/bar")
        error = "unexpected audit failure"

        def fail_unexpectedly(*args: object, **kwargs: object) -> None:
            raise TypeError(error)

        monkeypatch.setattr(hold_module, "record_event", fail_unexpectedly)

        with pytest.raises(TypeError, match=error):
            record_hold_event(result, "foo/bar")

        row = queue_row()
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
        assert row.disposition == DEPENDENCY_HOLD_DISPOSITION
        assert row.blocked_on_pr == _REF_11


def _auto_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cw.reconcile.tasks.stale_gate.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )


def _seed_with_watch(task: TicketTask, state: str) -> None:
    save_dev_queue(DevQueueStore(tasks=[task], watched_prs=[_watch(42, state)]))


class TestWorkerParkReleasePolicy:
    """A worker dependency park (session bound) follows ``reap_policy`` (#2321 R5)."""

    def test_worker_park_under_auto_policy_releases_on_merge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auto_policy(monkeypatch)
        _seed_with_watch(_hold(session_id="sess-1"), "MERGED")

        assert release_stale_gated_tasks() == ["ACM-1"]

        row = queue_row()
        assert row.status == QueueItemStatus.PENDING
        assert row.session_id is None
        [event] = events_of(OrchestratorEventType.SESSION_REAP_PROPOSED)
        assert event.payload["session_id"] == "sess-1"
        assert event.payload["proposed_action"] == "release_stale_gate_variant_b"

    def test_worker_park_under_auto_policy_never_releases_on_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _auto_policy(monkeypatch)
        _seed_with_watch(_hold(session_id="sess-1"), "CLOSED")

        assert release_stale_gated_tasks() == []

        row = queue_row()
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
        assert row.session_id == "sess-1"
        [event] = events_of(OrchestratorEventType.SESSION_REAP_PROPOSED)
        assert event.payload["proposed_action"] == "dependency_hold_pr_closed_unmerged"

    def test_variant_b_park_still_releases_on_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The per-class flag did not widen closed-unmerged handling to Variant B."""
        _auto_policy(monkeypatch)
        _seed_with_watch(
            _hold(
                disposition="merge_gate_blocked",
                blocked_reason="prior_pipeline_pr_open",
                session_id="sess-1",
            ),
            "CLOSED",
        )

        assert release_stale_gated_tasks() == ["ACM-1"]
        assert queue_row().status == QueueItemStatus.PENDING


def test_disposition_is_one_object_across_its_three_homes() -> None:
    """#2321 R1: enums defines it; dispositions and the package re-export it."""
    import cw.dev_queue
    from cw.dev_queue import dispositions
    from cw.models import enums

    assert enums.DEPENDENCY_HOLD_DISPOSITION == "dependency_hold"
    assert dispositions.DEPENDENCY_HOLD_DISPOSITION is enums.DEPENDENCY_HOLD_DISPOSITION
    assert cw.dev_queue.DEPENDENCY_HOLD_DISPOSITION is (
        dispositions.DEPENDENCY_HOLD_DISPOSITION
    )


_OR_9 = PrRef(repo="o/r", number=9)


def _other_client_watch(repo: str, pr_number: int) -> WatchedPr:
    return WatchedPr(
        pr_url=f"https://github.com/{repo}/pull/{pr_number}",
        repo=repo,
        pr_number=pr_number,
        client="other",
        source="dependency_hold",
    )


class TestCrossRepoHold:
    """#3634: an explicit ``owner/repo#N`` ref on the hold core."""

    def test_explicit_ref_is_stored(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])

        result = hold_ticket(ACME_TICKET, ACME, _OR_9, repo="o/r")

        assert queue_row().blocked_on_pr == _OR_9
        assert result.outcome == "held"
        assert result.pr_number == 9
        assert result.pr_repo == "o/r"
        assert result.previous_pr is None
        assert result.previous_pr_repo is None
        assert result.via_ticket is None

    def test_same_repo_hold_has_null_pr_repo(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])

        result = hold_ticket(ACME_TICKET, ACME, PrRef(number=9), repo="foo/bar")
        record_hold_event(result, "foo/bar")

        [held] = events_of(OrchestratorEventType.TICKET_HELD)
        assert held.payload["pr_repo"] is None
        assert held.payload["previous_pr_repo"] is None
        assert held.payload["via_ticket"] is None
        assert held.payload["pr_number"] == 9

    def test_move_same_repo_to_cross_repo_is_updated(self) -> None:
        seed_queue([_hold(session_id=None, blocked_on_pr=9)])

        result = hold_ticket(ACME_TICKET, ACME, _OR_9, repo="o/r")
        record_hold_event(result, "o/r")

        assert result.outcome == "updated"
        [held] = events_of(OrchestratorEventType.TICKET_HELD)
        assert held.payload["previous_pr"] == 9
        assert held.payload["previous_pr_repo"] is None
        assert held.payload["pr_repo"] == "o/r"
        assert held.payload["repo"] == "o/r"
        assert queue_row().blocked_on_pr == _OR_9

    def test_move_cross_repo_to_same_repo_names_previous_repo(self) -> None:
        seed_queue([_hold(session_id=None, blocked_on_pr=_OR_9)])

        result = hold_ticket(
            ACME_TICKET, ACME, PrRef(number=9), repo="foo/bar", via_ticket="ACM-2"
        )

        assert result.outcome == "updated"
        assert result.previous_pr == 9
        assert result.previous_pr_repo == "o/r"
        assert result.pr_repo is None
        assert result.via_ticket == "ACM-2"

    def test_is_operator_hold_on_compares_keys(self) -> None:
        task = _hold(session_id=None, blocked_on_pr=_OR_9)
        assert is_operator_hold_on(task, PrRef(repo="O/R", number=9)) is True
        assert is_operator_hold_on(task, PrRef(repo="x/y", number=9)) is False
        assert is_operator_hold_on(task, PrRef(number=9)) is False

    def test_collision_on_same_cross_repo_pr_is_refused(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[make_task(status=QueueItemStatus.PENDING)],
                watched_prs=[_other_client_watch("o/r", 9)],
            )
        )

        with pytest.raises(CwError) as excinfo:
            hold_ticket(ACME_TICKET, ACME, _OR_9, repo="o/r")

        assert str(excinfo.value) == (
            "PR o/r#9 is already watched by client other; refusing to hold ACM-1"
        )
        assert queue_row().status == QueueItemStatus.PENDING

    def test_same_number_in_another_repo_does_not_collide(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[make_task(status=QueueItemStatus.PENDING)],
                watched_prs=[_other_client_watch("x/y", 9)],
            )
        )

        assert hold_ticket(ACME_TICKET, ACME, _OR_9, repo="o/r").outcome == "held"

    def test_same_repo_collision_text_is_unchanged(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[make_task(status=QueueItemStatus.PENDING)],
                watched_prs=[_other_client_watch("foo/bar", 9)],
            )
        )

        with pytest.raises(CwError) as excinfo:
            hold_ticket(ACME_TICKET, ACME, PrRef(number=9), repo="foo/bar")

        assert str(excinfo.value) == (
            "PR #9 in foo/bar is already watched by client other;"
            " refusing to hold ACM-1"
        )


class TestSentinelRef:
    """#3634: ``blocker.depends_on_pr_ref``."""

    def test_parse_accepts_explicit_ref(self) -> None:
        assert parse_depends_on_pr_ref("o/r#5") == "o/r#5"

    @pytest.mark.parametrize("value", [5, "5", "o/r", True])
    def test_parse_rejects(self, value: object) -> None:
        assert parse_depends_on_pr_ref(value) is None

    def test_blocker_accepts_and_rejects(self) -> None:
        blocker = Blocker(
            stage="pre_flight", reason="dependency_unmerged", depends_on_pr_ref="o/r#5"
        )
        assert blocker.depends_on_pr_ref == "o/r#5"
        with pytest.raises(ValidationError):
            Blocker(
                stage="pre_flight",
                reason="dependency_unmerged",
                depends_on_pr_ref="o/r",
            )

    def test_blocker_with_unknown_extra_key_still_parses(self) -> None:
        blocker = Blocker.model_validate(
            {
                "stage": "pre_flight",
                "reason": "dependency_unmerged",
                "depends_on_something_new": "x",
            }
        )
        assert blocker.depends_on_pr_ref is None

    def test_both_ids_set_fails_closed(self) -> None:
        payload = {"depends_on_pr": 5, "depends_on_pr_ref": "o/r#5"}
        assert _dependency_park_pr("dependency_unmerged", payload) is None

    def test_ref_is_read_for_dependency_reason_only(self) -> None:
        payload = {"depends_on_pr_ref": "o/r#5"}
        expected = PrRef(repo="o/r", number=5)
        assert _dependency_park_pr("dependency_unmerged", payload) == expected
        assert _dependency_park_pr("agent_block", payload) is None
        bad = {"depends_on_pr_ref": "o/r"}
        assert _dependency_park_pr("dependency_unmerged", bad) is None

    def test_stamp_sets_explicit_ref(self) -> None:
        task = make_task(status=QueueItemStatus.BLOCKED_ON_USER)
        ref = PrRef(repo="o/r", number=5)
        _stamp_blocked_on_pr(task, "dependency_unmerged", {}, ref)
        assert task.blocked_on_pr == ref

    def test_real_routing_stamps_explicit_ref(self, tmp_path: Path) -> None:
        task = park_via_routing(
            ACME_TICKET,
            ACME,
            tmp_path,
            "blocked",
            {
                "status": "blocked",
                "blocker": {
                    "stage": "s4_finalize",
                    "reason": "dependency_unmerged",
                    "depends_on_pr_ref": "o/r#5",
                },
            },
        )

        assert task.disposition == DEPENDENCY_HOLD_DISPOSITION
        assert task.blocked_on_pr == PrRef(repo="o/r", number=5)
        assert task.session_id is not None


_OR_5 = PrRef(repo="o/r", number=5)


class TestRestampUnvettedHoldRef:
    """#3634 D7: the compare-and-set the stale-dispatch registrar uses."""

    def test_replaces_with_same_repo_ref(self) -> None:
        seed_queue([_hold(session_id="sess-1", blocked_on_pr=_OR_5)])

        assert restamp_unvetted_hold_ref(ACME_TICKET, ACME, _OR_5, PrRef(number=5))

        row = queue_row()
        assert row.blocked_on_pr == PrRef(number=5)
        assert row.disposition == DEPENDENCY_HOLD_DISPOSITION

    def test_none_replacement_parks_awaiting_operator(self) -> None:
        seed_queue([_hold(session_id="sess-1", blocked_on_pr=_OR_5)])

        assert restamp_unvetted_hold_ref(ACME_TICKET, ACME, _OR_5, None)

        row = queue_row()
        assert row.blocked_on_pr is None
        assert row.disposition == "awaiting_operator"
        assert occupies_lane_slot(row) is True
        assert _is_variant_b_gate_task(row) is False

    @pytest.mark.parametrize(
        "overrides",
        [
            {"blocked_on_pr": PrRef(repo="o/r", number=6)},
            {"disposition": "awaiting_operator"},
            {"status": QueueItemStatus.PENDING},
            {"session_id": None},
        ],
    )
    def test_changed_row_is_not_written(self, overrides: dict[str, object]) -> None:
        kwargs: dict[str, object] = {"session_id": "sess-1", "blocked_on_pr": _OR_5}
        kwargs.update(overrides)
        seed_queue([_hold(**kwargs)])
        before = load_dev_queue().model_dump_json()

        assert restamp_unvetted_hold_ref(ACME_TICKET, ACME, _OR_5, None) is False

        assert load_dev_queue().model_dump_json() == before

    def test_missing_row_returns_false(self) -> None:
        seed_queue([])
        assert restamp_unvetted_hold_ref(ACME_TICKET, ACME, _OR_5, None) is False

    def test_takes_queue_lock_and_no_sessions_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([_hold(session_id="sess-1", blocked_on_pr=_OR_5)])
        real_lock = hold_module._lock
        taken: list[str] = []

        @contextlib.contextmanager
        def _recording_lock(**kwargs: object) -> Iterator[None]:
            taken.append("dev_queue_lock")
            with real_lock():
                yield

        def _no_sessions_lock(**kwargs: object) -> None:
            msg = "sessions_lock must not be taken"
            raise AssertionError(msg)

        monkeypatch.setattr(hold_module, "_lock", _recording_lock)
        monkeypatch.setattr(hold_module, "sessions_lock", _no_sessions_lock)

        assert restamp_unvetted_hold_ref(ACME_TICKET, ACME, _OR_5, None)
        assert taken == ["dev_queue_lock"]


class TestStrictPullUrl:
    """#3634: the recorded url must be a github.com pull url, not a lookalike."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/o/r/pull/5", ("o/r", 5)),
            ("http://github.com/o/r/pull/5/", ("o/r", 5)),
            ("https://GitHub.com/Foo/Bar-x.y/pull/12", ("Foo/Bar-x.y", 12)),
            ("https://github.com/o/r/pull/5?x=1#issuecomment-2", ("o/r", 5)),
        ],
    )
    def test_accepts_a_real_pull_url(self, url: str, expected: tuple[str, int]) -> None:
        assert parse_github_pull_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://notgithub.com/other/repo/pull/5",
            "https://github.com.evil.example/o/r/pull/5",
            "https://example.com/github.com/o/r/pull/5",
            "https://evil.example/?u=https://github.com/o/r/pull/5",
            "https://user@github.com/o/r/pull/5",
            "https://user:pw@github.com/o/r/pull/5",
            "https://github.com:8443/o/r/pull/5",
            "https://github.com:notaport/o/r/pull/5",
            "ftp://github.com/o/r/pull/5",
            "github.com/o/r/pull/5",
            "https://github.com/o/r/pull/5abc",
            "https://github.com/o/r/pull/05",
            "https://github.com/o/r/pull/0",
            "https://github.com/o/r/pull/5/files",
            "https://github.com/o/r/issues/5",
            "https://github.com/o/pull/5",
            "https://github.com//r/pull/5",
            "",
        ],
    )
    def test_rejects_a_lookalike_or_malformed_url(self, url: str) -> None:
        assert parse_github_pull_url(url) is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://notgithub.com/other/repo/pull/5",
            "https://github.com.evil.example/o/r/pull/5",
            "https://example.com/github.com/o/r/pull/5",
            "https://github.com/o/r/pull/5abc",
        ],
    )
    def test_resolve_ticket_pr_refuses_a_lookalike(self, url: str) -> None:
        dep = make_task(ticket_id="ACM-2", pr_url=url)
        store = DevQueueStore(tasks=[make_task(), dep])

        with pytest.raises(CwError, match="is not a github pull request url"):
            resolve_ticket_pr(store, "ACM-2", ACME_TICKET, ACME)

    def test_resolve_ticket_pr_accepts_a_real_url(self) -> None:
        dep = make_task(ticket_id="ACM-2", pr_url="https://github.com/o/r/pull/5")
        store = DevQueueStore(tasks=[make_task(), dep])

        assert resolve_ticket_pr(store, "ACM-2", ACME_TICKET, ACME) == (
            PrRef(repo="o/r", number=5),
            "ACM-2",
        )
