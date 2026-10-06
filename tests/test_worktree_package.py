"""Seam assertions for the ``cw.worktree`` package and its ``_refresh`` split (#2569).

``cw.worktree._refresh`` was a single 1145-line module holding five concerns.
It is now ``_refresh_types``, ``_liveness``, ``_occupancy``, ``_fast_forward``
and a trimmed ``_refresh``. Four things have to stay true, and the behavioral
suite covers none of them:

1. The public surface (``from cw.worktree import X``) is frozen. The expected
   names are hard-coded here, not re-derived from the package, so a dropped
   re-export fails loudly.
2. ``tests/_worktree_helpers.patch_worktree`` patches every submodule. Its
   ``_SUBMODULES`` tuple is hand-maintained; a submodule left out of it keeps a
   stale binding that a patch silently misses.
3. Each patch seam lives in the module its consumer looks it up in. Tests
   patch ``cw.worktree._liveness.live_session_worktree_paths`` and friends by
   dotted path; a function moved back or sideways would decouple those patches
   from the real call sites while every patch still "succeeds".
4. Every reuse-refresh submodule logs as ``cw.worktree._refresh``, the
   pre-split name operators filter on.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import subprocess
from pathlib import Path

import pytest

import cw.worktree
from cw.models import ClientConfig
from cw.native_daemon import FakeNativeDaemonClient
from cw.worktree import (
    FetchOutcome,
    FetchResult,
    RefreshOutcome,
    ReuseRefreshReport,
    _fetch_gate,
    _ff_reused_worktree,
    _Occupancy,
    _occupancy_verdict,
    _warn_unresolvable_path_once,
)
from tests._worktree_helpers import _SUBMODULES

_PINNED_LOGGER = "cw.worktree._refresh"

# ``cw.worktree.__all__`` as of the #2569 split, sorted. Hard-coded on purpose.
_FROZEN_NAMES = [
    "FetchOutcome",
    "FetchResult",
    "FetchWarningKey",
    "RefreshOutcome",
    "RefreshResult",
    "ReuseRefreshReport",
    "UnresolvablePathWarningKey",
    "_BranchDiffScope",
    "_CW_EXCLUDE_PATTERNS",
    "_CW_SCRATCH_PREFIX",
    "_GIT_PORCELAIN_PATH_OFFSET",
    "_GIT_PORCELAIN_UNTRACKED",
    "_HASH_BASE_SEGMENTS",
    "_MISSING_REMOTE_REF_MARKER",
    "_NON_TERMINAL_SESSION_STATUSES",
    "_NUMSTAT_MIN_COLS",
    "_Occupancy",
    "_SCOPE_MISMATCH_RATIO_THRESHOLD",
    "_SHA_LOG_CHARS",
    "_STATE_READ_ERRORS",
    "_WORKSPACE_HASH_CHARS",
    "_WORKTREE_HELD_BY_RE",
    "_WORKTREE_NAME_CAP",
    "_branch_held_error",
    "_checked_out_branch",
    "_commits_ahead",
    "_fetch_default_branch",
    "_fetch_gate",
    "_ff_relation",
    "_ff_reused_worktree",
    "_first_line",
    "_get_behind_count",
    "_git_dir",
    "_handle_branch_absent",
    "_has_commits_beyond_base",
    "_hashed_worktree_base",
    "_home_match_reason",
    "_normalize_path",
    "_normalize_records",
    "_occupancy_verdict",
    "_own_remote_ref",
    "_parse_numstat_totals",
    "_parse_worktree_holder_path",
    "_raise_if_occupied",
    "_reconcile_scope_field",
    "_record_fast_forward",
    "_ref_exists",
    "_refresh_from_tracking_ref",
    "_refresh_reused_worktree",
    "_refresh_reused_worktree_steps",
    "_register_cw_exclude",
    "_resolve_branch_start_point",
    "_resolve_merge_base",
    "_reuse_occupancy",
    "_run_git",
    "_scope_mismatch_is_gross",
    "_sync_reused_submodules",
    "_uncommitted_changes_detail",
    "_unpushed_commits_detail",
    "_upstream_ref",
    "_warn_fetch_skip_once",
    "_warn_unresolvable_path_once",
    "apply_worker_tmpdir",
    "check_main_ff_safety",
    "check_not_main_checkout",
    "compute_branch_diff_scope",
    "create_worktree",
    "effective_worktree_bases",
    "fast_forward_main",
    "fetch_default_branch",
    "fetch_feature_branch",
    "get_head_branch",
    "is_genuinely_live_home_reason",
    "is_main_behind_origin",
    "is_main_checkout_dirty",
    "live_home_reason",
    "live_session_worktree_paths",
    "reconcile_result_scope",
    "remove_worktree",
    "resolve_scope_guard_default_branch",
    "resolve_task_worktree",
    "resolve_worker_tmpdir",
    "resolve_worktree_base",
    "slugify_branch",
    "unsaved_work_reason",
    "worktree_has_unsaved_work",
    "worktree_path_for",
]

# Patch seam -> the submodule whose globals its consumers resolve it in.
_SEAM_OWNERS = {
    "live_home_reason": "cw.worktree._liveness",
    "live_session_worktree_paths": "cw.worktree._liveness",
    "_non_terminal_session_surface_refs": "cw.worktree._liveness",
    "_occupancy_verdict": "cw.worktree._occupancy",
    "_record_fast_forward": "cw.worktree._fast_forward",
}


def _client(tmp_path: Path) -> ClientConfig:
    return ClientConfig(
        name="test", workspace_path=tmp_path, worktree_base=tmp_path / "wt"
    )


def _pinned_records(
    caplog: pytest.LogCaptureFixture, module: str
) -> list[logging.LogRecord]:
    """Records whose emitting code sits in ``cw/worktree/<module>.py``."""
    return [r for r in caplog.records if r.module == module]


def test_public_surface_is_frozen() -> None:
    assert sorted(cw.worktree.__all__) == _FROZEN_NAMES


def test_every_name_resolves() -> None:
    assert len(cw.worktree.__all__) == len(set(cw.worktree.__all__))
    missing = [n for n in cw.worktree.__all__ if not hasattr(cw.worktree, n)]
    assert missing == []


def test_reexports_come_from_package_submodules() -> None:
    """Every re-exported class or function is defined in a ``cw.worktree`` submodule."""
    for name in cw.worktree.__all__:
        obj = getattr(cw.worktree, name)
        if inspect.isclass(obj) or inspect.isfunction(obj):
            assert obj.__module__.startswith("cw.worktree."), name


def test_patch_worktree_covers_every_submodule() -> None:
    """``patch_worktree`` must see every submodule, or a stale binding escapes it."""
    found = pkgutil.iter_modules(cw.worktree.__path__)
    package_modules = {f"cw.worktree.{info.name}" for info in found}

    assert {mod.__name__ for mod in _SUBMODULES} == package_modules


class TestPatchOwnership:
    """Each dotted-path patch seam is defined in the module its consumer reads."""

    @pytest.mark.parametrize(("name", "owner"), sorted(_SEAM_OWNERS.items()))
    def test_seam_is_defined_in_its_owner(self, name: str, owner: str) -> None:
        # ``_non_terminal_session_surface_refs`` is not re-exported; resolve it
        # where its one consumer, ``live_home_reason``, looks it up.
        func = (
            getattr(cw.worktree, name)
            if hasattr(cw.worktree, name)
            else cw.worktree.live_home_reason.__globals__[name]
        )

        assert func.__globals__ is vars(importlib.import_module(owner))


class TestLoggerNamesArePinned:
    """Every reuse-refresh submodule logs as ``cw.worktree._refresh`` (#2569)."""

    @pytest.mark.parametrize(
        "module", ["_liveness", "_occupancy", "_fast_forward", "_refresh"]
    )
    def test_module_logger_is_pinned(self, module: str) -> None:
        mod = importlib.import_module(f"cw.worktree.{module}")

        assert mod._log.name == _PINNED_LOGGER

    def test_types_leaf_holds_the_pinned_name(self) -> None:
        from cw.worktree import _refresh_types

        assert _refresh_types._LOGGER_NAME == _PINNED_LOGGER

    def test_liveness_record_is_pinned(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=_PINNED_LOGGER):
            _warn_unresolvable_path_once(
                None, ("session", "/p", "boom"), "unresolvable %s", "/p"
            )

        records = _pinned_records(caplog, "_liveness")
        assert [r.name for r in records] == [_PINNED_LOGGER]

    def test_occupancy_record_is_pinned(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            "cw.worktree._occupancy._reuse_occupancy",
            lambda *_args, **_kw: _Occupancy(
                live=None, branch_mismatch=None, local="unsaved work (dirty)"
            ),
        )

        with caplog.at_level(logging.DEBUG, logger=_PINNED_LOGGER):
            verdict = _occupancy_verdict(
                _client(tmp_path),
                "dev/2569",
                tmp_path,
                action="not refreshing reused worktree",
                daemon=FakeNativeDaemonClient(),
            )

        assert verdict is not None
        assert verdict.outcome is RefreshOutcome.NOT_REFRESHED
        records = _pinned_records(caplog, "_occupancy")
        assert [r.name for r in records] == [_PINNED_LOGGER]

    def test_fast_forward_record_is_pinned(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def fake_git(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            del cwd, check
            refused = args[0] == "merge"
            return subprocess.CompletedProcess(
                list(args),
                1 if refused else 0,
                stdout="" if refused else "abc123\n",
                stderr="fatal: Not possible to fast-forward" if refused else "",
            )

        def free(*_args: object, **_kw: object) -> None:
            return None

        monkeypatch.setattr("cw.worktree._fast_forward._occupancy_verdict", free)
        monkeypatch.setattr("cw.worktree._fast_forward._run_git", fake_git)

        with caplog.at_level(logging.WARNING, logger=_PINNED_LOGGER):
            result = _ff_reused_worktree(
                _client(tmp_path),
                "dev/2569",
                tmp_path,
                "refs/remotes/origin/dev/2569",
                ReuseRefreshReport(),
                ticket_id=None,
                daemon=FakeNativeDaemonClient(),
            )

        assert result.outcome is RefreshOutcome.NOT_REFRESHED
        records = _pinned_records(caplog, "_fast_forward")
        assert [r.name for r in records] == [_PINNED_LOGGER]

    def test_refresh_record_is_pinned(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            "cw.worktree._refresh.fetch_feature_branch",
            lambda *_args: FetchResult(FetchOutcome.FAILED, "rc=128: fatal: boom"),
        )
        report = ReuseRefreshReport()

        with caplog.at_level(logging.DEBUG, logger=_PINNED_LOGGER):
            stopped = _fetch_gate(
                _client(tmp_path),
                "dev/2569",
                tmp_path,
                report,
                ticket_id=None,
                daemon=FakeNativeDaemonClient(),
            )

        assert stopped is not None
        assert stopped.outcome is RefreshOutcome.NOT_REFRESHED
        records = _pinned_records(caplog, "_refresh")
        assert [r.name for r in records] == [_PINNED_LOGGER]
