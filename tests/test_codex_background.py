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
    join_outstanding_codex_threads,
)
from cw.codex_review import CODEX_REVIEW_UNPARSEABLE
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
    assert fix_loop_mock.call_args.kwargs["fix_lean_profile_mode"] == "off"
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
    # SESSION_COMPLETED is deliberately NOT emitted on the failure branch.
    assert events == []
    # The claimed task is handed back for a later tick, with backoff stamped.
    stored = load_dev_queue().tasks[0]
    assert stored.status is QueueItemStatus.PENDING
    assert stored.session_id is None
    assert stored.spawn_error_count == 1
    assert stored.next_eligible_at is not None


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
