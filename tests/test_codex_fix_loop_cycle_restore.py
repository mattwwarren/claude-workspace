"""Unit tests for ``cw.codex_fix_loop.cycle_restore`` (#2967).

Real git through ``make_git_repo``; the loop-level refusal tests live in
``test_codex_fix_loop*.py`` and assert the same clean-tree outcome end to end.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cw.codex_fix_loop import cycle_restore
from cw.codex_fix_loop.cycle_restore import REF_ROOT, CycleRestore, preserve_and_reset
from cw.models.enums import OrchestratorEventType
from tests._codex_review_helpers import assert_clean_after_refusal, exclude_cw_dir
from tests._shared.git_repos import git_in

_DISK_GONE = "disk gone"
_INBOX_LOCKED = "inbox locked"


def _repo(make_git_repo: Callable[..., Path], name: str) -> Path:
    repo = make_git_repo(name)
    exclude_cw_dir(repo)
    (repo / "tracked.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "gone.py").write_text("g = 1\n", encoding="utf-8")
    git_in(repo, "add", "tracked.py", "gone.py")
    git_in(repo, "commit", "-m", "base")
    return repo


def _preserve(repo: Path, *, cycle: int = 1, reason: str = "codex_fix_scope") -> Any:
    return preserve_and_reset(
        repo,
        review_artifacts=frozenset(),
        ticket="T-1",
        client="C-1",
        cycle=cycle,
        reason=reason,
    )


def _fail_git(
    monkeypatch: pytest.MonkeyPatch,
    subcommand: str,
    error: type[Exception] = subprocess.CalledProcessError,
) -> None:
    """Make ``git <subcommand>`` fail inside the restore module only.

    ``CalledProcessError`` (the default) exits 1 with stderr ``boom``; ``OSError``
    raises a bare ``OSError`` instead.
    """
    real = cycle_restore.git_output

    def _fake(argv: list[str], **kwargs: Any) -> str:
        if argv[0] == subcommand:
            if error is subprocess.CalledProcessError:
                raise subprocess.CalledProcessError(1, argv, stderr="boom")
            raise error(argv)
        return real(argv, **kwargs)

    monkeypatch.setattr(cycle_restore, "git_output", _fake)


def _capture_events(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[Any, dict[str, Any]]]:
    calls: list[tuple[Any, dict[str, Any]]] = []

    def _record(
        event_type: Any, payload: dict[str, Any] | None = None, **_: Any
    ) -> None:
        calls.append((event_type, payload or {}))

    monkeypatch.setattr(cycle_restore, "record_event", _record)
    return calls


class TestPreserveAndReset:
    def test_round_trip_pins_edits_then_clean_tree_then_reapplies(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "rt")
        events = _capture_events(monkeypatch)
        start = git_in(repo, "rev-parse", "HEAD")
        (repo / "tracked.py").write_text("a = 2\n", encoding="utf-8")
        (repo / "new.py").write_text("n = 1\n", encoding="utf-8")
        (repo / "gone.py").unlink()

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is True
        assert result.ref is not None
        ref = assert_clean_after_refusal(repo, start)
        assert ref == result.ref
        # Re-applying the pinned edit reproduces the cycle's work.
        git_in(repo, "stash", "apply", ref)
        assert (repo / "tracked.py").read_text(encoding="utf-8") == "a = 2\n"
        assert (repo / "new.py").read_text(encoding="utf-8") == "n = 1\n"
        assert not (repo / "gone.py").exists()
        assert len(events) == 1
        event_type, payload = events[0]
        assert event_type is OrchestratorEventType.CODEX_FIX_CYCLE_PRESERVED
        assert payload["ref"] == ref
        assert payload["reset_requested"] is True
        assert payload["head"]
        assert "restored" not in payload
        assert payload["client"] == "C-1"
        assert payload["ticket_id"] == "T-1"
        assert payload["paths"] == ["gone.py", "new.py", "tracked.py"]

    def test_no_changes_returns_none_and_pins_nothing(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "noop")
        events = _capture_events(monkeypatch)
        assert _preserve(repo) is None
        assert git_in(repo, "for-each-ref", f"{REF_ROOT}/") == ""
        assert events == []

    def test_failed_reset_keeps_ref_and_reports_not_restored(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "reset-fail")
        events = _capture_events(monkeypatch)
        (repo / "tracked.py").write_text("a = 3\n", encoding="utf-8")
        _fail_git(monkeypatch, "reset")

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is not None
        assert result.why_not == "git exited 1"
        assert result.dirty_paths == ("tracked.py",)
        # The edit survives, pinned to the ref, and the worktree still holds it.
        assert git_in(repo, "cat-file", "-t", result.ref) == "commit"
        assert "tracked.py" in git_in(repo, "status", "--porcelain")
        assert events[0][1]["ref"] == result.ref
        # The audit is published before reset, so it records reset intent even
        # when the subsequent reset fails.
        assert events[0][1]["reset_requested"] is True
        assert "restored" not in events[0][1]

    def test_clean_recheck_failure_keeps_ref(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "not-clean")
        (repo / "tracked.py").write_text("a = 4\n", encoding="utf-8")
        monkeypatch.setattr(
            cycle_restore, "capture_cycle_baseline", lambda _w: object()
        )

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is not None
        assert result.why_not == cycle_restore._CLEAN_WHY
        assert result.dirty_paths == ("tracked.py",)

    def test_os_error_after_pin_keeps_ref(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "oserror")
        (repo / "tracked.py").write_text("a = 5\n", encoding="utf-8")

        def _boom(_w: Path) -> None:
            raise OSError(_DISK_GONE)

        monkeypatch.setattr(cycle_restore, "capture_cycle_baseline", _boom)

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is not None
        assert result.why_not == "OSError"
        assert result.dirty_paths == ("tracked.py",)
        assert git_in(repo, "cat-file", "-t", result.ref) == "commit"

    def test_update_ref_failure_resets_nothing(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "no-pin")
        events = _capture_events(monkeypatch)
        (repo / "tracked.py").write_text("a = 6\n", encoding="utf-8")
        _fail_git(monkeypatch, "update-ref")

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is None
        assert result.dirty_paths == ("tracked.py",)
        assert "tracked.py" in git_in(repo, "status", "--porcelain")
        assert events == []
        assert result.failed_step == cycle_restore._STEP_UPDATE_REF

    def test_git_add_failure_before_pin_resets_nothing(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "add-fail")
        (repo / "tracked.py").write_text("a = 9\n", encoding="utf-8")
        _fail_git(monkeypatch, "add")

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is None
        assert result.why_not == "git exited 1"
        assert result.failed_step == cycle_restore._STEP_COLLECT
        assert "tracked.py" in git_in(repo, "status", "--porcelain")

    def test_lock_acquisition_called_process_error_resets_nothing(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "lock-cpe")
        (repo / "tracked.py").write_text("a = 11\n", encoding="utf-8")
        # _worktree_lock runs rev-parse --git-path first: fails before any step.
        _fail_git(monkeypatch, "rev-parse")

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is None
        assert result.why_not == "git exited 1"
        assert result.failed_step == cycle_restore._STEP_LOCK
        assert "tracked.py" in git_in(repo, "status", "--porcelain")

    def test_lock_acquisition_os_error_resets_nothing(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "lock-oserror")
        (repo / "tracked.py").write_text("a = 12\n", encoding="utf-8")
        _fail_git(monkeypatch, "rev-parse", OSError)

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is None
        assert result.why_not == "OSError"
        assert result.failed_step == cycle_restore._STEP_LOCK
        assert "tracked.py" in git_in(repo, "status", "--porcelain")

    def test_stash_create_failure_pins_and_resets_nothing(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "stash-fail")
        (repo / "tracked.py").write_text("a = 13\n", encoding="utf-8")
        _fail_git(monkeypatch, "stash")

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is None
        assert result.failed_step == cycle_restore._STEP_STASH
        assert "tracked.py" in git_in(repo, "status", "--porcelain")

    def test_os_error_before_pin_resets_nothing(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "add-oserror")
        (repo / "tracked.py").write_text("a = 10\n", encoding="utf-8")
        real = cycle_restore.git_output

        def _fake(argv: list[str], *, cwd: Path) -> str:
            if argv[0] == "add":
                raise OSError(_DISK_GONE)
            return real(argv, cwd=cwd)

        monkeypatch.setattr(cycle_restore, "git_output", _fake)

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is None
        assert result.why_not == "OSError"
        assert result.failed_step == cycle_restore._STEP_COLLECT
        assert "tracked.py" in git_in(repo, "status", "--porcelain")

    def test_empty_stash_create_resets_nothing(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "no-stash")
        (repo / "tracked.py").write_text("a = 7\n", encoding="utf-8")
        real = cycle_restore.git_output

        def _fake(argv: list[str], **kwargs: Any) -> str:
            if argv[0] == "stash":
                return ""
            return real(argv, **kwargs)

        monkeypatch.setattr(cycle_restore, "git_output", _fake)

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is None
        assert result.why_not == cycle_restore._NO_STASH_WHY
        assert result.failed_step is None
        assert "tracked.py" in git_in(repo, "status", "--porcelain")

    def test_event_recording_oserror_keeps_ref_and_clean_tree(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "event-fail")
        (repo / "tracked.py").write_text("a = 8\n", encoding="utf-8")

        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError(_INBOX_LOCKED)

        monkeypatch.setattr(cycle_restore, "record_event", _boom)

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is not None
        assert result.dirty_paths == ("tracked.py",)
        assert result.why_not == cycle_restore._AUDIT_WHY
        assert result.failed_step == cycle_restore._STEP_AUDIT
        assert "tracked.py" in git_in(repo, "status", "--porcelain")
        assert git_in(repo, "cat-file", "-t", result.ref) == "commit"
