"""Shared test helpers for the ``cw.worktree`` package's split test files."""

from __future__ import annotations

import inspect
import math
import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.worktree import (
    _fast_forward,
    _freshness,
    _git,
    _lifecycle,
    _liveness,
    _occupancy,
    _paths,
    _refresh,
    _refresh_types,
    _scope,
    _tick_fetch,
    _unsaved,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_SUBMODULES = (
    _git,
    _paths,
    _scope,
    _freshness,
    _unsaved,
    _refresh_types,
    _liveness,
    _occupancy,
    _fast_forward,
    _refresh,
    _lifecycle,
    _tick_fetch,
)


class FakeClock:
    """A monotonic clock a test advances by hand (no sleeps)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class HangingRun:
    """A ``subprocess.run`` stub for a git call that hangs for *hang_for* seconds.

    Each call advances *clock* by the shorter of *hang_for* and the ``timeout``
    it received. A call that would outlast its timeout raises
    ``subprocess.TimeoutExpired``, as ``subprocess.run`` does after killing the
    child; a shorter one returns rc 0. A call with no ``timeout`` fails the test:
    that is the stall the tick fetch gate exists to prevent (#2910).
    """

    def __init__(self, clock: FakeClock, *, hang_for: float) -> None:
        self.clock = clock
        self.hang_for = hang_for
        self.argv: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(
        self, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        if not isinstance(timeout, int | float):
            pytest.fail(f"unbounded network git call: {cmd} (timeout={timeout!r})")
        self.argv.append(cmd)
        self.timeouts.append(float(timeout))
        if self.hang_for < timeout:
            self.clock.advance(self.hang_for)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.clock.advance(timeout)
        raise subprocess.TimeoutExpired(cmd, timeout)


def hanging_run(clock: FakeClock, *, hang_for: float = math.inf) -> HangingRun:
    """A :class:`HangingRun` stub; the default hangs until its timeout fires."""
    return HangingRun(clock, hang_for=hang_for)


def run_git_accepting_timeout(fake: Callable[..., object]) -> Callable[..., object]:
    """Let a ``_run_git`` fake written before #2910 take the ``timeout`` keyword.

    ``cw.worktree._tick_fetch.run_network_git`` passes ``timeout=`` to every
    read-only network call. A fake that accepts it (by name or ``**kwargs``) is
    returned unchanged so it can assert on the value; one that does not gets a
    wrapper that drops it, so no pre-existing fake needs editing.
    """
    try:
        params = inspect.signature(fake).parameters.values()
    except (TypeError, ValueError):
        return fake
    if any(
        p.name == "timeout" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params
    ):
        return fake

    def _drop_timeout(*args: object, **kwargs: object) -> object:
        kwargs.pop("timeout", None)
        return fake(*args, **kwargs)

    return _drop_timeout


def patch_worktree(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    """Patch *name* on every ``cw.worktree`` submodule that binds it.

    Submodules import shared helpers by name (``from cw.worktree._git import
    _run_git``), so each holds its own binding and one ``setattr`` on the
    defining submodule misses callers in the others. Patching every binding
    keeps the pre-split meaning of a single ``cw.worktree.<name>`` patch.

    A ``_run_git`` fake goes through :func:`run_git_accepting_timeout` first,
    the one place the #2910 ``timeout`` keyword is reconciled with fakes that
    predate it.
    """
    bound = [mod for mod in _SUBMODULES if hasattr(mod, name)]
    if not bound:
        msg = f"no cw.worktree submodule binds {name!r}"
        raise AttributeError(msg)
    if name == "_run_git" and callable(value):
        value = run_git_accepting_timeout(value)
    for mod in bound:
        monkeypatch.setattr(mod, name, value)
