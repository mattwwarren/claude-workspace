"""Keep unit tests from reading host state they do not own (#2664, #3078).

Two probes escaped the suite's existing optional-binary isolation and made
tests pass or fail according to the machine they ran on:

* ``opencode run --help`` (``cw.opencode_runner._supports_v1_flags``) is a bare
  ``subprocess.run`` -- ``shutil.which`` masking never sees it, so an installed
  opencode 1.x answered it and put ``--pure``/``--dir`` into an argv a test
  asserted was flag-free.
* ``psutil.process_iter`` (``cw.reconcile.codex_boot._codex_processes_in``)
  scans the *whole* host table and fails closed (``None``, inconclusive) when
  any process is unreadable. A single foreign or zombie ``codex`` with no
  readable cwd therefore parked every scan on the host, whatever the test
  meant to prove.

This module holds the logic; ``tests/_shared/autouse_isolation.py`` wires it
into autouse fixtures, the same split as ``tests/_git_ceiling.py``.

Tool launch guard
-----------------

:func:`install_binary_launch_guard` refuses, process-wide, a launch of any
tool in :data:`GUARDED_TOOLS` (``gh``, ``claude``, ``codex``, ``opencode``,
``uv``, ``pre-commit``) whose binary lies outside the test root. It covers:

* ``subprocess.Popen.__init__`` (the layer ``tests/_git_ceiling.py`` and
  ``tests/_lock_invariants.py`` hook; ``run``, ``check_output`` and
  ``check_call`` all funnel through it), including ``shell=True`` strings
  (judged by their first word, even when ``executable=`` swaps the shell), the
  ``executable=`` option of a non-shell launch, and bytes or ``PathLike`` argv.
  ``executable``, ``shell`` and ``cwd`` count whether passed by keyword or
  positionally;
* ``os.exec*``, ``os.spawn*``, ``os.posix_spawn*`` and ``os.system``.

A bare name is resolved with the real ``shutil.which`` (captured at import,
before the autouse masking) against the child's effective ``PATH``; a path is
resolved with ``realpath``. A binary inside the test root -- a test's own
stub from ``tests/_tool_fakes.py`` -- runs; anything else, including a bare
name that resolves nowhere, raises :class:`ToolLaunchBlockedError`. That subclasses
``FileNotFoundError(ENOENT)``, which is what a CI runner without the binary
raises, so code written for the absent case behaves identically here.

Every block is also recorded in a :class:`LaunchRecorder`, and the autouse
teardown (:func:`finish`) fails the test on any record, so code under test
that swallows the ``OSError`` (``cw.gh.runner`` does) cannot hide an unmocked
launch. A test that exercises the absent-tool path on purpose says so with
``@pytest.mark.tool_launch_expected("codex", ...)`` (no names: every tool).
The same recorder carries ``tests/_git_sandbox.py``'s raw-git refusals,
exempted by ``@pytest.mark.raw_git_allowed`` instead.

``@pytest.mark.binary_on_path`` only forces ``shutil.which``; a forced-present
tool must still be mocked at its seam, and the block message says so.

The guard raises *before* delegating, which is safe: ``Popen._child_created``
is a class attribute (``False``), so the half-built object's ``__del__`` has
nothing to clean up. The ``os`` patches skip their check while a guarded
``Popen`` is delegating (``subprocess`` may call ``os.posix_spawn`` itself),
so one launch is judged, and recorded, once.

Process table
-------------

:func:`empty_process_table` is the suite-wide default for
``psutil.process_iter``. A test that wants the real reader scopes its results
to pids it owns with :func:`scoped_process_iter`, which wraps the real
``process_iter`` (captured at import, before any patch) and filters by pid:
the real scan still enumerates the table, but what it returns -- and so what
the code under test can conclude -- never depends on other host processes.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import inspect
import os
import shlex
import shutil
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Literal, Protocol

import psutil

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator

    import pytest

# ``psutil.process_iter`` as it was when this module was first imported, i.e.
# before any fixture or test patched it. ``scoped_process_iter`` reads through
# this, so it keeps working while the autouse default is the empty table.
REAL_PROCESS_ITER = psutil.process_iter

# ``shutil.which`` before the autouse ``_hide_optional_binaries`` masking, so a
# bare name resolves where the real launch would find it.
REAL_WHICH = shutil.which

# The ``attrs`` types ``psutil.process_iter`` itself accepts.
ProcessAttrs = list[str] | tuple[str, ...] | set[str] | frozenset[str]

# External CLIs the unit tier must never launch for real, mapped to the shared
# fake the block message points at.
FAKE_FOR_TOOL: Mapping[str, str] = {
    "gh": "FakeGhCli, stub_gh",
    "claude": "FakeNativeDaemonClient, fake_claude_cli",
    "codex": "fake_codex_job_runner, FakeFireAndForgetRunner",
    "opencode": "fake_opencode_cli",
    "uv": "fake_uv_cli",
    "pre-commit": "fake_pre_commit_cli",
}
GUARDED_TOOLS: frozenset[str] = frozenset(FAKE_FOR_TOOL)

TOOL_MARKER = "tool_launch_expected"
RAW_GIT_MARKER = "raw_git_allowed"

# ``Popen``'s ``env`` is its 11th positional parameter counting ``args``.
_POSITIONAL_ENV_REST_LEN = 10

# Indices into the positional tuple after ``args`` (``bufsize, executable,
# stdin, stdout, stderr, preexec_fn, close_fds, shell, cwd, env``).
_POSITIONAL_EXECUTABLE_REST_INDEX = 1
_POSITIONAL_SHELL_REST_INDEX = 7
_POSITIONAL_CWD_REST_INDEX = 8

# True while a guarded launch is delegating, so a nested ``os`` call made by
# the real implementation (``subprocess`` -> ``os.posix_spawn``, ``execvp`` ->
# ``execv``) is not judged, or recorded, a second time.
_DELEGATING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "host_probe_guard_delegating", default=False
)


class ToolLaunchBlockedError(FileNotFoundError):
    """A guarded tool was launched for real in the unit tier."""


@dataclass(frozen=True, slots=True)
class LaunchViolation:
    """One refused launch (a guarded tool, raw git) or real ``.cw/`` write."""

    kind: Literal["tool", "git", "cw_write"]
    name: str
    detail: str


class LaunchRecorder:
    """Thread-safe record of every launch the guards refused during a test."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._violations: list[LaunchViolation] = []

    def record_tool(self, name: str, detail: str) -> None:
        with self._mutex:
            self._violations.append(LaunchViolation("tool", name, detail))

    def record_git(self, detail: str) -> None:
        with self._mutex:
            self._violations.append(LaunchViolation("git", "git", detail))

    def record_cw_write(self, detail: str) -> None:
        """Record a write ``tests/_real_cw_guard.py`` refused (#3113)."""
        with self._mutex:
            self._violations.append(LaunchViolation("cw_write", ".cw", detail))

    def violations(self) -> list[LaunchViolation]:
        with self._mutex:
            return list(self._violations)

    def drain(self) -> list[LaunchViolation]:
        """Return the recorded violations and clear them.

        For a test that provokes a refusal on purpose and consumes it itself,
        so teardown (``finish``) sees nothing left to fail on.
        """
        with self._mutex:
            drained = list(self._violations)
            self._violations.clear()
            return drained


@contextlib.contextmanager
def delegating() -> Iterator[None]:
    """Mark the block as a guarded launch already judged (see ``_DELEGATING``)."""
    token = _DELEGATING.set(True)
    try:
        yield
    finally:
        _DELEGATING.reset(token)


def launch_program(args: object, *, via_shell: bool, executable: object) -> str | None:
    """The program a launch would run, as text, or ``None`` if unknowable.

    Without ``shell=True``, ``executable=`` replaces the program. With it,
    ``executable=`` only replaces ``/bin/sh``: the shell still runs the command
    string, so the program is that string's first word.
    """
    if executable is not None and not via_shell:
        args = [executable]
    if isinstance(args, (list, tuple)):
        if not args:
            return None
        first: object = args[0]
    else:
        first = args
    if not isinstance(first, (str, bytes, os.PathLike)):
        return None
    text = os.fsdecode(first)
    if not via_shell:
        return text
    try:
        words = shlex.split(text)
    except ValueError:
        return None
    return words[0] if words else None


def _effective_path(env: Mapping[str, str] | None) -> str:
    source: Mapping[str, str] = os.environ if env is None else env
    return source.get("PATH", os.defpath)


def blocked_binary(
    args: object,
    denylist: Collection[str],
    *,
    root: Path,
    env: Mapping[str, str] | None = None,
    cwd: object = None,
    via_shell: bool = False,
    executable: object = None,
) -> str | None:
    """Return the guarded tool *args* would launch from outside *root*, else ``None``.

    *env* and *cwd* are the child's (``None``: this process's), *via_shell*
    is ``shell=True`` and *executable* the ``executable=`` keyword.
    """
    program = launch_program(args, via_shell=via_shell, executable=executable)
    if program is None:
        return None
    name = PurePath(program).name
    if name not in denylist:
        return None
    if os.sep in program:
        base = Path.cwd() if cwd is None else Path(os.fsdecode(str(cwd)))
        resolved: str | None = os.path.realpath(base / program)
    else:
        resolved = REAL_WHICH(program, path=_effective_path(env))
    if resolved is None:
        return name
    real_root = os.path.realpath(root)
    inside = os.path.commonpath([os.path.realpath(resolved), real_root]) == real_root
    return None if inside else name


def block_message(name: str, forced_present: Collection[str]) -> str:
    """The ``ToolLaunchBlockedError`` text for *name*."""
    fake = FAKE_FOR_TOOL.get(name, "a fake")
    message = (
        f"launch of {name!r} blocked in the unit tier; use the shared fake in "
        f"tests/_tool_fakes.py ({fake})"
    )
    if name in forced_present:
        message += "; binary_on_path forces shutil.which only, so mock the launch"
    return message


def _launch_site() -> str:
    """The innermost ``cw.*`` caller (else the innermost test frame), for messages."""
    frame = inspect.currentframe()
    fallback = "unknown"
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if isinstance(module, str):
            where = f"{module}.{frame.f_code.co_name}:{frame.f_lineno}"
            if module.startswith("cw."):
                return where
            if module.startswith("tests.test") and fallback == "unknown":
                fallback = where
        frame = frame.f_back
    return fallback


def popen_env(rest: tuple[object, ...], kwargs: Mapping[str, object]) -> object:
    """The ``env`` a ``Popen.__init__`` call received, keyword or positional."""
    if len(rest) >= _POSITIONAL_ENV_REST_LEN:
        return rest[_POSITIONAL_ENV_REST_LEN - 1]
    return kwargs.get("env")


def _popen_option(
    rest: tuple[object, ...], kwargs: Mapping[str, object], name: str, index: int
) -> object:
    """A ``Popen.__init__`` option, by keyword or else by its position in *rest*."""
    if name in kwargs:
        return kwargs[name]
    return rest[index] if len(rest) > index else None


def popen_shell(rest: tuple[object, ...], kwargs: Mapping[str, object]) -> bool:
    """Whether a ``Popen.__init__`` call is ``shell=True``, keyword or positional."""
    return bool(_popen_option(rest, kwargs, "shell", _POSITIONAL_SHELL_REST_INDEX))


def popen_cwd(rest: tuple[object, ...], kwargs: Mapping[str, object]) -> object:
    """The ``cwd`` a ``Popen.__init__`` call received, keyword or positional."""
    return _popen_option(rest, kwargs, "cwd", _POSITIONAL_CWD_REST_INDEX)


def popen_executable(rest: tuple[object, ...], kwargs: Mapping[str, object]) -> object:
    """The ``executable`` a ``Popen.__init__`` call received, keyword or positional."""
    return _popen_option(rest, kwargs, "executable", _POSITIONAL_EXECUTABLE_REST_INDEX)


class _Guard:
    """The launch check shared by the ``Popen`` wrapper and the ``os`` patches."""

    def __init__(
        self,
        denylist: Collection[str],
        forced_present: Collection[str],
        root: Path,
        recorder: LaunchRecorder | None,
    ) -> None:
        self.denylist = frozenset(denylist)
        self.forced_present = frozenset(forced_present)
        self.root = root
        self.recorder = recorder

    def check(
        self,
        args: object,
        *,
        env: object = None,
        cwd: object = None,
        via_shell: bool = False,
        executable: object = None,
    ) -> None:
        mapping_env = env if isinstance(env, Mapping) else None
        name = blocked_binary(
            args,
            self.denylist,
            root=self.root,
            env=mapping_env,
            cwd=cwd,
            via_shell=via_shell,
            executable=executable,
        )
        if name is None:
            return
        message = f"{block_message(name, self.forced_present)} [from {_launch_site()}]"
        if self.recorder is not None:
            self.recorder.record_tool(name, message)
        raise ToolLaunchBlockedError(errno.ENOENT, message, name)


def _install_popen(monkeypatch: pytest.MonkeyPatch, guard: _Guard) -> None:
    real_init: Callable[..., None] = subprocess.Popen.__init__

    def _guarded_init(
        self: subprocess.Popen[bytes], args: object, *rest: object, **kwargs: object
    ) -> None:
        guard.check(
            args,
            env=popen_env(rest, kwargs),
            cwd=popen_cwd(rest, kwargs),
            via_shell=popen_shell(rest, kwargs),
            executable=popen_executable(rest, kwargs),
        )
        with delegating():
            real_init(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _guarded_init)


# ``os`` launch functions: name -> (index of the program arg, index of the env
# arg or ``None``). ``execl*``/``spawnl*`` delegate to these at call time.
_OS_LAUNCHERS: dict[str, tuple[int, int | None]] = {
    "execv": (0, None),
    "execve": (0, 2),
    "execvp": (0, None),
    "execvpe": (0, 2),
    "spawnv": (1, None),
    "spawnve": (1, 3),
    "spawnvp": (1, None),
    "spawnvpe": (1, 3),
    "posix_spawn": (0, 2),
    "posix_spawnp": (0, 2),
}


def _wrap_os_launcher(
    name: str, guard: _Guard, real: Callable[..., object]
) -> Callable[..., object]:
    program_at, env_at = _OS_LAUNCHERS[name]

    def _guarded(*args: object, **kwargs: object) -> object:
        if not _DELEGATING.get():
            env = args[env_at] if env_at is not None and len(args) > env_at else None
            guard.check([args[program_at]], env=env)
        with delegating():
            return real(*args, **kwargs)

    return _guarded


def _wrap_os_system(guard: _Guard, real: Callable[..., int]) -> Callable[..., int]:
    def _guarded(command: object) -> int:
        if not _DELEGATING.get():
            guard.check(command, via_shell=True)
        with delegating():
            return real(command)

    return _guarded


def install_binary_launch_guard(
    monkeypatch: pytest.MonkeyPatch,
    denylist: Collection[str],
    forced_present: Collection[str],
    *,
    root: Path,
    recorder: LaunchRecorder | None = None,
) -> None:
    """Refuse every launch of a *denylist* tool whose binary is outside *root*.

    *forced_present* only enriches the message (``binary_on_path`` forces
    ``shutil.which``, never a launch). Installs chain with the other
    ``Popen.__init__`` wrappers: this one runs first when installed last, and
    delegates to whatever was there before.
    """
    guard = _Guard(denylist, forced_present, root, recorder)
    _install_popen(monkeypatch, guard)
    for name in _OS_LAUNCHERS:
        real = getattr(os, name, None)
        if real is not None:
            monkeypatch.setattr(os, name, _wrap_os_launcher(name, guard, real))
    monkeypatch.setattr(os, "system", _wrap_os_system(guard, os.system))


class MarkedNode(Protocol):
    """The one ``pytest.Item`` method :func:`finish` reads."""

    def iter_markers(self, name: str | None = None) -> Iterator[pytest.Mark]: ...


def _expected_tools(node: MarkedNode) -> frozenset[str] | None:
    """Tools named by every ``tool_launch_expected`` marker; ``None`` = all."""
    names: set[str] = set()
    found = False
    for marker in node.iter_markers(TOOL_MARKER):
        found = True
        if not marker.args:
            return None
        names.update(str(arg) for arg in marker.args)
    return frozenset(names) if found else frozenset()


def finish(recorder: LaunchRecorder, node: MarkedNode) -> None:
    """Fail the test on any recorded launch its markers do not expect."""
    expected = _expected_tools(node)
    raw_git_ok = any(True for _ in node.iter_markers(RAW_GIT_MARKER))
    failures = [
        violation
        for violation in recorder.violations()
        if not (
            (violation.kind == "git" and raw_git_ok)
            or (
                violation.kind == "tool"
                and (expected is None or violation.name in expected)
            )
        )
    ]
    if failures:
        lines = "\n".join(f"{v.kind}: {v.detail}" for v in failures)
        msg = (
            "refused launches recorded during the test (a caught OSError does "
            f"not hide them; mark deliberate ones):\n{lines}"
        )
        raise AssertionError(msg)


def empty_process_table(
    _attrs: ProcessAttrs | None = None, _ad_value: object = None
) -> Iterator[psutil.Process]:
    """Stand in for ``psutil.process_iter`` over a host with no processes."""
    return iter(())


def scoped_process_iter(
    pids: Collection[int],
) -> Callable[..., Iterator[psutil.Process]]:
    """Return a ``process_iter`` whose results are scoped to the owned *pids*.

    The real iterator still runs -- real kernel entries, real ``.info`` -- but
    only processes whose pid is in *pids* are yielded. A pid that is not in the
    table is simply absent.
    """
    owned = frozenset(pids)

    def _scoped(
        attrs: ProcessAttrs | None = None, ad_value: object = None
    ) -> Iterator[psutil.Process]:
        for process in REAL_PROCESS_ITER(attrs, ad_value):
            if process.pid in owned:
                yield process

    return _scoped
