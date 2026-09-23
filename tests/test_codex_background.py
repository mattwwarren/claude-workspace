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
from unittest.mock import ANY, MagicMock, patch

import pytest

from cw import codex_background
from cw.auto_dev_result import AutoDevResult, Blocker
from cw.codex_background import (
    _DEFAULT_CODEX_REVIEW_TIER_ENABLED,
    REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH,
    REVIEW_VERDICT_JSON_RELATIVE_PATH,
    REVIEW_VERDICT_OWNER_STAMP_FORMAT,
    _default_background,
    _persist_review_verdict,
    _post_review_comment,
    _resolve_claim_tier_enabled,
    _resolve_codex_fix_loop_enabled,
    _resolve_disposition_drift_check_enabled,
    _run_codex_review_and_complete,
    _start_daemon_thread,
    _sync_finding_dispositions_to_running_task,
    join_outstanding_codex_threads,
)
from cw.codex_review import (
    _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_REVIEW_UNPARSEABLE,
)
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
from cw.review_finding_dispositions import FindingDisposition, _disposition_key
from cw.review_findings import ReviewVerdictEnvelope, consolidate_verdict
from tests.conftest import (
    _make_daemon_session,
    _make_diff,
    _make_finding,
    _make_reviewer_doc,
)

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
        config_reasoning_effort=None,
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
        # #2280: verdict=None now takes the zero-documents park/post branch
        # (see test_run_codex_review_and_complete_posts_one_line_comment_
        # when_verdict_is_none below) — patched here so this general
        # success-path test stays hermetic instead of shelling out to gh.
        patch("cw.codex_background._post_review_comment") as post_mock,
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
    post_mock.assert_called_once()


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
    verdict = consolidate_verdict(
        [_make_reviewer_doc(_make_finding(severity="MUST_FIX"))],
        _make_diff(),
        reviewed_sha="deadbeef",
    )

    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            return_value=(result, verdict),
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
    written = artifact.read_text(encoding="utf-8")
    assert (
        written.splitlines()[0]
        == REVIEW_VERDICT_OWNER_STAMP_FORMAT.format(
            ticket_id="T-v", reviewed_sha="deadbeef"
        ).splitlines()[0]
    )
    assert written.endswith("rendered")
    # #2223: the structured verdict envelope lands beside the rendered .md,
    # through the same _persist_review_verdict stamp/write path (round 1 fix:
    # no second persist implementation) -- so the JSON body follows the same
    # ownership-stamp first line as the .md sibling.
    json_written = (worktree / REVIEW_VERDICT_JSON_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )
    json_lines = json_written.split("\n", 1)
    assert (
        json_lines[0]
        == REVIEW_VERDICT_OWNER_STAMP_FORMAT.format(
            ticket_id="T-v", reviewed_sha="deadbeef"
        ).splitlines()[0]
    )
    envelope = ReviewVerdictEnvelope.model_validate_json(json_lines[1])
    assert envelope.ticket_id == "T-v"
    assert envelope.verdict.reviewed_sha == "deadbeef"


def test_run_codex_review_and_complete_posts_one_line_comment_when_verdict_is_none(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2280: a zero-documents park (verdict=None) now persists + posts too.

    Before this ticket, Step 4b's ``if verdict is not None:`` gate meant a
    park where every reviewer failed left no worktree artifact and no ticket
    comment -- the operator saw only the coarse ``codex_review_unparseable``
    reason with nothing to diagnose it. The new ``elif result.blocker is not
    None:`` arm reuses ``result.blocker.details`` (already the per-role
    ``role (reason)`` summary ``_format_failures_detail`` built) as the
    comment text, mirroring the existing verdict-present branch's shape.
    """
    worktree = make_git_repo("wt-bg-unparseable")
    _seed_session("bg-unparseable")
    task = TicketTask(ticket_id="T-up", client="test", stage=Stage.REVIEW)
    result = make_blocked(
        ticket_id="T-up",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        details="editor (codex_timeout); reviewer (codex_timeout)",
        stage_reached="stage3_review",
    )

    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            return_value=(result, None),
        ),
        patch("cw.codex_background._post_review_comment") as post_mock,
    ):
        _run(
            sid="bg-unparseable", task=task, worktree=worktree, client=_client(worktree)
        )

    post_mock.assert_called_once()
    assert post_mock.call_args.args[0] == "T-up"
    assert (
        post_mock.call_args.args[1]
        == "editor (codex_timeout); reviewer (codex_timeout)"
    )
    assert post_mock.call_args.kwargs["tracker"] is None
    artifact = post_mock.call_args.kwargs["artifact_path"]
    assert artifact == worktree / ".claude" / "review-verdict-unparseable.md"
    written = artifact.read_text(encoding="utf-8")
    assert "codex_review_unparseable" in written
    assert "editor (codex_timeout); reviewer (codex_timeout)" in written


def test_run_codex_review_and_complete_skips_render_when_verdict_is_none(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2280: no ``ReviewVerdict`` means nothing for the renderer to render.

    ``render_verdict_comment`` needs a ``ReviewVerdict`` -- there is none on
    the zero-documents path -- so the new ``elif`` arm must not call it.
    ``_persist_review_verdict`` DOES still run on this path (#2280 round 2):
    it is the shared writer :func:`_persist_unparseable_artifact` delegates
    to, pointed at the unparseable artifact's own path.
    """
    worktree = make_git_repo("wt-bg-unparseable-skip")
    _seed_session("bg-unparseable-skip")
    task = TicketTask(ticket_id="T-ups", client="test", stage=Stage.REVIEW)
    result = make_blocked(
        ticket_id="T-ups",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        details="reviewer (invalid_json)",
        stage_reached="stage3_review",
    )

    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            return_value=(result, None),
        ),
        patch("cw.codex_background.render_verdict_comment") as render_mock,
        patch(
            "cw.codex_background._persist_review_verdict", wraps=_persist_review_verdict
        ) as persist_mock,
        patch("cw.codex_background._post_review_comment"),
    ):
        _run(
            sid="bg-unparseable-skip",
            task=task,
            worktree=worktree,
            client=_client(worktree),
        )

    render_mock.assert_not_called()
    persist_mock.assert_called_once_with(
        worktree,
        ANY,
        ticket_id="T-ups",
        reviewed_sha=ANY,
        relative_path=REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH,
    )


def test_run_codex_review_and_complete_skips_structured_verdict_when_verdict_is_none(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2223: no ReviewVerdict means no structured JSON artifact to write —
    only the unparseable-park .md artifact lands."""
    worktree = make_git_repo("wt-bg-no-json")
    _seed_session("bg-no-json")
    task = TicketTask(ticket_id="T-nj", client="test", stage=Stage.REVIEW)
    result = make_blocked(
        ticket_id="T-nj",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        details="reviewer (invalid_json)",
        stage_reached="stage3_review",
    )

    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            return_value=(result, None),
        ),
        patch("cw.codex_background._post_review_comment"),
    ):
        _run(
            sid="bg-no-json",
            task=task,
            worktree=worktree,
            client=_client(worktree),
        )

    assert not (worktree / REVIEW_VERDICT_JSON_RELATIVE_PATH).exists()
    assert (worktree / REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH).exists()


def test_run_codex_review_and_complete_writes_structured_verdict_json_on_must_fix_park(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2223 repro: fix loop disabled, cycle-0 verdict blocking, park on
    CODEX_MUST_FIX_FINDINGS — the structured verdict must still land so an
    operator adjudicating the park has ``check-voided``'s evidence anchor."""
    worktree = make_git_repo("wt-bg-must-fix-json")
    _seed_session("bg-must-fix-json")
    task = TicketTask(ticket_id="T-mf", client="test", stage=Stage.REVIEW)
    result = make_blocked(
        ticket_id="T-mf",
        worktree=worktree,
        reason=CODEX_MUST_FIX_FINDINGS,
        stage_reached="stage3_review",
    )
    verdict = consolidate_verdict(
        [_make_reviewer_doc(_make_finding(severity="MUST_FIX"))],
        _make_diff(),
        reviewed_sha="deadbeef",
    )
    assert verdict.blocking is True

    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            return_value=(result, verdict),
        ),
        patch("cw.codex_background._post_review_comment"),
    ):
        _run(
            sid="bg-must-fix-json",
            task=task,
            worktree=worktree,
            client=_client(worktree),
        )

    json_written = (worktree / REVIEW_VERDICT_JSON_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )
    envelope = ReviewVerdictEnvelope.model_validate_json(json_written.split("\n", 1)[1])
    assert envelope.ticket_id == "T-mf"
    assert envelope.verdict.must_fix[0].evidence == "def broken():"
    assert envelope.verdict.accepted[0].finding.evidence == "def broken():"


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
    verdict = consolidate_verdict(
        [_make_reviewer_doc(_make_finding(severity="MUST_FIX"))],
        _make_diff(),
        reviewed_sha="deadbeef",
    )
    during: list[int] = []

    def _capture_marker(**_kwargs: object) -> tuple[object, object]:
        during.append(len(load_executor_blocked_markers()))
        return (result, verdict)

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
    artifact = post_mock.call_args.kwargs["artifact_path"]
    written = artifact.read_text(encoding="utf-8")
    assert (
        written.splitlines()[0]
        == REVIEW_VERDICT_OWNER_STAMP_FORMAT.format(
            ticket_id="T-mark-v", reviewed_sha="deadbeef"
        ).splitlines()[0]
    )
    assert written.endswith("rendered")
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
# _resolve_claim_tier_enabled precedence (#2210)
# ---------------------------------------------------------------------------


def _claim_client(*lanes: LaneConfig) -> ClientConfig:
    return ClientConfig(name="test", workspace_path=Path("/tmp/x"), lanes=list(lanes))


def _claim_task(lane: str = "trial") -> TicketTask:
    return TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW, lane=lane)


@pytest.mark.parametrize(
    ("lanes", "task_lane", "master", "expected"),
    [
        # Master off is a kill switch: an armed lane cannot override it.
        (
            [LaneConfig(name="trial", codex_review_tiers={"claim_suppression": True})],
            "trial",
            False,
            False,
        ),
        # Master on, lane silent on the key -> the hardcoded-off floor.
        ([LaneConfig(name="trial")], "trial", True, False),
        (
            [LaneConfig(name="trial", codex_review_tiers={"claim_suppression": True})],
            "trial",
            True,
            True,
        ),
        (
            [LaneConfig(name="trial", codex_review_tiers={"claim_suppression": False})],
            "trial",
            True,
            False,
        ),
        # The task's lane is not declared by the client -> floor.
        (
            [LaneConfig(name="trial", codex_review_tiers={"claim_suppression": True})],
            "no-such-lane",
            True,
            False,
        ),
        # No declared lanes at all: a synthesised `default` lane carries no
        # tier map, so arming requires declaring the lane.
        ([], "default", True, False),
    ],
)
def test_resolve_claim_tier_enabled_table(
    lanes: list[LaneConfig], task_lane: str, master: bool, expected: bool
) -> None:
    config = OrchestratorConfig(codex_claim_suppression_enabled=master)
    resolved = _resolve_claim_tier_enabled(
        _claim_client(*lanes), _claim_task(task_lane), config
    )
    assert resolved is expected


def test_resolve_claim_tier_enabled_arms_only_the_named_lane() -> None:
    client = _claim_client(
        LaneConfig(name="armed", codex_review_tiers={"claim_suppression": True}),
        LaneConfig(name="quiet"),
    )
    config = OrchestratorConfig(codex_claim_suppression_enabled=True)
    assert _resolve_claim_tier_enabled(client, _claim_task("armed"), config) is True
    assert _resolve_claim_tier_enabled(client, _claim_task("quiet"), config) is False


def test_default_codex_review_tier_floor_is_off() -> None:
    assert _DEFAULT_CODEX_REVIEW_TIER_ENABLED == {"claim_suppression": False}


@pytest.mark.parametrize(
    ("lanes", "master", "expected"),
    [
        (
            [LaneConfig(name="trial", codex_review_tiers={"claim_suppression": True})],
            True,
            True,
        ),
        ([LaneConfig(name="trial")], False, False),
    ],
)
def test_run_codex_review_and_complete_forwards_claim_tier(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    lanes: list[LaneConfig],
    master: bool,
    expected: bool,
) -> None:
    worktree = make_git_repo(f"wt-bg-claim-{master}")
    _seed_session("bg-claim")
    task = TicketTask(
        ticket_id="T-claim", client="test", stage=Stage.REVIEW, lane="trial"
    )
    result = make_blocked(
        ticket_id="T-claim",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )
    client = ClientConfig(
        name="test",
        workspace_path=worktree,
        default_branch="main",
        lanes=list(lanes),
    )
    with (
        patch(
            "cw.codex_background.load_effective_config",
            return_value=OrchestratorConfig(codex_claim_suppression_enabled=master),
        ),
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            return_value=(result, None),
        ) as fix_loop_mock,
    ):
        _run(sid="bg-claim", task=task, worktree=worktree, client=client)

    assert fix_loop_mock.call_args.kwargs["claim_tier_enabled"] is expected


# ---------------------------------------------------------------------------
# _resolve_disposition_drift_check_enabled + the claim-tier arming gate (#2232)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lanes", "task_lane", "global_default", "expected"),
    [
        # A lane override wins in BOTH directions — off against an on global…
        (
            [LaneConfig(name="trial", disposition_drift_check_enabled=False)],
            "trial",
            True,
            False,
        ),
        # …and on against an off one. There is no master kill switch here.
        (
            [LaneConfig(name="trial", disposition_drift_check_enabled=True)],
            "trial",
            False,
            True,
        ),
        # Lane silent on the key -> the global, which defaults ON.
        ([LaneConfig(name="trial")], "trial", True, True),
        ([LaneConfig(name="trial")], "trial", False, False),
        # The task's lane is not declared by the client -> the global.
        (
            [LaneConfig(name="trial", disposition_drift_check_enabled=False)],
            "no-such-lane",
            True,
            True,
        ),
        # No declared lanes at all: the synthesised `default` lane carries no
        # override, so the global stands. Unlike the claim tier, that is ON.
        ([], "default", True, True),
    ],
)
def test_resolve_disposition_drift_check_enabled_table(
    lanes: list[LaneConfig],
    task_lane: str,
    global_default: bool,
    expected: bool,
) -> None:
    config = OrchestratorConfig(disposition_drift_check_enabled=global_default)
    resolved = _resolve_disposition_drift_check_enabled(
        _claim_client(*lanes), _claim_task(task_lane), config
    )
    assert resolved is expected


def test_resolve_disposition_drift_check_enabled_scopes_to_the_named_lane() -> None:
    client = _claim_client(
        LaneConfig(name="off", disposition_drift_check_enabled=False),
        LaneConfig(name="quiet"),
    )
    config = OrchestratorConfig()
    assert (
        _resolve_disposition_drift_check_enabled(client, _claim_task("off"), config)
        is False
    )
    assert (
        _resolve_disposition_drift_check_enabled(client, _claim_task("quiet"), config)
        is True
    )


def _run_with_config(
    tmp_worktree: Path,
    *,
    sid: str,
    lanes: list[LaneConfig],
    config: OrchestratorConfig,
) -> MagicMock:
    """Run the review unit of work under *config*, returning the loop mock."""
    _seed_session(sid)
    task = TicketTask(
        ticket_id="T-arm", client="test", stage=Stage.REVIEW, lane="trial"
    )
    result = make_blocked(
        ticket_id="T-arm",
        worktree=tmp_worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )
    client = ClientConfig(
        name="test",
        workspace_path=tmp_worktree,
        default_branch="main",
        lanes=list(lanes),
    )
    with (
        patch("cw.codex_background.load_effective_config", return_value=config),
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            return_value=(result, None),
        ) as fix_loop_mock,
    ):
        _run(sid=sid, task=task, worktree=tmp_worktree, client=client)
    return fix_loop_mock


@pytest.mark.parametrize(
    ("claim_tier", "drift_check"),
    [(True, True), (False, True), (False, False)],
)
def test_arming_gate_lets_the_other_three_cells_through(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    claim_tier: bool,
    drift_check: bool,
) -> None:
    """Only claim-tier-on + drift-check-off is refused (#2232)."""
    worktree = make_git_repo(f"wt-bg-arm-{claim_tier}-{drift_check}")
    fix_loop_mock = _run_with_config(
        worktree,
        sid=f"bg-arm-{claim_tier}-{drift_check}",
        lanes=[
            LaneConfig(
                name="trial",
                codex_review_tiers={"claim_suppression": claim_tier},
                disposition_drift_check_enabled=drift_check,
            )
        ],
        config=OrchestratorConfig(codex_claim_suppression_enabled=True),
    )

    fix_loop_mock.assert_called_once()
    kwargs = fix_loop_mock.call_args.kwargs
    assert kwargs["claim_tier_enabled"] is claim_tier
    assert kwargs["disposition_drift_check_enabled"] is drift_check


def test_arming_the_claim_tier_with_the_drift_check_off_is_refused(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2232: drift-checking is the claim tier's arming precondition.

    The refusal reaches the operator through the daemon thread's existing
    broad ``except Exception`` — session COMPLETED/blocked with
    ``UNEXPECTED_ERROR``, task handed back to PENDING with backoff — the exact
    contract ``test_run_codex_review_and_complete_exception_path`` already
    pins for any exception raised inside that ``try``. Nothing new is plumbed;
    the validator only has to raise.
    """
    worktree = make_git_repo("wt-bg-arm-refused")
    add_ticket(
        TicketTask(
            ticket_id="T-arm",
            client="test",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id="bg-arm-refused",
        )
    )
    fix_loop_mock = _run_with_config(
        worktree,
        sid="bg-arm-refused",
        lanes=[
            LaneConfig(
                name="trial",
                codex_review_tiers={"claim_suppression": True},
                disposition_drift_check_enabled=False,
            )
        ],
        config=OrchestratorConfig(codex_claim_suppression_enabled=True),
    )

    # The review never ran: the refusal fires before the fix loop is entered.
    fix_loop_mock.assert_not_called()
    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    persisted = AutoDevResult.model_validate(session.last_result)
    assert persisted.blocker is not None
    assert persisted.blocker.reason == UNEXPECTED_ERROR
    stored = load_dev_queue().tasks[0]
    assert stored.status is QueueItemStatus.PENDING
    assert stored.spawn_error_count == 1


def test_the_arming_refusal_message_names_both_settings_and_the_fix(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The message IS the operator-facing surface, via ``_log.exception``."""
    import logging

    worktree = make_git_repo("wt-bg-arm-message")
    with caplog.at_level(logging.ERROR, logger="cw.codex_background"):
        _run_with_config(
            worktree,
            sid="bg-arm-message",
            lanes=[
                LaneConfig(
                    name="trial",
                    codex_review_tiers={"claim_suppression": True},
                    disposition_drift_check_enabled=False,
                )
            ],
            config=OrchestratorConfig(codex_claim_suppression_enabled=True),
        )

    logged = "\n".join(
        record.getMessage()
        + ("" if record.exc_info is None else str(record.exc_info[1]))
        for record in caplog.records
    )
    assert "disposition_drift_check_enabled" in logged
    assert "codex_claim_suppression_enabled" in logged


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
    """#2095: the rendered verdict lands in .claude/review-verdict.md, stamped
    with its owning ticket_id/reviewed_sha (#2279)."""
    from cw.codex_background import (
        REVIEW_VERDICT_COMMENT_RELATIVE_PATH,
        _persist_review_verdict,
    )

    path = _persist_review_verdict(
        tmp_path, "## Verdict\n", ticket_id="2279", reviewed_sha="deadbeef"
    )
    assert path == tmp_path / REVIEW_VERDICT_COMMENT_RELATIVE_PATH
    written = path.read_text(encoding="utf-8")
    assert (
        written.splitlines()[0]
        == REVIEW_VERDICT_OWNER_STAMP_FORMAT.format(
            ticket_id="2279", reviewed_sha="deadbeef"
        ).splitlines()[0]
    )
    assert written.endswith("## Verdict\n")


def test_review_verdict_file_is_git_ignored() -> None:
    """#2279: the per-ticket verdict must never be committed — tracked, it
    merged to main and every sibling branch inherited another ticket's verdict,
    which finalize then read as its own (#2205)."""
    repo_root = Path(__file__).resolve().parent.parent
    ignored = (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/review-verdict.md" in ignored
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".claude/review-verdict.md"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode != 0, "review-verdict.md is still tracked"


def test_review_verdict_json_file_is_git_ignored() -> None:
    """#2223: the structured verdict envelope is a per-ticket artifact too —
    same never-committed contract as its .md sibling (#2279/#2205)."""
    repo_root = Path(__file__).resolve().parent.parent
    ignored = (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/review-verdict.json" in ignored
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".claude/review-verdict.json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode != 0, "review-verdict.json is still tracked"


def test_persist_review_verdict_stamp_handles_hyphenated_ticket_id(
    tmp_path: Path,
) -> None:
    """A Linear-style ticket_id (e.g. GEN-1) keeps the stamp line parse-friendly."""
    from cw.codex_background import _persist_review_verdict

    path = _persist_review_verdict(
        tmp_path, "## Verdict\n", ticket_id="GEN-1", reviewed_sha="cafef00d"
    )
    assert path is not None
    written = path.read_text(encoding="utf-8")
    assert (
        written.splitlines()[0]
        == REVIEW_VERDICT_OWNER_STAMP_FORMAT.format(
            ticket_id="GEN-1", reviewed_sha="cafef00d"
        ).splitlines()[0]
    )


def test_persist_review_verdict_degrades_on_oserror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A write failure is logged and returns None; it never raises into the
    daemon thread's success path."""
    from cw.codex_background import _persist_review_verdict

    (tmp_path / ".claude").write_text("not a directory", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert (
            _persist_review_verdict(
                tmp_path, "x", ticket_id="T-1", reviewed_sha="deadbeef"
            )
            is None
        )
    assert any("review_verdict_persist_failed" in r.message for r in caplog.records)


def test_persist_review_verdict_writes_structured_json_via_relative_path(
    tmp_path: Path,
) -> None:
    """#2223 round 1: the structured verdict envelope is written through the
    same ``_persist_review_verdict`` mkdir/atomic-write/log contract as the
    rendered ``.md`` sibling -- no second persist implementation -- by
    passing the envelope's serialized JSON as *review_text* and this
    artifact's own ``relative_path``."""
    verdict = consolidate_verdict(
        [_make_reviewer_doc(_make_finding(severity="MUST_FIX"))],
        _make_diff(),
        reviewed_sha="deadbeef",
    )
    envelope_text = ReviewVerdictEnvelope(
        ticket_id="T-1", verdict=verdict
    ).model_dump_json(indent=2)
    path = _persist_review_verdict(
        tmp_path,
        envelope_text,
        ticket_id="T-1",
        reviewed_sha="deadbeef",
        relative_path=REVIEW_VERDICT_JSON_RELATIVE_PATH,
    )
    assert path == tmp_path / ".claude" / "review-verdict.json"
    assert path is not None
    written = path.read_text(encoding="utf-8")
    lines = written.split("\n", 1)
    assert (
        lines[0]
        == REVIEW_VERDICT_OWNER_STAMP_FORMAT.format(
            ticket_id="T-1", reviewed_sha="deadbeef"
        ).splitlines()[0]
    )
    envelope = ReviewVerdictEnvelope.model_validate_json(lines[1])
    assert envelope.ticket_id == "T-1"
    assert envelope.verdict.accepted[0].finding.evidence == "def broken():"


def test_persist_unparseable_review_artifact_writes_worktree_copy(
    tmp_path: Path,
) -> None:
    """#2280: a zero-documents park writes its own durable worktree copy.

    Sibling of :func:`_persist_review_verdict`, for the park a
    ``ReviewVerdict`` was never produced for -- carries the blocker's reason
    and details instead of a rendered verdict comment.
    """
    from cw.codex_background import (
        REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH,
        _persist_unparseable_artifact,
    )

    blocker = Blocker(
        stage="stage3_review",
        reason=CODEX_REVIEW_UNPARSEABLE,
        details="editor (codex_timeout); reviewer (invalid_json)",
    )

    path = _persist_unparseable_artifact(
        tmp_path, blocker, ticket_id="1234", reviewed_sha="deadbeef"
    )

    assert path == tmp_path / REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH
    written = path.read_text(encoding="utf-8")
    assert "ticket_id=1234" in written
    assert "reviewed_sha=deadbeef" in written
    assert CODEX_REVIEW_UNPARSEABLE in written
    assert "editor (codex_timeout); reviewer (invalid_json)" in written


def test_persist_unparseable_review_artifact_degrades_on_oserror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A write failure is logged and returns None; never raises (#2280)."""
    from cw.codex_background import _persist_unparseable_artifact

    (tmp_path / ".claude").write_text("not a directory", encoding="utf-8")
    blocker = Blocker(
        stage="stage3_review", reason=CODEX_REVIEW_UNPARSEABLE, details="x"
    )
    with caplog.at_level("WARNING"):
        assert (
            _persist_unparseable_artifact(
                tmp_path, blocker, ticket_id="1234", reviewed_sha="deadbeef"
            )
            is None
        )
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
    """A record with the full provenance set the reader requires (#2210).

    ``summary`` defaults to the one every ``_ledger_entry`` key below is minted
    from, so the record's key binds to it.
    """
    payload: dict[str, object] = {
        "outcome": "REJECTED",
        "rationale": "settled by the operator",
        "recorded_at": "2026-08-16T00:00:00Z",
        "actor": "mattwwarren",
        "reviewed_sha": "abc1234",
        "summary": "Bug here",
    }
    payload.update(overrides)
    return FindingDisposition.model_validate(payload)


def _ledger_entry(
    file: str = "src/cw/foo.py", summary: str = "Bug here", **overrides: object
) -> dict[str, FindingDisposition]:
    """A one-entry ledger keyed through the real key function."""
    key = _disposition_key(file, summary)
    assert key is not None
    overrides.setdefault("summary", summary)
    return {key: _disposition(**overrides)}


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
        fresh = _ledger_entry()
        _sync_finding_dispositions_to_running_task(
            client_name="test", ticket_id="T-1838", dispositions=fresh
        )
        stored = load_dev_queue().tasks[0]
        assert stored.finding_dispositions == fresh

    def test_merge_is_additive_and_idempotent(self) -> None:
        old = _ledger_entry("src/cw/old.py", "Old bug")
        add_ticket(self._running(finding_dispositions=old))
        fresh = _ledger_entry()
        _sync_finding_dispositions_to_running_task(
            client_name="test", ticket_id="T-1838", dispositions=fresh
        )
        _sync_finding_dispositions_to_running_task(
            client_name="test", ticket_id="T-1838", dispositions=fresh
        )
        stored = load_dev_queue().tasks[0]
        assert stored.finding_dispositions == {**old, **fresh}

    def test_an_invalid_record_never_replaces_a_valid_row_entry(self) -> None:
        """Validate first, write second (#2210 round 3), at the persistence hop.

        The dev-queue row is the DURABLE ledger. A record that fails provenance
        — here one with a LATER ``recorded_at`` that would win a newest-wins
        merge — must leave the valid entry already on the row exactly as it
        was, even if a caller forgets to filter it out first.
        """
        settled = _ledger_entry(rationale="the settled one")
        add_ticket(self._running(finding_dispositions=settled))
        hijack = _ledger_entry(
            actor="", rationale="hijack", recorded_at="2099-01-01T00:00:00Z"
        )
        _sync_finding_dispositions_to_running_task(
            client_name="test", ticket_id="T-1838", dispositions=hijack
        )
        assert load_dev_queue().tasks[0].finding_dispositions == settled

    def test_an_invalid_record_is_not_persisted_as_a_new_entry(self) -> None:
        add_ticket(self._running())
        _sync_finding_dispositions_to_running_task(
            client_name="test",
            ticket_id="T-1838",
            dispositions=_ledger_entry(reviewed_sha=""),
        )
        assert load_dev_queue().tasks[0].finding_dispositions == {}

    def test_a_valid_newer_record_replaces_the_row_entry(self) -> None:
        add_ticket(self._running(finding_dispositions=_ledger_entry(rationale="old")))
        newer = _ledger_entry(rationale="new", recorded_at="2026-09-01T00:00:00Z")
        _sync_finding_dispositions_to_running_task(
            client_name="test", ticket_id="T-1838", dispositions=newer
        )
        assert load_dev_queue().tasks[0].finding_dispositions == newer

    def test_no_matching_running_row_is_a_no_op(self) -> None:
        add_ticket(self._running(status=QueueItemStatus.PENDING))
        _sync_finding_dispositions_to_running_task(
            client_name="test",
            ticket_id="T-1838",
            dispositions=_ledger_entry(),
        )
        assert load_dev_queue().tasks[0].finding_dispositions == {}

    def test_empty_dispositions_is_a_no_op(self) -> None:
        add_ticket(self._running())
        _sync_finding_dispositions_to_running_task(
            client_name="test", ticket_id="T-1838", dispositions={}
        )
        assert load_dev_queue().tasks[0].finding_dispositions == {}
