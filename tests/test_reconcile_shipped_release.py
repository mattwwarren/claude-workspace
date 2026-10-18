"""Tests for cw.reconcile.shipped_release (#2715, Sweep S).

The #2524 stranded routed-result session -- a live EMIT_CLI DAEMON session a
#2458 partial route already consumed, with no lane-occupying row bound -- is
closed once its recorded result is ``shipped``, its own PR is OPEN or MERGED,
its drain stamp is readable, its own and old, and its transcript is flat past
the stage's bucket. Every age check only postpones that evidence-triggered act.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

from cw._lock_guard import LockRank, is_rank_held
from cw.config import load_state, save_state, sessions_lock
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dispatch.loop import consume_completed_sessions
from cw.events import read_events
from cw.gh import BranchPr
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    LastResultSource,
    LivenessBucket,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
    counts_toward_client_ceiling,
)
from cw.reconcile import reconcile, shipped_release
from cw.reconcile._shared import _is_headless
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.idle._detect import _background_work_still_draining
from cw.reconcile.routed_result_sessions import (
    ROUTED_RESULT_STRANDED_REASON,
    find_stranded_routed_sessions,
)
from cw.reconcile.shipped_release import (
    SHIPPED_PR_MAX_PER_TICK,
    ShippedPrProbes,
    ShippedPrProbeUnavailableError,
    capture_shipped_pr_probes,
    detect_shipped_release,
    run_shipped_release,
    select_own_pr,
)
from tests._clients_yaml import write_clients_yaml
from tests._reconcile_helpers import (
    SHIPPED_SENTINEL_PR_URL,
    SHIPPED_SURFACE_REF,
    LockProbeDaemon,
    _routed_last_result,
    _shipped_salvage_payload,
    _write_agent_spawn_stamp,
    fake_branch_pr,
    seed_stranded_shipped_session,
    use_reconcile_daemon,
    write_unreadable_claim_context,
)

_SID = "2715"
_CLIENT = "client-a"
_LIVE = {SHIPPED_SURFACE_REF}
_MERGED_AT = "2026-10-18T03:33:58Z"
_ADR = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "adr"
    / "0006-reaping-is-gated-by-an-authority.md"
)


@pytest.fixture
def clients(tmp_path: Path) -> dict[str, ClientConfig]:
    return {
        _CLIENT: ClientConfig(
            name=_CLIENT,
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "worktrees",
        )
    }


def _row(
    *,
    status: QueueItemStatus = QueueItemStatus.PENDING,
    stage: Stage = Stage.REVIEW,
    session_id: str | None = None,
    ticket_id: str = _SID,
    disposition: str | None = None,
) -> TicketTask:
    """The ticket's advanced row (default: an unbound PENDING review row)."""
    return TicketTask(
        ticket_id=ticket_id,
        client=_CLIENT,
        status=status,
        stage=stage,
        session_id=session_id,
        disposition=disposition,
    )


def _probes(
    sessions: list[Session], prs: tuple[BranchPr, ...] | None
) -> ShippedPrProbes:
    """Probes captured for *sessions*, each answering *prs* (None = gh outage)."""
    probes = ShippedPrProbes()
    for sess in sessions:
        probes.capture((sess.client, sess.id, sess.id), read=lambda: prs)
    return probes


def _open_probes(sessions: list[Session]) -> ShippedPrProbes:
    return _probes(sessions, (fake_branch_pr(),))


def _detect(
    sessions: list[Session],
    *,
    now: datetime,
    tasks: list[TicketTask] | None = None,
    probes: ShippedPrProbes | None = None,
    clients: dict[str, ClientConfig],
    native_live: set[str] | None = None,
    config: OrchestratorConfig | None = None,
) -> list[shipped_release.ShippedReleaseCandidate]:
    return detect_shipped_release(
        CwState(sessions=sessions),
        [_row()] if tasks is None else tasks,
        native_live=_LIVE if native_live is None else native_live,
        clients=clients,
        probes=_open_probes(sessions) if probes is None else probes,
        now=now,
        config=config or OrchestratorConfig(),
    )


def _still_paged(
    sessions: list[Session],
    *,
    now: datetime,
    tasks: list[TicketTask] | None = None,
    config: OrchestratorConfig | None = None,
) -> bool:
    """The #2524 detector still finds the session (its page is unchanged)."""
    return bool(
        find_stranded_routed_sessions(
            CwState(sessions=sessions),
            [_row()] if tasks is None else tasks,
            now=now,
            native_live=_LIVE,
            config=config or OrchestratorConfig(),
        )
    )


class TestDetectCandidates:
    def test_open_own_pr_is_a_candidate(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)

        found = _detect([sess], now=now, clients=clients)

        assert len(found) == 1
        candidate = found[0]
        assert (candidate.session_id, candidate.ticket_id, candidate.client) == (
            _SID,
            _SID,
            _CLIENT,
        )
        assert candidate.evidence == shipped_release.ShippedPrEvidence(
            number=99, url=SHIPPED_SENTINEL_PR_URL, state="OPEN", merged_at=None
        )

    def test_merged_own_pr_carries_merged_at(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        probes = _probes(
            [sess], (fake_branch_pr(state="MERGED", merged_at=_MERGED_AT),)
        )

        found = _detect([sess], now=now, clients=clients, probes=probes)

        assert [c.evidence.merged_at for c in found] == [_MERGED_AT]

    def test_headless_marker_is_irrelevant(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)

        assert not _is_headless(sess)
        assert len(_detect([sess], now=now, clients=clients)) == 1

    def test_idle_status_is_a_candidate(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(
            tmp_path, now=now, status=SessionStatus.IDLE
        )

        assert len(_detect([sess], now=now, clients=clients)) == 1

    @pytest.mark.parametrize(
        "tasks",
        [
            [_row(status=QueueItemStatus.COMPLETED, session_id=_SID)],
            [],
        ],
        ids=["only-a-completed-bound-row", "no-row"],
    )
    def test_completed_row_or_no_row(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        tasks: list[TicketTask],
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now, stale_minutes=50)

        assert len(_detect([sess], now=now, clients=clients, tasks=tasks)) == 1


def _with(**updates: object) -> dict[str, object]:
    return updates


class TestDetectSkips:
    @pytest.mark.parametrize(
        ("updates", "paged"),
        [
            (_with(last_result=_routed_last_result()), True),
            (
                _with(
                    last_result={
                        **_routed_last_result(),
                        **_shipped_salvage_payload(_SID),
                        "sentinel_advance_refused": True,
                    }
                ),
                False,
            ),
            (_with(last_result=_shipped_salvage_payload(_SID)), False),
            (_with(last_result_source=LastResultSource.STOP_HOOK_HARVEST), True),
            (_with(status=SessionStatus.COMPLETED), False),
            (_with(purpose=SessionPurpose.ORCHESTRATE), False),
            (_with(origin=SessionOrigin.USER), False),
            (_with(surface_ref=None), False),
            (_with(name="client-a/no-ticket-here"), False),
        ],
        ids=[
            "consumed-stage-complete",
            "refusal-latched",
            "unconsumed-emit-result",
            "non-emit-cli-result",
            "completed",
            "orchestrate",
            "user-origin",
            "no-surface",
            "no-ticket-id",
        ],
    )
    def test_session_shape_skips(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        updates: dict[str, object],
        paged: bool,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        for field, value in updates.items():
            setattr(sess, field, value)

        assert _detect([sess], now=now, clients=clients) == []
        assert _still_paged([sess], now=now) is paged

    def test_roster_absent_surface_is_phantoms(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)

        assert _detect([sess], now=now, clients=clients, native_live=set()) == []

    @pytest.mark.parametrize(
        "row",
        [
            _row(status=QueueItemStatus.RUNNING, session_id=_SID),
            _row(
                status=QueueItemStatus.BLOCKED_ON_USER,
                session_id=_SID,
                disposition="finalize_gate_held",
            ),
            _row(status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, session_id=_SID),
        ],
        ids=["running", "finalize-gate-held", "awaiting-signoff"],
    )
    def test_pinned_session_is_skipped(
        self, tmp_path: Path, clients: dict[str, ClientConfig], row: TicketTask
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)

        assert _detect([sess], now=now, clients=clients, tasks=[row]) == []

    def test_client_outside_scope(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        other = {"other": ClientConfig(name="other", workspace_path=tmp_path / "other")}

        assert _detect([sess], now=now, clients=other) == []

    @pytest.mark.parametrize(
        "prs",
        [
            (fake_branch_pr(url="https://github.com/foo/bar/pull/100"),),
            (fake_branch_pr(state="CLOSED"),),
            (),
            None,
        ],
        ids=["another-pr-on-the-branch", "own-pr-closed", "no-pr", "gh-outage"],
    )
    def test_no_own_open_or_merged_pr(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        prs: tuple[BranchPr, ...] | None,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)

        found = _detect([sess], now=now, clients=clients, probes=_probes([sess], prs))

        assert found == []
        assert _still_paged([sess], now=now)

    def test_missing_probe_waits_a_tick(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)

        assert _detect([sess], now=now, clients=clients, probes=ShippedPrProbes()) == []

    def test_stale_probe_waits_a_tick(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        with freeze_time(now - timedelta(minutes=5)):
            probes = _open_probes([sess])

        assert _detect([sess], now=now, clients=clients, probes=probes) == []

    def test_reclaimed_session_is_a_probe_miss(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        probes = ShippedPrProbes()
        probes.capture(
            (_CLIENT, _SID, "older-session"), read=lambda: (fake_branch_pr(),)
        )

        assert _detect([sess], now=now, clients=clients, probes=probes) == []


def _write_context_json(worktree: Path, payload: object) -> None:
    """Write *payload* verbatim as the worktree's cw-context.json."""
    write_unreadable_claim_context(worktree, json.dumps(payload))


class TestDrainSignalMatrix:
    """S fails closed on the drain stamp; the #2524 page is unchanged."""

    @pytest.mark.parametrize(
        "context",
        [
            {},
            {"agent_spawn_stamp": {"unresolved_count": 0, "session_id": _SID}},
            {"agent_spawn_stamp": {"unresolved_count": -1, "session_id": _SID}},
            {"agent_spawn_stamp": {"unresolved_count": True, "session_id": _SID}},
            {"agent_spawn_stamp": {"unresolved_count": "1", "session_id": _SID}},
            {"agent_spawn_stamp": {"unresolved_count": 1, "session_id": _SID}},
            {
                "agent_spawn_stamp": {
                    "unresolved_count": 1,
                    "last_stamped_at": "garbage",
                    "session_id": _SID,
                }
            },
            [1, 2],
        ],
        ids=[
            "no-stamp",
            "count-0",
            "count-negative",
            "count-true",
            "count-str",
            "no-last-stamped-at",
            "garbage-last-stamped-at",
            "non-object-context",
        ],
    )
    def test_absent_or_unreadable_stamp_holds(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        context: object,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now, stamp_age_minutes=None)
        assert sess.worktree_path is not None
        _write_context_json(sess.worktree_path, context)

        assert _detect([sess], now=now, clients=clients) == []
        assert _still_paged([sess], now=now)

    def test_non_json_context_holds(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now, stamp_age_minutes=None)
        assert sess.worktree_path is not None
        write_unreadable_claim_context(sess.worktree_path, "{not json")

        assert _detect([sess], now=now, clients=clients) == []
        assert _still_paged([sess], now=now)

    def test_missing_context_file_holds(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        assert sess.worktree_path is not None
        (sess.worktree_path / HOOK_CONTEXT_RELATIVE_PATH).unlink()

        assert _detect([sess], now=now, clients=clients) == []
        assert _still_paged([sess], now=now)

    def test_no_worktree_path_holds(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        sess.worktree_path = None

        assert not shipped_release._drain_quiesced(
            sess, now=now, config=OrchestratorConfig()
        )

    @pytest.mark.parametrize(
        ("stamp_age", "found"),
        [
            (timedelta(minutes=-5), False),
            (timedelta(minutes=5), False),
            (timedelta(minutes=29, seconds=59), False),
            (timedelta(minutes=30), True),
            (timedelta(minutes=90), True),
        ],
        ids=["future", "5m", "29m59s", "exactly-30m", "90m"],
    )
    def test_stamp_age_against_the_deadline(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        stamp_age: timedelta,
        found: bool,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now, stamp_age_minutes=None)
        assert sess.worktree_path is not None
        _write_agent_spawn_stamp(
            sess.worktree_path,
            unresolved_count=1,
            stamped_at=now - stamp_age,
            session_id=_SID,
        )

        assert bool(_detect([sess], now=now, clients=clients)) is found

    def test_shorter_configured_deadline(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now, stamp_age_minutes=11)
        config = OrchestratorConfig(fix_loop_await_deadline_minutes=10)

        assert len(_detect([sess], now=now, clients=clients, config=config)) == 1

    @pytest.mark.parametrize(
        ("stale_minutes", "found"),
        [(None, False), (29.0, False), (31.0, True)],
        ids=["no-transcript", "flat-29m", "flat-31m"],
    )
    def test_transcript_bucket(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        stale_minutes: float | None,
        found: bool,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(
            tmp_path, now=now, stale_minutes=stale_minutes
        )

        assert bool(_detect([sess], now=now, clients=clients)) is found

    def test_stage_floor_above_30m_waits_for_the_45m_bucket(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        impl_row = [_row(stage=Stage.IMPL)]

        assert _detect([sess], now=now, clients=clients, tasks=impl_row) == []
        assert len(_detect([sess], now=now, clients=clients)) == 1

    @pytest.mark.parametrize(
        "stamp_session_id",
        ["another-session", None],
        ids=["newer-session-reused-the-worktree", "pre-2715-stamp"],
    )
    def test_stamp_must_carry_this_sessions_id(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        stamp_session_id: str | None,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(
            tmp_path, now=now, stamp_session_id=stamp_session_id
        )

        assert _detect([sess], now=now, clients=clients) == []
        assert _still_paged([sess], now=now)

    def test_shared_draining_veto_stays_fail_open(self, tmp_path: Path) -> None:
        """Characterization: the shared veto reads absent/unreadable as clear."""
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now, stamp_age_minutes=None)
        assert sess.worktree_path is not None
        config = OrchestratorConfig()

        assert not _background_work_still_draining(sess, now=now, config=config)
        write_unreadable_claim_context(sess.worktree_path, "{not json")
        assert not _background_work_still_draining(sess, now=now, config=config)


class TestSelectOwnPr:
    def test_matches_by_url_and_state(self) -> None:
        prs = [
            fake_branch_pr(url="https://github.com/foo/bar/pull/7", state="MERGED"),
            fake_branch_pr(state="MERGED", merged_at=_MERGED_AT),
        ]

        evidence = select_own_pr(prs, SHIPPED_SENTINEL_PR_URL)

        assert evidence is not None
        assert (evidence.number, evidence.state) == (99, "MERGED")

    def test_closed_own_pr_is_no_evidence(self) -> None:
        assert (
            select_own_pr([fake_branch_pr(state="CLOSED")], SHIPPED_SENTINEL_PR_URL)
            is None
        )


class TestProbes:
    def test_capture_cap_raises_the_adapter_error(self) -> None:
        probes = ShippedPrProbes(max_captures=1)
        probes.capture(("c", "t", "s1"), read=lambda: ())

        with pytest.raises(ShippedPrProbeUnavailableError):
            probes.capture(("c", "t", "s2"), read=lambda: ())
        assert probes.captures == 1
        assert probes.captured_keys == frozenset({("c", "t", "s1")})

    def test_capture_budget_spent_raises(self) -> None:
        probes = ShippedPrProbes(budget_seconds=0.0)

        with pytest.raises(ShippedPrProbeUnavailableError):
            probes.capture(("c", "t", "s"), read=lambda: ())


class _FetchRecorder:
    """Stands in for ``_deps.fetch_branch_prs``; records each call's context."""

    def __init__(self, prs: list[BranchPr] | None, *, available: bool = True) -> None:
        self.prs = prs
        self.available = available
        self.calls: list[tuple[str, Path | None, bool]] = []

    def __call__(
        self, branch: str, timeout: int = 15, *, cwd: Path | None = None
    ) -> tuple[list[BranchPr] | None, bool]:
        del timeout
        self.calls.append((branch, cwd, not is_rank_held(LockRank.SESSIONS)))
        return (list(self.prs) if self.prs is not None else None), self.available


class TestCapture:
    def test_captures_each_candidate_lockless(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[_row()]))
        fetch = _FetchRecorder([fake_branch_pr()])
        monkeypatch.setattr("cw.reconcile._deps.fetch_branch_prs", fetch)

        probes = capture_shipped_pr_probes(config=OrchestratorConfig(), clients=clients)

        assert fetch.calls == [(f"dev/{_SID}", tmp_path / "ws", True)]
        assert probes.lookup((_CLIENT, _SID, _SID)) == (fake_branch_pr(),)

    def test_gh_outage_stores_no_usable_evidence(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[_row()]))
        monkeypatch.setattr(
            "cw.reconcile._deps.fetch_branch_prs", _FetchRecorder(None, available=False)
        )

        probes = capture_shipped_pr_probes(config=OrchestratorConfig(), clients=clients)

        assert probes.lookup((_CLIENT, _SID, _SID)) is None
        assert _detect([sess], now=now, clients=clients, probes=probes) == []

    def test_honours_the_per_tick_cap(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(UTC)
        sessions = [
            seed_stranded_shipped_session(
                tmp_path, now=now, sid=f"27{i}", surface_ref=f"abcd000{i}"
            )
            for i in range(5)
        ]
        save_state(CwState(sessions=sessions))
        save_dev_queue(DevQueueStore(tasks=[_row(ticket_id=s.id) for s in sessions]))
        fetch = _FetchRecorder([fake_branch_pr()])
        monkeypatch.setattr("cw.reconcile._deps.fetch_branch_prs", fetch)

        probes = capture_shipped_pr_probes(config=OrchestratorConfig(), clients=clients)

        assert len(fetch.calls) == SHIPPED_PR_MAX_PER_TICK
        assert probes.captures == SHIPPED_PR_MAX_PER_TICK

    def test_no_clients_captures_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fetch = _FetchRecorder([])
        monkeypatch.setattr("cw.reconcile._deps.fetch_branch_prs", fetch)

        probes = capture_shipped_pr_probes(config=OrchestratorConfig(), clients={})

        assert fetch.calls == []
        assert probes.captures == 0

    @pytest.mark.parametrize("error", [OSError("disk"), ValueError("corrupt")])
    def test_state_read_error_yields_empty_probes(
        self,
        clients: dict[str, ClientConfig],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        error: Exception,
    ) -> None:
        def _boom() -> CwState:
            raise error

        monkeypatch.setattr("cw.reconcile.shipped_release.load_state", _boom)

        with caplog.at_level(logging.WARNING, logger="cw.reconcile.shipped_release"):
            probes = capture_shipped_pr_probes(
                config=OrchestratorConfig(), clients=clients
            )

        assert probes.captures == 0
        assert "could not read state" in caplog.text


def _run(
    state: CwState,
    *,
    now: datetime,
    clients: dict[str, ClientConfig],
    probes: ShippedPrProbes | None,
    tasks: list[TicketTask] | None = None,
    deferred: DeferredReconcileJobs | None = None,
) -> list[str]:
    sink = deferred if deferred is not None else DeferredReconcileJobs()
    with sessions_lock():
        closed = run_shipped_release(
            state,
            now=now,
            native_live=_LIVE,
            clients=clients,
            config=OrchestratorConfig(),
            tasks=[_row()] if tasks is None else tasks,
            probes=probes,
            deferred=sink,
        )
    run_post_lock_jobs(sink)
    return closed


def _completed_events(consumer: str) -> list[dict[str, object]]:
    return [
        dict(e.payload)
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
    ]


class TestRunShippedRelease:
    def test_closes_and_stamps_provenance(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[_row()]))
        daemon = LockProbeDaemon()
        use_reconcile_daemon(monkeypatch, daemon)

        with caplog.at_level(logging.INFO, logger="cw.reconcile.shipped_release"):
            closed = _run(state, now=now, clients=clients, probes=_open_probes([sess]))

        assert closed == [_SID]
        persisted = load_state().sessions[0]
        assert persisted.status is SessionStatus.COMPLETED
        assert persisted.completed_reason is CompletionReason.NORMAL
        assert persisted.completed_at == now
        assert persisted.reap_reason is ReapReason.ROUTED_RESULT_STRANDED
        assert daemon.stop_calls == [SHIPPED_SURFACE_REF]
        assert "routed_result_stranded" in caplog.text
        assert SHIPPED_SENTINEL_PR_URL in caplog.text

    def test_untouched_session_keeps_no_reap_reason(
        self, tmp_path: Path, clients: dict[str, ClientConfig]
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        state = CwState(sessions=[sess])
        save_state(state)

        assert _run(state, now=now, clients=clients, probes=_probes([sess], ())) == []
        assert load_state().sessions[0].reap_reason is None
        assert load_state().sessions[0].status is SessionStatus.ACTIVE

    @pytest.mark.parametrize(
        ("probes_none", "clients_empty"),
        [(True, False), (False, True)],
        ids=["no-probes", "no-clients"],
    )
    def test_no_op_without_probes_or_clients(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        probes_none: bool,
        clients_empty: bool,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        state = CwState(sessions=[sess])

        closed = _run(
            state,
            now=now,
            clients={} if clients_empty else clients,
            probes=None if probes_none else _open_probes([sess]),
        )

        assert closed == []
        assert sess.status is SessionStatus.ACTIVE

    @pytest.mark.parametrize(
        "row",
        [
            _row(status=QueueItemStatus.RUNNING, session_id=_SID),
            _row(
                status=QueueItemStatus.BLOCKED_ON_USER,
                session_id=_SID,
                disposition="finalize_gate_held",
            ),
            _row(status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, session_id=_SID),
        ],
        ids=["running", "hold-finalize", "signoff"],
    )
    def test_fresh_in_lock_pin_check_wins(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        row: TicketTask,
    ) -> None:
        """D3: the pre-lock read shows no pin; the fresh in-lock read does."""
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[_row()]))
        monkeypatch.setattr(
            "cw.reconcile.shipped_release.load_dev_queue",
            lambda: DevQueueStore(tasks=[row]),
        )
        daemon = LockProbeDaemon()
        use_reconcile_daemon(monkeypatch, daemon)

        with caplog.at_level(logging.INFO, logger="cw.reconcile.shipped_release"):
            closed = _run(state, now=now, clients=clients, probes=_open_probes([sess]))

        assert closed == []
        assert load_state().sessions[0].status is SessionStatus.ACTIVE
        assert _completed_events("pin-check") == []
        assert daemon.stop_calls == []
        assert "now pinned" in caplog.text

    @pytest.mark.parametrize("error", [OSError("disk"), ValueError("corrupt")])
    def test_fresh_queue_read_error_closes_nothing(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        state = CwState(sessions=[sess])
        save_state(state)

        def _boom() -> DevQueueStore:
            raise error

        monkeypatch.setattr("cw.reconcile.shipped_release.load_dev_queue", _boom)

        assert _run(state, now=now, clients=clients, probes=_open_probes([sess])) == []
        assert sess.status is SessionStatus.ACTIVE

    def test_queue_lock_is_released_before_the_event(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D3 lock order: dev_queue_lock nests under sessions_lock and is
        released before ``session.completed`` is written."""
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[_row()]))
        held: list[tuple[bool, bool]] = []

        def _record(*_args: object, **_kwargs: object) -> None:
            held.append((is_rank_held(LockRank.SESSIONS), is_rank_held(LockRank.STATE)))

        monkeypatch.setattr("cw.reconcile.dispositions.record_event", _record)

        assert _run(state, now=now, clients=clients, probes=_open_probes([sess])) == [
            _SID
        ]
        assert held == [(True, False)]

    def test_event_write_failure_is_logged_not_raised(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The session is already COMPLETED; no stop is queued (the
        leaked-worker sweep is the backstop for a terminal session)."""
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[_row()]))
        daemon = LockProbeDaemon()
        use_reconcile_daemon(monkeypatch, daemon)

        def _fail(*_args: object, **_kwargs: object) -> None:
            msg = "inbox full"
            raise OSError(msg)

        monkeypatch.setattr("cw.reconcile.dispositions.record_event", _fail)

        with caplog.at_level(logging.ERROR, logger="cw.reconcile.shipped_release"):
            closed = _run(state, now=now, clients=clients, probes=_open_probes([sess]))

        assert closed == [_SID]
        assert load_state().sessions[0].status is SessionStatus.COMPLETED
        assert daemon.stop_calls == []
        assert "was not recorded" in caplog.text


# ---------------------------------------------------------------------------
# Through reconcile()
# ---------------------------------------------------------------------------


def _roster(sessions: list[Session]) -> list[dict[str, object]]:
    return [
        {"sessionId": f"{s.surface_ref}-0000-4000-8000-000000000000"}
        for s in sessions
        if s.surface_ref is not None
    ]


def _world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sessions: list[Session],
    *,
    tasks: list[TicketTask] | None = None,
    prs: list[BranchPr] | None = None,
    available: bool = True,
) -> tuple[LockProbeDaemon, _FetchRecorder]:
    """Persist a reconcile()-ready world around *sessions*."""
    write_clients_yaml(
        ClientConfig(
            name=_CLIENT,
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "worktrees",
        )
    )
    for sess in sessions:
        sess.liveness_bucket = LivenessBucket.STALE_30M
    save_state(CwState(sessions=sessions))
    save_dev_queue(DevQueueStore(tasks=[_row()] if tasks is None else tasks))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: _roster(sessions)
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    fetch = _FetchRecorder(
        [fake_branch_pr()] if prs is None and available else prs, available=available
    )
    monkeypatch.setattr("cw.reconcile._deps.fetch_branch_prs", fetch)
    daemon = LockProbeDaemon()
    for sess in sessions:
        if sess.surface_ref is not None:
            daemon._live.add(sess.surface_ref)
    use_reconcile_daemon(monkeypatch, daemon)
    return daemon, fetch


def _event_types(consumer: str) -> list[str]:
    return [str(e.type) for e in read_events(consumer=consumer)]


class TestThroughReconcile:
    def test_closes_with_exactly_one_completion_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        daemon, fetch = _world(tmp_path, monkeypatch, [sess])
        queue_before = load_dev_queue().model_dump()
        assert counts_toward_client_ceiling(load_state().sessions[0], _CLIENT)

        reconcile()

        after = load_state().sessions[0]
        assert after.status is SessionStatus.COMPLETED
        assert after.reap_reason is ReapReason.ROUTED_RESULT_STRANDED
        assert after.reap_proposed_at is None
        assert not counts_toward_client_ceiling(after, _CLIENT)
        assert _event_types("e2e") == [str(OrchestratorEventType.SESSION_COMPLETED)]
        payload = _completed_events("e2e-payload")[0]
        assert payload == {
            "session_id": _SID,
            "session_name": sess.name,
            "client": _CLIENT,
            "ticket_id": _SID,
            # Backfilled from the roster earlier in the same tick.
            "claude_session_id": after.claude_session_id,
            "crashed": False,
            "salvaged": True,
            "status": "shipped",
            "pr_url": SHIPPED_SENTINEL_PR_URL,
        }
        assert load_dev_queue().model_dump() == queue_before
        assert [lock for lock, _statuses in daemon.probes] == ["free"]
        assert daemon.probes[0][1][_SID] is SessionStatus.COMPLETED
        assert [free for _b, _c, free in fetch.calls] == [True]

    def test_dispatch_consumer_then_transitions_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        _world(tmp_path, monkeypatch, [sess])
        reconcile()
        queue_before = load_dev_queue().model_dump()

        assert consume_completed_sessions() == 0
        assert load_dev_queue().model_dump() == queue_before

    def test_second_tick_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        daemon, fetch = _world(tmp_path, monkeypatch, [sess])
        reconcile()
        events_before = len(read_events())
        state_before = load_state().model_dump()

        reconcile()

        assert len(read_events()) == events_before
        assert load_state().model_dump() == state_before
        assert len(daemon.stop_calls) == 1
        assert len(fetch.calls) == 1

    def test_closable_session_is_never_paged_but_others_are(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        shipped = seed_stranded_shipped_session(tmp_path, now=now)
        stage_complete = seed_stranded_shipped_session(
            tmp_path,
            now=now,
            sid="2716",
            surface_ref="abcd5678",
            last_result=_routed_last_result(),
        )
        _world(
            tmp_path,
            monkeypatch,
            [shipped, stage_complete],
            tasks=[_row(), _row(ticket_id="2716")],
        )

        reconcile()

        by_id = {s.id: s for s in load_state().sessions}
        assert by_id[_SID].status is SessionStatus.COMPLETED
        assert by_id["2716"].status is SessionStatus.ACTIVE
        assert by_id["2716"].reap_proposed_at is not None
        pages = [
            e.payload
            for e in read_events(
                event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]
            )
        ]
        assert [p["session_id"] for p in pages] == ["2716"]
        assert pages[0]["paused_status"] == ROUTED_RESULT_STRANDED_REASON

    def test_bounded_per_tick(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        sessions = [
            seed_stranded_shipped_session(
                tmp_path, now=now, sid=f"27{i}", surface_ref=f"abcd000{i}"
            )
            for i in range(5)
        ]
        _world(
            tmp_path,
            monkeypatch,
            sessions,
            tasks=[_row(ticket_id=s.id) for s in sessions],
        )

        reconcile()
        first = [
            s.id for s in load_state().sessions if s.status is SessionStatus.COMPLETED
        ]
        reconcile()
        second = [
            s.id for s in load_state().sessions if s.status is SessionStatus.COMPLETED
        ]

        assert len(first) == SHIPPED_PR_MAX_PER_TICK
        assert len(second) == len(sessions)

    def test_pinned_session_is_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        signoff = _row(
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, session_id=_SID
        )
        daemon, _fetch = _world(tmp_path, monkeypatch, [sess], tasks=[signoff])

        reconcile()

        after = load_state().sessions[0]
        assert after.status is SessionStatus.ACTIVE
        assert counts_toward_client_ceiling(after, _CLIENT)
        assert daemon.stop_calls == []

    def test_daemon_outage_early_return_skips_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        _world(tmp_path, monkeypatch, [sess])
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

        reconcile()

        assert load_state().sessions[0].status is SessionStatus.ACTIVE

    @pytest.mark.parametrize(
        "variant",
        ["closed-pr", "no-pr", "gh-outage", "no-stamp", "foreign-stamp"],
    )
    def test_never_clock_triggered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
    ) -> None:
        """Three days of elapsed time never stand in for missing evidence."""
        now = datetime.now(UTC)
        later = now + timedelta(days=3)
        sess = seed_stranded_shipped_session(
            tmp_path,
            now=now,
            stamp_age_minutes=None if variant == "no-stamp" else 90.0,
            stamp_session_id="someone-else" if variant == "foreign-stamp" else _SID,
        )
        prs: dict[str, list[BranchPr] | None] = {
            "closed-pr": [fake_branch_pr(state="CLOSED")],
            "no-pr": [],
            "gh-outage": None,
        }
        daemon, _fetch = _world(
            tmp_path,
            monkeypatch,
            [sess],
            prs=prs.get(variant, [fake_branch_pr()]),
            available=variant != "gh-outage",
        )

        with freeze_time(later):
            reconcile()

        after = load_state().sessions[0]
        assert after.status is SessionStatus.ACTIVE
        assert after.reap_reason is None
        assert _completed_events("never-clock") == []
        assert daemon.stop_calls == []


class TestAcceptanceOwners:
    """Who owns each shape (S is a no-op outside its own)."""

    def test_unconsumed_emit_result_is_idles_not_s(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(
            tmp_path, now=now, last_result=_shipped_salvage_payload(_SID)
        )
        _daemon, fetch = _world(
            tmp_path,
            monkeypatch,
            [sess],
            tasks=[_row(status=QueueItemStatus.RUNNING, session_id=_SID)],
        )

        reconcile()

        assert fetch.calls == []

    def test_roster_absent_session_is_phantoms_not_s(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        sess = seed_stranded_shipped_session(tmp_path, now=now)
        other = seed_stranded_shipped_session(
            tmp_path, now=now, sid="2716", surface_ref="abcd5678"
        )
        _daemon, _fetch = _world(tmp_path, monkeypatch, [sess, other])
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json", lambda: _roster([other])
        )

        reconcile()

        by_id = {s.id: s for s in load_state().sessions}
        assert by_id[_SID].reap_reason is not ReapReason.ROUTED_RESULT_STRANDED


class TestBoundaryGuards:
    @pytest.mark.parametrize(
        "module_name",
        ["cw.reconcile.shipped_release", "cw.reconcile.lost_session_rows"],
    )
    def test_no_gh_argv_subprocess_or_stop_call(self, module_name: str) -> None:
        source = inspect.getsource(importlib.import_module(module_name))
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        stop_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "stop"
        ]
        gh_argv = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.List)
            and node.elts
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value == "gh"
        ]

        assert "subprocess" not in imports
        assert stop_calls == []
        assert gh_argv == []
