"""Tests for cw.worktree._refresh - reuse refresh and occupancy (#2213)."""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw import native_daemon
from cw.config import save_state, state_file
from cw.events import read_events
from cw.exceptions import StaleWorktreeError, WorktreeError, WorktreeOccupiedError
from cw.models import (
    ClientConfig,
    CwState,
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.worktree import (
    FetchOutcome,
    FetchResult,
    RefreshOutcome,
    RefreshResult,
    ReuseRefreshReport,
    _normalize_path,
    _Occupancy,
    _ref_exists,
    _refresh_reused_worktree,
    _reuse_occupancy,
    _run_git,
    create_worktree,
    fetch_feature_branch,
    is_main_behind_origin,
    live_home_reason,
    live_session_worktree_paths,
)
from cw.worktree_gc import _live_worktree_paths
from tests._worktree_helpers import patch_worktree
from tests.conftest import _symlink_loop, git_in, push_commit_to_origin

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import OrchestratorEvent
    from cw.worktree import UnresolvablePathWarningKey

_REUSE_BRANCH = "dev/2213"


def _seed_reuse(
    tmp_path: Path, make_git_repo: Callable[..., Path]
) -> tuple[ClientConfig, Path, Path, Path]:
    """Real-git fixture for the reuse-refresh tests (#2213).

    Builds a workspace with a bare ``origin`` carrying ``main``, provisions the
    ``dev/2213`` worktree through ``create_worktree``, commits ``tracked.txt``
    in it and pushes the branch. Returns ``(client, wt, origin, workspace)``
    with the worktree clean, fully pushed, and in sync with the workspace's
    ``refs/remotes/origin/dev/2213``.
    """
    workspace = make_git_repo("workspace")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git_in(origin, "init", "--bare", "-b", "main")
    git_in(workspace, "remote", "add", "origin", str(origin))
    git_in(workspace, "push", "origin", "main")
    git_in(workspace, "fetch", "origin")
    client = ClientConfig(
        name="test",
        workspace_path=workspace,
        worktree_base=tmp_path / "wt",
    )
    wt = create_worktree(client, _REUSE_BRANCH)
    (wt / "tracked.txt").write_text("v1\n", encoding="utf-8")
    git_in(wt, "add", "tracked.txt")
    git_in(wt, "commit", "-m", "add tracked")
    git_in(wt, "push", "origin", _REUSE_BRANCH)
    return client, wt, origin, workspace


def _cw_worktree_records(
    caplog: pytest.LogCaptureFixture, min_level: int
) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name.startswith("cw.worktree.") and r.levelno >= min_level
    ]


def _spy_fetch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wrap ``cw.worktree.fetch_feature_branch`` with a delegating recorder.

    Returns the list of branch names it was called with, so a test can prove
    the refresh did (or did not) touch the network.
    """
    real = fetch_feature_branch
    fetched: list[str] = []

    def spy(client: ClientConfig, branch_name: str) -> FetchResult:
        fetched.append(branch_name)
        return real(client, branch_name)

    patch_worktree(monkeypatch, "fetch_feature_branch", spy)
    return fetched


def _spy_git(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[str, ...], Path]]:
    """Wrap ``cw.worktree._run_git`` with a delegating recorder of ``(args, cwd)``."""
    calls: list[tuple[tuple[str, ...], Path]] = []

    def spy(
        *args: str, cwd: Path, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd))
        return _run_git(*args, cwd=cwd, check=check)

    patch_worktree(monkeypatch, "_run_git", spy)
    return calls


def _seed_session(
    workspace: Path, worktree: Path | None, status: SessionStatus
) -> None:
    """Persist one session homed on *worktree* into the (tmp) cw state."""
    save_state(
        CwState(
            sessions=[
                Session(
                    name="test/impl",
                    client="test",
                    purpose=SessionPurpose.IMPL,
                    status=status,
                    origin=SessionOrigin.DAEMON,
                    workspace_path=workspace,
                    worktree_path=worktree,
                )
            ]
        )
    )


def _force_push_rewrite(origin: Path, work_dir: Path, branch: str) -> str:
    """Replace ``origin/<branch>``'s history with a fresh commit off ``main``.

    Simulates a rewritten remote (force-push) so the worktree's pushed commit
    is no longer an ancestor of the remote tip. Returns the new tip SHA.
    """
    git_in(work_dir.parent, "clone", str(origin), str(work_dir))
    git_in(work_dir, "checkout", "-B", branch, "origin/main")
    (work_dir / "rewritten.txt").write_text("rewritten\n", encoding="utf-8")
    git_in(work_dir, "add", "rewritten.txt")
    git_in(
        work_dir,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=cw test",
        "commit",
        "-m",
        "rewritten history",
    )
    git_in(work_dir, "push", "--force", "origin", branch)
    return git_in(work_dir, "rev-parse", "HEAD")


def _session_at(name: str, status: SessionStatus, wt: Path | None) -> Session:
    return Session(
        name=name,
        client="c",
        purpose=SessionPurpose.IMPL,
        status=status,
        origin=SessionOrigin.DAEMON,
        workspace_path=Path("/repo"),
        worktree_path=wt,
    )


class TestLiveSessionWorktreePaths:
    """The session-state half of the live-path guard, shared by the worktree GC
    and the create_worktree reuse refresh (#2213)."""

    @pytest.mark.parametrize(
        "status",
        [SessionStatus.ACTIVE, SessionStatus.IDLE, SessionStatus.BACKGROUNDED],
    )
    def test_non_terminal_session_path_included(
        self, monkeypatch: pytest.MonkeyPatch, status: SessionStatus
    ) -> None:
        live = Path("/live/wt")
        state = CwState(sessions=[_session_at("c/impl", status, live)])
        monkeypatch.setattr("cw.worktree._refresh.load_state", lambda: state)

        assert live_session_worktree_paths() == frozenset({live})

    def test_terminal_and_pathless_sessions_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = CwState(
            sessions=[
                _session_at("c/done", SessionStatus.COMPLETED, Path("/done/wt")),
                _session_at("c/nopath", SessionStatus.ACTIVE, None),
            ]
        )
        monkeypatch.setattr("cw.worktree._refresh.load_state", lambda: state)

        assert live_session_worktree_paths() == frozenset()

    def test_state_load_failure_returns_none_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom() -> CwState:
            msg = "corrupt"
            raise ValueError(msg)

        monkeypatch.setattr("cw.worktree._refresh.load_state", _boom)

        with caplog.at_level("WARNING", logger="cw.worktree"):
            paths = live_session_worktree_paths()

        assert paths is None
        assert any(
            "failed to load session state" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.parametrize("kind", ["oserror", "invalid-json", "validation-error"])
    def test_expected_state_read_failures_return_none(
        self,
        caplog: pytest.LogCaptureFixture,
        kind: str,
    ) -> None:
        """The three failure families a state read can really raise -- I/O
        (a directory where the file should be), a JSON syntax error, and a
        pydantic ValidationError -- all degrade to None, via the real
        ``load_state`` and a real file on disk."""
        path = state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "oserror":
            path.mkdir()
        elif kind == "invalid-json":
            path.write_text("{not json", encoding="utf-8")
        else:
            path.write_text(
                json.dumps({"sessions": [{"name": "c/impl"}]}), encoding="utf-8"
            )

        with caplog.at_level("WARNING", logger="cw.worktree"):
            assert live_session_worktree_paths() is None

        assert any(
            "failed to load session state" in r.getMessage() for r in caplog.records
        )

    def test_unexpected_exception_type_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the enumerated read failures degrade to None; anything else is a
        bug that must surface rather than read as "no live sessions" (#2213)."""

        def _boom() -> CwState:
            msg = "not a state-read failure"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.worktree._refresh.load_state", _boom)

        with pytest.raises(RuntimeError, match="not a state-read failure"):
            live_session_worktree_paths()


def _write_corrupt_state() -> Path:
    """Put a syntactically invalid ``sessions.json`` on disk, for real."""
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    return path


class TestUnreadableStatePostures:
    """One shared helper, two deliberately opposite failure postures (#2213).

    The SAME unreadable ``sessions.json`` makes the reuse refresh fail CLOSED
    (a mutation must not proceed when a live session cannot be ruled out) and
    leaves the worktree GC failing OPEN (a corrupt state file must never block
    garbage collection, unchanged from before #2213). Neither is a bug in the
    other's terms; do not "fix" one into consistency with the other.
    """

    def test_reuse_refresh_fails_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")
        fetched = _spy_fetch(monkeypatch)
        # Written only after seeding, which itself reads and writes cw state.
        _write_corrupt_state()

        caplog.clear()  # drop seed-phase records
        with (
            caplog.at_level(logging.DEBUG, logger="cw.worktree"),
            pytest.raises(WorktreeOccupiedError) as excinfo,
        ):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert excinfo.value.path == wt
        assert "unreadable" in excinfo.value.reason
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any(
            r.levelno == logging.DEBUG and "unreadable" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.DEBUG)
        )

    def test_worktree_gc_fails_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write_corrupt_state()
        monkeypatch.setattr(
            "cw.worktree_gc.load_dev_queue", lambda: MagicMock(tasks=[])
        )

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            paths = _live_worktree_paths()

        assert paths == frozenset()
        assert any(
            "failed to load session state" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )


class TestCreateWorktreeReuseRefresh:
    """#2213: with ``refresh_on_reuse=True`` the reuse path best-effort fetches
    and fast-forwards a *behind, unoccupied, clean* worktree. It never resets,
    never raises, and leaves an occupied or diverged worktree exactly as it is."""

    def test_default_reuse_does_not_fetch_or_move(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")
        fetched = _spy_fetch(monkeypatch)

        result = create_worktree(client, _REUSE_BRANCH, allow_dirty_reuse=True)

        assert result == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert (
            git_in(workspace, "rev-parse", f"refs/remotes/origin/{_REUSE_BRANCH}")
            == old_sha
        )

    @pytest.mark.parametrize("allow_dirty_reuse", [False, True])
    def test_behind_worktree_fast_forwarded_on_reuse(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
        allow_dirty_reuse: bool,
    ) -> None:
        client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        new_sha = push_commit_to_origin(
            origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt"
        )
        assert new_sha != old_sha
        # Precondition: both the worktree and the workspace's tracking ref are
        # stale, so only a fetch plus fast-forward can bring HEAD to new_sha.
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert (
            git_in(workspace, "rev-parse", f"refs/remotes/origin/{_REUSE_BRANCH}")
            == old_sha
        )

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.INFO, logger="cw.worktree"):
            result = create_worktree(
                client,
                _REUSE_BRANCH,
                allow_dirty_reuse=allow_dirty_reuse,
                refresh_on_reuse=True,
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == new_sha
        assert git_in(workspace, "rev-parse", f"refs/heads/{_REUSE_BRANCH}") == new_sha
        assert (wt / "upstream.txt").exists()
        assert any(
            r.levelno == logging.INFO and "fast-forwarded" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.INFO)
        )

    @pytest.mark.parametrize(
        ("local_file", "local_content", "upstream_file", "upstream_content"),
        [
            pytest.param(
                "tracked.txt",
                "local churn\n",
                "upstream.txt",
                "out-of-band\n",
                id="uncommitted-nonoverlapping",
            ),
            pytest.param(
                "tracked.txt",
                "local edit\n",
                "tracked.txt",
                "upstream edit\n",
                id="uncommitted-overlapping",
            ),
            pytest.param(
                "scratch.txt",
                "untracked\n",
                "upstream.txt",
                "out-of-band\n",
                id="untracked",
            ),
        ],
    )
    def test_uncommitted_work_is_not_fast_forwarded(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        local_file: str,
        local_content: str,
        upstream_file: str,
        upstream_content: str,
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        (wt / local_file).write_text(local_content, encoding="utf-8")
        push_commit_to_origin(
            origin,
            _REUSE_BRANCH,
            tmp_path / "side",
            upstream_file,
            content=upstream_content,
        )
        fetched = _spy_fetch(monkeypatch)

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
            result = create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        # The occupancy check is pre-network: no fetch, HEAD and edit untouched.
        assert result == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert (wt / local_file).read_text(encoding="utf-8") == local_content
        assert _cw_worktree_records(caplog, logging.WARNING) == []
        assert any(
            r.levelno == logging.DEBUG
            and str(wt) in r.getMessage()
            and "uncommitted" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.DEBUG)
        )

    @pytest.mark.parametrize("remote_moved", [False, True])
    def test_unpushed_local_commit_is_not_moved(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        remote_moved: bool,
    ) -> None:
        """Ahead of origin (remote_moved False) or diverged from it
        (remote_moved True, an unpushed commit plus a different remote one):
        either way the unpushed commit marks the worktree occupied."""
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        git_in(wt, "commit", "--allow-empty", "-m", "unpushed local work")
        local_sha = git_in(wt, "rev-parse", "HEAD")
        if remote_moved:
            push_commit_to_origin(
                origin, _REUSE_BRANCH, tmp_path / "side", "theirs.txt"
            )
        fetched = _spy_fetch(monkeypatch)

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
            result = create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert result == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == local_sha
        assert _cw_worktree_records(caplog, logging.WARNING) == []
        assert any(
            r.levelno == logging.DEBUG and "not on" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.DEBUG)
        )

    @pytest.mark.parametrize(
        ("status", "homed_here", "expect_moved"),
        [
            pytest.param(SessionStatus.ACTIVE, True, False, id="active"),
            pytest.param(SessionStatus.IDLE, True, False, id="idle"),
            pytest.param(SessionStatus.BACKGROUNDED, True, False, id="backgrounded"),
            pytest.param(SessionStatus.COMPLETED, True, True, id="terminal-completed"),
            pytest.param(SessionStatus.ACTIVE, False, True, id="active-elsewhere"),
        ],
    )
    def test_live_session_homed_on_worktree_blocks_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        status: SessionStatus,
        homed_here: bool,
        expect_moved: bool,
    ) -> None:
        client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        new_sha = push_commit_to_origin(
            origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt"
        )
        _seed_session(workspace, wt if homed_here else tmp_path / "elsewhere", status)
        fetched = _spy_fetch(monkeypatch)

        caplog.clear()  # drop seed-phase records
        if expect_moved:
            with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
                result = create_worktree(
                    client,
                    _REUSE_BRANCH,
                    allow_dirty_reuse=True,
                    refresh_on_reuse=True,
                )
            assert result == wt
            assert git_in(wt, "rev-parse", "HEAD") == new_sha
            return
        with (
            caplog.at_level(logging.DEBUG, logger="cw.worktree"),
            pytest.raises(WorktreeOccupiedError) as excinfo,
        ):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert excinfo.value.path == wt
        assert "live session" in excinfo.value.reason
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _cw_worktree_records(caplog, logging.WARNING) == []
        assert any(
            r.levelno == logging.DEBUG
            and str(wt) in r.getMessage()
            and "live session" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.DEBUG)
        )

    def test_unreadable_session_state_fails_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A mutation must not proceed when a live session cannot be ruled out."""
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")
        # ``cw.worktree`` imports the function lazily (import cycle), so the
        # patch target is its home module.
        monkeypatch.setattr(
            "cw.worktree._refresh.live_session_worktree_paths", lambda: None
        )
        fetched = _spy_fetch(monkeypatch)

        caplog.clear()  # drop seed-phase records
        with (
            caplog.at_level(logging.DEBUG, logger="cw.worktree"),
            pytest.raises(WorktreeOccupiedError) as excinfo,
        ):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert excinfo.value.path == wt
        assert "unreadable" in excinfo.value.reason
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any(
            r.levelno == logging.DEBUG and "unreadable" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.DEBUG)
        )

    def test_diverged_after_remote_rewrite_left_alone_and_warns(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The worktree pushed commit A, then the remote history was rewritten
        to B. The stale tracking ref still equals HEAD, so occupancy passes;
        the fetch reveals the divergence and nothing is moved."""
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        new_tip = _force_push_rewrite(origin, tmp_path / "side", _REUSE_BRANCH)
        assert new_tip != old_sha

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.INFO, logger="cw.worktree"):
            result = create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any(
            "diverged" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )

    def test_merge_refusal_is_logged_and_leaves_worktree_alone(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``--ff-only`` can still refuse after the occupancy check passed (the
        tree was dirtied in between); the refusal degrades to as-is."""
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                return subprocess.CompletedProcess(
                    args, 1, "", "error: local changes would be overwritten\n"
                )
            return _run_git(*args, cwd=cwd, check=check)

        patch_worktree(monkeypatch, "_run_git", spy)

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.INFO, logger="cw.worktree"):
            result = create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any(
            "refused" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )

    def test_wrong_branch_still_raises_before_any_refresh(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The branch-identity guard (a StaleWorktreeError, stricter than
        use-as-is) fires before the refresh: no fetch, worktree untouched."""
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        git_in(wt, "checkout", "-b", "dev/other")
        fetched = _spy_fetch(monkeypatch)

        with pytest.raises(StaleWorktreeError):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert fetched == []
        assert git_in(wt, "branch", "--show-current") == "dev/other"

    def test_missing_path_creates_fresh_with_refresh_flag(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        client, _wt, _origin, workspace = _seed_reuse(tmp_path, make_git_repo)

        created = create_worktree(client, "dev/fresh", refresh_on_reuse=True)

        assert created.exists()
        assert git_in(created, "branch", "--show-current") == "dev/fresh"
        assert git_in(created, "rev-parse", "HEAD") == git_in(
            workspace, "rev-parse", "main"
        )

    def test_branch_not_on_origin_is_silent(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The quiet marker is git's English message; pin the locale.
        monkeypatch.setenv("LC_ALL", "C")
        client, _wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        never_pushed = "dev/never-pushed"
        wt = create_worktree(client, never_pushed)
        head = git_in(wt, "rev-parse", "HEAD")

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
            result = create_worktree(
                client, never_pushed, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == head
        assert _cw_worktree_records(caplog, logging.WARNING) == []

    def test_no_origin_remote_degrades(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        workspace = make_git_repo("workspace")
        client = ClientConfig(
            name="test",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
        )
        wt = create_worktree(client, _REUSE_BRANCH)
        head = git_in(wt, "rev-parse", "HEAD")

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.INFO, logger="cw.worktree"):
            result = create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == head
        assert _cw_worktree_records(caplog, logging.WARNING)

    def test_fetch_failure_skips_fast_forward_from_stale_tracking_ref(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed fetch leaves the tracking ref at whatever it was before, so
        a fast-forward "to origin" would really be a move to stale state. It is
        skipped entirely: HEAD untouched, the reason reported, nothing raised."""
        client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        new_sha = push_commit_to_origin(
            origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt"
        )
        # Another fetch (e.g. dispatch's freshness gate) advances the shared
        # tracking ref while the worktree's branch stays behind ...
        git_in(workspace, "fetch", "origin")
        tracking = f"refs/remotes/origin/{_REUSE_BRANCH}"
        assert git_in(workspace, "rev-parse", tracking) == new_sha
        assert old_sha != new_sha
        # ... then origin becomes unreachable, so the fetch inside reuse fails.
        origin.rename(tmp_path / "origin-gone.git")
        fetched = _spy_fetch(monkeypatch)

        result = _refresh_with_debug(client, caplog)

        assert result == wt
        assert fetched == [_REUSE_BRANCH]
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert not (wt / "upstream.txt").exists()
        # The reason is reported: fetch's own WARNING carries rc + git's stderr,
        # and the refresh names the skip.
        assert any(
            "fetch failed" in r.getMessage() and "rc=" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )
        assert any(
            "fast-forward skipped" in m and str(wt) in m for m in _debug_reasons(caplog)
        )

    def test_simulated_fetch_failure_leaves_head_and_reports_reason(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, workspace, old_sha, new_sha = _seed_behind(tmp_path, make_git_repo)
        # A stale-but-newer tracking ref is what a real failure would leave.
        git_in(workspace, "fetch", "origin")
        merges: list[tuple[str, ...]] = []

        def failing_fetch(client: ClientConfig, branch_name: str) -> FetchResult:
            return FetchResult(FetchOutcome.FAILED, "rc=128: fatal: simulated")

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                merges.append(args)
            return _run_git(*args, cwd=cwd, check=check)

        patch_worktree(monkeypatch, "fetch_feature_branch", failing_fetch)
        patch_worktree(monkeypatch, "_run_git", spy)

        result = _refresh_with_debug(client, caplog)

        assert result == wt
        assert merges == []  # skipped entirely, not merely refused
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert old_sha != new_sha
        # The log line names git's reason, not just that the fetch failed.
        assert any(
            "fast-forward skipped" in m and "fatal: simulated" in m
            for m in _debug_reasons(caplog)
        )

    def test_fetch_ok_but_tracking_ref_absent_is_a_no_op(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fetch can succeed without creating ``refs/remotes/origin/<branch>``
        (a narrow ``remote.origin.fetch`` refspec): no target, nothing to move."""
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        head = git_in(wt, "rev-parse", "HEAD")
        merges: list[tuple[str, ...]] = []

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                merges.append(args)
            return _run_git(*args, cwd=cwd, check=check)

        real_ref_exists = _ref_exists

        def no_tracking_ref(ref: str, git_cwd: Path) -> bool:
            if ref == f"refs/remotes/origin/{_REUSE_BRANCH}":
                return False
            return real_ref_exists(ref, git_cwd)

        patch_worktree(monkeypatch, "_run_git", spy)
        patch_worktree(monkeypatch, "_ref_exists", no_tracking_ref)
        patch_worktree(
            monkeypatch,
            "fetch_feature_branch",
            lambda *_args, **_kw: FetchResult(FetchOutcome.FETCHED),
        )
        monkeypatch.setattr(
            "cw.worktree._refresh._reuse_occupancy",
            lambda *_args, **_kw: _Occupancy(
                live=None, branch_mismatch=None, local=None
            ),
        )

        result = _refresh_reused_worktree(
            client,
            _REUSE_BRANCH,
            wt,
            ReuseRefreshReport(),
            ticket_id=None,
            daemon=native_daemon.get_native_daemon_client(),
        )

        assert merges == []
        assert git_in(wt, "rev-parse", "HEAD") == head
        assert result.outcome is RefreshOutcome.NOT_REFRESHED
        assert "origin/" in result.reason

    def test_no_notes_on_a_clean_refresh(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)

        _path, report = _refresh_reporting(client)

        assert git_in(wt, "rev-parse", "HEAD") == new_sha
        assert report.notes == []
        assert report.outcome is RefreshOutcome.REFRESHED
        assert report.reason is not None

    @pytest.mark.parametrize(
        ("fetch", "moves", "note_count", "expected"),
        [
            pytest.param(
                FetchResult(FetchOutcome.FETCHED),
                True,
                0,
                RefreshOutcome.REFRESHED,
                id="fetched",
            ),
            pytest.param(
                FetchResult(FetchOutcome.BRANCH_ABSENT, "couldn't find remote ref"),
                False,
                0,
                RefreshOutcome.NOT_REFRESHED,
                id="branch-absent",
            ),
            pytest.param(
                FetchResult(FetchOutcome.FAILED, "rc=128: fatal: no route"),
                False,
                1,
                RefreshOutcome.NOT_REFRESHED,
                id="failed",
            ),
        ],
    )
    def test_fast_forward_only_on_a_fetched_outcome(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        fetch: FetchResult,
        moves: bool,
        note_count: int,
        expected: RefreshOutcome,
    ) -> None:
        """The three fetch outcomes are handled apart (#2213 round 4): only
        ``FETCHED`` fast-forwards; ``BRANCH_ABSENT`` proceeds without a refresh
        and is NOT friction; ``FAILED`` skips the fast-forward and IS reported.

        The tracking ref is already fresh in every case, so an implementation
        that fast-forwarded regardless of the outcome would visibly move HEAD."""
        client, wt, workspace, old_sha, new_sha = _seed_behind(tmp_path, make_git_repo)
        git_in(workspace, "fetch", "origin")
        merges: list[tuple[str, ...]] = []

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                merges.append(args)
            return _run_git(*args, cwd=cwd, check=check)

        patch_worktree(monkeypatch, "_run_git", spy)
        patch_worktree(monkeypatch, "fetch_feature_branch", lambda _c, _b: fetch)

        _path, report = _refresh_reporting(client)

        assert git_in(wt, "rev-parse", "HEAD") == (new_sha if moves else old_sha)
        assert len(merges) == (1 if moves else 0)
        assert len(report.notes) == note_count
        assert report.outcome is expected
        assert report.reason
        if fetch.outcome is FetchOutcome.FAILED:
            # The friction note says WHY the fetch failed, not only that it did.
            assert fetch.reason is not None
            assert fetch.reason in report.notes[0]
            assert fetch.reason in report.reason

    def test_real_missing_branch_is_branch_absent_not_a_failure(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Against real git: a never-pushed branch is ``BRANCH_ABSENT`` (no
        friction note), where a genuinely failed fetch is ``FAILED``."""
        monkeypatch.setenv("LC_ALL", "C")  # the marker is git's English message
        client, _wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        never_pushed = "dev/never-pushed"
        wt = create_worktree(client, never_pushed)
        report = ReuseRefreshReport()

        create_worktree(
            client,
            never_pushed,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
        )

        fetched = fetch_feature_branch(client, never_pushed)
        assert fetched.outcome is FetchOutcome.BRANCH_ABSENT
        assert report.notes == []
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert wt.exists()

    def test_fetch_failure_is_reported_in_one_note(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A REAL failed fetch (origin unreachable): one note, naming the
        worktree AND git's own reason, and HEAD untouched. The outcome is
        ``NOT_REFRESHED`` (the tree is the caller's), never a raise."""
        monkeypatch.setenv("LC_ALL", "C")  # the reason text is git's English
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        origin.rename(tmp_path / "origin-gone.git")

        result, report = _refresh_reporting(client)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert len(report.notes) == 1
        assert str(wt) in report.notes[0]
        assert f"origin/{_REUSE_BRANCH}" in report.notes[0]
        assert "fetch" in report.notes[0]
        assert "does not appear to be a git repository" in report.notes[0]
        assert "\n" not in report.notes[0]
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "does not appear to be a git repository" in report.reason

    def test_diverged_branch_is_reported_in_one_note(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        _force_push_rewrite(origin, tmp_path / "side", _REUSE_BRANCH)

        result, report = _refresh_reporting(client)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert len(report.notes) == 1
        assert str(wt) in report.notes[0]
        assert "diverged" in report.notes[0]
        assert f"origin/{_REUSE_BRANCH}" in report.notes[0]
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "diverged" in report.reason

    def test_git_refusing_the_fast_forward_is_reported_in_one_note(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                return subprocess.CompletedProcess(
                    args, 1, "", "error: local changes would be overwritten\nmore\n"
                )
            return _run_git(*args, cwd=cwd, check=check)

        patch_worktree(monkeypatch, "_run_git", spy)

        result, report = _refresh_reporting(client)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert len(report.notes) == 1
        assert str(wt) in report.notes[0]
        assert "local changes would be overwritten" in report.notes[0]
        assert "\n" not in report.notes[0]
        assert report.outcome is RefreshOutcome.NOT_REFRESHED

    def test_os_error_is_reported_in_one_note(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A missing git binary (OSError) out of the refresh never escapes
        ``create_worktree``, and is reported. Injected at
        ``fetch_feature_branch`` itself: ``_fetch_default_branch`` already
        swallows ``FileNotFoundError`` internally, so a fetch-level ``_run_git``
        fault would never reach the helper's own ``except OSError``."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-oserror"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            stdout = "feat/oserror\n" if "--show-current" in args else ""
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        def boom(client: ClientConfig, branch_name: str) -> FetchResult:
            msg = "git vanished"
            raise FileNotFoundError(msg)

        def clean(
            client: ClientConfig, branch: str, *, wt_path: Path | None = None
        ) -> None:
            return None

        patch_worktree(monkeypatch, "_run_git", mock_run)
        patch_worktree(monkeypatch, "unsaved_work_reason", clean)
        patch_worktree(monkeypatch, "fetch_feature_branch", boom)
        report = ReuseRefreshReport()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            result = create_worktree(
                client,
                "feat/oserror",
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                refresh_report=report,
            )

        assert result == wt_path
        assert any(
            "refresh of reused worktree failed" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )
        assert len(report.notes) == 1
        assert str(wt_path) in report.notes[0]
        assert "git vanished" in report.notes[0]
        assert "OS error" in report.notes[0]
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "git vanished" in report.reason

    def test_os_error_after_the_fast_forward_step_started_is_one_note(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An OSError from the merge itself (not the fetch) is reported too,
        still as exactly one note."""
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                msg = "git vanished mid-merge"
                raise FileNotFoundError(msg)
            return _run_git(*args, cwd=cwd, check=check)

        patch_worktree(monkeypatch, "_run_git", spy)

        result, report = _refresh_reporting(client)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert len(report.notes) == 1
        assert str(wt) in report.notes[0]
        assert "git vanished mid-merge" in report.notes[0]

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_live_occupant_raises_and_nothing_is_fetched(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        source: str,
    ) -> None:
        """Round 5: occupied is a refusal a caller CANNOT ignore. It raises
        ``WorktreeOccupiedError`` (no usable path is returned), adds no friction
        note (it is the design working, not a failure), and never fetches."""
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        _occupy(source, workspace, wt)
        fetched = _spy_fetch(monkeypatch)

        error, report = _refresh_occupied(client)

        assert error.path == wt
        assert "live" in error.reason
        assert str(wt) in str(error)
        assert error.reason in str(error)
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert report.notes == []
        assert report.outcome is RefreshOutcome.OCCUPIED_BY_LIVE_SESSION
        assert report.reason == error.reason

    def test_unsaved_work_alone_is_not_occupied_by_a_live_session(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """Unsaved work refuses the refresh but leaves the worktree the
        caller's to use: ``NOT_REFRESHED`` (proceed), never a raise."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        (wt / "scratch.txt").write_text("churn\n", encoding="utf-8")

        path, report = _refresh_reporting(client)

        assert path == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "unsaved work" in report.reason
        assert report.notes == []

    def test_unsaved_work_does_not_mask_a_live_occupant(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """Unsaved work is exactly what a live worker leaves behind. It refuses
        the refresh first, but the live half is always evaluated."""
        client, wt, workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)
        (wt / "scratch.txt").write_text("worker output\n", encoding="utf-8")
        _occupy("roster", workspace, wt)

        error, _report = _refresh_occupied(client)

        assert "live daemon worker" in error.reason

    @pytest.mark.parametrize("kind", ["roster-invalid-json", "state-corrupt"])
    def test_unreadable_source_is_occupied_fail_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        kind: str,
    ) -> None:
        client, _wt, _workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)
        if kind == "roster-invalid-json":
            _seed_roster(raw="{not json")
        else:
            state_file().write_text("{not json", encoding="utf-8")

        error, report = _refresh_occupied(client)

        assert "unreadable" in error.reason
        assert report.outcome is RefreshOutcome.OCCUPIED_BY_LIVE_SESSION

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_occupant_appearing_during_the_fetch_raises(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        source: str,
    ) -> None:
        """The re-check before the merge refuses a live occupant too, so a
        caller is not told "free" by the first gate and then racing the second."""
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        real_fetch = fetch_feature_branch

        def fetch_then_occupy(client: ClientConfig, branch_name: str) -> FetchResult:
            result = real_fetch(client, branch_name)
            _occupy(source, workspace, wt)
            return result

        patch_worktree(monkeypatch, "fetch_feature_branch", fetch_then_occupy)

        error, report = _refresh_occupied(client)

        assert error.path == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert report.notes == []
        assert report.outcome is RefreshOutcome.OCCUPIED_BY_LIVE_SESSION

    def test_occupied_error_is_a_worktree_error_but_not_a_stale_one(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """It IS a ``WorktreeError`` (so a broad handler still contains it), but
        a caller matching ``StaleWorktreeError`` -- the branch that removes the
        worktree -- must NOT see it."""
        client, wt, workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)
        _occupy("roster", workspace, wt)

        with pytest.raises(WorktreeError) as excinfo:
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert isinstance(excinfo.value, WorktreeOccupiedError)
        assert not isinstance(excinfo.value, StaleWorktreeError)
        assert wt.exists()

    def test_default_reuse_ignores_a_live_occupant(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """``refresh_on_reuse=False`` is unchanged: path resolution only, so it
        neither consults occupancy nor raises (``cw start`` does not opt in)."""
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        _occupy("roster", workspace, wt)

        result = create_worktree(client, _REUSE_BRANCH, allow_dirty_reuse=True)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha

    def test_not_refreshed_dirty_returns_a_path(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        (wt / "tracked.txt").write_text("local edit\n", encoding="utf-8")

        path, report = _refresh_reporting(client)

        assert path == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert report.outcome is RefreshOutcome.NOT_REFRESHED

    def test_not_refreshed_branch_absent_returns_a_path(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LC_ALL", "C")
        client, _wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        never_pushed = "dev/never-pushed"
        wt = create_worktree(client, never_pushed)
        report = ReuseRefreshReport()

        path = create_worktree(
            client,
            never_pushed,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
        )

        assert path == wt
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "origin/dev/never-pushed" in report.reason
        assert report.notes == []

    def test_refreshed_reports_the_move(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _workspace, old_sha, new_sha = _seed_behind(tmp_path, make_git_repo)

        path, report = _refresh_reporting(client)

        assert path == wt
        assert git_in(wt, "rev-parse", "HEAD") == new_sha
        assert report.outcome is RefreshOutcome.REFRESHED
        assert report.reason is not None
        assert old_sha[:12] in report.reason
        assert new_sha[:12] in report.reason

    def test_equal_to_origin_is_not_refreshed_without_a_note(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        head = git_in(wt, "rev-parse", "HEAD")

        path, report = _refresh_reporting(client)

        assert path == wt
        assert git_in(wt, "rev-parse", "HEAD") == head
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "up to date" in report.reason
        assert report.notes == []

    def test_unknown_fetch_outcome_is_never_read_as_fetched(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 5 item 3: an outcome the refresh does not know is a bug, not
        "fetched". The tracking ref is fresh here, so a fall-through to "fetched"
        would visibly fast-forward HEAD; an exhaustive ``match`` hits
        ``assert_never`` (``AssertionError``) instead and HEAD stays put. The
        stand-in is a plain string: no real enum member is added."""
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        git_in(workspace, "fetch", "origin")
        patch_worktree(
            monkeypatch,
            "fetch_feature_branch",
            lambda _c, _b: FetchResult("not-a-real-outcome"),
        )

        with pytest.raises(AssertionError):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert git_in(wt, "rev-parse", "HEAD") == old_sha

    def test_unknown_refresh_outcome_never_yields_a_path(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``create_worktree`` dispatches on the refresh outcome exhaustively: an
        outcome it does not know must not be read as "proceed with this tree"."""
        client, _wt, _workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)
        patch_worktree(
            monkeypatch,
            "_refresh_reused_worktree",
            lambda *_a, **_kw: RefreshResult("not-a-real-outcome", "stand-in"),
        )

        with pytest.raises(AssertionError):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

    def test_unknown_ff_relation_never_fast_forwards(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The HEAD-vs-origin classification is matched exhaustively too: a
        relation the refresh does not know is a bug, never "behind"."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        patch_worktree(monkeypatch, "_ff_relation", lambda *_a, **_kw: "sideways")

        with pytest.raises(AssertionError):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert git_in(wt, "rev-parse", "HEAD") == old_sha

    def test_is_main_behind_origin_rejects_an_unknown_fetch_outcome(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = ClientConfig(name="test", workspace_path=tmp_path)
        monkeypatch.setattr(
            "cw.worktree._freshness._fetch_default_branch",
            lambda *_a, **_kw: FetchResult("not-a-real-outcome"),
        )

        with pytest.raises(AssertionError):
            is_main_behind_origin(client)

    def test_refresh_report_is_optional(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        """A caller with no surface (the dispatch claim) passes none."""
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)

        create_worktree(
            client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
        )

        assert git_in(wt, "rev-parse", "HEAD") == new_sha


def _seed_roster(cwd: Path | None = None, *, raw: str | None = None) -> Path:
    """Write the (tmp-isolated) daemon roster.

    *cwd* records one live worker homed there; *raw* writes arbitrary bytes
    instead. ``tests/conftest.py`` points ``native_daemon._ROSTER_PATH`` at a
    tmp path, so this never touches the developer's real roster.
    """
    roster = native_daemon._ROSTER_PATH
    roster.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        raw
        if raw is not None
        else json.dumps({"workers": {"aaaa1111": {"pid": 1, "cwd": str(cwd)}}})
    )
    roster.write_text(payload, encoding="utf-8")
    return roster


def _unnormalizable_path(
    kind: str, base: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Return a path under *base* whose ``stat()`` fails with the given errno."""
    if kind == "eloop":
        return _symlink_loop(base)
    if kind == "enotdir":
        blocker = base / "a-file"
        blocker.write_text("not a directory\n", encoding="utf-8")
        return blocker / "child"
    if kind == "enametoolong":
        return base / ("x" * 300)
    denied = base / "denied"
    denied.mkdir()
    real_stat = Path.stat

    def stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == denied:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    return denied


def _occupy(source: str, workspace: Path, path: Path) -> None:
    """Make *path* look occupied via cw state or via the daemon roster."""
    if source == "state":
        _seed_session(workspace, path, SessionStatus.ACTIVE)
    else:
        _seed_roster(path)


def _seed_behind(
    tmp_path: Path, make_git_repo: Callable[..., Path]
) -> tuple[ClientConfig, Path, Path, str, str]:
    """``_seed_reuse`` plus an upstream commit.

    Returns ``(client, wt, workspace, old_sha, new_sha)``.
    """
    client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
    old_sha = git_in(wt, "rev-parse", "HEAD")
    new_sha = push_commit_to_origin(
        origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt"
    )
    return client, wt, workspace, old_sha, new_sha


def _refresh_with_debug(client: ClientConfig, caplog: pytest.LogCaptureFixture) -> Path:
    caplog.clear()  # drop seed-phase records
    with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
        return create_worktree(
            client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
        )


def _refresh_reporting(client: ClientConfig) -> tuple[Path, ReuseRefreshReport]:
    """Reuse ``_REUSE_BRANCH`` with the refresh on, returning what it reported."""
    report = ReuseRefreshReport()
    path = create_worktree(
        client,
        _REUSE_BRANCH,
        allow_dirty_reuse=True,
        refresh_on_reuse=True,
        refresh_report=report,
    )
    return path, report


def _refresh_occupied(
    client: ClientConfig,
) -> tuple[WorktreeOccupiedError, ReuseRefreshReport]:
    """Reuse ``_REUSE_BRANCH`` with the refresh on, expecting the occupancy refusal.

    Returns the raised error and the report the caller passed in (which the
    refresh filled in before raising).
    """
    report = ReuseRefreshReport()
    with pytest.raises(WorktreeOccupiedError) as excinfo:
        create_worktree(
            client,
            _REUSE_BRANCH,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
        )
    return excinfo.value, report


def _refresh_occupied_with_debug(
    client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> WorktreeOccupiedError:
    """Like :func:`_refresh_occupied`, with DEBUG capture for the reason log."""
    caplog.clear()  # drop seed-phase records
    with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
        error, _report = _refresh_occupied(client)
    return error


def _refresh_stale_with_debug(
    client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> StaleWorktreeError:
    """Reuse ``_REUSE_BRANCH`` with the refresh on, expecting the pre-merge
    re-check's branch-mismatch refusal (:exc:`StaleWorktreeError`, not the
    occupancy path -- see :func:`_refresh_occupied_with_debug`)."""
    caplog.clear()  # drop seed-phase records
    with (
        caplog.at_level(logging.DEBUG, logger="cw.worktree"),
        pytest.raises(StaleWorktreeError) as excinfo,
    ):
        create_worktree(
            client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
        )
    return excinfo.value


def _debug_reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in _cw_worktree_records(caplog, logging.DEBUG)
        if r.levelno == logging.DEBUG
    ]


class TestReuseOccupancyRosterAndPaths:
    """#2213 round 2: the occupancy predicate consults the daemon roster as well
    as cw state, compares *resolved* paths, and fails closed on anything it
    cannot read."""

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_session_path_recorded_via_symlink_is_occupied(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
        source: str,
    ) -> None:
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        alias = tmp_path / "wt-alias"
        alias.symlink_to(wt, target_is_directory=True)
        assert alias != wt
        _occupy(source, workspace, alias)

        error = _refresh_occupied_with_debug(client, caplog)

        assert error.path == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any("live" in m and str(wt) in m for m in _debug_reasons(caplog))

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_worktree_reached_via_symlink_is_occupied(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
        source: str,
    ) -> None:
        """The reverse direction: the session/worker recorded the real path,
        but the caller reaches the worktree through a symlinked worktree base."""
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        alias_base = tmp_path / "wt-alias"
        alias_base.symlink_to(tmp_path / "wt", target_is_directory=True)
        aliased_client = client.model_copy(update={"worktree_base": alias_base})
        _occupy(source, workspace, wt)

        error = _refresh_occupied_with_debug(aliased_client, caplog)

        assert error.path != wt
        assert error.path.resolve() == wt.resolve()
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any("live" in m for m in _debug_reasons(caplog))

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_symlink_to_a_different_worktree_is_not_occupied(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
        source: str,
    ) -> None:
        """Control for the symlink tests: normalization must not over-match."""
        client, wt, workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)
        other = tmp_path / "other-dir"
        other.mkdir()
        alias = tmp_path / "other-alias"
        alias.symlink_to(other, target_is_directory=True)
        _occupy(source, workspace, alias)

        _refresh_with_debug(client, caplog)

        assert git_in(wt, "rev-parse", "HEAD") == new_sha

    def test_injected_daemon_worker_blocks_the_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """#2213 round 7: occupancy consults the CALLER'S daemon, not the real
        one. A worker seeded directly on an injected ``FakeNativeDaemonClient``
        -- never written to the real (tmp-isolated) roster file -- must still
        block the fast-forward, and the real roster must stay untouched."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        assert not native_daemon._ROSTER_PATH.exists()
        fake = native_daemon.FakeNativeDaemonClient()
        fake.seed_live_worker(wt)

        with pytest.raises(WorktreeOccupiedError) as excinfo:
            create_worktree(
                client,
                _REUSE_BRANCH,
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                native_daemon=fake,
            )

        assert excinfo.value.path == wt
        assert "live daemon worker" in excinfo.value.reason
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        # The real roster was never written -- the fake, not it, was consulted.
        assert not native_daemon._ROSTER_PATH.exists()

    def test_injected_daemon_unreadable_roster_fails_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """#2213 round 7: a fake roster reporting unreadable (``None``) fails
        closed exactly like the real one, even though cw state and the real
        (tmp-isolated) roster file are both clean."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        fake = native_daemon.FakeNativeDaemonClient()
        fake.roster_unreadable = True

        with pytest.raises(WorktreeOccupiedError) as excinfo:
            create_worktree(
                client,
                _REUSE_BRANCH,
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                native_daemon=fake,
            )

        assert excinfo.value.path == wt
        assert "roster unreadable" in excinfo.value.reason
        assert git_in(wt, "rev-parse", "HEAD") == old_sha

    def test_live_daemon_worker_homed_on_worktree_blocks_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        _seed_roster(wt)  # live in the roster, absent from cw state
        fetched = _spy_fetch(monkeypatch)

        error = _refresh_occupied_with_debug(client, caplog)

        assert error.path == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _cw_worktree_records(caplog, logging.WARNING) == []
        assert any(
            "live daemon worker" in m and str(wt) in m for m in _debug_reasons(caplog)
        )

    def test_daemon_worker_homed_elsewhere_does_not_block(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)
        _seed_roster(tmp_path / "elsewhere")

        _refresh_with_debug(client, caplog)

        assert git_in(wt, "rev-parse", "HEAD") == new_sha

    def test_absent_roster_means_no_live_worker_and_fast_forwards(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)
        assert not native_daemon._ROSTER_PATH.exists()
        # The isolation guard: the patched path is not the developer's real one.
        assert (
            Path.home() / ".claude" / "daemon" / "roster.json"
        ) != native_daemon._ROSTER_PATH

        _refresh_with_debug(client, caplog)

        assert git_in(wt, "rev-parse", "HEAD") == new_sha

    @pytest.mark.parametrize(
        "kind", ["invalid-json", "invalid-utf8", "directory", "entry-no-cwd"]
    )
    def test_unreadable_roster_fails_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        kind: str,
    ) -> None:
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        if kind == "invalid-json":
            _seed_roster(raw="{not json")
        elif kind == "invalid-utf8":
            _seed_roster().write_bytes(
                b'{"workers": {"aaaa1111": {"cwd": "\xff\xfe"}}}'
            )
        elif kind == "directory":
            native_daemon._ROSTER_PATH.mkdir(parents=True)  # OSError, not ENOENT
        else:
            _seed_roster(raw=json.dumps({"workers": {"aaaa1111": {"pid": 1}}}))
        fetched = _spy_fetch(monkeypatch)

        error = _refresh_occupied_with_debug(client, caplog)

        assert error.path == wt
        assert "roster unreadable" in error.reason
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any("roster unreadable" in m for m in _debug_reasons(caplog))

    def test_corrupt_state_file_fails_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Real corrupt sessions.json through the real reader: occupied."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        state_file().write_text("{not json", encoding="utf-8")

        error = _refresh_occupied_with_debug(client, caplog)

        assert "session state unreadable" in error.reason
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any("session state unreadable" in m for m in _debug_reasons(caplog))

    @pytest.mark.parametrize(
        ("change", "expected_reason"),
        [
            pytest.param("session", "live session", id="session-appeared"),
            pytest.param("roster", "live daemon worker", id="worker-appeared"),
            pytest.param("dirty", "unsaved work", id="tree-dirtied"),
            pytest.param("branch", "dev/other", id="branch-switched"),
        ],
    )
    def test_occupancy_change_after_fetch_aborts_before_the_merge(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        change: str,
        expected_reason: str,
    ) -> None:
        """The occupancy gate runs before the (slow) fetch; the whole predicate
        runs again immediately before ``merge --ff-only``. A session or worker
        appearing raises the occupancy error; the tree being dirtied aborts to
        use-as-is; a branch switch raises ``StaleWorktreeError`` (the same
        refusal ``create_worktree``'s own identity guard gives up front). HEAD
        stays untouched in every case."""
        client, wt, workspace, old_sha, new_sha = _seed_behind(tmp_path, make_git_repo)
        assert old_sha != new_sha
        real_fetch = fetch_feature_branch

        def fetch_then_change(client: ClientConfig, branch_name: str) -> FetchResult:
            fetched_ok = real_fetch(client, branch_name)
            if change == "session":
                _seed_session(workspace, wt, SessionStatus.ACTIVE)
            elif change == "roster":
                _seed_roster(wt)
            elif change == "dirty":
                (wt / "scratch.txt").write_text("late edit\n", encoding="utf-8")
            else:
                git_in(wt, "checkout", "-b", "dev/other")
            return fetched_ok

        merges: list[tuple[str, ...]] = []

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                merges.append(args)
            return _run_git(*args, cwd=cwd, check=check)

        patch_worktree(monkeypatch, "fetch_feature_branch", fetch_then_change)
        patch_worktree(monkeypatch, "_run_git", spy)

        # A live occupant (session/roster) refuses with the occupancy error; a
        # branch switch refuses with the stale-worktree error; a merely dirtied
        # tree is the caller's to use.
        if change in {"session", "roster"}:
            error = _refresh_occupied_with_debug(client, caplog)
            assert error.path == wt
            assert expected_reason in error.reason
        elif change == "branch":
            stale = _refresh_stale_with_debug(client, caplog)
            assert expected_reason in str(stale)
        else:
            result = _refresh_with_debug(client, caplog)
            assert result == wt
        assert merges == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _cw_worktree_records(caplog, logging.WARNING) == []
        assert any(
            "occupied after the fetch" in m and str(wt) in m and expected_reason in m
            for m in _debug_reasons(caplog)
        )

    def test_unchanged_occupancy_still_fast_forwards_after_recheck(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Control: the re-check must not veto a genuinely free worktree."""
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)

        _refresh_with_debug(client, caplog)

        assert git_in(wt, "rev-parse", "HEAD") == new_sha

    def test_predicate_reports_unexpected_branch(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        """The expected-branch check lives in the one predicate, so the
        pre-mutation re-check covers it (``create_worktree``'s own guard raises
        earlier, but a checkout can land in between)."""
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        daemon = native_daemon.get_native_daemon_client()
        assert _reuse_occupancy(client, _REUSE_BRANCH, wt, daemon=daemon).reason is None

        git_in(wt, "checkout", "-b", "dev/other")

        reason = _reuse_occupancy(client, _REUSE_BRANCH, wt, daemon=daemon).reason
        assert reason is not None
        assert "dev/other" in reason

    def test_predicate_reports_detached_head_as_unexpected_branch(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        git_in(wt, "checkout", "--detach")
        daemon = native_daemon.get_native_daemon_client()

        reason = _reuse_occupancy(client, _REUSE_BRANCH, wt, daemon=daemon).reason

        assert reason is not None
        assert "detached" in reason

    @pytest.mark.parametrize("kind", ["eloop", "enotdir", "eacces", "enametoolong"])
    @pytest.mark.parametrize("side", ["target", "session", "worker"])
    def test_path_that_cannot_be_normalized_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
        side: str,
    ) -> None:
        """Round 4: ANY ``OSError`` while normalizing a path reads as occupied,
        not just ``ELOOP``. Before, ``EACCES``, ``ENOTDIR``, ``ENAMETOOLONG``
        and friends were swallowed by the loop probe and read as "not
        occupied", which permitted the mutation. Covers the worktree being
        checked and both kinds of recorded home (session state, daemon roster).
        Real filesystem states, except ``EACCES``: ``chmod 000`` cannot deny a
        root test runner, so ``Path.stat`` is made to deny that one path."""
        bad = _unnormalizable_path(kind, tmp_path, monkeypatch)
        good = tmp_path / "wt"
        good.mkdir()
        if side == "session":
            monkeypatch.setattr(
                "cw.worktree._refresh.live_session_worktree_paths",
                lambda: frozenset({bad}),
            )
        elif side == "worker":
            _seed_roster(bad)

        reason = live_home_reason(
            bad if side == "target" else good,
            daemon=native_daemon.get_native_daemon_client(),
        )

        assert reason is not None
        assert "cannot be resolved" in reason

    def test_normalize_path_tolerates_only_a_missing_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone"
        assert _normalize_path(missing) == missing.resolve()
        with pytest.raises(OSError, match=r"\[Errno") as excinfo:
            _normalize_path(_symlink_loop(tmp_path))
        assert excinfo.value.errno == errno.ELOOP

    def test_permission_denied_session_path_refuses_the_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        denied = _unnormalizable_path("eacces", tmp_path, monkeypatch)
        _seed_session(workspace, denied, SessionStatus.ACTIVE)
        fetched = _spy_fetch(monkeypatch)

        error, report = _refresh_occupied(client)

        assert error.path == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert "cannot be resolved" in error.reason
        assert report.outcome is RefreshOutcome.OCCUPIED_BY_LIVE_SESSION

    def test_missing_recorded_path_is_not_a_loop(self, tmp_path: Path) -> None:
        """Control: a recorded path whose directory is simply gone is ENOENT,
        not ELOOP, and must not read as indeterminate -- a stale entry for a
        deleted worktree would otherwise veto every refresh."""
        _seed_roster(tmp_path / "deleted-worktree")

        assert (
            live_home_reason(
                tmp_path / "wt", daemon=native_daemon.get_native_daemon_client()
            )
            is None
        )

    def test_symlink_loop_session_refuses_the_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        _seed_session(workspace, _symlink_loop(tmp_path), SessionStatus.ACTIVE)
        fetched = _spy_fetch(monkeypatch)

        error = _refresh_occupied_with_debug(client, caplog)

        assert error.path == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any("cannot be resolved" in m for m in _debug_reasons(caplog))

    @pytest.mark.parametrize("kind", ["eloop", "enotdir", "eacces", "enametoolong"])
    @pytest.mark.parametrize("side", ["session", "worker"])
    def test_poisoned_record_is_skipped_not_vetoing_but_target_still_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        kind: str,
        side: str,
    ) -> None:
        """A single poisoned record must not veto an unrelated target -- but a
        skip still means the answer is not "definitely free" (#2240)."""
        bad = _unnormalizable_path(kind, tmp_path, monkeypatch)
        good = tmp_path / "wt"
        good.mkdir()
        if side == "session":
            monkeypatch.setattr(
                "cw.worktree._refresh.live_session_worktree_paths",
                lambda: frozenset({bad}),
            )
        else:
            _seed_roster(bad)

        reason = live_home_reason(good, daemon=native_daemon.get_native_daemon_client())

        assert reason is not None
        assert "cannot be resolved" in reason
        records = _cw_worktree_records(caplog, logging.WARNING)
        assert len(records) == 1
        message = records[0].getMessage()
        assert str(bad) in message
        assert side in message

    def test_bad_record_does_not_mask_a_genuine_match_on_a_different_record(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A poisoned record on one side must not corrupt attribution for a
        genuine match reported by the other, unrelated record (#2240)."""
        target = tmp_path / "wt"
        target.mkdir()
        bad_worker = _unnormalizable_path("eloop", tmp_path, monkeypatch)
        monkeypatch.setattr(
            "cw.worktree._refresh.live_session_worktree_paths",
            lambda: frozenset({target}),
        )
        _seed_roster(bad_worker)

        reason = live_home_reason(target, daemon=native_daemon.get_native_daemon_client())

        assert reason == "a live session is homed on this worktree"
        assert len(_cw_worktree_records(caplog, logging.WARNING)) == 1

        caplog.clear()
        (tmp_path / "b").mkdir()
        bad_session = _unnormalizable_path("eloop", tmp_path / "b", monkeypatch)
        monkeypatch.setattr(
            "cw.worktree._refresh.live_session_worktree_paths",
            lambda: frozenset({bad_session}),
        )
        _seed_roster(target)

        reason = live_home_reason(target, daemon=native_daemon.get_native_daemon_client())

        assert reason == "a live daemon worker is homed on this worktree"
        assert len(_cw_worktree_records(caplog, logging.WARNING)) == 1

    def test_multiple_skipped_records_are_each_counted_and_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Both a bad session record and a bad worker record must be counted
        and logged -- the old blanket ``try`` raised on the first one and
        never even attempted the second."""
        good = tmp_path / "wt"
        good.mkdir()
        (tmp_path / "s").mkdir()
        bad_session = _unnormalizable_path("eloop", tmp_path / "s", monkeypatch)
        monkeypatch.setattr(
            "cw.worktree._refresh.live_session_worktree_paths",
            lambda: frozenset({bad_session}),
        )
        (tmp_path / "w").mkdir()
        bad_worker = _unnormalizable_path("eacces", tmp_path / "w", monkeypatch)

        reason = live_home_reason(good, daemon=native_daemon.get_native_daemon_client())

        assert reason is not None
        assert "2" in reason
        records = _cw_worktree_records(caplog, logging.WARNING)
        assert len(records) == 2
        messages = [r.getMessage() for r in records]
        assert any(str(bad_session) in m for m in messages)
        assert any(str(bad_worker) in m for m in messages)

    def test_unresolvable_warning_deduped_when_caller_owns_the_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        good = tmp_path / "wt"
        good.mkdir()
        bad = _unnormalizable_path("eloop", tmp_path, monkeypatch)
        monkeypatch.setattr(
            "cw.worktree._refresh.live_session_worktree_paths",
            lambda: frozenset({bad}),
        )
        warned: set[UnresolvablePathWarningKey] = set()
        daemon = native_daemon.get_native_daemon_client()

        first = live_home_reason(good, daemon=daemon, warned_unresolvable=warned)
        second = live_home_reason(good, daemon=daemon, warned_unresolvable=warned)

        assert first is not None
        assert first == second
        assert len(_cw_worktree_records(caplog, logging.WARNING)) == 1

    def test_unresolvable_warning_not_deduped_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        good = tmp_path / "wt"
        good.mkdir()
        bad = _unnormalizable_path("eloop", tmp_path, monkeypatch)
        monkeypatch.setattr(
            "cw.worktree._refresh.live_session_worktree_paths",
            lambda: frozenset({bad}),
        )
        daemon = native_daemon.get_native_daemon_client()

        live_home_reason(good, daemon=daemon)
        live_home_reason(good, daemon=daemon)

        assert len(_cw_worktree_records(caplog, logging.WARNING)) == 2

    def test_unresolvable_warning_key_distinguishes_session_from_worker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The SAME raw bad path, recorded on both sides, must warn twice --
        the dedup key includes ``kind``, not just ``(path, error)``."""
        good = tmp_path / "wt"
        good.mkdir()
        bad = _unnormalizable_path("eloop", tmp_path, monkeypatch)
        monkeypatch.setattr(
            "cw.worktree._refresh.live_session_worktree_paths",
            lambda: frozenset({bad}),
        )
        _seed_roster(bad)
        warned: set[UnresolvablePathWarningKey] = set()

        live_home_reason(
            good,
            daemon=native_daemon.get_native_daemon_client(),
            warned_unresolvable=warned,
        )

        assert len(_cw_worktree_records(caplog, logging.WARNING)) == 2


_FULL_SHA_CHARS = 40


def _ff_events() -> list[OrchestratorEvent]:
    """Every ``worktree.fast_forwarded`` event in the (test-isolated) inbox."""
    return read_events(event_types=[OrchestratorEventType.WORKTREE_FAST_FORWARDED])


def _refresh_with_ticket(client: ClientConfig, ticket_id: str | None = "2213") -> Path:
    return create_worktree(
        client,
        _REUSE_BRANCH,
        allow_dirty_reuse=True,
        refresh_on_reuse=True,
        ticket_id=ticket_id,
    )


class TestFastForwardAuditEvent:
    """#2213 round 6: a fast-forward that actually moves HEAD leaves exactly one
    ``worktree.fast_forwarded`` audit event. Every path that moves nothing
    (already current, ahead, diverged, refused, occupied, not refreshed) leaves
    none: a record per turn would be noise."""

    def test_real_fast_forward_emits_exactly_one_event_with_both_shas(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _workspace, old_sha, new_sha = _seed_behind(tmp_path, make_git_repo)
        assert len(old_sha) == len(new_sha) == _FULL_SHA_CHARS

        assert _refresh_with_ticket(client) == wt

        events = _ff_events()
        assert len(events) == 1
        event = events[0]
        assert event.type is OrchestratorEventType.WORKTREE_FAST_FORWARDED
        assert event.correlation_id == "2213"
        assert event.payload == {
            "client": "test",
            "ticket_id": "2213",
            "branch": _REUSE_BRANCH,
            "worktree_path": str(wt),
            "old_sha": old_sha,
            "new_sha": new_sha,
        }
        assert git_in(wt, "rev-parse", "HEAD") == new_sha

    def test_unknown_ticket_is_a_null_ticket_and_no_correlation_id(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, _wt, _workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)

        _refresh_with_ticket(client, ticket_id=None)

        (event,) = _ff_events()
        assert event.payload["ticket_id"] is None
        assert event.correlation_id is None

    def test_default_reuse_moves_nothing_and_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, _wt, _workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)

        create_worktree(client, _REUSE_BRANCH, allow_dirty_reuse=True)

        assert _ff_events() == []

    def test_already_current_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, _wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)

        _refresh_with_ticket(client)

        assert _ff_events() == []

    def test_ahead_of_origin_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        git_in(wt, "commit", "--allow-empty", "-m", "unpushed local work")
        local_sha = git_in(wt, "rev-parse", "HEAD")

        _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == local_sha
        assert _ff_events() == []

    def test_diverged_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        _force_push_rewrite(origin, tmp_path / "side", _REUSE_BRANCH)

        _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    def test_dirty_overlapping_worktree_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        (wt / "tracked.txt").write_text("local edit\n", encoding="utf-8")
        push_commit_to_origin(
            origin,
            _REUSE_BRANCH,
            tmp_path / "side",
            "tracked.txt",
            content="upstream edit\n",
        )

        _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    def test_git_refusing_the_fast_forward_emits_nothing(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                return subprocess.CompletedProcess(
                    args, 1, "", "error: local changes would be overwritten\n"
                )
            return _run_git(*args, cwd=cwd, check=check)

        patch_worktree(monkeypatch, "_run_git", spy)

        _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_occupied_refusal_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path], source: str
    ) -> None:
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        _occupy(source, workspace, wt)

        with pytest.raises(WorktreeOccupiedError):
            _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    def test_failed_fetch_emits_nothing(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        patch_worktree(
            monkeypatch,
            "fetch_feature_branch",
            lambda *_a, **_kw: FetchResult(FetchOutcome.FAILED, "rc=128: offline"),
        )

        _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    def test_merge_that_moves_nothing_emits_nothing(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A merge that succeeds ("Already up to date") but leaves HEAD where it
        was is not a fast-forward that happened: REFRESHED, but no event."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                return subprocess.CompletedProcess(args, 0, "Already up to date.\n", "")
            return _run_git(*args, cwd=cwd, check=check)

        patch_worktree(monkeypatch, "_run_git", spy)
        report = ReuseRefreshReport()

        create_worktree(
            client,
            _REUSE_BRANCH,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
            ticket_id="2213",
        )

        assert report.outcome is RefreshOutcome.REFRESHED
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    def test_audit_write_failure_does_not_undo_the_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed audit write (``OSError``) is logged at WARNING and never
        turns a completed fast-forward into NOT_REFRESHED, a note or a raise."""
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)

        def boom(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.worktree._refresh.record_event", boom)
        report = ReuseRefreshReport()

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            result = create_worktree(
                client,
                _REUSE_BRANCH,
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                refresh_report=report,
                ticket_id="2213",
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == new_sha
        assert report.outcome is RefreshOutcome.REFRESHED
        assert report.notes == []
        assert any(
            "audit" in r.getMessage() and "disk full" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )
        assert _ff_events() == []
