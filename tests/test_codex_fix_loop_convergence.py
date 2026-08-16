"""Tests for cw.codex_fix_loop_convergence — the delta-aware admission gate (#1837).

The fix loop used to re-review the WHOLE PR diff every cycle, so each pass could
surface fresh MUST_FIX findings on code no fix cycle had touched — a treadmill
that burned the cycle cap without converging. These tests lock in the gate that
admits only findings the latest delta actually caused (or that carry a
substantiated release-critical exception), diverting the rest into a debt ledger.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

from cw.auto_dev_result import Review
from cw.codex_fix_loop import (
    _MAX_FIX_CYCLES,
    _park_survivors,
    _PersistedSnapshot,
    run_review_with_fix_loop,
)
from cw.codex_fix_loop_convergence import (
    _admit_new_must_fix,
    _finding_in_delta,
    _open_finding_key,
    _survivors_only_verdict,
    _track_open_findings,
)
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.codex_runner import CodexRunResult
from cw.events import read_events
from cw.local_runner import make_blocked
from cw.models.enums import OrchestratorEventType
from cw.review_findings import (
    AcceptedFinding,
    CapturedDiff,
    DebtRecord,
    Finding,
    ReviewVerdict,
    _dedup_key,
)
from tests._codex_review_helpers import _task
from tests.conftest import _make_diff, _make_finding

if TYPE_CHECKING:
    from collections.abc import Callable

_TICKET = "1837"
_FAKE_REREVIEW_REASON = "fake_rereview_result"


def _accepted(**overrides: object) -> AcceptedFinding:
    return AcceptedFinding(
        finding=_make_finding(**overrides), reviewers=["Code Quality Reviewer"]
    )


def _delta() -> CapturedDiff:
    """A delta touching only ``src/cw/producer.py``."""
    return _make_diff("def changed(x, y):", files={"src/cw/producer.py": [3]})


def _track(
    open_findings: dict[Any, AcceptedFinding],
    accepted: list[AcceptedFinding],
    **overrides: object,
) -> dict[Any, AcceptedFinding]:
    """Call ``_track_open_findings`` with cycle-0 defaults, overridable."""
    kwargs: dict[str, object] = {
        "delta_diff": None,
        "delta_changed_files": None,
        "debt_ledger": {},
        "previous_reviewed_sha": None,
        "reviewed_sha": "headsha",
        "worktree": Path(),
        "ticket_id": _TICKET,
    }
    kwargs.update(overrides)
    return _track_open_findings(open_findings, accepted, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _finding_in_delta
# ---------------------------------------------------------------------------


class TestFindingInDelta:
    def test_anchored_finding_inside_the_delta(self) -> None:
        finding = _make_finding(file="src/cw/producer.py", line_start=3, line_end=3)
        assert _finding_in_delta(finding, frozenset({"src/cw/producer.py"})) is True

    def test_finding_on_a_file_outside_the_delta(self) -> None:
        finding = _make_finding(file="src/cw/consumer.py")
        assert _finding_in_delta(finding, frozenset({"src/cw/producer.py"})) is False

    def test_file_level_finding_on_a_pure_deletion(self) -> None:
        """A deleted file has no added lines but IS in the delta's file list."""
        finding = _make_finding(file="src/cw/gone.py", line_start=None, line_end=None)
        assert _finding_in_delta(finding, frozenset({"src/cw/gone.py"})) is True


# ---------------------------------------------------------------------------
# _admit_new_must_fix
# ---------------------------------------------------------------------------


class TestAdmitNewMustFix:
    _CHANGED = frozenset({"src/cw/producer.py"})

    def _worktree(self, tmp_path: Path, content: str) -> Path:
        target = tmp_path / "src" / "cw" / "consumer.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return tmp_path

    def test_in_delta_finding_is_admitted(self, tmp_path: Path) -> None:
        finding = _make_finding(file="src/cw/producer.py", line_start=3, line_end=3)
        assert _admit_new_must_fix(
            finding, _delta(), self._CHANGED, worktree=tmp_path
        ) == (True, "in_delta")

    def test_causal_impact_evidence_in_delta_text_is_admitted(
        self, tmp_path: Path
    ) -> None:
        finding = _make_finding(
            file="src/cw/consumer.py",
            transitive_impact_evidence="def changed(x, y):",
        )
        assert _admit_new_must_fix(
            finding, _delta(), self._CHANGED, worktree=tmp_path
        ) == (True, "causal_impact")

    def test_causal_impact_evidence_absent_from_delta_is_treadmill(
        self, tmp_path: Path
    ) -> None:
        finding = _make_finding(
            file="src/cw/consumer.py",
            transitive_impact_evidence="def never_appeared():",
        )
        assert _admit_new_must_fix(
            finding, _delta(), self._CHANGED, worktree=tmp_path
        ) == (False, "treadmill")

    def test_substantiated_release_critical_exception_is_admitted(
        self, tmp_path: Path
    ) -> None:
        worktree = self._worktree(tmp_path, "before\ndef broken():\nafter\n")
        finding = _make_finding(
            file="src/cw/consumer.py",
            evidence="def broken():",
            release_critical_exception="unauthenticated write path, now reachable",
        )
        assert _admit_new_must_fix(
            finding, _delta(), self._CHANGED, worktree=worktree
        ) == (True, "release_critical_exception")

    def test_unsubstantiated_release_critical_exception_is_rejected(
        self, tmp_path: Path
    ) -> None:
        worktree = self._worktree(tmp_path, "nothing matching here\n")
        finding = _make_finding(
            file="src/cw/consumer.py",
            evidence="def broken():",
            release_critical_exception="unauthenticated write path, now reachable",
        )
        assert _admit_new_must_fix(
            finding, _delta(), self._CHANGED, worktree=worktree
        ) == (False, "unsubstantiated_evidence")

    def test_missing_file_makes_the_exception_unsubstantiated(
        self, tmp_path: Path
    ) -> None:
        finding = _make_finding(
            file="src/cw/nowhere.py",
            release_critical_exception="claims to matter",
        )
        assert _admit_new_must_fix(
            finding, _delta(), self._CHANGED, worktree=tmp_path
        ) == (False, "unsubstantiated_evidence")

    def test_out_of_delta_with_neither_field_is_treadmill(self, tmp_path: Path) -> None:
        finding = _make_finding(file="src/cw/consumer.py")
        assert _admit_new_must_fix(
            finding, _delta(), self._CHANGED, worktree=tmp_path
        ) == (False, "treadmill")


# ---------------------------------------------------------------------------
# _track_open_findings — fingerprint identity + admission gate
# ---------------------------------------------------------------------------


class TestTrackOpenFindingsIdentity:
    def test_line_movement_does_not_create_a_new_identity(self) -> None:
        """AC10: the same finding re-raised at a new line is one survivor."""
        first = _accepted(
            summary="Missing null check at line 10", line_start=10, line_end=10
        )
        moved = _accepted(
            summary="Missing null check at line 40", line_start=40, line_end=40
        )

        open_findings = _track({}, [first])
        updated = _track(
            open_findings,
            [moved],
            delta_diff=_delta(),
            delta_changed_files=frozenset({"src/cw/producer.py"}),
            previous_reviewed_sha="prev",
        )

        assert list(updated) == list(open_findings)
        assert len(updated) == 1

    def test_normalization_equivalent_rewording_is_the_same_survivor(self) -> None:
        first = _accepted(summary="Duplicated across 3 call sites")
        reworded = _accepted(summary="Duplicated across 4 call sites")

        open_findings = _track({}, [first])
        updated = _track(
            open_findings,
            [reworded],
            delta_diff=_delta(),
            delta_changed_files=frozenset({"src/cw/producer.py"}),
            previous_reviewed_sha="prev",
        )

        assert list(updated) == list(open_findings)

    def test_na_file_finding_falls_back_to_the_dedup_key(self) -> None:
        af = _accepted(file="N/A", no_diff_anchor=True, line_start=None, line_end=None)
        open_findings = _track({}, [af])
        assert list(open_findings) == [_dedup_key(af.finding)]
        assert _open_finding_key(af.finding) == _dedup_key(af.finding)


class TestTrackOpenFindingsAdmission:
    _CHANGED = frozenset({"src/cw/producer.py"})

    def _delta_track(
        self,
        open_findings: dict[Any, AcceptedFinding],
        accepted: list[AcceptedFinding],
        ledger: dict[tuple[str, str], DebtRecord],
        worktree: Path,
    ) -> dict[Any, AcceptedFinding]:
        return _track(
            open_findings,
            accepted,
            delta_diff=_delta(),
            delta_changed_files=self._CHANGED,
            debt_ledger=ledger,
            previous_reviewed_sha="prevsha",
            worktree=worktree,
        )

    def test_new_out_of_delta_finding_is_diverted_to_debt(self, tmp_path: Path) -> None:
        ledger: dict[tuple[str, str], DebtRecord] = {}
        af = _accepted(file="src/cw/consumer.py", summary="Old unrelated smell")

        updated = self._delta_track({}, [af], ledger, tmp_path)

        assert updated == {}
        assert [r.summary for r in ledger.values()] == ["Old unrelated smell"]
        events = read_events(
            event_types=[OrchestratorEventType.REVIEW_TREADMILL_DETECTED]
        )
        assert len(events) == 1
        assert events[0].correlation_id == _TICKET
        assert events[0].payload["file"] == "src/cw/consumer.py"
        assert events[0].payload["severity"] == "MUST_FIX"
        assert events[0].payload["previous_reviewed_sha"] == "prevsha"

    def test_new_in_delta_finding_still_blocks(self, tmp_path: Path) -> None:
        ledger: dict[tuple[str, str], DebtRecord] = {}
        af = _accepted(file="src/cw/producer.py", line_start=3, line_end=3)

        updated = self._delta_track({}, [af], ledger, tmp_path)

        assert list(updated) == [_open_finding_key(af.finding)]
        assert ledger == {}

    def test_cycle_zero_admits_an_ordinary_must_fix_unconditionally(self) -> None:
        """Item 1 regression lock: no admission gate runs when there is no delta.

        The pre-loop seeding call passes ``delta_diff=None`` — cycle 0 has no
        prior head to restrict against, so an ordinary MUST_FIX with neither
        causal-impact nor release-critical fields must still block.
        """
        ledger: dict[tuple[str, str], DebtRecord] = {}
        af = _accepted(file="src/cw/consumer.py")

        updated = _track({}, [af], debt_ledger=ledger)

        assert list(updated) == [_open_finding_key(af.finding)]
        assert ledger == {}

    def test_accepted_debt_severity_finding_enters_the_ledger(
        self, tmp_path: Path
    ) -> None:
        ledger: dict[tuple[str, str], DebtRecord] = {}
        debt = _accepted(severity="DEBT", summary="Known duplication")

        updated = self._delta_track({}, [debt], ledger, tmp_path)

        assert updated == {}
        assert [r.summary for r in ledger.values()] == ["Known duplication"]

    def test_prior_finding_disappearance_is_implicit_resolution(self) -> None:
        """Operator resolution 3: absence is resolution — no restatement needed."""
        af = _accepted()
        open_findings = _track({}, [af])
        assert open_findings

        updated = _track(
            open_findings,
            [],
            delta_diff=_delta(),
            delta_changed_files=frozenset({"src/cw/producer.py"}),
            previous_reviewed_sha="prev",
        )

        assert updated == {}


# ---------------------------------------------------------------------------
# _survivors_only_verdict — the key-shape fix
# ---------------------------------------------------------------------------


def _verdict(*accepted: AcceptedFinding, **overrides: object) -> ReviewVerdict:
    must_fix = [af.finding for af in accepted if af.finding.severity == "MUST_FIX"]
    payload: dict[str, object] = {
        "blocking": bool(must_fix),
        "must_fix": must_fix,
        "reviewed_sha": "sha",
        "accepted": list(accepted),
        "review": Review(
            must_fix_initial=len(must_fix),
            should_fix=0,
            fix_cycles_used=0,
            deferred=0,
            agents_run=1,
        ),
    }
    payload.update(overrides)
    return ReviewVerdict.model_validate(payload)


class TestSurvivorsOnlyVerdict:
    def test_fingerprinted_survivor_is_stamped_deferred(self) -> None:
        survivor = _accepted(summary="Missing null check at line 10")
        bystander = _accepted(severity="SHOULD_FIX", summary="Style nit")
        verdict = _verdict(survivor, bystander)
        open_findings = {_open_finding_key(survivor.finding): survivor}

        rebuilt = _survivors_only_verdict(verdict, open_findings, verdict.review)

        assert rebuilt.blocking is True
        assert rebuilt.must_fix == [survivor.finding]
        assert rebuilt.accepted[0].disposition == "deferred"
        assert rebuilt.accepted[1].disposition == "fixed"

    def test_park_survivors_stamps_fingerprinted_survivor_as_deferred(
        self, tmp_path: Path
    ) -> None:
        survivor = _accepted(summary="Missing null check at line 10")
        verdict = _verdict(survivor)
        open_findings = {_open_finding_key(survivor.finding): survivor}

        _result, survivors = _park_survivors(
            task=_task(),
            worktree=tmp_path,
            session_id="s-park",
            reason=CODEX_MUST_FIX_FINDINGS,
            verdict=verdict,
            open_findings=open_findings,
            cycle0_review=verdict.review,
            cycle_count=_MAX_FIX_CYCLES,
            retry_eligible=None,
            snapshot=_PersistedSnapshot("pointer", 0),
            had_real_commit=True,
        )

        assert survivors.blocking is True
        assert survivors.accepted[0].disposition == "deferred"


# ---------------------------------------------------------------------------
# End-to-end convergence through run_review_with_fix_loop
# ---------------------------------------------------------------------------


class _FakePrepared(NamedTuple):
    """Only the two fields the loop reads off ``_rereview``'s prepared inputs."""

    delta_diff: CapturedDiff | None
    delta_changed_files: frozenset[str] | None


class _FakeRunner:
    """Every codex invocation succeeds and changes nothing."""

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        return CodexRunResult(returncode=0, stdout="", stderr="")


class _FakeRereview:
    """Replays a scripted list of per-cycle accepted-finding sets."""

    def __init__(self, cycles: list[list[AcceptedFinding]], worktree: Path) -> None:
        self._cycles = cycles
        self._worktree = worktree
        self.previous_shas: list[str] = []
        self.prior_summaries: list[list[str]] = []
        self.calls = 0

    def __call__(self, **kwargs: Any) -> tuple[Any, ReviewVerdict, _FakePrepared]:
        self.previous_shas.append(kwargs["previous_reviewed_sha"])
        self.prior_summaries.append([f.summary for f in kwargs["prior_open_findings"]])
        index = min(self.calls, len(self._cycles) - 1)
        self.calls += 1
        accepted = self._cycles[index]
        verdict = _verdict(*accepted, reviewed_sha=f"sha{index + 1}")
        result = make_blocked(
            ticket_id=_TICKET,
            worktree=self._worktree,
            reason=_FAKE_REREVIEW_REASON,
            stage_reached="stage3_review",
        )
        return (
            result,
            verdict,
            _FakePrepared(_delta(), frozenset({"src/cw/producer.py"})),
        )


@pytest.fixture
def loop_repo(make_git_repo: Callable[[str], Path]) -> Path:
    return make_git_repo("wt-convergence")


def _drive_loop(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    cycle0: list[AcceptedFinding],
    cycles: list[list[AcceptedFinding]],
) -> tuple[Any, ReviewVerdict | None, _FakeRereview]:
    from cw import codex_fix_loop

    cycle0_verdict = _verdict(*cycle0, reviewed_sha="sha0")
    cycle0_result = make_blocked(
        ticket_id=_TICKET,
        worktree=repo,
        reason=_FAKE_REREVIEW_REASON,
        stage_reached="stage3_review",
    )
    monkeypatch.setattr(
        codex_fix_loop,
        "run_review",
        lambda **_kwargs: (cycle0_result, cycle0_verdict),
    )
    fake = _FakeRereview(cycles, repo)
    monkeypatch.setattr(codex_fix_loop, "_rereview", fake)

    result, verdict = run_review_with_fix_loop(
        runner=_FakeRunner(),  # type: ignore[arg-type]
        task=_task(),
        worktree=repo,
        default_branch="main",
        model=None,
        wall_clock_budget_seconds=None,
        session_id="s-convergence",
        fix_loop_enabled=True,
    )
    return result, verdict, fake


class TestConvergence:
    def test_convergence_fixture_does_not_reach_cycle_cap(
        self, monkeypatch: pytest.MonkeyPatch, loop_repo: Path
    ) -> None:
        """Three cycles of fresh out-of-delta findings converge instead of capping.

        Under the pre-#1837 full-rescan behavior each new finding was admitted,
        so ``open_findings`` never emptied and the loop burned the cap. Now each
        one is diverted into the debt ledger and the loop exits clean at cycle 3.
        """
        original = _accepted(summary="Original bug", file="src/cw/producer.py")
        finding_a = _accepted(summary="Old smell A", file="src/cw/consumer.py")
        finding_b = _accepted(summary="Old smell B", file="src/cw/other.py")
        finding_c = _accepted(summary="Old smell C", file="src/cw/third.py")

        result, verdict, fake = _drive_loop(
            monkeypatch,
            loop_repo,
            [original],
            [
                [original, finding_a],
                [original, finding_b],
                [finding_c],
            ],
        )

        assert verdict is not None
        assert result.blocker is not None
        # Clean exit: the loop returned `_rereview`'s own result, not a park.
        assert result.blocker.reason == _FAKE_REREVIEW_REASON
        assert verdict.review.fix_cycles_used == 3
        assert fake.calls == 3

        summaries = sorted(record.summary for record in verdict.debt)
        assert summaries == ["Old smell A", "Old smell B", "Old smell C"]
        events = read_events(
            event_types=[OrchestratorEventType.REVIEW_TREADMILL_DETECTED]
        )
        assert len(events) == 3

    def test_previous_sha_and_prior_findings_are_threaded(
        self, monkeypatch: pytest.MonkeyPatch, loop_repo: Path
    ) -> None:
        original = _accepted(summary="Original bug", file="src/cw/producer.py")

        _result, verdict, fake = _drive_loop(
            monkeypatch, loop_repo, [original], [[original], []]
        )

        assert verdict is not None
        assert fake.previous_shas[0] == "sha0"
        assert fake.previous_shas[1] == "sha1"
        assert fake.prior_summaries[0] == ["Original bug"]
        assert fake.prior_summaries[1] == ["Original bug"]

    def test_regression_from_latest_fix_still_blocks(
        self, monkeypatch: pytest.MonkeyPatch, loop_repo: Path
    ) -> None:
        """A new MUST_FIX anchored INSIDE the delta is the fix's own regression."""
        original = _accepted(summary="Original bug", file="src/cw/producer.py")
        regression = _accepted(
            summary="Fix introduced a crash",
            file="src/cw/producer.py",
            line_start=3,
            line_end=3,
        )

        result, verdict, _fake = _drive_loop(
            monkeypatch, loop_repo, [original], [[regression]]
        )

        assert verdict is not None
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert [f.summary for f in verdict.must_fix] == ["Fix introduced a crash"]

    def test_transitive_impact_on_unchanged_consumer_still_blocks(
        self, monkeypatch: pytest.MonkeyPatch, loop_repo: Path
    ) -> None:
        original = _accepted(summary="Original bug", file="src/cw/producer.py")
        transitive = _accepted(
            summary="Consumer now passes the wrong arity",
            file="src/cw/consumer.py",
            transitive_impact_evidence="def changed(x, y):",
        )

        result, verdict, _fake = _drive_loop(
            monkeypatch, loop_repo, [original], [[transitive]]
        )

        assert verdict is not None
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert [f.summary for f in verdict.must_fix] == [
            "Consumer now passes the wrong arity"
        ]
        assert verdict.debt == []

    def test_release_critical_exception_admits_old_code_blocker(
        self, monkeypatch: pytest.MonkeyPatch, loop_repo: Path
    ) -> None:
        (loop_repo / "src" / "cw").mkdir(parents=True, exist_ok=True)
        (loop_repo / "src" / "cw" / "consumer.py").write_text(
            "before\ndef broken():\nafter\n", encoding="utf-8"
        )
        original = _accepted(summary="Original bug", file="src/cw/producer.py")
        release_critical = _accepted(
            summary="Unauthenticated write path",
            file="src/cw/consumer.py",
            evidence="def broken():",
            release_critical_exception="always present, only now provably reachable",
        )

        result, verdict, _fake = _drive_loop(
            monkeypatch, loop_repo, [original], [[release_critical]]
        )

        assert verdict is not None
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert [f.summary for f in verdict.must_fix] == ["Unauthenticated write path"]
        assert verdict.debt == []

    def test_unsubstantiated_release_critical_exception_becomes_debt(
        self, monkeypatch: pytest.MonkeyPatch, loop_repo: Path
    ) -> None:
        original = _accepted(summary="Original bug", file="src/cw/producer.py")
        unsubstantiated = _accepted(
            summary="Unauthenticated write path",
            file="src/cw/consumer.py",
            evidence="def broken():",
            release_critical_exception="always present, only now provably reachable",
        )

        result, verdict, _fake = _drive_loop(
            monkeypatch, loop_repo, [original], [[unsubstantiated]]
        )

        assert verdict is not None
        assert result.blocker is not None
        assert result.blocker.reason == _FAKE_REREVIEW_REASON
        assert [r.summary for r in verdict.debt] == ["Unauthenticated write path"]


def test_finding_is_unhashable_so_survivor_membership_must_use_a_list() -> None:
    """Guards the set→list comprehension fix in ``_survivors_only_verdict``."""
    with pytest.raises(TypeError):
        hash(_make_finding())


def test_finding_equality_is_by_value() -> None:
    left: Finding = _make_finding()
    right: Finding = _make_finding()
    assert left is not right
    assert left == right
