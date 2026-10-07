"""Unit tests for cw.reconcile.harvest_synthesis (#2565).

The lockless git-facts store the local harvest reads under ``sessions_lock``
(capture, lookup, budget, identity, max age), the in-lock synthesis error
handling (a captured git failure parks, a timeout or a miss never does), and
the pinned logger name the moved warnings keep.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import freezegun
import pytest

from cw._git import run_git
from cw.config import load_state, save_state
from cw.dev_queue import save_dev_queue
from cw.events import read_events
from cw.local_runner import GitFacts
from cw.models import (
    CwState,
    DevQueueStore,
    LocalLivenessBackend,
    LocalLivenessHandle,
    QueueItemStatus,
    Session,
    Stage,
    TicketTask,
)
from cw.reconcile import harvest_synthesis
from cw.reconcile import local as reconcile_local
from cw.reconcile.harvest_synthesis import (
    GIT_SYNTHESIS_ERRORS,
    HARVEST_FACTS_MAX_AGE_SECONDS,
    HarvestFacts,
    HarvestFactsUnavailableError,
    _resolve_harvest_backend,
    _synthesize_harvest_sentinel,
    harvest_uses_git,
)
from cw.reconcile.local import capture_local_harvest_facts
from tests._clients_yaml import staged_client, write_clients_yaml
from tests._opencode_helpers import write_opencode_log
from tests._reconcile_helpers import (
    _local_git_worktree,
    _mk_local_session,
    _touch_aider_log,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_SID = "ses-2565"
_BRANCH = "main"
_HANDLE = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=123)
_T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
_LOCAL_LOGGER = "cw.reconcile.local"


def _facts(*, commits: tuple[str, ...] = ("c1",)) -> GitFacts:
    """A valid ``GitFacts`` literal (store tests never read its values)."""
    return GitFacts(
        branch="dev/T-2565",
        fork_point="f0f0",
        commits=list(commits),
        files=1,
        lines_actual=2,
    )


def _forbid_git(*_args: object, **_kwargs: object) -> GitFacts:
    msg = "this path must not run git"
    raise AssertionError(msg)


def _captured(worktree: Path, facts: GitFacts | None = None) -> HarvestFacts:
    """A store holding one capture of *facts* for ``_SID`` at *worktree*."""
    store = HarvestFacts()
    with patch.object(harvest_synthesis, "git_facts", return_value=facts or _facts()):
        store.capture(_SID, _HANDLE, worktree, _BRANCH)
    return store


# ---------------------------------------------------------------------------
# HarvestFacts: capture lockless, look up in-lock
# ---------------------------------------------------------------------------


def test_capture_then_lookup_returns_facts_and_lookup_runs_no_git(
    tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
) -> None:
    worktree = _local_git_worktree(make_git_repo, "wt-store", with_commit=True)
    store = HarvestFacts()
    store.capture(_SID, _HANDLE, worktree, _BRANCH)
    spy = MagicMock(wraps=run_git)

    with patch("cw.local_runner.run_git", spy):
        facts = store.lookup(_SID, _HANDLE, worktree, _BRANCH)

    assert len(facts["commits"]) == 1
    assert facts["fork_point"]
    spy.assert_not_called()


def test_lookup_never_captured_raises_unavailable(tmp_path: Path) -> None:
    with pytest.raises(HarvestFactsUnavailableError, match="never captured"):
        HarvestFacts().lookup(_SID, _HANDLE, tmp_path, _BRANCH)


@pytest.mark.parametrize(
    "changed", ["worktree", "default_branch", "pid", "start_time_ns"]
)
def test_lookup_misses_when_identity_changes(changed: str, tmp_path: Path) -> None:
    store = _captured(tmp_path)
    worktree = tmp_path / "elsewhere" if changed == "worktree" else tmp_path
    branch = "trunk" if changed == "default_branch" else _BRANCH
    handle = _HANDLE.model_copy(
        update={changed: 7} if changed in ("pid", "start_time_ns") else {}
    )

    with pytest.raises(HarvestFactsUnavailableError, match="identity changed"):
        store.lookup(_SID, handle, worktree, branch)
    with pytest.raises(HarvestFactsUnavailableError, match="never captured"):
        store.lookup("another-session", _HANDLE, tmp_path, _BRANCH)


@pytest.mark.parametrize(
    "offset",
    [
        pytest.param(HARVEST_FACTS_MAX_AGE_SECONDS, id="exactly-max-age"),
        pytest.param(HARVEST_FACTS_MAX_AGE_SECONDS + 30, id="older"),
        pytest.param(-1.0, id="negative-age"),
    ],
)
def test_lookup_misses_when_stale_or_negative_age(
    offset: float, tmp_path: Path
) -> None:
    with freezegun.freeze_time(_T0):
        store = _captured(tmp_path)
    with freezegun.freeze_time(
        _T0 + timedelta(seconds=HARVEST_FACTS_MAX_AGE_SECONDS - 1)
    ):
        assert store.lookup(_SID, _HANDLE, tmp_path, _BRANCH) == _facts()

    with (
        freezegun.freeze_time(_T0 + timedelta(seconds=offset)),
        pytest.raises(HarvestFactsUnavailableError, match="stale") as raised,
    ):
        store.lookup(_SID, _HANDLE, tmp_path, _BRANCH)
    assert f"{offset:.1f}s" in str(raised.value)


def test_capture_raises_before_git_once_budget_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(harvest_synthesis, "monotonic", lambda: clock["now"])
    store = HarvestFacts(budget_seconds=60.0)
    clock["now"] = 60.0
    monkeypatch.setattr(harvest_synthesis, "git_facts", _forbid_git)

    with pytest.raises(HarvestFactsUnavailableError, match="budget is spent"):
        store.capture(_SID, _HANDLE, tmp_path, _BRANCH)
    with pytest.raises(HarvestFactsUnavailableError, match="never captured"):
        store.lookup(_SID, _HANDLE, tmp_path, _BRANCH)


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(128, ["git", "rev-parse"]),
        OSError("worktree vanished"),
    ],
    ids=["called-process-error", "os-error"],
)
def test_captured_git_failure_is_replayed_on_lookup(
    error: Exception, tmp_path: Path
) -> None:
    """Evidence-based failures are stored, so the in-lock synthesis still parks."""
    store = HarvestFacts()
    with patch.object(harvest_synthesis, "git_facts", side_effect=error):
        store.capture(_SID, _HANDLE, tmp_path, _BRANCH)

    with pytest.raises(type(error)) as raised:
        store.lookup(_SID, _HANDLE, tmp_path, _BRANCH)
    assert raised.value is error


def test_capture_timeout_is_not_stored_and_propagates(tmp_path: Path) -> None:
    """A timeout is elapsed time, not evidence: nothing is stored to park on."""
    store = HarvestFacts()
    timeout = subprocess.TimeoutExpired(["git", "log"], 10.0)

    with (
        patch.object(harvest_synthesis, "git_facts", side_effect=timeout),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        store.capture(_SID, _HANDLE, tmp_path, _BRANCH)
    with pytest.raises(HarvestFactsUnavailableError, match="never captured"):
        store.lookup(_SID, _HANDLE, tmp_path, _BRANCH)


def test_git_synthesis_errors_is_the_park_tuple_without_timeout() -> None:
    park_tuple = GIT_SYNTHESIS_ERRORS
    assert park_tuple == (OSError, subprocess.CalledProcessError)
    assert not issubclass(subprocess.TimeoutExpired, GIT_SYNTHESIS_ERRORS)


# ---------------------------------------------------------------------------
# In-lock synthesis: park on captured git evidence, never on a timeout or miss
# ---------------------------------------------------------------------------


def _raising(error: BaseException) -> Callable[[], GitFacts]:
    def _facts_source() -> GitFacts:
        raise error

    return _facts_source


def _failed_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "harvest_synthesis_failed" in r.getMessage()]


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(128, ["git", "rev-parse"]),
        OSError("worktree vanished"),
    ],
    ids=["called-process-error", "os-error"],
)
def test_synthesize_harvest_sentinel_parks_captured_git_errors(
    error: Exception, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    task = TicketTask(ticket_id="T-PARK", client="client-a", stage=Stage.REVIEW)

    with caplog.at_level(logging.WARNING):
        result = _synthesize_harvest_sentinel(
            tmp_path, task, _BRANCH, _SID, "aider", facts=_raising(error)
        )

    assert result.status == "blocked"
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == "unexpected_error"
    [warning] = _failed_warnings(caplog)
    assert warning.exc_info is not None


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["git", "log"], 10.0),
        HarvestFactsUnavailableError("harvest facts were never captured"),
    ],
    ids=["timeout", "unavailable"],
)
def test_synthesize_harvest_sentinel_propagates_timeout_and_unavailable(
    error: Exception, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pins that nobody widens the in-lock ``except``: no park off a clock."""
    task = TicketTask(ticket_id="T-DEFER", client="client-a", stage=Stage.IMPL)

    with caplog.at_level(logging.WARNING), pytest.raises(type(error)):
        _synthesize_harvest_sentinel(
            tmp_path, task, _BRANCH, _SID, "aider", facts=_raising(error)
        )

    assert _failed_warnings(caplog) == []


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("aider", True), ("opencode", False), (None, False), ("codex", True)],
    ids=["aider", "opencode", "unproven", "unregistered"],
)
def test_harvest_uses_git_by_backend(
    backend: LocalLivenessBackend | None, expected: bool
) -> None:
    """An unregistered backend falls back to git synthesis, so it uses git too."""
    assert harvest_uses_git(backend) is expected


def test_harvest_synthesis_logs_under_the_pinned_local_logger_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The moved warnings keep the logger name operators filter on (#2565)."""
    assert harvest_synthesis._LOGGER_NAME == _LOCAL_LOGGER
    write_opencode_log(tmp_path, [])
    sess = _mk_local_session("s-name", tmp_path, _HANDLE)
    task = TicketTask(ticket_id="T-NAME", client="client-a", stage=Stage.FINALIZE)

    with caplog.at_level(logging.WARNING):
        assert _resolve_harvest_backend("aider", sess, task, tmp_path) == "opencode"
        _synthesize_harvest_sentinel(
            tmp_path, task, _BRANCH, _SID, "aider", facts=_raising(OSError("gone"))
        )

    names = {
        marker: [r.name for r in caplog.records if marker in r.getMessage()]
        for marker in ("harvest_backend_overridden", "harvest_synthesis_failed")
    }
    assert names == {
        "harvest_backend_overridden": [_LOCAL_LOGGER],
        "harvest_synthesis_failed": [_LOCAL_LOGGER],
    }


# ---------------------------------------------------------------------------
# capture_local_harvest_facts: the lockless driver reconcile() runs last
# ---------------------------------------------------------------------------


def _dead_session(
    sid: str,
    worktree: Path | None,
    *,
    backend: LocalLivenessBackend = "aider",
    spawn_stage: Stage = Stage.IMPL,
) -> Session:
    handle = LocalLivenessHandle(
        pid=_HANDLE.pid, start_time_ns=_HANDLE.start_time_ns, backend=backend
    )
    sess = _mk_local_session(sid, worktree or Path("/nonexistent"), handle)
    sess.worktree_path = worktree
    sess.stage = spawn_stage
    return sess


def _save(sessions: list[Session]) -> tuple[CwState, list[TicketTask]]:
    """Persist *sessions* with one RUNNING row each (row stage = spawn stage)."""
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
    save_state(CwState(sessions=sessions))
    rows = [
        TicketTask(
            ticket_id=s.id,
            client="client-a",
            stage=s.stage or Stage.IMPL,
            status=QueueItemStatus.RUNNING,
            session_id=s.id,
        )
        for s in sessions
    ]
    save_dev_queue(DevQueueStore(tasks=rows))
    return load_state(), rows


def _aider_worktree(tmp_path: Path, name: str) -> Path:
    worktree = tmp_path / name
    worktree.mkdir()
    _touch_aider_log(worktree)
    return worktree


def _local_warnings(
    caplog: pytest.LogCaptureFixture, marker: str
) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == _LOCAL_LOGGER and marker in r.getMessage()
    ]


def test_capture_local_harvest_facts_only_captures_git_backed_candidates(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    aider_wt = _aider_worktree(tmp_path, "aider")
    oc_wt = tmp_path / "oc-only"
    write_opencode_log(oc_wt, [])
    unproven_wt = _aider_worktree(tmp_path, "unproven")
    write_opencode_log(unproven_wt, [])
    sessions = [
        _dead_session("T-AIDER", aider_wt),
        _dead_session("T-OC-ONLY", oc_wt),
        _dead_session("T-CODEX", _aider_worktree(tmp_path, "codex"), backend="codex"),
        _dead_session("T-UNPROVEN", unproven_wt, spawn_stage=Stage.FINALIZE),
        _dead_session("T-NO-WT", None),
    ]
    state, rows = _save(sessions)
    facts = HarvestFacts()
    source = MagicMock(return_value=_facts())

    with patch.object(harvest_synthesis, "git_facts", source):
        capture_local_harvest_facts(state, rows, facts=facts)

    source.assert_called_once_with(aider_wt, _BRANCH)
    assert facts.lookup("T-AIDER", _HANDLE, aider_wt, _BRANCH) == _facts()
    for sid, worktree in (("T-OC-ONLY", oc_wt), ("T-UNPROVEN", unproven_wt)):
        with pytest.raises(HarvestFactsUnavailableError, match="never captured"):
            facts.lookup(sid, _HANDLE, worktree, _BRANCH)


def test_capture_local_harvest_facts_timeout_defers_and_warns(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hung git defers its candidate with one signal-only advisory; the sweep
    continues, and nothing is stored, latched or emitted."""
    hung_wt = _aider_worktree(tmp_path, "hung")
    ok_wt = _aider_worktree(tmp_path, "ok")
    state, rows = _save(
        [_dead_session("T-HUNG", hung_wt), _dead_session("T-OK", ok_wt)]
    )
    timeout = subprocess.TimeoutExpired(["git", "log"], 10.0)
    monkeypatch.setattr(
        harvest_synthesis, "git_facts", MagicMock(side_effect=[timeout, _facts()])
    )
    monkeypatch.setattr(
        reconcile_local, "monotonic", MagicMock(side_effect=[0.0, 12.5, 20.0])
    )
    facts = HarvestFacts()

    with caplog.at_level(logging.WARNING):
        capture_local_harvest_facts(state, rows, facts=facts)

    with pytest.raises(HarvestFactsUnavailableError, match="never captured"):
        facts.lookup("T-HUNG", _HANDLE, hung_wt, _BRANCH)
    assert facts.lookup("T-OK", _HANDLE, ok_wt, _BRANCH) == _facts()
    [advisory] = _local_warnings(caplog, "harvest_git_timeout")
    assert advisory.getMessage() == (
        "harvest_git_timeout: session=T-HUNG ticket=T-HUNG elapsed=12.5s;"
        " deferring to the next tick (GitHub #2565)"
    )
    assert advisory.exc_info is None
    assert read_events() == []


def test_capture_local_harvest_facts_budget_exhausted_warns_once_and_stops(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(harvest_synthesis, "monotonic", lambda: clock["now"])
    facts = HarvestFacts(budget_seconds=60.0)

    def _slow(*_args: object) -> GitFacts:
        clock["now"] += 61.0
        return _facts()

    source = MagicMock(side_effect=_slow)
    monkeypatch.setattr(harvest_synthesis, "git_facts", source)
    first_wt = _aider_worktree(tmp_path, "first")
    state, rows = _save(
        [
            _dead_session("T-FIRST", first_wt),
            _dead_session("T-SECOND", _aider_worktree(tmp_path, "second")),
            _dead_session("T-THIRD", _aider_worktree(tmp_path, "third")),
        ]
    )

    with caplog.at_level(logging.WARNING):
        capture_local_harvest_facts(state, rows, facts=facts)

    assert source.call_count == 1
    assert facts.lookup("T-FIRST", _HANDLE, first_wt, _BRANCH) == _facts()
    [budget] = _local_warnings(caplog, "harvest-facts budget")
    assert budget.getMessage() == (
        "reconcile: harvest-facts budget (60s) spent;"
        " 2 candidate(s) deferred to the next tick"
    )


def test_capture_local_harvest_facts_no_git_candidates_loads_no_clients(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oc_wt = tmp_path / "oc"
    oc_wt.mkdir()
    state, rows = _save([_dead_session("T-OC", oc_wt, backend="opencode")])
    loader = MagicMock()
    monkeypatch.setattr("cw.reconcile._deps.load_effective_clients", loader)

    capture_local_harvest_facts(state, rows, facts=HarvestFacts())

    loader.assert_not_called()
