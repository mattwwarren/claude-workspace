"""Tests for ``cw.codex_clean_confirm``: the codex-lane clean-mode gate (#2791).

A real feature branch pushed to a bare origin, a real ledger on disk driven
through the real engine, the real Claude review pass, and a fake ``claude -p``
answering each reviewer role with an envelope from
``tests/_claude_review_helpers``. ``_which_claude`` is patched for every test,
so no real ``claude`` binary is ever reached.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from cw import codex_clean_confirm
from cw.auto_dev_result import KNOWN_BLOCKER_REASONS, AutoDevResult
from cw.claude_review import CLAUDE_BACKEND_DETAILS_PREFIX, ClaudeRunResult
from cw.codex_clean_confirm import (
    CODEX_REVIEWER_LABEL,
    CleanConfirmOutcome,
    _decided,
    apply_clean_confirm,
    resume_pending_confirm,
)
from cw.codex_review import (
    CODEX_CONVERGENCE_LEDGER_ERROR,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_REVIEW_WORKTREE_MUTATED,
    make_codex_blocked,
    synthesize_codex_review_result,
)
from cw.dispatch_state import load_executor_blocked_markers
from cw.models import ClientConfig
from cw.models.client import ReviewConfig
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    ConvergenceLedger,
    frozen_counts_from_verdict,
    load_ledger,
    one_line,
    round_verdict_path,
)
from tests._claude_review_helpers import (
    RoleAwareClaudeRunner,
    error_envelope,
    review_envelope,
)
from tests._codex_review_helpers import _doc_json, _finding_payload, _task
from tests._review_convergence_helpers import (
    consolidated_verdict,
    fresh_ledger,
    operator_finding,
    record,
    round_finding,
    stamp_dispositions,
    structurally_invalid_doc_json,
    write_ledger,
)
from tests.conftest import _make_diff, _make_reviewer_doc, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import Blocker
    from cw.review_convergence import ConfirmBackend, RoundFinding
    from cw.review_findings import ReviewVerdict

_MUST_FIX_SUMMARY = "feature constant is wrong"
_SHOULD_FIX_SUMMARY = "feature constant needs a name"


@pytest.fixture(autouse=True)
def _claude_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cw.codex_clean_confirm._which_claude", lambda: "/x/claude")


@pytest.fixture
def wt(make_git_repo_with_origin: Callable[..., tuple[Path, Path]]) -> Path:
    worktree, _origin = make_git_repo_with_origin("clean-confirm")
    return worktree


def _head(wt: Path) -> str:
    return git_in(wt, "rev-parse", "HEAD")


def _fork(wt: Path) -> str:
    return git_in(wt, "merge-base", "origin/main", "HEAD")


def _client(
    wt: Path,
    *,
    convergence: str = "clean",
    confirm: ConfirmBackend | None = None,
    max_cycles: int | None = None,
    default_branch: str = "main",
) -> ClientConfig:
    return ClientConfig(
        name="test",
        workspace_path=wt,
        default_branch=default_branch,
        review=ReviewConfig.model_validate(
            {
                "convergence": convergence,
                "confirm_backend": confirm,
                "max_cycles": max_cycles,
            }
        ),
    )


def _codex(wt: Path, *, sha: str | None = None) -> tuple[AutoDevResult, ReviewVerdict]:
    """A clean codex result and verdict at HEAD, as the fix loop returns them."""
    result, verdict = synthesize_codex_review_result(
        task=_task(),
        worktree=wt,
        documents=[_make_reviewer_doc(reviewer_role="Code Quality Reviewer")],
        failures=[],
        diff=_make_diff("x = 1", files={"feature.py": [1]}),
        reviewed_sha=sha or _head(wt),
        session_id="s-codex",
        default_branch="main",
        fix_loop_enabled=True,
    )
    assert result.blocker is None
    assert verdict is not None
    return result, verdict


def _clean_role(role: str) -> ClaudeRunResult:
    return ClaudeRunResult(0, review_envelope(_doc_json(role=role)), "")


def _finding(severity: str, summary: str) -> dict[str, object]:
    return _finding_payload(
        severity=severity,
        file="feature.py",
        line_start=1,
        line_end=1,
        summary=summary,
        evidence="x = 1",
    )


def _must_fix_role(role: str) -> ClaudeRunResult:
    findings = [
        _finding("MUST_FIX", _MUST_FIX_SUMMARY),
        _finding("SHOULD_FIX", _SHOULD_FIX_SUMMARY),
    ]
    return ClaudeRunResult(
        0, review_envelope(_doc_json(role=role, findings=findings)), ""
    )


def _error_role(_role: str) -> ClaudeRunResult:
    return ClaudeRunResult(0, error_envelope(), "")


def _timeout_role(_role: str) -> ClaudeRunResult:
    return ClaudeRunResult(-1, "", "", timed_out=True)


def _discarding_role(_role: str) -> ClaudeRunResult:
    return ClaudeRunResult(
        0, review_envelope(structurally_invalid_doc_json("MUST_FIX")), ""
    )


def _apply(
    wt: Path,
    client: ClientConfig,
    runner: RoleAwareClaudeRunner,
    *,
    codex: tuple[AutoDevResult, ReviewVerdict | None] | None = None,
    deadline: float | None = None,
) -> CleanConfirmOutcome:
    result, verdict = codex if codex is not None else _codex(wt)
    return apply_clean_confirm(
        task=_task(),
        worktree=wt,
        client=client,
        sid="sid-1",
        runner=runner,
        deadline=deadline,
        result=result,
        verdict=verdict,
    )


def _resume(
    wt: Path, client: ClientConfig, runner: RoleAwareClaudeRunner
) -> CleanConfirmOutcome | None:
    return resume_pending_confirm(
        task=_task(),
        worktree=wt,
        client=client,
        sid="sid-1",
        runner=runner,
        deadline=None,
    )


def _ledger(wt: Path) -> ConvergenceLedger:
    ledger = load_ledger(wt / LEDGER_RELATIVE_PATH)
    assert ledger is not None
    return ledger


def _codex_ledger(
    wt: Path, *rounds: tuple[str, list[RoundFinding]], max_cycles: int = 8
) -> ConvergenceLedger:
    """A codex-lane ledger for this ticket and fork point, via the real engine."""
    ledger = fresh_ledger(
        confirm=None, loop_backend="codex", fork_point=_fork(wt), max_cycles=max_cycles
    )
    for sha, findings in rounds:
        ledger, _ = record(ledger, *findings, sha=sha, backend="codex")
    return ledger


def _awaiting_at(wt: Path, sha: str) -> ConvergenceLedger:
    ledger = _codex_ledger(wt, (sha, []))
    assert ledger.status == "awaiting_confirm"
    return ledger


def _awaiting_at_head_with(
    wt: Path,
    *,
    loop_backend: str = "codex",
    ticket_id: str = "T-1",
    fork_point: str | None = None,
) -> ConvergenceLedger:
    """An ``awaiting_confirm``-at-HEAD ledger with one identity field changed.

    The confirm choice is an explicit independent ``claude``, which is what the
    client resolves to on a codex lane, so a changed choice cannot be what
    makes the resume decline: only the identity field under test can.
    """
    ledger = fresh_ledger(
        confirm="claude",
        loop_backend=loop_backend,
        ticket_id=ticket_id,
        fork_point=fork_point or _fork(wt),
    )
    ledger, report = record(ledger, sha=_head(wt), backend=loop_backend)
    assert report.decision == "confirm_required"
    assert ledger.awaiting_confirm_sha == _head(wt)
    return ledger


def _converged_at(wt: Path, sha: str) -> ConvergenceLedger:
    ledger, report = record(_awaiting_at(wt, sha), sha=sha, kind="confirm")
    assert report.decision == "clean"
    return ledger


def _assert_unchanged(
    outcome: CleanConfirmOutcome, result: AutoDevResult, verdict: object
) -> None:
    assert outcome.result is result
    assert outcome.verdict is verdict
    assert outcome.reviewer_label == CODEX_REVIEWER_LABEL


# ---------------------------------------------------------------------------
# Skip branches: blocking mode never reads or writes a ledger
# ---------------------------------------------------------------------------


class TestSkipBranches:
    def test_a_blocking_client_is_untouched(self, wt: Path) -> None:
        codex = _codex(wt)
        runner = RoleAwareClaudeRunner(_clean_role)
        outcome = _apply(wt, _client(wt, convergence="blocking"), runner, codex=codex)
        _assert_unchanged(outcome, *codex)
        assert not (wt / LEDGER_RELATIVE_PATH).exists()
        assert runner.roles == []

    def test_a_blocked_result_is_untouched(self, wt: Path) -> None:
        result, verdict = _codex(wt)
        blocked = result.model_copy(
            update={"status": "blocked", "blocker": _blocker(wt)}
        )
        outcome = _apply(
            wt,
            _client(wt),
            RoleAwareClaudeRunner(_clean_role),
            codex=(blocked, verdict),
        )
        _assert_unchanged(outcome, blocked, verdict)
        assert not (wt / LEDGER_RELATIVE_PATH).exists()

    def test_a_missing_verdict_is_untouched(self, wt: Path) -> None:
        result, _ = _codex(wt)
        outcome = _apply(
            wt, _client(wt), RoleAwareClaudeRunner(_clean_role), codex=(result, None)
        )
        _assert_unchanged(outcome, result, None)
        assert not (wt / LEDGER_RELATIVE_PATH).exists()

    def test_resume_is_none_for_a_blocking_client(self, wt: Path) -> None:
        write_ledger(wt, _awaiting_at(wt, _head(wt)))
        runner = RoleAwareClaudeRunner(_clean_role)
        assert _resume(wt, _client(wt, convergence="blocking"), runner) is None
        assert runner.roles == []


def _blocker(wt: Path) -> Blocker:
    blocker = make_codex_blocked(
        ticket_id="T-1", worktree=wt, reason=CODEX_MUST_FIX_FINDINGS
    ).blocker
    assert blocker is not None
    return blocker


# ---------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------


class TestConverge:
    def test_the_clients_worker_model_is_the_confirm_model(self, wt: Path) -> None:
        client = _client(wt).model_copy(update={"worker_model": "claude-sonnet-5-5"})
        runner = RoleAwareClaudeRunner(_clean_role)
        _apply(wt, client, runner)
        assert runner.argvs
        assert all(
            argv[-2:] == ["--model", "claude-sonnet-5-5"] for argv in runner.argvs
        )

    def test_without_a_worker_model_no_model_flag_is_passed(self, wt: Path) -> None:
        runner = RoleAwareClaudeRunner(_clean_role)
        _apply(wt, _client(wt), runner)
        assert runner.argvs
        assert all("--model" not in argv for argv in runner.argvs)

    def test_unset_backend_runs_an_independent_claude_confirm(self, wt: Path) -> None:
        codex = _codex(wt)
        runner = RoleAwareClaudeRunner(_clean_role)
        outcome = _apply(wt, _client(wt), runner, codex=codex)

        assert runner.roles
        review = outcome.result.review
        assert review.convergence_mode == "clean"
        assert review.converged is True
        assert review.confirm_backend == "claude"
        assert review.rounds_run == 2
        assert review.final_round_clean_sha == _head(wt)
        assert outcome.verdict is codex[1]
        assert outcome.reviewer_label == CODEX_REVIEWER_LABEL
        ledger = _ledger(wt)
        assert ledger.status == "converged"
        assert ledger.last_clean_sha == _head(wt)
        assert ledger.loop_backend == "codex"
        assert ledger.confirm_backend == "claude"
        assert [(r.kind, r.backend) for r in ledger.rounds] == [
            ("loop", "codex"),
            ("confirm", "claude"),
        ]
        assert round_verdict_path(wt / LEDGER_RELATIVE_PATH, 2).exists()
        friction = outcome.result.friction_highlights
        assert any(
            line.startswith("review_round_1: kind=loop backend=codex")
            for line in friction
        )
        assert any(
            line.startswith("review_round_2: kind=confirm backend=claude")
            for line in friction
        )

    def test_only_the_five_fields_are_merged(self, wt: Path) -> None:
        result, verdict = _codex(wt)
        before = result.review.model_copy(update={"fix_cycles_used": 2})
        result = result.model_copy(update={"review": before})
        outcome = _apply(
            wt, _client(wt), RoleAwareClaudeRunner(_clean_role), codex=(result, verdict)
        )
        after = outcome.result.review
        assert after.fix_cycles_used == 2
        assert after.must_fix_initial == before.must_fix_initial
        assert after.should_fix == before.should_fix
        assert after.agents_run == before.agents_run
        AutoDevResult.model_validate(outcome.result.model_dump(mode="json"))

    def test_confirm_none_converges_on_the_loop_round(self, wt: Path) -> None:
        runner = RoleAwareClaudeRunner(_clean_role)
        outcome = _apply(wt, _client(wt, confirm="none"), runner)
        assert runner.roles == []
        assert outcome.result.review.converged is True
        assert outcome.result.review.confirm_backend == "none"
        assert outcome.result.review.rounds_run == 1
        assert (
            "confirm_round_opt_out: review.confirm_backend=none"
            in outcome.result.friction_highlights
        )

    def test_explicit_codex_is_not_independent(self, wt: Path) -> None:
        runner = RoleAwareClaudeRunner(_clean_role)
        outcome = _apply(wt, _client(wt, confirm="codex"), runner)
        assert runner.roles == []
        assert outcome.result.review.converged is True
        assert outcome.result.review.confirm_backend == "codex"
        assert (
            "confirm_not_independent: confirm_backend=codex equals loop backend "
            "codex; no independent round ran"
        ) in outcome.result.friction_highlights

    def test_ledger_argument_sources(self, wt: Path) -> None:
        codex = _codex(wt)
        client = _client(wt, max_cycles=5)
        _apply(wt, client, RoleAwareClaudeRunner(_clean_role), codex=codex)
        ledger = _ledger(wt)
        assert ledger.ticket_id == "T-1"
        assert ledger.fork_point == _fork(wt)
        assert ledger.checkpoint_3a_sha == _head(wt)
        assert ledger.max_cycles == client.review.effective_max_cycles == 5
        loop, confirm = ledger.rounds
        assert loop.reviewed_sha == codex[1].reviewed_sha
        assert loop.roster == [r.reviewer_role for r in codex[1].agents_run]
        assert ledger.frozen == frozen_counts_from_verdict(codex[1])
        assert confirm.reviewed_sha == _head(wt)
        assert confirm.roster

    def test_marker_names_claude_during_the_confirm(self, wt: Path) -> None:
        seen: list[tuple[str, str | None, str]] = []

        def _snapshot(_worktree: Path) -> None:
            marker = load_executor_blocked_markers()["test/T-1"]
            assert marker.client == "test"
            assert marker.ticket_id == "T-1"
            seen.append((marker.executor, marker.reviewer_role, marker.session_id))

        runner = RoleAwareClaudeRunner(_clean_role, side_effect=_snapshot)
        _apply(wt, _client(wt), runner)
        assert seen
        assert set(seen) == {("claude", "confirm", "sid-1")}


# ---------------------------------------------------------------------------
# The ledger-state table
# ---------------------------------------------------------------------------


class TestLedgerStates:
    def test_another_tickets_ledger_is_reset(self, wt: Path) -> None:
        write_ledger(wt, fresh_ledger(ticket_id="T-other", loop_backend="codex"))
        outcome = _apply(wt, _client(wt), RoleAwareClaudeRunner(_clean_role))
        ledger = _ledger(wt)
        assert ledger.ticket_id == "T-1"
        assert len(ledger.reset_history) == 1
        assert ledger.status == "converged"
        assert (
            "convergence_ledger_reset: ticket_mismatch"
            in outcome.result.friction_highlights
        )

    def test_in_progress_is_resumed(self, wt: Path) -> None:
        write_ledger(wt, _codex_ledger(wt, (_fork(wt), [round_finding()])))
        _apply(wt, _client(wt), RoleAwareClaudeRunner(_clean_role))
        ledger = _ledger(wt)
        assert [r.kind for r in ledger.rounds] == ["loop", "loop", "confirm"]
        assert ledger.status == "converged"

    def test_awaiting_confirm_at_head_skips_the_loop_record(self, wt: Path) -> None:
        write_ledger(wt, _awaiting_at(wt, _head(wt)))
        _apply(wt, _client(wt), RoleAwareClaudeRunner(_clean_role))
        assert [r.kind for r in _ledger(wt).rounds] == ["loop", "confirm"]

    def test_awaiting_confirm_elsewhere_records_a_new_candidate(self, wt: Path) -> None:
        write_ledger(wt, _awaiting_at(wt, _fork(wt)))
        _apply(wt, _client(wt), RoleAwareClaudeRunner(_clean_role))
        ledger = _ledger(wt)
        assert [(r.kind, r.reviewed_sha) for r in ledger.rounds] == [
            ("loop", _fork(wt)),
            ("loop", _head(wt)),
            ("confirm", _head(wt)),
        ]

    def test_converged_at_head_is_idempotent(self, wt: Path) -> None:
        path = write_ledger(wt, _converged_at(wt, _head(wt)))
        before = path.read_text(encoding="utf-8")
        runner = RoleAwareClaudeRunner(_clean_role)
        outcome = _apply(wt, _client(wt), runner)
        assert runner.roles == []
        assert path.read_text(encoding="utf-8") == before
        assert outcome.result.review.converged is True
        assert outcome.result.review.rounds_run == 2

    def test_converged_with_a_moved_head_is_rereviewed(self, wt: Path) -> None:
        write_ledger(wt, _converged_at(wt, _fork(wt)))
        _apply(wt, _client(wt), RoleAwareClaudeRunner(_clean_role))
        ledger = _ledger(wt)
        assert len(ledger.rounds) == 4
        assert ledger.last_clean_sha == _head(wt)

    def test_stopped_on_an_older_tip_reopens(self, wt: Path) -> None:
        write_ledger(wt, _codex_ledger(wt, (_fork(wt), [operator_finding()])))
        _apply(wt, _client(wt), RoleAwareClaudeRunner(_clean_role))
        ledger = _ledger(wt)
        assert ledger.status == "converged"
        assert ledger.releases[-1].cause == "new_commits"

    def test_stopped_same_tip_releasable_is_released(self, wt: Path) -> None:
        write_ledger(wt, _codex_ledger(wt, (_head(wt), [operator_finding()])))
        _apply(wt, _client(wt), RoleAwareClaudeRunner(_clean_role))
        ledger = _ledger(wt)
        assert ledger.status == "converged"
        assert ledger.releases[-1].cause == "operator_action_done"

    def test_stopped_same_tip_terminal_parks(self, wt: Path) -> None:
        stalled = [
            (sha, [round_finding(summary=f"stalled {i}.{n}") for n in range(3)])
            for i, sha in enumerate(("stall-0", "stall-1", _head(wt)))
        ]
        path = write_ledger(wt, _codex_ledger(wt, *stalled))
        before = path.read_text(encoding="utf-8")
        codex = _codex(wt)
        runner = RoleAwareClaudeRunner(_clean_role)
        outcome = _apply(wt, _client(wt), runner, codex=codex)
        blocker = outcome.result.blocker
        assert blocker is not None
        assert blocker.reason == "review_not_converging"
        assert blocker.retry_eligible is False
        assert "no_progress" in blocker.details
        assert outcome.result.review.converged is False
        assert outcome.verdict is codex[1]
        assert runner.roles == []
        assert path.read_text(encoding="utf-8") == before

    def test_a_foreign_loop_backend_parks_as_a_ledger_error(self, wt: Path) -> None:
        foreign = fresh_ledger(loop_backend="claude-native", fork_point=_fork(wt))
        write_ledger(wt, foreign)
        outcome = _apply(wt, _client(wt), RoleAwareClaudeRunner(_clean_role))
        blocker = outcome.result.blocker
        assert blocker is not None
        assert blocker.reason == CODEX_CONVERGENCE_LEDGER_ERROR
        assert "foreign convergence ledger" in blocker.details
        assert "--stage review" in blocker.details


# ---------------------------------------------------------------------------
# Pre-loop resume
# ---------------------------------------------------------------------------


class TestResume:
    def test_awaiting_at_head_runs_only_the_confirm(self, wt: Path) -> None:
        write_ledger(wt, _awaiting_at(wt, _head(wt)))
        runner = RoleAwareClaudeRunner(_clean_role)
        outcome = _resume(wt, _client(wt), runner)
        assert outcome is not None
        assert runner.roles
        assert outcome.reviewer_label == "Claude"
        assert outcome.verdict is not None
        assert outcome.result.review.converged is True
        assert "clean_confirm_resumed" in outcome.result.friction_highlights
        assert _ledger(wt).status == "converged"

    @pytest.mark.parametrize(
        "case",
        [
            "other_sha",
            "in_progress",
            "absent",
            "corrupt",
            "foreign_loop_backend",
            "other_ticket",
            "other_fork_point",
        ],
    )
    def test_every_other_state_runs_the_loop(self, wt: Path, case: str) -> None:
        path = wt / LEDGER_RELATIVE_PATH
        if case == "other_sha":
            write_ledger(wt, _awaiting_at(wt, _fork(wt)))
        elif case == "foreign_loop_backend":
            write_ledger(wt, _awaiting_at_head_with(wt, loop_backend="opencode"))
        elif case == "other_ticket":
            write_ledger(wt, _awaiting_at_head_with(wt, ticket_id="T-other"))
        elif case == "other_fork_point":
            write_ledger(wt, _awaiting_at_head_with(wt, fork_point="not-the-fork"))
        elif case == "in_progress":
            write_ledger(wt, _codex_ledger(wt, (_fork(wt), [round_finding()])))
        elif case == "corrupt":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")
        runner = RoleAwareClaudeRunner(_clean_role)
        assert _resume(wt, _client(wt), runner) is None
        assert runner.roles == []

    @pytest.mark.parametrize(
        ("choice", "friction"),
        [
            ("none", "confirm_round_opt_out: review.confirm_backend=none"),
            (
                "codex",
                "confirm_not_independent: confirm_backend=codex equals loop "
                "backend codex; no independent round ran",
            ),
        ],
    )
    def test_a_changed_confirm_backend_is_honoured_on_requeue(
        self, wt: Path, choice: ConfirmBackend, friction: str
    ) -> None:
        # The ledger was created while the client resolved to claude.
        write_ledger(wt, _awaiting_at(wt, _head(wt)))
        client = _client(wt, confirm=choice)
        runner = RoleAwareClaudeRunner(_clean_role)

        assert _resume(wt, client, runner) is None
        outcome = _apply(wt, client, runner)

        assert runner.roles == []
        assert outcome.result.blocker is None
        assert outcome.result.review.converged is True
        assert outcome.result.review.confirm_backend == choice
        assert friction in outcome.result.friction_highlights
        ledger = _ledger(wt)
        assert ledger.status == "converged"
        assert ledger.confirm_backend == choice
        assert ledger.confirm_failures == []

    def test_a_dirty_confirm_reruns_the_loop_on_requeue(self, wt: Path) -> None:
        _apply(wt, _client(wt), RoleAwareClaudeRunner(_must_fix_role))
        assert _ledger(wt).status != "awaiting_confirm"
        assert _resume(wt, _client(wt), RoleAwareClaudeRunner(_clean_role)) is None

    def test_a_bookkeeping_failure_during_resume_parks(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(wt, _awaiting_at(wt, _head(wt)))

        def _fail(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.codex_clean_confirm.save_ledger", _fail)
        outcome = _resume(wt, _client(wt), RoleAwareClaudeRunner(_clean_role))
        assert outcome is not None
        assert outcome.result.blocker is not None
        assert outcome.result.blocker.reason == CODEX_CONVERGENCE_LEDGER_ERROR
        assert outcome.verdict is None
        assert outcome.reviewer_label == "Claude"


# ---------------------------------------------------------------------------
# Claude unreachable: confirm_backend_unavailable, escalating at the cap
# ---------------------------------------------------------------------------


def _assert_unavailable(outcome: CleanConfirmOutcome, wt: Path, cause: str) -> None:
    blocker = outcome.result.blocker
    assert blocker is not None
    assert blocker.reason == "confirm_backend_unavailable"
    assert blocker.retry_eligible is True
    assert blocker.details.startswith(CLAUDE_BACKEND_DETAILS_PREFIX + cause)
    assert outcome.result.review.converged is False
    assert f"clean_confirm_backend_unavailable: cause={cause}" in (
        outcome.result.friction_highlights
    )
    ledger = _ledger(wt)
    assert ledger.status == "awaiting_confirm"
    assert [f.cause for f in ledger.confirm_failures] == [cause]


class TestOutage:
    def test_binary_missing(self, wt: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.codex_clean_confirm._which_claude", lambda: None)
        codex = _codex(wt)
        runner = RoleAwareClaudeRunner(_clean_role)
        outcome = _apply(wt, _client(wt), runner, codex=codex)
        _assert_unavailable(outcome, wt, "binary_missing")
        assert runner.roles == []
        assert outcome.verdict is codex[1]

    def test_every_role_a_backend_error(self, wt: Path) -> None:
        outcome = _apply(wt, _client(wt), RoleAwareClaudeRunner(_error_role))
        _assert_unavailable(outcome, wt, "backend_error")

    def test_a_crashing_call(self, wt: Path) -> None:
        def _crash(role: str) -> ClaudeRunResult:
            msg = f"spawn failed for {role}"
            raise OSError(msg)

        outcome = _apply(wt, _client(wt), RoleAwareClaudeRunner(_crash))
        _assert_unavailable(outcome, wt, "crash")

    def test_the_fourth_same_cause_failure_escalates(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.codex_clean_confirm._which_claude", lambda: None)
        runner = RoleAwareClaudeRunner(_clean_role)
        outcomes = [_apply(wt, _client(wt), runner) for _ in range(4)]
        reasons = [o.result.blocker.reason for o in outcomes if o.result.blocker]
        assert reasons == ["confirm_backend_unavailable"] * 3 + ["agent_block"]
        last = outcomes[-1].result.blocker
        assert last is not None
        assert last.retry_eligible is False
        assert len(_ledger(wt).confirm_failures) == 4


# ---------------------------------------------------------------------------
# A round that could not complete on a healthy backend: nothing recorded
# ---------------------------------------------------------------------------


def _partial_role() -> Callable[[str], ClaudeRunResult]:
    seen: list[str] = []

    def _respond(role: str) -> ClaudeRunResult:
        seen.append(role)
        return _error_role(role) if len(seen) == 1 else _clean_role(role)

    return _respond


class TestHealthyBackendFailures:
    @pytest.mark.parametrize(
        "respond",
        [_timeout_role, _discarding_role, _partial_role()],
        ids=["timeout", "discarded", "partial"],
    )
    def test_the_round_is_returned_unchanged(
        self, wt: Path, respond: Callable[[str], ClaudeRunResult]
    ) -> None:
        outcome = _apply(wt, _client(wt), RoleAwareClaudeRunner(respond))
        blocker = outcome.result.blocker
        assert blocker is not None
        assert blocker.reason not in (
            CODEX_MUST_FIX_FINDINGS,
            "confirm_backend_unavailable",
        )
        assert blocker.details.startswith(CLAUDE_BACKEND_DETAILS_PREFIX)
        assert outcome.reviewer_label == "Claude"
        assert outcome.result.review.convergence_mode is None
        ledger = _ledger(wt)
        assert ledger.status == "awaiting_confirm"
        assert ledger.confirm_failures == []
        assert [r.kind for r in ledger.rounds] == ["loop"]

    def test_an_exhausted_budget_never_counts_as_an_outage(self, wt: Path) -> None:
        runner = RoleAwareClaudeRunner(_clean_role)
        for _ in range(5):
            outcome = _apply(wt, _client(wt), runner, deadline=time.monotonic() - 1.0)
            blocker = outcome.result.blocker
            assert blocker is not None
            assert blocker.reason == CODEX_REVIEW_UNPARSEABLE
            assert blocker.retry_eligible is True
        assert runner.roles == []
        ledger = _ledger(wt)
        assert ledger.confirm_failures == []
        assert ledger.status == "awaiting_confirm"

    def test_a_live_budget_reaches_the_roles(self, wt: Path) -> None:
        outcome = _apply(
            wt,
            _client(wt),
            RoleAwareClaudeRunner(_clean_role),
            deadline=time.monotonic() + 3600,
        )
        assert outcome.result.review.converged is True

    def test_a_mutating_reviewer_parks_and_never_escalates(self, wt: Path) -> None:
        writes: list[str] = []

        def _hook(worktree: Path) -> None:
            name = f"hook-{len(writes)}.txt"
            writes.append(name)
            (worktree / name).write_text("x\n", encoding="utf-8")

        runner = RoleAwareClaudeRunner(_clean_role, side_effect=_hook)
        for _ in range(5):
            outcome = _apply(wt, _client(wt), runner)
            blocker = outcome.result.blocker
            assert blocker is not None
            assert blocker.reason == CODEX_REVIEW_WORKTREE_MUTATED
            assert blocker.retry_eligible is False
            assert outcome.verdict is None
        ledger = _ledger(wt)
        assert ledger.status == "awaiting_confirm"
        assert ledger.confirm_failures == []
        assert all((wt / name).exists() for name in writes)


# ---------------------------------------------------------------------------
# A Claude MUST_FIX
# ---------------------------------------------------------------------------


class TestMustFix:
    def test_a_dirty_confirm_parks_with_the_claude_verdict(self, wt: Path) -> None:
        outcome = _apply(wt, _client(wt), RoleAwareClaudeRunner(_must_fix_role))
        blocker = outcome.result.blocker
        assert blocker is not None
        assert blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert blocker.details.startswith(
            CLAUDE_BACKEND_DETAILS_PREFIX + "## Claude Review Verdict"
        )
        assert outcome.reviewer_label == "Claude"
        assert outcome.verdict is not None
        assert outcome.verdict.blocking is True
        assert outcome.result.review.converged is False
        ledger = _ledger(wt)
        assert ledger.status == "in_progress"
        confirm = ledger.rounds[-1]
        assert confirm.kind == "confirm"
        assert confirm.clean is False
        assert confirm.new_fix >= 1
        assert {e.outcome for e in ledger.settled} == {"defer"}
        assert all(e.identity.severity == "SHOULD_FIX" for e in ledger.settled)
        assert round_verdict_path(wt / LEDGER_RELATIVE_PATH, 2).exists()

    def test_a_stopping_confirm_appends_the_release_hint(self, wt: Path) -> None:
        write_ledger(
            wt, _codex_ledger(wt, (_fork(wt), [round_finding()]), max_cycles=1)
        )
        outcome = _apply(
            wt, _client(wt, max_cycles=1), RoleAwareClaudeRunner(_must_fix_role)
        )
        blocker = outcome.result.blocker
        assert blocker is not None
        assert blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert "convergence ledger stopped (max_cycles); release:" in blocker.details
        assert _ledger(wt).status == "stopped"


# ---------------------------------------------------------------------------
# Bookkeeping failures: a typed, retry-eligible park, never a raise
# ---------------------------------------------------------------------------


def _assert_ledger_error(
    outcome: CleanConfirmOutcome, codex: tuple[AutoDevResult, ReviewVerdict]
) -> None:
    blocker = outcome.result.blocker
    assert blocker is not None
    assert blocker.reason == CODEX_CONVERGENCE_LEDGER_ERROR
    assert blocker.retry_eligible is True
    assert outcome.verdict is codex[1]
    assert outcome.reviewer_label == CODEX_REVIEWER_LABEL
    assert outcome.result.review.convergence_mode == "clean"
    assert outcome.result.review.converged is False
    assert any(
        line.startswith("convergence_bookkeeping_failed: ")
        for line in outcome.result.friction_highlights
    )


class TestBookkeeping:
    def test_a_failed_save(self, wt: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.codex_clean_confirm.save_ledger", _fail)
        codex = _codex(wt)
        outcome = _apply(
            wt, _client(wt), RoleAwareClaudeRunner(_clean_role), codex=codex
        )
        _assert_ledger_error(outcome, codex)
        assert "OSError: disk full" in (outcome.result.blocker.details or "")
        assert outcome.result.review.rounds_run is None

    def test_an_unreadable_ledger(self, wt: Path) -> None:
        path = wt / LEDGER_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        codex = _codex(wt)
        outcome = _apply(
            wt, _client(wt), RoleAwareClaudeRunner(_clean_role), codex=codex
        )
        _assert_ledger_error(outcome, codex)

    def test_a_merge_base_failure(self, wt: Path) -> None:
        codex = _codex(wt)
        outcome = _apply(
            wt,
            _client(wt, default_branch="no-such-branch"),
            RoleAwareClaudeRunner(_clean_role),
            codex=codex,
        )
        _assert_ledger_error(outcome, codex)
        assert "CalledProcessError" in (outcome.result.blocker.details or "")

    def test_a_verdict_for_another_sha(self, wt: Path) -> None:
        codex = _codex(wt, sha=_fork(wt))
        outcome = _apply(
            wt, _client(wt), RoleAwareClaudeRunner(_clean_role), codex=codex
        )
        _assert_ledger_error(outcome, codex)
        assert not (wt / LEDGER_RELATIVE_PATH).exists()


class TestParkFinalizationFailure:
    """A failure inside ``_finish`` must not escape the bookkeeping handler."""

    @staticmethod
    def _fail_first_finish(monkeypatch: pytest.MonkeyPatch) -> list[int]:
        real = codex_clean_confirm._finish
        calls: list[int] = []

        def _finish(*args: object, **kwargs: object) -> AutoDevResult:
            calls.append(1)
            if len(calls) == 1:
                msg = "friction could not be merged"
                raise ValueError(msg)
            return real(*args, **kwargs)

        monkeypatch.setattr("cw.codex_clean_confirm._finish", _finish)
        return calls

    def test_the_loop_path_falls_back_to_the_bare_park(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._fail_first_finish(monkeypatch)
        codex = _codex(wt, sha=_fork(wt))
        outcome = _apply(
            wt, _client(wt), RoleAwareClaudeRunner(_clean_role), codex=codex
        )
        assert calls == [1]
        blocker = outcome.result.blocker
        assert blocker is not None
        assert blocker.reason == CODEX_CONVERGENCE_LEDGER_ERROR
        assert blocker.retry_eligible is True
        assert outcome.verdict is codex[1]
        assert outcome.reviewer_label == CODEX_REVIEWER_LABEL
        assert outcome.fix_loop_footer is True
        assert outcome.result.review.convergence_mode == "clean"
        assert outcome.result.review.converged is False

    def test_the_resume_path_falls_back_to_the_bare_park(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(wt, _awaiting_at(wt, _head(wt)))

        def _fail_save(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.codex_clean_confirm.save_ledger", _fail_save)
        self._fail_first_finish(monkeypatch)
        outcome = _resume(wt, _client(wt), RoleAwareClaudeRunner(_clean_role))
        assert outcome is not None
        assert outcome.result.blocker is not None
        assert outcome.result.blocker.reason == CODEX_CONVERGENCE_LEDGER_ERROR
        assert outcome.verdict is None
        assert outcome.reviewer_label == "Claude"
        assert outcome.fix_loop_footer is False
        assert outcome.result.review.convergence_mode == "clean"
        assert outcome.result.review.converged is False


def test_both_new_reasons_are_registered() -> None:
    assert CODEX_CONVERGENCE_LEDGER_ERROR in KNOWN_BLOCKER_REASONS
    assert CODEX_REVIEW_WORKTREE_MUTATED in KNOWN_BLOCKER_REASONS


def test_a_long_failure_detail_is_capped_to_one_line() -> None:
    capped = one_line("word\n" * 200)
    assert "\n" not in capped
    assert capped.endswith("...")
    assert len(capped) == 303
    assert one_line("a\n  b", limit=3) == "a b"
    assert one_line("abcdef", limit=3) == "abc..."


def test_an_undecided_must_fix_is_recorded_as_a_fix() -> None:
    verdict = consolidated_verdict(accepted=[("MUST_FIX", "undecided")])
    dropped = stamp_dispositions(verdict, {"undecided": ("dropped", "")})
    (item,) = dropped.accepted
    assert _decided(item).disposition == "fixed"
