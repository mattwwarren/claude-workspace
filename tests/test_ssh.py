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


class TestResolveIdentityAgentSock:
    """IdentityAgent resolution via ``ssh -G`` before the ``ssh-add -l`` probe (#1436).

    Routes ``cw.ssh._sp.run`` by inspecting ``args[0]`` so each test can give
    the first call (``ssh -G <host>``) and second call (``ssh-add -l``)
    independent, order-dependent behavior.
    """

    def test_identityagent_seeds_ssh_auth_sock_for_ssh_add(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Any, Any]] = []

        def _router(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            if args[0][0] == "ssh":
                return _make_run_result(
                    0, "identityagent /custom/1password/agent.sock\n"
                )
            assert kwargs["env"]["SSH_AUTH_SOCK"] == "/custom/1password/agent.sock"
            return _make_run_result(0, "256 SHA256:abc user@host (ED25519)\n")

        monkeypatch.setattr("cw.ssh._sp.run", _router)
        assert check_ssh_key_available() is True
        assert len(calls) == 2

    def test_no_identityagent_line_falls_back_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Any, Any]] = []

        def _router(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            if args[0][0] == "ssh":
                return _make_run_result(0, "user git\nhostname github.com\n")
            assert "env" not in kwargs
            return _make_run_result(1, "The agent has no identities.\n")

        monkeypatch.setattr("cw.ssh._sp.run", _router)
        assert check_ssh_key_available() is False
        assert len(calls) == 2

    def test_ssh_g_uses_expected_argv_and_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Any, Any]] = []

        def _router(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            if args[0][0] == "ssh":
                return _make_run_result(0, "user git\n")
            return _make_run_result(1, "")

        monkeypatch.setattr("cw.ssh._sp.run", _router)
        check_ssh_key_available(timeout=7)
        first_args, first_kwargs = calls[0]
        assert first_args[0] == ["ssh", "-G", "github.com"]
        assert first_kwargs == {
            "capture_output": True,
            "text": True,
            "check": False,
            "timeout": 7,
        }

    def test_ssh_g_filenotfound_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Any, Any]] = []

        def _router(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            if args[0][0] == "ssh":
                msg = "ssh"
                raise FileNotFoundError(msg)
            assert "env" not in kwargs
            return _make_run_result(0, "256 SHA256:abc user@host (ED25519)\n")

        monkeypatch.setattr("cw.ssh._sp.run", _router)
        assert check_ssh_key_available() is True
        assert len(calls) == 2

    def test_ssh_g_timeout_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[Any, Any]] = []

        def _router(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            if args[0][0] == "ssh":
                raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)
            return _make_run_result(0, "256 SHA256:abc user@host (ED25519)\n")

        monkeypatch.setattr("cw.ssh._sp.run", _router)
        assert check_ssh_key_available() is True
        assert len(calls) == 2

    def test_ssh_g_oserror_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[Any, Any]] = []

        def _router(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            if args[0][0] == "ssh":
                msg = "boom"
                raise OSError(msg)
            return _make_run_result(0, "256 SHA256:abc user@host (ED25519)\n")

        monkeypatch.setattr("cw.ssh._sp.run", _router)
        assert check_ssh_key_available() is True
        assert len(calls) == 2

    def test_identityagent_none_value_unsets_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Any, Any]] = []

        def _router(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            if args[0][0] == "ssh":
                return _make_run_result(0, "identityagent none\n")
            assert "env" not in kwargs
            return _make_run_result(0, "256 SHA256:abc user@host (ED25519)\n")

        monkeypatch.setattr("cw.ssh._sp.run", _router)
        assert check_ssh_key_available() is True
        assert len(calls) == 2
