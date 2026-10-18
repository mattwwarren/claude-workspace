"""Tests for the ``cw preflight`` command (#2697).

The command wraps :func:`cw.preflight.run_preflight`: one JSON object on
stdout, exit 1 on any hard failure. The four subprocess-touching checks are
stubbed with the shared ``_install_check_stubs``; agent presence, client and
repo-root resolution, tracker and slug resolution run for real.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click
import cw.preflight as pf
import pytest
from click.testing import CliRunner, Result

from cw.cli import main
from cw.config import clients_file
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import (
    _install_check_stubs,
    _make_agent_repo,
    git_in,
    init_repo_with_remote,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_ORIGIN = "git@github.com:acme/widgets.git"


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, ...]]:
    """Stub the subprocess checks; return what each stub was called with."""
    calls: dict[str, tuple[str, ...]] = {}
    _install_check_stubs(pf, monkeypatch, calls)
    return calls


@pytest.fixture
def acme_root(tmp_config_dir: Path, tmp_path: Path) -> Path:
    """A registered client ``acme`` whose repo has agents and a GitHub origin."""
    root = init_repo_with_remote(tmp_path / "acme", _ORIGIN)
    _make_agent_repo(root, tracker="github-issues")
    write_clients_yaml(ClientSpec("acme", workspace_path=root))
    return root


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, ["preflight", *args])


def _report(result: Result) -> dict[str, object]:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


def _checks(report: dict[str, object]) -> list[dict[str, object]]:
    checks = report["checks"]
    assert isinstance(checks, list)
    return checks


class TestPreflightCommand:
    def test_all_hard_checks_pass_exits_0_with_one_json_object(
        self, acme_root: Path, captured: dict[str, tuple[str, ...]]
    ) -> None:
        result = _invoke("123", "-c", "acme")

        assert result.exit_code == 0, result.output
        report = _report(result)
        assert report["ok"] is True
        assert report["ticket_id"] == "123"
        assert report["client"] == "acme"
        assert report["repo"] == "acme/widgets"
        assert report["repo_root"] == str(acme_root)
        assert report["tracker"] == "github-issues"
        assert captured["ticket_open"] == ("123", "acme/widgets", "github-issues")
        assert captured["not_queued"] == ("123", "acme")

    def test_long_client_option(
        self, acme_root: Path, captured: dict[str, tuple[str, ...]]
    ) -> None:
        result = _invoke("123", "--client", "acme")

        assert result.exit_code == 0, result.output
        assert _report(result)["client"] == "acme"

    def test_hard_failure_exits_1_and_still_prints_json(
        self,
        acme_root: Path,
        captured: dict[str, tuple[str, ...]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            pf,
            "_check_ticket_open",
            lambda _t, _r, _k: {
                "name": "ticket_open",
                "passed": False,
                "severity": "hard",
                "detail": "state=CLOSED",
            },
        )

        result = _invoke("123", "-c", "acme")

        assert result.exit_code == 1
        report = _report(result)
        assert report["ok"] is False
        failed = [c["name"] for c in _checks(report) if c["passed"] is False]
        assert failed == ["ticket_open"]

    def test_client_missing_from_clients_yaml_exits_1(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        captured: dict[str, tuple[str, ...]],
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        write_clients_yaml(ClientSpec("other", workspace_path=other))

        result = _invoke("123", "-c", "acme")

        assert result.exit_code == 1
        report = _report(result)
        assert report["ok"] is False
        checks = _checks(report)
        assert [c["name"] for c in checks] == ["client_repo_resolved"]
        assert checks[0]["passed"] is False
        assert captured == {}

    def test_repo_option_overrides_derived_slug(
        self, acme_root: Path, captured: dict[str, tuple[str, ...]]
    ) -> None:
        result = _invoke("123", "-c", "acme", "--repo", "explicit/repo")

        assert result.exit_code == 0, result.output
        assert _report(result)["repo"] == "explicit/repo"
        assert captured["ticket_open"][1] == "explicit/repo"
        assert captured["no_open_pr"][1] == "explicit/repo"

    def test_leading_hash_is_stripped(
        self, acme_root: Path, captured: dict[str, tuple[str, ...]]
    ) -> None:
        result = _invoke("#123", "-c", "acme")

        assert result.exit_code == 0, result.output
        assert _report(result)["ticket_id"] == "123"
        assert captured["ticket_open"][0] == "123"
        assert captured["not_queued"] == ("123", "acme")

    def test_missing_client_is_a_usage_error(
        self, captured: dict[str, tuple[str, ...]]
    ) -> None:
        result = _invoke("123")

        assert result.exit_code == click.UsageError.exit_code
        assert "--client" in result.output
        assert captured == {}

    def test_help_names_the_contract(self) -> None:
        result = _invoke("--help")

        assert result.exit_code == 0
        assert "Run pre-flight checks for TICKET" in result.output
        assert "-c, --client" in result.output
        assert "--repo" in result.output

    def test_linked_worktree_without_context_file_or_clients_yaml(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[..., Path],
        captured: dict[str, tuple[str, ...]],
    ) -> None:
        """Acceptance bullet 1 end to end: an orchestrator's fresh worktree.

        A linked worktree (``.git`` is a file) with no ``clients.yaml`` and
        no ``.claude/cw-context.json``: the repo root is the worktree itself,
        and the slug comes from the shared ``origin`` remote.
        """
        repo = make_git_repo("main-checkout")
        git_in(repo, "remote", "add", "origin", _ORIGIN)
        worktree = tmp_path / "linked"
        git_in(repo, "worktree", "add", "-b", "feature", str(worktree))
        assert (worktree / ".git").is_file()
        _make_agent_repo(worktree, tracker="github-issues")
        assert not (worktree / ".claude" / "cw-context.json").exists()
        assert not clients_file().exists()
        monkeypatch.chdir(worktree)

        result = _invoke("1", "-c", "acme")

        assert result.exit_code == 0, result.output
        report = _report(result)
        assert report["ok"] is True
        assert report["repo_root"] == str(worktree.resolve())
        assert report["repo"] == "acme/widgets"
        assert report["tracker"] == "github-issues"
        agents = next(c for c in _checks(report) if c["name"] == "agents_present")
        assert agents["passed"] is True
        assert captured["ticket_open"] == ("1", "acme/widgets", "github-issues")

    def test_linked_worktree_with_repo_option_and_no_remote(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[..., Path],
        captured: dict[str, tuple[str, ...]],
    ) -> None:
        """Without an origin remote, ``--repo`` supplies the slug."""
        repo = make_git_repo("main-checkout")
        worktree = tmp_path / "linked"
        git_in(repo, "worktree", "add", "-b", "feature", str(worktree))
        _make_agent_repo(worktree, tracker="github-issues")
        monkeypatch.chdir(worktree)

        result = _invoke("1", "-c", "acme", "--repo", "acme/widgets")

        assert result.exit_code == 0, result.output
        report = _report(result)
        assert report["repo_root"] == str(worktree.resolve())
        assert report["repo"] == "acme/widgets"
