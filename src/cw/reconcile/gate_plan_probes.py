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
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

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
_PLAN_KEY_PARTS = 4


class _ProbeStore[KeyT, PayloadT]:
    """Shared bounded, expiring storage mechanics for lockless probes.

    The domain wrappers supply their key/payload validators and error factory;
    this class owns only the capture budget, attempt count, timestamp lookup,
    and freshness check. It is deliberately private so each probe domain can
    keep its own public payload and source types.
    """

    def __init__(
        self,
        *,
        budget_seconds: float | None,
        max_captures: int | None,
        max_age_seconds: float,
        validate_key: Callable[[KeyT], bool],
        validate_payload: Callable[[PayloadT], bool],
        error_factory: Callable[[str], Exception],
    ) -> None:
        self.budget_seconds = budget_seconds
        self.max_captures = max_captures
        self._max_age_seconds = max_age_seconds
        self._validate_key = validate_key
        self._validate_payload = validate_payload
        self._error_factory = error_factory
        self._deadline = (
            None if budget_seconds is None else monotonic() + budget_seconds
        )
        self._captures = 0
        self._probes: dict[KeyT, PayloadT] = {}

    @property
    def captures(self) -> int:
        """How many live reads this store has attempted, failed ones included."""
        return self._captures

    @property
    def captured_keys(self) -> frozenset[KeyT]:
        """The keys whose payloads were captured successfully."""
        return frozenset(self._probes)

    def reserve_capture(self, *, budget_message: str, cap_message: str) -> None:
        """Check limits and count one live read before it starts."""
        if self._deadline is not None and monotonic() >= self._deadline:
            raise self._error_factory(budget_message)
        if self.max_captures is not None and self._captures >= self.max_captures:
            raise self._error_factory(cap_message)
        self._captures += 1

    def store(self, key: KeyT, payload: PayloadT) -> None:
        """Validate and retain one captured payload."""
        if not self._validate_key(key):
            msg = "invalid probe key"
            raise ValueError(msg)
        if not self._validate_payload(payload):
            msg = "invalid probe payload"
            raise ValueError(msg)
        self._probes[key] = payload

    def lookup(
        self,
        key: KeyT,
        *,
        missing_message: str,
        stale_message: Callable[[float], str],
        captured_at: Callable[[PayloadT], datetime],
    ) -> PayloadT:
        """Return a fresh payload or raise the domain's unavailable error."""
        if not self._validate_key(key):
            raise self._error_factory(missing_message)
        payload = self._probes.get(key)
        if payload is None:
            raise self._error_factory(missing_message)
        age = (datetime.now(UTC) - captured_at(payload)).total_seconds()
        if not 0 <= age < self._max_age_seconds:
            raise self._error_factory(stale_message(age))
        return payload


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
        self._store = _ProbeStore[
            _ProbeKey, PlanProbe
        ](
            budget_seconds=budget_seconds,
            max_captures=max_captures,
            max_age_seconds=PLAN_PROBE_MAX_AGE_SECONDS,
            validate_key=lambda key: len(key) == _PLAN_KEY_PARTS,
            validate_payload=lambda probe: isinstance(probe, PlanProbe),
            error_factory=PlanProbeUnavailableError,
        )
        self.budget_seconds = budget_seconds
        self.max_captures = max_captures

    @property
    def captured_keys(self) -> frozenset[tuple[str, str]]:
        """The client/ticket pairs of probes captured so far (diagnostics)."""
        return frozenset(
            (client, ticket_id)
            for client, ticket_id, *_ in self._store.captured_keys
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
        budget_message = (
            ""
            if self.budget_seconds is None
            else (
                f"the {self.budget_seconds:.0f}s plan prefetch budget is spent;"
                f" {who} was not read"
            )
        )
        cap_message = (
            ""
            if self.max_captures is None
            else (
                f"the {self.max_captures}-read per-tick plan prefetch cap is"
                f" reached; {who} was not read"
            )
        )
        self._store.reserve_capture(
            budget_message=budget_message,
            cap_message=cap_message,
        )
        captured_at = datetime.now(UTC)
        body = read(task)
        self._store.store(
            _probe_key(task, fingerprint),
            PlanProbe(body=body, captured_at=captured_at),
        )
        return body

    def lookup(self, task: TicketTask, fingerprint: str) -> str | None:
        """Return *task*'s captured body if still usable. Never runs a subprocess.

        Usable means captured under this same client, ticket, session and
        draft fingerprint, and aged in ``[0, PLAN_PROBE_MAX_AGE_SECONDS)``.
        Otherwise raises ``PlanProbeUnavailableError``.
        """
        who = f"{task.client}/{task.ticket_id}"
        missing_message = (
            f"no plan prefetch was captured for {who} under this session"
            " and plan fingerprint"
        )
        probe = self._store.lookup(
            _probe_key(task, fingerprint),
            missing_message=missing_message,
            stale_message=lambda age: (
                f"the plan prefetch for {who} is unusable at age {age:.1f}s"
            ),
            captured_at=lambda captured: captured.captured_at,
        )
        return probe.body


def lookup_plan_probe(probes: PlanProbes | None) -> PlanBodySource:
    """The in-lock body source: *probes*' lookup, or an always-miss one.

    ``None`` means nothing was captured, so every lookup misses and the
    candidate is skipped for the tick: fail closed.
    """
    return (probes if probes is not None else PlanProbes()).lookup
