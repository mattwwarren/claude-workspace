"""Tests for cw.doctor.agent_spec_drift — per-client reviewer agent-spec drift (#1776).

Direct calls to ``_check_agent_spec_drift()``, reusing #1773's fixtures
(``_isolate_global_agents_dir`` autouse conftest fixture, ``_write`` /
``_populate_global_agents_dir`` helpers) rather than touching the real
``~/.claude/agents/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.codex_review import _REVIEWER_ROLE_AGENT_FILES
from cw.doctor.agent_spec_drift import _check_agent_spec_drift
from cw.models import ClientConfig
from tests._codex_review_helpers import _populate_global_agents_dir, _write


def _write_repo_specs(root: Path, *, skip: set[str] | None = None) -> None:
    """Write a non-blank repo-local spec for every role except *skip* role names."""
    skip = skip or set()
    for role, filename in _REVIEWER_ROLE_AGENT_FILES.items():
        if role in skip:
            continue
        _write(root / ".claude" / "agents" / filename, "SPEC\n")


def test_empty_clients_yields_no_results() -> None:
    assert _check_agent_spec_drift({}) == []


def test_all_roles_repo_local_resolves_clean(sample_client: ClientConfig) -> None:
    _write_repo_specs(sample_client.workspace_path)

    results = _check_agent_spec_drift({"test-client": sample_client})

    assert len(results) == 1
    result = results[0]
    assert result.name == "agent-spec-drift/test-client"
    assert result.ok is True
    assert result.warn is False
    assert "9/9" in result.detail


def test_no_agents_dir_at_all_produces_actionable_warning(
    sample_client: ClientConfig,
) -> None:
    # No .claude/agents/ under sample_client.workspace_path; autouse
    # _isolate_global_agents_dir keeps the global fallback dir empty too.
    results = _check_agent_spec_drift({"test-client": sample_client})

    assert len(results) == 1
    result = results[0]
    assert result.warn is True
    agents_dir = sample_client.workspace_path / ".claude" / "agents"
    assert str(agents_dir) in result.detail
    assert "Code Quality Reviewer" in result.detail
    assert "code-reviewer.md" in result.detail


def test_role_falls_back_to_global_when_repo_missing(
    sample_client: ClientConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_repo_specs(sample_client.workspace_path, skip={"Deployment Reviewer"})
    global_dir = tmp_path / "global-agents"
    monkeypatch.setattr(
        "cw.codex_review._context._agent_spec._GLOBAL_AGENTS_DIR", global_dir
    )
    _populate_global_agents_dir(global_dir, deployment_reviewer="GLOBAL SPEC\n")

    results = _check_agent_spec_drift({"test-client": sample_client})

    result = results[0]
    assert result.warn is False
    assert "8 repo" in result.detail
    assert "1 global" in result.detail


def test_fallback_disabled_reports_global_only_role_as_absent(
    sample_client: ClientConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_repo_specs(sample_client.workspace_path, skip={"Deployment Reviewer"})
    _write(
        sample_client.workspace_path / "pyproject.toml",
        "[tool.cw.codex_review]\nagent_spec_global_fallback = false\n",
    )
    global_dir = tmp_path / "global-agents"
    monkeypatch.setattr(
        "cw.codex_review._context._agent_spec._GLOBAL_AGENTS_DIR", global_dir
    )
    _populate_global_agents_dir(global_dir, deployment_reviewer="GLOBAL SPEC\n")

    results = _check_agent_spec_drift({"test-client": sample_client})

    result = results[0]
    assert result.warn is True
    assert "Deployment Reviewer" in result.detail
    assert "not found" in result.detail
    assert "global fallback disabled" in result.detail


def test_blank_repo_file_reported_distinctly_from_missing_file(
    sample_client: ClientConfig,
) -> None:
    _write_repo_specs(
        sample_client.workspace_path,
        skip={"Deployment Reviewer", "API Contract Validator"},
    )
    # deployment_reviewer: blank tracked file. api_contract_validator: absent
    # entirely. Global fallback dir stays empty (autouse isolation), so both
    # roles remain unspecified but for different underlying reasons.
    _write(
        sample_client.workspace_path / ".claude" / "agents" / "deployment-reviewer.md",
        "   \n",
    )

    results = _check_agent_spec_drift({"test-client": sample_client})

    result = results[0]
    assert result.warn is True
    assert "Deployment Reviewer (deployment-reviewer.md, blank tracked file)" in (
        result.detail
    )
    assert "API Contract Validator (api-contract-validator.md, not found)" in (
        result.detail
    )


def test_blank_repo_file_recovered_by_global_fallback_reports_resolved(
    sample_client: ClientConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_repo_specs(sample_client.workspace_path, skip={"Deployment Reviewer"})
    _write(
        sample_client.workspace_path / ".claude" / "agents" / "deployment-reviewer.md",
        "   \n",
    )
    global_dir = tmp_path / "global-agents"
    monkeypatch.setattr(
        "cw.codex_review._context._agent_spec._GLOBAL_AGENTS_DIR", global_dir
    )
    _populate_global_agents_dir(global_dir, deployment_reviewer="GLOBAL SPEC\n")

    results = _check_agent_spec_drift({"test-client": sample_client})

    result = results[0]
    assert result.warn is False
    assert "9/9" in result.detail
    assert "1 global" in result.detail


def test_repo_path_wins_over_workspace_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "wt"
    repo.mkdir()
    worktree.mkdir()
    _write_repo_specs(repo)
    client = ClientConfig(
        name="client-a", workspace_path=worktree, repo_path=repo, branch="dev/x"
    )

    results = _check_agent_spec_drift({"client-a": client})

    result = results[0]
    assert result.warn is False
    assert "9/9" in result.detail


def test_multiple_clients_get_independent_check_results(
    sample_client: ClientConfig, tmp_path: Path
) -> None:
    _write_repo_specs(sample_client.workspace_path)

    other_repo = tmp_path / "other-repo"
    other_wt = tmp_path / "other-wt"
    other_repo.mkdir()
    other_wt.mkdir()
    other_client = ClientConfig(
        name="client-b", workspace_path=other_wt, repo_path=other_repo, branch="dev/y"
    )
    # No specs at all for client-b -> warns.

    results = _check_agent_spec_drift(
        {"test-client": sample_client, "client-b": other_client}
    )

    names = {result.name: result for result in results}
    assert set(names) == {"agent-spec-drift/test-client", "agent-spec-drift/client-b"}
    assert names["agent-spec-drift/test-client"].warn is False
    assert names["agent-spec-drift/client-b"].warn is True


def test_never_reads_real_home(sample_client: ClientConfig) -> None:
    _write_repo_specs(sample_client.workspace_path)

    # No monkeypatching of Path.home() at all -- the autouse
    # _isolate_global_agents_dir fixture and full repo-local coverage mean
    # this must resolve cleanly without ever touching the real home dir.
    results = _check_agent_spec_drift({"test-client": sample_client})

    assert results[0].ok is True
    assert results[0].warn is False
