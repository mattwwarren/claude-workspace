"""Tests for cw.reconcile.cancelled_row_probes (#2546).

Concierge recipe 3's ``git log`` (``_has_commits_beyond_base``) is captured in
a lockless pre-pass; the in-lock detect only looks the result up, and a
missing, stale or budget-exhausted capture means "not restored this tick".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.config import save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import CwState, DevQueueStore, OrchestratorConfig, QueueItemStatus
from cw.reconcile import cancelled_row_probes, reconcile
from cw.reconcile.cancelled_row_probes import (
    CANCELLED_ROW_PROBE_BUDGET_SECONDS,
    CANCELLED_ROW_PROBE_MAX_AGE_SECONDS,
    CancelledRowKey,
    cancelled_row_has_commits,
    cancelled_row_targets,
    capture_cancelled_row_probes,
    new_cancelled_row_probes,
)
from cw.reconcile.probe_store import ProbeStoreUnavailableError
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._reconcile_helpers import probe_sessions_lock_free
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import TicketTask

_NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
_HAS_COMMITS = "cw.reconcile.cancelled_row_probes._has_commits_beyond_base"


def _cancelled(
    worktree: Path | None, *, ticket_id: str = "GEN-1", client: str = "acme"
) -> TicketTask:
    return _make_ticket_task(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.CANCELLED,
        worktree_path=worktree,
    )


def _key(worktree: Path, *, ticket_id: str = "GEN-1") -> CancelledRowKey:
    return CancelledRowKey("acme", ticket_id, worktree, "main")


def _record_git(
    monkeypatch: pytest.MonkeyPatch, answer: bool
) -> list[tuple[Path, str]]:
    calls: list[tuple[Path, str]] = []

    def _fake(wt_path: Path, default_branch: str) -> bool:
        calls.append((wt_path, default_branch))
        return answer

    monkeypatch.setattr(_HAS_COMMITS, _fake)
    return calls


class TestTargets:
    @pytest.mark.parametrize(
        ("status", "worktree_kind", "client"),
        [
            (QueueItemStatus.PENDING, "exists", "acme"),
            (QueueItemStatus.CANCELLED, "none", "acme"),
            (QueueItemStatus.CANCELLED, "missing", "acme"),
            (QueueItemStatus.CANCELLED, "exists", "unknown-client"),
        ],
    )
    def test_ineligible_rows_yield_no_key(
        self,
        tmp_path: Path,
        status: QueueItemStatus,
        worktree_kind: str,
        client: str,
    ) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path, default_branch="main"))
        worktree = {
            "exists": tmp_path,
            "none": None,
            "missing": tmp_path / "gone",
        }[worktree_kind]
        task = _make_ticket_task(
            ticket_id="GEN-1", client=client, status=status, worktree_path=worktree
        )

        assert cancelled_row_targets([task]) == []

    def test_good_row_yields_its_key(self, tmp_path: Path) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path, default_branch="dev"))

        keys = cancelled_row_targets([_cancelled(tmp_path)])

        assert keys == [CancelledRowKey("acme", "GEN-1", tmp_path, "dev")]
        assert keys[0].worktree_path == tmp_path
        assert keys[0].default_branch == "dev"


class TestCapture:
    @pytest.mark.parametrize("answer", [True, False])
    def test_stores_the_git_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: bool
    ) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path, default_branch="main"))
        calls = _record_git(monkeypatch, answer)
        probes = new_cancelled_row_probes()

        capture_cancelled_row_probes([_cancelled(tmp_path)], probes=probes)

        assert calls == [(tmp_path, "main")]
        assert probes.lookup(_key(tmp_path)) is answer

    def test_reader_error_propagates_and_stores_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path, default_branch="main"))

        def _boom(_wt: Path, _branch: str) -> bool:
            msg = "unexpected"
            raise RuntimeError(msg)

        monkeypatch.setattr(_HAS_COMMITS, _boom)
        probes = new_cancelled_row_probes()

        with pytest.raises(RuntimeError, match="unexpected"):
            capture_cancelled_row_probes([_cancelled(tmp_path)], probes=probes)

        assert probes.keys == frozenset()
        assert cancelled_row_has_commits(probes, _key(tmp_path)) is False

    def test_budget_exhaustion_stops_without_git_and_warns_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path, default_branch="main"))
        calls = _record_git(monkeypatch, True)
        probes = new_cancelled_row_probes(budget_seconds=0.0)
        tasks = [_cancelled(tmp_path, ticket_id=f"GEN-{n}") for n in (1, 2)]

        with caplog.at_level(logging.WARNING, "cw.reconcile.cancelled_row_probes"):
            capture_cancelled_row_probes(tasks, probes=probes)

        assert calls == []
        assert probes.keys == frozenset()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "0 captured" in warnings[0].getMessage()
        assert "2 of 2" in warnings[0].getMessage()

    def test_budget_spent_after_first_read_keeps_it_and_defers_the_rest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path, default_branch="main"))
        now = [0.0]
        monkeypatch.setattr("cw.reconcile.probe_store.monotonic", lambda: now[0])
        calls: list[tuple[Path, str]] = []

        def _slow_git(wt_path: Path, default_branch: str) -> bool:
            calls.append((wt_path, default_branch))
            now[0] += CANCELLED_ROW_PROBE_BUDGET_SECONDS + 1.0
            return True

        monkeypatch.setattr(_HAS_COMMITS, _slow_git)
        probes = new_cancelled_row_probes()
        tasks = [_cancelled(tmp_path, ticket_id=f"GEN-{n}") for n in (1, 2)]

        with caplog.at_level(logging.WARNING, "cw.reconcile.cancelled_row_probes"):
            capture_cancelled_row_probes(tasks, probes=probes)

        assert calls == [(tmp_path, "main")]
        assert probes.lookup(_key(tmp_path, ticket_id="GEN-1")) is True
        with pytest.raises(ProbeStoreUnavailableError):
            probes.lookup(_key(tmp_path, ticket_id="GEN-2"))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "1 captured" in warnings[0].getMessage()
        assert "1 of 2" in warnings[0].getMessage()

    def test_empty_tasks_runs_no_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _record_git(monkeypatch, True)
        probes = new_cancelled_row_probes()

        capture_cancelled_row_probes([], probes=probes)

        assert calls == []
        assert probes.keys == frozenset()


class TestLookup:
    def test_miss_is_false(self, tmp_path: Path) -> None:
        assert (
            cancelled_row_has_commits(new_cancelled_row_probes(), _key(tmp_path))
            is False
        )

    def test_none_store_is_false(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, "cw.reconcile.cancelled_row_probes"):
            assert cancelled_row_has_commits(None, _key(tmp_path)) is False
        assert "cancelled_row_probe_unavailable" in caplog.text
        assert "reason=missing" in caplog.text

    @pytest.mark.parametrize(
        "move",
        [
            lambda: _NOW + timedelta(seconds=CANCELLED_ROW_PROBE_MAX_AGE_SECONDS),
            lambda: _NOW - timedelta(seconds=1),
        ],
        ids=["stale", "negative-age"],
    )
    def test_stale_or_negative_age_is_false(
        self, tmp_path: Path, move: Callable[[], datetime]
    ) -> None:
        probes = new_cancelled_row_probes(budget_seconds=None)
        key = _key(tmp_path)
        with freeze_time(_NOW) as clock:
            probes.capture(key, read=lambda _at: True)
            assert cancelled_row_has_commits(probes, key) is True
            clock.move_to(move())
            assert cancelled_row_has_commits(probes, key) is False

    @pytest.mark.parametrize(
        "other",
        [
            CancelledRowKey("acme", "GEN-1", Path("/elsewhere"), "main"),
            CancelledRowKey("acme", "GEN-1", Path("/wt"), "dev"),
        ],
        ids=["worktree-path", "default-branch"],
    )
    def test_different_key_fields_miss(self, other: CancelledRowKey) -> None:
        probes = new_cancelled_row_probes()
        probes.capture(
            CancelledRowKey("acme", "GEN-1", Path("/wt"), "main"), read=lambda _at: True
        )

        assert cancelled_row_has_commits(probes, other) is False
        with pytest.raises(ProbeStoreUnavailableError):
            probes.lookup(other)


def test_reconcile_restores_with_git_off_the_lock(
    tmp_config_dir: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real git, full reconcile(): recipe 3's git runs before sessions_lock.

    The autouse lock-invariant harness fails the test on any real in-lock
    subprocess under a ``cw.reconcile.concierge`` frame (not allowlisted), so
    this is also the "no subprocess under sessions_lock" pin for recipe 3.
    """
    repo = make_git_repo("acme")
    git_in(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit_tracked_file(repo, "file.txt")
    write_clients_yaml(ClientSpec("acme", repo, default_branch="main"))
    save_dev_queue(DevQueueStore(tasks=[_cancelled(repo)]))
    save_state(CwState(sessions=[]))
    monkeypatch.setattr(
        "cw.reconcile.core.load_orchestrator_config",
        lambda: OrchestratorConfig(concierge_enabled=True),
    )
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

    real = cancelled_row_probes._has_commits_beyond_base
    lock_free: list[bool] = []

    def _spy(wt_path: Path, default_branch: str) -> bool:
        lock_free.append(probe_sessions_lock_free())
        return real(wt_path, default_branch)

    monkeypatch.setattr(_HAS_COMMITS, _spy)

    reconcile()

    assert lock_free == [True]
    assert load_dev_queue().tasks[0].status == QueueItemStatus.PENDING
