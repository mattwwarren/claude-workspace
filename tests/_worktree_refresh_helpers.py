"""Shared helpers for the reuse-refresh test files (#2213).

Module-level fixtures and seed helpers split out of
``tests/test_worktree_refresh.py`` when ``cw.worktree._refresh`` was split
into ``_refresh_types``, ``_liveness``, ``_occupancy``, ``_fast_forward``
and ``_refresh`` (#2569). Each ``tests/test_worktree_<module>.py`` file
imports only what it uses.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw import native_daemon
from cw.config import save_state, state_file
from cw.events import read_events
from cw.exceptions import StaleWorktreeError, WorktreeOccupiedError
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
    FetchResult,
    ReuseRefreshReport,
    _run_git,
    create_worktree,
    fetch_feature_branch,
)
from tests._worktree_helpers import patch_worktree
from tests.conftest import _clean_git_env, _symlink_loop, git_in, push_commit_to_origin

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import OrchestratorEvent

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
    workspace: Path,
    worktree: Path | None,
    status: SessionStatus,
    *,
    surface_ref: str | None = None,
) -> None:
    """Persist one session homed on *worktree* into the (tmp) cw state.

    *surface_ref*, when given, is #2480's link between a daemon-roster entry
    and its owning cw session -- ``live_home_reason`` now counts a roster
    worker as live only when its short id matches a NON-terminal session's
    ``surface_ref``, so a test seeding a roster worker (``_seed_roster``,
    hardcoded short id ``"aaaa1111"``) as genuinely LIVE must also seed a
    session vouching for it via this parameter.
    """
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
                    surface_ref=surface_ref,
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


def _seed_session_with_surface_ref(
    name: str, status: SessionStatus, surface_ref: str | None
) -> Session:
    return Session(
        name=name,
        client="c",
        purpose=SessionPurpose.IMPL,
        status=status,
        origin=SessionOrigin.DAEMON,
        workspace_path=Path("/repo"),
        surface_ref=surface_ref,
    )


def _write_corrupt_state() -> Path:
    """Put a syntactically invalid ``sessions.json`` on disk, for real."""
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    return path


# Short id every ``_seed_roster(cwd)`` call (no ``raw=``) writes its one
# worker under. #2480: a roster worker only counts as a live occupant when
# this id matches a NON-terminal session's ``surface_ref`` -- see
# ``_vouch_for_roster_worker``.
_ROSTER_SHORT_ID = "aaaa1111"


def _seed_roster(cwd: Path | None = None, *, raw: str | None = None) -> Path:
    """Write the (tmp-isolated) daemon roster.

    *cwd* records one live worker homed there (short id
    :data:`_ROSTER_SHORT_ID`); *raw* writes arbitrary bytes instead.
    ``tests/conftest.py`` points ``native_daemon._ROSTER_PATH`` at a tmp
    path, so this never touches the developer's real roster.

    #2480: since ``live_home_reason`` now excludes a roster worker whose
    short id names no NON-terminal cw session, a test that wants THIS
    worker recognized as genuinely live must pair this call with
    :func:`_vouch_for_roster_worker`.
    """
    roster = native_daemon._ROSTER_PATH
    roster.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        raw
        if raw is not None
        else json.dumps({"workers": {_ROSTER_SHORT_ID: {"pid": 1, "cwd": str(cwd)}}})
    )
    roster.write_text(payload, encoding="utf-8")
    return roster


def _vouch_for_roster_worker(
    workspace: Path,
    *,
    short_id: str = _ROSTER_SHORT_ID,
    status: SessionStatus = SessionStatus.ACTIVE,
) -> None:
    """Seed a cw session whose ``surface_ref`` names a roster worker (#2480).

    ``worktree_path=None`` deliberately: this session must vouch for the
    roster worker's *surface_ref* only, never independently satisfy the
    session-homed side of :func:`~cw.worktree.live_home_reason` (which would
    mask which of the two sources actually matched in a test asserting the
    specific "a live daemon worker is homed..." reason string).
    """
    _seed_session(workspace, None, status, surface_ref=short_id)


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
    """Make *path* look occupied via cw state or via the daemon roster.

    The "roster" source also vouches for the roster worker (#2480,
    :func:`_vouch_for_roster_worker`) -- otherwise a roster-only entry with
    no matching cw session is now a leaked worker, not an occupant.
    """
    if source == "state":
        _seed_session(workspace, path, SessionStatus.ACTIVE)
    else:
        _seed_roster(path)
        _vouch_for_roster_worker(workspace)


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


def _make_bare_repo_with_commit(tmp_path: Path, name: str) -> Path:
    """Create a bare repo at ``tmp_path/<name>.git`` seeded with one commit.

    Serves as a submodule's own origin for the reuse-refresh submodule-sync
    tests (#2233).
    """
    seed = tmp_path / f"{name}-seed"
    subprocess.run(
        ["git", "init", "-b", "main", str(seed)],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_git_env(),
    )
    (seed / "README.md").write_text("sub\n", encoding="utf-8")
    git_in(seed, "add", "README.md")
    git_in(
        seed,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=cw test",
        "commit",
        "-m",
        "seed",
    )
    bare = tmp_path / f"{name}.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(bare)],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_git_env(),
    )
    return bare


def _push_submodule_add_to_origin(
    origin: Path, branch: str, work_dir: Path, sub_origin: Path, sub_name: str = "sub"
) -> str:
    """Push a commit to *branch* on *origin* that adds *sub_origin* as a submodule.

    Mirrors :func:`push_commit_to_origin`'s side-clone convention. Passes
    ``protocol.file.allow=always`` on the ``submodule add`` invocation only:
    git 2.38+ blocks local/``file://`` submodule transports by default (the
    CVE-2022-39253 hardening); both origins here are synthetic tmp-path
    repos, so this carries no real security relaxation. The *production*
    ``git submodule update`` call this exercises reads the allowance from
    repo config instead (see ``_seed_behind_with_submodule``).
    """
    if not work_dir.exists():
        subprocess.run(
            ["git", "clone", str(origin), str(work_dir)],
            capture_output=True,
            text=True,
            check=True,
            env=_clean_git_env(),
        )
    git_in(work_dir, "fetch", "origin")
    git_in(work_dir, "checkout", "-B", branch, f"origin/{branch}")
    subprocess.run(
        [
            "git",
            "-C",
            str(work_dir),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(sub_origin),
            sub_name,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_git_env(),
    )
    git_in(
        work_dir,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=cw test",
        "commit",
        "-m",
        "add submodule",
    )
    git_in(work_dir, "push", "origin", branch)
    return git_in(work_dir, "rev-parse", "HEAD")


def _allow_local_submodule_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point HOME/XDG_CONFIG_HOME at a throwaway global git config that
    re-enables the local/``file://`` submodule transport, for this test only.

    Git's CVE-2022-39253 file-transport hardening reads
    ``protocol.file.allow`` only from global/system config or an explicit
    ``-c`` -- never from a repo's own LOCAL config, by design (a hostile repo
    must not be able to re-enable its own transports). The PRODUCTION ``git
    submodule update --init --recursive`` call the submodule-sync tests
    exercise (:func:`_sync_reused_submodules`) passes no ``-c`` of its own, so
    setting ``protocol.file.allow`` on a worktree's local config would not
    reach it. This is the only way to unblock it without touching the real
    machine's git config, isolated per test via ``monkeypatch``.
    """
    home = tmp_path / "fake-home"
    (home / ".config" / "git").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "git" / "config").write_text(
        '[protocol "file"]\n\tallow = always\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


def _seed_behind_with_submodule(
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ClientConfig, Path, Path, str, str, Path]:
    """``_seed_behind`` plus an upstream commit that adds a submodule (#2233).

    Returns ``(client, wt, workspace, old_sha, new_sha, sub_origin)``. See
    :func:`_allow_local_submodule_transport` for why the file transport must
    be unblocked via HOME/XDG_CONFIG_HOME rather than repo config.
    """
    _allow_local_submodule_transport(tmp_path, monkeypatch)

    client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
    old_sha = git_in(wt, "rev-parse", "HEAD")
    sub_origin = _make_bare_repo_with_commit(tmp_path, "sub")
    new_sha = _push_submodule_add_to_origin(
        origin, _REUSE_BRANCH, tmp_path / "side-submodule", sub_origin
    )
    return client, wt, workspace, old_sha, new_sha, sub_origin


def _seed_behind_with_two_submodules(
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ClientConfig, Path, Path, str, str, Path, Path]:
    """``_seed_behind_with_submodule`` but with TWO submodules (#2233 SHOULD_FIX).

    ``git submodule update`` clones submodules one at a time, so deleting only
    one submodule's origin lets a test force a genuine PARTIAL sync failure --
    one submodule cloned, the other not -- rather than an all-or-nothing one.

    Returns ``(client, wt, workspace, old_sha, new_sha, ok_origin, broken_origin)``.
    """
    _allow_local_submodule_transport(tmp_path, monkeypatch)

    client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
    old_sha = git_in(wt, "rev-parse", "HEAD")
    ok_origin = _make_bare_repo_with_commit(tmp_path, "sub-ok")
    broken_origin = _make_bare_repo_with_commit(tmp_path, "sub-broken")
    work_dir = tmp_path / "side-two-submodules"
    _push_submodule_add_to_origin(
        origin, _REUSE_BRANCH, work_dir, ok_origin, sub_name="sub_a_ok"
    )
    new_sha = _push_submodule_add_to_origin(
        origin, _REUSE_BRANCH, work_dir, broken_origin, sub_name="sub_b_broken"
    )
    return client, wt, workspace, old_sha, new_sha, ok_origin, broken_origin


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
