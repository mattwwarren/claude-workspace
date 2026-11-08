"""The unit tier survives running inside a real ``pre-push`` hook (#3078, C5).

A pre-push hook in a linked worktree runs with ``GIT_DIR`` exported, pointing
at that worktree's admin dir. A unit run started from such a hook once flipped
``core.bare`` on the shared repository: some test-side git launch inherited
the hook's ``GIT_DIR`` and wrote config into the wrong repository.

This test builds the whole scene in ``tmp_path`` -- a bare ``origin``, a
checkout, a linked worktree and a ``pre-push`` hook in the shared
``common-dir/hooks`` -- and pushes through the sandboxed runner. The hook runs
a git-heavy slice of the unit suite.

The nested run's ``tests/conftest.py`` scrubs ``GIT_DIR`` from ``os.environ``
on import, which alone would hide the leak condition. So a small plugin,
written into ``tmp_path`` and loaded with ``-p``, puts the hook's ``GIT_DIR``
back before every nested test's setup (``tryfirst``, ahead of every fixture)
and restores it after teardown. Every nested test, including its
``git_in``/``make_git_repo`` setup, therefore runs with a leaked ``GIT_DIR``,
and the guard and runner -- not just the scrub -- are what keep it contained.

Integration-marked: it shells out to a nested pytest and takes minutes, so it
runs under ``-m integration`` only. The nested run passes ``-m "not
integration"``, which keeps it from re-entering this test.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

from tests import _git_sandbox
from tests._session_home import OPT_OUT_ENV_VARS

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Git-heavy unit files the hook runs. ``-m "not integration"`` keeps the nested
# run from re-entering this module.
SLICE: tuple[str, ...] = (
    "tests/test_git.py",
    "tests/test_worktree_freshness.py",
    "tests/test_worktree_refresh.py",
    "tests/test_merge_probe.py",
    "tests/test_git_ceiling.py",
    "tests/test_git_sandbox.py",
    "tests/test_mechanical_repair_real_git.py",
)

_NESTED_TIMEOUT_S = 540

# Loaded into the nested pytest with ``-p c5_reinject``. Re-injects the hook's
# GIT_DIR before every test's setup and restores the prior value after its
# teardown; counts the tests whose call phase saw it.
_PLUGIN = textwrap.dedent(
    """
    from __future__ import annotations

    import os
    from pathlib import Path

    import pytest

    _UNSET = object()
    _SAVED: list[object] = []


    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(item: pytest.Item) -> None:
        _SAVED.append(os.environ.get("GIT_DIR", _UNSET))
        os.environ["GIT_DIR"] = os.environ["C5_HOOK_GIT_DIR"]


    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(item: pytest.Item) -> None:
        if os.environ.get("GIT_DIR") == os.environ["C5_HOOK_GIT_DIR"]:
            with Path(os.environ["C5_SEEN_FILE"]).open("a") as seen:
                seen.write(item.nodeid + "\\n")


    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(item: pytest.Item, nextitem: object) -> object:
        try:
            return (yield)
        finally:
            previous = _SAVED.pop() if _SAVED else _UNSET
            if previous is _UNSET:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = str(previous)
    """
)

_HOOK = textwrap.dedent(
    """\
    #!/bin/sh
    env | grep '^GIT_DIR=' > "{marker}"
    C5_HOOK_GIT_DIR=$(cd "$GIT_DIR" && pwd)
    export C5_HOOK_GIT_DIR
    unset {opt_out}
    PYTHONPATH="{plugin_dir}" "{python}" -m pytest {slice} \\
        --rootdir "{root}" -c "{root}/pyproject.toml" \\
        -p no:cacheprovider -p c5_reinject -m "not integration" \\
        --basetemp="{basetemp}" -q > "{log}" 2>&1
    code=$?
    echo "$code" > "{code_file}"
    exit "$code"
    """
)


def _host_common_dir(root: Path) -> Path:
    """The host checkout's git common dir, read from disk (no git launch)."""
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    gitdir = Path(dot_git.read_text(encoding="utf-8").split(":", 1)[1].strip())
    commondir = gitdir / "commondir"
    if commondir.is_file():
        return (gitdir / commondir.read_text(encoding="utf-8").strip()).resolve()
    return gitdir


def _core_bare(config: Path) -> str | None:
    """The last ``core.bare`` value in a git config file, else ``None`` (read-only).

    A plain parse of the ``[core]`` section: the sandbox runner refuses paths
    outside the test root, and this must not launch git against the host repo.
    A bare ``bare`` key (no ``=``) is git's implicit true.
    """
    in_core = False
    value: str | None = None
    for raw in config.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_core = line.split("]", 1)[0].strip("[] \t").lower() == "core"
            continue
        if not in_core or not line or line[0] in "#;":
            continue
        key, sep, rest = line.partition("=")
        if key.strip().lower() == "bare":
            value = rest.split("#", 1)[0].split(";", 1)[0].strip() if sep else "true"
    return value


def _write_hook(scratch: Path, common: Path) -> dict[str, Path]:
    files = {
        "marker": scratch / "hook-marker",
        "log": scratch / "nested.log",
        "code_file": scratch / "nested.code",
        "seen": scratch / "nested.seen",
    }
    plugin_dir = scratch / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "c5_reinject.py").write_text(_PLUGIN, encoding="utf-8")
    hook = common / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        _HOOK.format(
            marker=files["marker"],
            opt_out=" ".join(OPT_OUT_ENV_VARS),
            plugin_dir=plugin_dir,
            python=sys.executable,
            slice=" ".join(f'"{_REPO_ROOT / path}"' for path in SLICE),
            root=_REPO_ROOT,
            basetemp=scratch / "nested",
            log=files["log"],
            code_file=files["code_file"],
        ),
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return files


def test_unit_slice_inside_a_pre_push_hook_leaves_every_repo_config_alone(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    worktree = tmp_path / "linked"
    _git_sandbox.run("init", "-q", "--bare", "-b", "main", str(origin))
    checkout.mkdir()
    _git_sandbox.run("init", "-q", "-b", "main", cwd=checkout)
    _git_sandbox.run("commit", "--allow-empty", "-q", "-m", "init", cwd=checkout)
    _git_sandbox.run("remote", "add", "origin", str(origin), cwd=checkout)
    _git_sandbox.run("push", "-q", "origin", "main", cwd=checkout)
    _git_sandbox.run(
        "worktree", "add", "-q", "-b", "feature", str(worktree), cwd=checkout
    )
    _git_sandbox.run("commit", "--allow-empty", "-q", "-m", "feat", cwd=worktree)

    common = checkout / ".git"
    files = _write_hook(tmp_path, common)
    host_config = _host_common_dir(_REPO_ROOT) / "config"
    before = {
        "scratch": (common / "config").read_bytes(),
        "origin": (origin / "config").read_bytes(),
    }
    host_bare_before = _core_bare(host_config)

    pushed = _git_sandbox.run(
        "push",
        "-q",
        "origin",
        "feature",
        cwd=worktree,
        check=False,
        timeout=_NESTED_TIMEOUT_S,
        extra_env={"C5_SEEN_FILE": str(files["seen"])},
    )

    log = files["log"].read_text(encoding="utf-8") if files["log"].exists() else ""
    assert pushed.returncode == 0, f"{pushed.stderr}\n--- nested ---\n{log[-4000:]}"
    assert files["marker"].read_text(encoding="utf-8").startswith("GIT_DIR=")
    assert files["code_file"].read_text(encoding="utf-8").strip() == "0"
    assert files["seen"].read_text(encoding="utf-8").strip(), (
        "GIT_DIR never re-injected"
    )
    assert (common / "config").read_bytes() == before["scratch"]
    assert (origin / "config").read_bytes() == before["origin"]
    # core.bare only, not the whole file: cw writes the shared config itself
    # (``git worktree add -b`` records upstream tracking), so a byte comparison
    # flakes under concurrent sessions. The leak this guards flips core.bare.
    assert _core_bare(host_config) == host_bare_before
    assert _git_sandbox.git(checkout, "config", "--bool", "core.bare") == "false"
    assert os.environ.get("GIT_DIR") is None
