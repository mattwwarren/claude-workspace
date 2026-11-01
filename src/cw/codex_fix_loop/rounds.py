"""The per-cycle observation seam of the codex fix loop (#2816).

Declarations only. :func:`cw.codex_fix_loop._driver.run_review_with_fix_loop`
hands each dirty pass (cycle 0 and every re-review that still has open
MUST_FIX findings) to an optional :class:`CycleObserver` as a
:class:`CycleRound`. The observer may answer with a :class:`RoundStop`, which
the driver turns into a park; ``None`` lets the loop continue. The same
observer supplies the settled-finding suppression the driver forwards to every
review pass of the loop.

The clean-mode codex lane's observer is
:class:`cw.codex_loop_rounds.LoopRoundRecorder`, which records each cycle as a
convergence-ledger round. Blocking mode passes no observer, so the loop is
unchanged there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.codex_fix_loop.convergence import _OpenFindingKey
    from cw.review_findings import AcceptedFinding, ReviewVerdict


class CycleRound(NamedTuple):
    """One dirty pass of the loop: its cycle number, verdict and open findings.

    ``cycle`` is 0 for the initial review and N for the re-review after fix
    cycle N. ``open_findings`` is the cross-cycle tracker's state after this
    pass (never empty: clean passes are not observed).
    """

    cycle: int
    verdict: ReviewVerdict
    open_findings: Mapping[_OpenFindingKey, AcceptedFinding]


class RoundStop(NamedTuple):
    """Why the observer stopped the loop: the park's blocker reason and details.

    ``details`` is appended to the rendered survivor verdict in the park's
    ``Blocker.details``; ``retry_eligible`` is the blocker's flag.
    """

    reason: str
    details: str
    retry_eligible: bool | None


class CycleObserver(Protocol):
    """Sees every dirty pass of the loop and may stop it."""

    def record(self, cycle_round: CycleRound) -> RoundStop | None:
        """Observe one dirty pass; a :class:`RoundStop` parks the loop."""
        ...

    def suppress(self, verdict: ReviewVerdict) -> ReviewVerdict:
        """Stamp the findings the observer already settled (synthesis seam)."""
        ...
