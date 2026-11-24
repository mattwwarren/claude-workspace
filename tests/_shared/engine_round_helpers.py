"""Shared helpers for the codex engine round's completion check (#3885).

Concern: driving ``run_engine_pass`` with a manifest that declares
``completion_check``, and the evidence/tree probes the completion tests need.
Fixtures here are plugin-registered (``tests/conftest.py``). The repo fixture
is ``engine_repo``, not ``repo``, so it cannot shadow a module's own ``repo``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from cw.codex_fix_loop import run_review_with_fix_loop
from cw.codex_review import _engine_round
from cw.codex_review._engine_round import (
    EnginePassInputs,
    EnginePassOutcome,
    EngineRoundSpec,
    run_engine_pass,
)
from cw.models import Stage
from cw.review_engine.constants import ENGINE_AGGREGATOR_PREAMBLE
from cw.review_engine.context import EngineContext
from cw.review_engine.materialize import engine_paths
from tests._codex_review_helpers import _ok_result
from tests._review_engine_helpers import (
    EngineRepo,
    copy_engine_fixture,
    make_aggregation_output,
    make_engine_repo,
)
from tests._shared.session_seeding import _make_ticket_task
from tests._tool_fakes import CodexReply, ScriptedCodexRunner, stdin_contains

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_runner import CodexRunResult
    from cw.models import TicketTask
    from cw.review_findings import ReviewVerdict

ENGINE_ROUND_STUB = Path(__file__).parent.parent / "fixtures" / "review_engine_stub"
ENGINE_ROUND_CHECKER = (
    Path(__file__).parent.parent / "fixtures" / "review_engine_checker"
)
ROUND_ID = "round-0"
SKILL = "repo-review"
SECURITY_MARK = "# Security role"
QUALITY_MARK = "# Quality role"
_LOGGER = "cw.codex_review._engine_round"


def _aggregation(findings: list[dict[str, object]] | None = None) -> CodexRunResult:
    from cw.codex_runner import CodexRunResult

    content = json.dumps(make_aggregation_output(findings))
    return CodexRunResult(0, "", "", output_file_content=content)


def task_for(repo: EngineRepo, **overrides: object) -> TicketTask:
    return _make_ticket_task(
        ticket_id=repo.ticket,
        client=repo.client,
        stage=Stage.REVIEW,
        scope_hint="small",
        **overrides,
    )


def spec_for(repo: EngineRepo, *, skill: str = SKILL) -> EngineRoundSpec:
    return EngineRoundSpec(
        context=EngineContext(
            skill=skill,
            guarded_paths=(),
            ticket_id=repo.ticket,
            client=repo.client,
            default_branch="main",
        )
    )


def inputs_for(repo: EngineRepo, runner: ScriptedCodexRunner) -> EnginePassInputs:
    return EnginePassInputs(
        runner=runner,
        task=task_for(repo),
        worktree=repo.worktree,
        base_branch="main",
        config_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="s-engine",
        fix_loop_enabled=True,
        prior_open_findings=None,
        claim_tier_enabled=False,
        disposition_drift_check_enabled=True,
        settled_suppression=None,
        max_prompt_chars=None,
    )


def engine_runner(
    *,
    security: CodexReply | None = None,
    quality: CodexReply | None = None,
) -> ScriptedCodexRunner:
    """Role and aggregator rules keyed on the spec text and the preamble."""
    return (
        ScriptedCodexRunner()
        .add(stdin_contains(ENGINE_AGGREGATOR_PREAMBLE), _aggregation())
        .add(stdin_contains(SECURITY_MARK), security or _ok_result("security"))
        .add(stdin_contains(QUALITY_MARK), quality or _ok_result("quality"))
    )


def run_pass(
    repo: EngineRepo,
    runner: ScriptedCodexRunner,
    *,
    spec: EngineRoundSpec | None = None,
) -> EnginePassOutcome:
    return run_engine_pass(
        spec or spec_for(repo),
        inputs_for(repo, runner),
        cycle=0,
        round_id=ROUND_ID,
    )


def run_fix_loop(
    repo: EngineRepo, runner: ScriptedCodexRunner
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Cycle 0 of the real codex fix-loop driver over an engine round.

    A verdict-less blocked outcome returns here before any ledger round is
    recorded, which is the state the attestation tests need to inspect.
    """
    return run_review_with_fix_loop(
        runner=runner,
        task=task_for(repo),
        worktree=repo.worktree,
        default_branch="main",
        base_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="s-engine",
        fix_loop_enabled=True,
        engine=spec_for(repo),
    )


def aggregator_stdins(runner: ScriptedCodexRunner) -> list[str]:
    stdins = [str(call["stdin"]) for call in runner.review_calls]
    return [s for s in stdins if s.startswith(ENGINE_AGGREGATOR_PREAMBLE)]


def blocked_details(outcome: EnginePassOutcome, *, retry: bool) -> str:
    """Assert a verdict-less ``agent_block`` with *retry*; return its details."""
    assert outcome.result.status == "blocked"
    blocker = outcome.result.blocker
    assert blocker is not None
    assert blocker.reason == "agent_block"
    assert blocker.retry_eligible is retry
    assert outcome.verdict is None
    return blocker.details


def assert_blocked_before_aggregation(
    runner: ScriptedCodexRunner, repo: EngineRepo, record_event_spy: MagicMock
) -> None:
    """A blocked round ran the role turns, then stopped: no aggregator turn, no
    ``summary.json`` in the round dir, and no event recorded."""
    role_stdins = [str(call["stdin"]) for call in runner.review_calls]
    assert any(SECURITY_MARK in s for s in role_stdins), "security role never ran"
    assert any(QUALITY_MARK in s for s in role_stdins), "quality role never ran"
    assert aggregator_stdins(runner) == []
    assert not engine_paths(repo.worktree, ROUND_ID).summary_path.exists()
    record_event_spy.assert_not_called()


def run_blocked(repo: EngineRepo, record_event_spy: MagicMock, *, retry: bool) -> str:
    """Run an engine pass that must block; return the blocker details.

    Asserts the verdict-less ``agent_block`` (``blocked_details``) and that the
    round stopped before aggregation and recorded nothing.
    """
    runner = engine_runner()
    outcome = run_pass(repo, runner)
    details = blocked_details(outcome, retry=retry)
    assert_blocked_before_aggregation(runner, repo, record_event_spy)
    return details


def make_checker_repo(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    script_body: str | None = None,
    *,
    mode: int = 0o755,
    timeout_seconds: int | None = None,
) -> EngineRepo:
    """A repo whose default branch carries the checker fixture, optionally with
    ``bin/check-completion`` overwritten by *script_body* at *mode* and the
    manifest's ``completion_check.timeout_seconds`` set to *timeout_seconds*."""
    engine = copy_engine_fixture(tmp_path, ENGINE_ROUND_CHECKER)
    if timeout_seconds is not None:
        manifest = engine / "cw-engine.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["completion_check"]["timeout_seconds"] = timeout_seconds
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    if script_body is not None:
        checker = engine / "bin" / "check-completion"
        checker.write_text(script_body, encoding="utf-8")
        checker.chmod(mode)
    return make_engine_repo(
        make_git_repo, engine_files=engine, name=f"checker-{tmp_path.name}"
    )


def round_dir(repo: EngineRepo) -> Path:
    return engine_paths(repo.worktree, ROUND_ID).round_dir


def observed_exists(repo: EngineRepo) -> bool:
    return (round_dir(repo) / "completion" / "observed.json").exists()


def read_observed(repo: EngineRepo) -> dict[str, object]:
    path = round_dir(repo) / "completion" / "observed.json"
    assert path.exists(), "checker never ran (no observed.json)"
    loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def read_result(repo: EngineRepo) -> dict[str, object]:
    """The retained ``completion/result.json`` the checker wrote for the round."""
    path = round_dir(repo) / "completion" / "result.json"
    assert path.exists(), "checker wrote no result.json"
    loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def tamper_roles(
    monkeypatch: pytest.MonkeyPatch,
    worktree: Path,
    action: Callable[[Path], None],
) -> None:
    """Run *action* on *worktree* right after the real role turns finish."""
    real = _engine_round.run_prepared_roles

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        action(worktree)
        return result

    monkeypatch.setattr(_engine_round, "run_prepared_roles", wrapper)


def assert_group_dead(pid_file: Path, *, grace_seconds: float = 5.0) -> None:
    """The pid recorded in *pid_file* is no longer alive (its group was killed)."""
    import time

    pid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    msg = f"process {pid} still alive after {grace_seconds}s"
    raise AssertionError(msg)


@pytest.fixture
def engine_repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    """The stub engine (no completion check) on the default branch."""
    return make_engine_repo(make_git_repo, engine_files=ENGINE_ROUND_STUB)


@pytest.fixture
def record_event_spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace every ``record_event`` binding in an imported ``cw`` module."""
    spy = MagicMock()
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] == "cw" and hasattr(module, "record_event"):
            monkeypatch.setattr(module, "record_event", spy)
    return spy


@pytest.fixture
def checker_tmpdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, list[str]]:
    """Redirect ``tempfile`` into a worker dir and record each checker scratch dir.

    ``tempfile.gettempdir()`` caches in ``tempfile.tempdir``, so the redirect is
    set directly (an env var set later would have no effect).
    """
    worker = tmp_path / "worker-tmp"
    worker.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(worker))
    recorded: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def spy(*args: Any, **kwargs: Any) -> str:
        path = real_mkdtemp(*args, **kwargs)
        if Path(path).name.startswith("cw-engine-check-"):
            recorded.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    return worker, recorded
