"""Tests for cw.reconcile.fix_dispatch — the async fix-loop handoff (#2017 R21).

The module under test is deliberately NOT a review recipe: it is called
unconditionally from ``reconcile.core``, so these tests assert the absence of a
``review_recipes_enabled`` gate as explicitly as they assert the dispatch
behaviour itself.

Fixtures come from ``tests/conftest.py``'s canonical builders
(``_make_ticket_task``, ``_make_daemon_session``) rather than hand-rolled model
construction.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.exceptions import CwError, HookContextConflictError
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    PendingFixDispatch,
    QueueItemStatus,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import fix_dispatch, reconcile
from tests.conftest import _make_daemon_session, _make_ticket_task
from tests.test_reconcile_review_recipes import _make_fix_client, _seed_origin

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TICKET = "2017"
_CLIENT = "acme"


def _pending(**overrides: Any) -> PendingFixDispatch:
    kwargs: dict[str, Any] = {
        "prompt": "fix the MUST_FIX items\n",
        "label": f"fix-{_TICKET}",
        "cycle": 1,
        "requested_by_session_id": "review-sess",
        "requested_at": datetime(2026, 8, 26, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return PendingFixDispatch(**kwargs)


def _seed_task(**overrides: Any) -> None:
    """Persist a single dev-queue row built from the canonical task builder."""
    kwargs: dict[str, Any] = {
        "ticket_id": _TICKET,
        "client": _CLIENT,
        "status": QueueItemStatus.RUNNING,
    }
    kwargs.update(overrides)
    save_dev_queue(DevQueueStore(tasks=[_make_ticket_task(**kwargs)]))


def _only_task() -> Any:
    tasks = load_dev_queue().tasks
    assert len(tasks) == 1
    return tasks[0]


@pytest.fixture
def acme_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClientConfig:
    """Resolvable ``acme`` client, patched in at the module's own lookup seam."""
    workspace = tmp_path / "acme-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    client = ClientConfig(name=_CLIENT, workspace_path=workspace)
    monkeypatch.setattr(
        fix_dispatch, "load_effective_clients", lambda: {_CLIENT: client}
    )
    return client


class _DispatchRecorder:
    """Records dispatch_fix_agent calls and applies an optional side effect."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.side_effect: Any = None

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.side_effect is not None:
            self.side_effect(**kwargs)
        return "fix-sess"


@pytest.fixture
def stub_dispatch(monkeypatch: pytest.MonkeyPatch) -> _DispatchRecorder:
    recorder = _DispatchRecorder()
    monkeypatch.setattr(fix_dispatch, "dispatch_fix_agent", recorder)
    return recorder


# --- detect phases ---------------------------------------------------------


def test_detect_pending_fix_dispatches_finds_marked_task() -> None:
    task = _make_ticket_task(ticket_id=_TICKET, client=_CLIENT)
    task.pending_fix_dispatch = _pending()

    candidates = fix_dispatch._detect_pending_fix_dispatches([task])

    assert [(c.ticket_id, c.client) for c in candidates] == [(_TICKET, _CLIENT)]


def test_detect_pending_fix_dispatches_skips_unmarked_task() -> None:
    task = _make_ticket_task(ticket_id=_TICKET, client=_CLIENT)

    assert fix_dispatch._detect_pending_fix_dispatches([task]) == []


def test_detect_fix_dispatch_completions_finds_dispatched_task() -> None:
    task = _make_ticket_task(ticket_id=_TICKET, client=_CLIENT)
    task.fix_dispatch_session_id = "fix-sess"

    candidates = fix_dispatch._detect_fix_dispatch_completions([task])

    assert [(c.ticket_id, c.client) for c in candidates] == [(_TICKET, _CLIENT)]


def test_detect_fix_dispatch_completions_skips_undispatched_task() -> None:
    task = _make_ticket_task(ticket_id=_TICKET, client=_CLIENT)

    assert fix_dispatch._detect_fix_dispatch_completions([task]) == []


# --- pending-dispatch act phase --------------------------------------------


def test_act_on_pending_fix_dispatches_spawns_and_clears_latch(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """A successful dispatch consumes the handoff and points at the new session."""
    _seed_task(pending_fix_dispatch=_pending())

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == [_TICKET]
    assert len(stub_dispatch.calls) == 1
    call = stub_dispatch.calls[0]
    assert call["branch"] == "dev/2017"
    assert call["prompt"] == "fix the MUST_FIX items\n"
    assert call["label"] == "fix-2017"
    assert call["parent"] == "review-sess"
    task = _only_task()
    assert task.pending_fix_dispatch is None
    assert task.fix_dispatch_session_id == "fix-sess"
    # The row must stay RUNNING: that is what keeps claim.py's PENDING-only
    # reclaim from dispatching a second REVIEW session mid-fix.
    assert task.status == QueueItemStatus.RUNNING


def test_act_on_pending_fix_dispatches_retries_on_transient_conflict(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """A still-held worktree leaves the handoff intact for the next tick.

    This is the expected failure while the REVIEW session finishes going
    terminal -- dropping the record here would lose the entire action list.
    """

    def _conflict(**_kwargs: Any) -> None:
        msg = "worktree still held"
        raise HookContextConflictError(msg, conflicting_session_id="review-sess")

    stub_dispatch.side_effect = _conflict
    _seed_task(pending_fix_dispatch=_pending())

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    task = _only_task()
    assert task.pending_fix_dispatch is not None
    assert task.fix_dispatch_session_id is None
    assert task.status == QueueItemStatus.RUNNING
    assert (
        read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]) == []
    )


def test_act_on_pending_fix_dispatches_clears_and_escalates_on_hard_failure(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """A merge conflict (or any non-conflict CwError) unparks and escalates.

    No session is running when an async dispatch fails, so the two events ARE
    the operator signal -- there is no sentinel to carry a blocker.reason.
    """

    def _boom(**_kwargs: Any) -> None:
        msg = "merging origin/main into dev/2017 conflicted"
        raise CwError(msg)

    stub_dispatch.side_effect = _boom
    _seed_task(pending_fix_dispatch=_pending())

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    task = _only_task()
    assert task.pending_fix_dispatch is None
    assert task.fix_dispatch_session_id is None
    assert task.status == QueueItemStatus.PENDING

    errored = read_events(event_types=[OrchestratorEventType.STAGE_ERRORED])
    assert len(errored) == 1
    assert errored[0].correlation_id == _TICKET
    assert errored[0].payload["session_id"] == "review-sess"
    assert errored[0].payload["ticket_id"] == _TICKET
    assert errored[0].payload["stage"] == "s3_fix_loop"
    assert errored[0].payload["error_kind"] == "fix_dispatch_failed"
    assert errored[0].payload["started_at"]

    attention = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(attention) == 1
    assert attention[0].correlation_id == _TICKET
    assert attention[0].payload["session_id"] == "review-sess"
    assert attention[0].payload["client"] == _CLIENT
    assert attention[0].payload["claude_session_id"] is None
    assert attention[0].payload["paused_status"] == "fix_dispatch_failed"
    assert "conflicted" in attention[0].payload["breadcrumbs"]
    assert attention[0].payload["crashed"] is False


def test_act_on_pending_fix_dispatches_skips_unresolvable_client(
    tmp_config_dir: Path,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """An unresolvable client cannot be dispatched and must not be dropped."""
    _seed_task(pending_fix_dispatch=_pending())

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={},
    )

    assert acted == []
    assert stub_dispatch.calls == []
    assert _only_task().pending_fix_dispatch is not None


def test_act_phases_tolerate_a_row_removed_mid_tick(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """A row cancelled between detect and act is skipped, not crashed on.

    Both act phases re-resolve the row under their own lock precisely so a
    concurrent ``cw dev-queue cancel`` cannot make them write back a snapshot
    of a row that no longer exists.
    """
    save_dev_queue(DevQueueStore(tasks=[]))
    candidates = [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)]

    assert (
        fix_dispatch._act_on_pending_fix_dispatches(
            candidates, clients={_CLIENT: acme_client}
        )
        == []
    )
    assert fix_dispatch._act_on_fix_dispatch_completions(candidates) == []
    assert stub_dispatch.calls == []


def test_stamp_helpers_tolerate_a_row_removed_after_dispatch(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
) -> None:
    """The post-lock stamps run after the spawn, so the row can be gone by then."""
    save_dev_queue(DevQueueStore(tasks=[]))
    job = fix_dispatch._DispatchJob(
        client_cfg=acme_client,
        branch="dev/2017",
        pending=_pending(),
        ticket_id=_TICKET,
        client=_CLIENT,
        lane="default",
    )

    fix_dispatch._stamp_dispatch_success(job, "fix-sess")
    fix_dispatch._stamp_dispatch_failure(job, CwError("gone"))

    assert load_dev_queue().tasks == []


# --- completion act phase ---------------------------------------------------


def _save_fix_session(status: SessionStatus) -> None:
    from cw.config import save_state

    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id="fix-sess",
                    name=f"{_CLIENT}/fix/{_TICKET}",
                    client=_CLIENT,
                    status=status,
                )
            ]
        )
    )


def test_act_on_fix_dispatch_completions_unparks_task(tmp_config_dir: Path) -> None:
    _seed_task(fix_dispatch_session_id="fix-sess")
    _save_fix_session(SessionStatus.COMPLETED)

    unparked = fix_dispatch._act_on_fix_dispatch_completions(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)]
    )

    assert unparked == [_TICKET]
    task = _only_task()
    assert task.fix_dispatch_session_id is None
    assert task.status == QueueItemStatus.PENDING


def test_act_on_fix_dispatch_completions_skips_still_live_session(
    tmp_config_dir: Path,
) -> None:
    _seed_task(fix_dispatch_session_id="fix-sess")
    _save_fix_session(SessionStatus.ACTIVE)

    unparked = fix_dispatch._act_on_fix_dispatch_completions(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)]
    )

    assert unparked == []
    task = _only_task()
    assert task.fix_dispatch_session_id == "fix-sess"
    assert task.status == QueueItemStatus.RUNNING


def test_act_on_fix_dispatch_completions_unparks_unresolvable_session(
    tmp_config_dir: Path,
) -> None:
    """A session cw cannot resolve at all is gone, not pending.

    Treating it as still-live would strand the row RUNNING forever, since
    nothing else clears fix_dispatch_session_id.
    """
    _seed_task(fix_dispatch_session_id="vanished")

    unparked = fix_dispatch._act_on_fix_dispatch_completions(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)]
    )

    assert unparked == [_TICKET]
    assert _only_task().status == QueueItemStatus.PENDING


# --- entry point ------------------------------------------------------------


def test_run_fix_dispatch_is_not_gated_by_review_recipes_enabled(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """The whole point of siting this outside the review_recipes package.

    Registering it there would have inherited a default-off master switch and
    silently disabled the fix loop for every client that never opted in.
    """
    _seed_task(pending_fix_dispatch=_pending())

    acted = fix_dispatch.run_fix_dispatch(
        config=OrchestratorConfig(review_recipes_enabled=False)
    )

    assert acted == [_TICKET]
    assert len(stub_dispatch.calls) == 1
    assert _only_task().fix_dispatch_session_id == "fix-sess"


def test_run_fix_dispatch_no_candidates_is_a_noop(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    _seed_task()

    assert fix_dispatch.run_fix_dispatch(config=OrchestratorConfig()) == []
    assert stub_dispatch.calls == []


# --- sessions_lock integration (#2064) ---------------------------------------


def test_run_fix_dispatch_spawns_real_fix_session_through_sessions_lock(
    tmp_config_dir: Path,
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconcile() must dispatch fix agents post-lock (#2064).

    Exercises the REAL ``dispatch_fix_agent`` -> ``spawn_create_impl`` path
    (only the native daemon is faked), so the spawn's own ``sessions_lock()``
    acquisition genuinely runs. On the pre-fix tree this dies with
    ``SessionsLockReentryError`` inside ``spawn_create_impl``, is caught by
    ``_act_on_pending_fix_dispatches``'s broad ``except CwError``, and no fix
    session is ever spawned.
    """
    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2019"
    _seed_origin(client, branch)
    monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: mock_native_daemon)
    monkeypatch.setattr(
        fix_dispatch, "load_effective_clients", lambda: {_CLIENT: client}
    )

    # Parent session dispatch_fix_agent -> spawn_create_impl resolves via
    # PendingFixDispatch.requested_by_session_id ("review-sess" default).
    # Terminal so it never enters phantom detection (_LIVE_STATUSES-only).
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id="review-sess",
                    name=f"{_CLIENT}/review/2019",
                    client=_CLIENT,
                    status=SessionStatus.COMPLETED,
                )
            ]
        )
    )
    task = _make_ticket_task(
        ticket_id="2019",
        client=_CLIENT,
        status=QueueItemStatus.RUNNING,
    )
    task.pending_fix_dispatch = _pending(label="fix-2019")
    save_dev_queue(DevQueueStore(tasks=[task]))

    reconcile()

    fix_sessions = [s for s in load_state().sessions if s.purpose == SessionPurpose.FIX]
    assert len(fix_sessions) == 1
    updated = _only_task()
    assert updated.pending_fix_dispatch is None
    assert updated.fix_dispatch_session_id == fix_sessions[0].id
    assert read_events(event_types=[OrchestratorEventType.STAGE_ERRORED]) == []


# --- per-tick spawn cap (#2064) -----------------------------------------------


def test_act_on_pending_fix_dispatches_caps_spawns_per_tick(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fan-out is bounded: only the first ``_MAX_FIX_DISPATCHES_PER_TICK``
    candidates spawn; the rest are left pending and reconsidered next tick."""
    monkeypatch.setattr(fix_dispatch, "_MAX_FIX_DISPATCHES_PER_TICK", 2)
    ticket_ids = ["2020", "2021", "2022"]
    tasks = [
        _make_ticket_task(
            ticket_id=ticket_id,
            client=_CLIENT,
            status=QueueItemStatus.RUNNING,
            pending_fix_dispatch=_pending(label=f"fix-{ticket_id}"),
        )
        for ticket_id in ticket_ids
    ]
    save_dev_queue(DevQueueStore(tasks=tasks))

    candidates = fix_dispatch._detect_pending_fix_dispatches(load_dev_queue().tasks)
    with caplog.at_level(logging.INFO, logger="cw.reconcile.fix_dispatch"):
        acted = fix_dispatch._act_on_pending_fix_dispatches(
            candidates, clients={_CLIENT: acme_client}
        )

    assert len(acted) == 2
    assert len(stub_dispatch.calls) == 2

    elided_ids = set(ticket_ids) - set(acted)
    assert len(elided_ids) == 1
    reloaded = {t.ticket_id: t for t in load_dev_queue().tasks}
    elided = reloaded[next(iter(elided_ids))]
    assert elided.pending_fix_dispatch is not None
    assert elided.fix_dispatch_session_id is None
    assert elided.status == QueueItemStatus.RUNNING

    assert any("cap" in rec.message for rec in caplog.records)
