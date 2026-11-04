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
    from collections.abc import Callable, Sequence

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
    """A ``subprocess.run`` stub for a git call that hangs, with a clock to match.

    Two modes, mutually exclusive (passing both raises ``ValueError``):

    - ``hang_for`` (default ``math.inf``, hang until the timeout fires): every
      call takes *hang_for* seconds.
    - ``script``: one entry per call, in order. A float is the seconds that call
      takes; ``None`` hangs until the timeout fires. Running past the end of the
      script raises ``StopIteration`` rather than inventing a default.

    Each call advances *clock* by the shorter of the seconds it takes and the
    ``timeout`` it received. A call that outlasts its timeout raises
    ``subprocess.TimeoutExpired``, as ``subprocess.run`` does after killing the
    child; one that fits returns rc 0. A call with no ``timeout`` fails the
    test: that is the stall the tick fetch gate exists to prevent (#2910).

    Boundary, where the two modes deliberately differ: a ``hang_for`` call of
    exactly its timeout expires (strict ``hang_for < timeout``), while a
    scripted step of exactly its timeout fits (inclusive ``step <= timeout``).
    """

    def __init__(
        self,
        clock: FakeClock,
        *,
        hang_for: float | None = None,
        script: Sequence[float | None] | None = None,
    ) -> None:
        if hang_for is not None and script is not None:
            msg = "hang_for and script are mutually exclusive"
            raise ValueError(msg)
        self.clock = clock
        self.hang_for = math.inf if hang_for is None else hang_for
        self._steps = None if script is None else iter(script)
        self.argv: list[list[str]] = []
        self.timeouts: list[float] = []

    def _fits(self, timeout: float) -> float | None:
        """Seconds the next call takes if it fits *timeout*, else ``None``."""
        if self._steps is None:
            return self.hang_for if self.hang_for < timeout else None
        step = next(self._steps)
        return step if step is not None and step <= timeout else None

    def __call__(
        self, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        if not isinstance(timeout, int | float):
            pytest.fail(f"unbounded network git call: {cmd} (timeout={timeout!r})")
        self.argv.append(cmd)
        self.timeouts.append(float(timeout))
        took = self._fits(timeout)
        if took is not None:
            self.clock.advance(took)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.clock.advance(timeout)
        raise subprocess.TimeoutExpired(cmd, timeout)


def hanging_run(
    clock: FakeClock,
    *,
    hang_for: float | None = None,
    script: Sequence[float | None] | None = None,
) -> HangingRun:
    """A :class:`HangingRun` stub; with neither argument it hangs until timeout."""
    return HangingRun(clock, hang_for=hang_for, script=script)


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
