"""Tests for cw.codex_background — CodexExecutor's backgrounded review worker (#1727).

Covers the concerns the module owns: the daemon-thread launcher plus its
outstanding-thread registry, the bounded join the dispatch loop's shutdown path
drains that registry with, the moved Step 3/4/4b/5 unit of work
(``_run_codex_review_and_complete``) on both its success and exception branches,
and the two helpers that unit of work is the sole consumer of —
``_resolve_codex_fix_loop_enabled`` and ``_post_review_comment``.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw import codex_background
from cw.auto_dev_result import AutoDevResult
from cw.codex_background import (
    _default_background,
    _post_review_comment,
    _resolve_codex_fix_loop_enabled,
    _run_codex_review_and_complete,
    _start_daemon_thread,
    _sync_finding_dispositions_to_running_task,
    join_outstanding_codex_threads,
)
from cw.codex_review import _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS, CODEX_REVIEW_UNPARSEABLE
from cw.config import load_state, save_state
from cw.dev_queue import add_ticket, load_dev_queue
from cw.local_runner import UNEXPECTED_ERROR, make_blocked
from cw.models import (
    ClientConfig,
    CwState,
    LaneConfig,
    LastResultSource,
    OrchestratorConfig,
    QueueItemStatus,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.review_finding_dispositions import FindingDisposition
from tests.conftest import _make_daemon_session

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cw.codex_runner import CodexRunResult


@pytest.fixture(autouse=True)
def _drain_registry() -> Iterator[None]:
    """Never let a test leak a registered thread into the next one."""
    yield
    join_outstanding_codex_threads(timeout_seconds=5.0)
    with codex_background._outstanding_lock:
        codex_background._outstanding.clear()


def _registry_len() -> int:
    with codex_background._outstanding_lock:
        return len(codex_background._outstanding)


# ---------------------------------------------------------------------------
# _start_daemon_thread / _default_background / the outstanding registry
# ---------------------------------------------------------------------------


def test_start_daemon_thread_runs_fn_and_deregisters() -> None:
    """The launched thread runs fn and removes itself from the registry."""
    ran = threading.Event()

    _start_daemon_thread(ran.set, name="codex-test")

    assert ran.wait(timeout=5.0)
    assert join_outstanding_codex_threads(timeout_seconds=5.0) == 0
    assert _registry_len() == 0


def test_start_daemon_thread_deregisters_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising fn still removes its thread from the registry (finally:).

    In production nothing reaches this branch — the only real caller,
    ``_run_codex_review_and_complete``, swallows its own exceptions — but the
    registry must not accumulate dead entries if that ever changes.
    ``threading.excepthook`` is silenced so the deliberate traceback is not
    reported by pytest against whichever test happens to run next.
    """
    entered = threading.Event()
    msg = "boom"

    def _boom() -> None:
        entered.set()
        raise RuntimeError(msg)

    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    _start_daemon_thread(_boom, name="codex-test-exc")

    assert entered.wait(timeout=5.0)
    assert join_outstanding_codex_threads(timeout_seconds=5.0) == 0
    assert _registry_len() == 0


def test_thread_is_registered_while_still_running() -> None:
    """A thread blocked mid-work is visible in the registry (R7(b))."""
    release = threading.Event()
    started = threading.Event()

    def _blocked() -> None:
        started.set()
        release.wait(timeout=10.0)

    _start_daemon_thread(_blocked, name="codex-test-blocked")
    assert started.wait(timeout=5.0)

    assert _registry_len() == 1

    release.set()
    assert join_outstanding_codex_threads(timeout_seconds=5.0) == 0
    assert _registry_len() == 0


def test_default_background_starts_a_daemon_thread() -> None:
    """_default_background is the production seam: a daemon thread, not inline."""
    seen: dict[str, object] = {}
    done = threading.Event()

    def _capture() -> None:
        current = threading.current_thread()
        seen["daemon"] = current.daemon
        seen["name"] = current.name
        seen["is_main"] = current is threading.main_thread()
        done.set()

    _default_background(_capture)

    assert done.wait(timeout=5.0)
    assert seen["daemon"] is True
    assert seen["is_main"] is False
    assert seen["name"] == "codex-review"


# ---------------------------------------------------------------------------
# join_outstanding_codex_threads (R7(b))
# ---------------------------------------------------------------------------


def test_join_returns_zero_when_thread_finishes_within_timeout() -> None:
    done = threading.Event()
    _start_daemon_thread(done.set, name="codex-quick")

    assert join_outstanding_codex_threads(timeout_seconds=5.0) == 0


def test_join_returns_count_still_running_past_deadline() -> None:
    """A thread still blocked at the deadline is counted, not waited out."""
    release = threading.Event()
    started = threading.Event()

    def _blocked() -> None:
        started.set()
        release.wait(timeout=10.0)

    _start_daemon_thread(_blocked, name="codex-slow")
    assert started.wait(timeout=5.0)

    assert join_outstanding_codex_threads(timeout_seconds=0.05) == 1

    # Release and join for real so the thread does not leak into other tests.
    release.set()
    assert join_outstanding_codex_threads(timeout_seconds=5.0) == 0


def test_join_budget_is_shared_across_threads_not_per_thread() -> None:
    """Two blocked threads consume one deadline, not one deadline each.

    Exercises the ``remaining <= 0`` short-circuit: the second thread is never
    joined at all, because the first already spent the whole budget.
    """
    release = threading.Event()
    started = threading.Barrier(3, timeout=5.0)

    def _blocked() -> None:
        started.wait()
        release.wait(timeout=10.0)

    _start_daemon_thread(_blocked, name="codex-slow-1")
    _start_daemon_thread(_blocked, name="codex-slow-2")
    started.wait()

    elapsed_start = time.monotonic()
    assert join_outstanding_codex_threads(timeout_seconds=0.1) == 2
    # Well under 2 x the budget, i.e. the budget was not spent twice.
    assert time.monotonic() - elapsed_start < 1.0

    release.set()
    assert join_outstanding_codex_threads(timeout_seconds=5.0) == 0


def test_join_with_empty_registry_returns_zero() -> None:
    assert join_outstanding_codex_threads(timeout_seconds=0.01) == 0


def test_join_timeout_default_is_bounded_and_short() -> None:
    """The chosen default is a clean-exit budget, not a wait-out-the-review one."""
    assert 0 < codex_background._CODEX_BACKGROUND_JOIN_TIMEOUT_SECONDS <= 30.0


# ---------------------------------------------------------------------------
# R3 docstring breadcrumb — precedent named, ADR trigger flagged
# ---------------------------------------------------------------------------


def test_module_docstring_names_the_threading_precedent() -> None:
    """R3: no ADR, but the second occurrence must name the first one."""
    doc = codex_background.__doc__ or ""
    assert "fire_push_notification" in doc
    assert "ADR" in doc


# ---------------------------------------------------------------------------
# _run_codex_review_and_complete — the moved Step 3/4/4b/5 unit of work
# ---------------------------------------------------------------------------


def _seed_session(sid: str, client_name: str = "test") -> None:
    save_state(CwState(sessions=[_make_daemon_session(id=sid, client=client_name)]))


def _client(worktree: Path) -> ClientConfig:
    return ClientConfig(name="test", workspace_path=worktree, default_branch="main")


class _UnusedRunner:
    """CodexRunner stand-in for tests that patch run_review_with_fix_loop out."""

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        raise AssertionError(self.run.__doc__)


def _run(
    *,
    sid: str,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
) -> None:
    _run_codex_review_and_complete(
        runner=_UnusedRunner(),
        task=task,
        worktree=worktree,
        client=client,
        wall_clock_budget_seconds=None,
        sid=sid,
        sess_name=f"{client.name}/auto-dev/{task.ticket_id}",
        config_model=None,
    )


def test_run_codex_review_and_complete_success_path(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Success: result persisted through the door, SESSION_COMPLETED emitted."""
    worktree = make_git_repo("wt-bg-success")
    _seed_session("bg-ok")
    task = TicketTask(ticket_id="T-ok", client="test", stage=Stage.REVIEW)
    result = make_blocked(
        ticket_id="T-ok",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )

    events: list[object] = []
    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            return_value=(result, None),
        ) as fix_loop_mock,
        patch(
            "cw.codex_background._record_orchestrator_event",
            side_effect=lambda *args, **_kw: events.append(args[0]),
        ),
    ):
        _run(sid="bg-ok", task=task, worktree=worktree, client=_client(worktree))

    fix_loop_mock.assert_called_once()
    assert fix_loop_mock.call_args.kwargs["session_id"] == "bg-ok"
    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.last_result_source is LastResultSource.EXECUTOR_DIRECT
    persisted = AutoDevResult.model_validate(session.last_result)
    assert persisted.blocker is not None
    assert persisted.blocker.reason == CODEX_REVIEW_UNPARSEABLE
    assert len(events) == 1


def test_run_codex_review_and_complete_posts_verdict_comment(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Step 4b still runs from the background thread when a verdict exists."""
    worktree = make_git_repo("wt-bg-verdict")
    _seed_session("bg-verdict")
    task = TicketTask(ticket_id="T-v", client="test", stage=Stage.REVIEW)
    result = make_blocked(
        ticket_id="T-v",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )

    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            return_value=(result, object()),
        ),
        patch("cw.codex_background.render_verdict_comment", return_value="rendered"),
        patch("cw.codex_background._post_review_comment") as post_mock,
    ):
        _run(sid="bg-verdict", task=task, worktree=worktree, client=_client(worktree))

    post_mock.assert_called_once()
    assert post_mock.call_args.args[0] == "T-v"
    assert post_mock.call_args.args[1] == "rendered"
    # #2095: the tracker is resolved from the client's workspace (None here --
    # no project-config.yaml -- so the GitHub post still fires, fail-open) and
    # the durable copy is written before the post is attempted.
    assert post_mock.call_args.kwargs["tracker"] is None
    artifact = post_mock.call_args.kwargs["artifact_path"]
    assert artifact == worktree / ".claude" / "review-verdict.md"
    assert artifact.read_text(encoding="utf-8") == "rendered"


def test_run_codex_review_and_complete_exception_path(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Exception: session COMPLETED/blocked, task reverted, nothing re-raised.

    The worker runs on a daemon thread with no caller to catch for it, so the
    exception must NOT propagate — dispatch's own ``except`` handler (which
    used to do the revert) is no longer on this call stack.
    """
    worktree = make_git_repo("wt-bg-exc")
    _seed_session("bg-exc")
    add_ticket(
        TicketTask(
            ticket_id="T-exc",
            client="test",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id="bg-exc",
        )
    )
    task = TicketTask(ticket_id="T-exc", client="test", stage=Stage.REVIEW)

    events: list[object] = []
    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            side_effect=RuntimeError("git boom"),
        ),
        patch(
            "cw.codex_background._record_orchestrator_event",
            side_effect=lambda *args, **_kw: events.append(args[0]),
        ),
    ):
        # No pytest.raises: the worker must swallow, not propagate.
        _run(sid="bg-exc", task=task, worktree=worktree, client=_client(worktree))

    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    persisted = AutoDevResult.model_validate(session.last_result)
    assert persisted.status == "blocked"
    assert persisted.blocker is not None
    assert persisted.blocker.reason == UNEXPECTED_ERROR
    assert persisted.next_actions == _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS
    # SESSION_COMPLETED is deliberately NOT emitted on the failure branch.
    assert events == []
    # The claimed task is handed back for a later tick, with backoff stamped.
    stored = load_dev_queue().tasks[0]
    assert stored.status is QueueItemStatus.PENDING
    assert stored.session_id is None
    assert stored.spawn_error_count == 1
    assert stored.next_eligible_at is not None


# ---------------------------------------------------------------------------
# ExecutorBlockedMarker lifecycle around the review (#1742)
# ---------------------------------------------------------------------------


def test_run_codex_review_and_complete_sets_marker_and_clears_on_success(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A marker is live for the whole review and gone once it returns (#1742)."""
    from datetime import UTC, datetime

    from cw.dispatch_state import load_executor_blocked_markers

    worktree = make_git_repo("wt-bg-marker")
    _seed_session("bg-marker")
    task = TicketTask(ticket_id="T-mark", client="test", stage=Stage.REVIEW)
    result = make_blocked(
        ticket_id="T-mark",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )
    seen: list[object] = []

    def _capture_marker(**_kwargs: object) -> tuple[object, None]:
        seen.append(load_executor_blocked_markers())
        return (result, None)

    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            side_effect=_capture_marker,
        ),
        patch("cw.codex_background._record_orchestrator_event"),
    ):
        _run(sid="bg-marker", task=task, worktree=worktree, client=_client(worktree))

    assert len(seen) == 1
    during = seen[0]
    assert isinstance(during, dict)
    assert list(during) == ["test/T-mark"]
    marker = during["test/T-mark"]
    assert marker.client == "test"
    assert marker.ticket_id == "T-mark"
    assert marker.executor == "codex"
    assert marker.session_id == "bg-marker"
    assert abs((datetime.now(UTC) - marker.started_at).total_seconds()) < 60
    # Cleared by the finally: once the review is done.
    assert load_executor_blocked_markers() == {}


def test_run_codex_review_and_complete_clears_marker_on_exception(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The finally: must fire on the failure branch too (#1742)."""
    from cw.dispatch_state import load_executor_blocked_markers

    worktree = make_git_repo("wt-bg-marker-exc")
    _seed_session("bg-marker-exc")
    add_ticket(
        TicketTask(
            ticket_id="T-mark-exc",
            client="test",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id="bg-marker-exc",
        )
    )
    task = TicketTask(ticket_id="T-mark-exc", client="test", stage=Stage.REVIEW)

    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            side_effect=RuntimeError("git boom"),
        ),
        patch("cw.codex_background._record_orchestrator_event"),
    ):
        _run(
            sid="bg-marker-exc",
            task=task,
            worktree=worktree,
            client=_client(worktree),
        )

    assert load_executor_blocked_markers() == {}


def test_run_codex_review_and_complete_marker_cleared_after_verdict_posting(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Marker is live during the review and cleared after Step 4b (#1742)."""
    from cw.dispatch_state import load_executor_blocked_markers

    worktree = make_git_repo("wt-bg-marker-verdict")
    _seed_session("bg-marker-verdict")
    task = TicketTask(ticket_id="T-mark-v", client="test", stage=Stage.REVIEW)
    result = make_blocked(
        ticket_id="T-mark-v",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )
    during: list[int] = []

    def _capture_marker(**_kwargs: object) -> tuple[object, object]:
        during.append(len(load_executor_blocked_markers()))
        return (result, object())

    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            side_effect=_capture_marker,
        ),
        patch("cw.codex_background.render_verdict_comment", return_value="rendered"),
        patch("cw.codex_background._post_review_comment") as post_mock,
    ):
        _run(
            sid="bg-marker-verdict",
            task=task,
            worktree=worktree,
            client=_client(worktree),
        )

    assert during == [1]
    post_mock.assert_called_once()
    assert load_executor_blocked_markers() == {}


# ---------------------------------------------------------------------------
# _resolve_codex_fix_loop_enabled precedence (#1553)
# ---------------------------------------------------------------------------


def test_resolve_codex_fix_loop_enabled_lane_true_wins_regardless_of_global() -> None:
    client = ClientConfig(
        name="test",
        workspace_path=Path("/tmp/x"),
        lanes=[LaneConfig(name="trial", codex_fix_loop_enabled=True)],
    )
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW, lane="trial")
    config = OrchestratorConfig(default_codex_fix_loop_enabled=False)

    assert _resolve_codex_fix_loop_enabled(client, task, config) is True


def test_resolve_codex_fix_loop_enabled_lane_unset_global_true() -> None:
    client = ClientConfig(
        name="test",
        workspace_path=Path("/tmp/x"),
        lanes=[LaneConfig(name="trial")],
    )
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW, lane="trial")
    config = OrchestratorConfig(default_codex_fix_loop_enabled=True)

    assert _resolve_codex_fix_loop_enabled(client, task, config) is True


def test_resolve_codex_fix_loop_enabled_lane_unset_global_default_false() -> None:
    client = ClientConfig(
        name="test",
        workspace_path=Path("/tmp/x"),
        lanes=[LaneConfig(name="trial")],
    )
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW, lane="trial")
    config = OrchestratorConfig()

    assert _resolve_codex_fix_loop_enabled(client, task, config) is False


def test_resolve_codex_fix_loop_enabled_unmatched_lane_falls_through_to_global() -> (
    None
):
    client = ClientConfig(
        name="test",
        workspace_path=Path("/tmp/x"),
        lanes=[LaneConfig(name="trial", codex_fix_loop_enabled=True)],
    )
    task = TicketTask(
        ticket_id="T-1", client="test", stage=Stage.REVIEW, lane="no-such-lane"
    )
    config = OrchestratorConfig(default_codex_fix_loop_enabled=True)

    assert _resolve_codex_fix_loop_enabled(client, task, config) is True


# ---------------------------------------------------------------------------
# _post_review_comment — Step 4b's best-effort GitHub write
# ---------------------------------------------------------------------------


def test_post_review_comment_suppresses_oserror() -> None:
    """_post_review_comment swallows OSError from a missing gh binary."""
    with patch("cw.gh._sp.run", side_effect=FileNotFoundError("no gh")):
        _post_review_comment("T-1", "findings")


def test_post_review_comment_suppresses_timeout() -> None:
    """_post_review_comment swallows TimeoutExpired when gh hangs."""
    with patch(
        "cw.gh._sp.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        _post_review_comment("T-1", "findings")


def test_post_review_comment_forwards_cwd() -> None:
    """#1279: _post_review_comment scopes the gh call to the client's repo."""
    want_cwd = Path("/some/client-a/repo")
    with patch("cw.codex_background.post_issue_comment") as post_mock:
        _post_review_comment("T-1", "findings", cwd=want_cwd)
    post_mock.assert_called_once_with("T-1", "findings", cwd=want_cwd)


def test_post_review_comment_logs_on_none_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """gh call couldn't run at all (missing binary / timeout) -> warning, not silent."""
    with (
        patch("cw.codex_background.post_issue_comment", return_value=None),
        caplog.at_level("WARNING"),
    ):
        _post_review_comment("T-1", "findings", cwd=None)
    assert any(
        "T-1" in r.message and "gh call failed" in r.message for r in caplog.records
    )


def test_post_review_comment_skips_gh_on_non_github_tracker(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """#2095: a Linear-tracked ticket never reaches gh (the call could only
    fail with 'invalid issue format'); the skip is a WARNING that names the
    tracker and the durable artifact, not a swallowed subprocess failure."""
    artifact = tmp_path / ".claude" / "review-verdict.md"
    with (
        patch("cw.codex_background.post_issue_comment") as post_mock,
        caplog.at_level("WARNING"),
    ):
        _post_review_comment(
            "GEN-1", "findings", cwd=None, tracker="linear", artifact_path=artifact
        )
    post_mock.assert_not_called()
    assert any(
        "review_comment_skipped" in r.message
        and "GEN-1" in r.message
        and "linear" in r.message
        and str(artifact) in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("tracker", [None, "github-issues"])
def test_post_review_comment_posts_on_github_or_unknown_tracker(
    tracker: str | None,
) -> None:
    """Fail-open: an unresolvable tracker, or a positively-GitHub one, posts."""
    with patch("cw.codex_background.post_issue_comment") as post_mock:
        post_mock.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=b"", stderr=b""
        )
        _post_review_comment("T-1", "findings", cwd=None, tracker=tracker)
    post_mock.assert_called_once_with("T-1", "findings", cwd=None)


def test_persist_review_verdict_writes_durable_copy(tmp_path: Path) -> None:
    """#2095: the rendered verdict lands in .claude/review-verdict.md."""
    from cw.codex_background import (
        REVIEW_VERDICT_COMMENT_RELATIVE_PATH,
        _persist_review_verdict,
    )

    path = _persist_review_verdict(tmp_path, "## Verdict\n")
    assert path == tmp_path / REVIEW_VERDICT_COMMENT_RELATIVE_PATH
    assert path.read_text(encoding="utf-8") == "## Verdict\n"


def test_persist_review_verdict_degrades_on_oserror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A write failure is logged and returns None; it never raises into the
    daemon thread's success path."""
    from cw.codex_background import _persist_review_verdict

    (tmp_path / ".claude").write_text("not a directory", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert _persist_review_verdict(tmp_path, "x") is None
    assert any("review_verdict_persist_failed" in r.message for r in caplog.records)


def test_post_review_comment_logs_on_nonzero_returncode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-zero gh exit -> warning carries ticket_id, returncode, and stderr."""
    fake_result = subprocess.CompletedProcess(
        args=["gh"], returncode=1, stdout=b"", stderr=b"invalid issue format"
    )
    with (
        patch("cw.codex_background.post_issue_comment", return_value=fake_result),
        caplog.at_level("WARNING"),
    ):
        _post_review_comment("T-1", "findings", cwd=None)
    assert any(
        "T-1" in r.message and "1" in r.message and "invalid issue format" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# _sync_finding_dispositions_to_running_task (#1838)
# ---------------------------------------------------------------------------


def _disposition(**overrides: object) -> FindingDisposition:
    payload: dict[str, object] = {
        "outcome": "REJECTED",
        "rationale": "settled by the operator",
        "recorded_at": "2026-08-16T00:00:00Z",
    }
    payload.update(overrides)
    return FindingDisposition.model_validate(payload)


class TestSyncFindingDispositionsToRunningTask:
    """#1838 R1: the merged ledger is persisted onto the RUNNING queue row.

    Structurally identical to ``_stamp_session_id_on_running_task``'s
    lock/load/find-matching-RUNNING-row/mutate/save shape.
    """

    def _running(self, **overrides: object) -> TicketTask:
        payload: dict[str, object] = {
            "ticket_id": "T-1838",
            "client": "test",
            "stage": Stage.REVIEW,
            "status": QueueItemStatus.RUNNING,
        }
        payload.update(overrides)
        return TicketTask.model_validate(payload)

    def test_merges_entries_onto_the_matching_running_row(self) -> None:
        add_ticket(self._running())
        _sync_finding_dispositions_to_running_task(
            client_name="test",
            ticket_id="T-1838",
            dispositions={"src/cw/foo.py::bug here": _disposition()},
        )
        stored = load_dev_queue().tasks[0]
        assert stored.finding_dispositions["src/cw/foo.py::bug here"].outcome == (
            "REJECTED"
        )

    def test_merge_is_additive_and_idempotent(self) -> None:
        add_ticket(
            self._running(
                finding_dispositions={"src/cw/old.py::old bug": _disposition()}
            )
        )
        fresh = {"src/cw/foo.py::bug here": _disposition()}
        _sync_finding_dispositions_to_running_task(
            client_name="test", ticket_id="T-1838", dispositions=fresh
        )
        _sync_finding_dispositions_to_running_task(
            client_name="test", ticket_id="T-1838", dispositions=fresh
        )
        stored = load_dev_queue().tasks[0]
        assert set(stored.finding_dispositions) == {
            "src/cw/old.py::old bug",
            "src/cw/foo.py::bug here",
        }

    def test_no_matching_running_row_is_a_no_op(self) -> None:
        add_ticket(self._running(status=QueueItemStatus.PENDING))
        _sync_finding_dispositions_to_running_task(
            client_name="test",
            ticket_id="T-1838",
            dispositions={"src/cw/foo.py::bug here": _disposition()},
        )
        assert load_dev_queue().tasks[0].finding_dispositions == {}

    def test_empty_dispositions_is_a_no_op(self) -> None:
        add_ticket(self._running())
        _sync_finding_dispositions_to_running_task(
            client_name="test", ticket_id="T-1838", dispositions={}
        )
        assert load_dev_queue().tasks[0].finding_dispositions == {}
