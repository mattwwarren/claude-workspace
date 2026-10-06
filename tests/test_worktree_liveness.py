"""Tests for cw.worktree._liveness - live-session and worker liveness (#2213)."""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw import native_daemon
from cw.config import state_file
from cw.exceptions import WorktreeOccupiedError
from cw.models import CwState, SessionStatus
from cw.worktree import (
    RefreshOutcome,
    _normalize_path,
    create_worktree,
    live_home_reason,
    live_session_worktree_paths,
)
from cw.worktree_gc import _live_worktree_paths
from tests._worktree_refresh_helpers import (
    _REUSE_BRANCH,
    _cw_worktree_records,
    _debug_reasons,
    _occupy,
    _refresh_occupied,
    _refresh_occupied_with_debug,
    _refresh_with_debug,
    _seed_behind,
    _seed_reuse,
    _seed_roster,
    _seed_session,
    _seed_session_with_surface_ref,
    _session_at,
    _spy_fetch,
    _unnormalizable_path,
    _vouch_for_roster_worker,
    _write_corrupt_state,
)
from tests.conftest import _symlink_loop, git_in, push_commit_to_origin

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.worktree import UnresolvablePathWarningKey


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


class TestNonTerminalSessionSurfaceRefs:
    """_non_terminal_session_surface_refs: the surface_ref half of the #2480
    occupancy filter, a deliberately separate state-load from
    live_session_worktree_paths (see that function's own docstring for why)."""

    @pytest.mark.parametrize(
        "status",
        [SessionStatus.ACTIVE, SessionStatus.IDLE, SessionStatus.BACKGROUNDED],
    )
    def test_non_terminal_session_surface_ref_included(
        self, monkeypatch: pytest.MonkeyPatch, status: SessionStatus
    ) -> None:
        from cw.worktree._refresh import _non_terminal_session_surface_refs

        state = CwState(
            sessions=[
                _seed_session_with_surface_ref("c/impl", status, "aaaa1111"),
            ]
        )
        monkeypatch.setattr("cw.worktree._refresh.load_state", lambda: state)

        assert _non_terminal_session_surface_refs() == frozenset({"aaaa1111"})

    def test_terminal_and_refless_sessions_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.worktree._refresh import _non_terminal_session_surface_refs

        state = CwState(
            sessions=[
                _seed_session_with_surface_ref(
                    "c/done", SessionStatus.COMPLETED, "bbbb2222"
                ),
                _seed_session_with_surface_ref("c/noref", SessionStatus.ACTIVE, None),
            ]
        )
        monkeypatch.setattr("cw.worktree._refresh.load_state", lambda: state)

        assert _non_terminal_session_surface_refs() == frozenset()

    def test_state_load_failure_returns_none_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from cw.worktree._refresh import _non_terminal_session_surface_refs

        def _boom() -> CwState:
            msg = "corrupt"
            raise ValueError(msg)

        monkeypatch.setattr("cw.worktree._refresh.load_state", _boom)

        with caplog.at_level("WARNING", logger="cw.worktree"):
            refs = _non_terminal_session_surface_refs()

        assert refs is None
        assert any(
            "failed to load session state for surface_ref lookup" in r.getMessage()
            for r in caplog.records
        )


class TestLiveHomeReasonSurfaceRefUnreadable:
    """#2480: live_home_reason's own fail-closed branch for a
    _non_terminal_session_surface_refs() failure, independent of
    live_session_worktree_paths (which can succeed while this fails -- see
    that function's docstring for why the two loads are kept separate)."""

    def test_surface_ref_lookup_failure_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = tmp_path / "wt"
        good.mkdir()
        monkeypatch.setattr(
            "cw.worktree._refresh.live_session_worktree_paths", frozenset
        )
        monkeypatch.setattr(
            "cw.worktree._refresh._non_terminal_session_surface_refs", lambda: None
        )

        reason = live_home_reason(good, daemon=native_daemon.get_native_daemon_client())

        assert reason == "session state unreadable, cannot rule out a live session"


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
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        assert not native_daemon._ROSTER_PATH.exists()
        fake = native_daemon.FakeNativeDaemonClient()
        short_id = fake.seed_live_worker(wt)
        _vouch_for_roster_worker(workspace, short_id=short_id)

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
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        # live in the roster, vouched for by a non-terminal session (#2480) --
        # absent from cw state's *worktree-homed* side, distinct from being
        # absent altogether (which would make it a leaked, non-occupying
        # worker instead).
        _seed_roster(wt)
        _vouch_for_roster_worker(workspace)
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
            _vouch_for_roster_worker(tmp_path)

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
            _vouch_for_roster_worker(tmp_path)

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
        _vouch_for_roster_worker(tmp_path)
        daemon = native_daemon.get_native_daemon_client()

        reason = live_home_reason(target, daemon=daemon)

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
        _vouch_for_roster_worker(tmp_path)

        reason = live_home_reason(target, daemon=daemon)

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
        _seed_roster(bad_worker)
        _vouch_for_roster_worker(tmp_path)

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
        _vouch_for_roster_worker(tmp_path)
        warned: set[UnresolvablePathWarningKey] = set()

        live_home_reason(
            good,
            daemon=native_daemon.get_native_daemon_client(),
            warned_unresolvable=warned,
        )

        assert len(_cw_worktree_records(caplog, logging.WARNING)) == 2
