"""Shared helpers for the ``cw.dispatch.gating`` test files (#2503).

Plain functions split out of ``tests/test_dispatch.py`` when the gating
test classes moved into one ``tests/test_dispatch_gating_<family>.py`` file
per gate family. Each file imports only what it uses.
"""

from __future__ import annotations

import pytest


def _force_gh_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the fleet-wide gh-availability probe to report unavailable.

    Overrides the autouse ``_mock_gh_availability`` default (which returns
    True) on the same ``cw.dispatch.gating.availability.check_gh_availability`` seam.
    """
    monkeypatch.setattr(
        "cw.dispatch.gating.availability.check_gh_availability", lambda **_kw: False
    )


def _force_ssh_key_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the SSH-agent-key preflight probe to report unavailable.

    Overrides the autouse ``_mock_ssh_key_available`` default (which returns
    True) on the same ``cw.dispatch.gating.check_ssh_key_available`` seam.
    """
    monkeypatch.setattr(
        "cw.dispatch.gating.check_ssh_key_available", lambda **_kw: False
    )
