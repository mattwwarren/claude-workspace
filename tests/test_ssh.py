"""Tests for cw.ssh — SSH agent key preflight check (#927)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from cw.ssh import check_ssh_key_available

if TYPE_CHECKING:
    import pytest


def _make_run_result(returncode: int = 0, stdout: str = "") -> Any:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    return result


class TestCheckSshKeyAvailable:
    """Direct tests for check_ssh_key_available.

    Sibling of TestCheckGhAvailability (tests/test_gh.py): mocks the same
    subprocess seam shape (``cw.ssh._sp.run``), but the match is on stdout
    content (ED25519/RSA marker), not returncode — a direct port of the
    ticket's ``ssh-add -l 2>/dev/null | grep -q "ED25519\\|RSA"`` pipeline.
    """

    def test_ed25519_identity_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.ssh._sp.run",
            lambda *_a, **_kw: _make_run_result(
                0, "256 SHA256:abc user@host (ED25519)\n"
            ),
        )
        assert check_ssh_key_available() is True

    def test_rsa_identity_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.ssh._sp.run",
            lambda *_a, **_kw: _make_run_result(0, "2048 SHA256:def user@host (RSA)\n"),
        )
        assert check_ssh_key_available() is True

    def test_no_identities_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.ssh._sp.run",
            lambda *_a, **_kw: _make_run_result(1, "The agent has no identities.\n"),
        )
        assert check_ssh_key_available() is False

    def test_ssh_add_not_found_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_a: object, **_kw: object) -> Any:
            msg = "ssh-add"
            raise FileNotFoundError(msg)

        monkeypatch.setattr("cw.ssh._sp.run", _raise)
        assert check_ssh_key_available() is False

    def test_timeout_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: object, **_kw: object) -> Any:
            raise subprocess.TimeoutExpired(cmd="ssh-add", timeout=5)

        monkeypatch.setattr("cw.ssh._sp.run", _raise)
        assert check_ssh_key_available() is False

    def test_oserror_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: object, **_kw: object) -> Any:
            msg = "boom"
            raise OSError(msg)

        monkeypatch.setattr("cw.ssh._sp.run", _raise)
        assert check_ssh_key_available() is False
