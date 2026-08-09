"""Tests for cw.codex_fix_loop — the bounded codex fix-loop adapter (#1392)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop import (
    _ESCALATE_AT_CYCLE,
    _FIX_CYCLE_FLOOR_SECONDS,
    _MAX_FIX_CYCLES,
    _build_fix_codex_argv,
    _build_fix_prompt,
    _track_open_findings,
    _verdict_snapshot_filename,
    run_review_with_fix_loop,
)
from cw.codex_review import (
    _DISABLED_FEATURES,
    _MIN_ROLE_TIMEOUT_SECONDS,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_FIX_SCOPE_VIOLATION,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_MUST_FIX_MECHANICALLY_REJECTED,
    CODEX_REVIEW_UNPARSEABLE,
    render_verdict_comment,
    run_review,
    synthesize_codex_review_result,
)
from cw.codex_review._capability import _PROBE_ARGV
from cw.codex_runner import CodexRunResult
from cw.executor_diagnostics import diagnostics_bundle_dir
from cw.local_runner import make_blocked
from cw.models import Stage, TicketTask
from cw.review_findings import (
    AcceptedFinding,
    ReviewVerdict,
    _dedup_key,
    consolidate_verdict,
    write_review_verdict,
)
from tests._codex_review_helpers import (
    _Clock,
    _config_override_values,
    _RunnerCall,
    _SequencedRunner,
    _write,
)
from tests.conftest import (
    _make_diff,
    _make_finding,
    _make_reviewer_doc,
    _make_ticket_task,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_runner import CodexRunner
    from cw.review_findings import ReviewerFindingsDocument

    _FixBehavior = CodexRunResult | Callable[[Path, list[str]], CodexRunResult] | None

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


# #1714: a MUST_FIX whose evidence is absent from the diff is rejected
# `evidence_not_in_diff` — a MECHANICAL rejection, so it never reaches
# `verdict.must_fix` and must never enter the fix loop.
_MF_BAD_EVIDENCE = _finding_dict(
    severity="MUST_FIX",
    line=1,
    evidence="this string appears nowhere in the captured diff",
    summary="MFX-mechanically-rejected",
)

_MF_DOC = _doc([_MF_A])
_MF_AB_DOC = _doc([_MF_A, _MF_B])
_MF_SF_DOC = _doc([_MF_A, _SF])
_SF_DOC = _doc([_SF])
_CLEAN_DOC = _doc([])
_MF_MECH_REJECTED_DOC = _doc([_MF_BAD_EVIDENCE])


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
        *,
        review_stdout: str = "",
    ) -> None:
        self._review_docs = review_docs
        self._fix_behaviors = list(fix_behaviors or [])
        # #1710: JSONL audit stream every review invocation emits on stdout;
        # "" (the default) reproduces the pre-#1710 no-audit-stream shape.
        self._review_stdout = review_stdout
        self._pass = 0
        self._fix_i = 0
        self.calls: list[_RunnerCall] = []
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
            returncode=0,
            stdout=self._review_stdout,
            stderr="",
            output_file_content=doc,
        )


def _run_loop(
    runner: CodexRunner,
    worktree: Path,
    *,
    budget: int | None = None,
    session_id: str = "sess-fix",
    fix_loop_enabled: bool = True,
    task: TicketTask | None = None,
    reasoning_effort: str | None = None,
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
        reasoning_effort=reasoning_effort,
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
        assert "workspace-write" in runner.calls[0]["argv"]
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
        default_branch="main",
        fix_loop_enabled=True,
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

        fix_calls = [c for c in runner.calls if "workspace-write" in c["argv"]]
        assert fix_calls
        for call in fix_calls:
            assert "read-only" not in call["argv"]
            assert "-o" not in call["argv"]

    def test_fix_prompt_excludes_should_fix(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-prompt")
        runner = _FixLoopRunner([_MF_SF_DOC, _CLEAN_DOC])
        _run_loop(runner, worktree, session_id="s-prompt")

        fix_call = next(c for c in runner.calls if "workspace-write" in c["argv"])
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
        assert (bundle / _verdict_snapshot_filename(0)).exists()
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
        out, verdict = _run_loop(runner, worktree, session_id="s-clean2")

        assert out.status == "stage_complete"
        assert out.blocker is None
        assert out.review.fix_cycles_used == 2
        assert out.health.fix_loop_escalated is False
        assert runner.fix_calls == 2
        # #1705 bug #2: the returned *verdict* (not just out.review) must
        # carry the finalized cross-cycle counts — _clean_exit previously
        # stamped review onto the AutoDevResult but returned the stale
        # cycle-terminal ReviewVerdict unchanged.
        assert verdict is not None
        assert verdict.review.must_fix_initial == 1
        assert verdict.review.fix_cycles_used == 2

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

    def test_clean_exit_persists_snapshot_for_every_cycle(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-clean2-snapshot")
        runner = _FixLoopRunner([_MF_DOC, _MF_DOC, _CLEAN_DOC])
        out, _verdict = _run_loop(runner, worktree, session_id="s-clean2-snapshot")

        assert out.status == "stage_complete"
        assert out.review.fix_cycles_used == 2
        bundle = diagnostics_bundle_dir("s-clean2-snapshot")
        # Every cycle (0 through the terminal cycle) gets its own snapshot,
        # not just cycle 0 — the pointer in friction_highlights now names the
        # latest (terminal) cycle's file, cycle 2.
        assert (bundle / _verdict_snapshot_filename(0)).exists()
        assert (bundle / _verdict_snapshot_filename(1)).exists()
        assert (bundle / _verdict_snapshot_filename(2)).exists()
        assert any("[diagnostics:" in h for h in out.friction_highlights)
        assert any(
            "cycle-2 MUST_FIX findings snapshot persisted" in h
            for h in out.friction_highlights
        )

    def test_capped_run_persists_latest_cycle_snapshot(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-cap-snapshot")
        runner = _FixLoopRunner([_MF_DOC])
        out, _verdict = _run_loop(runner, worktree, session_id="s-cap-snapshot")

        assert out.status == "blocked"
        bundle = diagnostics_bundle_dir("s-cap-snapshot")
        # A capped run exhausts every cycle up to the cap, so the terminal
        # pointer names the last (cycle _MAX_FIX_CYCLES) snapshot, not cycle 0.
        assert (bundle / _verdict_snapshot_filename(_MAX_FIX_CYCLES)).exists()
        assert any("[diagnostics:" in h for h in out.friction_highlights)
        assert any(
            f"cycle-{_MAX_FIX_CYCLES} MUST_FIX findings snapshot persisted" in h
            for h in out.friction_highlights
        )

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
        snapshot_path = bundle / _verdict_snapshot_filename(0)
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
        # The failed rereview never reaches the per-cycle persist call, so the
        # pointer still names cycle 0's snapshot — the only one written.
        assert (bundle / _verdict_snapshot_filename(0)).exists()
        assert not (bundle / _verdict_snapshot_filename(1)).exists()
        assert any("[diagnostics:" in h for h in out.friction_highlights)

    def test_cycle0_snapshot_write_failure_does_not_block_loop(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``_persist_cycle_snapshot`` never-raises: an ``OSError`` during the
        write is logged and swallowed, and the loop still returns its normal
        parked result with the pointer text in ``friction_highlights`` (the
        pointer is returned unconditionally per the plan's Adopted
        Assumption #1 — never-raise takes priority over pointer-text
        precision on this rare write-failure path)."""
        worktree = _worktree(make_git_repo, "wt-snapshot-write-fail")

        def _boom(*_a: object, **_k: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.codex_fix_loop.write_review_verdict", _boom)
        runner = _FixLoopRunner([_MF_DOC])

        import logging

        with caplog.at_level(logging.WARNING):
            out, _verdict = _run_loop(
                runner, worktree, session_id="s-snapshot-write-fail"
            )

        assert out.status == "blocked"
        assert any("[diagnostics:" in h for h in out.friction_highlights)
        assert any(
            "cycle-0 findings snapshot write failed" in r.getMessage()
            for r in caplog.records
        )

    def test_later_cycle_snapshot_write_failure_does_not_block_loop(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A write failure on a fix-cycle snapshot (not just cycle 0) is
        equally never-raising: cycle 0's write succeeds, cycle 1's is made to
        fail, and the loop still completes with cycle 0's snapshot on disk,
        no cycle-1 snapshot, and the failure logged for the correct cycle."""
        worktree = _worktree(make_git_repo, "wt-snapshot-write-fail-later")
        real_write = write_review_verdict

        def _fail_after_cycle0(verdict: ReviewVerdict, path: Path) -> None:
            if path.name == _verdict_snapshot_filename(0):
                real_write(verdict, path)
                return
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(
            "cw.codex_fix_loop.write_review_verdict", _fail_after_cycle0
        )
        runner = _FixLoopRunner([_MF_DOC])

        import logging

        with caplog.at_level(logging.WARNING):
            out, _verdict = _run_loop(
                runner, worktree, session_id="s-snapshot-write-fail-later"
            )

        assert out.status == "blocked"
        bundle = diagnostics_bundle_dir("s-snapshot-write-fail-later")
        assert (bundle / _verdict_snapshot_filename(0)).exists()
        assert not (bundle / _verdict_snapshot_filename(1)).exists()
        assert any(
            "cycle-1 findings snapshot write failed" in r.getMessage()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# TestRealCommitTracking — #1723
# ---------------------------------------------------------------------------


class TestRealCommitTracking:
    """``Review.had_real_commit`` — true iff at least one fix cycle produced a
    real commit (OR'd across cycles), so a no-op/flaked fix-loop convergence
    is positively distinguishable from a genuine fix (#1723)."""

    def test_clean_exit_with_editor_fix_marks_had_real_commit_true(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo=make_git_repo,
            name="wt-had-commit-true",
        )
        runner = _FixLoopRunner(
            review_docs=[_MF_DOC, _CLEAN_DOC],
            fix_behaviors=[_editor()],
        )
        out, verdict = _run_loop(
            runner=runner,
            worktree=worktree,
            session_id="s-had-commit-true",
        )

        assert out.status == "stage_complete"
        assert out.review.had_real_commit is True
        assert verdict is not None
        assert verdict.review.had_real_commit is True

    def test_clean_exit_with_default_noop_fix_marks_had_real_commit_false(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo=make_git_repo,
            name="wt-had-commit-false",
        )
        runner = _FixLoopRunner(
            review_docs=[_MF_DOC, _CLEAN_DOC]
        )  # no-op fix, then clean
        out, _verdict = _run_loop(
            runner=runner,
            worktree=worktree,
            session_id="s-had-commit-false",
        )

        assert out.review.had_real_commit is False
        assert out.status == "stage_complete"
        assert out.review.fix_cycles_used == 1

    def test_multi_cycle_any_real_commit_marks_true_even_if_later_cycle_is_noop(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo=make_git_repo,
            name="wt-had-commit-or",
        )
        # cycle 1: real edit; cycle 2: no-op (default fix behavior).
        runner = _FixLoopRunner(
            review_docs=[_MF_DOC, _MF_DOC, _CLEAN_DOC],
            fix_behaviors=[_editor(), None],
        )
        out, _verdict = _run_loop(
            runner=runner,
            worktree=worktree,
            session_id="s-had-commit-or",
        )

        assert out.review.had_real_commit is True

    def test_capped_park_reports_had_real_commit_false_when_every_cycle_is_noop(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(
            make_git_repo=make_git_repo,
            name="wt-had-commit-capped",
        )
        runner = _FixLoopRunner(review_docs=[_MF_DOC])
        out, _verdict = _run_loop(
            runner=runner,
            worktree=worktree,
            session_id="s-had-commit-capped",
        )

        assert out.status == "blocked"
        assert out.review.had_real_commit is False


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
            fix_loop_enabled=False,
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

    def test_mechanically_rejected_must_fix_does_not_enter_fix_loop(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#1714 R4: a mechanically-rejected MUST_FIX blocks but never autofixes.

        The finding's line/evidence anchor is by definition unreliable (that is
        *why* it was rejected), so handing it to a fix agent would ask codex to
        patch code the finding may not even describe. The fix-loop gate reads
        ``verdict.blocking``, which stays False — the park is carried by the
        separate ``rejected_must_fix`` signal instead.
        """
        worktree = _worktree(make_git_repo, "wt-mech-reject")
        runner = _FixLoopRunner([_MF_MECH_REJECTED_DOC])
        out, verdict = _run_loop(runner, worktree, session_id="s-mech-reject")

        assert runner.fix_calls == 0
        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_MUST_FIX_MECHANICALLY_REJECTED
        assert verdict is not None
        assert verdict.blocking is False
        # One entry per selected reviewer role (the runner double replays the
        # same document for each); rejections are not deduped the way accepted
        # findings are, so assert on the reason rather than a role count.
        assert verdict.rejected_must_fix
        assert {rf.reason for rf in verdict.rejected_must_fix} == {
            "evidence_not_in_diff"
        }


# ---------------------------------------------------------------------------
# TestFixLoopDisabledGate
# ---------------------------------------------------------------------------


def _verdict_without_durations(verdict: ReviewVerdict | None) -> dict[str, object]:
    """Dump *verdict*, blanking the one field that cannot be reproducible.

    ``ReviewerRunRecord.duration_seconds`` (#1710) is measured wall time, so it
    differs between two otherwise-identical review passes. Blanking only that
    field keeps the rest of the equality assertion strict.
    """
    assert verdict is not None
    dumped = verdict.model_dump()
    for record in dumped["agents_run"]:
        record["duration_seconds"] = None
    return dumped


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
            fix_loop_enabled=False,
        )

        assert loop_result == plain_result
        # #1710: ReviewerRunRecord.duration_seconds is a real measured wall
        # clock, so two separate invocations can never be byte-identical on
        # that one field. Everything else about the verdict — including the
        # rest of the audit telemetry — still must match exactly.
        assert _verdict_without_durations(loop_verdict) == _verdict_without_durations(
            plain_verdict
        )
        assert loop_result.status == "blocked"
        assert loop_result.blocker is not None
        assert loop_result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert loop_runner.fix_calls == 0
        # One review pass worth of per-role calls (however many roles
        # run_review selects) — no re-review, since no fix cycle ran.
        # +1 for the #1709 filesystem-capability probe: loop_runner runs first
        # on a cold per-test cache and pays the probe; plain_runner's later call
        # is a warm cache hit. The probe's argv has no "workspace-write", so
        # _FixLoopRunner books it as a review call.
        assert loop_runner.review_calls == plain_runner.review_calls + 1
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
        # The scope violation parks before the cycle-1 rereview/persist call
        # runs, so only cycle 0's snapshot exists.
        assert (bundle / _verdict_snapshot_filename(0)).exists()
        assert not (bundle / _verdict_snapshot_filename(1)).exists()
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

    def test_out_of_scope_sensitive_modification_parks_verdict_stamped(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        # #1705 Decisions #2 regression pin: _park_scope_violation must stamp
        # the finalized Review onto the returned *verdict* too, not just the
        # returned AutoDevResult — mirrors _clean_exit's bug #2 fix, applied
        # to the scope-violation park path.
        worktree = _worktree(
            make_git_repo,
            "wt-scope-outsens-stamp",
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
        out, verdict = _run_loop(runner, worktree, session_id="s-scope-outsens-stamp")

        assert out.status == "blocked"
        assert out.blocker is not None
        assert out.blocker.reason == CODEX_FIX_SCOPE_VIOLATION
        assert verdict is not None
        assert verdict.review.must_fix_initial == out.review.must_fix_initial
        assert verdict.review.fix_cycles_used == out.review.fix_cycles_used
        assert verdict.review.must_fix_initial == 1
        assert verdict.review.fix_cycles_used == 1


# ---------------------------------------------------------------------------
# TestVerdictCommentDistinguishesHistories — #1705 R3
# ---------------------------------------------------------------------------


def _render_fix_loop_history(
    *,
    make_git_repo: Callable[..., Path],
    name: str,
    review_docs: list[str],
    fix_behaviors: list[_FixBehavior] | None,
    fix_loop_enabled: bool,
) -> str:
    worktree = _worktree(
        make_git_repo=make_git_repo,
        name=f"wt-history-{name}",
    )
    runner = _FixLoopRunner(
        review_docs=review_docs,
        fix_behaviors=fix_behaviors,
    )
    _, verdict = _run_loop(
        runner=runner,
        worktree=worktree,
        session_id=f"s-history-{name}",
        fix_loop_enabled=fix_loop_enabled,
    )
    assert verdict is not None
    return render_verdict_comment(
        verdict,
        fix_loop_enabled=fix_loop_enabled,
    )


class TestVerdictCommentDistinguishesHistories:
    """render_verdict_comment must render four distinct comments for four
    operationally different histories, even when the underlying Review can
    look alike (e.g. fix_cycles_used == 0 for both a genuinely-clean cycle 0
    and a fix-loop-disabled pass) — the fix_loop_enabled discriminator is
    what tells them apart (#1705), and ``had_real_commit`` further
    distinguishes a genuine fix from a no-op/flaked convergence (#1723)."""

    def test_converged_clean_off_and_flaked_render_four_distinct_comments(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        histories: tuple[
            tuple[str, list[str], list[_FixBehavior] | None, bool, tuple[str, ...]],
            ...,
        ] = (
            (
                "converged",
                [_MF_DOC, _CLEAN_DOC],
                [_editor()],
                True,
                ("resolved across 1 fix cycle",),
            ),
            ("clean", [_CLEAN_DOC], None, True, ("available",)),
            ("off", [_CLEAN_DOC], None, False, ("disabled",)),
            (
                "flaked",
                [_MF_DOC, _CLEAN_DOC],
                None,
                True,
                ("UNVERIFIED", "without changing any file"),
            ),
        )
        bodies = {
            name: _render_fix_loop_history(
                make_git_repo=make_git_repo,
                name=name,
                review_docs=review_docs,
                fix_behaviors=fix_behaviors,
                fix_loop_enabled=fix_loop_enabled,
            )
            for name, review_docs, fix_behaviors, fix_loop_enabled, _ in histories
        }

        assert len(set(bodies.values())) == len(histories)
        for name, _, _, _, expected_markers in histories:
            assert all(marker in bodies[name] for marker in expected_markers)


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


# ---------------------------------------------------------------------------
# Codex audit metrics survive the fix loop's terminal verdict (#1710)
# ---------------------------------------------------------------------------


_AUDIT_JSONL = (
    Path(__file__).parent
    / "fixtures"
    / "codex_audit_events"
    / "clean_with_command.jsonl"
).read_text(encoding="utf-8")


class TestFixLoopCarriesAuditMetrics:
    """`_finalize_review`/`_survivors_only_verdict` model_copy the verdict but
    never touch `agents_run`, so per-role metrics must survive to the terminal
    verdict of a multi-cycle run."""

    def test_clean_exit_terminal_verdict_carries_metrics(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-metrics-clean")
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC], review_stdout=_AUDIT_JSONL)
        out, verdict = _run_loop(runner, worktree, session_id="s-metrics-clean")

        assert out.status == "stage_complete"
        assert verdict is not None
        assert verdict.agents_run
        for record in verdict.agents_run:
            assert record.thread_id == "<THREAD_ID>"
            assert record.terminal_event == "turn.completed"
            assert record.tool_call_counts["command_execution"] == 1
            assert record.had_command_evidence is True

    def test_capped_run_terminal_verdict_carries_metrics(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-metrics-capped")
        runner = _FixLoopRunner([_MF_DOC], review_stdout=_AUDIT_JSONL)
        out, verdict = _run_loop(runner, worktree, session_id="s-metrics-capped")

        assert out.status == "blocked"
        assert verdict is not None
        assert verdict.agents_run
        assert all(r.thread_id == "<THREAD_ID>" for r in verdict.agents_run)


# ---------------------------------------------------------------------------
# Filesystem-capability probe is spent once per invocation (#1709)
# ---------------------------------------------------------------------------


class TestCapabilityProbeIsCachedAcrossCycles:
    """The probe is a real ``codex exec`` round-trip, so a fix loop that
    re-prepares a review pass every cycle must not pay for it every cycle."""

    def test_probe_runs_exactly_once_across_a_two_cycle_loop(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-probe-once")
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC], [_editor()])
        out, _verdict = _run_loop(runner, worktree, session_id="s-probe-once")

        assert out.status == "stage_complete"
        # Cycle 0's review pass plus _rereview's per-cycle pass both call
        # _prepare_review_pass; only the first misses the cache.
        assert runner.fix_calls == 1
        probe_calls = [c for c in runner.calls if c["argv"] == list(_PROBE_ARGV)]
        assert len(probe_calls) == 1


# ---------------------------------------------------------------------------
# Lean reviewer profile on the fix path (#1711)
# ---------------------------------------------------------------------------


class TestFixArgvSharesTheLeanProfile:
    def test_same_disabled_features_and_mcp_override_as_the_reviewer_path(
        self,
    ) -> None:
        argv = _build_fix_codex_argv(model="gpt-x", reasoning_effort="high")
        # Asserted via the shared constant, never a hand-copied literal: the
        # point is that both builders emit the SAME block.
        for feature in _DISABLED_FEATURES:
            assert argv[argv.index(feature) - 1] == "--disable"
        overrides = _config_override_values(argv)
        assert "mcp_servers={}" in overrides
        assert "project_doc_max_bytes=0" in overrides
        assert "model_reasoning_effort=high" in overrides
        assert "--ignore-user-config" in argv
        assert "--ignore-rules" not in argv
        assert "--strict-config" in argv
        # Fix invocations stay write-capable and document-free.
        assert argv[:4] == ["codex", "exec", "--sandbox", "workspace-write"]
        assert "--output-schema" not in argv
        assert argv[-2:] == ["-m", "gpt-x"]

    def test_effort_omitted_by_default(self) -> None:
        argv = _build_fix_codex_argv(model=None)
        assert not any(
            o.startswith("model_reasoning_effort=")
            for o in _config_override_values(argv)
        )


class TestParkFixFailureRecordsRealEffort:
    def test_diagnostics_argv_carries_the_threaded_effort(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """MUST_FIX 3 (#1711): _park_fix_failure reconstructs the fix argv for
        the diagnostics bundle. That reconstruction must reflect the effort the
        run actually used, not the default and not an omission."""
        worktree = _worktree(make_git_repo, "wt-fix-effort")
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[CodexRunResult(returncode=1, stdout="", stderr="boom")],
        )
        out, _ = _run_loop(
            runner,
            worktree,
            session_id="s-fix-effort",
            reasoning_effort="minimal",
        )

        assert out.status == "blocked"
        bundle = diagnostics_bundle_dir("s-fix-effort")
        failures = list(bundle.glob("fix-cycle-1-nonzero_exit-*.json"))
        assert failures
        payload = json.loads(failures[0].read_text(encoding="utf-8"))
        argv = payload["argv_sanitized"]
        assert "model_reasoning_effort=minimal" in _config_override_values(argv)

    def test_live_fix_invocation_carries_the_threaded_effort(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-fix-effort-live")
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[CodexRunResult(returncode=1, stdout="", stderr="boom")],
        )
        _run_loop(
            runner,
            worktree,
            session_id="s-fix-effort-live",
            reasoning_effort="minimal",
        )

        fix_call = next(c for c in runner.calls if "workspace-write" in c["argv"])
        argv = fix_call["argv"]
        assert isinstance(argv, list)
        assert "model_reasoning_effort=minimal" in _config_override_values(argv)

    def test_rereview_roles_carry_the_threaded_effort(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """_rereview never touches _build_fix_codex_argv — it calls
        run_codex_roles directly, so the effort must reach it that way."""
        worktree = _worktree(make_git_repo, "wt-rereview-effort")
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC], fix_behaviors=[_editor()])
        _run_loop(
            runner,
            worktree,
            session_id="s-rereview-effort",
            reasoning_effort="minimal",
        )

        review_calls = [c for c in runner.calls if "read-only" in c["argv"]]
        # The capability probe plus every reviewer role invocation.
        assert len(review_calls) > 1
        role_calls = [c for c in review_calls if "--output-schema" in c["argv"]]
        assert role_calls
        for call in role_calls:
            argv = call["argv"]
            assert isinstance(argv, list)
            assert "model_reasoning_effort=minimal" in _config_override_values(argv)
