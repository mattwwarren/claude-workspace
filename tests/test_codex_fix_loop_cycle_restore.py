"""Unit tests for ``cw.codex_fix_loop.cycle_restore`` (#2967).

Real git through ``make_git_repo``; the loop-level refusal tests live in
``test_codex_fix_loop*.py`` and assert the same clean-tree outcome end to end.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from cw.codex_fix_loop import cycle_restore
from cw.codex_fix_loop.cycle_restore import (
    CycleRestore,
    apply_to_breach,
    failure_note,
    preserve_and_reset,
)
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach
from tests._codex_review_helpers import assert_clean_after_refusal, exclude_cw_dir
from tests.conftest import git_in


def _repo(make_git_repo: Callable[..., Path], name: str) -> Path:
    repo = make_git_repo(name)
    exclude_cw_dir(repo)
    (repo / "tracked.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "gone.py").write_text("g = 1\n", encoding="utf-8")
    git_in(repo, "add", "tracked.py", "gone.py")
    git_in(repo, "commit", "-m", "base")
    return repo


def _preserve(repo: Path, *, cycle: int = 1, reason: str = "codex_fix_scope") -> object:
    return preserve_and_reset(
        repo,
        review_artifacts=frozenset(),
        ticket="T-1",
        cycle=cycle,
        reason=reason,
    )


class TestPreserveAndReset:
    def test_round_trip_pins_edits_then_clean_tree_then_reapplies(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "rt")
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

    def test_no_changes_returns_none_and_pins_nothing(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "noop")
        assert _preserve(repo) is None
        assert git_in(repo, "for-each-ref", "refs/cw/refused-cycles/") == ""

    def test_failed_drop_keeps_ref_and_reports_not_restored(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(make_git_repo, "drop-fail")
        (repo / "tracked.py").write_text("a = 3\n", encoding="utf-8")

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise subprocess.CalledProcessError(1, "restore")

        monkeypatch.setattr(cycle_restore, "_drop_paths", _boom)

        result = _preserve(repo)

        assert isinstance(result, CycleRestore)
        assert result.restored is False
        assert result.ref is not None
        assert result.why_not == "git restore exited 1"
        assert result.dirty_paths == ("tracked.py",)
        # The edit survives, pinned to the ref, and the worktree still holds it.
        assert git_in(repo, "cat-file", "-t", result.ref) == "commit"
        assert "tracked.py" in git_in(repo, "status", "--porcelain")


class TestHintRewrite:
    _BREACH = FenceBreach(
        reason="codex_fix_scope",
        paths=("x.py",),
        details="out of scope",
        recovery_hint=f"Fix the scope. {LEFT_STAGED_HINT}",
    )

    def test_restored_replaces_left_staged_hint(self) -> None:
        restore = CycleRestore(True, "refs/cw/refused-cycles/T-1/c", None, "abc123")
        out = apply_to_breach(self._BREACH, restore)
        assert LEFT_STAGED_HINT not in out.recovery_hint
        assert "refs/cw/refused-cycles/T-1/c" in out.recovery_hint
        assert "git stash apply refs/cw/refused-cycles/T-1/c" in out.recovery_hint

    def test_not_restored_keeps_old_hint_and_appends_reason(self) -> None:
        restore = CycleRestore(
            False, "refs/x", "git restore exited 1", dirty_paths=("x.py",)
        )
        out = apply_to_breach(self._BREACH, restore)
        assert LEFT_STAGED_HINT in out.recovery_hint
        assert "git restore exited 1" in out.recovery_hint
        assert "`x.py`" in out.recovery_hint

    def test_non_restorable_breach_untouched(self) -> None:
        breach = FenceBreach(
            reason="codex_fix_dirty_start",
            paths=(),
            details="dirty",
            recovery_hint="clean up first",
            restorable=False,
        )
        restore = CycleRestore(True, "refs/x", None, "abc")
        assert apply_to_breach(breach, restore) is breach

    def test_none_restore_untouched(self) -> None:
        assert apply_to_breach(self._BREACH, None) is self._BREACH


class TestFailureNote:
    def test_none_gives_no_note(self) -> None:
        assert failure_note(None) is None

    def test_restored_note_names_ref(self) -> None:
        note = failure_note(CycleRestore(True, "refs/x", None, "abc"))
        assert note is not None
        assert "git stash apply refs/x" in note

    def test_not_restored_note_names_reason(self) -> None:
        note = failure_note(CycleRestore(False, None, "git exited 128"))
        assert note is not None
        assert "git exited 128" in note
