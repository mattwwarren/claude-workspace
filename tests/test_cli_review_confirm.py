"""Tests for ``cw review confirm``: the independent codex confirmation round (#2787).

The command wraps ``cw.codex_review.run_review`` for the converge-to-clean
review mode and speaks a small exit-code contract the prose obeys (R19, R26,
R29): 0 is a verdict (never a clean signal by itself), 3 means the backend is
down and nothing else does, 4 means the round failed on a healthy backend,
2 is Click's usage error, and 1 is a ``CwError`` or an uncaught exception.

The codex reviewer is a system we do not own, so every runner result is built
from the existing success builders (``_ok_result``) or the captured
``capacity_turn_failed`` codex stream, never a hand-authored codex payload.
``codex`` itself is never spawned: the autouse ``_hide_optional_binaries``
fixture hides it, ``@needs_codex`` makes it appear present for a deterministic
``/usr/bin/codex``, and a ``_SequencedRunner`` is injected in place of
``RealCodexRunner``.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw import claude_review, codex_review
from cw.claude_review import ClaudeReviewRound, ClaudeRunResult, FakeClaudeRunner
from cw.cli import main
from cw.codex_review import (
    CODEX_BUDGET_EXHAUSTED,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_REVIEW_WORKTREE_MUTATED,
    CODEX_REVIEWER_FAILURE_DISCARDED_FINDINGS,
    _prepare_review_pass,
    make_codex_blocked,
)
from cw.codex_runner import CodexRunResult, FakeCodexRunner
from cw.dev_queue import add_ticket, load_dev_queue
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import QueueItemStatus, Stage
from cw.models.orchestrator_config.stage import StageExecutorConfig
from cw.review_findings import ReviewVerdict
from tests._claude_review_helpers import (
    error_envelope,
    review_envelope,
    truncated_envelope,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._codex_review_helpers import (
    _doc_json,
    _finding_payload,
    _ok_result,
    _SequencedRunner,
)
from tests._review_convergence_helpers import (
    codex_audit_fixture,
    ledger_with_rounds,
    structurally_invalid_doc_json,
    warm_codex_capability,
    write_ledger,
)
from tests.conftest import _make_ticket_task, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from click.testing import Result

    from cw.auto_dev_result import AutoDevResult
    from cw.models import TicketTask

_CLIENT = "acme"
_TICKET = "T-2787"
_SESSION = "sess-abc"
_CAPACITY_FIXTURE = "capacity_turn_failed.jsonl"
_UNAVAILABLE = 3
_ROUND_FAILED = 4
_STOP = "stop here"

#: Makes ``codex`` look present on PATH (a deterministic ``/usr/bin/codex``).
needs_codex = pytest.mark.binary_on_path("codex")


def _capacity() -> CodexRunResult:
    """A non-zero backend exit: the captured codex stream ending in ``turn.failed``."""
    return CodexRunResult(
        returncode=1, stdout=codex_audit_fixture(_CAPACITY_FIXTURE), stderr=""
    )


def _unparseable() -> CodexRunResult:
    """A clean exit whose output file is not a document."""
    return dataclasses.replace(_ok_result(), output_file_content="not json")


def _discarding(severity: str) -> CodexRunResult:
    """A clean exit whose document cannot be built while claiming one finding."""
    return CodexRunResult(
        returncode=0,
        stdout="",
        stderr="",
        output_file_content=structurally_invalid_doc_json(severity),
    )


def _must_fix_result() -> CodexRunResult:
    """A document carrying a MUST_FIX anchored in the harness diff (``feature.py``)."""
    return _ok_result(
        findings=[
            _finding_payload(
                severity="MUST_FIX",
                file="feature.py",
                line_start=1,
                line_end=1,
                evidence="x = 1",
            )
        ]
    )


@dataclass
class Harness:
    """A pushed feature worktree, its dev-queue row and the injected runner seam."""

    repo: Path
    task: TicketTask
    roles: list[str]
    cli: CliRunner
    monkeypatch: pytest.MonkeyPatch

    def results(
        self, *first: CodexRunResult, rest: CodexRunResult
    ) -> list[CodexRunResult]:
        """One result per role: *first* in role order, then *rest* for the others."""
        return [*first, *([rest] * (len(self.roles) - len(first)))]

    def all_roles(self, result: CodexRunResult) -> list[CodexRunResult]:
        return [result] * len(self.roles)

    def args(self, *extra: str) -> list[str]:
        return [
            "review",
            "confirm",
            "--ticket-id",
            _TICKET,
            "-c",
            _CLIENT,
            "--worktree",
            str(self.repo),
            *extra,
        ]

    def invoke(
        self, results: Sequence[CodexRunResult], *extra: str
    ) -> tuple[Result, _SequencedRunner]:
        """Run ``confirm`` against a ``_SequencedRunner`` queued with *results*."""
        runner = _SequencedRunner(list(results))
        self.monkeypatch.setattr("cw.codex_runner.RealCodexRunner", lambda: runner)
        return self.cli.invoke(main, self.args(*extra)), runner

    def spy_run_review(
        self, *, after: Callable[[Path], None] | None = None
    ) -> list[dict[str, object]]:
        """Wrap the real ``run_review``, recording each call's keyword arguments."""
        real = codex_review.run_review
        calls: list[dict[str, object]] = []

        def _spy(**kwargs: object) -> object:
            calls.append(dict(kwargs))
            outcome = real(**kwargs)
            if after is not None:
                after(self.repo)
            return outcome

        self.monkeypatch.setattr("cw.codex_review.run_review", _spy)
        return calls

    def head(self) -> str:
        return git_in(self.repo, "rev-parse", "HEAD")

    def invoke_claude(
        self, results: Sequence[ClaudeRunResult], *extra: str
    ) -> tuple[Result, FakeClaudeRunner]:
        """Run ``confirm --backend claude`` against a ``FakeClaudeRunner`` (#2791).

        ``claude`` is not in the optional-binary denylist, so the binary check
        is the ``_which_claude`` seam, patched to a deterministic path.
        """
        runner = FakeClaudeRunner(results=list(results))
        self.monkeypatch.setattr("cw.claude_review.RealClaudeRunner", lambda: runner)
        self.monkeypatch.setattr(
            "cw.claude_review._which_claude", lambda: "/usr/bin/claude"
        )
        return self.cli.invoke(main, self.args("--backend", "claude", *extra)), runner

    def spy_claude_review(
        self, *, after: Callable[[Path], None] | None = None
    ) -> list[dict[str, object]]:
        """Wrap the real ``run_claude_review``, recording each call's kwargs."""
        real = claude_review.run_claude_review
        calls: list[dict[str, object]] = []

        def _spy(**kwargs: object) -> object:
            calls.append(dict(kwargs))
            outcome = real(**kwargs)
            if after is not None:
                after(self.repo)
            return outcome

        self.monkeypatch.setattr("cw.claude_review.run_claude_review", _spy)
        return calls


@pytest.fixture
def harness(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> Harness:
    repo, _origin = make_git_repo_with_origin("wt-confirm")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    write_clients_yaml(ClientSpec(_CLIENT, workspace_path=workspace))
    task = _make_ticket_task(
        ticket_id=_TICKET,
        client=_CLIENT,
        stage=Stage.REVIEW,
        status=QueueItemStatus.RUNNING,
        session_id=_SESSION,
        worktree_path=repo,
        scope_hint="small",
    )
    add_ticket(task)
    warm_codex_capability(repo, task)
    roles = _prepare_review_pass(
        task, repo, "main", runner=FakeCodexRunner(), session_id="confirm-roles"
    ).roles
    return Harness(
        repo=repo,
        task=task,
        roles=list(roles),
        cli=CliRunner(),
        monkeypatch=monkeypatch,
    )


def _payload(result: Result) -> dict[str, object]:
    payload: dict[str, object] = json.loads(result.stdout)
    return payload


def _detail(result: Result) -> str:
    detail = _payload(result)["detail"]
    assert isinstance(detail, str)
    return detail


def _assert_unavailable(
    result: Result, cause: str, backend: str = "codex"
) -> dict[str, object]:
    assert result.exit_code == _UNAVAILABLE, result.output
    payload = _payload(result)
    assert payload["available"] is False
    assert payload["backend"] == backend
    assert payload["cause"] == cause
    assert "detail" in payload
    return payload


def _assert_round_failed(
    result: Result, cause: str, backend: str = "codex"
) -> dict[str, object]:
    assert result.exit_code == _ROUND_FAILED, result.output
    payload = _payload(result)
    assert payload["available"] is True
    assert payload["backend"] == backend
    assert payload["round_failed"] is True
    assert payload["cause"] == cause
    assert "verdict" not in payload
    return payload


def _assert_not_outage(result: Result) -> None:
    """Exit is not 3 and stdout never claims ``available: false``."""
    assert result.exit_code != _UNAVAILABLE
    if result.stdout.strip():
        assert _payload(result).get("available") is not False


# ---------------------------------------------------------------------------
# Exit-code contract (R19, R26, R29), asserted as a table
# ---------------------------------------------------------------------------


def _raise(exc: BaseException) -> Callable[..., object]:
    def _boom(**_kwargs: object) -> object:
        raise exc

    return _boom


def _scn_ok(h: Harness) -> Result:
    return h.invoke(h.all_roles(_ok_result()))[0]


def _scn_binary_missing(h: Harness) -> Result:
    h.monkeypatch.setattr("shutil.which", lambda *_a, **_k: None)
    return h.invoke(h.all_roles(_ok_result()))[0]


def _scn_crash(h: Harness) -> Result:
    h.monkeypatch.setattr("cw.codex_review.run_review", _raise(OSError("boom")))
    return h.invoke(h.all_roles(_ok_result()))[0]


def _scn_backend_error(h: Harness) -> Result:
    return h.invoke(h.all_roles(_capacity()))[0]


def _scn_partial(h: Harness) -> Result:
    return h.invoke(h.results(_ok_result(), rest=_capacity()))[0]


def _scn_reviewers_failed(h: Harness) -> Result:
    return h.invoke(h.all_roles(_unparseable()))[0]


def _scn_discarded(h: Harness) -> Result:
    return h.invoke(h.results(_discarding("SHOULD_FIX"), rest=_ok_result()))[0]


def _scn_usage_error(h: Harness) -> Result:
    return h.cli.invoke(main, ["review", "confirm", "--ticket-id", _TICKET])


def _scn_unknown_ticket(h: Harness) -> Result:
    args = h.args()
    args[args.index(_TICKET)] = "T-9999"
    return h.cli.invoke(main, args)


_EXIT_TABLE: dict[str, tuple[Callable[[Harness], Result], int]] = {
    "verdict": (_scn_ok, 0),
    "binary_missing": (_scn_binary_missing, 3),
    "crash": (_scn_crash, 3),
    "backend_error": (_scn_backend_error, 3),
    "partial": (_scn_partial, 4),
    "reviewers_failed": (_scn_reviewers_failed, 4),
    "discarded_findings": (_scn_discarded, 4),
    "usage_error": (_scn_usage_error, 2),
    "cw_error": (_scn_unknown_ticket, 1),
}


@needs_codex
@pytest.mark.parametrize("scenario", list(_EXIT_TABLE))
def test_exit_code_table(harness: Harness, scenario: str) -> None:
    scenario_fn, expected = _EXIT_TABLE[scenario]

    result = scenario_fn(harness)

    assert result.exit_code == expected, result.output
    # Only exit 3 prints available:false; exit 0 and exit 4 print available:true.
    availability = {3: False, 0: True, 4: True}
    if expected in availability:
        assert _payload(result)["available"] is availability[expected]
    else:
        _assert_not_outage(result)


# ---------------------------------------------------------------------------
# Backend down: exit 3, the only uncharged class
# ---------------------------------------------------------------------------


def test_codex_binary_absent_is_binary_missing(harness: Harness) -> None:
    # No @needs_codex: the autouse fixture hides codex, exactly as on a CI host.
    runner = _SequencedRunner([])
    harness.monkeypatch.setattr("cw.codex_runner.RealCodexRunner", lambda: runner)

    result = harness.cli.invoke(main, harness.args())

    payload = _assert_unavailable(result, "binary_missing")
    assert "codex" in str(payload["detail"])
    assert runner.calls == []


@needs_codex
@pytest.mark.parametrize("exc", [OSError("no such device"), CwError("cw broke")])
def test_run_review_oserror_or_cwerror_is_crash(
    harness: Harness, exc: Exception
) -> None:
    harness.monkeypatch.setattr("cw.codex_review.run_review", _raise(exc))

    result, _runner = harness.invoke([])

    payload = _assert_unavailable(result, "crash")
    assert str(exc) in str(payload["detail"])
    assert type(exc).__name__ in str(payload["detail"])


@needs_codex
def test_nonzero_backend_exit_for_every_role_is_backend_error(
    harness: Harness,
) -> None:
    result, runner = harness.invoke(harness.all_roles(_capacity()))

    payload = _assert_unavailable(result, "backend_error")
    assert len(runner.calls) == len(harness.roles)
    for role in harness.roles:
        assert f"{role} (codex_model_capacity)" in str(payload["detail"])


# ---------------------------------------------------------------------------
# Round failed on a healthy backend: exit 4, never exit 3 (R26)
# ---------------------------------------------------------------------------


@needs_codex
def test_partial_round_is_exit_four_with_the_per_role_detail(harness: Harness) -> None:
    result, _runner = harness.invoke(harness.results(_ok_result(), rest=_capacity()))

    _assert_round_failed(result, "partial")
    for role in harness.roles[1:]:
        assert f"{role} (codex_model_capacity)" in _detail(result)


@needs_codex
def test_all_unparseable_is_reviewers_failed(harness: Harness) -> None:
    result, _runner = harness.invoke(harness.all_roles(_unparseable()))

    _assert_round_failed(result, "reviewers_failed")
    for role in harness.roles:
        assert f"{role} ({CODEX_REVIEW_UNPARSEABLE})" in _detail(result)


@needs_codex
def test_mixed_nonzero_and_unparseable_is_reviewers_failed(harness: Harness) -> None:
    result, _runner = harness.invoke(harness.results(_capacity(), rest=_unparseable()))

    _assert_round_failed(result, "reviewers_failed")
    assert f"{harness.roles[0]} (codex_model_capacity)" in _detail(result)


@needs_codex
def test_failures_matching_neither_class_are_reviewers_failed(
    harness: Harness,
) -> None:
    """A zero-document run whose reasons are not backend-side is not an outage."""
    result, _runner = harness.invoke(
        harness.all_roles(
            CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        )
    )

    _assert_round_failed(result, "reviewers_failed")
    assert "codex_timeout" in _detail(result)


def _dirty_untracked(repo: Path) -> None:
    (repo / "stray.txt").write_text("a reviewer wrote this\n", encoding="utf-8")


def _move_head(repo: Path) -> None:
    (repo / "tracked.py").write_text("y = 2\n", encoding="utf-8")
    git_in(repo, "add", "tracked.py")
    git_in(repo, "commit", "-m", "reviewer commit")


@needs_codex
@pytest.mark.parametrize("mutate", [_dirty_untracked, _move_head])
def test_worktree_changed_across_the_call_is_worktree_mutated(
    harness: Harness, mutate: Callable[[Path], None]
) -> None:
    harness.spy_run_review(after=mutate)

    result, _runner = harness.invoke(harness.all_roles(_ok_result()))

    payload = _assert_round_failed(result, "worktree_mutated")
    assert "worktree" in str(payload["detail"])


@needs_codex
@pytest.mark.parametrize("severity", ["SHOULD_FIX", "MUST_FIX"])
def test_discarded_findings_round_is_exit_four_never_zero_or_three(
    harness: Harness, severity: str
) -> None:
    queued = harness.results(_discarding(severity), rest=_ok_result())
    # Precondition on the REAL classification: the structurally invalid document
    # is a ReviewerRunFailure carrying the tally, and run_review blocks on it
    # ahead of the partial branch.
    outcome, verdict = codex_review.run_review(
        runner=_SequencedRunner(list(queued)),
        task=harness.task,
        worktree=harness.repo,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="confirm-precondition",
        fix_loop_enabled=False,
    )
    assert outcome.blocker is not None
    assert outcome.blocker.reason == CODEX_REVIEWER_FAILURE_DISCARDED_FINDINGS
    assert verdict is not None
    [failure] = verdict.run_failures_with_should_fix_discards
    assert failure.role == harness.roles[0]
    assert failure.reason == CODEX_REVIEW_UNPARSEABLE
    assert failure.discarded_finding_severities == {severity: 1}

    result, _runner = harness.invoke(queued)

    _assert_round_failed(result, "discarded_findings")
    detail = _detail(result)
    assert harness.roles[0] in detail
    assert CODEX_REVIEW_UNPARSEABLE in detail
    assert severity in detail


@needs_codex
def test_nit_only_discard_is_partial_not_discarded_findings(harness: Harness) -> None:
    """The discard gate is SHOULD_FIX and above: a NIT tally is an ordinary partial."""
    result, _runner = harness.invoke(
        harness.results(_discarding("NIT"), rest=_ok_result())
    )

    _assert_round_failed(result, "partial")


@needs_codex
def test_discarded_findings_round_is_not_recorded_clean_and_never_converges(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = harness.head()
    ledger = ledger_with_rounds(
        (), shas=[head], confirm="codex", ticket_id=_TICKET, fork_point="main"
    )
    assert ledger.status == "awaiting_confirm"
    ledger_path = write_ledger(harness.repo, ledger)
    before = ledger_path.read_bytes()

    result, _runner = harness.invoke(
        harness.results(_discarding("SHOULD_FIX"), rest=_ok_result())
    )

    _assert_round_failed(result, "discarded_findings")
    assert ledger_path.read_bytes() == before
    monkeypatch.chdir(harness.repo)
    status = harness.cli.invoke(
        main, ["review", "converge", "status", "--ledger", str(ledger_path)]
    )
    assert status.exit_code == 0, status.output
    summary = json.loads(status.stdout)
    assert summary["status"] == "awaiting_confirm"
    assert summary["next_action"] == "confirm"
    assert summary["awaiting_confirm_sha"] == head
    assert not summary.get("last_clean_sha")
    finalize = harness.cli.invoke(
        main, ["review", "converge", "finalize", "--ledger", str(ledger_path)]
    )
    assert finalize.exit_code == 1, finalize.output
    assert json.loads(finalize.stdout)["decision"] == "confirm_required"
    assert "sentinel" not in finalize.stdout.lower()
    assert list(harness.repo.rglob("round-*-verdict.json")) == []
    assert ledger_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Not an outage (each exits with a code other than 3)
# ---------------------------------------------------------------------------


@needs_codex
@pytest.mark.parametrize(
    "args",
    [
        ["--client", _CLIENT],
        ["--ticket-id", _TICKET],
        ["--ticket-id", _TICKET, "-c", _CLIENT, "--no-such-option"],
    ],
    ids=["missing-ticket-id", "missing-client", "unknown-option"],
)
def test_usage_errors_exit_two_with_nothing_on_stdout(
    harness: Harness, args: list[str]
) -> None:
    result = harness.cli.invoke(main, ["review", "confirm", *args])

    assert result.exit_code == 2
    assert result.stdout == ""
    _assert_not_outage(result)


@needs_codex
def test_unknown_ticket_is_a_cwerror_before_run_review(harness: Harness) -> None:
    calls = harness.spy_run_review()
    args = harness.args()
    args[args.index(_TICKET)] = "T-9999"

    result = harness.cli.invoke(main, args)

    assert result.exit_code == 1
    assert calls == []
    _assert_not_outage(result)


@needs_codex
def test_unresolvable_client_is_a_cwerror_before_run_review(harness: Harness) -> None:
    calls = harness.spy_run_review()
    args = harness.args()
    args[args.index(_CLIENT)] = "no-such-client"

    result = harness.cli.invoke(main, args)

    assert result.exit_code == 1
    assert calls == []
    _assert_not_outage(result)


@needs_codex
def test_unreadable_worktree_is_a_cwerror_not_an_outage(
    harness: Harness, tmp_path: Path
) -> None:
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    calls = harness.spy_run_review()

    result = harness.cli.invoke(main, [*harness.args(), "--worktree", str(not_a_repo)])

    assert result.exit_code == 1
    assert calls == []
    _assert_not_outage(result)


@needs_codex
def test_worktree_of_another_ticket_is_refused_before_run_review(
    harness: Harness,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
) -> None:
    other_repo, _origin = make_git_repo_with_origin("wt-other-ticket")
    calls = harness.spy_run_review()

    result = harness.cli.invoke(main, [*harness.args(), "--worktree", str(other_repo)])

    assert result.exit_code == 1
    assert "does not belong to ticket" in result.output
    assert calls == []
    _assert_not_outage(result)


@needs_codex
def test_unresolvable_ticket_worktree_is_refused_before_run_review(
    harness: Harness,
) -> None:
    calls = harness.spy_run_review()
    harness.monkeypatch.setattr(
        "cw.cli.review.confirm.resolve_task_worktree", lambda *_a, **_k: None
    )

    result = harness.cli.invoke(main, harness.args())

    assert result.exit_code == 1
    assert "Cannot resolve the worktree" in result.output
    assert calls == []
    _assert_not_outage(result)


@needs_codex
def test_unexpected_exception_outside_the_try_exits_one(harness: Harness) -> None:
    def _broken_runner() -> object:
        msg = "runner constructor exploded"
        raise RuntimeError(msg)

    harness.monkeypatch.setattr("cw.codex_runner.RealCodexRunner", _broken_runner)

    result = harness.cli.invoke(main, harness.args())

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    _assert_not_outage(result)


@needs_codex
def test_unexpected_exception_inside_run_review_is_not_swallowed(
    harness: Harness,
) -> None:
    """Only OSError and CwError are crash; anything else fails closed as exit 1."""
    harness.monkeypatch.setattr(
        "cw.codex_review.run_review", _raise(RuntimeError("logic bug"))
    )

    result, _runner = harness.invoke([])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    _assert_not_outage(result)


# ---------------------------------------------------------------------------
# Exit 0: a verdict, never a clean signal by itself
# ---------------------------------------------------------------------------


@needs_codex
def test_dirty_plus_partial_is_exit_zero_with_the_must_fix_verdict(
    harness: Harness,
) -> None:
    """The block_reason branch wins over the partial branch, so this is exit 0."""
    result, _runner = harness.invoke(
        harness.results(_must_fix_result(), rest=_capacity())
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["available"] is True
    assert payload["result_blocker_reason"] == "codex_must_fix_findings"
    verdict = ReviewVerdict.model_validate(payload["verdict"])
    assert verdict.blocking is True
    assert [af.finding.severity for af in verdict.accepted] == ["MUST_FIX"]
    assert "round_failed" not in payload
    assert "cause" not in payload


@needs_codex
def test_partial_without_the_finding_is_exit_four(harness: Harness) -> None:
    """The control: the same roster without the MUST_FIX is a failed round."""
    result, _runner = harness.invoke(harness.results(_ok_result(), rest=_capacity()))

    _assert_round_failed(result, "partial")


@needs_codex
def test_happy_path_prints_the_verdict_envelope(harness: Harness) -> None:
    result, runner = harness.invoke(harness.all_roles(_ok_result()))

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["available"] is True
    assert payload["backend"] == "codex"
    assert payload["reviewed_sha"] == harness.head()
    assert payload["result_blocker_reason"] is None
    verdict = ReviewVerdict.model_validate(payload["verdict"])
    assert verdict.reviewed_sha == harness.head()
    assert verdict.blocking is False
    assert len(runner.calls) == len(harness.roles)


@needs_codex
def test_the_exit_zero_verdict_is_accepted_by_converge_suppress(
    harness: Harness,
) -> None:
    result, _runner = harness.invoke(harness.all_roles(_ok_result()))
    assert result.exit_code == 0, result.output
    ledger_path = write_ledger(
        harness.repo, ledger_with_rounds((), shas=[harness.head()], ticket_id=_TICKET)
    )

    suppressed = harness.cli.invoke(
        main,
        ["review", "converge", "suppress", "-", "--ledger", str(ledger_path)],
        input=json.dumps({"verdict": _payload(result)["verdict"]}),
    )

    assert suppressed.exit_code == 0, suppressed.output
    output = json.loads(suppressed.stdout)
    assert output["suppressed"] == 0
    assert output["verdict"] == _payload(result)["verdict"]


# ---------------------------------------------------------------------------
# Arguments handed to run_review
# ---------------------------------------------------------------------------


@needs_codex
def test_arguments_to_run_review(harness: Harness) -> None:
    calls = harness.spy_run_review()

    result, runner = harness.invoke(harness.all_roles(_ok_result()))

    assert result.exit_code == 0, result.output
    [kwargs] = calls
    assert kwargs["runner"] is runner
    assert kwargs["task"] == harness.task
    assert kwargs["worktree"] == harness.repo
    assert kwargs["default_branch"] == "main"
    assert kwargs["model"] is None
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["session_id"] == f"confirm-{_SESSION}"
    assert kwargs["wall_clock_budget_seconds"] is None
    assert kwargs["fix_loop_enabled"] is False
    # Left at run_review's own defaults rather than passed.
    assert "claim_tier_enabled" not in kwargs
    assert "disposition_drift_check_enabled" not in kwargs
    # cw imposes no deadline on any role (R20).
    assert all(call["timeout"] is None for call in runner.calls)


@needs_codex
def test_ledger_fork_point_is_diff_base_not_scope_branch(
    harness: Harness,
) -> None:
    ledger_path = write_ledger(
        harness.repo,
        ledger_with_rounds(
            (),
            shas=[harness.head()],
            confirm="codex",
            ticket_id=_TICKET,
            fork_point="main",
        ),
    )
    assert ledger_path.exists()
    seen: list[dict[str, object]] = []

    def _stub(**kwargs: object) -> object:
        seen.append(dict(kwargs))
        raise OSError(_STOP)

    harness.monkeypatch.setattr("cw.codex_review.run_review", _stub)

    result, _runner = harness.invoke([], "--default-branch", "develop")

    assert result.exit_code == _UNAVAILABLE
    assert seen[0]["default_branch"] == "develop"


@needs_codex
def test_default_reasoning_effort_is_none_when_the_codex_default_is_unpinned(
    harness: Harness,
) -> None:
    class _Unpinned:
        reasoning_effort = None

    harness.monkeypatch.setattr("cw.cli.review.confirm.StageExecutorConfig", _Unpinned)
    calls = harness.spy_run_review()

    result, _runner = harness.invoke(harness.all_roles(_ok_result()))

    assert result.exit_code == 0, result.output
    assert calls[0]["reasoning_effort"] is None


def test_stage_executor_default_is_what_the_command_narrows() -> None:
    """Guards the premise: the default is the pinned codex default, high."""
    effort = StageExecutorConfig().reasoning_effort
    assert effort is not None
    assert effort.value == "high"


@needs_codex
def test_session_id_falls_back_to_the_ticket_id_without_a_session(
    harness: Harness,
) -> None:
    row = harness.task.model_copy(update={"session_id": None})
    harness.monkeypatch.setattr("cw.cli.review.confirm._find_ticket", lambda *_a: row)
    calls = harness.spy_run_review()

    result, _runner = harness.invoke(harness.all_roles(_ok_result()))

    assert result.exit_code == 0, result.output
    assert calls[0]["session_id"] == f"confirm-{_TICKET}"


@needs_codex
def test_explicit_options_are_passed_through(harness: Harness) -> None:
    seen: list[dict[str, object]] = []

    def _stub(**kwargs: object) -> object:
        seen.append(dict(kwargs))
        raise OSError(_STOP)

    harness.monkeypatch.setattr("cw.codex_review.run_review", _stub)

    result, _runner = harness.invoke(
        [],
        "--default-branch",
        "develop",
        "--model",
        "gpt-test",
        "--reasoning-effort",
        "low",
    )

    assert result.exit_code == _UNAVAILABLE
    [kwargs] = seen
    assert kwargs["default_branch"] == "develop"
    assert kwargs["model"] == "gpt-test"
    assert kwargs["reasoning_effort"] == "low"


@needs_codex
def test_default_branch_comes_from_the_client_config(
    harness: Harness, tmp_path: Path
) -> None:
    write_clients_yaml(
        ClientSpec(_CLIENT, workspace_path=tmp_path / "ws", default_branch="trunk")
    )
    seen: list[dict[str, object]] = []

    def _stub(**kwargs: object) -> object:
        seen.append(dict(kwargs))
        raise OSError(_STOP)

    harness.monkeypatch.setattr("cw.codex_review.run_review", _stub)

    harness.invoke([])

    assert seen[0]["default_branch"] == "trunk"


def test_help_lists_no_budget_or_timeout_option() -> None:
    result = CliRunner().invoke(main, ["review", "confirm", "--help"])

    assert result.exit_code == 0
    lowered = result.output.lower()
    for option in ("--ticket-id", "--client", "--worktree", "--default-branch"):
        assert option in lowered
    assert "budget" not in lowered
    assert "--timeout" not in lowered
    assert "exit" in lowered


# ---------------------------------------------------------------------------
# Side-effect set
# ---------------------------------------------------------------------------


def _row_snapshot() -> dict[str, object]:
    [row] = [
        t
        for t in load_dev_queue().tasks
        if t.ticket_id == _TICKET and t.client == _CLIENT
    ]
    return {
        "status": row.status,
        "stage": row.stage,
        "session_id": row.session_id,
        "pending_fix_dispatch": row.pending_fix_dispatch,
        "finding_dispositions": dict(row.finding_dispositions),
    }


@needs_codex
def test_confirm_leaves_the_row_alone_and_posts_nothing(harness: Harness) -> None:
    def _no_comment(*_args: object, **_kwargs: object) -> None:
        msg = "confirm must not post a tracker comment"
        raise AssertionError(msg)

    harness.monkeypatch.setattr("cw.gh.post_issue_comment", _no_comment)
    before = _row_snapshot()

    result, _runner = harness.invoke(harness.all_roles(_ok_result()))

    assert result.exit_code == 0, result.output
    assert _row_snapshot() == before
    assert read_events() == []


# ---------------------------------------------------------------------------
# --backend claude (#2791): the same contract on a claude -p reviewer
# ---------------------------------------------------------------------------


def _claude_ok() -> ClaudeRunResult:
    return ClaudeRunResult(returncode=0, stdout=review_envelope(_doc_json()), stderr="")


def _claude_error() -> ClaudeRunResult:
    return ClaudeRunResult(returncode=0, stdout=error_envelope(), stderr="")


def _claude_unparseable() -> ClaudeRunResult:
    return ClaudeRunResult(returncode=0, stdout=truncated_envelope(), stderr="")


def _claude_discarding() -> ClaudeRunResult:
    return ClaudeRunResult(
        returncode=0,
        stdout=review_envelope(structurally_invalid_doc_json("SHOULD_FIX")),
        stderr="",
    )


def _claude_results(
    h: Harness, *first: ClaudeRunResult, rest: ClaudeRunResult
) -> list[ClaudeRunResult]:
    return [*first, *([rest] * (len(h.roles) - len(first)))]


def _claude_round(result: AutoDevResult) -> Callable[..., ClaudeReviewRound]:
    def _stub(**_kwargs: object) -> ClaudeReviewRound:
        return ClaudeReviewRound(result, None, backend_down=False)

    return _stub


def _scn_claude_ok(h: Harness) -> Result:
    return h.invoke_claude(_claude_results(h, rest=_claude_ok()))[0]


def _scn_claude_binary_missing(h: Harness) -> Result:
    runner = FakeClaudeRunner()
    h.monkeypatch.setattr("cw.claude_review.RealClaudeRunner", lambda: runner)
    h.monkeypatch.setattr("cw.claude_review._which_claude", lambda: None)
    result = h.cli.invoke(main, h.args("--backend", "claude"))
    assert runner.calls == []
    return result


def _scn_claude_backend_error(h: Harness) -> Result:
    return h.invoke_claude(_claude_results(h, rest=_claude_error()))[0]


def _scn_claude_crash(h: Harness) -> Result:
    h.monkeypatch.setattr(
        "cw.claude_review.run_claude_review", _raise(CwError("cw broke"))
    )
    return h.invoke_claude([])[0]


def _scn_claude_partial(h: Harness) -> Result:
    return h.invoke_claude(_claude_results(h, _claude_ok(), rest=_claude_error()))[0]


def _scn_claude_reviewers_failed(h: Harness) -> Result:
    return h.invoke_claude(_claude_results(h, rest=_claude_unparseable()))[0]


def _scn_claude_discarded(h: Harness) -> Result:
    return h.invoke_claude(_claude_results(h, _claude_discarding(), rest=_claude_ok()))[
        0
    ]


def _scn_claude_unknown_backend(h: Harness) -> Result:
    return h.cli.invoke(main, h.args("--backend", "gemini"))


_CLAUDE_EXIT_TABLE: dict[str, tuple[Callable[[Harness], Result], int, str | None]] = {
    "verdict": (_scn_claude_ok, 0, None),
    "binary_missing": (_scn_claude_binary_missing, 3, "binary_missing"),
    "backend_error": (_scn_claude_backend_error, 3, "backend_error"),
    "crash": (_scn_claude_crash, 3, "crash"),
    "partial": (_scn_claude_partial, 4, "partial"),
    "reviewers_failed": (_scn_claude_reviewers_failed, 4, "reviewers_failed"),
    "discarded_findings": (_scn_claude_discarded, 4, "discarded_findings"),
    "unknown_backend": (_scn_claude_unknown_backend, 2, None),
}


@pytest.mark.parametrize("scenario", list(_CLAUDE_EXIT_TABLE))
def test_claude_exit_code_table(harness: Harness, scenario: str) -> None:
    scenario_fn, expected, cause = _CLAUDE_EXIT_TABLE[scenario]

    result = scenario_fn(harness)

    assert result.exit_code == expected, result.output
    if expected == _UNAVAILABLE:
        assert cause is not None
        _assert_unavailable(result, cause, backend="claude")
    elif expected == _ROUND_FAILED:
        assert cause is not None
        _assert_round_failed(result, cause, backend="claude")
    elif expected == 0:
        assert _payload(result)["backend"] == "claude"
    else:
        assert result.stdout == ""
        _assert_not_outage(result)


def test_claude_happy_path_prints_the_verdict_envelope(harness: Harness) -> None:
    result, runner = harness.invoke_claude(_claude_results(harness, rest=_claude_ok()))

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["available"] is True
    assert payload["backend"] == "claude"
    assert payload["reviewed_sha"] == harness.head()
    assert payload["result_blocker_reason"] is None
    verdict = ReviewVerdict.model_validate(payload["verdict"])
    assert verdict.reviewed_sha == harness.head()
    assert len(runner.calls) == len(harness.roles)
    assert all(call["cwd"] == harness.repo for call in runner.calls)
    assert all(call["timeout"] is None for call in runner.calls)


def test_claude_backend_error_detail_names_the_backend(harness: Harness) -> None:
    result, _runner = harness.invoke_claude(
        _claude_results(harness, rest=_claude_error())
    )

    payload = _assert_unavailable(result, "backend_error", backend="claude")
    assert str(payload["detail"]).startswith("backend=claude; ")


@pytest.mark.parametrize("mutate", [_dirty_untracked, _move_head])
def test_claude_call_level_worktree_change_is_worktree_mutated(
    harness: Harness, mutate: Callable[[Path], None]
) -> None:
    harness.spy_claude_review(after=mutate)

    result, _runner = harness.invoke_claude(_claude_results(harness, rest=_claude_ok()))

    payload = _assert_round_failed(result, "worktree_mutated", backend="claude")
    assert "worktree" in str(payload["detail"])


def test_claude_returned_mutation_blocker_is_worktree_mutated(
    harness: Harness,
) -> None:
    """A per-role G4 park maps to the same cause even with no call-level change."""
    blocked = make_codex_blocked(
        ticket_id=_TICKET,
        worktree=harness.repo,
        reason=CODEX_REVIEW_WORKTREE_MUTATED,
        retry_eligible=False,
        details="backend=claude; role=Code Quality Reviewer; HEAD a -> b",
    )
    harness.monkeypatch.setattr(
        "cw.claude_review.run_claude_review", _claude_round(blocked)
    )

    result, _runner = harness.invoke_claude([])

    payload = _assert_round_failed(result, "worktree_mutated", backend="claude")
    assert (
        payload["detail"] == "backend=claude; role=Code Quality Reviewer; HEAD a -> b"
    )


def test_claude_budget_exhaustion_is_a_round_failure_not_an_outage(
    harness: Harness,
) -> None:
    blocked = make_codex_blocked(
        ticket_id=_TICKET,
        worktree=harness.repo,
        reason=CODEX_REVIEW_UNPARSEABLE,
        retry_eligible=True,
        details=f"backend=claude; Code Quality Reviewer ({CODEX_BUDGET_EXHAUSTED})",
    )
    harness.monkeypatch.setattr(
        "cw.claude_review.run_claude_review", _claude_round(blocked)
    )

    result, _runner = harness.invoke_claude([])

    _assert_round_failed(result, "reviewers_failed", backend="claude")


def test_claude_arguments_and_the_ledger_fork_point_base(harness: Harness) -> None:
    fork = git_in(harness.repo, "rev-parse", "HEAD~1")
    write_ledger(
        harness.repo,
        ledger_with_rounds(
            (),
            shas=[harness.head()],
            confirm="claude",
            ticket_id=_TICKET,
            fork_point=fork,
        ),
    )
    calls = harness.spy_claude_review()

    result, runner = harness.invoke_claude(
        _claude_results(harness, rest=_claude_ok()),
        "--model",
        "claude-sonnet-5-5",
        "--reasoning-effort",
        "low",
    )

    assert result.exit_code == 0, result.output
    [kwargs] = calls
    assert kwargs["runner"] is runner
    assert kwargs["task"] == harness.task
    assert kwargs["worktree"] == harness.repo
    assert kwargs["default_branch"] == "main"
    assert kwargs["base_ref"] == fork
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["wall_clock_budget_seconds"] is None
    assert kwargs["session_id"] == f"confirm-{_SESSION}"
    assert "reasoning_effort" not in kwargs
    argv = runner.calls[0]["argv"]
    assert isinstance(argv, list)
    assert argv[-2:] == ["--model", "claude-sonnet-5-5"]


def test_claude_without_a_ledger_diffs_from_the_configured_branch(
    harness: Harness,
) -> None:
    calls = harness.spy_claude_review()

    result, _runner = harness.invoke_claude(_claude_results(harness, rest=_claude_ok()))

    assert result.exit_code == 0, result.output
    assert calls[0]["base_ref"] == "main"


def test_claude_path_never_needs_codex(harness: Harness) -> None:
    """No @needs_codex here: codex is hidden, and the claude path still runs."""
    result, _runner = harness.invoke_claude(_claude_results(harness, rest=_claude_ok()))

    assert result.exit_code == 0, result.output


def test_claude_leaves_the_row_alone_and_posts_nothing(harness: Harness) -> None:
    def _no_comment(*_args: object, **_kwargs: object) -> None:
        msg = "confirm must not post a tracker comment"
        raise AssertionError(msg)

    harness.monkeypatch.setattr("cw.gh.post_issue_comment", _no_comment)
    before = _row_snapshot()

    result, _runner = harness.invoke_claude(_claude_results(harness, rest=_claude_ok()))

    assert result.exit_code == 0, result.output
    assert _row_snapshot() == before
    assert read_events() == []


@needs_codex
def test_explicit_codex_backend_is_the_default_path(harness: Harness) -> None:
    calls = harness.spy_run_review()

    result, _runner = harness.invoke(
        harness.all_roles(_ok_result()), "--backend", "codex"
    )

    assert result.exit_code == 0, result.output
    assert _payload(result)["backend"] == "codex"
    assert len(calls) == 1


def test_help_lists_the_backend_option() -> None:
    result = CliRunner().invoke(main, ["review", "confirm", "--help"])

    assert result.exit_code == 0
    assert "--backend" in result.output
    flat = " ".join(result.output.split())
    assert "[codex|claude]" in flat
    assert "default: codex" in flat
