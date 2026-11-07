"""Tests for cw.codex_loop_rounds: every codex fix-loop cycle is a ledger round (#2816).

The recorder runs against a real git repo and a real ledger file. The
end-to-end cases drive the real ``run_review_with_fix_loop`` (scripted
reviews, real fix commits) with the real recorder as its cycle observer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw import codex_loop_rounds
from cw.auto_dev_result.schema._vocab import REVIEW_NOT_CONVERGING_BLOCKER_REASON
from cw.codex_clean_confirm import LOOP_DEFER_RATIONALE, apply_clean_confirm
from cw.codex_fix_loop import run_review_with_fix_loop
from cw.codex_fix_loop.convergence import _open_finding_key
from cw.codex_fix_loop.rounds import CycleRound
from cw.codex_loop_rounds import LoopRoundRecorder, loop_round_recorder
from cw.codex_review import CODEX_CONVERGENCE_LEDGER_ERROR
from cw.models import ClientConfig
from cw.models.client import ReviewConfig
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    LEDGER_STATUS_AWAITING_CONFIRM,
    LEDGER_STATUS_CONVERGED,
    ConvergenceError,
    ConvergenceLedger,
    finding_identity,
    friction_lines,
    load_ledger,
)
from cw.review_findings import AcceptedFinding
from tests._claude_review_helpers import RoleAwareClaudeRunner
from tests._codex_review_helpers import (
    _drive_loop,
    _FakeRunner,
    _loop_verdict,
    _task,
)
from tests._review_convergence_helpers import (
    consolidated_verdict,
    dirty_confirm_ledger,
    fresh_ledger,
    live_merge_base,
    record,
    round_finding,
    write_ledger,
)
from tests.conftest import _make_finding, commit_tracked_file, git_in
from tests.test_codex_background import _clean_result
from tests.test_codex_fix_loop import (
    _MF_DOC,
    _editor,
    _FixLoopRunner,
    _worktree,
)
from tests.test_review_convergence import _sharded

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.claude_review import ClaudeRunResult
    from cw.codex_fix_loop.rounds import RoundStop
    from cw.review_convergence import ConfirmBackend
    from cw.review_findings import Disposition, ReviewVerdict, Severity

_TICKET = "T-1"
_SID = "s-rounds"
_PRODUCER = "src/cw/producer.py"
_LOGGER = "cw.codex_loop_rounds"


def _exclude_cw(worktree: Path) -> Path:
    """Git-exclude ``.cw/`` as real dispatch worktrees do (the ledger lives there)."""
    exclude = worktree / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write(".cw/\n")
    return worktree


@pytest.fixture
def wt(make_git_repo_with_origin: Callable[..., tuple[Path, Path]]) -> Path:
    worktree, _origin = make_git_repo_with_origin("wt-loop-rounds")
    return _exclude_cw(worktree)


def _client(
    worktree: Path,
    *,
    max_cycles: int | None = None,
    confirm: ConfirmBackend | None = None,
) -> ClientConfig:
    return ClientConfig(
        name="test",
        workspace_path=worktree,
        default_branch="main",
        review=ReviewConfig(
            convergence="clean", max_cycles=max_cycles, confirm_backend=confirm
        ),
    )


def _no_claude(role: str) -> ClaudeRunResult:
    msg = f"the loop recorder must never call Claude (role {role})"
    raise AssertionError(msg)


def _recorder(
    worktree: Path,
    *,
    client: ClientConfig | None = None,
    seeds: tuple[AcceptedFinding, ...] = (),
) -> LoopRoundRecorder:
    return loop_round_recorder(
        task=_task(),
        worktree=worktree,
        client=client or _client(worktree),
        sid=_SID,
        runner=RoleAwareClaudeRunner(_no_claude),
        deadline=None,
        seed_findings=seeds,
    )


def _af(
    summary: str,
    *,
    severity: Severity = "MUST_FIX",
    disposition: Disposition = "fixed",
    file: str = _PRODUCER,
) -> AcceptedFinding:
    return AcceptedFinding(
        finding=_make_finding(severity=severity, file=file, summary=summary),
        reviewers=["Code Quality Reviewer"],
        disposition=disposition,
    )


def _cycle(
    cycle: int,
    sha: str,
    *accepted: AcceptedFinding,
    open_: tuple[AcceptedFinding, ...] | None = None,
) -> CycleRound:
    """A cycle's verdict at *sha*; open findings default to its open MUST_FIX."""
    chosen = accepted if open_ is None else open_
    open_findings = {
        _open_finding_key(af.finding): af
        for af in chosen
        if af.finding.severity == "MUST_FIX" and af.disposition == "fixed"
    }
    return CycleRound(cycle, _loop_verdict(*accepted, reviewed_sha=sha), open_findings)


#: Words, not digits: the fingerprint and the identity both normalize digits,
#: so ``stalled 0.1`` and ``stalled 1.1`` would be one finding.
_WORDS = ("alpha", "bravo", "charlie", "delta", "echo")


def _stalled(cycle: int, count: int) -> list[AcceptedFinding]:
    """*count* distinct MUST_FIX findings unique to *cycle*."""
    return [_af(f"stalled {_WORDS[cycle]} {_WORDS[n]}") for n in range(count)]


def _head(worktree: Path) -> str:
    return git_in(worktree, "rev-parse", "HEAD")


def _advance(worktree: Path, name: str) -> str:
    commit_tracked_file(worktree, name)
    return _head(worktree)


def _ledger(worktree: Path) -> ConvergenceLedger:
    loaded = load_ledger(worktree / LEDGER_RELATIVE_PATH)
    assert loaded is not None
    return loaded


def _codex_ledger(
    worktree: Path,
    *,
    confirm: ConfirmBackend = "claude",
    loop_backend: str = "codex",
    ticket_id: str = _TICKET,
) -> ConvergenceLedger:
    """An empty codex-lane ledger keyed as the gate keys it."""
    return fresh_ledger(
        confirm=confirm,
        loop_backend=loop_backend,
        ticket_id=ticket_id,
        fork_point=live_merge_base(worktree),
    )


def _assert_ledger_error(stop: RoundStop | None, recorder: LoopRoundRecorder) -> None:
    assert stop is not None
    assert stop.reason == CODEX_CONVERGENCE_LEDGER_ERROR
    assert stop.retry_eligible is True
    assert any(
        token.startswith("convergence_bookkeeping_failed: ")
        for token in recorder.tokens
    )


def _assert_ledger_untouched(
    worktree: Path, recorder: LoopRoundRecorder, before: ConvergenceLedger
) -> None:
    """A park wrote no round and moved the ledger to no terminal status."""
    on_disk = _ledger(worktree)
    assert on_disk.status == before.status
    assert on_disk.status not in (
        LEDGER_STATUS_CONVERGED,
        LEDGER_STATUS_AWAITING_CONFIRM,
    )
    assert on_disk.last_clean_sha == before.last_clean_sha
    assert on_disk.last_clean_sha != _head(worktree)
    assert len(on_disk.rounds) == len(before.rounds)
    in_memory = recorder.gate.ledger
    assert in_memory is None or len(in_memory.rounds) == len(before.rounds)


# ---------------------------------------------------------------------------
# Recording rounds
# ---------------------------------------------------------------------------


class TestRecording:
    def test_dirty_cycle0_records_loop_round_with_findings(
        self, wt: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        head = _head(wt)
        recorder = _recorder(wt)

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            stop = recorder.record(_cycle(0, head, _af("Bug A"), _af("Bug B")))

        assert stop is None
        ledger = _ledger(wt)
        (round_,) = ledger.rounds
        assert (round_.kind, round_.backend) == ("loop", "codex")
        assert round_.new_accepted == 2
        assert round_.reviewed_sha == head
        assert ledger.fix_cycles_used == 1
        assert ledger.frozen is not None
        assert ledger.frozen.must_fix_initial == 2
        assert ledger.diff_base == live_merge_base(wt)
        assert {entry.reviewed_sha for entry in ledger.fixed} == {head}
        (logged,) = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
        for fragment in (
            f"ticket={_TICKET}",
            "client=test",
            f"session={_SID}",
            "cycle=0",
            "round=1",
            "decision=continue",
            "new_accepted=2",
        ):
            assert fragment in logged

    def test_sharded_cycle_records_its_shard_tally(self, wt: Path) -> None:
        """#2867: a sharded cycle-0 round is stored with its shard count."""
        base = _cycle(0, _head(wt), _af("Bug A"))
        sharded = CycleRound(
            0, _sharded(base.verdict, True, True, True), base.open_findings
        )

        stop = _recorder(wt).record(sharded)

        assert stop is None
        ledger = _ledger(wt)
        (round_,) = ledger.rounds
        assert (round_.shard_count, round_.shards_reviewed) == (3, 3)
        assert "confirm_round_sharded: round=1 shards=3 reviewed=3" in (
            friction_lines(ledger)
        )

    def test_unsharded_cycle_records_one_shard(self, wt: Path) -> None:
        stop = _recorder(wt).record(_cycle(0, _head(wt), _af("Bug A")))

        assert stop is None
        ledger = _ledger(wt)
        (round_,) = ledger.rounds
        assert (round_.shard_count, round_.shards_reviewed) == (1, 1)
        assert not any(
            line.startswith("confirm_round_sharded") for line in friction_lines(ledger)
        )

    def test_each_cycle_appends_a_round(self, wt: Path) -> None:
        recorder = _recorder(wt)
        shas = [_head(wt), _advance(wt, "c1.py"), _advance(wt, "c2.py")]

        stops = [
            recorder.record(_cycle(0, shas[0], _af("Bug A"), _af("Bug B"))),
            recorder.record(_cycle(1, shas[1], _af("Bug C"))),
            recorder.record(_cycle(2, shas[2], _af("Bug D"))),
        ]

        assert stops == [None, None, None]
        ledger = _ledger(wt)
        assert [r.new_accepted for r in ledger.rounds] == [2, 1, 1]
        assert [r.reviewed_sha for r in ledger.rounds] == shas
        assert ledger.fix_cycles_used == 3
        # The frozen baseline is cycle 0's, never a later cycle's.
        assert ledger.frozen is not None
        assert ledger.frozen.must_fix_initial == 2

    def test_treadmill_diverted_must_fix_not_counted(self, wt: Path) -> None:
        kept, diverted = _af("Bug A"), _af("Old unrelated smell", file="src/old.py")

        _recorder(wt).record(_cycle(0, _head(wt), kept, diverted, open_=(kept,)))

        (round_,) = _ledger(wt).rounds
        assert round_.new_accepted == 1
        assert [e.identity.summary_normalized for e in _ledger(wt).fixed] == ["Bug A"]

    def test_should_fix_is_deferred_not_gating(self, wt: Path) -> None:
        _recorder(wt).record(
            _cycle(0, _head(wt), _af("Bug A"), _af("Style", severity="SHOULD_FIX"))
        )

        ledger = _ledger(wt)
        assert ledger.rounds[0].new_accepted == 1
        (settled,) = ledger.settled
        assert (settled.identity.severity, settled.outcome) == ("SHOULD_FIX", "defer")
        # The persisted rationale names the codex loop, not a Claude confirm round.
        assert settled.rationale == LOOP_DEFER_RATIONALE
        assert "confirm" not in settled.rationale

    def test_explicitly_fixed_should_fix_survives_loop_normalization(
        self, wt: Path
    ) -> None:
        explicit = _af("Blocking plan requirement", severity="SHOULD_FIX").model_copy(
            update={"adjudicated_fix": True}
        )
        _recorder(wt).record(_cycle(0, _head(wt), explicit))

        ledger = _ledger(wt)
        assert ledger.rounds[0].new_fix == 1
        assert ledger.rounds[0].clean is False
        assert ledger.settled == []

    def test_operator_action_finding_stops(self, wt: Path) -> None:
        recorder = _recorder(wt)

        stop = recorder.record(
            _cycle(
                0,
                _head(wt),
                _af("Bug A"),
                _af("Rotate the credential", disposition="operator_actionable"),
            )
        )

        assert stop is not None
        assert stop.reason == REVIEW_NOT_CONVERGING_BLOCKER_REASON
        assert "operator_action" in stop.details
        assert _ledger(wt).stop_reason == "operator_action"


# ---------------------------------------------------------------------------
# The three detectors
# ---------------------------------------------------------------------------


class TestDetectors:
    def test_oscillation_when_fixed_finding_returns_at_new_sha_stops(
        self, wt: Path
    ) -> None:
        recorder = _recorder(wt)
        head = _head(wt)
        assert recorder.record(_cycle(0, head, _af("Bug A"))) is None

        stop = recorder.record(_cycle(1, _advance(wt, "c1.py"), _af("Bug A")))

        assert stop is not None
        assert stop.reason == REVIEW_NOT_CONVERGING_BLOCKER_REASON
        assert stop.retry_eligible is False
        assert "convergence ledger stopped (oscillation)" in stop.details
        assert "operator-settle" in stop.details
        ledger = _ledger(wt)
        assert (ledger.status, ledger.stop_reason) == ("stopped", "oscillation")
        assert "codex_loop_round_stop: decision=stop_not_converging round=2" in (
            recorder.tokens
        )

    def test_no_progress_stops_on_third_rising_round(self, wt: Path) -> None:
        recorder = _recorder(wt)
        shas = [_head(wt), _advance(wt, "c1.py"), _advance(wt, "c2.py")]
        counts = (2, 3, 4)

        stops = [
            recorder.record(
                _cycle(
                    cycle,
                    shas[cycle],
                    *_stalled(cycle, count),
                )
            )
            for cycle, count in enumerate(counts)
        ]

        assert stops[:2] == [None, None]
        assert stops[2] is not None
        assert "no_progress" in stops[2].details
        ledger = _ledger(wt)
        assert (ledger.status, ledger.stop_reason) == ("stopped", "no_progress")

    def test_noise_floor_series_does_not_stop(self, wt: Path) -> None:
        """1,2,3 never falls, so only the noise floor (smallest count 1) saves it."""
        recorder = _recorder(wt)
        shas = [_head(wt), _advance(wt, "c1.py"), _advance(wt, "c2.py")]

        stops = [
            recorder.record(_cycle(cycle, shas[cycle], *_stalled(cycle, count)))
            for cycle, count in enumerate((1, 2, 3))
        ]

        assert stops == [None, None, None]
        ledger = _ledger(wt)
        assert [r.new_accepted for r in ledger.rounds] == [1, 2, 3]
        assert ledger.status == "in_progress"

    def test_max_cycles_stops_at_cap(self, wt: Path) -> None:
        recorder = _recorder(wt, client=_client(wt, max_cycles=2))
        shas = [_head(wt), _advance(wt, "c1.py"), _advance(wt, "c2.py")]

        stops = [
            recorder.record(_cycle(cycle, sha, _af(f"Bug {cycle}")))
            for cycle, sha in enumerate(shas)
        ]

        assert stops[:2] == [None, None]
        assert stops[2] is not None
        assert "max_cycles" in stops[2].details
        assert _ledger(wt).stop_reason == "max_cycles"

    def test_max_cycles_counts_the_dirty_confirm_cycle(self, wt: Path) -> None:
        """A dirty Claude confirm already charged a cycle; loop rounds add to it."""
        claude = consolidated_verdict(
            accepted=[("MUST_FIX", "Claude finding")], sha=_head(wt)
        )
        dirty_confirm_ledger(wt, claude, ticket_id=_TICKET, max_cycles=2)
        recorder = _recorder(wt, client=_client(wt, max_cycles=2))

        first = recorder.record(_cycle(0, _head(wt), _af("Codex finding")))
        second = recorder.record(_cycle(1, _advance(wt, "c1.py"), _af("Another")))

        assert first is None
        assert second is not None
        ledger = _ledger(wt)
        assert [r.kind for r in ledger.rounds] == ["loop", "confirm", "loop", "loop"]
        assert ledger.stop_reason == "max_cycles"


# ---------------------------------------------------------------------------
# Seeds (#2807)
# ---------------------------------------------------------------------------


class TestSeeds:
    def test_seed_only_cycle0_records_no_round(self, wt: Path) -> None:
        seed = _af("Claude finding")
        recorder = _recorder(wt, seeds=(seed,))

        stop = recorder.record(_cycle(0, _head(wt), seed))

        assert stop is None
        assert not (wt / LEDGER_RELATIVE_PATH).exists()

    def test_seed_plus_own_finding_records_only_the_own_finding(self, wt: Path) -> None:
        seed, own = _af("Claude finding"), _af("Codex finding")
        recorder = _recorder(wt, seeds=(seed,))

        recorder.record(_cycle(0, _head(wt), seed, own))
        recorder.record(_cycle(1, _advance(wt, "c1.py"), seed))

        ledger = _ledger(wt)
        assert [r.new_accepted for r in ledger.rounds] == [1, 1]
        assert ledger.fixed[0].identity == finding_identity(
            "MUST_FIX", _PRODUCER, "Codex finding"
        )
        # A seed that persists past the first fix cycle is charged like any
        # other finding at its new sha.
        assert ledger.fixed[1].identity == finding_identity(
            "MUST_FIX", _PRODUCER, "Claude finding"
        )


# ---------------------------------------------------------------------------
# Ledger lifecycle at the first round of an iteration
# ---------------------------------------------------------------------------


class TestLifecycle:
    def _oscillation_stop(self, worktree: Path) -> ConvergenceLedger:
        recurring = round_finding(summary="Recurring defect", file=_PRODUCER)
        ledger = _codex_ledger(worktree)
        ledger, _ = record(
            ledger,
            recurring,
            sha=git_in(worktree, "rev-parse", "HEAD~1"),
            backend="codex",
        )
        ledger, report = record(ledger, recurring, sha=_head(worktree), backend="codex")
        assert report.decision == "stop_not_converging"
        write_ledger(worktree, ledger)
        return ledger

    def test_stopped_ledger_at_open_returns_stop_without_record_round(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stopped = self._oscillation_stop(wt)

        def _must_not_record(*_args: object, **_kwargs: object) -> None:
            msg = "record_round must not run on a stopped ledger"
            raise AssertionError(msg)

        monkeypatch.setattr(codex_loop_rounds, "record_round", _must_not_record)
        recorder = _recorder(wt)

        stop = recorder.record(_cycle(0, _head(wt), _af("Bug A")))

        assert stop is not None
        assert stop.reason == REVIEW_NOT_CONVERGING_BLOCKER_REASON
        assert "convergence ledger stopped (oscillation)" in stop.details
        assert _ledger(wt) == stopped
        assert "codex_loop_round_stop: decision=stopped round=2" in recorder.tokens

    def test_stopped_ledger_released_by_new_commits_records_normally(
        self, wt: Path
    ) -> None:
        self._oscillation_stop(wt)
        head = _advance(wt, "fix.py")

        stop = _recorder(wt).record(_cycle(0, head, _af("Bug A")))

        assert stop is None
        ledger = _ledger(wt)
        assert ledger.status == "in_progress"
        assert [r.cause for r in ledger.releases] == ["new_commits"]
        assert ledger.epoch_start_round == 2
        assert ledger.fix_cycles_used == 1
        assert ledger.rounds[-1].reviewed_sha == head

    def test_converged_ledger_demoted_then_records(self, wt: Path) -> None:
        ledger = _codex_ledger(wt, confirm="none")
        ledger, report = record(ledger, sha=_head(wt), backend="codex")
        assert report.decision == "clean"
        write_ledger(wt, ledger)

        stop = _recorder(wt).record(_cycle(0, _head(wt), _af("Bug A")))

        assert stop is None
        reopened = _ledger(wt)
        assert reopened.status == "in_progress"
        assert [r.clean for r in reopened.rounds] == [True, False]

    def test_reset_token_surfaced(self, wt: Path) -> None:
        write_ledger(wt, _codex_ledger(wt, ticket_id="T-other"))
        recorder = _recorder(wt)

        recorder.record(_cycle(0, _head(wt), _af("Bug A")))

        assert "convergence_ledger_reset: ticket_mismatch" in recorder.tokens
        assert _ledger(wt).ticket_id == _TICKET

    def test_reset_token_withheld_when_nothing_was_written(self, wt: Path) -> None:
        """The post-loop gate re-reconciles and reports the reset itself."""
        write_ledger(wt, _codex_ledger(wt, ticket_id="T-other"))
        seed = _af("Claude finding")
        recorder = _recorder(wt, seeds=(seed,))

        recorder.record(_cycle(0, _head(wt), seed))

        assert recorder.tokens == []
        assert _ledger(wt).ticket_id == "T-other"

    def test_reset_token_surfaced_on_a_bookkeeping_park(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A parked result skips the post-loop gate, so the recorder reports it."""
        write_ledger(wt, _codex_ledger(wt, ticket_id="T-other"))
        _failing_engine(wt, monkeypatch)
        recorder = _recorder(wt)

        stop = recorder.record(_cycle(0, _head(wt), _af("Bug A")))

        _assert_ledger_error(stop, recorder)
        assert "convergence_ledger_reset: ticket_mismatch" in recorder.tokens


# ---------------------------------------------------------------------------
# Settled findings stay settled inside the loop
# ---------------------------------------------------------------------------


def _settled_ledger(worktree: Path) -> ConvergenceLedger:
    """A codex ledger that settled ``Bug A`` (reject) in an earlier round."""
    ledger = _codex_ledger(worktree)
    ledger, report = record(
        ledger,
        round_finding(summary="Other bug", file=_PRODUCER),
        round_finding(
            summary="Bug A", file=_PRODUCER, outcome="reject", rationale="false alarm"
        ),
        sha=git_in(worktree, "rev-parse", "HEAD~1"),
        backend="codex",
    )
    assert report.decision == "continue"
    write_ledger(worktree, ledger)
    return ledger


class TestSuppression:
    def test_settled_finding_is_suppressed(self, wt: Path) -> None:
        _settled_ledger(wt)
        verdict = _loop_verdict(_af("Bug A"), reviewed_sha=_head(wt))

        suppressed = _recorder(wt).suppress(verdict)

        assert suppressed.blocking is False
        assert suppressed.accepted[0].disposition == "rejected"

    @pytest.mark.parametrize(
        ("confirm", "decision"),
        [(None, "confirm_required"), ("none", "clean")],
        ids=["confirm-required", "clean"],
    )
    def test_clean_decision_on_dirty_pass_parks_ledger_error(
        self,
        wt: Path,
        caplog: pytest.LogCaptureFixture,
        confirm: ConfirmBackend | None,
        decision: str,
    ) -> None:
        before = _settled_ledger(wt)
        recorder = _recorder(wt, client=_client(wt, confirm=confirm))

        with caplog.at_level(logging.ERROR, logger=_LOGGER):
            stop = recorder.record(_cycle(0, _head(wt), _af("Bug A")))

        _assert_ledger_error(stop, recorder)
        assert stop is not None
        assert f"decision={decision}" in stop.details
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        _assert_ledger_untouched(wt, recorder, before)

    def test_suppress_failure_then_record_parks(
        self,
        wt: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        before = _settled_ledger(wt)
        recorder = _recorder(wt)

        def _boom(*_args: object) -> object:
            msg = "settled memory unreadable"
            raise ConvergenceError(msg)

        monkeypatch.setattr(codex_loop_rounds, "suppress_settled", _boom)
        verdict = _loop_verdict(_af("Bug A"), reviewed_sha=_head(wt))
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            returned = recorder.suppress(verdict)

        assert returned is verdict
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        _assert_ledger_error(
            recorder.record(_cycle(0, _head(wt), _af("Bug A"))), recorder
        )
        _assert_ledger_untouched(wt, recorder, before)

    def test_suppress_failure_is_sticky_across_records(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _settled_ledger(wt)
        recorder = _recorder(wt)
        recorded: list[object] = []

        def _boom(*_args: object) -> object:
            msg = "settled memory unreadable"
            raise ConvergenceError(msg)

        monkeypatch.setattr(codex_loop_rounds, "suppress_settled", _boom)
        recorder.suppress(_loop_verdict(_af("Bug A"), reviewed_sha=_head(wt)))
        monkeypatch.setattr(
            codex_loop_rounds,
            "record_round",
            lambda *_a, **_k: recorded.append("record_round"),
        )

        stops = [
            recorder.record(_cycle(0, _head(wt), _af("Bug A"))),
            recorder.record(_cycle(1, _advance(wt, "c1.py"), _af("Bug B"))),
        ]

        for stop in stops:
            _assert_ledger_error(stop, recorder)
            assert stop is not None
            assert stop.details.startswith("ConvergenceError: ")
        assert recorded == []
        _assert_ledger_untouched(wt, recorder, before)


# ---------------------------------------------------------------------------
# Bookkeeping failures: a typed, retry-eligible park, never a raise
# ---------------------------------------------------------------------------


def _corrupt(worktree: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    path = worktree / LEDGER_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")


def _foreign(worktree: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    write_ledger(worktree, _codex_ledger(worktree, loop_backend="claude-native"))


def _failing_save(_worktree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(codex_loop_rounds, "save_ledger", _raise)


def _failing_engine(_worktree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        msg = "undecided finding"
        raise ConvergenceError(msg)

    monkeypatch.setattr(codex_loop_rounds, "record_round", _raise)


class TestBookkeepingFailures:
    @pytest.mark.parametrize(
        ("arrange", "error"),
        [
            (_corrupt, "ValidationError"),
            (_foreign, "CwError"),
            (_failing_save, "OSError"),
            (_failing_engine, "ConvergenceError"),
        ],
        ids=["corrupt", "foreign", "save", "engine"],
    )
    def test_failure_parks_ledger_error(
        self,
        wt: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        arrange: Callable[[Path, pytest.MonkeyPatch], None],
        error: str,
    ) -> None:
        arrange(wt, monkeypatch)
        recorder = _recorder(wt)

        with caplog.at_level(logging.ERROR, logger=_LOGGER):
            stop = recorder.record(_cycle(0, _head(wt), _af("Bug A")))

        _assert_ledger_error(stop, recorder)
        assert stop is not None
        assert stop.details.startswith(f"{error}: ")
        assert f"convergence_bookkeeping_failed: {error}" in recorder.tokens
        assert any(r.exc_info for r in caplog.records)

    def test_suppress_swallows_a_bookkeeping_error(
        self,
        wt: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _corrupt(wt, monkeypatch)
        verdict = _loop_verdict(_af("Bug A"), reviewed_sha=_head(wt))

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            returned = _recorder(wt).suppress(verdict)

        assert returned is verdict
        assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# End to end: the real driver with the real recorder
# ---------------------------------------------------------------------------


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    worktree: Path,
    cycle0: list[AcceptedFinding],
    cycles: list[list[AcceptedFinding] | None],
    *,
    client: ClientConfig | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None, _FakeRunner, LoopRoundRecorder]:
    runner = _FakeRunner(edit=True)
    recorder = _recorder(worktree, client=client)
    result, verdict, _fake = _drive_loop(
        monkeypatch,
        worktree,
        cycle0,
        cycles,
        cycle0_sha=_head(worktree),
        reviewed_sha=lambda _index: _head(worktree),
        runner=runner,
        cycle_observer=recorder,
    )
    return result, verdict, runner, recorder


class TestThroughTheDriver:
    def test_a_round_per_dirty_cycle_then_the_gate_awaits_confirm(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.codex_clean_confirm._which_claude", lambda: None)

        _result, verdict, runner, _rec = _drive(
            monkeypatch, wt, [_af("Bug A"), _af("Bug B")], [[_af("Bug C")], []]
        )

        assert runner.fix_calls == 2
        ledger = _ledger(wt)
        assert [r.new_accepted for r in ledger.rounds] == [2, 1]
        assert ledger.fix_cycles_used == 2
        assert ledger.rounds[0].reviewed_sha != ledger.rounds[1].reviewed_sha
        assert verdict is not None

        clean, clean_verdict = _clean_result(wt, _TICKET, sha=_head(wt))
        apply_clean_confirm(
            task=_task(),
            worktree=wt,
            client=_client(wt),
            sid=_SID,
            runner=RoleAwareClaudeRunner(_no_claude),
            deadline=None,
            result=clean,
            verdict=clean_verdict,
        )
        gated = _ledger(wt)
        assert [(r.kind, r.clean) for r in gated.rounds] == [
            ("loop", False),
            ("loop", False),
            ("loop", True),
        ]
        assert gated.status == "awaiting_confirm"

    def test_oscillation_parks_the_loop(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, _verdict, runner, _rec = _drive(
            monkeypatch, wt, [_af("Bug A")], [[_af("Bug A")], []]
        )

        assert runner.fix_calls == 1
        blocker = result.blocker
        assert blocker is not None
        assert blocker.reason == REVIEW_NOT_CONVERGING_BLOCKER_REASON
        assert "convergence ledger stopped (oscillation)" in blocker.details
        assert _ledger(wt).stop_reason == "oscillation"

    def test_no_progress_parks_the_loop(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _round(cycle: int, count: int) -> list[AcceptedFinding]:
            return _stalled(cycle, count)

        result, _verdict, runner, _rec = _drive(
            monkeypatch, wt, _round(0, 2), [_round(1, 3), _round(2, 4), []]
        )

        assert runner.fix_calls == 2
        blocker = result.blocker
        assert blocker is not None
        assert blocker.reason == REVIEW_NOT_CONVERGING_BLOCKER_REASON
        assert _ledger(wt).stop_reason == "no_progress"

    def test_max_cycles_parks_the_loop(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, _verdict, runner, _rec = _drive(
            monkeypatch,
            wt,
            [_af("Bug 0")],
            [[_af("Bug 1")], [_af("Bug 2")], []],
            client=_client(wt, max_cycles=2),
        )

        assert runner.fix_calls == 2
        blocker = result.blocker
        assert blocker is not None
        assert blocker.reason == REVIEW_NOT_CONVERGING_BLOCKER_REASON
        assert _ledger(wt).stop_reason == "max_cycles"


def test_reviewed_shas_are_the_pre_fix_and_post_fix_heads(
    make_git_repo: Callable[..., Path],
) -> None:
    """Deferred premise (#2816): real reviews, real fix commit, real shas.

    The fixed entry records the pre-fix head (cycle 0's reviewed sha) and the
    re-review after the fix commit reviewed the new HEAD, so oscillation
    compares two distinct shas.
    """
    worktree = _exclude_cw(_worktree(make_git_repo, "wt-premise"))
    cycle0_head = _head(worktree)
    runner = _FixLoopRunner([_MF_DOC, _MF_DOC], fix_behaviors=[_editor()])

    out, _verdict = run_review_with_fix_loop(
        runner=runner,
        task=_task(),
        worktree=worktree,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="s-premise",
        fix_loop_enabled=True,
        cycle_observer=_recorder(worktree),
    )

    post_fix_head = _head(worktree)
    assert post_fix_head != cycle0_head
    ledger = _ledger(worktree)
    (fixed,) = ledger.fixed
    assert fixed.reviewed_sha == cycle0_head
    assert [r.reviewed_sha for r in ledger.rounds] == [cycle0_head, post_fix_head]
    assert ledger.stop_reason == "oscillation"
    assert out.blocker is not None
    assert out.blocker.reason == REVIEW_NOT_CONVERGING_BLOCKER_REASON
    assert runner.fix_calls == 1
