"""The sandboxed git runner every unit test launches git through (#3078).

Why: a unit run started inside a git hook inherits the hook's ``GIT_DIR``
(and ``GIT_WORK_TREE``, ``GIT_INDEX_FILE``), and a test-side git launch that
honoured it wrote into the wrong repository -- the shared checkout's
``core.bare`` flipped. Stripping ``GIT_*`` at each call site was a convention;
this module makes it the only way in.

:func:`run` (and :func:`git`, its stripped-stdout form) launches git with
:func:`sandbox_env`:

* every ``GIT_*`` variable stripped, then ``GIT_CEILING_DIRECTORIES`` set to
  the sandbox root, ``GIT_CONFIG_NOSYSTEM=1`` and ``GIT_TERMINAL_PROMPT=0``;
* ``HOME`` kept when it is already under the root (the per-test
  ``tmp_path/_home``), else moved to a root-local home holding the test git
  identity.

Before launching, :func:`validate` refuses -- with
:class:`GitSandboxViolationError` -- a ``cwd`` outside the root and any argument
that names a path outside it: ``-C``, ``--git-dir``, ``--work-tree``,
``--file`` values, ``-c key=/abs`` values, absolute, ``~`` and ``../``
positionals, and ``file://`` URLs. A repo-location variable passed through
``extra_env`` is refused too.

The root is the pytest basetemp, bound once per session by the
``_git_sandbox_root`` fixture in ``tests/conftest.py`` (the same root
``tests/_git_ceiling.py`` confines discovery to). ``integration`` tests run
the runner with containment off: the live codex suite builds repos under a
home-tree base.

Limits, honestly: the path refusal is **lexical**. It does not resolve
symlinks, so a symlink inside the root that points outside is not caught. It
also false-positives on message and pathspec text that looks like a path
(``git commit -m "/x"``); pass such text with ``-F`` or ``stdin=``. The runner
narrows what a test can reach; it is not a security boundary.

:func:`scrub_process_env` removes the six repo-location variables from this
process's environment so bash scripts a test runs (whose git no ``Popen``
guard can see) also start clean. It runs as a side effect of importing this
module, which ``tests/conftest.py`` does next to ``tests._session_home``,
ahead of every ``cw`` import. It honours the same live-run opt-out as the
``HOME`` redirect, and unlike the runner leaves ``GIT_SSH_COMMAND``,
``GIT_ASKPASS`` and ``GIT_EXEC_PATH`` alone for code that relies on them.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, overload

from tests._git_ceiling import CEILING_ENV_VAR
from tests._host_probe_guard import (
    launch_program,
    popen_cwd,
    popen_env,
    popen_executable,
    popen_shell,
)
from tests._session_home import wants_real_home, write_minimal_gitconfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, MutableMapping, Sequence

    import pytest

    from tests._host_probe_guard import LaunchRecorder

# Variables that relocate the repository git operates on. The process-wide
# scrub removes exactly these; the runner strips every ``GIT_*``.
REPO_LOCATION_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
)

# Options whose next argument is a path.
_PATH_VALUE_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree", "--file"})
_FILE_URL = "file://"
_DEV_NULL = "/dev/null"
_SANDBOX_HOME = "_git_sandbox_home"

# ``subprocess.run`` at import, so a test that patches ``subprocess.run`` (to
# fake ``gh``, say) cannot swallow the git calls that build its fixtures. The
# ``Popen.__init__`` hooks (ceiling, lock harness, guards) still see them.
_REAL_RUN = subprocess.run

# Set while :func:`run` is launching, so the raw-git guard lets it through.
_RUNNER_LAUNCH: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "git_sandbox_runner_launch", default=False
)


class GitSandboxViolationError(AssertionError):
    """A runner call would reach outside the sandbox root."""


@dataclass
class _Sandbox:
    root: Path | None = None
    enforced: bool = True


_STATE = _Sandbox()


def bind(root: Path) -> None:
    """Make *root* the sandbox root for every later :func:`run`."""
    _STATE.root = root


def unbind() -> None:
    """Forget the sandbox root; :func:`run` refuses until :func:`bind`."""
    _STATE.root = None


def bound_root() -> Path | None:
    """The current sandbox root, or ``None``."""
    return _STATE.root


def containment_enforced() -> bool:
    """Whether :func:`run` currently validates paths and confines ``HOME``."""
    return _STATE.enforced


@contextlib.contextmanager
def containment(*, enforced: bool) -> Iterator[None]:
    """Run the block with path containment switched on or off."""
    previous = _STATE.enforced
    _STATE.enforced = enforced
    try:
        yield
    finally:
        _STATE.enforced = previous


def in_runner_launch() -> bool:
    """True while :func:`run` is launching git (read by the raw-git guard)."""
    return _RUNNER_LAUNCH.get()


def _strip_git(base: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in base.items() if not k.startswith("GIT_")}


def _lexical(path: Path | str) -> str:
    return os.path.normpath(Path.cwd() / path)


def _inside(path: Path | str, root: Path) -> bool:
    candidate = _lexical(path)
    for base in {_lexical(root), os.path.realpath(root)}:
        if os.path.commonpath([candidate, base]) == base:
            return True
    return False


def _home_for(root: Path, base: Mapping[str, str]) -> str:
    home = base.get("HOME")
    if home and _inside(home, root):
        return home
    sandbox_home = root / _SANDBOX_HOME
    sandbox_home.mkdir(parents=True, exist_ok=True)
    if not (sandbox_home / ".gitconfig").exists():
        write_minimal_gitconfig(sandbox_home)
    return str(sandbox_home)


def sandbox_env(
    root: Path, *, base: Mapping[str, str] | None = None, ceiling: bool = True
) -> dict[str, str]:
    """*base* (``os.environ``) with ``GIT_*`` stripped and git confined to *root*."""
    source: Mapping[str, str] = os.environ if base is None else base
    env = _strip_git(source)
    if ceiling:
        env[CEILING_ENV_VAR] = str(root.resolve())
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["HOME"] = _home_for(root, source)
    return env


def _refuse(arg: str, why: str) -> GitSandboxViolationError:
    return GitSandboxViolationError(f"git sandbox refused {arg!r}: {why}")


def _check_path_text(text: str, cwd: Path, root: Path) -> None:
    """Refuse *text* when it names a path outside *root* (lexically)."""
    if text.startswith(_FILE_URL):
        target = text[len(_FILE_URL) :]
        if not _inside(target, root):
            raise _refuse(text, "file:// URL outside the sandbox root")
        return
    if text.startswith("~"):
        raise _refuse(text, "home-relative path")
    if text.startswith("/"):
        if text != _DEV_NULL and not _inside(text, root):
            raise _refuse(text, "absolute path outside the sandbox root")
        return
    if (text == ".." or text.startswith("../") or "/../" in text) and not _inside(
        cwd / text, root
    ):
        raise _refuse(text, "relative path escapes the sandbox root")


def _check_path_value(option: str, value: str, cwd: Path, root: Path) -> Path:
    """Refuse a path-valued option outside *root*; return the resolved value."""
    target = Path(value) if value.startswith("/") else cwd / value
    if value.startswith("~") or not _inside(target, root):
        shown = f"{option} {value}"
        raise _refuse(shown, "path outside the sandbox root")
    return Path(_lexical(target))


def validate(argv: Sequence[str], *, cwd: Path, root: Path) -> None:
    """Refuse *argv* (git's arguments) if it reaches outside *root* from *cwd*."""
    if not _inside(cwd, root):
        raise _refuse(str(cwd), "cwd outside the sandbox root")
    args = list(argv)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _PATH_VALUE_OPTIONS and index + 1 < len(args):
            resolved = _check_path_value(arg, args[index + 1], cwd, root)
            if arg == "-C":
                cwd = resolved
            index += 2
            continue
        if arg == "-c" and index + 1 < len(args):
            _key, _, value = args[index + 1].partition("=")
            _check_path_text(value, cwd, root)
            index += 2
            continue
        if arg.startswith("-"):
            option, sep, value = arg.partition("=")
            if sep and option in _PATH_VALUE_OPTIONS:
                _check_path_value(option, value, cwd, root)
            elif sep:
                _check_path_text(value, cwd, root)
        else:
            _check_path_text(arg, cwd, root)
        index += 1


def _require_root() -> Path:
    root = _STATE.root
    if root is None:
        msg = "git sandbox: no sandbox root bound (tests/conftest.py binds basetemp)"
        raise GitSandboxViolationError(msg)
    return root


@overload
def run(
    *args: str,
    cwd: Path | str | None = None,
    check: bool = True,
    capture_output: bool = True,
    text: Literal[True] = True,
    timeout: float | None = None,
    stdin: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    ceiling: bool = True,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run(
    *args: str,
    cwd: Path | str | None = None,
    check: bool = True,
    capture_output: bool = True,
    text: Literal[False],
    timeout: float | None = None,
    stdin: bytes | None = None,
    extra_env: Mapping[str, str] | None = None,
    ceiling: bool = True,
) -> subprocess.CompletedProcess[bytes]: ...


def run(
    *args: str,
    cwd: Path | str | None = None,
    check: bool = True,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    stdin: str | bytes | None = None,
    extra_env: Mapping[str, str] | None = None,
    ceiling: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run ``git *args`` confined to the sandbox root (see the module docstring).

    *cwd* defaults to the root; *stdin* is fed to git (``subprocess``'s
    ``input``). *ceiling* ``False`` omits the per-call
    ``GIT_CEILING_DIRECTORIES`` (the suite-wide ``Popen`` ceiling still
    applies).
    """
    root = _require_root()
    workdir = root if cwd is None else Path(cwd)
    leaked = sorted(set(extra_env or {}) & set(REPO_LOCATION_VARS))
    if leaked:
        msg = f"git sandbox refused extra_env {leaked}: it relocates the repository"
        raise GitSandboxViolationError(msg)
    if _STATE.enforced:
        validate(args, cwd=workdir, root=root)
        env = sandbox_env(root, ceiling=ceiling)
    else:
        env = _strip_git(os.environ)
    env.update(extra_env or {})
    token = _RUNNER_LAUNCH.set(True)
    try:
        return _REAL_RUN(
            ["git", *args],
            cwd=workdir,
            check=check,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            input=stdin,
            env=env,
        )
    finally:
        _RUNNER_LAUNCH.reset(token)


def git(repo: Path | str, *args: str) -> str:
    """Run ``git *args`` in *repo* through :func:`run`; return stripped stdout."""
    return run(*args, cwd=repo).stdout.strip()


# --- Raw-git guard ------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_CW = os.path.realpath(_REPO_ROOT / "src" / "cw")
_SCRIPTS = os.path.realpath(_REPO_ROOT / ".claude" / "scripts")

# What a ``src``/``script`` git launch must never carry: these redirect git to
# another repository (the hook leak this module exists for).
ENV_ASSERT_VARS: tuple[str, ...] = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

Origin = Literal["src", "script", "test"]


class RawGitLaunchError(AssertionError):
    """Test code launched git without the runner, or code launched it leaky."""


class _Code(Protocol):
    @property
    def co_filename(self) -> str: ...


class FrameLike(Protocol):
    """The one frame attribute provenance reads (a real frame satisfies it)."""

    @property
    def f_code(self) -> _Code: ...


@functools.lru_cache(maxsize=4096)
def _origin_of(filename: str) -> Origin | None:
    real = os.path.realpath(filename)
    for base, origin in ((_SRC_CW, "src"), (_SCRIPTS, "script")):
        if os.path.commonpath([real, base]) == base:
            return "src" if origin == "src" else "script"
    return None


def classify_origin(frames: Iterable[FrameLike]) -> Origin:
    """``src``/``script`` if ANY frame is ``src/cw`` or ``.claude/scripts`` code.

    Whole-stack, innermost first, so a passthrough wrapper that patches
    ``subprocess.run`` and delegates (test frame -> wrapper -> ``src/cw`` ->
    ``subprocess.py``) is attributed to the ``src`` caller, not the wrapper.
    By design this also allows a test callback that ``src`` invokes.
    """
    for frame in frames:
        origin = _origin_of(frame.f_code.co_filename)
        if origin is not None:
            return origin
    return "test"


def _stack() -> Iterator[FrameLike]:
    frame = inspect.currentframe()
    while frame is not None:
        yield frame
        frame = frame.f_back


def assert_cw_from_checkout() -> None:
    """Fail loudly unless ``cw`` runs from this checkout's ``src/cw``.

    Provenance keys on ``co_filename``; a ``cw`` imported from site-packages
    would make every ``src`` frame look like test code.
    """
    import cw

    location = os.path.realpath(cw.__file__ or "")
    if os.path.commonpath([location, _SRC_CW]) != _SRC_CW:
        msg = (
            f"cw is imported from {location}, not {_SRC_CW}; the raw-git guard "
            "needs the checkout"
        )
        raise AssertionError(msg)


def is_git_launch(
    args: object, kwargs: Mapping[str, object], rest: tuple[object, ...] = ()
) -> bool:
    """Whether ``Popen(args, *rest, **kwargs)`` would run git, in any launch form."""
    program = launch_program(
        args,
        via_shell=popen_shell(rest, kwargs),
        executable=popen_executable(rest, kwargs),
    )
    return program is not None and Path(program).name == "git"


def leaked_env_detail(env: object, origin: str) -> str | None:
    """Why a ``src``/``script`` launch's effective env is unsafe, or ``None``.

    The effective env is *env* when it is a mapping, else ``os.environ``.
    """
    source: Mapping[str, str] = env if isinstance(env, Mapping) else os.environ
    leaked = [name for name in ENV_ASSERT_VARS if name in source]
    if not leaked:
        return None
    return (
        f"raw git: a {origin} git launch carries {leaked}; strip GIT_* "
        "(cw._git.git_clean_env) before launching"
    )


def outside_root_detail(argv: object, cwd: object, root: Path) -> str | None:
    """Why a ``src`` launch runs outside *root* (its cwd or ``-C``), or ``None``."""
    workdir = Path(os.fsdecode(str(cwd))) if cwd is not None else Path.cwd()
    if isinstance(argv, (list, tuple)):
        words = [os.fsdecode(str(word)) for word in argv]
        for index, word in enumerate(words[:-1]):
            if word == "-C":
                workdir = workdir / words[index + 1]
    if _inside(workdir, root):
        return None
    return f"raw git: a src git launch runs in {workdir}, outside the sandbox root"


def install_raw_git_guard(
    monkeypatch: pytest.MonkeyPatch,
    recorder: LaunchRecorder,
    *,
    root: Path,
    enforce_containment: bool = False,
) -> None:
    """Refuse a git launch that bypasses the runner from test code.

    Wraps ``Popen.__init__`` (chaining like ``tests/_git_ceiling.py``). The
    runner's own launches pass. Otherwise a launch is allowed only when some
    frame is ``src/cw`` or ``.claude/scripts`` code, and then only with no
    ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE`` in its effective env;
    *enforce_containment* also requires a ``src`` launch's cwd inside *root*.
    Every refusal raises :class:`RawGitLaunchError` and is recorded, so a broad
    ``except`` cannot hide it.
    """
    assert_cw_from_checkout()
    real_init: Callable[..., None] = subprocess.Popen.__init__

    def _guarded_init(
        self: subprocess.Popen[bytes], args: object, *rest: object, **kwargs: object
    ) -> None:
        if is_git_launch(args, kwargs, rest) and not in_runner_launch():
            detail = _refusal(args, rest, kwargs, root, enforce_containment)
            if detail is not None:
                recorder.record_git(detail)
                raise RawGitLaunchError(detail)
        real_init(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _guarded_init)


def _refusal(
    args: object,
    rest: tuple[object, ...],
    kwargs: Mapping[str, object],
    root: Path,
    enforce_containment: bool,
) -> str | None:
    origin = classify_origin(_stack())
    if origin == "test":
        return (
            "raw git launch from test code; use tests/_git_sandbox.run or "
            "git_in (or mark the test raw_git_allowed)"
        )
    detail = leaked_env_detail(popen_env(rest, kwargs), origin)
    if detail is None and enforce_containment and origin == "src":
        detail = outside_root_detail(args, popen_cwd(rest, kwargs), root)
    return detail


def scrub_process_env(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Drop the repo-location variables from *environ* (``os.environ``).

    A no-op for a live run that opted out of the ``HOME`` redirect
    (``tests._session_home.wants_real_home``). Returns the names removed.
    """
    target: MutableMapping[str, str] = os.environ if environ is None else environ
    if wants_real_home(target):
        return []
    removed = [name for name in REPO_LOCATION_VARS if name in target]
    for name in removed:
        del target[name]
    return removed


# Import-time scrub: see the module docstring. Must run before any test or
# ``cw`` import reads ``os.environ``.
scrub_process_env()
