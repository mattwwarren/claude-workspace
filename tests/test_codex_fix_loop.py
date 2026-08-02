"""Tests for cw.codex_fix_loop — the bounded codex fix-loop adapter (#1392)."""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop import (
    _CYCLE0_SNAPSHOT_FILENAME,
    _ESCALATE_AT_CYCLE,
    _FIX_CYCLE_FLOOR_SECONDS,
    _MAX_FIX_CYCLES,
    _build_fix_codex_argv,
    _build_fix_prompt,
    _track_open_findings,
    run_review_with_fix_loop,
)
from cw.codex_review import (
    _MIN_ROLE_TIMEOUT_SECONDS,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_FIX_SCOPE_VIOLATION,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_REVIEW_UNPARSEABLE,
    run_review,
    synthesize_codex_review_result,
)
from cw.codex_runner import CodexRunResult
from cw.executor_diagnostics import diagnostics_bundle_dir
from cw.local_runner import make_blocked
from cw.models import Stage, TicketTask
from cw.review_findings import (
    AcceptedFinding,
    ReviewVerdict,
    _dedup_key,
    consolidate_verdict,
)
from tests._codex_review_helpers import _Clock, _SequencedRunner, _write
from tests.conftest import (
    _make_diff,
    _make_finding,
    _make_reviewer_doc,
    _make_ticket_task,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_runner import CodexRunner
    from cw.review_findings import ReviewerFindingsDocument

    _FixBehavior = CodexRunResult | Callable[[Path, list[str]], CodexRunResult]

# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------

_CONTENT = "def broken():\n    return 1\n"


def _git(repo: Path, *args: str) -> None:
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, env=clean_env
    )


def _worktree(
    make_git_repo: Callable[..., Path],
    name: str,
    *,
    content: str = _CONTENT,
    manifest: dict[str, str] | None = None,
    feature_files: dict[str, str] | None = None,
) -> Path:
    repo = make_git_repo(name)
    if manifest is not None:
        for rel_path, text in manifest.items():
            _write(repo / rel_path, text)
        _git(repo, "add", *manifest.keys())
        _git(repo, "commit", "-m", "add manifest")
    _git(repo, "checkout", "-b", "feature")
    files = feature_files if feature_files is not None else {"new.py": content}
    for rel_path, text in files.items():
        _write(repo / rel_path, text)
    _git(repo, "add", *files.keys())
    _git(repo, "commit", "-m", "add new.py")
    return repo


def _head(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()


def _task(*, scope_hint: str | None = None) -> TicketTask:
    return _make_ticket_task(
        ticket_id="T-1", client="test", stage=Stage.REVIEW, scope_hint=scope_hint
    )


def _finding_dict(
    *, severity: str, line: int, evidence: str, summary: str
) -> dict[str, object]:
    return {
        "severity": severity,
        "file": "new.py",
        "line_start": line,
        "line_end": line,
        "summary": summary,
        "consequence": "it breaks",
        "suggested_fix": "fix it",
        "evidence": evidence,
        "confidence": "HIGH",
    }


_MF_A = _finding_dict(
    severity="MUST_FIX", line=1, evidence="def broken():", summary="MFA"
)
_MF_B = _finding_dict(severity="MUST_FIX", line=2, evidence="return 1", summary="MFB")
_SF = _finding_dict(
    severity="SHOULD_FIX", line=1, evidence="def broken():", summary="SFN"
)


def _doc(findings: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "reviewer_role": "Code Quality Reviewer",
            "status": "ok",
            "detail": "reviewed; no issues found.",
            "findings": findings,
        }
    )


_MF_DOC = _doc([_MF_A])
_MF_AB_DOC = _doc([_MF_A, _MF_B])
_MF_SF_DOC = _doc([_MF_A, _SF])
_SF_DOC = _doc([_SF])
_CLEAN_DOC = _doc([])


class _FixLoopRunner:
    """CodexRunner double for the fix loop.

    Review calls (argv carries ``read-only``/``-o``) return the review doc for
    the current pass; the pass index advances after each fix invocation. Fix
    calls (argv carries ``workspace-write``) run the queued behavior — a
    ``CodexRunResult``, or a ``(worktree, argv) -> CodexRunResult`` callable that
    may mutate the worktree — defaulting to a no-op success.
    """

    def __init__(
        self,
        review_docs: list[str],
        fix_behaviors: list[_FixBehavior] | None = None,
    ) -> None:
        self._review_docs = review_docs
        self._fix_behaviors = list(fix_behaviors or [])
        self._pass = 0
        self._fix_i = 0
        self.calls: list[dict[str, object]] = []
        self.review_calls = 0
        self.fix_calls = 0

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        self.calls.append(
            {"argv": list(argv), "timeout": timeout_seconds, "stdin": stdin}
        )
        if "workspace-write" in argv:
            self.fix_calls += 1
            behavior = (
                self._fix_behaviors[self._fix_i]
                if self._fix_i < len(self._fix_behaviors)
                else None
            )
            self._fix_i += 1
            self._pass += 1
            if callable(behavior):
                return behavior(worktree, argv)
            if isinstance(behavior, CodexRunResult):
                return behavior
            return CodexRunResult(returncode=0, stdout="", stderr="")
        self.review_calls += 1
        doc = self._review_docs[min(self._pass, len(self._review_docs) - 1)]
        return CodexRunResult(
            returncode=0, stdout="", stderr="", output_file_content=doc
        )


def _run_loop(
    runner: CodexRunner,
    worktree: Path,
    *,
    budget: int | None = None,
    session_id: str = "sess-fix",
    fix_loop_enabled: bool = True,
    task: TicketTask | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    return run_review_with_fix_loop(
        runner=runner,
        task=task if task is not None else _task(),
        worktree=worktree,
        default_branch="main",
        model=None,
        wall_clock_budget_seconds=budget,
        session_id=session_id,
        fix_loop_enabled=fix_loop_enabled,
    )


def _editor(
    filename: str = "fix.py", content: str = "patched = 1\n"
) -> Callable[[Path, list[str]], CodexRunResult]:
    def _edit(worktree: Path, _argv: list[str]) -> CodexRunResult:
        _write(worktree / filename, content)
        return CodexRunResult(returncode=0, stdout="", stderr="")

    return _edit


def _renamer(old: str, new: str) -> Callable[[Path, list[str]], CodexRunResult]:
    """Fix-behavior callable that ``git mv``s *old* to *new* in the worktree.

    Mirrors ``_editor``'s shape (a ``(worktree, argv) -> CodexRunResult``
    callable for ``_FixLoopRunner``'s ``fix_behaviors`` list) but exercises the
    rename-aware branch of ``_porcelain_changed_paths`` instead of a plain
    add/modify.
    """

    def _rename(worktree: Path, _argv: list[str]) -> CodexRunResult:
        (worktree / new).parent.mkdir(parents=True, exist_ok=True)
        _git(worktree, "mv", old, new)
        return CodexRunResult(returncode=0, stdout="", stderr="")

    return _rename


# ---------------------------------------------------------------------------
# TestFixCycleFloor
# ---------------------------------------------------------------------------


def _blocking_stub(
    worktree: Path,
) -> tuple[AutoDevResult, ReviewVerdict]:
    diff = _make_diff("def broken():", files={"new.py": [1]})
    doc = _make_reviewer_mf_doc()
    verdict = consolidate_verdict([doc], diff, reviewed_sha="sha0")
    result = make_blocked(
        ticket_id="T-1",
        worktree=worktree,
        reason=CODEX_MUST_FIX_FINDINGS,
        stage_reached="stage3_review",
    )
    return result, verdict


def _make_reviewer_mf_doc() -> ReviewerFindingsDocument:
    return _make_reviewer_doc(
        _make_finding(
            severity="MUST_FIX",
            file="new.py",
            line_start=1,
            line_end=1,
            evidence="def broken():",
        )
    )


class TestFixCycleFloor:
    def test_floor_parks_before_invoking_fix(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-floor")
        result0, verdict = _blocking_stub(worktree)
        monkeypatch.setattr(
            "cw.codex_fix_loop.run_review", lambda **_k: (result0, verdict)
        )
        # deadline = 0 + 1000; remaining at loop = 1000 - 950 = 50 < 60.
        monkeypatch.setattr("cw.codex_fix_loop.time.monotonic", _Clock([0.0, 950.0]))
        runner = _SequencedRunner([])
        out, out_verdict = _run_loop(
            runner, worktree, budget=1000, session_id="s-floor"
        )

        assert len(runner.calls) == 0  # fix invocation never reached
        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_BUDGET_EXHAUSTED
        assert out.blocker.retry_eligible is True
        assert out_verdict is not None
        assert out_verdict.blocking is True

    def test_unlimited_budget_never_floors(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-floor-none")
        result0, verdict = _blocking_stub(worktree)
        monkeypatch.setattr(
            "cw.codex_fix_loop.run_review", lambda **_k: (result0, verdict)
        )
        clean_result, clean_verdict = _stage_complete(worktree)
        monkeypatch.setattr(
            "cw.codex_fix_loop._rereview", lambda **_k: (clean_result, clean_verdict)
        )
        runner = _SequencedRunner([CodexRunResult(returncode=0, stdout="", stderr="")])
        out, _ = _run_loop(runner, worktree, budget=None, session_id="s-floor-none")

        # The fix invocation ran (floor never blocked it) and the loop cleared.
        assert len(runner.calls) == 1
        assert "workspace-write" in runner.calls[0]["argv"]  # type: ignore[operator]
        assert out.status == "stage_complete"

    def test_floor_constant_is_two_role_floors(self) -> None:
        assert _FIX_CYCLE_FLOOR_SECONDS == 2 * _MIN_ROLE_TIMEOUT_SECONDS == 60

    def test_floor_uses_strict_less_than(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # remaining == floor exactly must NOT park (uses <, not <=).
        worktree = _worktree(make_git_repo, "wt-floor-eq")
        result0, verdict = _blocking_stub(worktree)
        monkeypatch.setattr(
            "cw.codex_fix_loop.run_review", lambda **_k: (result0, verdict)
        )
        clean_result, clean_verdict = _stage_complete(worktree)
        monkeypatch.setattr(
            "cw.codex_fix_loop._rereview", lambda **_k: (clean_result, clean_verdict)
        )
        # deadline 1000; remaining = 1000 - 940 = 60 == floor → not floored.
        monkeypatch.setattr("cw.codex_fix_loop.time.monotonic", _Clock([0.0, 940.0]))
        runner = _SequencedRunner([CodexRunResult(returncode=0, stdout="", stderr="")])
        out, _ = _run_loop(runner, worktree, budget=1000, session_id="s-floor-eq")

        assert len(runner.calls) == 1  # fix invocation reached
        assert out.status == "stage_complete"


def _stage_complete(worktree: Path) -> tuple[AutoDevResult, ReviewVerdict]:
    diff = _make_diff("def broken():", files={"new.py": [1]})
    doc = _make_reviewer_doc(reviewer_role="Code Quality Reviewer")
    result, verdict = synthesize_codex_review_result(
        task=_task(),
        worktree=worktree,
        documents=[doc],
        failures=[],
        diff=diff,
        reviewed_sha="sha-clean",
        session_id="s-stage-complete",
    )
    assert verdict is not None
    return result, verdict


# ---------------------------------------------------------------------------
# TestFixInvocation
# ---------------------------------------------------------------------------


class TestFixInvocation:
    def test_fix_argv_is_workspace_write_not_read_only(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-argv")
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC])
        _run_loop(runner, worktree, session_id="s-argv")

        fix_calls = [c for c in runner.calls if "workspace-write" in c["argv"]]  # type: ignore[operator]
        assert fix_calls
        for call in fix_calls:
            assert "read-only" not in call["argv"]  # type: ignore[operator]
            assert "-o" not in call["argv"]  # type: ignore[operator]

    def test_fix_prompt_excludes_should_fix(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-prompt")
        runner = _FixLoopRunner([_MF_SF_DOC, _CLEAN_DOC])
        _run_loop(runner, worktree, session_id="s-prompt")

        fix_call = next(c for c in runner.calls if "workspace-write" in c["argv"])  # type: ignore[operator]
        prompt = fix_call["stdin"]
        assert isinstance(prompt, str)
        assert "MFA" in prompt  # the MUST_FIX finding's summary
        assert "SFN" not in prompt  # the SHOULD_FIX finding's summary excluded

    def test_fix_timeout_parks_retry_eligible_with_bundle(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-fix-timeout")
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[
                CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
            ],
        )
        out, _ = _run_loop(runner, worktree, session_id="s-fix-timeout")

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == "codex_timeout"
        assert out.blocker.retry_eligible is True
        bundle = diagnostics_bundle_dir("s-fix-timeout")
        assert list(bundle.glob("fix-cycle-1-timeout-*.json"))

    def test_fix_error_parks_for_operator(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-fix-error")
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[CodexRunResult(returncode=1, stdout="", stderr="boom")],
        )
        out, _ = _run_loop(runner, worktree, session_id="s-fix-error")

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == "codex_error"
        # A hard error is not transient → not retry-eligible.
        assert out.blocker.retry_eligible is None
        bundle = diagnostics_bundle_dir("s-fix-error")
        assert list(bundle.glob("fix-cycle-1-nonzero_exit-*.json"))

    def test_fix_failure_persists_cycle0_snapshot(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-fix-error-snapshot")
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[CodexRunResult(returncode=1, stdout="", stderr="boom")],
        )
        out, _ = _run_loop(runner, worktree, session_id="s-fix-error-snapshot")

        assert out.status == "blocked"
        bundle = diagnostics_bundle_dir("s-fix-error-snapshot")
        assert (bundle / _CYCLE0_SNAPSHOT_FILENAME).exists()
        assert any("[diagnostics:" in h for h in out.friction_highlights)

    def test_successful_fix_with_change_commits_with_conventional_message(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-fix-commit")
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC], fix_behaviors=[_editor()])
        _run_loop(runner, worktree, session_id="s-fix-commit")

        log = subprocess.check_output(
            ["git", "-C", str(worktree), "log", "--format=%s"], text=True
        )
        import re

        commits = [line for line in log.splitlines() if line.startswith("fix(review):")]
        assert len(commits) == 1
        assert re.match(r"^fix\(review\): codex fix cycle \d+ — .+$", commits[0])

    def test_noop_fix_does_not_commit_but_still_counts(
        self,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        worktree = _worktree(make_git_repo, "wt-fix-noop")
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC])  # no-op fix, then clean
        with caplog.at_level(logging.WARNING, logger="cw.codex_fix_loop"):
            out, _ = _run_loop(runner, worktree, session_id="s-fix-noop")

        log = subprocess.check_output(
            ["git", "-C", str(worktree), "log", "--format=%s"], text=True
        )
        assert not any(line.startswith("fix(review):") for line in log.splitlines())
        assert any("produced no changes" in r.message for r in caplog.records)
        # The no-op cycle still counted: the loop reached a clean re-review at 1.
        assert out.status == "stage_complete"
        assert out.review.fix_cycles_used == 1

    def test_commit_exception_treated_as_fix_failure(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-fix-commit-exc")

        def _boom(*_a: object, **_k: object) -> str | None:
            raise subprocess.CalledProcessError(1, ["git", "commit"], stderr="nope")

        monkeypatch.setattr("cw.codex_fix_loop._commit_fix_cycle", _boom)
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor()])
        out, _ = _run_loop(runner, worktree, session_id="s-fix-commit-exc")

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == "codex_error"  # runtime_error → codex_error
        bundle = diagnostics_bundle_dir("s-fix-commit-exc")
        assert list(bundle.glob("fix-cycle-1-runtime_error-*.json"))


# ---------------------------------------------------------------------------
# TestFixLoopDispositionTracking
# ---------------------------------------------------------------------------


def _accepted(finding_dict: dict[str, object]) -> AcceptedFinding:
    return AcceptedFinding(
        finding=_make_finding(**finding_dict), reviewers=["Code Quality Reviewer"]
    )


class TestFixLoopDispositionTracking:
    def test_finding_absent_next_cycle_is_dropped(self) -> None:
        af = _accepted(_MF_A)
        open_findings = {_dedup_key(af.finding): af}
        # Next cycle's accepted set no longer contains the finding → dropped.
        updated = _track_open_findings(open_findings, [])
        assert updated == {}

    def test_finding_first_seen_later_cycle_is_tracked(self) -> None:
        af = _accepted(_MF_B)
        updated = _track_open_findings({}, [af])
        assert _dedup_key(af.finding) in updated

    def test_should_fix_never_enters_tracker(self) -> None:
        sf = _accepted(_SF)
        updated = _track_open_findings({}, [sf])
        assert updated == {}


# ---------------------------------------------------------------------------
# TestMustFixInitialSnapshot
# ---------------------------------------------------------------------------


class TestMustFixInitialSnapshot:
    def test_new_finding_in_later_cycle_does_not_inflate_initial(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-snap-new")
        # cycle 0 sees only MF_A; from the 4th review pass a new MF_B appears.
        runner = _FixLoopRunner(
            [_MF_DOC, _MF_DOC, _MF_DOC, _MF_AB_DOC, _MF_AB_DOC, _MF_AB_DOC]
        )
        out, _ = _run_loop(runner, worktree, session_id="s-snap-new")

        assert out.status == "blocked"
        # must_fix_initial is cycle 0's snapshot (1), even though deferred grew.
        assert out.review.must_fix_initial == 1
        assert out.review.deferred == 2
        assert out.review.fix_cycles_used == _MAX_FIX_CYCLES

    def test_cycle0_survivor_counted_in_both_initial_and_deferred(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-snap-survive")
        runner = _FixLoopRunner([_MF_DOC])  # MF_A never clears
        out, _ = _run_loop(runner, worktree, session_id="s-snap-survive")

        assert out.review.must_fix_initial == 1
        assert out.review.deferred == 1


# ---------------------------------------------------------------------------
# TestDeferredCumulativeTracking
# ---------------------------------------------------------------------------


class TestDeferredCumulativeTracking:
    def test_deferred_counts_distinct_findings_from_any_cycle(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-defer-distinct")
        # MF_A open throughout; MF_B arrives from pass 3.
        runner = _FixLoopRunner(
            [_MF_DOC, _MF_DOC, _MF_DOC, _MF_AB_DOC, _MF_AB_DOC, _MF_AB_DOC]
        )
        out, _ = _run_loop(runner, worktree, session_id="s-defer-distinct")
        assert out.review.deferred == 2

    def test_flapping_finding_counted_once_at_cap(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-defer-flap")
        # MF_B present@0, gone@1-3, back@4-5. MF_A keeps the loop alive.
        runner = _FixLoopRunner(
            [_MF_AB_DOC, _MF_DOC, _MF_DOC, _MF_DOC, _MF_AB_DOC, _MF_AB_DOC]
        )
        out, _ = _run_loop(runner, worktree, session_id="s-defer-flap")
        # Both A and B open at cap, B counted exactly once despite flapping.
        assert out.review.deferred == 2


# ---------------------------------------------------------------------------
# TestFixLoopBlockingIndependentOfDisposition
# ---------------------------------------------------------------------------


class TestFixLoopBlockingIndependentOfDisposition:
    def test_capped_verdict_blocking_despite_deferred_stamp(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-blocking")
        runner = _FixLoopRunner([_MF_DOC])
        out, verdict = _run_loop(runner, worktree, session_id="s-blocking")

        assert out.status == "blocked"
        assert verdict is not None
        # Survivors are stamped disposition="deferred" for reporting.
        mf_accepted = [
            af for af in verdict.accepted if af.finding.severity == "MUST_FIX"
        ]
        assert mf_accepted
        assert all(af.disposition == "deferred" for af in mf_accepted)
        # A NAIVE re-derivation off the disposition-stamped list reads False...
        naive_blocking = bool(
            [
                af
                for af in verdict.accepted
                if af.finding.severity == "MUST_FIX" and af.disposition != "deferred"
            ]
        )
        assert naive_blocking is False
        # ...but the authoritative, open_findings-derived verdict is blocking.
        assert verdict.blocking is True
        assert verdict.must_fix


# ---------------------------------------------------------------------------
# TestFixLoopCapAndEscalation
# ---------------------------------------------------------------------------


class TestFixLoopCapAndEscalation:
    def test_cap_parks_with_full_cycle_count_and_verdict_details(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-cap")
        runner = _FixLoopRunner([_MF_DOC])
        out, _verdict = _run_loop(runner, worktree, session_id="s-cap")

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert out.review.fix_cycles_used == _MAX_FIX_CYCLES
        assert out.health.fix_loop_escalated is True
        assert runner.fix_calls == _MAX_FIX_CYCLES
        # details render the survivor verdict comment.
        assert "BLOCKING" in out.blocker.details

    def test_clean_exit_cycle_two_not_escalated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-clean2")
        runner = _FixLoopRunner([_MF_DOC, _MF_DOC, _CLEAN_DOC])
        out, _verdict = _run_loop(runner, worktree, session_id="s-clean2")

        assert out.status == "stage_complete"
        assert out.blocker is None
        assert out.review.fix_cycles_used == 2
        assert out.health.fix_loop_escalated is False
        assert runner.fix_calls == 2

    def test_clean_exit_cycle_three_is_escalated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-clean3")
        runner = _FixLoopRunner([_MF_DOC, _MF_DOC, _MF_DOC, _CLEAN_DOC])
        out, _ = _run_loop(runner, worktree, session_id="s-clean3")

        assert out.status == "stage_complete"
        assert out.review.fix_cycles_used == 3
        assert out.health.fix_loop_escalated is True
        assert _ESCALATE_AT_CYCLE == 3

    def test_capped_run_review_counts_snapshot(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-cap-counts")
        runner = _FixLoopRunner([_MF_DOC])
        out, _ = _run_loop(runner, worktree, session_id="s-cap-counts")
        assert out.review.must_fix_initial == 1
        assert out.review.deferred == 1
        assert out.review.fix_cycles_used == 5

    def test_clean_exit_persists_cycle0_snapshot(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-clean2-snapshot")
        runner = _FixLoopRunner([_MF_DOC, _MF_DOC, _CLEAN_DOC])
        out, _verdict = _run_loop(runner, worktree, session_id="s-clean2-snapshot")

        assert out.status == "stage_complete"
        bundle = diagnostics_bundle_dir("s-clean2-snapshot")
        assert (bundle / _CYCLE0_SNAPSHOT_FILENAME).exists()
        assert any("[diagnostics:" in h for h in out.friction_highlights)

    def test_capped_run_persists_cycle0_snapshot(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-cap-snapshot")
        runner = _FixLoopRunner([_MF_DOC])
        out, _verdict = _run_loop(runner, worktree, session_id="s-cap-snapshot")

        assert out.status == "blocked"
        bundle = diagnostics_bundle_dir("s-cap-snapshot")
        assert (bundle / _CYCLE0_SNAPSHOT_FILENAME).exists()
        assert any("[diagnostics:" in h for h in out.friction_highlights)

    def test_cycle0_snapshot_content_matches_original_findings_after_fix(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-snapshot-content")
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC], fix_behaviors=[_editor()])
        out, verdict = _run_loop(runner, worktree, session_id="s-snapshot-content")

        assert out.status == "stage_complete"
        assert verdict is not None
        assert not verdict.must_fix  # terminal state cleared the finding

        bundle = diagnostics_bundle_dir("s-snapshot-content")
        snapshot_path = bundle / _CYCLE0_SNAPSHOT_FILENAME
        snapshot = ReviewVerdict.model_validate_json(snapshot_path.read_text())
        assert any(f.summary == "MFA" for f in snapshot.must_fix)
        assert any(af.finding.summary == "MFA" for af in snapshot.accepted)

    def test_unparseable_rereview_persists_cycle0_snapshot(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-rereview-fail-snapshot")
        runner = _FixLoopRunner([_MF_DOC, "not json{{"])
        out, verdict = _run_loop(
            runner, worktree, session_id="s-rereview-fail-snapshot"
        )

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_REVIEW_UNPARSEABLE
        assert verdict is None
        bundle = diagnostics_bundle_dir("s-rereview-fail-snapshot")
        assert (bundle / _CYCLE0_SNAPSHOT_FILENAME).exists()
        assert any("[diagnostics:" in h for h in out.friction_highlights)


# ---------------------------------------------------------------------------
# TestFixLoopReviewParity
# ---------------------------------------------------------------------------


class TestFixLoopReviewParity:
    def test_reviewed_sha_advances_after_commit(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-sha-advance")
        orig = _head(worktree)
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC], fix_behaviors=[_editor()])
        _out, verdict = _run_loop(runner, worktree, session_id="s-sha-advance")

        assert verdict is not None
        assert verdict.reviewed_sha != orig
        assert verdict.reviewed_sha == _head(worktree)

    def test_reviewed_sha_unchanged_for_noop_cycle(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-sha-noop")
        orig = _head(worktree)
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC])  # no-op fix
        _out, verdict = _run_loop(runner, worktree, session_id="s-sha-noop")

        assert verdict is not None
        assert verdict.reviewed_sha == orig  # no commit landed

    def test_rereview_zero_documents_parks_unparseable(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        # A re-review whose every role fails to parse → zero documents →
        # verdict None terminal (blocked/unparseable), loop stops.
        worktree = _worktree(make_git_repo, "wt-rereview-fail")
        runner = _FixLoopRunner([_MF_DOC, "not json{{"])
        out, verdict = _run_loop(runner, worktree, session_id="s-rereview-fail")

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_REVIEW_UNPARSEABLE
        assert verdict is None
        assert runner.fix_calls == 1  # one fix ran before the failed re-review


# ---------------------------------------------------------------------------
# TestFixLoopNonBlockingPassthrough
# ---------------------------------------------------------------------------


class TestFixLoopNonBlockingPassthrough:
    def test_non_blocking_cycle0_passthrough_no_fix(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-passthrough")
        loop_runner = _FixLoopRunner([_SF_DOC])
        loop_result, loop_verdict = _run_loop(
            loop_runner, worktree, session_id="s-pass"
        )
        plain_runner = _FixLoopRunner([_SF_DOC])
        plain_result, plain_verdict = run_review(
            runner=plain_runner,
            task=_task(),
            worktree=worktree,
            default_branch="main",
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-pass-plain",
        )
        assert loop_runner.fix_calls == 0
        assert loop_result.status == plain_result.status == "stage_complete"
        assert loop_verdict is not None
        assert plain_verdict is not None
        assert loop_verdict.blocking is plain_verdict.blocking is False
        assert loop_result.review.should_fix == plain_result.review.should_fix == 1

    def test_unparseable_cycle0_passthrough(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-unparseable")
        runner = _FixLoopRunner(["not json{{"])
        out, verdict = _run_loop(runner, worktree, session_id="s-unparse")

        assert runner.fix_calls == 0
        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_REVIEW_UNPARSEABLE
        assert verdict is None


# ---------------------------------------------------------------------------
# TestFixLoopDisabledGate
# ---------------------------------------------------------------------------


class TestFixLoopDisabledGate:
    def test_disabled_gate_blocking_cycle0_returns_run_review_tuple_unchanged(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-gate-blocking")
        head_before = _head(worktree)
        loop_runner = _FixLoopRunner([_MF_DOC])
        loop_result, loop_verdict = _run_loop(
            loop_runner, worktree, session_id="s-gate", fix_loop_enabled=False
        )
        plain_runner = _FixLoopRunner([_MF_DOC])
        plain_result, plain_verdict = run_review(
            runner=plain_runner,
            task=_task(),
            worktree=worktree,
            default_branch="main",
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-gate",
        )

        assert loop_result == plain_result
        assert loop_verdict == plain_verdict
        assert loop_result.status == "blocked"
        assert loop_result.blocker is not None
        assert loop_result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert loop_runner.fix_calls == 0
        # One review pass worth of per-role calls (however many roles
        # run_review selects) — no re-review, since no fix cycle ran.
        assert loop_runner.review_calls == plain_runner.review_calls
        assert loop_result.review.fix_cycles_used == 0
        # No commits landed — the disabled gate never invoked the fix loop.
        assert _head(worktree) == head_before

    def test_disabled_gate_non_blocking_cycle0_unaffected(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-gate-nonblocking")
        runner = _FixLoopRunner([_SF_DOC])
        result, _verdict = _run_loop(
            runner, worktree, session_id="s-gate-sf", fix_loop_enabled=False
        )

        assert runner.fix_calls == 0
        assert result.status == "stage_complete"


# ---------------------------------------------------------------------------
# TestScopeViolationGate
# ---------------------------------------------------------------------------

_MANIFEST = """\
sensitive_files:
  - path: pyproject.toml
    category: dependency
    reason: dependency changes need review
  - path: ".github/workflows/**"
    category: ci
    reason: CI changes need review
  - path: ".github/workflows/"
    category: ci
    reason: CI changes need review
"""

_PYPROJECT_CONTENT = '[project]\nname = "x"\n'


def _finding_dict_at(
    file: str, *, severity: str, line: int, evidence: str, summary: str
) -> dict[str, object]:
    return {
        "severity": severity,
        "file": file,
        "line_start": line,
        "line_end": line,
        "summary": summary,
        "consequence": "it breaks",
        "suggested_fix": "fix it",
        "evidence": evidence,
        "confidence": "HIGH",
    }


_MF_PYPROJECT_DOC = _doc(
    [
        _finding_dict_at(
            "pyproject.toml",
            severity="MUST_FIX",
            line=1,
            evidence="[project]",
            summary="MFP",
        )
    ]
)


class TestScopeViolationGate:
    """The scope-violation gate (#1464): a fix-cycle change parks only if it is

    BOTH out of the cycle-0 reviewed diff's file set AND matches the
    sensitive-files manifest.
    """

    def test_in_scope_sensitive_edit_allowed(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo,
            "wt-scope-in",
            manifest={".claude/sensitive-files.yml": _MANIFEST},
            feature_files={"pyproject.toml": _PYPROJECT_CONTENT},
        )
        runner = _FixLoopRunner(
            [_MF_PYPROJECT_DOC, _CLEAN_DOC],
            fix_behaviors=[
                _editor(filename="pyproject.toml", content='[project]\nname = "y"\n')
            ],
        )
        out, _ = _run_loop(runner, worktree, session_id="s-scope-in")

        assert out.status == "stage_complete"
        assert out.blocker is None
        assert out.review.fix_cycles_used == 1

    def test_out_of_scope_non_sensitive_addition_allowed(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-scope-nonsens")
        runner = _FixLoopRunner(
            [_MF_DOC, _CLEAN_DOC],
            fix_behaviors=[_editor(filename="extra.py", content="x = 1\n")],
        )
        out, _ = _run_loop(runner, worktree, session_id="s-scope-nonsens")

        assert out.status == "stage_complete"
        assert out.blocker is None
        assert out.review.fix_cycles_used == 1

    def test_out_of_scope_sensitive_modification_parks(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo,
            "wt-scope-outsens",
            manifest={
                ".claude/sensitive-files.yml": _MANIFEST,
                "pyproject.toml": _PYPROJECT_CONTENT,
            },
        )
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[
                _editor(filename="pyproject.toml", content='[project]\nname = "z"\n')
            ],
        )
        out, _ = _run_loop(runner, worktree, session_id="s-scope-outsens")

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_FIX_SCOPE_VIOLATION

    def test_scope_violation_persists_cycle0_snapshot(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo,
            "wt-scope-outsens-snapshot",
            manifest={
                ".claude/sensitive-files.yml": _MANIFEST,
                "pyproject.toml": _PYPROJECT_CONTENT,
            },
        )
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[
                _editor(filename="pyproject.toml", content='[project]\nname = "z"\n')
            ],
        )
        out, _ = _run_loop(runner, worktree, session_id="s-scope-outsens-snapshot")

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_FIX_SCOPE_VIOLATION
        bundle = diagnostics_bundle_dir("s-scope-outsens-snapshot")
        assert (bundle / _CYCLE0_SNAPSHOT_FILENAME).exists()
        assert any("[diagnostics:" in h for h in out.friction_highlights)

    def test_untracked_sensitive_addition_parks_small_tier(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo,
            "wt-scope-untracked-small",
            manifest={".claude/sensitive-files.yml": _MANIFEST},
        )
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[
                _editor(filename=".github/workflows/new.yml", content="name: x\n")
            ],
        )
        out, _ = _run_loop(runner, worktree, session_id="s-scope-untracked-small")

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_FIX_SCOPE_VIOLATION
        assert ".github/workflows/new.yml" in out.blocker.details

    def test_untracked_sensitive_addition_parks_large_tier(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo,
            "wt-scope-untracked-large",
            manifest={".claude/sensitive-files.yml": _MANIFEST},
        )
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[
                _editor(filename=".github/workflows/new.yml", content="name: x\n")
            ],
        )
        out, _ = _run_loop(
            runner,
            worktree,
            session_id="s-scope-untracked-large",
            task=_task(scope_hint="large"),
        )

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_FIX_SCOPE_VIOLATION
        assert ".github/workflows/new.yml" in out.blocker.details

    def test_rename_into_sensitive_path_parks(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo,
            "wt-scope-rename",
            manifest={".claude/sensitive-files.yml": _MANIFEST},
        )
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[_renamer("new.py", ".github/workflows/renamed.yml")],
        )
        out, _ = _run_loop(runner, worktree, session_id="s-scope-rename")

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_FIX_SCOPE_VIOLATION
        assert ".github/workflows/renamed.yml" in out.blocker.details

    def test_park_details_name_violating_path_and_cycle_count(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo,
            "wt-scope-details",
            manifest={
                ".claude/sensitive-files.yml": _MANIFEST,
                "pyproject.toml": _PYPROJECT_CONTENT,
            },
        )
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[
                _editor(filename="pyproject.toml", content='[project]\nname = "z"\n')
            ],
        )
        out, _ = _run_loop(runner, worktree, session_id="s-scope-details")

        assert out.status == "blocked"
        assert out.blocker is not None
        # Path is named, and both AND-gate conditions are stated explicitly.
        assert "pyproject.toml" in out.blocker.details
        assert "out of" in out.blocker.details.lower()
        assert "sensitive" in out.blocker.details.lower()
        assert out.review.fix_cycles_used == 1
        assert out.review.must_fix_initial == 1
        assert out.blocker.retry_eligible is None


# ---------------------------------------------------------------------------
# _build_fix_prompt / _build_fix_codex_argv units
# ---------------------------------------------------------------------------


class TestFixPromptAndArgv:
    def test_prompt_renders_findings_and_context(self) -> None:
        finding = _make_finding(severity="MUST_FIX", summary="the bug")
        prompt = _build_fix_prompt(
            [finding], plan_text="PLAN", ticket_text="TICKET", cycle=2
        )
        assert "Codex Fix Cycle 2" in prompt
        assert "the bug" in prompt
        assert "PLAN" in prompt
        assert "TICKET" in prompt
        assert "minimal change" in prompt

    def test_argv_omits_schema_and_output_flags(self) -> None:
        argv = _build_fix_codex_argv(model="gpt-x")
        assert argv[:4] == ["codex", "exec", "--sandbox", "workspace-write"]
        assert "--output-schema" not in argv
        assert "-o" not in argv
        assert argv[-2:] == ["-m", "gpt-x"]
