"""Tests for cw.dispatch_loop_identity (#2729).

The dispatch loop records its code identity in the singleton lock file at
start; ``assess_dispatch_loop()`` compares it with the checkout. Records are
written by ``tests/_loop_identity_helpers.py`` so no loop has to run.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cw
from cw import dispatch_loop_identity as dli
from cw.config import dispatch_loop_lock, dispatch_loop_lock_file
from cw.dispatch_loop_identity import (
    COMMIT_KEY,
    PROC_START_KEY,
    VERSION_KEY,
    CodeIdentity,
    LoopAssessment,
    LoopStatus,
    assess_dispatch_loop,
    checkout_code_identity,
    loop_record_fields,
    process_code_identity,
)
from cw.exceptions import DispatchLoopLockedError
from cw.local_runner import read_process_start_time_ns
from tests._loop_identity_helpers import (
    COMMIT_A,
    COMMIT_B,
    VERSION,
    patch_checkout_identity,
    write_loop_record,
)
from tests._shared.git_repos import git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_RESTART = "restart `cw dev-queue serve`"
_SECOND_NS = 1_000_000_000
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture(autouse=True)
def _fresh_process_identity() -> Iterator[None]:
    process_code_identity.cache_clear()
    yield
    process_code_identity.cache_clear()


def _own_start() -> int:
    start = read_process_start_time_ns(os.getpid())
    assert start is not None
    return start


class TestNoLoop:
    def test_no_lock_file(self) -> None:
        assert assess_dispatch_loop().status is LoopStatus.NO_LOOP

    @pytest.mark.parametrize(
        "content", ["", "not json {{", "[1, 2]", '{"cmd": "x"}', '{"pid": 0}']
    )
    def test_empty_garbage_or_pidless_record(self, content: str) -> None:
        path = dispatch_loop_lock_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        assessment = assess_dispatch_loop()
        assert assessment.status is LoopStatus.NO_LOOP
        assert assessment.message == "no dispatch loop lock holder recorded"

    def test_boolean_pid_is_not_a_pid(self) -> None:
        path = dispatch_loop_lock_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"pid": true, "version": "1"}', encoding="utf-8")
        assert assess_dispatch_loop().status is LoopStatus.NO_LOOP

    def test_unreadable_lock_path_is_no_loop(self) -> None:
        dispatch_loop_lock_file().mkdir(parents=True)
        assert assess_dispatch_loop().status is LoopStatus.NO_LOOP

    def test_dead_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        write_loop_record()
        monkeypatch.setattr(dli, "read_process_start_time_ns", lambda _pid: None)
        assert assess_dispatch_loop().status is LoopStatus.NO_LOOP

    def test_recycled_pid_beyond_tolerance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(proc_start_ns=_own_start() - 3 * _SECOND_NS)
        calls = patch_checkout_identity(monkeypatch)
        assert assess_dispatch_loop().status is LoopStatus.NO_LOOP
        assert calls == []

    def test_recycled_pid_recorded_start_later_than_live_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(proc_start_ns=_own_start() + 3 * _SECOND_NS)
        calls = patch_checkout_identity(monkeypatch)
        assert assess_dispatch_loop().status is LoopStatus.NO_LOOP
        assert calls == []

    def test_start_time_within_tolerance_is_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(proc_start_ns=_own_start() + _SECOND_NS)
        patch_checkout_identity(monkeypatch)
        assert assess_dispatch_loop().status is LoopStatus.MATCH


class TestComparison:
    def test_equal_commits_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        write_loop_record()
        patch_checkout_identity(monkeypatch)
        assessment = assess_dispatch_loop()
        assert assessment.status is LoopStatus.MATCH
        assert assessment.pid == os.getpid()
        assert assessment.message == (
            f"dispatch loop (pid {os.getpid()}) matches the checkout"
            f" ({VERSION} @ {COMMIT_A[:12]})"
        )

    def test_different_commits_are_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        write_loop_record(version="1.85.0", commit=COMMIT_A)
        patch_checkout_identity(monkeypatch, version="1.85.1", commit=COMMIT_B)
        assessment = assess_dispatch_loop()
        assert assessment.status is LoopStatus.STALE
        assert assessment.message == (
            f"dispatch loop (pid {os.getpid()}) started on 1.85.0 @ {COMMIT_A[:12]}"
            f" but the checkout is at 1.85.1 @ {COMMIT_B[:12]}; {_RESTART}"
        )

    def test_commits_differ_versions_equal_is_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(commit=COMMIT_A)
        patch_checkout_identity(monkeypatch, commit=COMMIT_B)
        assert assess_dispatch_loop().status is LoopStatus.STALE

    def test_commits_equal_versions_differ_is_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(version="1.0.0")
        patch_checkout_identity(monkeypatch, version="2.0.0")
        assert assess_dispatch_loop().status is LoopStatus.MATCH

    def test_no_current_commit_equal_versions_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Git failed on the checkout side only: dist-info lags, so no MATCH."""
        write_loop_record()
        patch_checkout_identity(monkeypatch, commit=None)
        assert assess_dispatch_loop().status is LoopStatus.IDENTITY_UNKNOWN

    def test_no_current_commit_different_versions_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(version="1.0.0")
        patch_checkout_identity(monkeypatch, version="2.0.0", commit=None)
        assert assess_dispatch_loop().status is LoopStatus.IDENTITY_UNKNOWN

    def test_no_recorded_commit_equal_versions_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(version="1.0.0", commit=None)
        patch_checkout_identity(monkeypatch, version="1.0.0", commit=COMMIT_B)
        assert assess_dispatch_loop().status is LoopStatus.IDENTITY_UNKNOWN

    def test_no_recorded_commit_different_versions_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(version="1.0.0", commit=None)
        patch_checkout_identity(monkeypatch, version="2.0.0", commit=COMMIT_B)
        assert assess_dispatch_loop().status is LoopStatus.IDENTITY_UNKNOWN

    def test_neither_side_has_a_commit_equal_versions_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(commit=None)
        patch_checkout_identity(monkeypatch, commit=None)
        assert assess_dispatch_loop().status is LoopStatus.MATCH

    def test_neither_side_has_a_commit_different_versions_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(version="1.0.0", commit=None)
        patch_checkout_identity(monkeypatch, version="2.0.0", commit=None)
        assessment = assess_dispatch_loop()
        assert assessment.status is LoopStatus.STALE
        assert "started on 1.0.0 but the checkout is at 2.0.0;" in assessment.message

    def test_no_commit_either_side_and_no_version_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(version=None, commit=None)
        patch_checkout_identity(monkeypatch, commit=None)
        assessment = assess_dispatch_loop()
        assert assessment.status is LoopStatus.IDENTITY_UNKNOWN
        assert "identity unknown" in assessment.message
        assert "cannot be compared" in assessment.message
        assert "loop: unknown" in assessment.message


class TestBootstrap:
    def test_pre_change_record_is_identity_unknown_not_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(with_identity=False)
        calls = patch_checkout_identity(monkeypatch, commit=COMMIT_B)
        assessment = assess_dispatch_loop()
        assert assessment.status is LoopStatus.IDENTITY_UNKNOWN
        assert calls == []
        assert assessment.message == (
            f"dispatch loop (pid {os.getpid()}) did not record its code identity"
            " (it started before this check existed); identity unknown,"
            f" {_RESTART} to enable this check"
        )

    def test_null_start_with_identity_is_unknown_never_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record that carries identity but a literal null start time.

        The pid cannot be verified, so even differing commits must not read as
        STALE (a recycled pid would produce a false alarm).
        """
        write_loop_record(commit=COMMIT_A, null_proc_start=True)
        written = json.loads(dispatch_loop_lock_file().read_text())
        assert written[PROC_START_KEY] is None
        patch_checkout_identity(monkeypatch, commit=COMMIT_B)
        assessment = assess_dispatch_loop()
        assert assessment.status is LoopStatus.IDENTITY_UNKNOWN
        assert assessment.pid == os.getpid()

    def test_null_start_with_dead_pid_is_no_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(null_proc_start=True)
        monkeypatch.setattr(dli, "read_process_start_time_ns", lambda _pid: None)
        assert assess_dispatch_loop().status is LoopStatus.NO_LOOP

    def test_pre_change_record_with_dead_pid_is_no_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(with_identity=False)
        monkeypatch.setattr(dli, "read_process_start_time_ns", lambda _pid: None)
        assert assess_dispatch_loop().status is LoopStatus.NO_LOOP


class TestLabel:
    @pytest.mark.parametrize(
        ("identity", "label"),
        [
            (CodeIdentity("1.2.3", COMMIT_A), f"1.2.3 @ {COMMIT_A[:12]}"),
            (CodeIdentity("1.2.3", None), "1.2.3"),
            (CodeIdentity(None, COMMIT_A), COMMIT_A[:12]),
            (CodeIdentity(None, None), "unknown"),
        ],
    )
    def test_label(self, identity: CodeIdentity, label: str) -> None:
        assert identity.label() == label

    def test_stale_message_with_missing_sides_says_unknown(self) -> None:
        assessment = LoopAssessment(
            LoopStatus.STALE, pid=7, recorded=None, current=None
        )
        assert "started on unknown but the checkout is at unknown" in (
            assessment.message
        )


def _fake_package(root: Path, *, pyproject: bool = True, git: str = "dir") -> Path:
    pkg = root / "src" / "cw"
    pkg.mkdir(parents=True)
    if pyproject:
        (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    if git == "dir":
        (root / ".git").mkdir()
    elif git == "file":
        (root / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    return pkg


class TestSourceRoot:
    def test_checkout_root_accepted(self, tmp_path: Path) -> None:
        pkg = _fake_package(tmp_path)
        assert dli._source_root_of(pkg) == tmp_path

    def test_worktree_git_file_accepted(self, tmp_path: Path) -> None:
        pkg = _fake_package(tmp_path, git="file")
        assert dli._source_root_of(pkg) == tmp_path

    def test_root_without_pyproject_rejected(self, tmp_path: Path) -> None:
        pkg = _fake_package(tmp_path, pyproject=False)
        assert dli._source_root_of(pkg) is None

    def test_root_without_git_rejected(self, tmp_path: Path) -> None:
        pkg = _fake_package(tmp_path, git="none")
        assert dli._source_root_of(pkg) is None

    def test_site_packages_layout_rejected(self, tmp_path: Path) -> None:
        pkg = tmp_path / "site-packages" / "cw"
        pkg.mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        assert dli._source_root_of(pkg) is None

    def test_source_root_uses_the_imported_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = Path(cw.__file__).resolve().parent.parent.parent
        if dli._source_root_of(Path(cw.__file__).resolve().parent) is None:
            pytest.skip("cw is not imported from a source checkout")
        assert dli._source_root() == expected
        # Not the working directory: the same root from anywhere else.
        monkeypatch.chdir(tmp_path)
        assert dli._source_root() == expected


class TestCheckoutIdentity:
    def test_reads_head_of_the_source_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[..., Path],
    ) -> None:
        repo = make_git_repo("src-checkout")
        monkeypatch.setattr(dli, "_source_root", lambda: repo)
        identity = checkout_code_identity()
        assert identity.commit == git_in(repo, "rev-parse", "HEAD")
        assert identity.commit is not None
        assert _HEX40.match(identity.commit)

    def test_no_source_root_means_no_commit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dli, "_source_root", lambda: None)
        assert checkout_code_identity().commit is None

    def test_failed_git_means_no_commit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(dli, "_source_root", lambda: tmp_path)
        monkeypatch.setattr(dli, "capture_head_sha", lambda *_a, **_k: "")
        assert checkout_code_identity().commit is None

    def test_checkout_git_read_is_bounded_so_a_hung_git_cannot_stall_the_caller(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The checkout ``git rev-parse`` runs non-strict and under a timeout."""
        seen: dict[str, object] = {}
        sha = "a" * 40

        def _spy(_root: Path, **kwargs: object) -> str:
            seen.update(kwargs)
            return sha

        monkeypatch.setattr(dli, "_source_root", lambda: tmp_path)
        monkeypatch.setattr(dli, "capture_head_sha", _spy)
        assert checkout_code_identity().commit == sha
        assert seen["timeout"] == dli._GIT_TIMEOUT_S
        assert seen["strict"] is False

    def test_git_missing_from_path_means_no_commit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[..., Path],
    ) -> None:
        repo = make_git_repo("no-git-path")
        monkeypatch.setattr(dli, "_source_root", lambda: repo)
        monkeypatch.setenv("PATH", "")
        assert checkout_code_identity().commit is None

    def test_package_not_found_means_no_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _missing(_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(_name)

        monkeypatch.setattr(importlib.metadata, "version", _missing)
        monkeypatch.setattr(dli, "_source_root", lambda: None)
        assert checkout_code_identity() == CodeIdentity(version=None, commit=None)

    def test_installed_version_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dli, "_source_root", lambda: None)
        expected = importlib.metadata.version("claude-workspace")
        assert checkout_code_identity().version == expected


class TestProcessIdentity:
    def test_process_version_is_the_import_time_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An editable reinstall after import must not change what is recorded."""
        monkeypatch.setattr(dli, "_source_root", lambda: None)
        monkeypatch.setattr(dli.cw, "__version__", "9.9.9")
        monkeypatch.setattr(importlib.metadata, "version", lambda _name: "8.8.8")
        assert process_code_identity().version == "9.9.9"
        assert checkout_code_identity().version == "8.8.8"

    def test_memoized_for_the_process_lifetime(
        self,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[..., Path],
    ) -> None:
        repo = make_git_repo("memo")
        monkeypatch.setattr(dli, "_source_root", lambda: repo)
        first = process_code_identity()
        git_in(repo, "commit", "--allow-empty", "-m", "move head")
        moved = git_in(repo, "rev-parse", "HEAD")
        assert first.commit != moved
        assert process_code_identity() == first
        assert checkout_code_identity().commit == moved

    def test_loop_record_fields_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dli, "_source_root", lambda: None)
        fields = loop_record_fields()
        assert set(fields) == {PROC_START_KEY, VERSION_KEY, COMMIT_KEY}
        assert fields[PROC_START_KEY] == _own_start()
        assert fields[COMMIT_KEY] is None


class TestNoLockTaken:
    def test_assess_never_takes_the_flock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dli, "_source_root", lambda: None)
        patch_checkout_identity(
            monkeypatch, version=process_code_identity().version, commit=None
        )
        with dispatch_loop_lock(identity=loop_record_fields()):
            path = dispatch_loop_lock_file()
            before = path.read_text(encoding="utf-8")
            assessment = assess_dispatch_loop()
            assert path.read_text(encoding="utf-8") == before
            assert json.loads(before)["pid"] == os.getpid()
            assert assessment.status is LoopStatus.MATCH
            with pytest.raises(DispatchLoopLockedError), dispatch_loop_lock():
                pass
