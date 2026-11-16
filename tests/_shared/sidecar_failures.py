"""Shared failing writer for dispatch-sidecar failure-path tests (#3638).

One helper replaces the private ``_boom`` raisers that each test module used
to define. It patches ``cw.dispatch_state.atomic_write_text`` (the single seam
every sidecar writer resolves at call time) so every failure path goes through
the same recorded, raising writer.

Plain module, no fixtures, so it needs no ``pytest_plugins`` registration.
``cw`` is imported only through the monkeypatch string target.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def fail_state_writes(
    monkeypatch: pytest.MonkeyPatch, message: str = "disk full"
) -> list[tuple[Path, str]]:
    """Make every sidecar write raise ``OSError(message)``; return the attempts.

    Each attempted ``(path, text)`` is recorded before the raise, so a test can
    assert a write was attempted (and therefore swallowed or propagated) while
    the sidecar file itself stays unmodified.
    """
    attempts: list[tuple[Path, str]] = []

    def _boom(path: Path, text: str) -> None:
        attempts.append((path, text))
        raise OSError(message)

    monkeypatch.setattr("cw.dispatch_state.atomic_write_text", _boom)
    return attempts
