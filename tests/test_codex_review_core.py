"""Tests for cw.codex_review.core — the ``run_review`` orchestration (#1236,
#1392)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest

from cw.codex_review import (
    CODEX_MAX_PROMPT_CHARS,
    CODEX_REVIEW_PARTIAL,
    ReviewInputTooLargeError,
    _prepare_review_pass,
    run_review,
)
from cw.codex_review import core as codex_review_core
from cw.codex_review._shard import plan_prompt_shards
from cw.codex_runner import CodexRunResult, FakeCodexRunner
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from cw.review_convergence import unread_from_verdict
from cw.review_finding_dispositions import (
    FindingDisposition,
    _disposition_key,
)
from cw.review_markers import RefusedDisposition
from tests._cli_review_helpers import CLAIM_ROW1_CANDIDATE, CLAIM_ROW1_RECORDED
from tests._codex_review_helpers import (
    _finding_payload,
    _ok_result,
    _SequencedRunner,
    _shard_limit_inputs,
    _task,
)
from tests.conftest import _make_reviewer_doc, commit_tracked_file, git_in
from tests.test_review_adjudication import _make_voided_finding

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_review import _ReviewPassInputs
    from cw.codex_review._shard import _ShardPlan
    from cw.review_findings import ReviewVerdict


def test_run_review_threads_session_id_and_reasoning_effort_to_run_codex_role(
    make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = make_git_repo("wt-run-review-thread")
    captured: dict[str, object] = {}

    def _spy_run_codex_role(**kwargs: object) -> tuple[object, object, object, object]:
        captured["session_id"] = kwargs["session_id"]
        captured["reasoning_effort"] = kwargs["reasoning_effort"]
        return _make_reviewer_doc(), None, {}, []

    monkeypatch.setattr("cw.codex_review._roles._run_codex_role", _spy_run_codex_role)
    run_review(
        # One queued result for the cold-cache filesystem-capability probe
        # (#1709); _run_codex_role is monkeypatched, so no role consumes a slot.
        runner=_SequencedRunner([_ok_result()]),
        task=_task(),
        worktree=worktree,
        default_branch="main",
        model=None,
        reasoning_effort="max",
        wall_clock_budget_seconds=None,
        session_id="sess-thread",
        fix_loop_enabled=False,
    )
    assert captured["session_id"] == "sess-thread"
    assert captured["reasoning_effort"] == "max"


# ---------------------------------------------------------------------------
# _prepare_review_pass extraction (#1392)
# ---------------------------------------------------------------------------


class TestPrepareReviewPass:
    def test_run_review_delegates_through_prepared_inputs(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # run_review's output is unchanged by the extraction: a clean per-role
        # pass over the prepared inputs still yields stage_complete, and a
        # real non-empty (non-blocking) finding survives diff-based validation
        # and is reflected in the consolidated verdict.
        repo = make_git_repo("wt-prepare-run")
        git_in(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n", encoding="utf-8")
        git_in(repo, "add", "mod.py")
        git_in(repo, "commit", "-m", "add mod.py")

        # This throwaway pass also warms the #1709 capability cache, so the
        # real run_review below gets a cache hit and spends no runner slot.
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-prepare-run"
        )
        results = [_ok_result() for _ in prepared.roles]
        results[0] = _ok_result(
            findings=[
                _finding_payload(
                    severity="SHOULD_FIX", file="mod.py", line_start=1, line_end=1
                )
            ]
        )
        runner = _SequencedRunner(results)
        result, verdict = run_review(
            runner=runner,
            task=_task(),
            worktree=repo,
            default_branch="main",
            model=None,
            reasoning_effort=None,
            wall_clock_budget_seconds=None,
            session_id="sess-prepare-run",
            fix_loop_enabled=False,
        )
        assert result.status == "stage_complete"
        assert verdict is not None
        assert verdict.blocking is False
        assert verdict.review.should_fix == 1
        assert len(runner.calls) == len(prepared.roles)
        # #1773: the prepared pass's per-role agent-spec status reaches the
        # verdict through run_review's synthesis hop.
        assert {s.role for s in verdict.agent_spec_status} == set(prepared.roles)

    def test_run_review_threads_prepared_voided_findings_into_synthesis(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #1814: same passthrough shape as the #1773 assertion above, for the
        # voided-findings hop. Covers core.py's own
        # prepared.voided_findings -> synthesize_codex_review_result threading;
        # the suppression function itself is unit-tested elsewhere.
        repo = make_git_repo("wt-prepare-voided-run")
        git_in(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n", encoding="utf-8")
        git_in(repo, "add", "mod.py")
        git_in(repo, "commit", "-m", "add mod.py")

        monkeypatch.setattr(
            "cw.codex_review._context.core._load_voided_findings",
            lambda *_a, **_kw: [
                _make_voided_finding(
                    severity="MUST_FIX",
                    file="mod.py",
                    summary="Bug here",
                    evidence="def broken():",
                )
            ],
        )
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-voided-run"
        )
        results = [_ok_result() for _ in prepared.roles]
        results[0] = _ok_result(
            findings=[
                _finding_payload(
                    severity="MUST_FIX", file="mod.py", line_start=1, line_end=1
                )
            ]
        )
        result, verdict = run_review(
            runner=_SequencedRunner(results),
            task=_task(),
            worktree=repo,
            default_branch="main",
            model=None,
            reasoning_effort=None,
            wall_clock_budget_seconds=None,
            session_id="sess-voided-run",
            fix_loop_enabled=False,
        )

        assert verdict is not None
        assert verdict.blocking is False
        assert result.status == "stage_complete"
        assert [af.disposition for af in verdict.accepted] == ["rejected"]

    def test_run_review_threads_prepared_finding_dispositions_into_synthesis(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #1838: same passthrough shape as the #1814 assertion above, for the
        # finding-dispositions hop. Covers core.py's own
        # prepared.finding_dispositions -> synthesize_codex_review_result
        # threading; the suppression function itself is unit-tested elsewhere.
        repo = make_git_repo("wt-prepare-dispositions-run")
        git_in(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n", encoding="utf-8")
        git_in(repo, "add", "mod.py")
        git_in(repo, "commit", "-m", "add mod.py")

        key = _disposition_key("mod.py", "Bug here")
        assert key is not None
        monkeypatch.setattr(
            "cw.codex_review._context.core._load_finding_dispositions",
            lambda *_a, **_kw: (
                {
                    key: FindingDisposition(
                        outcome="REJECTED",
                        rationale="settled in an earlier round",
                        recorded_at="2026-08-16T00:00:00Z",
                        # #2210 round 2: only a fully-provenanced record is
                        # applied, so the fixture carries what the writer
                        # records.
                        actor="mattwwarren",
                        # #2232: the sha this pass reviews, i.e. "settled
                        # against exactly this code". This case is about the
                        # ledger reaching synthesis, not about drift.
                        reviewed_sha=git_in(repo, "rev-parse", "HEAD"),
                        summary="Bug here",
                    )
                },
                [],
            ),
        )
        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-dispositions-run",
        )
        assert prepared.finding_dispositions
        results = [_ok_result() for _ in prepared.roles]
        results[0] = _ok_result(
            findings=[
                _finding_payload(
                    severity="MUST_FIX", file="mod.py", line_start=1, line_end=1
                )
            ]
        )
        result, verdict = run_review(
            runner=_SequencedRunner(results),
            task=_task(),
            worktree=repo,
            default_branch="main",
            model=None,
            reasoning_effort=None,
            wall_clock_budget_seconds=None,
            session_id="sess-dispositions-run",
            fix_loop_enabled=False,
        )

        assert verdict is not None
        assert verdict.blocking is False
        assert result.status == "stage_complete"
        assert [af.disposition for af in verdict.accepted] == ["rejected"]

    def test_run_review_threads_refused_marker_records_onto_the_verdict(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #2210 round 3: a marker record refused at parse time is not in the
        # ledger, so it rides beside it (prepared.refused_dispositions ->
        # synthesize_codex_review_result) to reach the verdict. The finding it
        # tried to silence keeps blocking.
        repo = make_git_repo("wt-prepare-dispositions-refused")
        git_in(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n", encoding="utf-8")
        git_in(repo, "add", "mod.py")
        git_in(repo, "commit", "-m", "add mod.py")

        key = _disposition_key("mod.py", "Bug here")
        assert key is not None
        refused = [RefusedDisposition(key=key, missing=["actor"])]
        monkeypatch.setattr(
            "cw.codex_review._context.core._load_finding_dispositions",
            lambda *_a, **_kw: ({}, refused),
        )
        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-dispositions-refused",
        )
        assert prepared.refused_dispositions == refused
        results = [_ok_result() for _ in prepared.roles]
        results[0] = _ok_result(
            findings=[
                _finding_payload(
                    severity="MUST_FIX", file="mod.py", line_start=1, line_end=1
                )
            ]
        )
        result, verdict = run_review(
            runner=_SequencedRunner(results),
            task=_task(),
            worktree=repo,
            default_branch="main",
            model=None,
            reasoning_effort=None,
            wall_clock_budget_seconds=None,
            session_id="sess-dispositions-refused",
            fix_loop_enabled=False,
        )

        assert verdict is not None
        assert verdict.blocking is True
        assert verdict.refused_dispositions == refused
        assert result.blocker is not None
        assert "Disposition records refused" in result.blocker.details

    def _reworded_run(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        session_id: str,
        claim_tier_enabled: bool = False,
        disposition_drift_check_enabled: bool = True,
    ) -> tuple[AutoDevResult, ReviewVerdict | None]:
        """One `run_review` over a MUST_FIX that REWORDS a ledgered finding."""
        key = _disposition_key("mod.py", CLAIM_ROW1_RECORDED)
        assert key is not None
        monkeypatch.setattr(
            "cw.codex_review._context.core._load_finding_dispositions",
            lambda *_a, **_kw: (
                {
                    key: FindingDisposition(
                        outcome="REJECTED",
                        rationale="settled in an earlier round",
                        recorded_at="2026-08-16T00:00:00Z",
                        actor="mattwwarren",
                        # #2232: the sha this pass reviews. These cases are
                        # about the claim tier's gate, not drift, so the
                        # record says the code has not moved and the drift
                        # check short-circuits without shelling out.
                        reviewed_sha=git_in(repo, "rev-parse", "HEAD"),
                        summary=CLAIM_ROW1_RECORDED,
                    )
                },
                [],
            ),
        )
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id=session_id
        )
        results = [_ok_result() for _ in prepared.roles]
        results[0] = _ok_result(
            findings=[
                _finding_payload(
                    severity="MUST_FIX",
                    file="mod.py",
                    line_start=1,
                    line_end=1,
                    summary=CLAIM_ROW1_CANDIDATE,
                )
            ]
        )
        return run_review(
            runner=_SequencedRunner(results),
            task=_task(),
            worktree=repo,
            default_branch="main",
            model=None,
            reasoning_effort=None,
            wall_clock_budget_seconds=None,
            session_id=session_id,
            fix_loop_enabled=False,
            claim_tier_enabled=claim_tier_enabled,
            disposition_drift_check_enabled=disposition_drift_check_enabled,
        )

    def _feature_repo(self, make_git_repo: Callable[[str], Path], name: str) -> Path:
        repo = make_git_repo(name)
        git_in(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n", encoding="utf-8")
        git_in(repo, "add", "mod.py")
        git_in(repo, "commit", "-m", "add mod.py")
        return repo

    def test_run_review_threads_claim_tier_gate_into_synthesis(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._feature_repo(make_git_repo, "wt-claim-tier-on")
        result, verdict = self._reworded_run(
            repo,
            monkeypatch,
            session_id="sess-claim-on",
            claim_tier_enabled=True,
        )

        assert result.status == "stage_complete"
        assert verdict is not None
        assert verdict.blocking is False
        assert [af.disposition for af in verdict.accepted] == ["rejected"]

    def test_run_review_gate_off_keeps_the_reworded_finding_blocking(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._feature_repo(make_git_repo, "wt-claim-tier-off")
        result, verdict = self._reworded_run(
            repo, monkeypatch, session_id="sess-claim-off"
        )

        assert result.status == "blocked"
        assert verdict is not None
        assert verdict.blocking is True
        shadows = read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_CLAIM_SHADOWED]
        )
        assert len(shadows) == 1

    @pytest.mark.parametrize("drift_check_enabled", [True, False])
    def test_run_review_threads_disposition_drift_check_gate_into_synthesis(
        self,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
        drift_check_enabled: bool,
    ) -> None:
        """#2232: cycle 0's hop carries the gate to the ledger backstop."""
        repo = self._feature_repo(make_git_repo, f"wt-drift-{drift_check_enabled}")
        seen: list[object] = []
        real_synth = codex_review_core.synthesize_codex_review_result

        def _spy(**kwargs: object) -> object:
            seen.append(kwargs.get("disposition_drift_check_enabled"))
            return real_synth(**kwargs)

        monkeypatch.setattr(codex_review_core, "synthesize_codex_review_result", _spy)
        self._reworded_run(
            repo,
            monkeypatch,
            session_id=f"sess-drift-{drift_check_enabled}",
            disposition_drift_check_enabled=drift_check_enabled,
        )

        assert seen == [drift_check_enabled]


# ---------------------------------------------------------------------------
# run_review threads codex audit metrics onto the verdict (#1710)
# ---------------------------------------------------------------------------


_AUDIT_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "codex_audit_events"
    / "clean_with_command.jsonl"
)


def test_run_review_threads_metrics_onto_verdict_agents_run(
    make_git_repo: Callable[[str], Path],
) -> None:
    repo = make_git_repo("wt-run-review-metrics")
    git_in(repo, "checkout", "-b", "feature")
    (repo / "mod.py").write_text("def broken():\n", encoding="utf-8")
    git_in(repo, "add", "mod.py")
    git_in(repo, "commit", "-m", "add mod.py")

    # Warms the #1709 capability cache; run_review below is then a cache hit.
    prepared = _prepare_review_pass(
        _task(),
        repo,
        "main",
        runner=FakeCodexRunner(),
        session_id="s-run-review-metrics",
    )
    jsonl = _AUDIT_FIXTURE.read_text(encoding="utf-8")
    runner = _SequencedRunner([_ok_result(stdout=jsonl) for _ in prepared.roles])
    _result, verdict = run_review(
        runner=runner,
        task=_task(),
        worktree=repo,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="sess-run-review-metrics",
        fix_loop_enabled=False,
    )
    assert verdict is not None
    assert verdict.agents_run
    for record in verdict.agents_run:
        assert record.thread_id == "<THREAD_ID>"
        assert record.terminal_event == "turn.completed"
        assert record.input_tokens == 26617
        assert record.had_command_evidence is True
        assert record.duration_seconds is not None


# ---------------------------------------------------------------------------
# run_review forwards the ledger's settled suppression (#2816)
# ---------------------------------------------------------------------------


def _suppression_repo(make_git_repo: Callable[[str], Path], name: str) -> Path:
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", "feature")
    (repo / "mod.py").write_text("def broken():\n", encoding="utf-8")
    git_in(repo, "add", "mod.py")
    git_in(repo, "commit", "-m", "add mod.py")
    return repo


def _run_review_with(
    repo: Path,
    session_id: str,
    settled_suppression: Callable[[ReviewVerdict], ReviewVerdict] | None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    prepared = _prepare_review_pass(
        _task(), repo, "main", runner=FakeCodexRunner(), session_id=session_id
    )
    runner = _SequencedRunner([_ok_result() for _ in prepared.roles])
    return run_review(
        runner=runner,
        task=_task(),
        worktree=repo,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id=session_id,
        fix_loop_enabled=False,
        settled_suppression=settled_suppression,
    )


def test_run_review_forwards_no_suppression_by_default(
    make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _suppression_repo(make_git_repo, "wt-suppress-default")
    seen: list[object] = []
    real_synth = codex_review_core.synthesize_codex_review_result

    def _spy(**kwargs: object) -> object:
        seen.append(kwargs.get("settled_suppression", "absent"))
        return real_synth(**kwargs)

    monkeypatch.setattr(codex_review_core, "synthesize_codex_review_result", _spy)
    prepared = _prepare_review_pass(
        _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-sup-default"
    )
    run_review(
        runner=_SequencedRunner([_ok_result() for _ in prepared.roles]),
        task=_task(),
        worktree=repo,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="s-sup-default",
        fix_loop_enabled=False,
    )

    assert seen == [None]


def test_run_review_forwards_and_applies_the_settled_suppression(
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2816: cycle 0 applies the ledger's ``suppress_settled`` seam."""
    repo = _suppression_repo(make_git_repo, "wt-suppress-given")
    suppressed: list[str] = []

    def _suppress(verdict: ReviewVerdict) -> ReviewVerdict:
        suppressed.append(verdict.reviewed_sha)
        return verdict

    _result, verdict = _run_review_with(repo, "s-sup-given", _suppress)

    assert verdict is not None
    assert suppressed == [git_in(repo, "rev-parse", "HEAD")]


# ---------------------------------------------------------------------------
# run_review shards a diff over max_prompt_chars (#2864)
# ---------------------------------------------------------------------------

_SHARD_FILES = ("pkg_a/a1.py", "pkg_a/a2.py", "pkg_b/b1.py", "pkg_c/c1.py")
_CHANGED_FILES_HEADER = "## Changed Files\n"


def _stem_content(relpath: str, lines: int = 20) -> str:
    stem = Path(relpath).stem
    return "".join(f"{stem}_{n} = {n}\n" for n in range(lines))


def _large_diff_repo(make_git_repo: Callable[[str], Path], name: str) -> Path:
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", "feature")
    for relpath in _SHARD_FILES:
        commit_tracked_file(repo, relpath, _stem_content(relpath))
    return repo


class _Sharded(NamedTuple):
    prepared: _ReviewPassInputs
    limit: int
    plans: list[_ShardPlan]


def _sharded_setup(repo: Path, session_id: str) -> _Sharded:
    """Warm the capability cache and derive a limit that shards *repo*'s diff."""
    prepared = _prepare_review_pass(
        _task(), repo, "main", runner=FakeCodexRunner(), session_id=session_id
    )
    overhead, costs = _shard_limit_inputs(prepared)
    limit = overhead + 2 * max(costs.values())
    assert max(len(p) for p in prepared.prompts_by_role.values()) > limit
    assert limit >= overhead + max(costs.values())
    plans = plan_prompt_shards(prepared, limit)
    assert len(plans) > 1
    return _Sharded(prepared, limit, plans)


def _prompt_files(prompt: str) -> list[str]:
    section = prompt.split(_CHANGED_FILES_HEADER, 1)[1].split("\n\n", 1)[0]
    return section.splitlines()


def _run_limited(
    repo: Path,
    runner: _SequencedRunner,
    *,
    limit: int | None,
    session_id: str,
    budget: int | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    return run_review(
        runner=runner,
        task=_task(),
        worktree=repo,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=budget,
        session_id=session_id,
        fix_loop_enabled=False,
        max_prompt_chars=limit,
    )


def _must_fix_in(path: str) -> list[dict[str, object]]:
    stem = Path(path).stem
    return [
        _finding_payload(
            severity="MUST_FIX",
            file=path,
            line_start=1,
            line_end=1,
            evidence=f"{stem}_0 = 0",
        )
    ]


def _all_ok(roles: list[str], shards: int) -> list[CodexRunResult]:
    return [_ok_result(role) for _shard in range(shards) for role in roles]


class TestShardedRunReview:
    def test_a_diff_over_the_limit_is_reviewed_shard_by_shard(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-shard-run")
        setup = _sharded_setup(repo, "s-shard-run")
        roles, shards = setup.prepared.roles, len(setup.plans)
        runner = _SequencedRunner(_all_ok(roles, shards))

        result, verdict = _run_limited(
            repo, runner, limit=setup.limit, session_id="s-shard-run"
        )

        assert len(runner.calls) == len(roles) * shards
        prompts = [str(call["stdin"]) for call in runner.calls]
        assert all(len(prompt) <= setup.limit for prompt in prompts)
        seen = {path for prompt in prompts for path in _prompt_files(prompt)}
        assert seen == set(setup.prepared.changed_files)
        assert verdict is not None
        assert [s.index for s in verdict.shards] == list(range(1, shards + 1))
        assert all(s.reviewed for s in verdict.shards)
        assert [s.files for s in verdict.shards] == [list(p.files) for p in setup.plans]
        # All shards clean: not blocked, and one record per roster role.
        assert result.blocker is None
        assert [r.reviewer_role for r in verdict.agents_run] == roles

    def test_a_must_fix_in_the_second_shard_blocks_with_that_finding(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-shard-dirty")
        setup = _sharded_setup(repo, "s-shard-dirty")
        roles, shards = setup.prepared.roles, len(setup.plans)
        target = setup.plans[1].files[0]
        results = _all_ok(roles, shards)
        results[len(roles)] = _ok_result(roles[0], findings=_must_fix_in(target))

        result, verdict = _run_limited(
            repo, _SequencedRunner(results), limit=setup.limit, session_id="s-dirty"
        )

        assert result.blocker is not None
        assert result.blocker.reason == "codex_must_fix_findings"
        assert verdict is not None
        assert verdict.blocking is True
        assert [f.file for f in verdict.must_fix] == [target]

    def test_a_role_failure_in_one_shard_is_a_partial_review(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-shard-partial")
        setup = _sharded_setup(repo, "s-shard-partial")
        roles, shards = setup.prepared.roles, len(setup.plans)
        results = _all_ok(roles, shards)
        results[len(roles)] = CodexRunResult(returncode=1, stdout="", stderr="boom")

        result, verdict = _run_limited(
            repo, _SequencedRunner(results), limit=setup.limit, session_id="s-partial"
        )

        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_PARTIAL
        assert f"{roles[0]} (codex_error) shard 2/{shards}" in result.blocker.details
        assert verdict is not None
        assert verdict.shards[1].reviewed is False

    def test_a_failed_role_beside_a_must_fix_repeats_in_agents_run_only(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Phase 1 item 3b: the accepted duplicate on a failure path."""
        repo = _large_diff_repo(make_git_repo, "wt-shard-dup")
        setup = _sharded_setup(repo, "s-shard-dup")
        roles, shards = setup.prepared.roles, len(setup.plans)
        results = _all_ok(roles, shards)
        results[0] = _ok_result(
            roles[0], findings=_must_fix_in(setup.plans[0].files[0])
        )
        results[len(roles)] = CodexRunResult(returncode=1, stdout="", stderr="boom")

        result, verdict = _run_limited(
            repo, _SequencedRunner(results), limit=setup.limit, session_id="s-dup"
        )

        assert result.blocker is not None
        assert result.blocker.reason == "codex_must_fix_findings"
        assert verdict is not None
        statuses = [r.status for r in verdict.agents_run if r.reviewer_role == roles[0]]
        assert sorted(statuses) == ["failed", "ok"]
        assert unread_from_verdict(verdict).failed_roles == (roles[0],)
        assert verdict.shards[1].reviewed is False
        assert roles[0] not in verdict.shards[1].roles_reviewed

    def test_a_shared_budget_is_split_across_the_shards(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-shard-budget")
        setup = _sharded_setup(repo, "s-shard-budget")
        roles, shards = setup.prepared.roles, len(setup.plans)
        runner = _SequencedRunner(_all_ok(roles, shards))

        _result, verdict = _run_limited(
            repo, runner, limit=setup.limit, session_id="s-budget", budget=3600
        )

        assert verdict is not None
        assert all(s.reviewed for s in verdict.shards)
        timeouts = [call["timeout"] for call in runner.calls]
        assert all(isinstance(t, int) and 0 < t <= 3600 for t in timeouts)

    def test_one_file_over_the_limit_fails_before_any_codex_call(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-shard-huge")
        commit_tracked_file(repo, "huge/blob.py", _stem_content("huge/blob.py", 400))
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-huge"
        )
        overhead, costs = _shard_limit_inputs(prepared)
        small = max(cost for path, cost in costs.items() if path != "huge/blob.py")
        limit = overhead + small + 1
        assert costs["huge/blob.py"] > limit - overhead
        runner = _SequencedRunner([])

        with pytest.raises(ReviewInputTooLargeError) as caught:
            _run_limited(repo, runner, limit=limit, session_id="s-huge")

        assert caught.value.cause == "file_too_large"
        assert caught.value.file == "huge/blob.py"
        assert runner.calls == []

    def test_without_a_limit_the_prompts_are_unchanged_and_never_planned(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-shard-none")
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-none"
        )
        planned: list[object] = []
        monkeypatch.setattr(
            codex_review_core, "plan_prompt_shards", lambda *a: planned.append(a)
        )
        runner = _SequencedRunner([_ok_result(role) for role in prepared.roles])

        _result, verdict = _run_limited(repo, runner, limit=None, session_id="s-none")

        assert [call["stdin"] for call in runner.calls] == [
            prepared.prompts_by_role[role] for role in prepared.roles
        ]
        assert verdict is not None
        assert verdict.shards == []
        assert planned == []

    def test_a_limit_the_diff_fits_takes_the_unsharded_path(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-shard-fits")
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-fits"
        )
        runner = _SequencedRunner([_ok_result(role) for role in prepared.roles])

        _result, verdict = _run_limited(
            repo, runner, limit=CODEX_MAX_PROMPT_CHARS, session_id="s-fits"
        )

        assert [call["stdin"] for call in runner.calls] == [
            prepared.prompts_by_role[role] for role in prepared.roles
        ]
        assert verdict is not None
        assert verdict.shards == []
