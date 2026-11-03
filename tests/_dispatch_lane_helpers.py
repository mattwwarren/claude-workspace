"""Shared lane circuit-breaker helpers for the dispatch tests (#875, #2696).

Moved out of ``tests/test_dispatch.py`` so ``tests/test_dispatch_lane_circuit.py``
(the 1:1 test module of ``cw.dispatch.lane_circuit``) can seed and read the
same per-lane override without importing from a 21k-line test module. Plain
functions and constants only; no ``test_`` prefix, so pytest does not collect it
(same convention as ``tests/_dispatch_gating_helpers.py``). The
``breaker_config`` fixture lives in ``tests/conftest.py`` instead: a fixture
imported from a non-conftest module trips F401/F811.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from cw.config import _load_concurrency_overrides, _save_concurrency_overrides
from cw.models import ConcurrencyOverrides, LaneConcurrencyOverride
from cw.native_daemon import FakeNativeDaemonClient, RealNativeDaemonClient

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_BREAKER_LANE_KEY = "test-client/default"


def _save_lane_override(override: LaneConcurrencyOverride) -> None:
    """Persist *override* as the only lane override (the default test-client lane)."""
    _save_concurrency_overrides(
        ConcurrencyOverrides(lanes={_BREAKER_LANE_KEY: override})
    )


def _seed_lane_override(count: int, *, paused: bool | None = None) -> None:
    """Persist a LaneConcurrencyOverride for the default test-client lane."""
    _save_lane_override(
        LaneConcurrencyOverride(consecutive_spawn_errors=count, paused=paused)
    )


def _lane_override() -> LaneConcurrencyOverride:
    """Read back the persisted default test-client lane override."""
    return _load_concurrency_overrides().lanes[_BREAKER_LANE_KEY]


class _RaisingNativeDaemon(FakeNativeDaemonClient):
    """Native daemon whose ``spawn_bg`` always raises the configured exception.

    Used to exercise the spawn-failure containment added for issue #149:
    a single failure must not crash dispatch_tick.
    """

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def spawn_bg(
        self,
        *,
        cwd: Path,
        prompt: str,
        extra_args: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> str:
        self.spawn_calls.append((cwd, prompt))
        raise self._exc


class _RealClientNativeDaemon(FakeNativeDaemonClient):
    """Fake daemon whose ``spawn_bg`` runs the real client's ``spawn_bg`` (#2722).

    Roster, list and stop methods stay the inherited Fake ones, so a tick never
    touches the host roster. Only the spawn path is real, so the real
    ``FileNotFoundError`` -> typed-error conversion is exercised end to end
    (pair with ``_fail_claude_subprocess``). Records ``spawn_calls`` first, like
    ``_RaisingNativeDaemon``, so a test can assert no new spawn was attempted.
    """

    def __init__(self, roster_path: Path) -> None:
        super().__init__()
        self._real = RealNativeDaemonClient(roster_path=roster_path)

    def spawn_bg(
        self,
        *,
        cwd: Path,
        prompt: str,
        extra_args: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> str:
        self.spawn_calls.append((cwd, prompt))
        return self._real.spawn_bg(
            cwd=cwd,
            prompt=prompt,
            extra_args=extra_args,
            permission_mode=permission_mode,
        )


def _fail_claude_subprocess(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    """Make ``subprocess.run`` raise *exc* for a ``claude`` argv, real otherwise.

    The real run is captured before patching, so the tick's own git calls still
    execute. ``claude`` is not in the optional-binary denylist.
    """
    real_run = subprocess.run

    def _run(*args: Any, **kwargs: Any) -> Any:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list | tuple) and argv and argv[0] == "claude":
            raise exc
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)
