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

from typing import TYPE_CHECKING

from cw.config import _load_concurrency_overrides, _save_concurrency_overrides
from cw.models import ConcurrencyOverrides, LaneConcurrencyOverride
from cw.native_daemon import FakeNativeDaemonClient

if TYPE_CHECKING:
    from pathlib import Path

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
