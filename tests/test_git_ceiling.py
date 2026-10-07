"""Self-tests for the suite-wide git-discovery ceiling (``tests/_git_ceiling.py``).

A test that means "this directory is not a repository" must stay true when
``TMPDIR`` points inside a checkout, which is where every dispatched worker's
``tmp_path`` lives (#2470, #2598). The ceiling is injected at the ``Popen``
layer because every production git seam strips ``GIT_*`` from the env it hands
down, so a bare ``setenv`` would be discarded.

The guard tests build the "TMPDIR inside a worktree" layout by hand (a repo
holding a fake basetemp) so they prove the fix on any host, not only when the
suite itself happens to run under an in-repo ``TMPDIR``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from cw._git import capture_head_sha, run_git
from tests import _git_ceiling
from tests._git_ceiling import CEILING_ENV_VAR, confined_env
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_PRINT_CEILING = f"import os; print(os.environ.get({CEILING_ENV_VAR!r}))"
_PRINT_MARK_AND_CEILING = (
    f"import os; print(os.environ.get('MARK'), os.environ.get({CEILING_ENV_VAR!r}))"
)
_POSITIONAL_ARGS_BEFORE_ENV = (-1, None, None, subprocess.PIPE, None, None, True, False)


@pytest.fixture
def enclosed(
    make_git_repo: Callable[..., Path],
) -> tuple[Path, Path, Path]:
    """``(outer repo, fake basetemp, plain dir)`` for a TMPDIR inside a worktree."""
    outer = make_git_repo("outer")
    fake_basetemp = outer / ".cw" / "tmp" / "pytest-of-x" / "pytest-0"
    plain = fake_basetemp / "test_x0" / "plain"
    plain.mkdir(parents=True)
    return outer, fake_basetemp, plain


def _toplevel_argv(plain: Path) -> list[str]:
    return ["git", "-C", str(plain), "rev-parse", "--show-toplevel"]


class TestConfinedEnv:
    def test_none_env_starts_from_os_environ(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(CEILING_ENV_VAR, raising=False)
        monkeypatch.setenv("CW_GIT_CEILING_PROBE", "1")

        result = confined_env(None, tmp_path)

        assert result["CW_GIT_CEILING_PROBE"] == "1"
        assert result[CEILING_ENV_VAR] == str(tmp_path.resolve())
        assert CEILING_ENV_VAR not in os.environ

    def test_env_lacking_the_key_gains_it_without_mutating_the_input(
        self, tmp_path: Path
    ) -> None:
        original = {"PATH": "/bin"}

        result = confined_env(original, tmp_path)

        assert result == {"PATH": "/bin", CEILING_ENV_VAR: str(tmp_path.resolve())}
        assert original == {"PATH": "/bin"}

    def test_a_ceiling_the_caller_already_set_wins(self, tmp_path: Path) -> None:
        original = {CEILING_ENV_VAR: "/caller/chose/this"}

        result = confined_env(original, tmp_path)

        assert result == original
        assert result is not original

    def test_ceiling_is_resolved(self, tmp_path: Path) -> None:
        link = tmp_path / "link"
        link.symlink_to(tmp_path)

        assert confined_env({}, link)[CEILING_ENV_VAR] == str(tmp_path.resolve())


class TestInstalledHook:
    def test_git_control_ascends_into_the_enclosing_repo(
        self, enclosed: tuple[Path, Path, Path]
    ) -> None:
        """Without a nearby ceiling, git does find the repo around ``plain``."""
        outer, _fake_basetemp, plain = enclosed

        assert git_in(plain, "rev-parse", "--show-toplevel") == str(outer.resolve())

    def test_ceiling_survives_the_git_env_strip(
        self, enclosed: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``run_git`` strips every ``GIT_*`` var; the hook still confines it."""
        _outer, fake_basetemp, plain = enclosed
        _git_ceiling.install(monkeypatch, fake_basetemp)

        result = run_git(
            ["rev-parse", "--show-toplevel"], cwd=plain, capture_output=True
        )

        assert result.returncode != 0

    def test_capture_head_sha_sees_a_non_repository(
        self, enclosed: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _outer, fake_basetemp, plain = enclosed
        _git_ceiling.install(monkeypatch, fake_basetemp)

        with pytest.raises(subprocess.CalledProcessError):
            capture_head_sha(plain, strict=True)
        assert capture_head_sha(plain, strict=False) == ""

    def test_env_none_path_is_confined_too(
        self, enclosed: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare ``subprocess.run`` (no ``env=``) inherits ``os.environ``."""
        _outer, fake_basetemp, plain = enclosed
        monkeypatch.delenv(CEILING_ENV_VAR, raising=False)
        _git_ceiling.install(monkeypatch, fake_basetemp)

        result = subprocess.run(
            _toplevel_argv(plain), capture_output=True, text=True, check=False
        )

        assert result.returncode != 0

    def test_callers_own_ceiling_is_left_alone(
        self,
        enclosed: tuple[Path, Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _outer, fake_basetemp, _plain = enclosed
        _git_ceiling.install(monkeypatch, fake_basetemp)

        result = subprocess.run(
            [sys.executable, "-c", _PRINT_CEILING],
            env={CEILING_ENV_VAR: str(tmp_path)},
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == str(tmp_path)

    def test_positional_env_is_passed_through_untouched(
        self, enclosed: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``env`` as a positional arg must not collide with an injected keyword."""
        _outer, fake_basetemp, _plain = enclosed
        _git_ceiling.install(monkeypatch, fake_basetemp)

        proc = subprocess.Popen(
            [sys.executable, "-c", _PRINT_MARK_AND_CEILING],
            *_POSITIONAL_ARGS_BEFORE_ENV,
            None,
            {"MARK": "kept"},
            text=True,
        )
        stdout, _stderr = proc.communicate()

        assert stdout.strip() == "kept None"


class TestAutouseWiring:
    def test_every_child_inherits_the_basetemp_ceiling(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Passes only when the real autouse fixture is installed."""
        result = subprocess.run(
            [sys.executable, "-c", _PRINT_CEILING],
            env={},
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == str(tmp_path_factory.getbasetemp().resolve())

    def test_a_plain_tmp_path_dir_is_not_a_repository(self, tmp_path: Path) -> None:
        """Direct check for a ``TMPDIR`` inside a worktree (trivially green in CI)."""
        plain = tmp_path / "plain"
        plain.mkdir()

        result = run_git(
            ["rev-parse", "--show-toplevel"], cwd=plain, capture_output=True
        )

        assert result.returncode != 0
