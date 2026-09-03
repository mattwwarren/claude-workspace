"""Tests for cw.ssh — SSH agent key preflight check (#927)."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from cw.ssh import (
    _classify_remote_url,
    check_ssh_key_available,
    push_remote_scheme,
    remote_needs_ssh_probe,
)

if TYPE_CHECKING:
    from pathlib import Path


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


# ---------------------------------------------------------------------------
# push_remote_scheme / remote_needs_ssh_probe (#1495)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def _isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the host's global/system git config from the probe under test.

    A host-level ``url.<base>.insteadOf`` rewrite (e.g. a git proxy that maps
    ``git@github.com:`` to an HTTP URL) is exactly what ``push_remote_scheme``
    is designed to honour, so left visible it would silently flip these
    fixtures' expected schemes on such a machine.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    # Same rewrite can arrive as environment config (GIT_CONFIG_COUNT +
    # GIT_CONFIG_KEY_n/VALUE_n), which GIT_CONFIG_GLOBAL does not hide.
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)


class TestClassifyRemoteUrl:
    """URL-shape classification behind ``push_remote_scheme`` (#1495)."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/o/r.git", "http"),
            ("http://local_proxy@127.0.0.1:8080/git/o/r", "http"),
            ("HTTPS://GITHUB.COM/o/r", "http"),
            ("git@github.com:o/r.git", "ssh"),
            ("github.com:o/r.git", "ssh"),
            ("ssh://git@github.com/o/r.git", "ssh"),
            ("git+ssh://github.com/o/r.git", "ssh"),
            ("file:///srv/git/r.git", "local"),
            ("/srv/git/r.git", "local"),
            ("../sibling.git", "local"),
            ("git://github.com/o/r.git", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_classifies_url_shapes(self, url: str, expected: str) -> None:
        assert _classify_remote_url(url) == expected


class TestRemoteNeedsSshProbe:
    """Only http/local remotes are exempt; unknown stays fail-closed."""

    def test_http_and_local_skip_probe(self) -> None:
        assert remote_needs_ssh_probe("http") is False
        assert remote_needs_ssh_probe("local") is False

    def test_ssh_and_unknown_engage_probe(self) -> None:
        assert remote_needs_ssh_probe("ssh") is True
        assert remote_needs_ssh_probe("unknown") is True


@pytest.mark.usefixtures("_isolated_git_config")
class TestPushRemoteScheme:
    """``push_remote_scheme`` against real git repos and failing subprocesses."""

    def test_https_origin_resolves_http(self, tmp_path: Path) -> None:
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "remote", "add", "origin", "https://github.com/o/r.git")

        assert push_remote_scheme(tmp_path) == "http"

    def test_ssh_origin_resolves_ssh(self, tmp_path: Path) -> None:
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "remote", "add", "origin", "git@github.com:o/r.git")

        assert push_remote_scheme(tmp_path) == "ssh"

    def test_push_url_wins_over_fetch_url(self, tmp_path: Path) -> None:
        """The effective *push* transport is what matters, not the fetch URL."""
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "remote", "add", "origin", "https://github.com/o/r.git")
        _git(
            tmp_path, "remote", "set-url", "--push", "origin", "git@github.com:o/r.git"
        )

        assert push_remote_scheme(tmp_path) == "ssh"

    def test_push_insteadof_rewrite_is_applied(self, tmp_path: Path) -> None:
        """``pushInsteadOf`` rewrites are honoured, so config-level SSH shows up."""
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "remote", "add", "origin", "https://github.com/o/r.git")
        _git(
            tmp_path,
            "config",
            "url.git@github.com:.pushInsteadOf",
            "https://github.com/",
        )

        assert push_remote_scheme(tmp_path) == "ssh"

    def test_no_origin_resolves_unknown(self, tmp_path: Path) -> None:
        _git(tmp_path, "init", "-q")

        assert push_remote_scheme(tmp_path) == "unknown"

    def test_not_a_repo_resolves_unknown(self, tmp_path: Path) -> None:
        assert push_remote_scheme(tmp_path / "nowhere") == "unknown"

    def test_git_missing_resolves_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_a: object, **_kw: object) -> Any:
            msg = "git"
            raise FileNotFoundError(msg)

        monkeypatch.setattr("cw.ssh._sp.run", _raise)
        assert push_remote_scheme(tmp_path) == "unknown"

    def test_timeout_resolves_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_a: object, **_kw: object) -> Any:
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr("cw.ssh._sp.run", _raise)
        assert push_remote_scheme(tmp_path) == "unknown"

    def test_empty_stdout_resolves_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.ssh._sp.run", lambda *_a, **_kw: _make_run_result(0, "\n")
        )
        assert push_remote_scheme(tmp_path) == "unknown"
