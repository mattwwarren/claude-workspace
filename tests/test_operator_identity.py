"""Tests for cw.operator_identity — operator self-identity resolution (#1153)."""

from __future__ import annotations

import subprocess as _sp
from pathlib import Path
from typing import Any

import pytest

from cw import operator_identity
from cw.models import ClientConfig, OrchestratorConfig
from tests.test_gh import _make_run_result


def _make_identity_dispatch(identity: str | int | BaseException) -> Any:
    """Fake ``_sp.run`` that responds to a ``gh api user`` invocation.

    Slim analogue of ``TestFetchApprovedPlanComment._make_dispatched_run``
    (``tests/test_gh.py``), narrowed to the identity call only:
    - str: successful login (used as stdout)
    - int: non-zero returncode (gh api user failed)
    - BaseException instance: raised (FileNotFoundError, TimeoutExpired)
    """

    def _fake_run(args: list[str], **_kw: object) -> Any:
        argv = list(args)
        if argv[:3] == ["gh", "api", "user"]:
            if isinstance(identity, BaseException):
                raise identity
            if isinstance(identity, int):
                return _make_run_result(identity, "")
            return _make_run_result(0, identity)
        msg = f"unexpected _sp.run args: {argv}"
        raise AssertionError(msg)

    return _fake_run


@pytest.fixture(autouse=True)
def _clear_gh_login_cache() -> None:
    """Reset the process-lifetime login cache before every test in this module."""
    operator_identity.cache_clear()


class TestCachedGhLogin:
    def test_success_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _counting_run(args: list[str], **_kw: object) -> Any:
            calls.append(list(args))
            return _make_run_result(0, "mattwwarren")

        monkeypatch.setattr("cw.gh._sp.run", _counting_run)

        first = operator_identity.cached_gh_login()
        second = operator_identity.cached_gh_login()

        assert first == "mattwwarren"
        assert second == "mattwwarren"
        assert len(calls) == 1

    def test_failed_fetch_not_cached_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.gh._sp.run", _make_identity_dispatch(FileNotFoundError("gh"))
        )
        first = operator_identity.cached_gh_login()
        assert first is None

        monkeypatch.setattr("cw.gh._sp.run", _make_identity_dispatch("mattwwarren"))
        second = operator_identity.cached_gh_login()
        assert second == "mattwwarren"

    def test_timeout_not_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.gh._sp.run",
            _make_identity_dispatch(_sp.TimeoutExpired(["gh"], 30)),
        )
        first = operator_identity.cached_gh_login()
        assert first is None

        monkeypatch.setattr("cw.gh._sp.run", _make_identity_dispatch("mattwwarren"))
        second = operator_identity.cached_gh_login()
        assert second == "mattwwarren"


class TestResolveOperatorLogin:
    def test_override_wins_over_runtime_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def _counting_run(args: list[str], **_kw: object) -> Any:
            calls.append(list(args))
            return _make_run_result(0, "runtime-user")

        monkeypatch.setattr("cw.gh._sp.run", _counting_run)
        client = ClientConfig(
            name="acme",
            workspace_path=Path("/dev/null"),
            operator_github_login="override-user",
        )

        result = operator_identity.resolve_operator_login(client)

        assert result == "override-user"
        assert calls == []

    def test_no_override_falls_back_to_runtime_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.gh._sp.run", _make_identity_dispatch("runtime-user"))
        client = ClientConfig(name="acme", workspace_path=Path("/dev/null"))

        result = operator_identity.resolve_operator_login(client)

        assert result == "runtime-user"

    def test_no_override_and_failed_runtime_login_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.gh._sp.run", _make_identity_dispatch(FileNotFoundError("gh"))
        )
        client = ClientConfig(name="acme", workspace_path=Path("/dev/null"))

        result = operator_identity.resolve_operator_login(client)

        assert result is None


class TestResolveOperatorLoginForRepo:
    """resolve_operator_login_for_repo (#1171): repo-keyed override resolution.

    Deliberately NEVER calls cached_gh_login() itself — see the #1195
    per-repo-resolution-must-be-a-pure-dict-lookup rationale on the
    function's docstring. test_never_calls_cached_gh_login is the explicit
    regression guard for that design decision.
    """

    def test_repo_in_map_wins_over_fallback(self) -> None:
        config = OrchestratorConfig(
            operator_github_login_by_repo={"acme/widgets": "override-user"}
        )

        result = operator_identity.resolve_operator_login_for_repo(
            "acme/widgets", config, fallback="runtime-user"
        )

        assert result == "override-user"

    def test_repo_absent_from_map_falls_back(self) -> None:
        config = OrchestratorConfig()

        result = operator_identity.resolve_operator_login_for_repo(
            "acme/widgets", config, fallback="runtime-user"
        )

        assert result == "runtime-user"

    def test_repo_absent_and_fallback_none_returns_none(self) -> None:
        config = OrchestratorConfig()

        result = operator_identity.resolve_operator_login_for_repo(
            "acme/widgets", config, fallback=None
        )

        assert result is None

    def test_never_calls_cached_gh_login(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail_if_called() -> str | None:
            msg = "resolve_operator_login_for_repo must never call cached_gh_login"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.operator_identity.cached_gh_login", _fail_if_called)
        config = OrchestratorConfig(
            operator_github_login_by_repo={"acme/widgets": "override-user"}
        )

        # Map hit.
        assert (
            operator_identity.resolve_operator_login_for_repo(
                "acme/widgets", config, fallback="runtime-user"
            )
            == "override-user"
        )
        # Map miss.
        assert (
            operator_identity.resolve_operator_login_for_repo(
                "other/repo", config, fallback="runtime-user"
            )
            == "runtime-user"
        )

    def test_case_sensitive_exact_match(self) -> None:
        config = OrchestratorConfig(
            operator_github_login_by_repo={"Acme/Widgets": "override-user"}
        )

        result = operator_identity.resolve_operator_login_for_repo(
            "acme/widgets", config, fallback="runtime-user"
        )

        assert result == "runtime-user"
