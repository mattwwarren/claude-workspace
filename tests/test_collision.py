"""Tests for cw.collision — wave file-collision detection."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.collision import _git_changed_files, detect_wave_collisions
from cw.events import read_events
from cw.models import OrchestratorEventType, QueueItemStatus, TicketTask

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# _git_changed_files
# ---------------------------------------------------------------------------


class TestGitChangedFiles:
    def test_missing_worktree_returns_empty(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent"
        result = _git_changed_files(missing, base_ref="HEAD~1")
        assert result == frozenset()

    def test_nonzero_returncode_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="error"
            )

        monkeypatch.setattr("cw.collision.subprocess.run", _fail)
        result = _git_changed_files(tmp_path, base_ref="abc123")
        assert result == frozenset()

    def test_oserror_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        msg = "no such binary"

        def _raise(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise OSError(msg)

        monkeypatch.setattr("cw.collision.subprocess.run", _raise)
        result = _git_changed_files(tmp_path, base_ref="abc123")
        assert result == frozenset()

    def test_blank_lines_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _ok(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="src/a.py\n\nsrc/b.py\n", stderr=""
            )

        monkeypatch.setattr("cw.collision.subprocess.run", _ok)
        result = _git_changed_files(tmp_path, base_ref="abc123")
        assert result == frozenset({"src/a.py", "src/b.py"})

    def test_real_git_commits_returns_file_list(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("collision/test-repo")
        (repo / "alpha.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "alpha.py"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "add alpha"],
            check=True,
            capture_output=True,
        )
        base_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        result = _git_changed_files(repo, base_ref=base_ref)
        assert "alpha.py" in result

    def test_no_commits_since_base_returns_empty(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("collision/test-repo2")
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        result = _git_changed_files(repo, base_ref=head)
        assert result == frozenset()


# ---------------------------------------------------------------------------
# detect_wave_collisions
# ---------------------------------------------------------------------------


class TestDetectWaveCollisions:
    def _make_running(
        self,
        ticket_id: str,
        client: str = "test-client",
        worktree: Path | None = None,
        stage_base_ref: str | None = "abc123",
    ) -> TicketTask:
        return TicketTask(
            ticket_id=ticket_id,
            client=client,
            status=QueueItemStatus.RUNNING,
            worktree_path=worktree,
            stage_base_ref=stage_base_ref,
        )

    def test_no_tasks_no_events(self, tmp_path: Path) -> None:
        warned: set[frozenset[str]] = set()
        detect_wave_collisions([], warned_collision=warned)
        events = read_events(
            consumer="test-no-tasks",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        assert events == []

    def test_single_task_no_events(self, tmp_path: Path) -> None:
        task = self._make_running("T-1", worktree=tmp_path / "wt1")
        warned: set[frozenset[str]] = set()
        detect_wave_collisions([task], warned_collision=warned)
        events = read_events(
            consumer="test-single-task",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        assert events == []

    def test_stage_base_ref_none_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[str] = []

        def _mock_files(path: Path, base_ref: str) -> frozenset[str]:
            called.append(str(path))
            return frozenset({"src/common.py"})

        monkeypatch.setattr("cw.collision._git_changed_files", _mock_files)

        task_no_ref = self._make_running(
            "T-2", worktree=tmp_path / "wt1", stage_base_ref=None
        )
        task_with_ref = self._make_running(
            "T-3", worktree=tmp_path / "wt2", stage_base_ref="abc"
        )

        warned: set[frozenset[str]] = set()
        detect_wave_collisions([task_no_ref, task_with_ref], warned_collision=warned)

        # Only the task with stage_base_ref should have _git_changed_files called
        assert len(called) == 1

    def test_different_client_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _mock_files(path: Path, base_ref: str) -> frozenset[str]:
            return frozenset({"src/shared.py"})

        monkeypatch.setattr("cw.collision._git_changed_files", _mock_files)

        task_a = self._make_running("T-4", client="client-a", worktree=tmp_path / "wt1")
        task_b = self._make_running("T-5", client="client-b", worktree=tmp_path / "wt2")

        warned: set[frozenset[str]] = set()
        lines: list[str] = []
        detect_wave_collisions(
            [task_a, task_b], warned_collision=warned, emit=lines.append
        )

        events = read_events(
            consumer="test-diff-client",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        assert events == []
        assert lines == []

    def test_missing_worktree_no_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        def _mock_files(path: Path, base_ref: str) -> frozenset[str]:
            nonlocal called
            called = True
            return frozenset()

        monkeypatch.setattr("cw.collision._git_changed_files", _mock_files)

        task = self._make_running("T-6", worktree=tmp_path / "missing-wt")
        warned: set[frozenset[str]] = set()
        detect_wave_collisions([task], warned_collision=warned)
        # No crash; no collision emitted

    def test_no_intersection_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _mock_files(path: Path, base_ref: str) -> frozenset[str]:
            if "wt1" in str(path):
                return frozenset({"src/a.py"})
            return frozenset({"src/b.py"})

        monkeypatch.setattr("cw.collision._git_changed_files", _mock_files)

        task_a = self._make_running("T-7", worktree=tmp_path / "wt1")
        task_b = self._make_running("T-8", worktree=tmp_path / "wt2")

        warned: set[frozenset[str]] = set()
        detect_wave_collisions([task_a, task_b], warned_collision=warned)

        events = read_events(
            consumer="test-no-intersection",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        assert events == []

    def test_overlapping_files_emits_wave_collision_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _mock_files(path: Path, base_ref: str) -> frozenset[str]:
            return frozenset({"src/shared.py", "src/models.py"})

        monkeypatch.setattr("cw.collision._git_changed_files", _mock_files)

        task_a = self._make_running("T-9", worktree=tmp_path / "wt1")
        task_b = self._make_running("T-10", worktree=tmp_path / "wt2")

        warned: set[frozenset[str]] = set()
        lines: list[str] = []
        detect_wave_collisions(
            [task_a, task_b], warned_collision=warned, emit=lines.append
        )

        events = read_events(
            consumer="test-overlap",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["ticket_ids"] == ["T-10", "T-9"]  # sorted
        assert "src/models.py" in payload["files"]
        assert "src/shared.py" in payload["files"]
        assert payload["files"] == sorted(payload["files"])
        assert len(lines) == 1
        assert "COLLISION" in lines[0]
        assert "T-9" in lines[0]
        assert "T-10" in lines[0]

    def test_dedup_same_pair_not_re_emitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _mock_files(path: Path, base_ref: str) -> frozenset[str]:
            return frozenset({"src/shared.py"})

        monkeypatch.setattr("cw.collision._git_changed_files", _mock_files)

        task_a = self._make_running("T-11", worktree=tmp_path / "wt1")
        task_b = self._make_running("T-12", worktree=tmp_path / "wt2")

        warned: set[frozenset[str]] = set()
        detect_wave_collisions([task_a, task_b], warned_collision=warned)
        detect_wave_collisions([task_a, task_b], warned_collision=warned)

        events = read_events(
            consumer="test-dedup",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        assert len(events) == 1

    def test_warned_collision_none_event_still_fires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _mock_files(path: Path, base_ref: str) -> frozenset[str]:
            return frozenset({"src/x.py"})

        monkeypatch.setattr("cw.collision._git_changed_files", _mock_files)

        task_a = self._make_running("T-13", worktree=tmp_path / "wt1")
        task_b = self._make_running("T-14", worktree=tmp_path / "wt2")

        detect_wave_collisions([task_a, task_b], warned_collision=None)

        events = read_events(
            consumer="test-warned-none",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        assert len(events) == 1

    def test_emit_none_quiet_mode_event_still_fires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _mock_files(path: Path, base_ref: str) -> frozenset[str]:
            return frozenset({"src/y.py"})

        monkeypatch.setattr("cw.collision._git_changed_files", _mock_files)

        task_a = self._make_running("T-15", worktree=tmp_path / "wt1")
        task_b = self._make_running("T-16", worktree=tmp_path / "wt2")

        warned: set[frozenset[str]] = set()
        detect_wave_collisions([task_a, task_b], warned_collision=warned, emit=None)

        events = read_events(
            consumer="test-emit-none",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        assert len(events) == 1

    def test_sorted_files_in_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _mock_files(path: Path, base_ref: str) -> frozenset[str]:
            return frozenset({"zzz.py", "aaa.py", "mmm.py"})

        monkeypatch.setattr("cw.collision._git_changed_files", _mock_files)

        task_a = self._make_running("T-17", worktree=tmp_path / "wt1")
        task_b = self._make_running("T-18", worktree=tmp_path / "wt2")

        warned: set[frozenset[str]] = set()
        detect_wave_collisions([task_a, task_b], warned_collision=warned)

        events = read_events(
            consumer="test-sorted-files",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        assert len(events) == 1
        assert events[0].payload["files"] == ["aaa.py", "mmm.py", "zzz.py"]

    def test_multiple_pairs_each_emits_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _mock_files(path: Path, base_ref: str) -> frozenset[str]:
            # All three tasks share the same file
            return frozenset({"src/shared.py"})

        monkeypatch.setattr("cw.collision._git_changed_files", _mock_files)

        task_a = self._make_running("T-19", worktree=tmp_path / "wt1")
        task_b = self._make_running("T-20", worktree=tmp_path / "wt2")
        task_c = self._make_running("T-21", worktree=tmp_path / "wt3")

        warned: set[frozenset[str]] = set()
        detect_wave_collisions([task_a, task_b, task_c], warned_collision=warned)

        events = read_events(
            consumer="test-multi-pairs",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        # 3 pairs: (T-19,T-20), (T-19,T-21), (T-20,T-21)
        assert len(events) == 3
