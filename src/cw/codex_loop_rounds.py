"""Every codex fix-loop cycle as a clean-mode ledger round (#2816).

:class:`LoopRoundRecorder` is the clean-mode codex lane's
:class:`~cw.codex_fix_loop.rounds.CycleObserver`. The fix loop hands it each
dirty pass (cycle 0, then every re-review that still has open MUST_FIX
findings); it records the pass as a ``kind="loop"`` round on the convergence
ledger at ``<worktree>/.cw/review-convergence.json``, so the ledger's
no-progress, oscillation and ``max_cycles`` detectors see the loop's own
cycles. The final clean pass is not recorded here: the post-loop gate
(:func:`cw.codex_clean_confirm.apply_clean_confirm`) still records it.

It reuses the gate's ledger helpers (``_Gate``, the reconcile, the diff-base
pin, the disposition normalizer) rather than a second copy, and lives beside
that module only because :mod:`cw.codex_clean_confirm` is already large.

Lifecycle, per loop call (one recorder each):

- The first ``record`` or ``suppress`` reconciles the ledger exactly as the
  gate does, at the cycle-0 reviewed sha (the pre-fix HEAD). A ledger that is
  still stopped returns a :class:`~cw.codex_fix_loop.rounds.RoundStop` with no
  round recorded; a converged one is demoted before the first round.
- Each recorded round is pinned (first write only), recorded and saved.
- Claude seeds (#2807) were already charged by the dirty confirm round, so
  they are left out of the cycle-0 round; a cycle 0 with nothing else records
  nothing.

Decisions: ``continue`` lets the loop run; a ``stop_*`` decision parks
``review_not_converging`` with the gate's stop details. A clean or
confirm-required decision on a dirty pass parks
``codex_convergence_ledger_error``: the ledger has already moved to
``converged``/``awaiting_confirm`` at a sha with open MUST_FIX findings, so
continuing would fail open. Any ledger, git or file failure parks the same way
(retry-eligible), never a raise; ``suppress`` instead logs it and returns the
verdict unchanged, and the next ``record`` parks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cw.auto_dev_result.schema._vocab import REVIEW_NOT_CONVERGING_BLOCKER_REASON
from cw.codex_clean_confirm import (
    _BOOKKEEPING_ERRORS,
    BOOKKEEPING_FAILED_FRICTION_PREFIX,
    LEDGER_RESET_FRICTION_PREFIX,
    _decided,
    _Gate,
    _load_reconciled_ledger,
    _now,
    _pinned,
    stop_details,
)
from cw.codex_fix_loop.convergence import _open_finding_key
from cw.codex_fix_loop.rounds import RoundStop
from cw.codex_review import CODEX_CONVERGENCE_LEDGER_ERROR
from cw.models import CODEX_BACKEND
from cw.review_convergence import (
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_STOPPED,
    STOP_DECISIONS,
    demote_for_rereview,
    from_verdict,
    frozen_counts_from_verdict,
    one_line,
    record_round,
    save_ledger,
    suppress_settled,
    unread_from_verdict,
)
from cw.review_convergence._models import DECISION_CONTINUE

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.claude_review import ClaudeRunner
    from cw.codex_fix_loop.convergence import _OpenFindingKey
    from cw.codex_fix_loop.rounds import CycleRound
    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger, RoundFinding, RoundReport
    from cw.review_findings import AcceptedFinding, ReviewVerdict

_log = logging.getLogger(__name__)

_MUST_FIX = "MUST_FIX"
#: MUST_FIX dispositions the open-findings tracker can hold (``fixed``) or a
#: survivor or undecided stamp of one; anything else was decided upstream.
_UNDECIDED_MUST_FIX = frozenset({"fixed", "dropped", "unresolved"})
#: Round outcomes that keep a round dirty: without one there is nothing to record.
_GATING_OUTCOMES = frozenset({"fix", "operator_action"})


def _round_findings(
    cycle_round: CycleRound, excluded: frozenset[_OpenFindingKey]
) -> list[RoundFinding]:
    """The adjudicated round findings of one dirty pass.

    A MUST_FIX the open-findings tracker holds is a ``fix`` (each identity
    once). One it does not hold (diverted to the debt ledger by the admission
    gate) or whose key is *excluded* (a seed at cycle 0) is left out. Every
    other finding goes through the gate's ``_decided``: rejected and deferred
    pass through, a SHOULD_FIX is stamped deferred.
    """
    open_keys = set(cycle_round.open_findings) - excluded
    kept: list[AcceptedFinding] = []
    for item in cycle_round.verdict.accepted:
        undecided = (
            item.finding.severity == _MUST_FIX
            and item.disposition in _UNDECIDED_MUST_FIX
        )
        if not undecided:
            kept.append(_decided(item))
            continue
        key = _open_finding_key(item.finding)
        if key in open_keys:
            open_keys.discard(key)
            kept.append(item.model_copy(update={"disposition": "fixed"}))
    decided = cycle_round.verdict.model_copy(update={"accepted": kept})
    findings, _unread = from_verdict(decided)
    return findings


@dataclass
class LoopRoundRecorder:
    """Records each dirty codex pass of one loop call as a ledger round.

    Implements :class:`~cw.codex_fix_loop.rounds.CycleObserver`. *gate* is
    its own ``_Gate`` (identity, ledger state, tokens); *seed_keys* are the
    open-finding keys of the Claude seeds merged into this call's cycle 0.
    """

    gate: _Gate
    seed_keys: frozenset[_OpenFindingKey] = frozenset()
    _saved: bool = field(default=False, init=False)

    @property
    def tokens(self) -> list[str]:
        """Friction lines for the run's result (reset tokens only once written).

        The reconcile's reset token is withheld until the recorder writes the
        ledger, because otherwise the post-loop gate re-reconciles and reports
        it itself.
        """
        if self._saved:
            return list(self.gate.tokens)
        return [
            t
            for t in self.gate.tokens
            if not t.startswith(LEDGER_RESET_FRICTION_PREFIX)
        ]

    def suppress(self, verdict: ReviewVerdict) -> ReviewVerdict:
        """*verdict* with every finding the ledger already settled stamped.

        A bookkeeping failure is logged and the verdict returned unchanged;
        the next :meth:`record` then parks on the same failure.
        """
        try:
            ledger = self._open_ledger(verdict.reviewed_sha)
            suppressed, _adjudications = suppress_settled(ledger, verdict)
        except _BOOKKEEPING_ERRORS:
            _log.warning(
                "codex loop round suppression skipped: ledger unreadable "
                "ticket=%s client=%s session=%s",
                self.gate.task.ticket_id,
                self.gate.client.name,
                self.gate.sid,
                exc_info=True,
            )
            return verdict
        return suppressed

    def record(self, cycle_round: CycleRound) -> RoundStop | None:
        """Record one dirty pass; a :class:`RoundStop` parks the loop."""
        try:
            return self._record(cycle_round)
        except _BOOKKEEPING_ERRORS as exc:
            return self._bookkeeping_stop(exc)

    def _open_ledger(self, head: str) -> ConvergenceLedger:
        """The iteration's ledger, reconciled at *head* on first use."""
        if self.gate.ledger is None:
            return _load_reconciled_ledger(self.gate, head)
        return self.gate.ledger

    def _record(self, cycle_round: CycleRound) -> RoundStop | None:
        verdict = cycle_round.verdict
        ledger = self._open_ledger(verdict.reviewed_sha)
        if ledger.status == LEDGER_STATUS_STOPPED:
            return self._stop_token(
                RoundStop(
                    REVIEW_NOT_CONVERGING_BLOCKER_REASON, stop_details(ledger), False
                ),
                decision="stopped",
                round_number=len(ledger.rounds),
            )
        excluded = self.seed_keys if cycle_round.cycle == 0 else frozenset()
        findings = _round_findings(cycle_round, excluded)
        if not any(finding.outcome in _GATING_OUTCOMES for finding in findings):
            return None
        if ledger.status == LEDGER_STATUS_CONVERGED:
            ledger = demote_for_rereview(ledger)
        ledger = _pinned(self.gate, ledger)
        ledger, report = self._save_round(cycle_round, ledger, findings)
        return self._stop_for(ledger, report)

    def _save_round(
        self,
        cycle_round: CycleRound,
        ledger: ConvergenceLedger,
        findings: list[RoundFinding],
    ) -> tuple[ConvergenceLedger, RoundReport]:
        verdict = cycle_round.verdict
        ledger, report = record_round(
            ledger,
            kind="loop",
            backend=CODEX_BACKEND,
            reviewed_sha=verdict.reviewed_sha,
            findings=findings,
            roster=[record.reviewer_role for record in verdict.agents_run],
            now=_now(),
            unread=unread_from_verdict(verdict),
            frozen=frozen_counts_from_verdict(verdict)
            if cycle_round.cycle == 0
            else None,
        )
        save_ledger(self.gate.ledger_path, ledger)
        self.gate.ledger = ledger
        self._saved = True
        _log.info(
            "codex loop round recorded ticket=%s client=%s session=%s cycle=%s "
            "round=%s decision=%s new_accepted=%s",
            self.gate.task.ticket_id,
            self.gate.client.name,
            self.gate.sid,
            cycle_round.cycle,
            report.round,
            report.decision,
            report.new_accepted,
        )
        return ledger, report

    def _stop_for(
        self, ledger: ConvergenceLedger, report: RoundReport
    ) -> RoundStop | None:
        """``None`` to continue, or the park a recorded decision calls for."""
        if report.decision == DECISION_CONTINUE:
            return None
        if report.decision in STOP_DECISIONS:
            stop = RoundStop(
                REVIEW_NOT_CONVERGING_BLOCKER_REASON, stop_details(ledger), False
            )
            return self._stop_token(
                stop, decision=report.decision, round_number=report.round
            )
        # Clean or confirm-required on a dirty pass: the ledger now names a
        # dirty sha as clean. Fail closed (see the module docstring).
        _log.error(
            "codex loop round decided %s on a pass with open MUST_FIX findings "
            "ticket=%s client=%s session=%s round=%s",
            report.decision,
            self.gate.task.ticket_id,
            self.gate.client.name,
            self.gate.sid,
            report.round,
        )
        self.gate.tokens.append(
            f"{BOOKKEEPING_FAILED_FRICTION_PREFIX} decision={report.decision}"
        )
        stop = RoundStop(
            CODEX_CONVERGENCE_LEDGER_ERROR,
            f"unexpected ledger decision={report.decision} on round {report.round}, "
            "a codex loop pass with open MUST_FIX findings",
            True,
        )
        return self._stop_token(
            stop, decision=report.decision, round_number=report.round
        )

    def _stop_token(
        self, stop: RoundStop, *, decision: str, round_number: int
    ) -> RoundStop:
        self.gate.tokens.append(
            f"codex_loop_round_stop: decision={decision} round={round_number}"
        )
        return stop

    def _bookkeeping_stop(self, exc: Exception) -> RoundStop:
        _log.exception(
            "codex loop round bookkeeping failed ticket=%s client=%s session=%s",
            self.gate.task.ticket_id,
            self.gate.client.name,
            self.gate.sid,
        )
        self._saved = True
        self.gate.tokens.append(
            f"{BOOKKEEPING_FAILED_FRICTION_PREFIX} {type(exc).__name__}"
        )
        return RoundStop(
            CODEX_CONVERGENCE_LEDGER_ERROR,
            f"{type(exc).__name__}: {one_line(str(exc))}",
            True,
        )


def loop_round_recorder(
    *,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    sid: str,
    runner: ClaudeRunner,
    deadline: float | None,
    seed_findings: Sequence[AcceptedFinding],
) -> LoopRoundRecorder:
    """A recorder for one loop call of a clean-mode codex run.

    The ``_Gate`` fields mirror the post-loop gate's; *runner* and *deadline*
    are carried for that shape only (the recorder never calls Claude).
    """
    gate = _Gate(
        task=task,
        worktree=worktree,
        client=client,
        sid=sid,
        runner=runner,
        deadline=deadline,
    )
    return LoopRoundRecorder(
        gate=gate,
        seed_keys=frozenset(_open_finding_key(seed.finding) for seed in seed_findings),
    )
