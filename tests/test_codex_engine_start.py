"""The codex lane's engine check at the start of a background review (#3216).

``cw.codex_review._engine_start.engine_round_spec`` reads the skill's
``cw-engine.json`` from the default branch once, before any codex call, and
either returns the engine to run or refuses the row. The refusal text is
``cw.review_engine.constants.engine_refusal_details``, shared with the
opencode/local pre-flight guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.auto_dev_result import (
    REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON,
    AutoDevResult,
)
from cw.codex_review._engine_round import (
    EngineRoundSpec,
    engine_unresolvable_blocked,
    manifest_refusal_blocked,
)
from cw.codex_review._engine_start import engine_round_spec
from cw.models import ClientConfig, Stage
from cw.models.client import ReviewConfig, ReviewEngineConfig
from cw.review_engine.constants import (
    _REFUSAL_REMEDY,
    ENGINE_REFUSAL_CAUSES,
    FRICTION,
    EngineRoundFailure,
    engine_refusal_details,
)
from cw.review_engine.context import EngineContext
from cw.review_engine.manifest_source import read_manifest_file
from tests._review_engine_helpers import (
    EngineRepo,
    make_engine_repo,
    manifest_directory_engine,
)
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable

_STUB = Path(__file__).parent / "fixtures" / "review_engine_stub"
_SKILL = "repo-review"
_ROLE = {"id": "security", "agent_spec": "agents/security.md"}


def _client(
    repo: EngineRepo,
    *,
    skill: str | None = _SKILL,
    default_branch: str = "main",
    guarded: list[str] | None = None,
) -> ClientConfig:
    review = (
        ReviewConfig()
        if skill is None
        else ReviewConfig(
            convergence="clean",
            engine=ReviewEngineConfig(skill=skill, guarded_paths=guarded or []),
        )
    )
    return ClientConfig(
        name=repo.client,
        workspace_path=repo.worktree,
        default_branch=default_branch,
        review=review,
    )


def _spec_or_block(
    repo: EngineRepo, client: ClientConfig
) -> EngineRoundSpec | AutoDevResult | None:
    task = _make_ticket_task(
        ticket_id=repo.ticket, client=repo.client, stage=Stage.REVIEW
    )
    return engine_round_spec(client, task, repo.worktree)


def _stub_with_manifest(tmp_path: Path, manifest: dict[str, object]) -> Path:
    engine = tmp_path / "engine-files"
    for source in _STUB.rglob("*"):
        if source.is_file():
            target = engine / source.relative_to(_STUB)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    (engine / "cw-engine.json").write_text(json.dumps(manifest), encoding="utf-8")
    return engine


def _refusal(outcome: EngineRoundSpec | AutoDevResult | None) -> str:
    """Assert *outcome* is the non-retryable manifest refusal; return its details."""
    assert isinstance(outcome, AutoDevResult)
    assert outcome.status == "blocked"
    assert outcome.blocker is not None
    assert outcome.blocker.reason == REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON
    assert outcome.blocker.retry_eligible is False
    return outcome.blocker.details


class TestEngineRoundSpec:
    def test_no_engine_configured_returns_none(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_engine_repo(make_git_repo, engine_files=_STUB)

        assert _spec_or_block(repo, _client(repo, skill=None)) is None

    def test_a_valid_manifest_returns_the_engine_context(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_engine_repo(make_git_repo, engine_files=_STUB)
        client = _client(repo, guarded=["scripts/validate.py"])

        outcome = _spec_or_block(repo, client)

        assert outcome == EngineRoundSpec(
            context=EngineContext(
                skill=_SKILL,
                guarded_paths=("scripts/validate.py",),
                ticket_id=repo.ticket,
                client=repo.client,
                default_branch="main",
            )
        )

    def test_an_absent_manifest_is_refused_with_no_codex_manifest(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_engine_repo(make_git_repo)

        details = _refusal(_spec_or_block(repo, _client(repo)))

        assert details == engine_refusal_details(
            "codex", "no_codex_manifest", f"{_SKILL} has no cw-engine.json"
        )
        assert "cause=no_codex_manifest" in details

    def test_a_single_file_command_layout_is_refused_with_no_codex_manifest(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_engine_repo(make_git_repo, layout=".claude/commands")

        details = _refusal(_spec_or_block(repo, _client(repo)))

        assert "cause=no_codex_manifest" in details
        assert "single-file command" in details

    def test_an_invalid_manifest_is_refused_with_manifest_invalid(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        engine = _stub_with_manifest(tmp_path, {})
        repo = make_engine_repo(make_git_repo, engine_files=engine, name="invalid")

        details = _refusal(_spec_or_block(repo, _client(repo)))

        assert "cause=manifest_invalid" in details
        assert "cw review engine validate --kind manifest" in details

    def test_a_manifest_directory_is_refused_with_manifest_invalid(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        """#3445: lane start reads a directory manifest as INVALID, like round time."""
        engine = manifest_directory_engine(tmp_path, _STUB)
        repo = make_engine_repo(make_git_repo, engine_files=engine, name="dir")

        details = _refusal(_spec_or_block(repo, _client(repo)))

        assert details == engine_refusal_details(
            "codex", "manifest_invalid", "unreadable: -"
        )
        assert "cause=no_codex_manifest" not in details

    def test_start_and_round_time_share_the_refusal_builder(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        """#3445: the same manifest condition gives byte-identical details."""
        engine = _stub_with_manifest(tmp_path, {})
        repo = make_engine_repo(make_git_repo, engine_files=engine, name="parity")

        outcome = _spec_or_block(repo, _client(repo))

        round_time = manifest_refusal_blocked(
            ticket_id=repo.ticket,
            worktree=repo.worktree,
            read=read_manifest_file(engine / "cw-engine.json", _SKILL),
        )
        assert round_time.blocker is not None
        assert _refusal(outcome) == round_time.blocker.details
        assert round_time.blocker.retry_eligible is False

    def test_a_backends_failure_is_refused_with_codex_not_declared(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        engine = _stub_with_manifest(
            tmp_path,
            {
                "manifest_version": 1,
                "backends": ["claude-native"],
                "roles": [_ROLE],
                "aggregation_prompt": "prompts/aggregate.md",
            },
        )
        repo = make_engine_repo(make_git_repo, engine_files=engine, name="no-codex")

        details = _refusal(_spec_or_block(repo, _client(repo)))

        assert "cause=codex_not_declared" in details
        assert 'list "codex" in the manifest' in details

    @pytest.mark.parametrize(
        ("skill", "default_branch", "cause"),
        [
            (_SKILL, "nonexistent", "default_branch_unresolved"),
            ("no-such-skill", "main", "skill_not_found"),
        ],
        ids=["default-branch", "skill"],
    )
    def test_an_unresolvable_engine_is_a_retryable_agent_block(
        self,
        make_git_repo: Callable[..., Path],
        skill: str,
        default_branch: str,
        cause: str,
    ) -> None:
        repo = make_engine_repo(make_git_repo, engine_files=_STUB)
        client = _client(repo, skill=skill, default_branch=default_branch)

        outcome = _spec_or_block(repo, client)

        assert isinstance(outcome, AutoDevResult)
        assert outcome.status == "blocked"
        assert outcome.blocker is not None
        assert outcome.blocker.reason == "agent_block"
        assert outcome.blocker.retry_eligible is True
        assert outcome.blocker.details == FRICTION[
            EngineRoundFailure.ENGINE_UNRESOLVABLE
        ].format(skill=skill, cause=cause)

    def test_the_unresolvable_block_is_the_shared_round_time_builder(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#3438: lane start and round time build the same block."""
        repo = make_engine_repo(make_git_repo, engine_files=_STUB)
        client = _client(repo, default_branch="nonexistent")

        outcome = _spec_or_block(repo, client)

        shared = engine_unresolvable_blocked(
            ticket_id=repo.ticket,
            worktree=repo.worktree,
            skill=_SKILL,
            cause="default_branch_unresolved",
        )
        assert isinstance(outcome, AutoDevResult)
        assert outcome.blocker is not None
        assert shared.blocker is not None
        assert outcome.blocker.reason == shared.blocker.reason == "agent_block"
        assert outcome.blocker.details == shared.blocker.details
        assert outcome.blocker.retry_eligible is shared.blocker.retry_eligible is True


class TestEngineRefusalDetails:
    @pytest.mark.parametrize(
        ("cause", "remedy"),
        [
            (
                "no_codex_manifest",
                "add cw-engine.json beside SKILL.md on the default branch, or "
                "point review.engine at a skill that has one",
            ),
            ("codex_not_declared", 'list "codex" in the manifest\'s backends'),
            (
                "manifest_invalid",
                "repair cw-engine.json (see `cw review engine validate --kind "
                "manifest`)",
            ),
            (
                "backend_unsupported",
                "run review on a claude-native or codex lane, or remove review.engine",
            ),
        ],
    )
    def test_the_text_is_pinned_with_and_without_a_detail(
        self, cause: str, remedy: str
    ) -> None:
        head = f"review engine refused on codex lane: cause={cause}"
        tail = "then cw dev-queue requeue"

        assert engine_refusal_details("codex", cause) == f"{head}; {remedy}; {tail}"
        assert (
            engine_refusal_details("codex", cause, "some detail")
            == f"{head}; some detail; {remedy}; {tail}"
        )

    def test_the_cause_vocabulary_and_the_remedy_table_agree(self) -> None:
        assert {
            "no_codex_manifest",
            "codex_not_declared",
            "manifest_invalid",
            "backend_unsupported",
        } == ENGINE_REFUSAL_CAUSES
        assert set(_REFUSAL_REMEDY) == set(ENGINE_REFUSAL_CAUSES)

    def test_the_backend_is_named(self) -> None:
        text = engine_refusal_details("opencode", "backend_unsupported", "skill=x")

        assert text.startswith(
            "review engine refused on opencode lane: cause=backend_unsupported; "
            "skill=x; "
        )
