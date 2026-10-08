"""Bounded store shared by the lockless capture pre-passes (#2545, #2548).

A capture pass reads values live before ``sessions_lock`` and keeps them here;
the in-lock consumer only looks them up. The store owns the mechanics every
such pre-pass needs: a wall-clock budget, a per-pass capture cap, the capture
timestamp, and a freshness bound on lookup. Domain adapters
(``gate_plan_probes.PlanProbes``, ``dirty_checks.DirtyChecks``) supply the key
and payload types and translate :class:`ProbeStoreUnavailableError` into their
own public error.

A leaf module: standard library only, and it binds no logger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class ProbeStoreUnavailableError(Exception):
    """The shared store cannot provide a usable captured value."""

    def __init__(self, reason: str, *, age: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.age = age


@dataclass(frozen=True)
class StoredProbe[PayloadT]:
    payload: PayloadT
    captured_at: datetime


class BoundedProbeStore[KeyT, PayloadT]:
    """Shared deadline, attempt-count, timestamp and freshness mechanics.

    Domain adapters provide the key and payload types, while translating the
    small set of store failures into their own public exception and message.
    The reader is called only after the attempt is counted and the timestamp
    is stamped, so reader failures retain the same accounting semantics.
    """

    def __init__(
        self,
        *,
        budget_seconds: float | None,
        max_captures: int | None,
        max_age_seconds: float,
    ) -> None:
        self.budget_seconds = budget_seconds
        self.max_captures = max_captures
        self.max_age_seconds = max_age_seconds
        self._deadline = (
            None if budget_seconds is None else monotonic() + budget_seconds
        )
        self._captures = 0
        self._probes: dict[KeyT, StoredProbe[PayloadT]] = {}

    @property
    def captures(self) -> int:
        return self._captures

    @property
    def keys(self) -> frozenset[KeyT]:
        return frozenset(self._probes)

    def capture(
        self,
        key: KeyT,
        *,
        read: Callable[[datetime], PayloadT],
    ) -> PayloadT:
        self.ensure_capture_available()
        self._captures += 1
        captured_at = datetime.now(UTC)
        payload = read(captured_at)
        self._probes[key] = StoredProbe(payload=payload, captured_at=captured_at)
        return payload

    def ensure_capture_available(self) -> None:
        """Raise if the next capture would exceed its budget or cap."""
        if self._deadline is not None and monotonic() >= self._deadline:
            reason = "budget"
            raise ProbeStoreUnavailableError(reason)
        if self.max_captures is not None and self._captures >= self.max_captures:
            reason = "cap"
            raise ProbeStoreUnavailableError(reason)

    def lookup(self, key: KeyT) -> PayloadT:
        probe = self._probes.get(key)
        if probe is None:
            reason = "missing"
            raise ProbeStoreUnavailableError(reason)
        age = (datetime.now(UTC) - probe.captured_at).total_seconds()
        if not 0 <= age < self.max_age_seconds:
            reason = "stale"
            raise ProbeStoreUnavailableError(reason, age=age)
        return probe.payload
