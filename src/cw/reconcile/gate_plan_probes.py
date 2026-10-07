"""Lockless prefetch of the gate recipes' plan-of-record read (#2545).

The ``auto_adopt_clean_plan`` recipe needs the plan-of-record body to decide
how a plan gate is released (reviewed -> IMPL, unreviewed -> back to PLAN).
Reading it runs ``gh issue view``/``gh api user`` and, for the ``.cw/plan.md``
fallback, ``git branch --show-current``. None of that may run under
``sessions_lock`` (ADR-0019 invariant 3), where the recipe's detect runs.

So the read is split, the same way the codex clean probes are (#2563):

- **Capture, lockless.** ``reconcile()`` re-runs the plan detect before it
  takes ``sessions_lock``, with :meth:`PlanProbes.capture` as the body source.
  It reads each candidate's body live and keeps it here.
- **Consume, in-lock.** The in-lock detect uses :meth:`PlanProbes.lookup`,
  which never runs a subprocess.
- **Defer on a miss.** A probe that is missing, bound to another claim or
  draft, or stale raises :class:`PlanProbeUnavailableError`, and the detect
  skips that candidate for this tick. It is retried on the next one. Expiry
  only defers; it is never a disposition (ADR-0014), so nothing is parked or
  released because time passed.

A captured ``None`` body (no reviewed plan found) is a hit, not a miss: the
recipe releases that plan as unreviewed, exactly as a live ``None`` did.

A leaf module: it must not import ``cw.reconcile.gate_recipes``, which
imports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.reconcile.probe_store import BoundedProbeStore as _BoundedProbeStore
from cw.reconcile.probe_store import (
    ProbeStoreUnavailableError as _ProbeStoreUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from cw.models import TicketTask

# Most live plan-of-record reads one capture pass attempts. A read that
# returns ``None`` (or raises) still counts. A live read is normally 1-3 s but
# can reach ~60 s (two ``gh`` calls at a 30 s timeout each, plus an untimed
# ``git`` call), and the pre-pass also runs for ``cw status``/``list``/
# ``start``/``doctor``. Plan-gate parks are operator-scale, and a released
# candidate leaves the set, so a capped-out candidate waits a tick or two.
PLAN_PREFETCH_MAX_PER_TICK = 5
# Wall-clock budget for one capture pass, checked before each live read. Half
# the codex clean-probe pre-pass's budget: the two pre-passes run back to back.
PLAN_PREFETCH_BUDGET_SECONDS: float = 30.0
# How old a captured plan probe may be when it is consumed under the lock.
# Strict ``<``: a probe exactly this old is stale, and a negative age (the
# clock went backwards) fails closed too. A freshness bound, not a clock that
# decides anything: an aged-out probe only defers its candidate.
PLAN_PROBE_MAX_AGE_SECONDS: float = 120.0


class PlanProbeUnavailableError(Exception):
    """No usable plan prefetch for a candidate: capped, missing, mismatched or stale.

    A plain ``Exception``, deliberately not a ``CwError``, so no caller's broad
    ``except CwError`` swallows it: the plan detect catches it by name and
    skips the candidate for this tick.
    """


@dataclass(frozen=True)
class PlanProbe:
    """One candidate's plan-of-record body, read before the lock.

    ``body`` is exactly what the live read returned; ``None`` means no plan of
    record was found, which is a usable answer. ``captured_at`` is stamped
    before the read starts, so the probe's age is measured conservatively from
    the start of the observation.
    """

    body: str | None
    captured_at: datetime


# Reads one task's plan-of-record body live (runs gh/git; lockless only).
type PlanBodyReader = Callable[[TicketTask], str | None]
# How the plan detect obtains a candidate's body, given its draft
# fingerprint: a lockless capture (``PlanProbes.capture`` bound to a reader)
# or an in-lock lookup that never runs a subprocess (``PlanProbes.lookup``).
type PlanBodySource = Callable[[TicketTask, str], str | None]

type _ProbeKey = tuple[str, str, str | None, str]


def _probe_key(task: TicketTask, fingerprint: str) -> _ProbeKey:
    """The claim identity a plan probe is bound to.

    A re-claimed session or an amended draft between capture and the lock
    yields a different key, so the stale body is never used for it.
    """
    return task.client, task.ticket_id, task.session_id, fingerprint


class PlanProbes:
    """Plan-of-record bodies captured lockless, keyed by claim identity.

    One pass captures (:meth:`capture`, runs the live reader, lockless only),
    then the in-lock detect looks up (:meth:`lookup`, never runs a
    subprocess). With *budget_seconds* set, captures stop once that much
    monotonic time has passed since construction; with *max_captures* set,
    they stop after that many live reads.
    """

    def __init__(
        self,
        *,
        budget_seconds: float | None = None,
        max_captures: int | None = None,
    ) -> None:
        self._store: _BoundedProbeStore[_ProbeKey, PlanProbe] = _BoundedProbeStore(
            budget_seconds=budget_seconds,
            max_captures=max_captures,
            max_age_seconds=PLAN_PROBE_MAX_AGE_SECONDS,
        )

    @property
    def budget_seconds(self) -> float | None:
        return self._store.budget_seconds

    @property
    def max_captures(self) -> int | None:
        return self._store.max_captures

    @property
    def captured_keys(self) -> frozenset[tuple[str, str]]:
        """The client/ticket pairs of probes captured so far (diagnostics)."""
        return frozenset(
            (client, ticket_id) for client, ticket_id, *_ in self._store.keys
        )

    @property
    def captures(self) -> int:
        """How many live reads this store has attempted, failed ones included."""
        return self._store.captures

    def capture(
        self, task: TicketTask, fingerprint: str, *, read: PlanBodyReader
    ) -> str | None:
        """Read *task*'s plan-of-record body live and keep it. Lockless only.

        Raises ``PlanProbeUnavailableError`` before reading once the budget is
        spent or the per-tick cap is reached. The attempt is counted before
        *read* runs, so a read that raises still counts toward the cap; its
        exception propagates and nothing is stored.
        """
        who = f"{task.client}/{task.ticket_id}"
        try:
            return self._store.capture(
                _probe_key(task, fingerprint),
                read=lambda captured_at: PlanProbe(
                    body=read(task), captured_at=captured_at
                ),
            ).body
        except _ProbeStoreUnavailableError as err:
            if err.reason == "budget":
                budget_seconds = self.budget_seconds
                if budget_seconds is None:
                    msg = "the plan prefetch budget is unavailable"
                    raise PlanProbeUnavailableError(msg) from err
                msg = (
                    f"the {budget_seconds:.0f}s plan prefetch budget is spent;"
                    f" {who} was not read"
                )
            else:
                max_captures = self.max_captures
                if max_captures is None:
                    msg = "the plan prefetch cap is unavailable"
                    raise PlanProbeUnavailableError(msg) from err
                msg = (
                    f"the {max_captures}-read per-tick plan prefetch cap is"
                    f" reached; {who} was not read"
                )
            raise PlanProbeUnavailableError(msg) from err

    def lookup(self, task: TicketTask, fingerprint: str) -> str | None:
        """Return *task*'s captured body if still usable. Never runs a subprocess.

        Usable means captured under this same client, ticket, session and
        draft fingerprint, and aged in ``[0, PLAN_PROBE_MAX_AGE_SECONDS)``.
        Otherwise raises ``PlanProbeUnavailableError``.
        """
        who = f"{task.client}/{task.ticket_id}"
        try:
            return self._store.lookup(_probe_key(task, fingerprint)).body
        except _ProbeStoreUnavailableError as err:
            if err.reason == "missing":
                msg = (
                    f"no plan prefetch was captured for {who} under this session"
                    " and plan fingerprint"
                )
            else:
                age = err.age
                if age is None:
                    msg = f"the plan prefetch for {who} is unusable"
                else:
                    msg = f"the plan prefetch for {who} is unusable at age {age:.1f}s"
            raise PlanProbeUnavailableError(msg) from err


def lookup_plan_probe(probes: PlanProbes | None) -> PlanBodySource:
    """The in-lock body source: *probes*' lookup, or an always-miss one.

    ``None`` means nothing was captured, so every lookup misses and the
    candidate is skipped for the tick: fail closed.
    """
    return (probes if probes is not None else PlanProbes()).lookup
