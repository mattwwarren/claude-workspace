"""The pre-push diff-cover hook must not leak git's ``GIT_*`` env into pytest (#3076).

git exports ``GIT_DIR`` into a ``pre-push`` hook run from a linked worktree.
The hook's pytest run inherits it, and every test that shells out to git
without a stripped env then operates on the pushing worktree's repository
instead of its own ``tmp_path`` repo. The hook entry therefore unsets
``GIT_DIR``, ``GIT_WORK_TREE`` and ``GIT_INDEX_FILE`` first.

Both tests run the real ``entry`` read from ``.pre-commit-config.yaml``, not a
copy, with a stub ``uv`` first on ``PATH`` that logs its argv and the three
names it can see, so the real pytest and diff-cover never run.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import TYPE_CHECKING

from tests._git_sandbox import run as sandbox_run
from tests.conftest import _clean_git_env, add_bare_origin, git_in
from tests.test_ci_workflow import DIFF_COVER_HOOK_ID, _hook

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_LEAK_NAMES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
_UV_CALLS = 2
_STUB = """\
#!/usr/bin/env bash
{{
  printf 'ARGV %s\\n' "$*"
  env | grep -E '^GIT_(DIR|WORK_TREE|INDEX_FILE)=' || true
}} >> {log}
exit 0
"""


def _entry_argv() -> list[str]:
    return shlex.split(_hook(DIFF_COVER_HOOK_ID)["entry"])


def _stub_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """A ``GIT_*``-free env whose ``PATH`` resolves ``uv`` to a logging stub."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    log = tmp_path / "uv.log"
    stub = stub_dir / "uv"
    stub.write_text(_STUB.format(log=shlex.quote(str(log))), encoding="utf-8")
    stub.chmod(0o755)
    env = _clean_git_env()
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
    return env, log


def _linked_worktree(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> tuple[Path, Path, Path]:
    """``(repo, origin, worktree)``: a repo with a bare origin and a linked worktree."""
    repo = make_git_repo("r")
    origin = add_bare_origin(repo)
    worktree = tmp_path / "w"
    git_in(repo, "worktree", "add", "-b", "feat", str(worktree))
    return repo, origin, worktree


def _logged(log: Path) -> tuple[list[str], list[str]]:
    """``(argv lines, leaked GIT_* lines)`` the stub recorded."""
    lines = log.read_text(encoding="utf-8").splitlines()
    argvs = [line for line in lines if line.startswith("ARGV ")]
    leaks = [line for line in lines if line.startswith(_LEAK_NAMES)]
    return argvs, leaks


def test_hook_entry_strips_git_env_before_uv(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    repo, _origin, worktree = _linked_worktree(make_git_repo, tmp_path)
    env, log = _stub_env(tmp_path)
    gitdir = repo / ".git" / "worktrees" / worktree.name
    env |= {
        "GIT_DIR": str(gitdir),
        "GIT_WORK_TREE": str(worktree),
        "GIT_INDEX_FILE": str(gitdir / "index"),
    }

    # Control: the stub itself does see the three names when they are set.
    subprocess.run([str(tmp_path / "stub-bin" / "uv")], env=env, check=True)
    _argvs, control_leaks = _logged(log)
    assert {line.split("=", 1)[0] for line in control_leaks} == set(_LEAK_NAMES)
    log.unlink()

    done = subprocess.run(
        _entry_argv(),
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    argvs, leaks = _logged(log)
    assert len(argvs) == _UV_CALLS, argvs
    assert leaks == []


def test_real_push_from_linked_worktree_does_not_leak_git_env(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    repo, origin, worktree = _linked_worktree(make_git_repo, tmp_path)
    env, log = _stub_env(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        f"#!/usr/bin/env bash\nexec {shlex.join(_entry_argv())}\n", encoding="utf-8"
    )
    hook.chmod(0o755)
    git_in(worktree, "commit", "--allow-empty", "-m", "feat")

    done = sandbox_run(
        "-C",
        str(worktree),
        "push",
        "origin",
        "HEAD:refs/heads/feat",
        extra_env={"PATH": env["PATH"]},
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert git_in(origin, "rev-parse", "refs/heads/feat") == git_in(
        worktree, "rev-parse", "HEAD"
    )
    argvs, leaks = _logged(log)
    assert len(argvs) == _UV_CALLS, argvs
    assert leaks == []
