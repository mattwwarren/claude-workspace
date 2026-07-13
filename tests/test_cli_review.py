"""Tests for the ``cw review`` CLI group (GitHub #1154, RFC 0011 S2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.dev_queue import load_dev_queue

if TYPE_CHECKING:
    from pathlib import Path

_URL = "https://github.com/acme/widgets/pull/42"
_OPERATOR = "mattwwarren"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _patch_identity(
    monkeypatch: pytest.MonkeyPatch, login: str | None = _OPERATOR
) -> None:
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: login)


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch, review_requests: list[dict[str, Any]] | None
) -> None:
    payload = None if review_requests is None else {"reviewRequests": review_requests}
    monkeypatch.setattr("cw.gh.fetch_pr_view", lambda *_a, **_kw: payload)


class TestReviewRegisterCommand:
    def test_individual_target_registers_and_prints_confirmation(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _patch_identity(monkeypatch)
        _patch_fetch(monkeypatch, [{"login": _OPERATOR}])
        result = runner.invoke(main, ["review", "register", _URL])
        assert result.exit_code == 0
        assert "Registered" in result.output
        watched = load_dev_queue().watched_prs
        assert len(watched) == 1
        assert watched[0].source == "cli"
        assert watched[0].requester_login is None

    def test_team_target_prints_reason_and_exits_zero(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _patch_identity(monkeypatch)
        _patch_fetch(monkeypatch, [{"slug": "eng-team"}])
        result = runner.invoke(main, ["review", "register", _URL])
        assert result.exit_code == 0
        assert "team_targeted" in result.output
        assert load_dev_queue().watched_prs == []

    def test_identity_unresolved_raises_cw_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _patch_identity(monkeypatch, login=None)
        _patch_fetch(monkeypatch, [{"login": _OPERATOR}])
        result = runner.invoke(main, ["review", "register", _URL])
        assert result.exit_code != 0

    def test_unparseable_pr_argument_raises_cw_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        result = runner.invoke(main, ["review", "register", "not-a-url"])
        assert result.exit_code != 0

    def test_gh_fetch_failure_raises_cw_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _patch_identity(monkeypatch)
        _patch_fetch(monkeypatch, None)
        result = runner.invoke(main, ["review", "register", _URL])
        assert result.exit_code != 0

    def test_register_idempotent_prints_already_registered(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _patch_identity(monkeypatch)
        _patch_fetch(monkeypatch, [{"login": _OPERATOR}])
        first = runner.invoke(main, ["review", "register", _URL])
        assert first.exit_code == 0
        second = runner.invoke(main, ["review", "register", _URL])
        assert second.exit_code == 0
        assert "already_registered" in second.output
        assert len(load_dev_queue().watched_prs) == 1
