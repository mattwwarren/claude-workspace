"""The dispatch tick's bound on read-only network git calls (#2910).

A hung ``git fetch`` (an ssh child waiting on a credential-agent prompt) used
to stall ``dispatch_tick`` for every client. :class:`TickFetchGate` is the
loop-scoped object that bounds the in-tick read-only fetches, and
:func:`run_network_git` is the single seam they all go through: the freshness
gate's ``fetch origin <default>``, the claim path's ``fetch_feature_branch`` /
``fetch_default_branch`` (via ``create_worktree``), and the fix agent's
``fetch origin``. The gate carries:

- a per-call timeout (:data:`TICK_FETCH_TIMEOUT_SECONDS`);
- a per-tick aggregate budget (:data:`TICK_FETCH_BUDGET_SECONDS`), shared by
  every gated call, successful ones included;
- a per-client backoff after a timeout (:data:`TICK_FETCH_BACKOFF_SECONDS`);
- the loop's ``warned_fetch_fail`` dedup set, so the claim path and the fix
  agent warn once per distinct failure too.

Expiry raises :class:`~cw.exceptions.GitTimeoutError`; a declined call (budget
spent, client backing off) raises :class:`~cw.exceptions.FetchSkippedError`.
Callers degrade both to freshness-unknown and never park, revert or skip a row
(ADR-0014, "Clarification (#2910)"). Only ``fetch`` and ``ls-remote`` may be
bounded: ``_run_git`` refuses a timeout on any other verb, and no mutating
command (``pull``, ``merge``, ``submodule update``, ``push``) ever gets one.

The gate is active only inside :meth:`TickFetchGate.tick`, which the dispatch
loop wraps around each ``dispatch_tick``. Outside it (interactive ``cw start``,
operator commands) :func:`run_network_git` runs unbounded, so a human approving
a key prompt is never cut off. State is in memory only; a loop restart clears
the backoff map.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from cw.exceptions import FetchSkippedError, GitTimeoutError
from cw.worktree._git import _run_git, require_read_only_network_verb

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from cw.worktree._freshness import FetchWarningKey

#: N: the longest one gated call may run.
TICK_FETCH_TIMEOUT_SECONDS: Final = 15.0
#: M: the total gated-call time one tick may spend. With the 30 s default tick
#: interval, M + sleep stays 30 s under ``TICK_STALE_SECONDS`` (90 s).
TICK_FETCH_BUDGET_SECONDS: Final = 30.0
#: B: how long a client's gated calls are skipped after one of them timed out
#: (4 default ticks), so a down remote costs at most N per client per window.
TICK_FETCH_BACKOFF_SECONDS: Final = 120.0
#: With less budget left than this, a call is skipped rather than started.
_MIN_ATTEMPT_SECONDS: Final = 1.0

#: Fixed skip reasons: identical every tick, so the warning dedups on them.
#: They name no seconds, because the granted timeout shrinks with the budget.
BUDGET_SKIP_REASON: Final = "fetch skipped: tick fetch budget spent"
BACKOFF_SKIP_REASON: Final = (
    "fetch skipped: client is backing off after a fetch timeout"
)

_ACTIVE: ContextVar[TickFetchGate | None] = ContextVar("_ACTIVE", default=None)


@dataclass
class TickFetchGate:
    """Per-call timeout, per-tick budget and per-client backoff for in-tick fetches.

    Loop-scoped like the dispatch loop's ``warned_*`` sets: *warned* IS the
    loop's ``warned_fetch_fail``. The budget and the degraded set reset at each
    :meth:`tick`; the backoff map persists across ticks. *clock* is monotonic
    seconds; tests inject a fake one.
    """

    warned: set[FetchWarningKey]
    clock: Callable[[], float] = time.monotonic
    per_call_s: float = TICK_FETCH_TIMEOUT_SECONDS
    budget_s: float = TICK_FETCH_BUDGET_SECONDS
    backoff_s: float = TICK_FETCH_BACKOFF_SECONDS
    _spent: float = field(default=0.0, init=False, repr=False)
    _resume_at: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _degraded: set[str] = field(default_factory=set, init=False, repr=False)

    @contextmanager
    def tick(self) -> Iterator[TickFetchGate]:
        """Scope one dispatch tick: fresh budget, cleared degraded set, gate active."""
        self._spent = 0.0
        self._degraded.clear()
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)

    @contextmanager
    def bounded(self, client_name: str) -> Iterator[float]:
        """Admit one gated call for *client_name*, yielding its timeout in seconds.

        Raises :class:`~cw.exceptions.FetchSkippedError` without running
        anything (and charging nothing) while the client is backing off or the
        tick's budget is spent. Charges the call's elapsed time to the budget,
        and starts the client's backoff when the call timed out.
        """
        started = self.clock()
        resume_at = self._resume_at.get(client_name)
        if resume_at is not None and started < resume_at:
            self._degraded.add(client_name)
            raise FetchSkippedError(BACKOFF_SKIP_REASON)
        remaining = self.budget_s - self._spent
        if remaining < _MIN_ATTEMPT_SECONDS:
            self._degraded.add(client_name)
            raise FetchSkippedError(BUDGET_SKIP_REASON)
        try:
            yield min(self.per_call_s, remaining)
        except GitTimeoutError:
            self._resume_at[client_name] = self.clock() + self.backoff_s
            self._degraded.add(client_name)
            raise
        finally:
            self._spent += self.clock() - started

    def degraded(self, client_name: str) -> bool:
        """True when a gated call for *client_name* timed out or was skipped.

        Scoped to the current tick: :meth:`tick` clears it.
        """
        return client_name in self._degraded


def active_tick_fetch() -> TickFetchGate | None:
    """The gate of the tick in progress, or None outside a dispatch tick."""
    return _ACTIVE.get()


def active_warned() -> set[FetchWarningKey] | None:
    """The active gate's warning-dedup set, or None outside a dispatch tick."""
    gate = _ACTIVE.get()
    return gate.warned if gate is not None else None


def run_network_git(
    client_name: str, *args: str, cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a read-only network git call (``fetch`` / ``ls-remote``) for *client_name*.

    The single seam for in-tick network git: inside a tick it runs under the
    active gate's timeout (see :meth:`TickFetchGate.bounded`); outside one it
    is unbounded. Raises ``ValueError`` for any other verb, gate or not.
    """
    require_read_only_network_verb(args)
    gate = _ACTIVE.get()
    if gate is None:
        return _run_git(*args, cwd=cwd, check=check, timeout=None)
    with gate.bounded(client_name) as timeout:
        return _run_git(*args, cwd=cwd, check=check, timeout=timeout)
