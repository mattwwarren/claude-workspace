"""Tests for cw.worktree._scope - branch-diff scope measurement (#1487)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.worktree import (
    resolve_scope_guard_default_branch,
)
from tests._reconcile_helpers import _no_op_salvage_payload
from tests._worktree_helpers import patch_worktree
from tests.conftest import git_in
from tests.test_result import _valid_payload

if TYPE_CHECKING:
    from collections.abc import Callable


class TestHasCommitsBeyondBase:
    """Tests for _has_commits_beyond_base."""

    def test_commits_present_returns_true(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_run_git returns non-empty stdout → True."""
        from cw.worktree import _has_commits_beyond_base

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0)
            result.stdout = "abc1234 chore: add feature\n"
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path, "main") is True

    def test_no_commits_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_run_git returns empty stdout → False."""
        from cw.worktree import _has_commits_beyond_base

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0)
            result.stdout = ""
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path, "main") is False

    def test_git_failure_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_run_git returns nonzero exit → False."""
        from cw.worktree import _has_commits_beyond_base

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=128)
            result.stdout = ""
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path, "main") is False

    def test_nonexistent_path_returns_false(self) -> None:
        """Path that doesn't exist → False."""
        from pathlib import Path as _Path

        from cw.worktree import _has_commits_beyond_base

        assert _has_commits_beyond_base(_Path("/nonexistent/path/xyz"), "main") is False

    def test_oserror_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """OSError from _run_git (e.g. git not on PATH) → False."""
        from cw.worktree import _has_commits_beyond_base

        def mock_run(*args: str, cwd: object, check: bool = True) -> None:
            msg = "git not found"
            raise OSError(msg)

        patch_worktree(monkeypatch, "_run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path, "main") is False

    def test_uses_default_branch_in_ref(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """default_branch is passed to git log, not hardcoded 'main'."""
        from cw.worktree import _has_commits_beyond_base

        captured_args: list[str] = []

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            captured_args.extend(args)
            result = MagicMock(returncode=0)
            result.stdout = "abc1234 feat: custom branch commit\n"
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path, "dev") is True
        assert "origin/dev..HEAD" in captured_args
        assert "origin/main..HEAD" not in captured_args


# ----------------------------------------------------------------------
# #1487 — git-fact scope verification (_parse_numstat_totals,
# compute_branch_diff_scope, reconcile_result_scope)
# ----------------------------------------------------------------------


def _commit_files(repo: Path, prefix: str, *, files: int, lines: int) -> None:
    """Add *files* new files of *lines* lines each and commit them."""
    for i in range(files):
        body = "".join(f"{prefix}-{i}-{n}\n" for n in range(lines))
        (repo / f"{prefix}_{i}.txt").write_text(body, encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", f"{prefix}: {files} files")


def _make_self_origin_repo(make_git_repo: Callable[[str], Path], name: str) -> Path:
    """Create a repo whose ``origin`` remote points at itself, with origin/main set."""
    repo = make_git_repo(name)
    git_in(repo, "remote", "add", "origin", str(repo))
    git_in(repo, "fetch", "origin", "main")
    return repo


def _make_diverged_repo(
    make_git_repo: Callable[[str], Path],
    name: str,
    *,
    branch: str = "dev/1487",
    branch_files: int = 3,
    branch_lines: int = 5,
    main_files: int = 8,
    main_lines: int = 40,
) -> Path:
    """Repo where ``origin/main`` advanced *after* *branch* forked from it.

    This is the #1393 shape: a self-report computed against a stale merge-base
    inflates ``files``/``lines_actual`` by counting main's own churn. A correct
    measurement uses ``merge-base origin/main HEAD`` and sees only the branch's
    own changes (*branch_files* / ``branch_files * branch_lines``).
    """
    repo = _make_self_origin_repo(make_git_repo, name)
    git_in(repo, "checkout", "-b", branch)
    _commit_files(repo, "branchwork", files=branch_files, lines=branch_lines)
    git_in(repo, "checkout", "main")
    _commit_files(repo, "mainchurn", files=main_files, lines=main_lines)
    # Refresh origin/main so it now points past the branch's fork point.
    git_in(repo, "fetch", "origin", "main")
    git_in(repo, "checkout", branch)
    return repo


class TestParseNumstatTotals:
    def test_normal_add_and_remove_lines(self) -> None:
        from cw.worktree import _parse_numstat_totals

        out = "3\t1\ta.py\n10\t0\tb.py\n"
        assert _parse_numstat_totals(out) == (2, 14)

    def test_binary_file_dash_is_skipped(self) -> None:
        from cw.worktree import _parse_numstat_totals

        out = "-\t-\timage.png\n5\t2\tc.py\n"
        assert _parse_numstat_totals(out) == (1, 7)

    def test_empty_output_is_zero(self) -> None:
        from cw.worktree import _parse_numstat_totals

        assert _parse_numstat_totals("") == (0, 0)

    def test_short_lines_are_ignored(self) -> None:
        from cw.worktree import _parse_numstat_totals

        assert _parse_numstat_totals("garbage\n1\t1\n") == (0, 0)


class TestComputeBranchDiffScope:
    def test_measures_only_branch_changes_past_a_stale_merge_base(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """origin/main advanced after the fork → only the branch's own churn counts."""
        from cw.worktree import compute_branch_diff_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-diverged")

        scope = compute_branch_diff_scope(repo, "main")

        assert scope is not None
        assert scope["branch"] == "dev/1487"
        assert scope["files"] == 3
        assert scope["lines_actual"] == 15
        assert scope["merge_base"]

    def test_no_changes_returns_zeroes(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.worktree import compute_branch_diff_scope

        repo = _make_self_origin_repo(make_git_repo, "wt-1487-clean")
        git_in(repo, "checkout", "-b", "dev/clean")

        scope = compute_branch_diff_scope(repo, "main")

        assert scope is not None
        assert scope["files"] == 0
        assert scope["lines_actual"] == 0

    def test_missing_origin_ref_returns_none(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.worktree import compute_branch_diff_scope

        repo = make_git_repo("wt-1487-no-origin")
        assert compute_branch_diff_scope(repo, "main") is None

    def test_detached_head_returns_none(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.worktree import compute_branch_diff_scope

        repo = _make_self_origin_repo(make_git_repo, "wt-1487-detached")
        git_in(repo, "checkout", "--detach")
        assert compute_branch_diff_scope(repo, "main") is None

    def test_missing_path_returns_none(self, tmp_path: Path) -> None:
        from cw.worktree import compute_branch_diff_scope

        assert compute_branch_diff_scope(tmp_path / "nope", "main") is None

    def test_merge_base_oserror_returns_none(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """git vanishes between the branch probe and merge-base → None, no raise."""
        from cw import worktree as wt_mod

        repo = _make_self_origin_repo(make_git_repo, "wt-1487-oserror")

        def boom(*args: str, cwd: object, check: bool = True) -> None:
            msg = "git not found"
            raise OSError(msg)

        patch_worktree(monkeypatch, "_checked_out_branch", lambda _p: "dev/x")
        patch_worktree(monkeypatch, "_run_git", boom)
        assert wt_mod.compute_branch_diff_scope(repo, "main") is None

    def test_numstat_oserror_returns_none(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """merge-base resolves but the numstat call raises → None, no raise."""
        from cw import worktree as wt_mod

        repo = _make_diverged_repo(make_git_repo, "wt-1487-numstat-oserror")

        def boom_on_diff(*args: str, cwd: object, check: bool = True) -> None:
            msg = "git not found"
            raise OSError(msg)

        patch_worktree(monkeypatch, "_checked_out_branch", lambda _p: "dev/x")
        monkeypatch.setattr(
            "cw.worktree._scope._resolve_merge_base", lambda _p, _b: "deadbeef"
        )
        patch_worktree(monkeypatch, "_run_git", boom_on_diff)
        assert wt_mod.compute_branch_diff_scope(repo, "main") is None

    def test_numstat_nonzero_returncode_returns_none(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unresolvable merge-base ref makes git diff exit non-zero → None."""
        from cw import worktree as wt_mod

        repo = _make_diverged_repo(make_git_repo, "wt-1487-numstat-rc")

        monkeypatch.setattr(
            "cw.worktree._scope._resolve_merge_base",
            lambda _p, _b: "0000000000000000000000000000",
        )
        assert wt_mod.compute_branch_diff_scope(repo, "main") is None


class TestScopeMismatchIsGross:
    def test_equal_values_are_not_gross(self) -> None:
        """Guards the 0-vs-0 case, where the ratio test has no meaningful answer."""
        from cw.worktree import _scope_mismatch_is_gross

        assert _scope_mismatch_is_gross(0, 0) is False
        assert _scope_mismatch_is_gross(7, 7) is False


def _result_with_scope(files: int, lines_actual: int) -> AutoDevResult:
    """Build a post-impl AutoDevResult carrying a self-reported scope."""
    payload = _valid_payload()
    payload["scope"] = {
        "tier": "small",
        "files": files,
        "lines_estimate": 42,
        "lines_actual": lines_actual,
        "forbidden_touched": False,
    }
    return AutoDevResult.model_validate(payload)


class TestReconcileResultScope:
    def test_pre_impl_stage_is_exempt(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """stage1_* exits have no measurable diff — leave the self-report alone."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-preimpl")
        payload = _no_op_salvage_payload()
        payload["scope"] = {
            "tier": None,
            "files": 99,
            "lines_estimate": 0,
            "lines_actual": None,
            "forbidden_touched": False,
        }
        result = AutoDevResult.model_validate(payload)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out is result
        assert out.scope.files == 99
        assert caplog.text == ""

    def test_worktree_path_none_is_a_noop(self) -> None:
        from cw.worktree import reconcile_result_scope

        result = _result_with_scope(18, 1567)
        out = reconcile_result_scope(result, worktree_path=None, default_branch="main")
        assert out is result

    def test_inflated_self_report_is_corrected_and_warns(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """#1393 acceptance shape: 18/1567 self-reported vs 3/15 measured."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-inflated")
        result = _result_with_scope(18, 1567)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out.scope.files == 3
        assert out.scope.lines_actual == 15
        assert out.scope.lines_estimate == 42
        assert out.scope.tier == "small"
        assert out.scope.forbidden_touched is False
        assert "scope.files" in caplog.text
        assert "scope.lines_actual" in caplog.text

    def test_matching_self_report_is_untouched_and_silent(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-match")
        result = _result_with_scope(3, 15)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out is result
        assert caplog.text == ""

    def test_small_divergence_corrects_without_warning(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Both non-zero and within the ratio threshold → corrected, no WARNING."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-small-delta")
        result = _result_with_scope(4, 20)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out.scope.files == 3
        assert out.scope.lines_actual == 15
        assert caplog.text == ""

    def test_zero_vs_nonzero_always_warns(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 0 self-report against real work is a gross mismatch at any ratio."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-zero")
        result = _result_with_scope(0, 0)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out.scope.files == 3
        assert out.scope.lines_actual == 15
        assert "scope.files" in caplog.text
        assert "scope.lines_actual" in caplog.text

    def test_nonzero_vs_zero_measured_always_warns(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Self-reported work against an empty branch is the inverse gross case."""
        from cw.worktree import reconcile_result_scope

        repo = _make_self_origin_repo(make_git_repo, "wt-1487-empty-branch")
        git_in(repo, "checkout", "-b", "dev/empty")
        result = _result_with_scope(7, 300)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out.scope.files == 0
        assert out.scope.lines_actual == 0
        assert "scope.files" in caplog.text

    def test_fields_are_evaluated_independently(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """files matches but lines is grossly wrong → only lines warns."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-independent")
        result = _result_with_scope(3, 900)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out.scope.files == 3
        assert out.scope.lines_actual == 15
        assert "scope.lines_actual" in caplog.text
        assert "scope.files" not in caplog.text

    def test_lines_only_mismatch_leaves_files_untouched(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Inverse of the above: lines matches, files does not."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-files-only")
        result = _result_with_scope(11, 15)

        out = reconcile_result_scope(result, worktree_path=repo, default_branch="main")

        assert out.scope.files == 3
        assert out.scope.lines_actual == 15

    def test_git_failure_leaves_result_unchanged_and_warns(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """No origin/<default_branch> → unverifiable; fail open, never raise."""
        from cw.worktree import reconcile_result_scope

        repo = make_git_repo("wt-1487-unverifiable")
        result = _result_with_scope(18, 1567)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out is result
        assert out.scope.files == 18
        assert "scope_verification_unavailable" in caplog.text


# ----------------------------------------------------------------------
# #1487 (fix loop) — resolve_scope_guard_default_branch: the shared
# client-resolution helper both cli.stop_hook and reconcile._shared now call,
# replacing two independently-written try/except wrappers.
# ----------------------------------------------------------------------


def _write_clients_yaml(
    tmp_config_dir: Path, name: str, *, default_branch: str
) -> None:
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        "clients:\n"
        f"  {name}:\n"
        f"    workspace_path: /tmp/ws-{name}\n"
        f"    default_branch: {default_branch}\n"
    )


class TestResolveScopeGuardDefaultBranch:
    def test_configured_client_resolves_its_own_default_branch(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_clients_yaml(tmp_config_dir, "acme", default_branch="trunk")

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            branch = resolve_scope_guard_default_branch("acme", log_context="ctx")

        assert branch == "trunk"
        assert caplog.text == ""

    def test_no_clients_yaml_falls_back_to_main_and_warns(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            branch = resolve_scope_guard_default_branch("acme", log_context="ctx")

        assert branch == "main"
        assert "scope_verification_client_unresolved" in caplog.text
        assert "client=acme" in caplog.text

    def test_unknown_client_key_in_valid_yaml_falls_back_and_warns(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A well-formed clients.yaml missing the requested key is a logged fallback.

        This is the case the two independent pre-fix wrappers diverged on:
        stop_hook.py's get_client() raised CwError here (logged), but
        _shared.py's dict.get() silently returned None (unlogged). The shared
        helper must log in both cases.
        """
        _write_clients_yaml(tmp_config_dir, "beta", default_branch="develop")

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            branch = resolve_scope_guard_default_branch("acme", log_context="ctx")

        assert branch == "main"
        assert "scope_verification_client_unresolved" in caplog.text
        assert "client=acme" in caplog.text

    def test_malformed_yaml_falls_back_to_main_and_never_raises(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A real YAML syntax error must not propagate past this guard (#1487)."""
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text("clients: [\n  unterminated\n")

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            branch = resolve_scope_guard_default_branch("acme", log_context="ctx")

        assert branch == "main"
        assert "scope_verification_client_unresolved" in caplog.text

    def test_log_context_rides_along_in_the_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            resolve_scope_guard_default_branch("acme", log_context="session=sess-42")

        assert "session=sess-42" in caplog.text
