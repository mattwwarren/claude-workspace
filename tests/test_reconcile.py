"""Unit tests for cw.reconcile — cross-cutting reconcile integration.

World-state (merged-PR) guard before revert, and the gate-recipe reconcile
integration path. Per-submodule tests live in the sibling test_reconcile_*
files; see #1307.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import freezegun
import pytest

from cw.config import (
    load_state,
    orchestrator_config_file,
    save_state,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import (
    reconcile,
)
from cw.reconcile.gate_recipes import (
    RECIPE_AUTO_ADOPT_PLAN,
    RECIPE_AUTO_APPROVE_REVIEW,
)
from tests._reconcile_helpers import (
    _auto_config,
    _mk_headless_daemon_session,
    _mk_phantom_daemon_session,
)
from tests.conftest import (
    _make_ticket_task,
    plan_body,
    stub_fetch_plan,
)
from tests.test_reconcile_gate_recipes import (
    _clean_result,
    _make_session,
    _plan_result,
    _write_acme_clients_yaml,
)


class TestWorldStateCheckBeforeRevert:
    """The phantom sweep and the reconcile() gh pre-pass check world state
    (merged PR / gh availability) before dispositioning a dead session's task.

    Since the process-kill-timeout removal the stalled/idle sweeps no longer
    disposition sessions, so the pre-pass world-state check is consumed only
    by the phantom sweep.
    """

    # --- phantom ---

    def test_phantom_merged_ticket_completes_not_crashes(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids → COMPLETED+NORMAL phantom, not COMPLETED+CRASHED."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        sess = _mk_phantom_daemon_session("phantom-merged-1", started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="phantom-merged-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="phantom-merged-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="phantom-merged-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="phantom-merged-1",
            worktree_dirty=False,
            client="client-a",
        )

        reverted, _names, _limited, _salvaged, _results, merged = (
            _act_on_phantom_candidates(
                state,
                [candidate],
                now=now,
                config=_auto_config(),
                merged_ticket_ids=frozenset({"phantom-merged-1"}),
            )
        )

        assert reverted == []
        assert "phantom-merged-1" in merged
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "phantom-merged-1")
        assert t.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-phantom-merged-1",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        completed = [e for e in events if not e.payload.get("crashed")]
        assert len(completed) == 1

    def test_phantom_gh_blocked_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh_blocked_ticket_ids → phantom skipped, BLOCKED_ON_USER."""
        from cw.reconcile import (
            _GH_CHECK_BLOCKED_REASON,
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        sess = _mk_phantom_daemon_session("phantom-ghblock-1", started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="phantom-ghblock-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="phantom-ghblock-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="phantom-ghblock-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="phantom-ghblock-1",
            worktree_dirty=False,
            client="client-a",
            lane="phantom-lane",
        )

        reverted, _names, _limited, _salvaged, _results, merged = (
            _act_on_phantom_candidates(
                state,
                [candidate],
                now=now,
                config=_auto_config(),
                gh_blocked_ticket_ids=frozenset({"phantom-ghblock-1"}),
            )
        )

        assert reverted == []
        assert merged == []

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "phantom-ghblock-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == _GH_CHECK_BLOCKED_REASON
        assert sess.status == SessionStatus.COMPLETED

        events = read_events(
            consumer="test-phantom-ghblock-1",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload["lane"] == "phantom-lane"

    # --- reconcile() pre-pass ---

    def test_reconcile_prepass_merged_pr_populates_completed_ticket_ids(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: merged PR → completed_ticket_ids, task COMPLETED."""
        worktree = tmp_path / "wt-reconcile-merged"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("reconcile-merged-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="reconcile-merged-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="reconcile-merged-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        # Roster carries only a decoy: the session is a phantom, but the
        # daemon is clearly reachable (outage guard must not fire).
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)

        with freezegun.freeze_time(now):
            report = reconcile()

        assert "reconcile-merged-1" in report.completed_ticket_ids
        assert "reconcile-merged-1" not in report.reverted_ticket_ids

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "reconcile-merged-1")
        assert t.status == QueueItemStatus.COMPLETED

    def test_reconcile_prepass_gh_unavailable_blocks_not_reverts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: gh unavailable → task BLOCKED_ON_USER, not PENDING."""
        worktree = tmp_path / "wt-reconcile-ghblock"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("reconcile-ghblock-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="reconcile-ghblock-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="reconcile-ghblock-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (None, False),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        # Roster carries only a decoy: the session is a phantom, but the
        # daemon is clearly reachable (outage guard must not fire).
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)

        with freezegun.freeze_time(now):
            report = reconcile()

        assert "reconcile-ghblock-1" not in report.reverted_ticket_ids

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "reconcile-ghblock-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_reconcile_prepass_custom_prefix(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass passes branch='feat/<ticket_id>' when prefix='feat'."""
        ticket_id = "reconcile-feat-1"
        worktree = tmp_path / "wt-reconcile-feat"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        # Write clients.yaml with feature_branch_prefix: feat
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-feat\n"
            "    default_branch: main\n"
            "    feature_branch_prefix: feat\n"
        )

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured_branch: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            captured_branch.append(branch)
            return False, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)

        with freezegun.freeze_time(now):
            reconcile()

        assert captured_branch == [f"feat/{ticket_id}"]

    def test_reconcile_prepass_passes_cwd(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass resolves cwd from client's workspace_path (#1269)."""
        ticket_id = "reconcile-cwd-1"
        worktree = tmp_path / "wt-reconcile-cwd"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-feat\n"
            "    default_branch: main\n"
            "    feature_branch_prefix: feat\n"
        )

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured_cwd: list[Path | None] = []

        def _capture(
            tid: str, *, branch: str, cwd: Path | None = None, **_kw: object
        ) -> tuple[bool, bool]:
            captured_cwd.append(cwd)
            return False, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)

        with freezegun.freeze_time(now):
            reconcile()

        assert captured_cwd == [Path("/tmp/ws-feat")]

    def test_reconcile_prepass_default_prefix_fallback(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: no clients.yaml → branch='dev/<ticket>' (default)."""
        ticket_id = "reconcile-default-1"
        worktree = tmp_path / "wt-reconcile-default"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        # No clients.yaml written — load_clients() returns {}

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured_branch: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            captured_branch.append(branch)
            return False, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)

        with freezegun.freeze_time(now):
            reconcile()

        assert captured_branch == [f"dev/{ticket_id}"]

    def test_reconcile_prepass_dangling_client_skips_gh_call(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: clients.yaml populated but missing this
        session's client → skip the gh call entirely (GitHub #1269).

        Distinct from "no clients.yaml at all" (see
        test_reconcile_prepass_default_prefix_fallback, which must still
        call gh with cwd=None): here clients.yaml is populated with a
        *different* client, so this session's client is dangling/config-
        drifted. An unscoped gh call would risk the same cross-repo
        misattribution the ticket describes.
        """
        ticket_id = "reconcile-dangling-1"
        worktree = tmp_path / "wt-reconcile-dangling"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-b:\n"
            "    workspace_path: /tmp/ws-other\n"
            "    default_branch: main\n"
        )

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        calls: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            calls.append(tid)
            return True, True  # would falsely report merged if ever called

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)

        with freezegun.freeze_time(now):
            reconcile()

        assert calls == []

    def test_dead_session_wrong_repo_merge_does_not_phantom_reap(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dead session's ticket must not be phantom-reaped as
        merged/shipped just because the gh CLI's ambient CWD (not scoped to
        the client's repo) happens to answer a same-numbered ticket in a
        different repo as merged. Real repro of GitHub #1269: fakes
        ``cw.gh._sp.run`` itself (not ``_deps.pr_is_merged_for_ticket``) so
        the exact code path under test — cwd threading through
        ``pr_is_merged_for_ticket`` — is exercised end to end via the real
        ``reconcile()`` entry point.

        Pre-fix: no cwd is ever passed to the gh subprocess, so every call
        lands in the "ambient" branch below, which reports the ticket
        MERGED — reproducing the incident (task COMPLETED/"shipped",
        SESSION_COMPLETED with reason="phantom_reap_merged").

        Post-fix: the pre-pass resolves cwd from the client's
        ``workspace_path`` via ``_git_dir``, so the gh calls land in the
        "scoped correctly" branch, which reports the ticket genuinely
        unmerged in client A's own repo — the phantom sweep then routes the
        task to BLOCKED_ON_USER under the default SIGNAL_ONLY reap policy
        instead.
        """
        ticket_id = "wrongrepo-1"
        repo_a = tmp_path / "repo-a"
        repo_a.mkdir()
        worktree = tmp_path / "wt-wrongrepo"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            f"    workspace_path: {repo_a}\n"
            "    default_branch: main\n"
        )

        def _fake_run(args: list[str], **kwargs: object) -> Any:
            cwd = kwargs.get("cwd")
            scoped_correctly = cwd == repo_a
            result = MagicMock()
            result.returncode = 0
            if "issue" in args:
                # Correctly scoped to client A's repo: ticket has no linked
                # PRs there (genuinely unmerged). Ambient/unscoped: simulate
                # the collision — a same-numbered ticket that IS merged in
                # whatever repo the ambient CWD happens to resolve to.
                refs = [] if scoped_correctly else [{"number": 999}]
                result.stdout = json.dumps({"closedByPullRequestsReferences": refs})
            elif "list" in args:
                result.stdout = json.dumps([])
            else:
                result.stdout = json.dumps(
                    {"state": "OPEN" if scoped_correctly else "MERGED"}
                )
            return result

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        # Roster carries only a decoy: the session is a phantom, but the
        # daemon is clearly reachable (outage guard must not fire).
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)

        with freezegun.freeze_time(now):
            reconcile()

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.status != QueueItemStatus.COMPLETED
        assert t.disposition != "shipped"

        events = read_events(
            consumer=f"test-{ticket_id}-phantom-reap-guard",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(e.payload.get("reason") == "phantom_reap_merged" for e in events)

    # --- no process-kill timeouts ---

    def test_quiet_live_session_far_past_old_budgets_left_untouched(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression (process-kill-timeout removal): a live-roster headless
        session with no transcript activity and hours of elapsed wall-clock is
        left completely alone by a full reconcile() tick.

        Pre-removal, this session would have been reverted/parked by the
        stalled sweep's wall-clock budget (1 h) or the idle watchdog (15 min).
        Now no sweep dispositions a session off elapsed time: the session must
        stay ACTIVE and its task RUNNING.
        """
        ticket_id = "no-kill-1"
        worktree = tmp_path / "wt-no-kill"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)  # 9 h elapsed

        sess = _mk_headless_daemon_session(
            ticket_id, worktree, started_at, surface_ref="live-ref"
        )
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (False, True),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        # The session's surface IS on the roster: it is alive, just quiet.
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "live-ref"}],
        )

        with freezegun.freeze_time(now):
            report = reconcile()

        assert report.reverted_ticket_ids == []
        assert report.phantom_session_ids == []
        reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
        assert reloaded.status == SessionStatus.ACTIVE
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.RUNNING


def _write_gate_orchestrator_yaml(*, gate_recipes_enabled: bool) -> None:
    """Write orchestrator.yaml toggling the gate-recipe master switch.

    concierge_enabled stays False so the tick exercises only the gate-recipe
    path (single-concern), and reconcile()'s real load_orchestrator_config()
    reads this file off disk rather than an in-memory _config() object.
    """
    path = orchestrator_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"gate_recipes_enabled: {str(gate_recipes_enabled).lower()}\n"
        "concierge_enabled: false\n"
    )


def _stub_gate_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub cw.gh._sp.run so the post-approve comment never hits real gh."""

    def _fake_run(
        argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("cw.gh._sp.run", _fake_run)


class TestReconcileGateRecipeIntegration:
    """RFC 0009 (#1088 item 2): behavioral reconcile()-level coverage of the
    gate-recipe act path — the real load_orchestrator_config() disk read,
    per-lane enablement, gate advance, and event emission through ONE unmocked
    tick. The sibling TestConciergeAndEscalationWiring is mocked wiring-only
    (asserts the sweeps are *called*); this drives the reactor for real.

    The owning session is refless (surface_ref=None) so compute_drift skips it
    (no phantom → the cheap no-phantoms branch of _reconcile_locked, which still
    runs run_gate_recipes via _run_terminal_backstops_and_sweeps).
    """

    @staticmethod
    def _blocked_task(stage: Stage) -> TicketTask:
        return _make_ticket_task(
            ticket_id="GEN-1",
            client="acme",
            status=QueueItemStatus.BLOCKED_ON_USER,
            stage=stage,
            session_id="sess-1",
        )

    def test_clean_review_auto_approved_advances_to_finalize(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _write_gate_orchestrator_yaml(gate_recipes_enabled=True)
        _stub_gate_comment(monkeypatch)
        save_dev_queue(DevQueueStore(tasks=[self._blocked_task(Stage.REVIEW)]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        reconcile()

        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.FINALIZE
        events = read_events(
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "GEN-1"
        assert events[0].payload["recipe"] == RECIPE_AUTO_APPROVE_REVIEW

    def test_clean_plan_auto_adopted_advances_to_impl(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _write_gate_orchestrator_yaml(gate_recipes_enabled=True)
        _stub_gate_comment(monkeypatch)
        stub_fetch_plan(monkeypatch, plan_body())
        save_dev_queue(DevQueueStore(tasks=[self._blocked_task(Stage.PLAN)]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        reconcile()

        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.IMPL
        events = read_events(
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "GEN-1"
        assert events[0].payload["recipe"] == RECIPE_AUTO_ADOPT_PLAN

    def test_master_switch_off_leaves_task_blocked(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _write_gate_orchestrator_yaml(gate_recipes_enabled=False)
        _stub_gate_comment(monkeypatch)
        save_dev_queue(DevQueueStore(tasks=[self._blocked_task(Stage.REVIEW)]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        reconcile()

        store = load_dev_queue()
        # Recipe never fired: the row stays parked at the review gate. (Do not
        # assert escalation_parked_at — the escalation sweep may stamp it in the
        # same tick; only the gate-recipe non-fire is under test here.)
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].stage == Stage.REVIEW
        events = read_events(
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert events == []
