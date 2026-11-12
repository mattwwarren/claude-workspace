"""Unit tests for ``cw.codex_fix_loop.restore_report`` (#2967, #3268).

Pure text and dataclass behavior; no git. The re-export guard pins that the
``cycle_restore`` import surface still names the same objects.
"""

from __future__ import annotations

from cw.codex_fix_loop import cycle_restore, restore_report
from cw.codex_fix_loop.cycle_restore import (
    CycleRestore,
    apply_to_breach,
    failure_note,
    preserve_and_reset,
)
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach


def test_cycle_restore_reexports_are_the_same_objects() -> None:
    assert cycle_restore.CycleRestore is restore_report.CycleRestore
    assert cycle_restore.apply_to_breach is restore_report.apply_to_breach
    assert cycle_restore.failure_note is restore_report.failure_note
    assert callable(preserve_and_reset)


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
            False, "refs/x", "git reset exited 1", dirty_paths=("x.py",)
        )
        out = apply_to_breach(self._BREACH, restore)
        assert LEFT_STAGED_HINT in out.recovery_hint
        assert "git reset exited 1" in out.recovery_hint
        assert "`x.py`" in out.recovery_hint
        assert "git stash apply refs/x" in out.recovery_hint

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

    def test_not_restored_with_ref_names_ref(self) -> None:
        note = failure_note(CycleRestore(False, "refs/y", "git exited 1"))
        assert note is not None
        assert "refs/y" in note
        assert "git stash apply refs/y" in note

    def test_failed_step_is_named_when_no_ref(self) -> None:
        note = failure_note(
            CycleRestore(
                False,
                None,
                "git exited 1",
                dirty_paths=("a.py",),
                failed_step="git update-ref",
            )
        )
        assert note is not None
        assert "cw neither saved nor discarded" in note
        assert "Failed step: git update-ref." in note
        assert "Dirty paths: `a.py`." in note

    def test_no_failed_step_names_no_step(self) -> None:
        note = failure_note(CycleRestore(False, None, "nothing to save"))
        assert note is not None
        assert "Failed step:" not in note
        assert "Dirty paths:" not in note

    def test_dirty_paths_beyond_cap_show_more_suffix(self) -> None:
        paths = tuple(f"f{i:02d}.py" for i in range(restore_report.MAX_NAMED_PATHS + 3))
        note = failure_note(CycleRestore(False, None, "why", dirty_paths=paths))
        assert note is not None
        assert "(+3 more)" in note
        assert f"`{paths[restore_report.MAX_NAMED_PATHS - 1]}`" in note
        assert f"`{paths[restore_report.MAX_NAMED_PATHS]}`" not in note
