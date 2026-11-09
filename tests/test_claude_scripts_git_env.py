"""Every ``.claude/scripts`` git launch strips ``GIT_*`` before running (#3078).

The scripts are stdlib-only and must not import ``cw``, so each carries its own
copy of ``cw._git.git_clean_env``'s rule. Without it a script run inside a git
hook (or a pytest run inside one) inherits the hook's ``GIT_DIR``,
``GIT_WORK_TREE`` and ``GIT_INDEX_FILE`` and answers for the hook's repository
instead of the one it was pointed at.

Each test points those three variables at a decoy repository, builds a real
repository with the sandboxed runner, and drives the script's git function
in-process. The answer must be the real repository.
"""

from __future__ import annotations

import os
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from tests import _git_sandbox, _host_probe_guard
from tests import _review_monitor_helpers as review_monitor
from tests.conftest import load_guard_script_module

_SCRIPTS = Path(__file__).resolve().parents[1] / ".claude" / "scripts"


def _script(name: str) -> types.ModuleType:
    return load_guard_script_module(_SCRIPTS / f"{name}.py", f"_git_env_{name}")


def _shell() -> types.ModuleType:
    review_monitor.load_entry()
    return sys.modules["review_monitor_lib.shell"]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real repo (the cwd) plus a decoy every ``GIT_*`` variable points at."""
    real = tmp_path / "real"
    decoy = tmp_path / "decoy"
    for path in (real, decoy):
        path.mkdir()
        _git_sandbox.run("init", "-q", "-b", "main", cwd=path)
    (real / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    monkeypatch.chdir(real)
    return real


def _toplevel_gate_key(repo: Path) -> str:
    return str(Path(_script("gate_key").git_common_dir()).parent)


def _toplevel_prep_pr_state(_repo: Path) -> str:
    result: str = _script("prep_pr_state")._run_git(["rev-parse", "--show-toplevel"])
    return result


def _toplevel_prep_pr_finalize(_repo: Path) -> str:
    result: str = _script("prep_pr_finalize").git("rev-parse", "--show-toplevel")
    return result


def _toplevel_changelog_frozen(repo: Path) -> str:
    module = _script("check_changelog_frozen")
    return str(module._repo_root(repo / "CHANGELOG.md"))


def _toplevel_not_main_checkout(repo: Path) -> str:
    return str(_script("check_not_main_checkout")._git_toplevel(repo))


def _toplevel_review_monitor_shell(repo: Path) -> str:
    result: str = _shell()._run_git(["rev-parse", "--show-toplevel"], cwd=str(repo))
    return result


_PROBES: list[tuple[str, Callable[[Path], str]]] = [
    ("gate_key.git_common_dir", _toplevel_gate_key),
    ("prep_pr_state._run_git", _toplevel_prep_pr_state),
    ("prep_pr_finalize.git", _toplevel_prep_pr_finalize),
    ("check_changelog_frozen._repo_root", _toplevel_changelog_frozen),
    ("check_not_main_checkout._git_toplevel", _toplevel_not_main_checkout),
    ("review_monitor_lib.shell._run_git", _toplevel_review_monitor_shell),
]


@pytest.mark.parametrize(
    "probe", [probe for _name, probe in _PROBES], ids=[name for name, _ in _PROBES]
)
def test_script_git_answers_for_the_real_repo(
    repo: Path, probe: Callable[[Path], str]
) -> None:
    assert probe(repo) == str(repo.resolve())


@pytest.mark.parametrize(
    "name",
    [
        "gate_key",
        "prep_pr_state",
        "prep_pr_finalize",
        "check_changelog_frozen",
        "check_not_main_checkout",
    ],
)
def test_script_strip_helper_drops_every_git_variable(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh")
    monkeypatch.setenv("KEEP_ME", "1")

    env = _script(name)._git_env()

    assert not [key for key in env if key.startswith("GIT_")]
    assert env["KEEP_ME"] == "1"
    assert "GIT_SSH_COMMAND" in os.environ


def test_review_monitor_shell_strip_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/x")

    assert "GIT_DIR" not in _shell()._git_env()


def test_an_unstripped_script_launch_trips_the_raw_git_guard(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a script ever stops stripping, the guard's env check catches it."""
    module = _script("gate_key")
    monkeypatch.setattr(module, "_git_env", lambda: dict(os.environ))
    recorder = _host_probe_guard.LaunchRecorder()
    _git_sandbox.install_raw_git_guard(monkeypatch, recorder, root=tmp_path)

    with pytest.raises(_git_sandbox.RawGitLaunchError, match="GIT_DIR"):
        module.git_common_dir()

    assert [v.kind for v in recorder.violations()] == ["git"]
