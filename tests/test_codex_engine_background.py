"""A codex lane running a repo review engine, through the background review (#3216).

Drives ``cw.codex_background._review_and_confirm`` (and the unit above it) over a
real tmp git repo carrying the stub engine, with a scripted codex runner. The
two Claude-boundary tests are the load-bearing ones: the engine round itself
makes no Claude call (R22), and a clean engine verdict under the default confirm
backend leads to the confirm round's Claude calls and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.claude_review import ClaudeRunResult
from cw.claude_review._runner import FakeClaudeRunner, RealClaudeRunner
from cw.codex_background import _review_and_confirm, _run_codex_review_and_complete
from cw.codex_background_types import ReviewOutcome
from cw.codex_runner import CodexRunResult
from cw.config import load_state, save_state
from cw.models import ClientConfig, CwState, OrchestratorConfig, Stage, TicketTask
from cw.models.client import ReviewConfig
from cw.review_engine.constants import (
    ENGINE_AGGREGATOR_PREAMBLE,
    ENGINE_ROLE,
    ENGINE_ROLE_PREAMBLE,
)
from tests._claude_review_helpers import RoleAwareClaudeRunner, review_envelope
from tests._codex_review_helpers import _doc_json, _ok_result
from tests._review_engine_helpers import (
    EngineRepo,
    make_aggregation_output,
    make_engine_repo,
)
from tests._tool_fakes import ScriptedCodexRunner, stdin_contains
from tests.conftest import _make_daemon_session, git_in

if TYPE_CHECKING:
    from collections.abc import Callable

_STUB = Path(__file__).parent / "fixtures" / "review_engine_stub"
_SKILL = "repo-review"
_BUDGET = 600


@pytest.fixture(autouse=True)
def _no_ticket_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cw.codex_review._context.core._fetch_ticket_comments",
        lambda _worktree, _ticket_id: None,
    )


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo, engine_files=_STUB)


@pytest.fixture
def no_real_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real ``RealClaudeRunner`` can never launch ``claude`` in this module."""

    def _refuse(*_args: object, **_kwargs: object) -> ClaudeRunResult:
        msg = "RealClaudeRunner.run must not be reached"
        raise AssertionError(msg)

    monkeypatch.setattr(RealClaudeRunner, "run", _refuse)
    monkeypatch.setattr("cw.codex_clean_confirm._which_claude", lambda: "/x/claude")


def _client(repo: EngineRepo, *, confirm_backend: str | None = None) -> ClientConfig:
    review: dict[str, object] = {
        "convergence": "clean",
        "engine": {"skill": _SKILL},
    }
    if confirm_backend is not None:
        review["confirm_backend"] = confirm_backend
    return ClientConfig(
        name=repo.client,
        workspace_path=repo.worktree,
        default_branch="main",
        review=ReviewConfig.model_validate(review),
    )


def _task(repo: EngineRepo) -> TicketTask:
    return TicketTask(
        ticket_id=repo.ticket,
        client=repo.client,
        stage=Stage.REVIEW,
        scope_hint="small",
    )


def _engine_runner() -> ScriptedCodexRunner:
    """Two clean role documents, then a clean aggregation."""
    clean = json.dumps(make_aggregation_output())
    return (
        ScriptedCodexRunner()
        .add(
            stdin_contains(ENGINE_AGGREGATOR_PREAMBLE),
            CodexRunResult(0, "", "", output_file_content=clean),
        )
        .add(stdin_contains("# Security role"), _ok_result("security"))
        .add(stdin_contains("# Quality role"), _ok_result("quality"))
    )


def _review(
    repo: EngineRepo,
    client: ClientConfig,
    runner: ScriptedCodexRunner,
    *,
    budget: int | None = None,
) -> ReviewOutcome:
    return _review_and_confirm(
        runner=runner,
        task=_task(repo),
        worktree=repo.worktree,
        client=client,
        wall_clock_budget_seconds=budget,
        deadline=None,
        sid="bg-engine",
        config_model=None,
        config_reasoning_effort=None,
    )


class TestEngineRoundMakesNoClaudeCall:
    """R22: no Claude call from the preflight, the roles, the aggregator or record."""

    def test_confirm_backend_none_never_touches_claude(
        self,
        repo: EngineRepo,
        monkeypatch: pytest.MonkeyPatch,
        no_real_claude: None,
    ) -> None:
        fake = FakeClaudeRunner()
        monkeypatch.setattr("cw.codex_background.RealClaudeRunner", lambda: fake)
        runner = _engine_runner()

        outcome = _review(repo, _client(repo, confirm_backend="none"), runner)

        assert fake.calls == []
        stdins = [str(call["stdin"]) for call in runner.review_calls]
        assert len(stdins) == 3
        assert stdins[2].startswith(ENGINE_AGGREGATOR_PREAMBLE)
        assert ENGINE_ROLE_PREAMBLE in stdins[0]
        assert outcome.result.status == "stage_complete"
        assert outcome.result.blocker is None
        assert outcome.verdict is not None
        assert [r.reviewer_role for r in outcome.verdict.agents_run] == [ENGINE_ROLE]

    def test_the_default_confirm_backend_makes_only_the_confirm_rounds_calls(
        self,
        repo: EngineRepo,
        monkeypatch: pytest.MonkeyPatch,
        no_real_claude: None,
    ) -> None:
        claude = RoleAwareClaudeRunner(
            lambda role: ClaudeRunResult(0, review_envelope(_doc_json(role=role)), "")
        )
        monkeypatch.setattr("cw.codex_background.RealClaudeRunner", lambda: claude)
        runner = _engine_runner()

        outcome = _review(repo, _client(repo), runner)

        assert claude.roles, "the default backend runs a Claude confirm round"
        assert ENGINE_ROLE not in claude.roles
        for prompt in claude.prompts:
            assert ENGINE_ROLE_PREAMBLE not in prompt
            assert ENGINE_AGGREGATOR_PREAMBLE not in prompt
            assert "cw-engine.json" not in prompt
        assert len(runner.review_calls) == 3
        assert outcome.result.blocker is None


class TestWallClockBudget:
    """ADR-0014: the engine adds no budget of its own to the roles or aggregator."""

    def test_roles_and_aggregator_run_under_the_lanes_budget(
        self, repo: EngineRepo, no_real_claude: None
    ) -> None:
        runner = _engine_runner()

        with patch("cw.codex_background.RealClaudeRunner", FakeClaudeRunner):
            _review(repo, _client(repo, confirm_backend="none"), runner, budget=_BUDGET)

        timeouts = [call["timeout"] for call in runner.review_calls]
        *role_timeouts, aggregator_timeout = timeouts
        assert aggregator_timeout == _BUDGET
        assert len(role_timeouts) == 2
        assert all(isinstance(t, int) and 0 < t <= _BUDGET for t in role_timeouts)


class TestStartupRefusal:
    def test_an_absent_manifest_parks_before_the_resume_check_and_any_codex_call(
        self, make_git_repo: Callable[..., Path], no_real_claude: None
    ) -> None:
        repo = make_engine_repo(make_git_repo)
        runner = _engine_runner()

        with (
            patch("cw.codex_background.resume_pending_confirm") as resume,
            patch("cw.codex_background.run_review_with_fix_loop") as loop,
        ):
            outcome = _review(repo, _client(repo), runner)

        resume.assert_not_called()
        loop.assert_not_called()
        assert runner.calls == []
        assert outcome.verdict is None
        assert outcome.result.status == "blocked"
        assert outcome.result.blocker is not None
        assert outcome.result.blocker.reason == "review_engine_unsupported_backend"
        assert outcome.result.blocker.retry_eligible is False
        assert "cause=no_codex_manifest" in outcome.result.blocker.details


class TestStagedSentinel:
    def test_the_persisted_sentinel_carries_the_stamped_stage_facts(
        self, repo: EngineRepo, no_real_claude: None
    ) -> None:
        """The A1 path: ``stamp_stage_facts`` runs on the engine result too."""
        save_state(CwState(sessions=[_make_daemon_session(id="bg-engine")]))
        client = _client(repo, confirm_backend="none")

        with (
            patch("cw.codex_background.RealClaudeRunner", FakeClaudeRunner),
            patch(
                "cw.codex_background.load_effective_config",
                return_value=OrchestratorConfig(),
            ),
            patch("cw.codex_background._post_review_comment"),
        ):
            _run_codex_review_and_complete(
                runner=_engine_runner(),
                task=_task(repo),
                worktree=repo.worktree,
                client=client,
                wall_clock_budget_seconds=None,
                sid="bg-engine",
                sess_name=f"{client.name}/auto-dev/{repo.ticket}",
                config_model=None,
                config_reasoning_effort=None,
            )

        persisted = AutoDevResult.model_validate(load_state().sessions[0].last_result)
        assert persisted.status == "stage_complete"
        assert persisted.plan_source is not None
        assert persisted.fork_point_sha == git_in(
            repo.worktree, "merge-base", "origin/main", "HEAD"
        )
        assert persisted.commits == []

    def test_a_startup_refusal_is_persisted_as_the_sessions_result(
        self, make_git_repo: Callable[..., Path], no_real_claude: None
    ) -> None:
        repo = make_engine_repo(make_git_repo)
        save_state(CwState(sessions=[_make_daemon_session(id="bg-refuse")]))
        client = _client(repo)
        runner = _engine_runner()

        with (
            patch(
                "cw.codex_background.load_effective_config",
                return_value=OrchestratorConfig(),
            ),
            patch("cw.codex_background._post_review_comment"),
            patch("cw.codex_background.resume_pending_confirm") as resume,
        ):
            _run_codex_review_and_complete(
                runner=runner,
                task=_task(repo),
                worktree=repo.worktree,
                client=client,
                wall_clock_budget_seconds=None,
                sid="bg-refuse",
                sess_name=f"{client.name}/auto-dev/{repo.ticket}",
                config_model=None,
                config_reasoning_effort=None,
            )

        resume.assert_not_called()
        assert runner.calls == []
        persisted = AutoDevResult.model_validate(load_state().sessions[0].last_result)
        assert persisted.status == "blocked"
        assert persisted.blocker is not None
        assert persisted.blocker.reason == "review_engine_unsupported_backend"
        assert persisted.blocker.retry_eligible is False
