"""Tests for cw.worktree - Git worktree operations."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw.exceptions import WorktreeError
from cw.models import ClientConfig
from cw.worktree import (
    _git_dir,
    create_worktree,
    is_main_behind_origin,
    remove_worktree,
    resolve_worktree_base,
    slugify_branch,
    worktree_path_for,
)

if TYPE_CHECKING:
    pass


class TestSlugifyBranch:
    def test_slash_to_hyphen(self) -> None:
        assert slugify_branch("feat/search") == "feat-search"

    def test_multiple_slashes(self) -> None:
        assert slugify_branch("feat/ui/search") == "feat-ui-search"

    def test_backslash(self) -> None:
        assert slugify_branch("feat\\search") == "feat-search"

    def test_no_slashes(self) -> None:
        assert slugify_branch("main") == "main"

    def test_trailing_slash_stripped(self) -> None:
        assert slugify_branch("feat/") == "feat"

    def test_hash_replaced(self) -> None:
        # Regression: GitHub issue ids like "#7" used to leak through and
        # break `claude -w` worktree path validation (issue #83). The double
        # hyphen is fine — claude's segment validator allows `-`; readability
        # is the lesser concern.
        assert slugify_branch("auto-dev-#7") == "auto-dev--7"

    def test_hash_run_collapses(self) -> None:
        assert slugify_branch("auto-dev-##7") == "auto-dev--7"

    def test_leading_hash_stripped(self) -> None:
        assert slugify_branch("#7") == "7"

    def test_spaces_replaced(self) -> None:
        assert slugify_branch("feat search bar") == "feat-search-bar"

    def test_unicode_replaced(self) -> None:
        assert slugify_branch("feat-café") == "feat-caf"

    def test_dots_and_underscores_preserved(self) -> None:
        assert slugify_branch("v1.2_beta") == "v1.2_beta"


class TestGitDir:
    def test_legacy_client(self, tmp_path: Path) -> None:
        client = ClientConfig(name="test", workspace_path=tmp_path / "ws")
        assert _git_dir(client) == tmp_path / "ws"

    def test_worktree_client(self, tmp_path: Path) -> None:
        client = ClientConfig(
            name="test",
            repo_path=tmp_path / "repo",
            branch="client-a",
        )
        assert _git_dir(client) == tmp_path / "repo"


class TestResolveWorktreeBase:
    def test_uses_client_worktree_base(self, tmp_path: Path) -> None:
        custom_base = tmp_path / "custom-worktrees"
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=custom_base,
        )
        assert resolve_worktree_base(client) == custom_base

    def test_default_sibling_directory(self, tmp_path: Path) -> None:
        ws = tmp_path / "projects" / "my-repo"
        client = ClientConfig(name="test", workspace_path=ws)
        expected = tmp_path / "projects" / ".worktrees" / "my-repo"
        assert resolve_worktree_base(client) == expected


class TestWorktreePathFor:
    def test_combines_base_and_slug(self, tmp_path: Path) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        result = worktree_path_for(client, "feat/search")
        assert result == tmp_path / "wt" / "feat-search"

    def test_long_default_path_falls_back_to_hashed_base(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the default worktree path would exceed cmux's 64-char cap,
        fall back to a short hash-based base under ``~/.cw/wt/``."""
        monkeypatch.setattr(Path, "home", lambda: Path("/home/u"))

        # Mimic the failing real-world case from the bug report: a long
        # workspace parent + a longish repo name.
        ws = Path("/home/matthew/workspace/companies/infini-player")
        client = ClientConfig(name="infini-player", workspace_path=ws)

        result = worktree_path_for(client, "auto-dev/1")

        # Must be under cmux's 64-char cap.
        assert len(str(result)) <= 64, (
            f"worktree path length {len(str(result))} exceeds cmux cap 64: {result}"
        )
        # Must be under the hash-based fallback root, not the sibling default.
        assert str(result).startswith("/home/u/.cw/wt/")
        # Branch slug preserved at the tail.
        assert result.name == "auto-dev-1"

    def test_short_default_path_unchanged(self) -> None:
        """Short default paths keep the sibling-directory layout."""
        ws = Path("/p/r")
        client = ClientConfig(name="r", workspace_path=ws)
        result = worktree_path_for(client, "main")
        assert result == Path("/p/.worktrees/r/main")

    def test_hashed_fallback_is_stable_across_calls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """create_worktree and remove_worktree must compute the same path
        for the same client; the hash fallback must be deterministic."""
        monkeypatch.setattr(Path, "home", lambda: Path("/home/u"))
        ws = Path("/home/matthew/workspace/companies/infini-player")
        client = ClientConfig(name="infini-player", workspace_path=ws)

        first = worktree_path_for(client, "auto-dev/1")
        second = worktree_path_for(client, "auto-dev/1")
        assert first == second

    def test_client_override_used_even_when_long(self) -> None:
        """An explicit ``worktree_base`` is respected even if it makes the
        resulting path exceed the cap — user choice wins over our fallback."""
        override = Path("/this/is/a/deliberately/long/override/path/from/the/user")
        client = ClientConfig(
            name="test",
            workspace_path=Path("/ws"),
            worktree_base=override,
        )
        result = worktree_path_for(client, "feat/x")
        assert result == override / "feat-x"


class TestCreateWorktree:
    def test_idempotent_existing_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If worktree path already exists, return it without running git."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-search"
        wt_path.mkdir(parents=True)

        # Should NOT call git at all
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            calls.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        result = create_worktree(client, "feat/search")
        assert result == wt_path
        assert len(calls) == 0

    def test_creates_new_branch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            result = MagicMock(stderr="")
            if "rev-parse" in args:
                result.returncode = 128  # branch doesn't exist
            else:
                result.returncode = 0
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        result = create_worktree(client, "feat/new")
        assert result == tmp_path / "wt" / "feat-new"
        # Should have called rev-parse then worktree add -b
        add_call = git_calls[-1]
        assert "worktree" in add_call
        assert "-b" in add_call

    def test_uses_existing_branch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            result = MagicMock(stderr="")
            result.returncode = 0  # branch exists
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        create_worktree(client, "feat/existing")
        add_call = git_calls[-1]
        assert "-b" not in add_call


class TestSubmoduleInit:
    def test_submodule_init_when_gitmodules_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        # Create .gitmodules to trigger submodule init
        (ws / ".gitmodules").write_text("[submodule]\n")

        client = ClientConfig(
            name="test",
            workspace_path=ws,
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            result = MagicMock(stderr="")
            if "rev-parse" in args:
                result.returncode = 128  # branch doesn't exist
            else:
                result.returncode = 0
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        create_worktree(client, "feat/new")

        # Should have: rev-parse, worktree add, submodule update
        submodule_calls = [c for c in git_calls if "submodule" in c]
        assert len(submodule_calls) == 1
        assert "update" in submodule_calls[0]
        assert "--init" in submodule_calls[0]
        assert "--recursive" in submodule_calls[0]

    def test_no_submodule_init_without_gitmodules(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        # No .gitmodules file

        client = ClientConfig(
            name="test",
            workspace_path=ws,
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            result = MagicMock(stderr="")
            if "rev-parse" in args:
                result.returncode = 128
            else:
                result.returncode = 0
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        create_worktree(client, "feat/new")

        submodule_calls = [c for c in git_calls if "submodule" in c]
        assert len(submodule_calls) == 0

    def test_worktree_client_uses_repo_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worktree-mode client uses repo_path for git cwd."""
        repo = tmp_path / "repo"
        repo.mkdir()
        client = ClientConfig(
            name="test",
            repo_path=repo,
            branch="client-a",
            worktree_base=tmp_path / "wt",
        )
        git_cwds: list[object] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_cwds.append(cwd)
            result = MagicMock(stderr="")
            if "rev-parse" in args:
                result.returncode = 128
            else:
                result.returncode = 0
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        create_worktree(client, "client-a")

        # All git commands should use repo_path, not workspace_path
        for cwd in git_cwds:
            assert str(cwd) == str(repo)


class TestRemoveWorktree:
    def test_removes_existing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-search"
        wt_path.mkdir(parents=True)

        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            return MagicMock(returncode=0, stderr="")

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        remove_worktree(client, "feat/search")
        assert any("remove" in call for call in git_calls)

    def test_noop_if_not_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            return MagicMock(returncode=0, stderr="")

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        remove_worktree(client, "feat/nonexistent")
        assert len(git_calls) == 0

    def test_force_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-dirty"
        wt_path.mkdir(parents=True)

        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            return MagicMock(returncode=0, stderr="")

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        remove_worktree(client, "feat/dirty", force=True)
        assert any("--force" in call for call in git_calls)

    def test_raises_on_dirty_without_force(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from subprocess import CalledProcessError

        from cw.exceptions import WorktreeError

        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-dirty"
        wt_path.mkdir(parents=True)

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            if "--force" not in args:
                msg = (
                    "fatal: 'feat-dirty' contains modified or untracked files, "
                    "use --force to delete it"
                )
                err = CalledProcessError(
                    128,
                    " ".join(args),
                    stderr=msg,
                )
                raise WorktreeError("Git command failed") from err
            return MagicMock(returncode=0, stderr="")

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        with pytest.raises(WorktreeError):
            remove_worktree(client, "feat/dirty", force=False)


class TestIsMainBehindOrigin:
    """Tests for is_main_behind_origin."""

    # ------------------------------------------------------------------
    # Option A: real bare-repo tests
    # ------------------------------------------------------------------

    def _make_bare_origin_and_clone(
        self, tmp_path: Path
    ) -> tuple[Path, Path, ClientConfig]:
        """Create a bare origin repo, clone it, return (bare, clone, client)."""
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

        def _git(*args: str, cwd: Path | None = None) -> str:
            result = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                check=True,
                cwd=str(cwd) if cwd else None,
                env=clean_env,
            )
            return result.stdout.strip()

        bare = tmp_path / "bare.git"
        bare.mkdir()
        _git("init", "--bare", "-b", "main", str(bare))

        clone = tmp_path / "clone"
        _git("clone", str(bare), str(clone))
        _git("config", "user.email", "test@example.com", cwd=clone)
        _git("config", "user.name", "cw test", cwd=clone)
        # Create an initial commit so main exists
        (clone / "README.md").write_text("init\n")
        _git("add", "README.md", cwd=clone)
        _git("commit", "-m", "initial", cwd=clone)
        _git("push", "origin", "main", cwd=clone)

        client = ClientConfig(
            name="test-client",
            workspace_path=clone,
            default_branch="main",
        )
        return bare, clone, client

    def test_fresh_main_returns_false(self, tmp_path: Path) -> None:
        """When local main == origin/main, returns (False, sha, sha, 0)."""
        _bare, _clone, client = self._make_bare_origin_and_clone(tmp_path)
        stale, local_sha, origin_sha, behind = is_main_behind_origin(client)
        assert stale is False
        assert local_sha == origin_sha
        assert behind == 0
        assert len(local_sha) == 40  # full SHA

    def test_stale_main_returns_true_with_counts(self, tmp_path: Path) -> None:
        """When origin has a new commit local doesn't have, returns (True, ...)."""
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

        def _git(*args: str, cwd: Path | None = None) -> str:
            result = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                check=True,
                cwd=str(cwd) if cwd else None,
                env=clean_env,
            )
            return result.stdout.strip()

        bare, _clone, client = self._make_bare_origin_and_clone(tmp_path)

        # Create a second clone to push a new commit to the bare origin
        clone2 = tmp_path / "clone2"
        _git("clone", str(bare), str(clone2))
        _git("config", "user.email", "test@example.com", cwd=clone2)
        _git("config", "user.name", "cw test", cwd=clone2)
        (clone2 / "extra.txt").write_text("extra\n")
        _git("add", "extra.txt", cwd=clone2)
        _git("commit", "-m", "second commit", cwd=clone2)
        _git("push", "origin", "main", cwd=clone2)

        # Now the original clone's local main is behind origin/main
        stale, local_sha, origin_sha, behind = is_main_behind_origin(client)
        assert stale is True
        assert local_sha != origin_sha
        assert behind == 1

    # ------------------------------------------------------------------
    # Option B: patched _run_git tests
    # ------------------------------------------------------------------

    def test_no_remote_configured_returns_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fetch rc=128 (no remote) → (False, "", "", 0) + WARNING."""
        client = ClientConfig(
            name="test-client",
            workspace_path=tmp_path / "ws",
            default_branch="main",
        )

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            result = MagicMock()
            result.returncode = 128
            result.stdout = ""
            result.stderr = "fatal: 'origin' does not appear to be a git repository"
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        import logging

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            stale, local_sha, origin_sha, behind = is_main_behind_origin(client)

        assert stale is False
        assert local_sha == ""
        assert origin_sha == ""
        assert behind == 0
        assert any("fetch failed" in r.message for r in caplog.records)

    def test_fetch_failure_raises_worktreeerror_returns_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """WorktreeError on fetch → (False, "", "", 0) + WARNING."""
        client = ClientConfig(
            name="test-client",
            workspace_path=tmp_path / "ws",
            default_branch="main",
        )
        call_count = 0

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if "fetch" in args:
                raise WorktreeError("fetch failed")
            result = MagicMock()
            result.returncode = 0
            result.stdout = "abc123\n"
            result.stderr = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        import logging

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            stale, local_sha, origin_sha, behind = is_main_behind_origin(client)

        assert stale is False
        assert local_sha == ""
        assert origin_sha == ""
        assert behind == 0
        assert any("fetch failed" in r.message for r in caplog.records)

    def test_rev_parse_failure_returns_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """WorktreeError on rev-parse → (False, "", "", 0) + WARNING."""
        client = ClientConfig(
            name="test-client",
            workspace_path=tmp_path / "ws",
            default_branch="main",
        )

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            if "fetch" in args:
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
                return result
            if "rev-parse" in args:
                raise WorktreeError("rev-parse failed")
            result = MagicMock()
            result.returncode = 0
            result.stdout = "0\n"
            result.stderr = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        import logging

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            stale, local_sha, origin_sha, behind = is_main_behind_origin(client)

        assert stale is False
        assert local_sha == ""
        assert origin_sha == ""
        assert behind == 0
        assert any("rev-parse/rev-list failed" in r.message for r in caplog.records)

    def test_legacy_client_uses_workspace_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All _run_git calls use _git_dir(client) as cwd."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        captured_cwds: list[object] = []
        call_index = 0

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            nonlocal call_index
            captured_cwds.append(cwd)
            result = MagicMock()
            result.returncode = 0
            if "fetch" in args:
                result.stdout = ""
                result.stderr = ""
            elif "rev-list" in args:
                result.stdout = "0\n"
                result.stderr = ""
            else:
                # rev-parse calls
                result.stdout = "deadbeef" * 5 + "\n"
                result.stderr = ""
            call_index += 1
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        is_main_behind_origin(client)

        expected_cwd = _git_dir(client)
        for cwd in captured_cwds:
            assert cwd == expected_cwd
