"""Tick-scoped lockless snapshot of the daemon roster (#2550).

``reconcile()`` used to call ``claude agents --json`` inside
``_reconcile_locked``, under ``sessions_lock`` (an ADR-0019 invariant 3
exception). It is split the way the other lockless pre-passes are:

- **Capture, lockless.** ``reconcile()`` builds one :class:`RosterSnapshot`
  per tick. The dirty-check pre-pass reads the roster through
  :meth:`RosterSnapshot.shared`, and the final pre-lock step,
  :meth:`RosterSnapshot.refresh`, runs after the harvest pass and re-reads
  only when the held reading is not a success younger than
  ``ROSTER_REUSE_MAX_AGE_SECONDS``. A fast tick makes one call; a slow one
  makes two, both before the lock.
- **Consume, in-lock.** ``_reconcile_locked`` calls :func:`lookup_roster`,
  which never runs a subprocess.
- **Fail closed.** A lookup raises :class:`RosterUnavailableError` for a
  failed read (``reason="failed"``: an error, a timeout, unparseable output),
  for no capture at all (``"missing"``) and for one at least
  ``ROSTER_SNAPSHOT_MAX_AGE_SECONDS`` old (``"stale"``, a long lock wait). A
  failed read keeps today's daemon-outage path; a stale or missing snapshot
  skips every roster-dependent sweep for the tick (``skips_tick``).

TOCTOU and the spawn grace: the in-lock read used to run milliseconds before
the phantom sweep; a snapshot is now up to ``ROSTER_SNAPSHOT_MAX_AGE_SECONDS``
old. A session spawned or resumed after the capture is absent from it, so
``compute_drift`` is passed the capture time and spares any session whose
``started_at``/``resumed_at``/``surface_bound_at`` falls within
``ROSTER_REGISTRATION_SLACK_SECONDS`` before it or later; the age bound keeps
the snapshot inside the 30 s spawn grace as well. A run of stale or missing
snapshots pages the operator once (``cw.reconcile.roster_skip_streak``,
#3335).

A leaf module: it imports ``cw.reconcile.probe_store`` and the standard
library only.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cw.reconcile.probe_store import BoundedProbeStore, ProbeStoreUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

_log = logging.getLogger(__name__)

# How old a roster capture may be when consumed under the lock: the 30 s
# ``SPAWN_GRACE_SECONDS`` minus 10 s of margin for daemon registration lag and
# clock skew. Invariant: ``CLAUDE_AGENTS_TIMEOUT_SECONDS`` (10 s) <
# ``ROSTER_SNAPSHOT_MAX_AGE_SECONDS`` < ``SPAWN_GRACE_SECONDS``. Strict ``<``:
# a capture exactly this old is stale, and a negative age (the clock went
# backwards) is stale too. A stale snapshot only skips the tick's
# roster-dependent sweeps; it never authorizes an act.
ROSTER_SNAPSHOT_MAX_AGE_SECONDS: float = 20.0
# How young a successful capture must be for the final pre-lock refresh to
# reuse it instead of re-reading. Leaves at least 15 s of the max age for the
# lock wait and the in-lock pre-guard sweeps. Read at call time.
ROSTER_REUSE_MAX_AGE_SECONDS: float = 5.0

_KEY = "roster"

# Reads the live daemon roster (``claude agents --json``); lockless only.
type RosterReader = Callable[[], list[dict[str, object]]]

type RosterUnavailableReason = Literal["failed", "stale", "missing"]

# Consequence each reason has for the tick, for the one in-lock warning.
_CONSEQUENCE: dict[RosterUnavailableReason, str] = {
    "failed": "treating as a daemon outage",
    "stale": "skipping the roster-dependent sweeps this tick",
    "missing": "skipping the roster-dependent sweeps this tick",
}


class RosterUnavailableError(Exception):
    """No usable roster for this tick: the read failed, or is missing or stale.

    A plain ``Exception``, deliberately not a ``CwError``, so no caller's broad
    ``except CwError`` swallows it (mirrors ``DirtyCheckUnavailableError``).
    *reason* is ``"failed"`` (the reader raised), ``"missing"`` or
    ``"stale"``; the message is the cause.
    """

    def __init__(
        self, message: str, *, reason: RosterUnavailableReason = "failed"
    ) -> None:
        super().__init__(message)
        self.reason: RosterUnavailableReason = reason

    @property
    def skips_tick(self) -> bool:
        """True for a stale or missing snapshot (#2550).

        A failed read is a daemon fact and keeps flowing through the outage
        heuristic; a stale or missing snapshot is a lock-wait artifact, so it
        skips the tick: no roster-dependent sweep may run on its fabricated
        empty roster.
        """
        return self.reason != "failed"


# Everything a roster read may raise that means "no roster this time". The
# one definition, shared by the snapshot capture and the dirty-check pre-pass
# (``prepass_phantom_ids``), which also sees ``RosterUnavailableError`` from
# the snapshot-bound reader. Anything else propagates.
ROSTER_READ_ERRORS: tuple[type[Exception], ...] = (
    subprocess.CalledProcessError,
    json.JSONDecodeError,
    FileNotFoundError,
    subprocess.TimeoutExpired,
    RosterUnavailableError,
)


@dataclass(frozen=True, slots=True)
class RosterReading:
    """One capture attempt: the agents, or ``None`` with the failure text."""

    agents: list[dict[str, object]] | None
    failure: str | None = None


class RosterSnapshot:
    """The daemon roster captured lockless, once or twice per tick.

    One key in a :class:`BoundedProbeStore` with no budget and no cap; the
    latest attempt always wins, so a failed re-read overwrites an earlier
    success.
    """

    def __init__(self) -> None:
        self._store: BoundedProbeStore[str, RosterReading] = BoundedProbeStore(
            budget_seconds=None,
            max_captures=None,
            max_age_seconds=ROSTER_SNAPSHOT_MAX_AGE_SECONDS,
        )
        self._captured_at: datetime | None = None

    @property
    def captures(self) -> int:
        """How many capture attempts ran, failed ones included."""
        return self._store.captures

    @property
    def captured_at(self) -> datetime | None:
        """When the latest attempt was stamped (before the read), or ``None``."""
        return self._captured_at

    def capture(self, reader: RosterReader) -> RosterReading:
        """Run *reader* now and keep its result. Lockless only.

        A ``ROSTER_READ_ERRORS`` exception becomes a failed reading; anything
        else propagates and stores nothing.
        """

        def _read(captured_at: datetime) -> RosterReading:
            self._captured_at = captured_at
            try:
                agents = reader()
            except ROSTER_READ_ERRORS as exc:
                return RosterReading(
                    agents=None, failure=f"{type(exc).__name__}: {exc}"
                )
            return RosterReading(agents=agents)

        return self._store.capture(_KEY, read=_read)

    def refresh(self, reader: RosterReader) -> None:
        """Re-read unless a success younger than the reuse window is held."""
        try:
            held = self._store.lookup(
                _KEY, max_age_seconds=ROSTER_REUSE_MAX_AGE_SECONDS
            )
        except ProbeStoreUnavailableError:
            held = None
        if held is None or held.agents is None:
            self.capture(reader)

    def shared(self, reader: RosterReader) -> RosterReader:
        """A reader over this snapshot: refresh, then look up.

        The dirty-check pre-pass's roster read, so the final pre-lock refresh
        can reuse it. A failed read raises ``RosterUnavailableError``.
        """

        def _read_shared() -> list[dict[str, object]]:
            self.refresh(reader)
            return self.lookup()

        return _read_shared

    def lookup(self) -> list[dict[str, object]]:
        """The captured agents if usable. Never runs a subprocess.

        Raises :class:`RosterUnavailableError` for a missing, stale (aged
        outside ``[0, ROSTER_SNAPSHOT_MAX_AGE_SECONDS)``) or failed reading.
        An empty roster is a hit.
        """
        try:
            reading = self._store.lookup(_KEY)
        except ProbeStoreUnavailableError as err:
            if err.age is None:
                msg = "missing"
                raise RosterUnavailableError(msg, reason="missing") from err
            msg = f"stale age={err.age:.1f}s"
            raise RosterUnavailableError(msg, reason="stale") from err
        if reading.agents is None:
            msg = f"failed: {reading.failure}"
            raise RosterUnavailableError(msg, reason="failed")
        return reading.agents


def lookup_roster(snapshot: RosterSnapshot | None) -> list[dict[str, object]]:
    """The in-lock roster read: the snapshot's agents, never a subprocess.

    ``None`` is treated as an empty snapshot (missing). Any miss logs one
    ``roster_unavailable`` warning naming its cause and consequence, then
    re-raises ``RosterUnavailableError`` for the caller.
    """
    store = snapshot if snapshot is not None else RosterSnapshot()
    try:
        return store.lookup()
    except RosterUnavailableError as exc:
        _log.warning("roster_unavailable: cause=%s; %s", exc, _CONSEQUENCE[exc.reason])
        raise
