"""One repo review engine pass on a codex review lane (#3216, RFC 0018 A4).

Drives ``cw.codex_review._engine_round.run_engine_pass`` over a real tmp git
repo whose default branch carries the stub engine
(``tests/fixtures/review_engine_stub``: a manifest, two role specs and an
aggregation prompt), a scripted codex runner and a fixed ``round_id``. Every
failure is a blocked result; none records an event.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from cw.auto_dev_result import (
    REVIEW_ENGINE_MODIFIED_BLOCKER_REASON,
    REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON,
)
from cw.codex_review import (
    CODEX_MUST_FIX_FINDINGS,
    _prepare_review_pass,
    synthesize_codex_review_result,
)
from cw.codex_review._context import EngineRoster
from cw.codex_review._engine_round import (
    EnginePassInputs,
    EnginePassOutcome,
    EngineRoundSpec,
    _role_failure,
    engine_unresolvable_blocked,
    run_engine_pass,
)
from cw.codex_review._shard import ReviewInputTooLargeError, plan_prompt_shards
from cw.codex_review._shard_run import ShardedRun
from cw.codex_runner import CodexRunResult
from cw.events import read_events
from cw.models import Stage
from cw.review_adjudication import VoidedFinding
from cw.review_engine.constants import (
    ENGINE_AGGREGATOR_PREAMBLE,
    ENGINE_ROLE,
    ENGINE_ROLE_PREAMBLE,
    FRICTION,
    EngineRoundFailure,
)
from cw.review_engine.context import EngineContext
from cw.review_engine.materialize import MaterializeError
from cw.review_finding_dispositions import FindingDisposition, _disposition_key
from cw.review_findings import ShardRecord
from tests._codex_review_helpers import (
    _doc_json,
    _ok_result,
    _shard_limit_inputs,
    _stem_content,
)
from tests._review_engine_helpers import (
    EngineRepo,
    engine_finding,
    make_aggregation_output,
    make_engine_repo,
)
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._shared.session_seeding import _make_ticket_task
from tests._tool_fakes import CodexReply, ScriptedCodexRunner, stdin_contains

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import TicketTask

_STUB = Path(__file__).parent / "fixtures" / "review_engine_stub"
_SECURITY_SPEC = (_STUB / "agents" / "security.md").read_text(encoding="utf-8")
_QUALITY_SPEC = (_STUB / "agents" / "quality.md").read_text(encoding="utf-8")
_ROUND = "round-0"
_LOGGER = "cw.codex_review._engine_round"
_SKILL = "repo-review"
_SECURITY_MARK = "# Security role"
_QUALITY_MARK = "# Quality role"
_SKILL_DIR = f".claude/skills/{_SKILL}"
_MUST_FIX = engine_finding(
    "MUST_FIX",
    rule_id="security.injection",
    summary="Unsafe command construction.",
    evidence="x = 1",
)


@pytest.fixture(autouse=True)
def _no_ticket_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pass's ``gh`` comment fetch answers "no comments" without a launch."""
    monkeypatch.setattr(
        "cw.codex_review._context.core._fetch_ticket_comments",
        lambda _worktree, _ticket_id: None,
    )


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo, engine_files=_STUB)


@pytest.fixture
def record_event_spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace every ``record_event`` binding in an imported ``cw`` module."""
    spy = MagicMock()
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] == "cw" and hasattr(module, "record_event"):
            monkeypatch.setattr(module, "record_event", spy)
    return spy


def _task(repo: EngineRepo, **overrides: object) -> TicketTask:
    return _make_ticket_task(
        ticket_id=repo.ticket,
        client=repo.client,
        stage=Stage.REVIEW,
        scope_hint="small",
        **overrides,
    )


def _spec(
    repo: EngineRepo, *, skill: str = _SKILL, default_branch: str = "main"
) -> EngineRoundSpec:
    return EngineRoundSpec(
        context=EngineContext(
            skill=skill,
            guarded_paths=(),
            ticket_id=repo.ticket,
            client=repo.client,
            default_branch=default_branch,
        )
    )


def _inputs(
    repo: EngineRepo,
    runner: ScriptedCodexRunner,
    *,
    task: TicketTask | None = None,
    wall_clock_budget_seconds: int | None = None,
    max_prompt_chars: int | None = None,
) -> EnginePassInputs:
    return EnginePassInputs(
        runner=runner,
        task=task or _task(repo),
        worktree=repo.worktree,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        session_id="s-engine",
        fix_loop_enabled=True,
        prior_open_findings=None,
        claim_tier_enabled=False,
        disposition_drift_check_enabled=True,
        settled_suppression=None,
        max_prompt_chars=max_prompt_chars,
    )


def _aggregation(
    findings: list[dict[str, object]] | None = None, **overrides: object
) -> CodexRunResult:
    """An exit-0 aggregation turn whose ``-o`` file holds a valid output."""
    content = json.dumps(make_aggregation_output(findings, **overrides))
    return CodexRunResult(0, "", "", output_file_content=content)


def _engine_runner(
    *,
    security: CodexReply | None = None,
    quality: CodexReply | None = None,
    aggregation: CodexReply | None = None,
) -> ScriptedCodexRunner:
    """Role and aggregator rules, keyed on the spec text and the preamble."""
    return (
        ScriptedCodexRunner()
        .add(
            stdin_contains(ENGINE_AGGREGATOR_PREAMBLE),
            aggregation or _aggregation(),
        )
        .add(stdin_contains(_SECURITY_MARK), security or _ok_result("security"))
        .add(stdin_contains(_QUALITY_MARK), quality or _ok_result("quality"))
    )


def _pass(
    repo: EngineRepo,
    runner: ScriptedCodexRunner,
    *,
    cycle: int = 0,
    round_id: str | None = _ROUND,
    spec: EngineRoundSpec | None = None,
    inputs: EnginePassInputs | None = None,
) -> EnginePassOutcome:
    return run_engine_pass(
        spec or _spec(repo),
        inputs or _inputs(repo, runner),
        cycle=cycle,
        round_id=round_id,
    )


def _stdins(runner: ScriptedCodexRunner) -> list[str]:
    return [str(call["stdin"]) for call in runner.review_calls]


def _role_stdins(runner: ScriptedCodexRunner) -> list[str]:
    return [s for s in _stdins(runner) if not s.startswith(ENGINE_AGGREGATOR_PREAMBLE)]


def _aggregator_stdins(runner: ScriptedCodexRunner) -> list[str]:
    return [s for s in _stdins(runner) if s.startswith(ENGINE_AGGREGATOR_PREAMBLE)]


def _blocked(
    outcome: EnginePassOutcome, *, reason: str = "agent_block", retry: bool = True
) -> str:
    """Assert *outcome* is a verdict-less block; return its details."""
    assert outcome.result.status == "blocked"
    blocker = outcome.result.blocker
    assert blocker is not None
    assert blocker.reason == reason
    assert blocker.retry_eligible is retry
    assert outcome.verdict is None
    return blocker.details


def _failure_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == logging.WARNING
    ]


class TestPass:
    def test_two_roles_then_the_aggregator_give_a_clean_engine_document(
        self, repo: EngineRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _spy(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return synthesize_codex_review_result(**kwargs)

        monkeypatch.setattr(
            "cw.codex_review._engine_round.synthesize_codex_review_result", _spy
        )
        runner = _engine_runner()

        outcome = _pass(repo, runner)

        assert outcome.result.status == "stage_complete"
        assert outcome.result.blocker is None
        assert outcome.verdict is not None
        assert outcome.verdict.blocking is False
        assert [r.reviewer_role for r in outcome.verdict.agents_run] == [ENGINE_ROLE]
        assert outcome.prepared is not None
        assert outcome.prepared.roles == ["security", "quality"]
        (document,) = captured["documents"]
        assert document.reviewer_role == ENGINE_ROLE
        assert captured["failures"] == []
        stdins = _stdins(runner)
        assert len(stdins) == 3
        assert stdins[2].startswith(ENGINE_AGGREGATOR_PREAMBLE)
        assert _SECURITY_MARK in stdins[0]
        assert _QUALITY_MARK in stdins[1]
        summary = repo.worktree / ".cw" / "review-engine" / _ROUND / "summary.json"
        assert json.loads(summary.read_text(encoding="utf-8"))["head_sha"] == repo.head

    def test_each_role_spec_follows_the_role_preamble_under_its_heading(
        self, repo: EngineRepo
    ) -> None:
        runner = _engine_runner()

        _pass(repo, runner)

        security, quality, aggregator = _stdins(runner)
        assert (
            f"## Agent Specification\n{ENGINE_ROLE_PREAMBLE}\n\n{_SECURITY_SPEC}"
            in security
        )
        assert (
            f"## Agent Specification\n{ENGINE_ROLE_PREAMBLE}\n\n{_QUALITY_SPEC}"
            in quality
        )
        assert aggregator.startswith(ENGINE_AGGREGATOR_PREAMBLE)
        assert "## Aggregation Instructions" in aggregator
        assert f"Reviewed head: {repo.head}" in aggregator

    def test_a_blocking_finding_blocks_with_a_verdict(self, repo: EngineRepo) -> None:
        runner = _engine_runner(aggregation=_aggregation([_MUST_FIX]))

        outcome = _pass(repo, runner)

        assert outcome.result.status == "blocked"
        assert outcome.result.blocker is not None
        assert outcome.result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert outcome.verdict is not None
        assert outcome.verdict.blocking is True
        assert [f.rule_id for f in outcome.verdict.must_fix] == ["security.injection"]
        assert outcome.verdict.must_fix[0].source == "engine"

    def test_a_default_round_id_is_generated(
        self, repo: EngineRepo, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _pass(repo, _engine_runner(), cycle=2, round_id=None)

        (info,) = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
        assert "round_id=codex-2-" in info


class TestObservability:
    def test_one_info_per_pass_and_no_event(
        self,
        repo: EngineRepo,
        caplog: pytest.LogCaptureFixture,
        record_event_spy: MagicMock,
    ) -> None:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _pass(repo, _engine_runner(aggregation=_aggregation([_MUST_FIX])), cycle=3)

        infos = [
            r.getMessage()
            for r in caplog.records
            if r.name == _LOGGER and r.levelno == logging.INFO
        ]
        assert infos == [
            f"codex engine round: ticket={repo.ticket} cycle=3 roles=2 "
            f"round_id={_ROUND}"
        ]
        assert _failure_warnings(caplog) == []
        record_event_spy.assert_not_called()
        assert read_events() == []

    def test_one_warning_per_failure_and_no_event(
        self,
        repo: EngineRepo,
        caplog: pytest.LogCaptureFixture,
        record_event_spy: MagicMock,
    ) -> None:
        runner = _engine_runner(security=CodexRunResult(1, "", "boom"))

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _pass(repo, runner, cycle=1)

        assert _failure_warnings(caplog) == [
            f"codex engine round failed: ticket={repo.ticket} cycle=1 "
            "failure=engine_role_failed"
        ]
        record_event_spy.assert_not_called()
        assert read_events() == []

    def test_a_park_warns_once_and_records_no_event(
        self,
        repo: EngineRepo,
        caplog: pytest.LogCaptureFixture,
        record_event_spy: MagicMock,
    ) -> None:
        commit_tracked_file(repo.worktree, f"{_SKILL_DIR}/notes.md", "edited\n")

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _pass(repo, _engine_runner())

        assert _failure_warnings(caplog) == [
            f"codex engine round failed: ticket={repo.ticket} cycle=0 "
            f"failure={REVIEW_ENGINE_MODIFIED_BLOCKER_REASON}"
        ]
        record_event_spy.assert_not_called()


class TestRoleFailures:
    @pytest.mark.parametrize(
        ("reply", "reason", "retry"),
        [
            (CodexRunResult(-1, "", "", timed_out=True), "codex_timeout", True),
            (CodexRunResult(1, "", "boom"), "codex_error", False),
            (
                CodexRunResult(0, "", "", output_file_content="{not json"),
                "codex_review_unparseable",
                False,
            ),
            (
                CodexRunResult(
                    0,
                    "",
                    "",
                    output_file_content=_doc_json(role="security", status="failed"),
                ),
                "failed_document",
                False,
            ),
        ],
        ids=["timeout", "nonzero", "invalid-json", "failed-document"],
    )
    def test_a_failed_role_skips_the_aggregator_and_blocks_retryably(
        self, repo: EngineRepo, reply: CodexRunResult, reason: str, retry: bool
    ) -> None:
        runner = _engine_runner(security=reply)

        outcome = _pass(repo, runner)

        details = _blocked(outcome, retry=retry)
        assert details == FRICTION[EngineRoundFailure.ENGINE_ROLE_FAILED].format(
            role="security", reason=reason
        )
        assert _aggregator_stdins(runner) == []
        assert outcome.prepared is not None

    def test_an_exhausted_budget_fails_the_first_role(self, repo: EngineRepo) -> None:
        runner = _engine_runner()
        inputs = _inputs(repo, runner, wall_clock_budget_seconds=1)

        outcome = _pass(repo, runner, inputs=inputs)

        assert _blocked(outcome) == FRICTION[
            EngineRoundFailure.ENGINE_ROLE_FAILED
        ].format(role="security", reason="budget_exhausted")
        assert _stdins(runner) == []

    def test_a_second_role_failure_is_reported_when_the_first_succeeds(
        self, repo: EngineRepo
    ) -> None:
        runner = _engine_runner(quality=CodexRunResult(-1, "", "", timed_out=True))

        outcome = _pass(repo, runner)

        assert "quality (codex_timeout)" in _blocked(outcome)

    def test_an_unreviewed_shard_is_a_role_failure_of_its_own(self) -> None:
        unreviewed = ShardRecord(
            index=2,
            files=["a.py"],
            prompt_chars=10,
            roles_reviewed=["security"],
            reviewed=False,
        )
        run = ShardedRun([], [], {}, [], [unreviewed])

        assert _role_failure(run) == ("*", "unreviewed_shard")
        assert _role_failure(ShardedRun([], [], {}, [], [])) is None


class TestShardedRoles:
    def _sharded_repo(self, repo: EngineRepo) -> tuple[int, int]:
        """Three more files; returns ``(limit that shards them, shard count)``."""
        for relpath in ("pkg_a/a1.py", "pkg_b/b1.py", "pkg_c/c1.py"):
            commit_tracked_file(repo.worktree, relpath, _stem_content(relpath, 60))
        roster = EngineRoster(
            (
                ("security", f"{ENGINE_ROLE_PREAMBLE}\n\n{_SECURITY_SPEC}"),
                ("quality", f"{ENGINE_ROLE_PREAMBLE}\n\n{_QUALITY_SPEC}"),
            )
        )
        prepared = _prepare_review_pass(
            _task(repo),
            repo.worktree,
            "main",
            config_branch="main",
            runner=ScriptedCodexRunner(),
            session_id="s-limit",
            engine_roster=roster,
        )
        overhead, costs = _shard_limit_inputs(prepared)
        limit = overhead + 2 * max(costs.values())
        shards = len(plan_prompt_shards(prepared, limit))
        assert shards > 1
        return limit, shards

    def test_every_role_reviews_every_shard_then_the_aggregator_runs(
        self, repo: EngineRepo
    ) -> None:
        limit, shards = self._sharded_repo(repo)
        runner = _engine_runner()

        outcome = _pass(
            repo, runner, inputs=_inputs(repo, runner, max_prompt_chars=limit)
        )

        assert outcome.result.status == "stage_complete"
        assert len(_role_stdins(runner)) == 2 * shards
        assert len(_aggregator_stdins(runner)) == 1

    def test_a_role_failing_on_a_shard_fails_the_round(self, repo: EngineRepo) -> None:
        limit, _shards = self._sharded_repo(repo)
        runner = _engine_runner(security=CodexRunResult(1, "", "boom"))

        outcome = _pass(
            repo, runner, inputs=_inputs(repo, runner, max_prompt_chars=limit)
        )

        assert "security (codex_error)" in _blocked(outcome, retry=False)
        assert _aggregator_stdins(runner) == []


class TestAggregatorFailures:
    @pytest.mark.parametrize(
        ("reply", "prefix", "retry"),
        [
            (
                CodexRunResult(-1, "", "", timed_out=True),
                "engine_aggregation_failed: codex_timeout",
                True,
            ),
            (
                CodexRunResult(2, "", "boom"),
                "engine_aggregation_failed: codex_error",
                False,
            ),
            (
                CodexRunResult(0, "", "", output_file_content=""),
                "engine_aggregation_failed: empty_output",
                False,
            ),
            (
                CodexRunResult(0, "", "", output_file_content=None),
                "engine_aggregation_failed: missing_output",
                False,
            ),
            (
                CodexRunResult(0, "", "", output_file_content="{nope"),
                "engine_aggregation_failed: invalid_json",
                False,
            ),
            (
                _aggregation(counts={}),
                "engine_aggregation_failed: schema_mismatch",
                False,
            ),
            (
                _aggregation(coverage="PARTIAL"),
                "engine_coverage_incomplete: coverage=PARTIAL",
                True,
            ),
            (
                _aggregation(coverage="FAILED"),
                "engine_coverage_incomplete: coverage=FAILED",
                True,
            ),
            (
                _aggregation([engine_finding("NIT")], verdict="FAIL"),
                "engine_summary_invalid: fail_without_gating_finding",
                True,
            ),
        ],
        ids=[
            "timeout",
            "nonzero",
            "empty",
            "missing",
            "invalid-json",
            "schema-invalid",
            "partial",
            "failed",
            "fail-without-gating",
        ],
    )
    def test_each_failure_blocks_retryably_with_its_friction(
        self, repo: EngineRepo, reply: CodexRunResult, prefix: str, retry: bool
    ) -> None:
        runner = _engine_runner(aggregation=reply)

        outcome = _pass(repo, runner)

        assert _blocked(outcome, retry=retry).startswith(prefix)
        assert len(_aggregator_stdins(runner)) == 1
        assert outcome.prepared is not None

    def test_a_head_that_moved_during_the_round_is_a_head_mismatch(
        self, repo: EngineRepo
    ) -> None:
        def _commit_in_a_role(_argv: list[str], _stdin: str | None) -> CodexRunResult:
            commit_tracked_file(repo.worktree, "src/moved.py", "y = 2\n")
            return _ok_result("security")

        runner = _engine_runner(security=_commit_in_a_role)

        outcome = _pass(repo, runner)

        assert _blocked(outcome, retry=True).startswith(
            "engine_head_mismatch: summary=-"
        )

    def test_a_role_that_dirties_the_tree_is_a_worktree_mutation(
        self, repo: EngineRepo
    ) -> None:
        def _edit_in_a_role(_argv: list[str], _stdin: str | None) -> CodexRunResult:
            (repo.worktree / "src" / "a.py").write_text("x = 99\n", encoding="utf-8")
            return _ok_result("quality")

        runner = _engine_runner(quality=_edit_in_a_role)

        outcome = _pass(repo, runner)

        assert (
            _blocked(outcome, retry=False)
            == FRICTION[EngineRoundFailure.ENGINE_WORKTREE_MUTATED]
        )

    def test_a_summary_write_failure_is_engine_no_summary_and_not_retryable(
        self, repo: EngineRepo
    ) -> None:
        """A directory where the summary file goes: cw cannot write it.

        cw writes the summary on the codex lane, so this is an environment
        failure outside the transient retry policy: a plain requeue would hit
        the same unwritable path.
        """
        summary_dir = repo.worktree / ".cw" / "review-engine" / _ROUND / "summary.json"

        def _block_summary(_argv: list[str], _stdin: str | None) -> CodexRunResult:
            summary_dir.mkdir(parents=True, exist_ok=True)
            return _aggregation()

        runner = _engine_runner(aggregation=_block_summary)

        outcome = _pass(repo, runner)

        assert _blocked(outcome, retry=False).startswith("engine_no_summary:")

    def test_an_unwritable_aggregation_scratch_is_engine_no_summary(
        self, repo: EngineRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#3436: the scratch OSError reaches the pass as a typed block."""

        def _unwritable(_session_id: str) -> Path:
            msg = "read-only state dir"
            raise OSError(msg)

        monkeypatch.setattr(
            "cw.codex_review._engine_aggregate._codex_scratch_dir", _unwritable
        )
        runner = _engine_runner()

        outcome = _pass(repo, runner)

        assert _blocked(outcome, retry=False).startswith("engine_no_summary:")
        assert _aggregator_stdins(runner) == []


class TestPreflight:
    def test_a_branch_touching_the_engine_parks_before_any_codex_call(
        self, repo: EngineRepo
    ) -> None:
        touched = f"{_SKILL_DIR}/notes.md"
        commit_tracked_file(repo.worktree, touched, "edited\n")
        runner = _engine_runner()

        outcome = _pass(repo, runner)

        details = _blocked(
            outcome, reason=REVIEW_ENGINE_MODIFIED_BLOCKER_REASON, retry=False
        )
        assert touched in details
        assert "cw review engine allow" in details
        assert runner.calls == []
        assert outcome.prepared is None

    def test_an_allowed_head_runs_the_round(
        self, repo: EngineRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commit_tracked_file(repo.worktree, f"{_SKILL_DIR}/notes.md", "edited\n")
        seen: list[tuple[str, str, str]] = []

        def _allowed(ticket_id: str, client: str, head: str) -> bool:
            seen.append((ticket_id, client, head))
            return True

        monkeypatch.setattr(
            "cw.review_engine.preflight.review_engine_head_allowed", _allowed
        )
        runner = _engine_runner()

        outcome = _pass(repo, runner)

        assert outcome.result.status == "stage_complete"
        assert seen == [
            (repo.ticket, repo.client, git_in(repo.worktree, "rev-parse", "HEAD"))
        ]
        assert len(_stdins(runner)) == 3

    def test_the_preflight_runs_on_every_round(self, repo: EngineRepo) -> None:
        """R19: a fix commit that touches the engine parks the next cycle."""
        runner = _engine_runner()
        first = _pass(repo, runner, cycle=0, round_id="round-0")
        assert first.result.status == "stage_complete"
        calls_after_first = len(runner.calls)
        commit_tracked_file(repo.worktree, f"{_SKILL_DIR}/refs/extra.md", "fix\n")

        second = _pass(repo, runner, cycle=1, round_id="round-1")

        details = _blocked(
            second, reason=REVIEW_ENGINE_MODIFIED_BLOCKER_REASON, retry=False
        )
        assert f"{_SKILL_DIR}/refs/extra.md" in details
        assert len(runner.calls) == calls_after_first

    def test_an_unresolvable_default_branch_is_retryable_agent_block(
        self, repo: EngineRepo
    ) -> None:
        runner = _engine_runner()

        outcome = _pass(repo, runner, spec=_spec(repo, default_branch="nonexistent"))

        assert _blocked(outcome) == FRICTION[
            EngineRoundFailure.ENGINE_UNRESOLVABLE
        ].format(skill=_SKILL, cause="default_branch_unresolved")
        assert runner.calls == []

    def test_the_unresolvable_block_is_the_shared_lane_start_builder(
        self, repo: EngineRepo
    ) -> None:
        """#3438: round time and lane start build the same block."""
        outcome = _pass(
            repo, _engine_runner(), spec=_spec(repo, default_branch="nonexistent")
        )

        shared = engine_unresolvable_blocked(
            ticket_id=repo.ticket,
            worktree=repo.worktree,
            skill=_SKILL,
            cause="default_branch_unresolved",
        )
        assert shared.blocker is not None
        details = _blocked(outcome, reason=shared.blocker.reason, retry=True)
        assert details == shared.blocker.details

    def test_a_skill_that_is_not_on_the_default_branch_is_unresolvable(
        self, repo: EngineRepo
    ) -> None:
        outcome = _pass(repo, _engine_runner(), spec=_spec(repo, skill="no-such-skill"))

        assert _blocked(outcome) == FRICTION[
            EngineRoundFailure.ENGINE_UNRESOLVABLE
        ].format(skill="no-such-skill", cause="skill_not_found")

    def test_a_dirty_tree_before_the_materialize_is_unresolvable(
        self, repo: EngineRepo
    ) -> None:
        (repo.worktree / "src" / "a.py").write_text("x = 5\n", encoding="utf-8")
        runner = _engine_runner()

        outcome = _pass(repo, runner)

        assert _blocked(outcome) == FRICTION[
            EngineRoundFailure.ENGINE_UNRESOLVABLE
        ].format(skill=_SKILL, cause="materialize_failed")
        assert runner.calls == []

    def test_a_materialize_error_carries_its_cause(
        self, repo: EngineRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_args: object, **_kwargs: object) -> None:
            cause = "skill_unreadable"
            raise MaterializeError(cause, "x")

        monkeypatch.setattr("cw.codex_review._engine_round.materialize_skill", _fail)

        outcome = _pass(repo, _engine_runner())

        assert _blocked(outcome) == FRICTION[
            EngineRoundFailure.ENGINE_UNRESOLVABLE
        ].format(skill=_SKILL, cause="skill_unreadable")


class TestManifestAtRoundTime:
    def _repo_with_manifest(
        self, make_git_repo: Callable[..., Path], tmp_path: Path, manifest: str
    ) -> EngineRepo:
        engine = tmp_path / "engine-files"
        for source in _STUB.rglob("*"):
            if source.is_file():
                target = engine / source.relative_to(_STUB)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        (engine / "cw-engine.json").write_text(manifest, encoding="utf-8")
        return make_engine_repo(
            make_git_repo, engine_files=engine, name="manifest-repo"
        )

    @pytest.mark.parametrize(
        ("manifest", "cause"),
        [
            ("{}", "manifest_invalid"),
            (
                json.dumps(
                    {
                        "manifest_version": 1,
                        "backends": ["claude-native"],
                        "roles": [{"id": "a", "agent_spec": "agents/security.md"}],
                        "aggregation_prompt": "prompts/aggregate.md",
                    }
                ),
                "codex_not_declared",
            ),
        ],
        ids=["invalid", "codex-not-declared"],
    )
    def test_a_manifest_that_fails_to_load_parks_unsupported_backend(
        self,
        make_git_repo: Callable[..., Path],
        tmp_path: Path,
        manifest: str,
        cause: str,
    ) -> None:
        repo = self._repo_with_manifest(make_git_repo, tmp_path, manifest)
        runner = _engine_runner()

        outcome = _pass(repo, runner)

        details = _blocked(
            outcome,
            reason=REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON,
            retry=False,
        )
        assert f"cause={cause}" in details
        assert "review engine refused on codex lane" in details
        assert runner.calls == []
        assert outcome.prepared is None

    def test_a_manifest_directory_is_invalid_not_absent(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        engine = tmp_path / "engine-files"
        for source in _STUB.rglob("*"):
            if source.is_file() and source.name != "cw-engine.json":
                target = engine / source.relative_to(_STUB)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        marker = engine / "cw-engine.json" / "marker"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("not a manifest", encoding="utf-8")
        repo = make_engine_repo(
            make_git_repo, engine_files=engine, name="manifest-directory-repo"
        )

        outcome = _pass(repo, _engine_runner())

        details = _blocked(
            outcome,
            reason=REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON,
            retry=False,
        )
        assert "cause=manifest_invalid" in details


class TestFullDiffReview:
    def test_a_re_review_reviews_the_whole_diff_not_the_delta(
        self, repo: EngineRepo
    ) -> None:
        runner = _engine_runner()
        first = _pass(repo, runner, cycle=0, round_id="round-0")
        assert first.prepared is not None
        commit_tracked_file(repo.worktree, "src/b.py", "y = 2\n")

        second = _pass(repo, runner, cycle=1, round_id="round-1")

        prepared = second.prepared
        assert prepared is not None
        assert prepared.delta_diff is None
        assert prepared.delta_changed_files is None
        assert set(prepared.changed_files) == {"src/a.py", "src/b.py"}
        for role_stdin in _role_stdins(runner)[2:]:
            assert "src/a.py" in role_stdin
            assert "src/b.py" in role_stdin
            assert "x = 1" in role_stdin
            assert "y = 2" in role_stdin


class TestNeverRejected:
    def test_operator_settlements_leave_an_engine_finding_standing(
        self,
        repo: EngineRepo,
        monkeypatch: pytest.MonkeyPatch,
        record_event_spy: MagicMock,
    ) -> None:
        """R20: neither the disposition ledger nor a void reaches the verdict."""
        key = _disposition_key("src/a.py", "Unsafe command construction.")
        assert key is not None
        ledger = {
            key: FindingDisposition.model_validate(
                {
                    "outcome": "REJECTED",
                    "rationale": "settled by the operator",
                    "recorded_at": "2026-08-16T00:00:00Z",
                    "actor": "an-operator",
                    "reviewed_sha": repo.head[:7],
                    "summary": "Unsafe command construction.",
                }
            )
        }
        void = VoidedFinding(
            severity="MUST_FIX",
            file="src/a.py",
            summary="Unsafe command construction.",
            evidence="x = 1",
            operator_comment_id="c-1",
        )
        monkeypatch.setattr(
            "cw.codex_review._context.core._load_voided_findings",
            lambda *_args, **_kwargs: [void],
        )
        captured: dict[str, Any] = {}

        def _spy(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return synthesize_codex_review_result(**kwargs)

        monkeypatch.setattr(
            "cw.codex_review._engine_round.synthesize_codex_review_result", _spy
        )
        runner = _engine_runner(aggregation=_aggregation([_MUST_FIX]))

        outcome = _pass(
            repo,
            runner,
            inputs=_inputs(repo, runner, task=_task(repo, finding_dispositions=ledger)),
        )

        assert outcome.prepared is not None
        assert outcome.prepared.voided_findings == [void]
        assert key in outcome.prepared.finding_dispositions
        assert captured["voided_findings"] == []
        assert captured["finding_dispositions"] == {}
        assert captured["refused_dispositions"] == []
        assert outcome.verdict is not None
        assert outcome.verdict.blocking is True
        assert [f.rule_id for f in outcome.verdict.must_fix] == ["security.injection"]
        assert outcome.verdict.rejected == []
        assert outcome.verdict.unmatched_voided_count == 0
        record_event_spy.assert_not_called()
        assert read_events() == []


class TestPromptTooLarge:
    def test_the_pass_lets_the_planner_error_propagate_before_any_role_runs(
        self, repo: EngineRepo
    ) -> None:
        runner = _engine_runner()

        with pytest.raises(ReviewInputTooLargeError) as raised:
            _pass(repo, runner, inputs=_inputs(repo, runner, max_prompt_chars=10))

        assert raised.value.max_chars == 10
        assert _stdins(runner) == []
