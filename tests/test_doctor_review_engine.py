"""Tests for ``cw.doctor.review_engine`` (GitHub #3039, RFC 0017 I1; #3216).

A ``review.engine`` runs in the claude-native review stage and, through the
skill's ``cw-engine.json`` manifest on the default branch, on a codex review
lane. The doctor warns, once per lane, when a lane cannot run it: five states
against real git repos (claude-native nothing, codex loaded nothing, codex
absent or invalid manifest, codex cannot-verify, opencode/local unsupported).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.doctor.core import _client_config_checks
from cw.doctor.review_engine import _check_review_engine
from cw.models import (
    ClientConfig,
    LaneConfig,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
)
from cw.models.client import ReviewConfig, ReviewEngineConfig
from cw.review_engine.manifest_source import ManifestRead, read_default_branch_manifest
from tests._review_engine_helpers import copy_engine_fixture, make_engine_repo
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable

STUB_DIR = Path(__file__).parent / "fixtures" / "review_engine_stub"
_SKILL = "repo-review"


def _pipeline(
    backend: str, *, stages: list[Stage] | None = None
) -> StagePipelineConfig:
    fields: dict[str, object] = {
        "executors": {Stage.REVIEW: StageExecutorConfig(backend=backend)}
    }
    if stages is not None:
        fields["stages"] = stages
    return StagePipelineConfig.model_validate(fields)


def _client(
    name: str = "acme",
    *,
    engine: bool = True,
    lanes: list[LaneConfig] | None = None,
    pipeline: StagePipelineConfig | None = None,
    workspace: Path = Path("/dev/null"),
) -> ClientConfig:
    fields: dict[str, object] = {
        "name": name,
        "workspace_path": workspace,
        "review": ReviewConfig(
            convergence="clean",
            engine=ReviewEngineConfig(skill=_SKILL) if engine else None,
        ),
    }
    if lanes is not None:
        fields["lanes"] = lanes
    if pipeline is not None:
        fields["pipeline"] = pipeline
    return ClientConfig.model_validate(fields)


def test_warns_for_opencode_review_lane() -> None:
    client = _client(pipeline=_pipeline("opencode"))

    results = _check_review_engine({"acme": client})

    assert [r.name for r in results] == ["review-engine/acme/default"]
    assert (results[0].ok, results[0].warn) == (True, True)
    assert "backend=opencode" in results[0].detail
    assert "review_engine_unsupported_backend" in results[0].detail
    assert "cause=backend_unsupported" in results[0].detail


def test_warns_for_local_lane() -> None:
    client = _client(pipeline=_pipeline("local"))

    results = _check_review_engine({"acme": client})

    assert [r.name for r in results] == ["review-engine/acme/default"]
    assert (results[0].ok, results[0].warn) == (True, True)
    assert "backend=local" in results[0].detail
    assert "review_engine_unsupported_backend" in results[0].detail


@pytest.fixture
def read_counter(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str, str]]:
    """Every ``read_default_branch_manifest`` call the doctor makes, passed through."""
    calls: list[tuple[Path, str, str]] = []

    def _counting(repo: Path, default_branch: str, skill: str) -> ManifestRead:
        calls.append((repo, default_branch, skill))
        return read_default_branch_manifest(repo, default_branch, skill)

    monkeypatch.setattr(
        "cw.doctor.review_engine.read_default_branch_manifest", _counting
    )
    return calls


def _stub_manifest_copy(tmp_path: Path, **overrides: object) -> Path:
    """A copy of the stub engine whose ``cw-engine.json`` has *overrides* applied."""
    return copy_engine_fixture(tmp_path, STUB_DIR, name="stub-copy", **overrides)


def _codex_client(workspace: Path, name: str = "acme") -> ClientConfig:
    return _client(name, pipeline=_pipeline("codex"), workspace=workspace)


def test_codex_lane_with_a_loaded_manifest_emits_nothing(
    make_git_repo: Callable[..., Path],
    read_counter: list[tuple[Path, str, str]],
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)

    results = _check_review_engine({"acme": _codex_client(repo.worktree)})

    assert results == []
    assert read_counter == [(repo.worktree, "main", _SKILL)]


def test_codex_lane_with_no_manifest_warns(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo)

    (result,) = _check_review_engine({"acme": _codex_client(repo.worktree)})

    assert result.name == "review-engine/acme/default"
    assert (result.ok, result.warn) == (True, True)
    assert "cause=no_codex_manifest" in result.detail
    assert "review_engine_unsupported_backend" in result.detail
    assert "cw-engine.json" in result.detail
    assert "cannot be verified" not in result.detail


def test_codex_lane_with_a_single_file_command_skill_warns_as_absent(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, layout=".claude/commands")

    (result,) = _check_review_engine({"acme": _codex_client(repo.worktree)})

    assert "cause=no_codex_manifest" in result.detail
    assert "single-file command" in result.detail


def test_codex_lane_with_an_invalid_manifest_names_reason_and_field_path(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    stub = _stub_manifest_copy(tmp_path, manifest_version=2)
    repo = make_engine_repo(make_git_repo, engine_files=stub)
    manifest = read_default_branch_manifest(repo.worktree, "main", _SKILL)
    assert manifest.status == "invalid"
    assert manifest.error is not None

    (result,) = _check_review_engine({"acme": _codex_client(repo.worktree)})

    assert (result.ok, result.warn) == (True, True)
    assert f"cause={manifest.cause}" in result.detail
    assert f"{manifest.error.reason}: {manifest.error.field_path}" in result.detail
    assert "manifest_version" in result.detail
    assert "review_engine_unsupported_backend" in result.detail


def test_codex_lane_with_an_unreadable_ref_is_a_cannot_verify_warning(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)
    client = _client(pipeline=_pipeline("codex"), workspace=repo.worktree).model_copy(
        update={"default_branch": "no-such-branch"}
    )

    (result,) = _check_review_engine({"acme": client})

    assert (result.ok, result.warn) == (True, True)
    assert "cannot be verified" in result.detail
    assert "cause=default_branch_unresolved" in result.detail
    assert "does not pass it" in result.detail
    assert "no_codex_manifest" not in result.detail


def test_codex_lane_with_an_unknown_skill_is_a_cannot_verify_warning(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)
    client = _codex_client(repo.worktree)
    engine = ReviewEngineConfig(skill="no-such-skill")
    client = client.model_copy(
        update={"review": client.review.model_copy(update={"engine": engine})}
    )

    (result,) = _check_review_engine({"acme": client})

    assert "cannot be verified" in result.detail
    assert "cause=skill_not_found" in result.detail
    assert "'no-such-skill'" in result.detail


def test_codex_lane_in_a_directory_git_cannot_run_in_is_never_a_pass(
    tmp_path: Path,
) -> None:
    """git failing to start (a missing workspace) reads as unresolvable, not a crash."""
    client = _codex_client(tmp_path / "does-not-exist")

    (result,) = _check_review_engine({"acme": client})

    assert "cannot be verified" in result.detail
    assert "cause=default_branch_unresolved" in result.detail


def test_codex_lane_in_a_repo_without_the_default_branch_is_unresolvable(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)
    git_in(repo.worktree, "branch", "-m", "main", "trunk")
    git_in(repo.worktree, "update-ref", "-d", "refs/remotes/origin/main")

    (result,) = _check_review_engine({"acme": _codex_client(repo.worktree)})

    assert "cannot be verified" in result.detail


def test_manifest_is_read_once_per_client_across_codex_lanes(
    make_git_repo: Callable[..., Path],
    read_counter: list[tuple[Path, str, str]],
) -> None:
    repo = make_engine_repo(make_git_repo)
    lanes = [
        LaneConfig(name="one", pipeline=_pipeline("codex")),
        LaneConfig(name="two", pipeline=_pipeline("codex")),
    ]
    client = _client(lanes=lanes, workspace=repo.worktree)

    results = _check_review_engine({"acme": client})

    assert [r.name for r in results] == [
        "review-engine/acme/one",
        "review-engine/acme/two",
    ]
    assert len(read_counter) == 1


def test_manifest_is_read_lazily_only_for_a_codex_lane(
    make_git_repo: Callable[..., Path],
    read_counter: list[tuple[Path, str, str]],
) -> None:
    repo = make_engine_repo(make_git_repo)
    lanes = [
        LaneConfig(name="native", pipeline=_pipeline("claude-native")),
        LaneConfig(name="open", pipeline=_pipeline("opencode")),
        LaneConfig(name="local", pipeline=_pipeline("local")),
    ]

    results = _check_review_engine(
        {"acme": _client(lanes=lanes, workspace=repo.worktree)}
    )

    assert [r.name for r in results] == [
        "review-engine/acme/open",
        "review-engine/acme/local",
    ]
    assert read_counter == []


def test_manifest_is_read_once_per_client_not_shared_across_clients(
    make_git_repo: Callable[..., Path],
    read_counter: list[tuple[Path, str, str]],
) -> None:
    first = make_engine_repo(make_git_repo, name="repo-a")
    second = make_engine_repo(make_git_repo, name="repo-b", engine_files=STUB_DIR)

    results = _check_review_engine(
        {
            "a": _codex_client(first.worktree, "a"),
            "b": _codex_client(second.worktree, "b"),
        }
    )

    assert [r.name for r in results] == ["review-engine/a/default"]
    assert [call[0] for call in read_counter] == [first.worktree, second.worktree]


def test_claude_native_lane_emits_nothing() -> None:
    assert _check_review_engine({"acme": _client()}) == []
    explicit = _client(pipeline=_pipeline("claude-native"))
    assert _check_review_engine({"acme": explicit}) == []


def test_lane_without_review_stage_emits_nothing() -> None:
    lane = LaneConfig(
        name="build",
        pipeline=_pipeline("codex", stages=[Stage.PLAN, Stage.IMPL, Stage.FINALIZE]),
    )

    assert _check_review_engine({"acme": _client(lanes=[lane])}) == []


def test_engine_absent_emits_nothing() -> None:
    client = _client(engine=False, pipeline=_pipeline("codex"))

    assert _check_review_engine({"acme": client}) == []


def test_empty_clients_emits_nothing() -> None:
    assert _check_review_engine({}) == []


def test_not_gated_on_is_clean() -> None:
    """The model forbids engine without clean, so build the lane around it."""
    client = ClientConfig.model_construct(
        name="acme",
        workspace_path=Path("/dev/null"),
        lanes=[],
        pipeline=_pipeline("opencode"),
        review=ReviewConfig.model_construct(
            convergence="blocking", engine=ReviewEngineConfig(skill=_SKILL)
        ),
    )
    assert client.review.is_clean is False

    results = _check_review_engine({"acme": client})

    assert [r.name for r in results] == ["review-engine/acme/default"]


def test_detail_carries_names_only() -> None:
    client = ClientConfig.model_validate(
        {
            "name": "acme",
            "workspace_path": Path("/dev/null"),
            "review": ReviewConfig(
                convergence="clean",
                engine=ReviewEngineConfig(
                    skill=_SKILL, guarded_paths=["scripts/validate_*.py"]
                ),
            ),
            "pipeline": _pipeline("opencode"),
        }
    )

    detail = _check_review_engine({"acme": client})[0].detail

    assert detail == (
        "review.engine is set, but lane 'default' runs REVIEW on backend=opencode, "
        "which cannot run a repo review engine; its REVIEW rows park as "
        "review_engine_unsupported_backend (cause=backend_unsupported). Move the "
        "lane to claude-native or codex, or remove review.engine."
    )
    assert _SKILL not in detail
    assert "scripts/validate_" not in detail
    assert "/" not in detail
    assert "SKILL.md" not in detail


def test_mixed_lanes_one_claude_native_one_opencode_yields_one_warning() -> None:
    lanes = [
        LaneConfig(name="native", pipeline=_pipeline("claude-native")),
        LaneConfig(name="slow", pipeline=_pipeline("opencode")),
    ]

    results = _check_review_engine({"acme": _client(lanes=lanes)})

    assert [r.name for r in results] == ["review-engine/acme/slow"]


def test_one_warning_per_client_with_engine() -> None:
    clients = {
        "a": _client("a", pipeline=_pipeline("local")),
        "b": _client("b", engine=False, pipeline=_pipeline("codex")),
        "c": _client("c", pipeline=_pipeline("opencode")),
    }

    results = _check_review_engine(clients)

    assert [r.name for r in results] == [
        "review-engine/a/default",
        "review-engine/c/default",
    ]


def test_registered_in_client_config_checks() -> None:
    client = _client(pipeline=_pipeline("opencode"))

    names = [check.name for check in _client_config_checks({"acme": client})]

    assert "review-engine/acme/default" in names
