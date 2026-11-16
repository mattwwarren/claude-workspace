"""Test-local clock that advances only while a held child process is in flight.

A ``Hold`` is a marker/release file pair. The child shell blocks on the
release file; it creates the marker first and removes it on exit. A
``HoldClock`` replaces a module's ``time`` attribute: each read taken while
the marker exists advances the clock by ``step``, and after ``polls`` such
reads it creates the release file so the child can finish. Wall-clock time
never enters the picture, so timing tests no longer race real sleeps (#3585).
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

_MAX_HOLD_POLLS = 3000
_HOLD_POLL_S = 0.02
_WAIT_POLL_S = 0.02
_DEFAULT_DEADLINE_S = 30.0


@dataclass(frozen=True)
class Hold:
    """Marker/release file pair a child blocks on until a clock releases it."""

    directory: Path

    @property
    def marker(self) -> Path:
        return self.directory / "hold.marker"

    @property
    def release(self) -> Path:
        return self.directory / "hold.release"

    def shell(self, then: str = "") -> str:
        """Return a ``sh`` snippet that blocks until released, then runs *then*.

        The loop is bounded (about 60 s), so an orphaned child self-terminates.
        """
        rel = shlex.quote(str(self.release))
        mark = shlex.quote(str(self.marker))
        body = (
            f"rm -f {rel}; : > {mark}; i=0; "
            f"while [ ! -e {rel} ] && [ $i -lt {_MAX_HOLD_POLLS} ]; "
            f"do sleep {_HOLD_POLL_S}; i=$((i+1)); done; rm -f {mark}"
        )
        return f"{body}; {then}" if then else body

    def config(self) -> list[str]:
        """Return ``[marker, release]`` for the ``PrepPrStateStub`` side file."""
        return [str(self.marker), str(self.release)]


def wait_for_path(path: Path, deadline_s: float = _DEFAULT_DEADLINE_S) -> bool:
    """Poll every 0.02 s until *path* exists or the deadline passes."""
    deadline = time.monotonic() + deadline_s
    while not path.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(_WAIT_POLL_S)
    return True


class HoldClock:
    """Fake ``time`` replacement: advances only while a hold is in flight."""

    def __init__(self, hold: Hold, *, step: float = 1.0, polls: int = 3) -> None:
        self.hold = hold
        self.step = step
        self.polls = polls
        self.now: float = 0.0
        self.held_reads: int = 0
        self.total_held_reads: int = 0

    def monotonic(self) -> float:
        if self.hold.marker.exists():
            self.now += self.step
            self.held_reads += 1
            self.total_held_reads += 1
            if self.held_reads >= self.polls:
                self.hold.release.touch()
        else:
            self.held_reads = 0
        return self.now


def install_hold_clock(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    directory: Path,
    *,
    step: float = 1.0,
    polls: int = 3,
) -> HoldClock:
    """Replace *module*'s ``time`` with a :class:`HoldClock` (lock_helpers seam)."""
    clock = HoldClock(Hold(directory), step=step, polls=polls)
    monkeypatch.setattr(module, "time", clock)
    return clock
