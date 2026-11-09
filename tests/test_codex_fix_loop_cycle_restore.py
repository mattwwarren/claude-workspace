"""Tests for ``cw.codex_fix_loop.cycle_restore`` (#2967).

A refused fix cycle's edits are saved as a patch under ``.cw/codex_fix_refused/``
and the worktree is restored to the cycle-start tree, so a plain requeue starts
clean. ``TestSnapshotAndRestore`` drives ``snapshot_and_restore`` directly on
real git repos; ``TestApplyToBreach`` / ``TestFailureNote`` pin the pure text
rewrites; ``TestCleanTreeAfterRefusal`` runs every refusal reason through the
real loop and asserts a clean tree afterwards.
"""

from __future__ import annotations

import ast
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.codex_fix_loop import commit, cycle_restore
from cw.codex_fix_loop.baseline import CycleBaseline, capture_cycle_baseline
from cw.codex_fix_loop.cycle_restore import (
    CycleRestore,
    _Snapshot,
    apply_to_breach,
    failure_note,
    snapshot_and_restore,
)
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach
from cw.codex_review import (
    CODEX_FIX_CONSTRAINT_VIOLATION,
    CODEX_FIX_GROWTH_BUDGET,
    CODEX_FIX_HOOK_FAILED,
    CODEX_FIX_REVERTED_BRANCH,
    CODEX_FIX_SCOPE_DRIFT,
    CODEX_FIX_SCOPE_VIOLATION,
    CODEX_TIMEOUT,
)
from cw.codex_runner import CodexRunResult
from tests._codex_review_helpers import (
    _install_pre_commit_hook,
    _write,
    assert_clean_after_refusal,
    exclude_cw_dir,
    refused_patches,
)
from tests.conftest import commit_tracked_file, git_in
from tests.test_codex_fix_loop import (
    _CLEAN_DOC,
    _CONTENT,
    _FOO_BAR_CONTENT,
    _LINEAR_CONFIG,
    _LOGIN,
    _MF_DOC,
    _ROW_FOO_BAR,
    _cumulative_editors,
    _editor,
    _FixLoopRunner,
    _no_comment_fetch,
    _remover,
    _row_task,
    _run_loop,
    _with_plan,
    _worktree,
)
from tests.test_codex_fix_loop_commit import _SENSITIVE, _blocker_reason, _heads
from tests.test_codex_fix_loop_hook_repair import (
    _ALWAYS_FAILING_HOOK,
    _CLEAN_REPAIR,
    _LINT_HOOK,
    _MARKER_FIX,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cw.auto_dev_result import AutoDevResult
    from tests.test_codex_fix_loop import _FixBehavior

_PATCH_HINT = "git apply --index .cw/codex_fix_refused/"
_REFUSED_DIR = ".cw/codex_fix_refused"
_LOGGER = "cw.codex_fix_loop.cycle_restore"
_BINARY_V1 = bytes(range(256))
_BINARY_V2 = bytes(reversed(range(256))) + b"\x00\x00"
_PASSING_HOOK = "#!/bin/sh\nexit 0\n"
# Appends on every run and exits 1: the ticket's reformatting-hook repro.
_REFORMATTING_HOOK = "#!/bin/sh\nprintf 'reformatted = 1\\n' >> fix.py\nexit 1\n"
_ARTIFACT = ".claude/review-verdict.json"


def _seed_repo(
    make_git_repo: Callable[..., Path], name: str, *, exclude: bool = True
) -> tuple[Path, CycleBaseline]:
    """A repo with tracked text, binary, executable and to-be-deleted files."""
    repo = make_git_repo(name)
    if exclude:
        exclude_cw_dir(repo)
    _write(repo / "a.txt", "alpha\n")
    _write(repo / "gone.txt", "to be deleted\n")
    _write(repo / "script.sh", "#!/bin/sh\n")
    (repo / "bin.dat").write_bytes(_BINARY_V1)
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", "seed")
    baseline = capture_cycle_baseline(repo)
    assert isinstance(baseline, CycleBaseline)
    return repo, baseline


def _restore(
    repo: Path, baseline: CycleBaseline, *, cycle: int = 1, reason: str = "r_x"
) -> CycleRestore | None:
    return snapshot_and_restore(repo, baseline, cycle=cycle, reason=reason)


def _only_patch(repo: Path) -> Path:
    [patch] = refused_patches(repo)
    return patch


_GIT_BIN = "git"


def _boom(*_args: object, **_kwargs: object) -> None:
    raise subprocess.CalledProcessError(128, [_GIT_BIN, "boom"])


def _oserror(*_args: object, **_kwargs: object) -> None:
    msg = "disk on fire"
    raise OSError(msg)


class TestSnapshotAndRestore:
    def test_modified_new_deleted_modechange_binary_roundtrip(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-roundtrip")
        _write(repo / "a.txt", "alpha changed\n")
        _write(repo / "new.txt", "brand new\n")
        _write(repo / "nested/deep/x.txt", "deep\n")
        (repo / "gone.txt").unlink()
        (repo / "script.sh").chmod(0o755)
        (repo / "bin.dat").write_bytes(_BINARY_V2)
        expected = {
            "a.txt": b"alpha changed\n",
            "new.txt": b"brand new\n",
            "nested/deep/x.txt": b"deep\n",
            "bin.dat": _BINARY_V2,
        }

        result = _restore(repo, baseline, reason="codex_fix_growth_budget_exceeded")

        assert result is not None
        assert result.restored is True
        assert result.why_not is None
        assert git_in(repo, "status", "--porcelain") == ""
        assert git_in(repo, "rev-parse", "HEAD") == baseline.head_sha
        patch = _only_patch(repo)
        assert result.patch == str(patch.relative_to(repo))
        assert patch.name.startswith("cycle-1-codex_fix_growth_budget_exceeded-")

        git_in(repo, "apply", "--index", str(patch))

        for rel, data in expected.items():
            assert (repo / rel).read_bytes() == data
        assert not (repo / "gone.txt").exists()
        assert git_in(repo, "ls-files", "-s", "script.sh").startswith("100755")
        assert set(git_in(repo, "diff", "--cached", "--name-only").splitlines()) == {
            *expected,
            "gone.txt",
            "script.sh",
        }

    def test_mm_state_snapshots_final_hook_formatted_content(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """The ticket's ``MM`` state: staged v1, then a hook rewrote it unstaged."""
        repo, baseline = _seed_repo(make_git_repo, "r-mm")
        _write(repo / "a.txt", "staged version\n")
        git_in(repo, "add", "a.txt")
        _write(repo / "a.txt", "hook formatted version\n")
        assert git_in(repo, "status", "--porcelain") == "MM a.txt"

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is True
        assert git_in(repo, "status", "--porcelain") == ""
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha\n"
        assert "+hook formatted version" in _only_patch(repo).read_text(
            encoding="utf-8"
        )

    def test_tracked_modified_review_artifact_survives_byte_identical(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("r-tracked-artifact")
        exclude_cw_dir(repo)
        commit_tracked_file(repo, _ARTIFACT, '{"v": 1}\n')
        commit_tracked_file(repo, "a.txt", "alpha\n")
        _write(repo / _ARTIFACT, '{"v": 2, "note": "cw rewrote me"}\n')
        before = (repo / _ARTIFACT).read_bytes()
        baseline = capture_cycle_baseline(repo)
        assert isinstance(baseline, CycleBaseline)
        assert baseline.review_artifacts == frozenset({_ARTIFACT})
        _write(repo / "a.txt", "alpha changed\n")

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is True
        assert (repo / _ARTIFACT).read_bytes() == before
        # git_in strips stdout, so the leading status space is gone.
        assert git_in(repo, "status", "--porcelain") == f"M {_ARTIFACT}"
        assert _ARTIFACT not in _only_patch(repo).read_text(encoding="utf-8")

    def test_untracked_review_artifact_and_ignored_file_survive(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("r-untracked-artifact")
        exclude_cw_dir(repo)
        commit_tracked_file(repo, ".gitignore", "*.log\n")
        commit_tracked_file(repo, "a.txt", "alpha\n")
        _write(repo / _ARTIFACT, '{"v": 1}\n')
        baseline = capture_cycle_baseline(repo)
        assert isinstance(baseline, CycleBaseline)
        _write(repo / "a.txt", "alpha changed\n")
        _write(repo / "build.log", "ignored output\n")

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is True
        assert (repo / _ARTIFACT).read_text(encoding="utf-8") == '{"v": 1}\n'
        assert (repo / "build.log").read_text(encoding="utf-8") == "ignored output\n"
        status = git_in(repo, "status", "--porcelain", "--untracked-files=all")
        assert status == f"?? {_ARTIFACT}"
        assert "build.log" not in _only_patch(repo).read_text(encoding="utf-8")

    def test_head_moved_skips_snapshot_and_restore(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-head-moved")
        _write(repo / "b.txt", "committed meanwhile\n")
        git_in(repo, "add", "b.txt")
        git_in(repo, "commit", "-m", "someone committed")
        _write(repo / "a.txt", "alpha changed\n")

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            result = _restore(repo, baseline)

        assert result == CycleRestore(
            False, None, cycle_restore.HEAD_MOVED_WHY, silent=True
        )
        assert refused_patches(repo) == []
        assert not (repo / _REFUSED_DIR).exists()
        assert git_in(repo, "status", "--porcelain") == "M a.txt"
        [record] = [r for r in caplog.records if r.name == _LOGGER]
        assert record.args == (1, "r_x", cycle_restore.HEAD_MOVED_WHY)

    def test_patch_write_oserror_does_not_restore(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-write-error")
        _write(repo / "a.txt", "alpha changed\n")
        # A regular file where the patch directory must go: a real mkdir error.
        _write(repo / ".cw" / "codex_fix_refused", "in the way\n")

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is None
        assert result.why_not is not None
        assert "FileExistsError" in result.why_not
        assert git_in(repo, "status", "--porcelain") == "M  a.txt"
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha changed\n"

    def test_short_or_corrupt_write_is_caught_by_readback(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-readback")
        _write(repo / "a.txt", "alpha changed\n")
        real_read = Path.read_bytes

        def _corrupt(self: Path) -> bytes:
            data = real_read(self)
            # The readback runs on the temp file beside the final name.
            return data[:-1] + b"?" if ".patch" in self.name else data

        monkeypatch.setattr(Path, "read_bytes", _corrupt)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.why_not == cycle_restore._READBACK_WHY
        assert refused_patches(repo) == []
        assert list((repo / _REFUSED_DIR).glob("*.tmp")) == []
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha changed\n"

    def test_empty_diff_for_touched_paths_does_not_restore(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No durable non-empty patch means no discard, and the dirty path is named."""
        repo, baseline = _seed_repo(make_git_repo, "r-empty-diff")
        _write(repo / "a.txt", "alpha changed\n")
        monkeypatch.setattr(cycle_restore, "_diff_bytes", lambda *_a, **_k: b"")

        result = _restore(repo, baseline)

        assert result == CycleRestore(
            False,
            None,
            cycle_restore._EMPTY_PATCH_WHY,
            dirty_paths=("a.txt",),
        )
        assert refused_patches(repo) == []
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha changed\n"

    def test_failed_fsync_leaves_no_patch_under_final_name_and_no_discard(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-fsync-fail")
        _write(repo / "a.txt", "alpha changed\n")
        monkeypatch.setattr(cycle_restore.os, "fsync", _oserror)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is None
        assert result.why_not == "OSError saving the patch"
        assert result.dirty_paths == ("a.txt",)
        assert refused_patches(repo) == []
        assert list((repo / _REFUSED_DIR).glob("*.tmp")) == []
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha changed\n"

    def test_directory_fsync_after_publication_removes_patch_and_does_not_discard(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-directory-fsync-fail")
        _write(repo / "a.txt", "alpha changed\n")
        real_fsync = cycle_restore.os.fsync
        calls = 0

        def _fail_after_publication(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                message = "directory on fire"
                raise OSError(message)
            real_fsync(fd)

        monkeypatch.setattr(cycle_restore.os, "fsync", _fail_after_publication)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is None
        assert result.why_not == "OSError saving the patch"
        assert refused_patches(repo) == []
        assert list((repo / _REFUSED_DIR).glob("*.tmp")) == []
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha changed\n"

    def test_published_patch_unlink_failure_reports_final_path(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-published-unlink-fail")
        _write(repo / "a.txt", "alpha changed\n")
        real_unlink = Path.unlink

        def _fail_published_unlink(self: Path, *, missing_ok: bool = False) -> None:
            if self.suffix == ".patch":
                message = "published patch is busy"
                raise OSError(message)
            real_unlink(self, missing_ok=missing_ok)

        def _fail_verify(*_args: object, **_kwargs: object) -> None:
            message = "verification failed"
            raise cycle_restore._NotRestoredError(message)

        monkeypatch.setattr(Path, "unlink", _fail_published_unlink)
        monkeypatch.setattr(cycle_restore, "_verify_patch", _fail_verify)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is not None
        assert (repo / result.patch).is_file()
        assert "could not durably remove the published patch" in (result.why_not or "")
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha changed\n"

    def test_published_patch_cleanup_fsync_failure_reports_final_path(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-published-cleanup-fsync-fail")
        _write(repo / "a.txt", "alpha changed\n")
        real_fsync_directory = cycle_restore._fsync_directory
        calls = 0

        def _fail_cleanup_fsync(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                message = "directory cleanup is not durable"
                raise OSError(message)
            real_fsync_directory(path)

        def _fail_verify(*_args: object, **_kwargs: object) -> None:
            message = "verification failed"
            raise cycle_restore._NotRestoredError(message)

        monkeypatch.setattr(cycle_restore, "_fsync_directory", _fail_cleanup_fsync)
        monkeypatch.setattr(cycle_restore, "_verify_patch", _fail_verify)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is None
        assert "neither saved nor discarded" in (failure_note(result) or "")
        assert "could not durably remove the published patch" in (result.why_not or "")
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha changed\n"

    def test_head_moved_during_restore_keeps_patch_and_names_both_shas(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-head-race")
        _write(repo / "a.txt", "alpha changed\n")
        real_restore_paths = cycle_restore._restore_paths

        def _move_head_then_restore(
            worktree: Path, base: CycleBaseline, snapshot: _Snapshot
        ) -> None:
            git_in(repo, "commit", "--allow-empty", "-m", "moved mid-cycle")
            real_restore_paths(worktree, base, snapshot)

        monkeypatch.setattr(cycle_restore, "_restore_paths", _move_head_then_restore)

        result = _restore(repo, baseline)

        moved = git_in(repo, "rev-parse", "HEAD")
        assert moved != baseline.head_sha
        assert result is not None
        assert result.restored is False
        assert result.patch is not None
        assert (repo / result.patch).is_file()
        assert result.why_not is not None
        assert result.why_not.startswith("cycle_restore_head_moved")
        assert baseline.head_sha[:12] in result.why_not
        assert moved[:12] in result.why_not
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha changed\n"

    def test_path_changed_after_snapshot_is_not_discarded(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-content-race")
        _write(repo / "a.txt", "alpha changed\n")
        real_restore_paths = cycle_restore._restore_paths

        def _edit_then_restore(
            worktree: Path, base: CycleBaseline, snapshot: _Snapshot
        ) -> None:
            _write(repo / "a.txt", "racing edit\n")
            real_restore_paths(worktree, base, snapshot)

        monkeypatch.setattr(cycle_restore, "_restore_paths", _edit_then_restore)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is not None
        assert result.why_not == "`a.txt` changed after the snapshot"
        assert (repo / "a.txt").read_text(encoding="utf-8") == "racing edit\n"

    def test_cw_dir_not_ignored_does_not_restore(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-not-ignored", exclude=False)
        _write(repo / "a.txt", "alpha changed\n")

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is None
        assert result.why_not == cycle_restore._NOT_IGNORED_WHY
        assert not (repo / _REFUSED_DIR).exists()
        assert git_in(repo, "status", "--porcelain") == "M  a.txt"

    def test_check_ignore_git_failure_does_not_restore(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-check-ignore")
        _write(repo / "a.txt", "alpha changed\n")
        real = cycle_restore.run_git

        def _fail_check_ignore(
            argv: Sequence[str],
            *,
            cwd: Path | str | None = None,
            check: bool = False,
            capture_output: bool = False,
            text: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            if argv[0] == "check-ignore":
                return subprocess.CompletedProcess(list(argv), 128, "", "fatal")
            return real(
                argv, cwd=cwd, check=check, capture_output=capture_output, text=text
            )

        monkeypatch.setattr(cycle_restore, "run_git", _fail_check_ignore)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.why_not == "`git check-ignore` failed (exit 128)"

    def test_diff_git_failure_does_not_restore(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-diff-fail")
        _write(repo / "a.txt", "alpha changed\n")
        monkeypatch.setattr(cycle_restore, "_diff_bytes", _boom)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is None
        assert result.why_not == "`git boom` failed (exit 128)"
        assert refused_patches(repo) == []
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha changed\n"

    def test_restore_failure_keeps_patch_and_reports_not_restored(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-restore-fail")
        _write(repo / "a.txt", "alpha changed\n")
        monkeypatch.setattr(cycle_restore, "_restore_paths", _boom)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch == str(_only_patch(repo).relative_to(repo))
        assert result.why_not == "`git boom` failed (exit 128)"

    def test_restore_oserror_keeps_patch(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-restore-oserror")
        _write(repo / "a.txt", "alpha changed\n")
        monkeypatch.setattr(cycle_restore, "_restore_paths", _oserror)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is not None
        assert result.why_not == "OSError while restoring"

    def test_post_restore_dirty_probe_reports_not_restored(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-still-dirty")
        _write(repo / "a.txt", "alpha changed\n")
        monkeypatch.setattr(cycle_restore, "_restore_paths", lambda *_a: None)

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is not None
        assert result.why_not == cycle_restore._STILL_DIRTY_WHY

    def test_empty_cycle_writes_no_patch(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-empty")

        assert _restore(repo, baseline) is None
        assert not (repo / _REFUSED_DIR).exists()

    def test_second_refusal_same_cycle_gets_distinct_patch_name(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-collision")
        with freeze_time("2026-11-09 12:00:00"):
            _write(repo / "a.txt", "first refusal\n")
            first = _restore(repo, baseline)
            first_bytes = _only_patch(repo).read_bytes()
            _write(repo / "a.txt", "second refusal\n")
            second = _restore(repo, baseline)

        assert first is not None
        assert second is not None
        assert first.patch is not None
        assert second.patch is not None
        assert first.patch.endswith("-20261109T120000000000.patch")
        assert second.patch.endswith("-20261109T120000000000-2.patch")
        assert (repo / first.patch).read_bytes() == first_bytes
        assert b"+second refusal" in (repo / second.patch).read_bytes()

    def test_name_exhaustion_does_not_restore(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-exhausted")
        monkeypatch.setattr(cycle_restore, "MAX_PATCH_NAME_ATTEMPTS", 1)
        with freeze_time("2026-11-09 12:00:00"):
            _write(repo / "a.txt", "first\n")
            first = _restore(repo, baseline)
            _write(repo / "a.txt", "second\n")
            second = _restore(repo, baseline)

        assert first is not None
        assert first.restored is True
        assert second is not None
        assert second.restored is False
        assert second.why_not == cycle_restore._NAME_EXHAUSTED_WHY
        assert (repo / "a.txt").read_text(encoding="utf-8") == "second\n"

    def test_more_than_chunk_paths_restored(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-chunks")
        monkeypatch.setattr(cycle_restore, "RESTORE_CHUNK_SIZE", 2)
        for i in range(7):
            _write(repo / f"many{i}.txt", f"{i}\n")
        _write(repo / "a.txt", "alpha changed\n")

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is True
        assert git_in(repo, "status", "--porcelain") == ""

    def test_glob_characters_in_paths_are_literal(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-literal")
        _write(repo / "star*.txt", "star\n")
        _write(repo / "starfish.txt", "keep me in the cycle\n")

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is True
        assert git_in(repo, "status", "--porcelain") == ""

    def test_unmerged_marker_set_by_fix_reports_not_restored(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-merge-head")
        git_in(repo, "checkout", "-b", "other")
        _write(repo / "other.txt", "from other\n")
        git_in(repo, "add", "other.txt")
        git_in(repo, "commit", "-m", "other work")
        git_in(repo, "checkout", "-")
        git_in(repo, "merge", "--no-commit", "--no-ff", "other")

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is False
        assert result.patch is not None
        assert result.why_not == cycle_restore._STILL_DIRTY_WHY

    def test_success_logs_info_with_patch_path_and_short_sha(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-log-info")
        _write(repo / "a.txt", "alpha changed\n")

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            result = _restore(repo, baseline, cycle=3, reason="codex_fix_hook_failed")

        assert result is not None
        [record] = [r for r in caplog.records if r.name == _LOGGER]
        assert record.levelno == logging.INFO
        assert record.args == (
            3,
            "codex_fix_hook_failed",
            result.patch,
            baseline.head_sha[:12],
        )
        assert "alpha changed" not in record.getMessage()

    def test_failure_logs_warning_with_reason_and_never_the_diff(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "r-log-warn", exclude=False)
        _write(repo / "a.txt", "SECRET-DIFF-TEXT\n")

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _restore(repo, baseline, cycle=2, reason="codex_fix_hook_failed")

        [record] = [r for r in caplog.records if r.name == _LOGGER]
        assert record.levelno == logging.WARNING
        assert record.args == (
            2,
            "codex_fix_hook_failed",
            cycle_restore._NOT_IGNORED_WHY,
        )
        assert "SECRET-DIFF-TEXT" not in record.getMessage()


_PATCH = ".cw/codex_fix_refused/cycle-1-r-20261109T120000000000.patch"
_RESTORED = CycleRestore(True, _PATCH, None, head_short="abc123def456")
_PATCH_SAVED = CycleRestore(False, _PATCH, "boom", head_short="abc123def456")
_NOTHING_SAVED = CycleRestore(False, None, "no dice")
_SILENT = CycleRestore(False, None, "HEAD moved", silent=True)


def _breach(hint: str, *, restorable: bool = True) -> FenceBreach:
    return FenceBreach("r", ("a.py",), "details", hint, restorable=restorable)


class TestApplyToBreach:
    def test_restored_text_replaces_left_staged_hint_and_names_the_patch(self) -> None:
        breach = _breach(f"Settle it. {LEFT_STAGED_HINT}")

        out = apply_to_breach(breach, _RESTORED)

        assert LEFT_STAGED_HINT not in out.recovery_hint
        assert out.recovery_hint.startswith("Settle it. Saved the rejected changes")
        assert f"git apply --index {_PATCH}" in out.recovery_hint
        assert f"git apply --stat {_PATCH}" in out.recovery_hint
        assert "`abc123def456`" in out.recovery_hint
        assert out.details == breach.details
        assert out.reason == breach.reason

    def test_mid_string_hint_keeps_the_text_around_it(self) -> None:
        breach = _breach(f"Before. {LEFT_STAGED_HINT} After the hint.")

        out = apply_to_breach(breach, _RESTORED)

        assert out.recovery_hint.startswith("Before. Saved")
        assert out.recovery_hint.endswith("After the hint.")

    def test_hint_without_the_marker_text_gets_the_patch_text_appended(self) -> None:
        out = apply_to_breach(_breach("Custom hint."), _RESTORED)

        assert out.recovery_hint.startswith("Custom hint. Saved the rejected")

    def test_patch_saved_but_not_restored_keeps_old_hint_and_appends_reason(
        self,
    ) -> None:
        breach = _breach(f"Settle it. {LEFT_STAGED_HINT}")

        out = apply_to_breach(breach, _PATCH_SAVED)

        assert out.recovery_hint.startswith(breach.recovery_hint)
        assert f"Saved the rejected changes to `{_PATCH}` but could not" in (
            out.recovery_hint
        )
        assert "boom" in out.recovery_hint

    def test_nothing_saved_keeps_old_hint_and_appends_reason(self) -> None:
        breach = _breach(LEFT_STAGED_HINT)

        out = apply_to_breach(breach, _NOTHING_SAVED)

        assert out.recovery_hint.startswith(LEFT_STAGED_HINT)
        assert "neither saved nor discarded" in out.recovery_hint
        assert "no dice" in out.recovery_hint

    @pytest.mark.parametrize("restore", [_RESTORED, _PATCH_SAVED, _NOTHING_SAVED])
    def test_non_restorable_breach_is_returned_untouched(
        self, restore: CycleRestore
    ) -> None:
        breach = _breach("Inspect first.", restorable=False)

        assert apply_to_breach(breach, restore) is breach

    @pytest.mark.parametrize("restore", [None, _SILENT])
    def test_empty_or_silent_outcome_returns_the_breach_untouched(
        self, restore: CycleRestore | None
    ) -> None:
        breach = _breach(LEFT_STAGED_HINT)

        assert apply_to_breach(breach, restore) is breach


class TestFailureNote:
    def test_restored_note_names_patch_and_apply_command(self) -> None:
        note = failure_note(_RESTORED)

        assert note is not None
        assert _PATCH in note
        assert "git apply --index" in note

    def test_patch_saved_not_restored_note(self) -> None:
        note = failure_note(_PATCH_SAVED)

        assert note is not None
        assert note == (
            f"Saved the rejected changes to `{_PATCH}` but could not restore "
            "the worktree: boom."
        )

    def test_nothing_saved_note(self) -> None:
        note = failure_note(_NOTHING_SAVED)

        assert note is not None
        assert "no dice" in note

    @pytest.mark.parametrize("restore", [None, _SILENT])
    def test_no_note_for_empty_or_silent_outcome(
        self, restore: CycleRestore | None
    ) -> None:
        assert failure_note(restore) is None


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


class TestImportDirection:
    @pytest.mark.parametrize("module", ["fence", "baseline"])
    def test_guard_modules_do_not_import_cycle_restore(self, module: str) -> None:
        source = Path(cycle_restore.__file__).with_name(f"{module}.py")

        assert "cw.codex_fix_loop.cycle_restore" not in _imported_modules(source)


# ---------------------------------------------------------------------------
# Through the real loop: one test per refusal reason.
# ---------------------------------------------------------------------------


def _assert_parked_clean(
    out: AutoDevResult, worktree: Path, start: str, reason: str
) -> None:
    assert _blocker_reason(out) == reason
    assert out.blocker is not None
    assert out.blocker.recovery_hint is not None
    assert _PATCH_HINT in out.blocker.recovery_hint
    assert LEFT_STAGED_HINT not in out.blocker.recovery_hint
    assert_clean_after_refusal(worktree, start)


def _hook_loop(
    make_git_repo: Callable[..., Path],
    name: str,
    hook: str,
    behaviors: list[_FixBehavior],
) -> tuple[AutoDevResult, Path, str]:
    worktree = _worktree(make_git_repo, name)
    _install_pre_commit_hook(worktree, hook)
    start = git_in(worktree, "rev-parse", "HEAD")
    runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC], fix_behaviors=behaviors)
    out, _ = _run_loop(runner, worktree, session_id=f"s-{name}")
    return out, worktree, start


def _partial_edit_then_timeout(worktree: Path, _argv: list[str]) -> CodexRunResult:
    _write(worktree / "half.py", "half = 1\n")
    return CodexRunResult(returncode=1, stdout="", stderr="", timed_out=True)


class TestCleanTreeAfterRefusal:
    def test_hook_failed_not_attempted_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, worktree, start = _hook_loop(
            make_git_repo,
            "c-hook-na",
            "#!/bin/sh\necho LINT-ERROR-XYZ\nexit 1\n",
            [_editor()],
        )

        _assert_parked_clean(out, worktree, start, CODEX_FIX_HOOK_FAILED)
        assert "Repair turn: not attempted." in (
            out.blocker.details if out.blocker else ""
        )

    def test_hook_failed_repair_turn_failed_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        def _untracked_then_timeout(path: Path, _argv: list[str]) -> CodexRunResult:
            _write(path / "stray.py", "x = 1\n")
            return CodexRunResult(returncode=1, stdout="", stderr="", timed_out=True)

        out, worktree, start = _hook_loop(
            make_git_repo,
            "c-hook-turn",
            _LINT_HOOK,
            [_MARKER_FIX, _untracked_then_timeout],
        )

        _assert_parked_clean(out, worktree, start, CODEX_FIX_HOOK_FAILED)
        assert "stray.py" in _only_patch(worktree).read_text(encoding="utf-8")

    def test_hook_failed_repair_rejected_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, worktree, start = _hook_loop(
            make_git_repo,
            "c-hook-rejected",
            _ALWAYS_FAILING_HOOK,
            [_MARKER_FIX, _CLEAN_REPAIR],
        )

        _assert_parked_clean(out, worktree, start, CODEX_FIX_HOOK_FAILED)

    def test_reformatting_hook_mm_state_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#2967 repro: the hook rewrites the file on every run, ending ``MM``."""
        out, worktree, start = _hook_loop(
            make_git_repo, "c-hook-mm", _REFORMATTING_HOOK, [_editor()]
        )

        _assert_parked_clean(out, worktree, start, CODEX_FIX_HOOK_FAILED)
        assert "+reformatted = 1" in _only_patch(worktree).read_text(encoding="utf-8")

    def test_growth_budget_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _with_plan(_worktree(make_git_repo, "c-growth"))
        start = git_in(worktree, "rev-parse", "HEAD")
        grown = _CONTENT + "".join(f"g{i} = {i}\n" for i in range(12))
        runner = _FixLoopRunner(
            [_MF_DOC, _CLEAN_DOC], fix_behaviors=[_editor("new.py", grown)]
        )

        out, _ = _run_loop(
            runner, worktree, session_id="s-c-growth", growth_budget_lines=5
        )

        _assert_parked_clean(out, worktree, start, CODEX_FIX_GROWTH_BUDGET)

    def test_cumulative_growth_budget_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _with_plan(_worktree(make_git_repo, "c-cumulative"))
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=_cumulative_editors(5))

        out, _ = _run_loop(runner, worktree, session_id="s-c-cumulative")

        assert _blocker_reason(out) == CODEX_FIX_GROWTH_BUDGET
        assert out.blocker is not None
        assert "cumulative" in out.blocker.details
        # Cycles 1-3 committed; the refused cycle 4 restores to THAT head.
        _assert_parked_clean(
            out,
            worktree,
            git_in(worktree, "rev-parse", "HEAD"),
            CODEX_FIX_GROWTH_BUDGET,
        )
        assert _only_patch(worktree).name.startswith("cycle-4-")

    def test_constraint_violation_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree(make_git_repo, "c-constraint")
        commit_tracked_file(worktree, ".claude/project-config.yaml", _LINEAR_CONFIG)
        _no_comment_fetch(monkeypatch)
        start = git_in(worktree, "rev-parse", "HEAD")
        runner = _FixLoopRunner(
            [_MF_DOC, _CLEAN_DOC], fix_behaviors=[_editor("new.py", _FOO_BAR_CONTENT)]
        )

        out, _ = _run_loop(
            runner,
            worktree,
            session_id="s-c-constraint",
            task=_row_task(_ROW_FOO_BAR),
            operator_login=_LOGIN,
        )

        _assert_parked_clean(out, worktree, start, CODEX_FIX_CONSTRAINT_VIOLATION)

    def test_scope_violation_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "c-violation", manifest=_SENSITIVE)
        start = git_in(worktree, "rev-parse", "HEAD")
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[_editor("pyproject.toml", '[project]\nname = "z"\n')],
        )

        out, _ = _run_loop(runner, worktree, session_id="s-c-violation")

        _assert_parked_clean(out, worktree, start, CODEX_FIX_SCOPE_VIOLATION)

    def test_scope_drift_fence_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _with_plan(_worktree(make_git_repo, "c-drift"))
        start = git_in(worktree, "rev-parse", "HEAD")
        runner = _FixLoopRunner(
            [_MF_DOC, _CLEAN_DOC],
            fix_behaviors=[_editor("server/route.py", "r = 1\n")],
        )

        out, _ = _run_loop(runner, worktree, session_id="s-c-drift")

        _assert_parked_clean(out, worktree, start, CODEX_FIX_SCOPE_DRIFT)
        assert "server/route.py" in _only_patch(worktree).read_text(encoding="utf-8")

    def test_reverted_branch_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "c-revert")
        start = git_in(worktree, "rev-parse", "HEAD")
        runner = _FixLoopRunner(
            [_MF_DOC, _CLEAN_DOC], fix_behaviors=[_remover("new.py")]
        )

        out, _ = _run_loop(runner, worktree, session_id="s-c-revert")

        _assert_parked_clean(out, worktree, start, CODEX_FIX_REVERTED_BRANCH)
        assert (worktree / "new.py").exists()

    def test_non_hook_commit_git_error_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "c-git-error")
        # An empty committer name makes `git commit` itself fail, no hook.
        git_in(worktree, "config", "user.name", "")
        start = git_in(worktree, "rev-parse", "HEAD")
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor()])

        out, _ = _run_loop(runner, worktree, session_id="s-c-git-error")

        assert _blocker_reason(out) == "codex_error"
        assert out.blocker is not None
        assert ".cw/codex_fix_refused/" in out.blocker.details
        assert_clean_after_refusal(worktree, start)

    def test_repair_restage_git_error_leaves_clean_tree(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(commit, "_restage", _boom)
        timeout = CodexRunResult(returncode=1, stdout="", stderr="", timed_out=True)
        out, worktree, start = _hook_loop(
            make_git_repo, "c-restage", _LINT_HOOK, [_MARKER_FIX, timeout]
        )

        assert _blocker_reason(out) == "codex_error"
        assert out.blocker is not None
        assert ".cw/codex_fix_refused/" in out.blocker.details
        assert_clean_after_refusal(worktree, start)

    def test_failed_invocation_partial_edits_leaves_clean_tree_retry_eligible(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "c-timeout")
        start = git_in(worktree, "rev-parse", "HEAD")
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_partial_edit_then_timeout])

        out, _ = _run_loop(runner, worktree, session_id="s-c-timeout")

        assert _blocker_reason(out) == CODEX_TIMEOUT
        assert out.blocker is not None
        assert out.blocker.retry_eligible is True
        assert "half.py" in _only_patch(worktree).read_text(encoding="utf-8")
        assert_clean_after_refusal(worktree, start)

    def test_secret_scanner_failure_posts_only_the_patch_path(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, worktree, start = _hook_loop(
            make_git_repo,
            "c-scanner",
            "#!/bin/sh\necho '- hook id: gitleaks'\necho '- exit code: 1'\n"
            "echo 'leaked SECRET-ABC123'\nexit 1\n",
            [_editor("fix.py", "token = 'SECRET-ABC123'\n")],
        )

        assert out.blocker is not None
        assert out.blocker.recovery_hint is not None
        assert "SECRET-ABC123" not in out.blocker.details
        assert "SECRET-ABC123" not in out.blocker.recovery_hint
        assert _PATCH_HINT in out.blocker.recovery_hint
        assert_clean_after_refusal(worktree, start)


class TestAutomaticRestore:
    def test_restore_is_automatic_without_worktree_opt_in(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, baseline = _seed_repo(make_git_repo, "n-automatic-restore")
        _write(repo / "a.txt", "operator keeps this edit\n")

        result = _restore(repo, baseline)

        assert result is not None
        assert result.restored is True
        assert result.patch is not None
        assert (repo / "a.txt").read_text(encoding="utf-8") == "alpha\n"

    def test_dirty_start_never_restores(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "n-dirty")
        _write(worktree / "operator-work.py", "precious = 1\n")
        _write(worktree / "new.py", "def edited_by_operator():\n    pass\n")
        runner = _FixLoopRunner([_MF_DOC])

        out, _ = _run_loop(runner, worktree, session_id="s-n-dirty")

        assert _blocker_reason(out) == "codex_fix_dirty_start"
        assert runner.fix_calls == 0
        assert (worktree / "operator-work.py").read_text(encoding="utf-8") == (
            "precious = 1\n"
        )
        assert "edited_by_operator" in (worktree / "new.py").read_text(encoding="utf-8")
        assert not (worktree / _REFUSED_DIR).exists()

    def test_baseline_probe_git_error_never_restores(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree(make_git_repo, "n-probe")
        _write(worktree / "operator-work.py", "precious = 1\n")
        monkeypatch.setattr(commit, "capture_cycle_baseline", _boom)
        runner = _FixLoopRunner([_MF_DOC])

        out, _ = _run_loop(runner, worktree, session_id="s-n-probe")

        assert _blocker_reason(out) == "codex_error"
        assert (worktree / "operator-work.py").exists()
        assert not (worktree / _REFUSED_DIR).exists()

    def test_staged_set_extra_staged_path_stays_staged(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree(make_git_repo, "n-staged-extra")
        real = commit._cycle_breach

        def _racing(*args: object, **kwargs: object) -> object:
            result = real(*args, **kwargs)
            _write(worktree / "race.py", "r = 1\n")
            git_in(worktree, "add", "race.py")
            return result

        monkeypatch.setattr(commit, "_cycle_breach", _racing)
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor()])

        out, _ = _run_loop(runner, worktree, session_id="s-n-staged-extra")

        assert _blocker_reason(out) == CODEX_FIX_SCOPE_DRIFT
        assert out.blocker is not None
        assert out.blocker.recovery_hint is not None
        assert _PATCH_HINT not in out.blocker.recovery_hint
        assert "race.py" in git_in(worktree, "diff", "--cached", "--name-only")
        assert not (worktree / _REFUSED_DIR).exists()

    def test_self_committed_fix_keeps_commit_and_head(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "n-self-commit")
        start = git_in(worktree, "rev-parse", "HEAD")

        def _self_commit(path: Path, _argv: list[str]) -> CodexRunResult:
            _write(path / "fix.py", "patched = 1\n")
            git_in(path, "add", "fix.py")
            git_in(path, "commit", "-m", "codex committed on its own")
            return CodexRunResult(returncode=0, stdout="", stderr="")

        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_self_commit])

        out, _ = _run_loop(runner, worktree, session_id="s-n-self-commit")

        assert _blocker_reason(out) == CODEX_FIX_SCOPE_DRIFT
        assert git_in(worktree, "rev-parse", "HEAD") != start
        assert (worktree / "fix.py").exists()
        assert not (worktree / _REFUSED_DIR).exists()

    def test_push_failure_after_real_commit_keeps_commit(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        worktree = _worktree(make_git_repo, "n-push")
        _install_pre_commit_hook(worktree, _PASSING_HOOK)
        git_in(worktree, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
        start = git_in(worktree, "rev-parse", "HEAD")
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor()])

        out, _ = _run_loop(runner, worktree, session_id="s-n-push")

        assert _blocker_reason(out) == "codex_error"
        head, count, _tip = _heads(worktree)
        assert head != start
        assert git_in(worktree, "show", "--name-only", "--format=", "HEAD") == "fix.py"
        assert int(count) >= 2
        assert not (worktree / _REFUSED_DIR).exists()
        assert out.blocker is not None
        assert _PATCH_HINT not in out.blocker.details

    def test_snapshot_failure_keeps_edits_staged_and_old_hint(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cycle_restore, "_write_exclusive", _oserror)
        out, worktree, start = _hook_loop(
            make_git_repo,
            "n-snapshot-fail",
            "#!/bin/sh\necho LINT-ERROR-XYZ\nexit 1\n",
            [_editor()],
        )

        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        assert out.blocker is not None
        assert out.blocker.recovery_hint is not None
        assert LEFT_STAGED_HINT in out.blocker.recovery_hint
        assert "neither saved nor discarded" in out.blocker.recovery_hint
        assert "fix.py" in git_in(worktree, "diff", "--cached", "--name-only")
        assert git_in(worktree, "rev-parse", "HEAD") == start


class TestRequeueAfterRefusal:
    def test_requeue_after_hook_failure_runs_a_cycle_not_dirty_start(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, worktree, start = _hook_loop(
            make_git_repo, "q-hook", _REFORMATTING_HOOK, [_editor()]
        )
        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        _install_pre_commit_hook(worktree, _PASSING_HOOK)
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC], fix_behaviors=[_editor()])

        again, _ = _run_loop(runner, worktree, session_id="s-q-hook-2")

        assert again.status == "stage_complete"
        assert runner.fix_calls == 1
        assert git_in(worktree, "rev-parse", "HEAD") != start

    def test_requeue_after_growth_refusal_runs_a_cycle(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _with_plan(_worktree(make_git_repo, "q-growth"))
        grown = _CONTENT + "".join(f"g{i} = {i}\n" for i in range(12))
        first = _FixLoopRunner(
            [_MF_DOC, _CLEAN_DOC], fix_behaviors=[_editor("new.py", grown)]
        )
        out, _ = _run_loop(
            first, worktree, session_id="s-q-growth-1", growth_budget_lines=5
        )
        assert _blocker_reason(out) == CODEX_FIX_GROWTH_BUDGET
        start = git_in(worktree, "rev-parse", "HEAD")
        second = _FixLoopRunner(
            [_MF_DOC, _CLEAN_DOC],
            fix_behaviors=[_editor("new.py", _CONTENT + "ok = 1\n")],
        )

        again, _ = _run_loop(
            second, worktree, session_id="s-q-growth-2", growth_budget_lines=5
        )

        assert again.status == "stage_complete"
        assert second.fix_calls == 1
        assert git_in(worktree, "rev-parse", "HEAD") != start
