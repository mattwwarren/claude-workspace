"""Tests for the gate recipes' lockless plan-of-record prefetch (#2545).

``cw.reconcile.gate_plan_probes`` holds the captured plan bodies;
``gate_recipes.capture_plan_probes`` fills it lockless by re-running the
plan detect with the live reader, and the in-lock detect only looks bodies
up, skipping a candidate whose prefetch is missing, mismatched or stale.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time

from cw.config import save_state, sessions_lock
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.gh import FETCH_COMMENTS_TIMEOUT
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    LaneConfig,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import gate_plan_probes
from cw.reconcile.deferred import DeferredReconcileJobs
from cw.reconcile.gate_plan_probes import (
    PLAN_PREFETCH_BUDGET_SECONDS,
    PLAN_PREFETCH_MAX_PER_TICK,
    PLAN_PROBE_MAX_AGE_SECONDS,
    PlanProbes,
    PlanProbeUnavailableError,
    lookup_plan_probe,
)
from cw.reconcile.gate_recipes import (
    RECIPE_AUTO_ADOPT_PLAN,
    GateRecipeCandidate,
    _detect_auto_adopt_plan,
    _plan_of_record_body,
    capture_plan_probes,
    run_gate_recipes,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._lock_invariants import install
from tests._reconcile_helpers import forbidden_plan_fetch, recording_plan_fetch
from tests._worktree_helpers import patch_worktree
from tests.conftest import plan_body, stub_fetch_plan
from tests.test_reconcile_gate_recipes import (
    _GATE_LANES,
    _NOW,
    _PLAN_FP,
    _PLAN_SNAPSHOT,
    _SEAM1_CLIENTS,
    _UNREVIEWED_PLAN_SNAPSHOT,
    _assert_released_unreviewed,
    _config,
    _linear_clients,
    _make_session,
    _make_task,
    _plan_result,
)

# The binding ``stub_fetch_plan`` patches: gate_recipes' own import of the
# tracker read.
_FETCH_TARGET = "cw.reconcile.gate_recipes.fetch_approved_plan_comment"
_OTHER_FP = "b" * 64
_LOGGER = "cw.reconcile.gate_recipes"
# Seconds a stub reader "spends", just under the probe TTL.
_SLOW_READ_SECONDS = PLAN_PROBE_MAX_AGE_SECONDS - 0.5
# Rows seeded for the cap test: two more than the per-tick cap.
_OVER_CAP_ROWS = PLAN_PREFETCH_MAX_PER_TICK + 2


def _plan_task(**kwargs: Any) -> TicketTask:
    return _make_task(**{"stage": Stage.PLAN, **kwargs})


def _plan_state(*sessions_kwargs: dict[str, Any]) -> CwState:
    return CwState(
        sessions=[
            _make_session(last_result=_plan_result(), **kw) for kw in sessions_kwargs
        ]
    )


def _read_body(body: str | None) -> Callable[[TicketTask], str | None]:
    def _read(_task: TicketTask) -> str | None:
        return body

    return _read


def _rows(count: int) -> tuple[list[TicketTask], CwState]:
    """*count* clean plan rows GEN-1.. with their sessions sess-1.."""
    tasks = [
        _plan_task(ticket_id=f"GEN-{i}", session_id=f"sess-{i}")
        for i in range(1, count + 1)
    ]
    state = _plan_state(
        *(
            {"ticket_id": f"GEN-{i}", "session_id": f"sess-{i}"}
            for i in range(1, count + 1)
        )
    )
    return tasks, state


def _lookup_detect(
    state: CwState,
    tasks: list[TicketTask],
    probes: PlanProbes,
    *,
    clients: dict[str, ClientConfig] = _SEAM1_CLIENTS,
) -> list[GateRecipeCandidate]:
    return _detect_auto_adopt_plan(
        state, tasks, clients=clients, config=_config(), body_source=probes.lookup
    )


class TestPlanProbes:
    def test_lookup_returns_the_captured_body(self) -> None:
        probes = PlanProbes()
        task = _plan_task()

        captured = probes.capture(task, _PLAN_FP, read=_read_body("body"))

        assert captured == "body"
        assert probes.lookup(task, _PLAN_FP) == "body"

    def test_captured_none_body_is_a_hit_not_a_miss(self) -> None:
        probes = PlanProbes()
        task = _plan_task()

        probes.capture(task, _PLAN_FP, read=_read_body(None))

        assert probes.lookup(task, _PLAN_FP) is None
        assert probes.captured_keys == frozenset({("acme", "GEN-1")})

    def test_lookup_without_capture_raises(self) -> None:
        with pytest.raises(
            PlanProbeUnavailableError,
            match="no plan prefetch was captured for acme/GEN-1 under this "
            "session and plan fingerprint",
        ):
            PlanProbes().lookup(_plan_task(), _PLAN_FP)

    @pytest.mark.parametrize(
        ("task_kwargs", "fingerprint"),
        [
            ({"client": "beta"}, _PLAN_FP),
            ({"session_id": "sess-2"}, _PLAN_FP),
            ({}, _OTHER_FP),
        ],
        ids=["client", "session_id", "fingerprint"],
    )
    def test_key_binds_claim_identity(
        self, task_kwargs: dict[str, Any], fingerprint: str
    ) -> None:
        probes = PlanProbes()
        probes.capture(_plan_task(), _PLAN_FP, read=_read_body(plan_body()))

        with pytest.raises(PlanProbeUnavailableError, match="no plan prefetch"):
            probes.lookup(_plan_task(**task_kwargs), fingerprint)

    def test_probe_at_max_age_is_stale_and_just_under_is_usable(self) -> None:
        probes = PlanProbes()
        task = _plan_task()
        with freeze_time(_NOW) as clock:
            probes.capture(task, _PLAN_FP, read=_read_body("body"))
            clock.tick(_SLOW_READ_SECONDS)
            assert probes.lookup(task, _PLAN_FP) == "body"
            clock.tick(PLAN_PROBE_MAX_AGE_SECONDS - _SLOW_READ_SECONDS)
            with pytest.raises(
                PlanProbeUnavailableError,
                match=r"the plan prefetch for acme/GEN-1 is unusable at age 120\.0s",
            ):
                probes.lookup(task, _PLAN_FP)

    def test_negative_age_fails_closed(self) -> None:
        probes = PlanProbes()
        task = _plan_task()
        with freeze_time(_NOW) as clock:
            probes.capture(task, _PLAN_FP, read=_read_body("body"))
            clock.move_to(_NOW - timedelta(seconds=1))
            with pytest.raises(PlanProbeUnavailableError, match=r"age -1\.0s"):
                probes.lookup(task, _PLAN_FP)

    def test_captured_at_is_stamped_before_the_live_read(self) -> None:
        """A slow read ages the probe from its start, not its end: stamping
        after the read would leave this probe fresh one second later."""
        probes = PlanProbes()
        task = _plan_task()
        with freeze_time(_NOW) as clock:

            def _slow_read(_task: TicketTask) -> str | None:
                clock.tick(_SLOW_READ_SECONDS)
                return "body"

            probes.capture(task, _PLAN_FP, read=_slow_read)
            assert probes.lookup(task, _PLAN_FP) == "body"
            clock.tick(1)
            with pytest.raises(PlanProbeUnavailableError, match="unusable at age"):
                probes.lookup(task, _PLAN_FP)

    def test_spent_budget_raises_before_reading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr(gate_plan_probes, "monotonic", lambda: clock["now"])
        probes = PlanProbes(budget_seconds=PLAN_PREFETCH_BUDGET_SECONDS)
        clock["now"] = PLAN_PREFETCH_BUDGET_SECONDS
        reads: list[TicketTask] = []

        def _read(task: TicketTask) -> str | None:
            reads.append(task)
            return "body"

        with pytest.raises(
            PlanProbeUnavailableError,
            match="the 30s plan prefetch budget is spent; acme/GEN-1 was not read",
        ):
            probes.capture(_plan_task(), _PLAN_FP, read=_read)

        assert reads == []
        assert probes.budget_seconds == PLAN_PREFETCH_BUDGET_SECONDS

    def test_cap_reached_raises_before_reading(self) -> None:
        probes = PlanProbes(max_captures=2)
        reads: list[str] = []

        def _read(task: TicketTask) -> str | None:
            reads.append(task.ticket_id)
            return None

        probes.capture(_plan_task(ticket_id="GEN-1"), _PLAN_FP, read=_read)
        probes.capture(_plan_task(ticket_id="GEN-2"), _PLAN_FP, read=_read)
        with pytest.raises(
            PlanProbeUnavailableError,
            match="the 2-read per-tick plan prefetch cap is reached; "
            "acme/GEN-3 was not read",
        ):
            probes.capture(_plan_task(ticket_id="GEN-3"), _PLAN_FP, read=_read)

        assert reads == ["GEN-1", "GEN-2"]
        assert probes.captures == 2
        assert probes.max_captures == 2

    def test_failed_read_counts_toward_the_cap_and_propagates(self) -> None:
        probes = PlanProbes(max_captures=1)
        task = _plan_task()

        def _boom(_task: TicketTask) -> str | None:
            msg = "a reader bug"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="a reader bug"):
            probes.capture(task, _PLAN_FP, read=_boom)

        assert probes.captures == 1
        with pytest.raises(PlanProbeUnavailableError, match="cap is reached"):
            probes.capture(task, _PLAN_FP, read=_read_body("body"))
        with pytest.raises(PlanProbeUnavailableError, match="no plan prefetch"):
            probes.lookup(task, _PLAN_FP)

    def test_lookup_plan_probe_none_always_misses(self) -> None:
        with pytest.raises(PlanProbeUnavailableError, match="no plan prefetch"):
            lookup_plan_probe(None)(_plan_task(), _PLAN_FP)

        probes = PlanProbes()
        probes.capture(_plan_task(), _PLAN_FP, read=_read_body("body"))
        assert lookup_plan_probe(probes)(_plan_task(), _PLAN_FP) == "body"

    def test_budget_plus_one_worst_case_read_leaves_headroom_under_the_ttl(
        self,
    ) -> None:
        """The capture budget plus one worst-case live read (two ``gh`` calls
        at ``FETCH_COMMENTS_TIMEOUT`` each) fits under the probe TTL.

        That leaves 30 s of headroom, which is LESS than
        ``DEFAULT_SESSIONS_LOCK_TIMEOUT_S`` (60 s): under worst-case lock
        contention an early probe can still age out before the in-lock detect
        reads it. That is the safe direction -- the candidate skips and is
        retried next tick -- so no constant is tuned to hide it.
        """
        worst_case_read = 2 * FETCH_COMMENTS_TIMEOUT
        assert (
            PLAN_PREFETCH_BUDGET_SECONDS + worst_case_read < PLAN_PROBE_MAX_AGE_SECONDS
        )
        assert PLAN_PREFETCH_MAX_PER_TICK > 0


class TestCaptureMirrorsDetect:
    """Ticket requirement 1: never fetch for a row the locked detect skips."""

    def test_eligible_row_fetches_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(
            _FETCH_TARGET, recording_plan_fetch(calls, body=plan_body())
        )
        task = _plan_task()
        state = _plan_state({})
        probes = PlanProbes()

        capture_plan_probes(
            state, [task], clients=_SEAM1_CLIENTS, config=_config(), probes=probes
        )
        candidates = _lookup_detect(state, [task], probes)

        assert calls == ["GEN-1"]
        assert [c.evidence for c in candidates] == [_PLAN_SNAPSHOT]

    @pytest.mark.parametrize(
        ("row_kwargs", "result_kwargs", "config_kwargs"),
        [
            ({}, {"forbidden_touched": True}, {}),
            ({}, {"forbidden_touched": None}, {}),
            ({}, {"fingerprint": None}, {}),
            ({}, {"fingerprint": "abc123"}, {}),
            ({"scope_hint": "large"}, {}, {}),
            ({"stage": Stage.REVIEW}, {}, {}),
            ({"plan_approved_at": _NOW, "plan_approved_fingerprint": _PLAN_FP}, {}, {}),
            ({"gate_recipe_failed_at": _NOW}, {}, {}),
            ({"status": QueueItemStatus.PENDING}, {}, {}),
            ({"session_id": None}, {}, {}),
            ({"session_id": "ghost"}, {}, {}),
            ({}, {}, {"gate_recipes_enabled": False}),
        ],
        ids=[
            "forbidden-touched",
            "forbidden-touched-missing",
            "no-fingerprint",
            "malformed-fingerprint",
            "scope-hint-large",
            "wrong-stage",
            "draft-already-approved",
            "failure-latched",
            "not-blocked-on-user",
            "no-session-id",
            "session-absent",
            "master-switch-off",
        ],
    )
    def test_skipped_rows_never_fetch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        row_kwargs: dict[str, Any],
        result_kwargs: dict[str, Any],
        config_kwargs: dict[str, Any],
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(
            _FETCH_TARGET, recording_plan_fetch(calls, body=plan_body())
        )
        task = _plan_task(**row_kwargs)
        state = CwState(
            sessions=[_make_session(last_result=_plan_result(**result_kwargs))]
        )
        probes = PlanProbes()

        capture_plan_probes(
            state,
            [task],
            clients=_SEAM1_CLIENTS,
            config=_config(**config_kwargs),
            probes=probes,
        )

        assert calls == []
        assert probes.captures == 0

    def test_recipe_disabled_on_lane_never_fetches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lane gate is checked before the body read (#2545 reorder)."""
        calls: list[str] = []
        monkeypatch.setattr(
            _FETCH_TARGET, recording_plan_fetch(calls, body=plan_body())
        )
        clients = {
            "acme": ClientConfig(
                name="acme",
                workspace_path=Path("/tmp/ws"),
                default_branch="main",
                lanes=[
                    LaneConfig(
                        name="default", gate_recipes={RECIPE_AUTO_ADOPT_PLAN: False}
                    )
                ],
            )
        }

        capture_plan_probes(
            _plan_state({}),
            [_plan_task()],
            clients=clients,
            config=_config(),
            probes=PlanProbes(),
        )

        assert calls == []

    def test_capture_then_lookup_yields_same_candidates_as_live_detect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bodies = {"GEN-1": plan_body(), "GEN-2": None, "GEN-3": plan_body()}

        def _fetch(ticket_id: str, **_k: object) -> str | None:
            return bodies[ticket_id]

        monkeypatch.setattr(_FETCH_TARGET, _fetch)
        tasks, state = _rows(3)
        tasks[2] = _plan_task(
            ticket_id="GEN-3", session_id="sess-3", scope_hint="large"
        )

        def _live(task: TicketTask, _fingerprint: str) -> str | None:
            return _plan_of_record_body(task, _SEAM1_CLIENTS.get(task.client))

        live = _detect_auto_adopt_plan(
            state, tasks, clients=_SEAM1_CLIENTS, config=_config(), body_source=_live
        )
        probes = PlanProbes()
        capture_plan_probes(
            state, tasks, clients=_SEAM1_CLIENTS, config=_config(), probes=probes
        )

        prefetched = _lookup_detect(state, tasks, probes)

        assert prefetched == live
        assert [c.ticket_id for c in prefetched] == ["GEN-1", "GEN-2"]
        assert [c.evidence for c in prefetched] == [
            _PLAN_SNAPSHOT,
            _UNREVIEWED_PLAN_SNAPSHOT,
        ]

    def test_local_fallback_reads_happen_in_capture_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The `.cw/plan.md` fallback and its `git branch --show-current`
        run in the capture pass, never in the lookup detect."""
        from cw.reconcile import gate_recipes

        monkeypatch.setattr(_FETCH_TARGET, forbidden_plan_fetch())
        wt = tmp_path / "wt"
        (wt / ".cw").mkdir(parents=True)
        (wt / ".cw" / "plan.md").write_text(plan_body(), encoding="utf-8")
        patch_worktree(monkeypatch, "worktree_path_for", lambda _c, _b: wt)
        branch_reads: list[Path] = []

        def _branch(path: Path) -> str:
            branch_reads.append(path)
            return "dev/GEN-1"

        patch_worktree(monkeypatch, "_checked_out_branch", _branch)
        local_reads: list[str] = []
        real_local = gate_recipes._local_plan_body

        def _spy_local(task: TicketTask, client_cfg: ClientConfig | None) -> str | None:
            local_reads.append(task.ticket_id)
            return real_local(task, client_cfg)

        monkeypatch.setattr(gate_recipes, "_local_plan_body", _spy_local)
        clients = _linear_clients(tmp_path / "ws")
        task = _plan_task(worktree_path=None)
        state = _plan_state({})
        probes = PlanProbes()

        capture_plan_probes(
            state, [task], clients=clients, config=_config(), probes=probes
        )

        assert local_reads == ["GEN-1"]
        assert branch_reads == [wt]
        local_reads.clear()
        branch_reads.clear()

        candidates = _lookup_detect(state, [task], probes, clients=clients)

        assert local_reads == []
        assert branch_reads == []
        assert [c.evidence for c in candidates] == [_PLAN_SNAPSHOT]


class TestFailClosed:
    """Ticket requirement 3: no in-lock fetch fallback."""

    def test_in_lock_detect_with_no_probe_skips_and_never_fetches(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(_FETCH_TARGET, forbidden_plan_fetch())

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            candidates = _lookup_detect(_plan_state({}), [_plan_task()], PlanProbes())

        assert candidates == []
        assert "no usable plan prefetch for acme/GEN-1" in caplog.text

    def test_changed_fingerprint_between_capture_and_lock_misses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_fetch_plan(monkeypatch, plan_body())
        task = _plan_task()
        probes = PlanProbes()
        capture_plan_probes(
            _plan_state({}),
            [task],
            clients=_SEAM1_CLIENTS,
            config=_config(),
            probes=probes,
        )
        monkeypatch.setattr(_FETCH_TARGET, forbidden_plan_fetch())
        amended = CwState(
            sessions=[_make_session(last_result=_plan_result(fingerprint=_OTHER_FP))]
        )

        assert _lookup_detect(amended, [task], probes) == []

    def test_replaced_session_between_capture_and_lock_misses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_fetch_plan(monkeypatch, plan_body())
        probes = PlanProbes()
        capture_plan_probes(
            _plan_state({}),
            [_plan_task()],
            clients=_SEAM1_CLIENTS,
            config=_config(),
            probes=probes,
        )
        monkeypatch.setattr(_FETCH_TARGET, forbidden_plan_fetch())
        reclaimed = _plan_task(session_id="sess-2")

        assert (
            _lookup_detect(_plan_state({"session_id": "sess-2"}), [reclaimed], probes)
            == []
        )

    def test_stale_probe_skips_the_candidate(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        stub_fetch_plan(monkeypatch, plan_body())
        task = _plan_task()
        state = _plan_state({})
        probes = PlanProbes()
        with freeze_time(_NOW) as clock:
            capture_plan_probes(
                state, [task], clients=_SEAM1_CLIENTS, config=_config(), probes=probes
            )
            clock.tick(PLAN_PROBE_MAX_AGE_SECONDS + 1)
            with caplog.at_level(logging.WARNING, logger=_LOGGER):
                candidates = _lookup_detect(state, [task], probes)

        assert candidates == []
        assert "unusable at age" in caplog.text

    def test_unreviewed_none_body_hit_releases_for_re_review(self) -> None:
        task = _plan_task()
        probes = PlanProbes()
        probes.capture(task, _PLAN_FP, read=_read_body(None))

        candidates = _lookup_detect(_plan_state({}), [task], probes)

        _assert_released_unreviewed(candidates)
        assert candidates[0].evidence["plan_reviewed"] is False

    def test_reviewed_body_hit_carries_marker_versions(self) -> None:
        task = _plan_task()
        probes = PlanProbes()
        probes.capture(task, _PLAN_FP, read=_read_body(plan_body()))

        candidates = _lookup_detect(_plan_state({}), [task], probes)

        assert [c.evidence for c in candidates] == [_PLAN_SNAPSHOT]

    def test_detect_body_source_has_no_default(self) -> None:
        parameter = inspect.signature(_detect_auto_adopt_plan).parameters["body_source"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    def test_run_gate_recipes_plan_probes_has_no_default(self) -> None:
        parameter = inspect.signature(run_gate_recipes).parameters["plan_probes"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


class TestPerTickCap:
    """Ticket requirement 2: the live reads per tick are capped."""

    def test_cap_limits_live_fetches(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(
            _FETCH_TARGET, recording_plan_fetch(calls, body=plan_body())
        )
        tasks, state = _rows(_OVER_CAP_ROWS)
        probes = PlanProbes(max_captures=PLAN_PREFETCH_MAX_PER_TICK)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            capture_plan_probes(
                state, tasks, clients=_SEAM1_CLIENTS, config=_config(), probes=probes
            )
        capture_log = caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            candidates = _lookup_detect(state, tasks, probes)
        lookup_log = caplog.text

        assert calls == [f"GEN-{i}" for i in range(1, PLAN_PREFETCH_MAX_PER_TICK + 1)]
        assert len(candidates) == PLAN_PREFETCH_MAX_PER_TICK
        for skipped in ("GEN-6", "GEN-7"):
            assert (
                f"the 5-read per-tick plan prefetch cap is reached; acme/{skipped}"
                " was not read"
            ) in capture_log
            assert f"no plan prefetch was captured for acme/{skipped}" in lookup_log
        assert "cap is reached" not in lookup_log

    def test_budget_spent_stops_further_fetches(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr(gate_plan_probes, "monotonic", lambda: clock["now"])
        calls: list[str] = []

        def _slow_fetch(ticket_id: str, **_k: object) -> str | None:
            calls.append(ticket_id)
            clock["now"] += PLAN_PREFETCH_BUDGET_SECONDS
            return plan_body()

        monkeypatch.setattr(_FETCH_TARGET, _slow_fetch)
        tasks, state = _rows(3)
        probes = PlanProbes(budget_seconds=PLAN_PREFETCH_BUDGET_SECONDS)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            capture_plan_probes(
                state, tasks, clients=_SEAM1_CLIENTS, config=_config(), probes=probes
            )

        assert calls == ["GEN-1"]
        assert "the 30s plan prefetch budget is spent; acme/GEN-2 was not read" in (
            caplog.text
        )


def _seed_plan_row(tmp_path: Path) -> None:
    write_clients_yaml(
        ClientSpec("acme", tmp_path, default_branch="main", lanes=_GATE_LANES)
    )
    save_dev_queue(DevQueueStore(tasks=[_plan_task()]))
    save_state(_plan_state({}))


def _empty_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make every binary (``gh``, ``git``) deterministically absent."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


class TestNoSubprocessUnderSessionsLock:
    """ADR-0019 invariant 3: the plan read never runs under sessions_lock."""

    def test_run_gate_recipes_with_a_miss_spawns_nothing_under_the_lock(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_plan_row(tmp_path)
        _empty_path(monkeypatch, tmp_path)
        trace = install(monkeypatch, allowlist={})

        with sessions_lock():
            approved = run_gate_recipes(
                now=_NOW,
                config=_config(),
                deferred=DeferredReconcileJobs(),
                plan_probes=PlanProbes(),
            )

        assert trace.subprocess_calls() == ()
        assert approved == []
        row = load_dev_queue().tasks[0]
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
        assert row.stage == Stage.PLAN

    def test_captured_plan_is_consumed_under_the_lock_with_no_subprocess(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hit path, act phase included, runs no subprocess in-lock."""
        _seed_plan_row(tmp_path)
        probes = PlanProbes()
        probes.capture(_plan_task(), _PLAN_FP, read=_read_body(plan_body()))
        _empty_path(monkeypatch, tmp_path)
        trace = install(monkeypatch, allowlist={})
        deferred = DeferredReconcileJobs()

        with sessions_lock():
            approved = run_gate_recipes(
                now=_NOW, config=_config(), deferred=deferred, plan_probes=probes
            )

        assert trace.subprocess_calls() == ()
        assert approved == ["GEN-1"]
        row = load_dev_queue().tasks[0]
        assert row.stage == Stage.IMPL
        assert row.status != QueueItemStatus.BLOCKED_ON_USER
        assert [job.label for job in deferred.post_lock] == [
            f"gate_comment:{RECIPE_AUTO_ADOPT_PLAN}:acme:GEN-1"
        ]

    @pytest.mark.lock_violations_expected("subprocess")
    def test_harness_sees_a_live_plan_read_under_the_lock(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the harness records the old in-lock read, so the two tests
        above are not vacuous. ``gh`` is absent, so the read is deterministic."""
        _empty_path(monkeypatch, tmp_path)
        trace = install(monkeypatch, allowlist={})

        with sessions_lock():
            body = _plan_of_record_body(_plan_task(), None)

        assert body is None
        calls = trace.subprocess_calls()
        assert calls
        assert "cw.gh" in calls[0].cw_modules
