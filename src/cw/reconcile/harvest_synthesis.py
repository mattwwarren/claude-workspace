"""Per-backend result synthesis for the local harvest sweep (#2369, #2512).

Split out of ``cw.reconcile.local`` (GitHub #2565) to keep that module under
the size ceiling. ``local.py`` imports from here; nothing here imports from
``local.py``.

A dead ``local_liveness`` process leaves no sentinel, so the harvest builds
one from what the backend left behind: git facts for aider, the JSONL log for
opencode. A recorded ``aider`` backend is verified against the worktree's
launch logs and the spawn stage first (:func:`_resolve_harvest_backend`); an
unproven backend parks the row instead of guessing.

The harvest runs under ``sessions_lock``, where no subprocess may run (ADR-0019,
#2565). So the git facts an aider result is built from are captured first,
lockless, into a :class:`HarvestFacts` store (``reconcile()``'s last
pre-pass), and the in-lock synthesis only looks them up. A missing,
mismatched or stale capture raises :class:`HarvestFactsUnavailableError`,
which the caller turns into a one-tick defer. A captured ``OSError`` or
``CalledProcessError`` is git's own evidence and is replayed in-lock, where it
parks the row as before; a ``subprocess.TimeoutExpired`` is elapsed time, not
evidence, so it is never stored and never parks (ADR-0014).
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, cast

from cw.local_runner import (
    AIDER_LOG_RELATIVE_PATH,
    LOCAL_EXECUTOR_FAILURE_ACTION,
    UNEXPECTED_ERROR,
    git_facts,
    make_blocked,
    synthesize_git_result,
)
from cw.models import OPENCODE_BACKEND, Stage
from cw.opencode_runner import (
    OPENCODE_LOG_RELATIVE_PATH,
    stage_entry_marker,
    synthesize_opencode_result,
)
from cw.opencode_runner import (
    make_blocked as make_opencode_blocked,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.local_runner import GitFacts
    from cw.models import (
        LocalLivenessBackend,
        LocalLivenessHandle,
        Session,
        TicketTask,
    )

# The warnings below were emitted from ``cw.reconcile.local`` before the split,
# and operators filter on that logger name. Pinned, never ``__name__``, so the
# move does not silently rename it (the ``cw.cli.stop_hook._constants`` rule).
_LOGGER_NAME = "cw.reconcile.local"
_log = logging.getLogger(_LOGGER_NAME)

AIDER_BACKEND: LocalLivenessBackend = "aider"

# The git failures that are evidence about the worktree, so a harvest parks on
# them: captured lockless and replayed in-lock (``HarvestFacts``), and caught
# by ``_synthesize_harvest_sentinel``. ``subprocess.TimeoutExpired`` is
# deliberately absent: a timeout is elapsed time with no git answer, and a
# disposition off a clock is what ADR-0014 forbids, so it defers instead.
GIT_SYNTHESIS_ERRORS: tuple[type[OSError], type[subprocess.CalledProcessError]] = (
    OSError,
    subprocess.CalledProcessError,
)

# Wall-clock budget for one lockless harvest-facts capture pass. A candidate
# reached after it is spent gets no facts and defers to the next tick.
HARVEST_CAPTURE_BUDGET_SECONDS: float = 60.0
# How old captured facts may be when the in-lock harvest consumes them. Strict
# ``<``: facts exactly this old are stale. Above the worst capture span (the
# budget plus one candidate's four bounded git calls, 100 s), and expiry only
# defers the candidate, so this is a freshness bound, not a disposition clock.
HARVEST_FACTS_MAX_AGE_SECONDS: float = 180.0


class HarvestFactsUnavailableError(Exception):
    """No usable git facts for a harvest candidate: never captured, mismatched, stale.

    A plain ``Exception``, deliberately not a ``CwError``, so no caller's broad
    ``except CwError`` swallows it: the harvest catches it by name and defers
    the candidate to the next tick (like ``CleanProbeUnavailableError``).
    """


@dataclass(frozen=True)
class _CapturedFacts:
    """One session's capture: its identity, when, and git's answer.

    ``outcome`` is the facts, or the ``GIT_SYNTHESIS_ERRORS`` instance git
    raised, which ``HarvestFacts.lookup`` re-raises. ``captured_at`` is stamped
    before the git calls, so the age is measured from the start.
    """

    worktree: Path
    default_branch: str
    pid: int
    start_time_ns: int
    captured_at: datetime
    outcome: GitFacts | OSError | subprocess.CalledProcessError


class HarvestFacts:
    """Git facts captured lockless for the local harvest, keyed by session id.

    One pass captures (:meth:`capture`, runs git, lockless only), then the
    in-lock harvest looks up (:meth:`lookup`, never runs git). The identity a
    lookup re-checks is the worktree, the default branch and the liveness
    handle's PID and start time, so a session re-spawned under the same id
    misses. With *budget_seconds* set, captures stop once that much monotonic
    time has passed since construction. Mirrors ``codex_boot.CleanProbes``.
    """

    def __init__(self, *, budget_seconds: float | None = None) -> None:
        self.budget_seconds = budget_seconds
        self._deadline = (
            None if budget_seconds is None else monotonic() + budget_seconds
        )
        self._captures: dict[str, _CapturedFacts] = {}

    def capture(
        self,
        session_id: str,
        handle: LocalLivenessHandle,
        worktree: Path,
        default_branch: str,
    ) -> None:
        """Collect *worktree*'s git facts live and keep them. Lockless only.

        Raises ``HarvestFactsUnavailableError`` before running any git once
        the budget is spent. A ``GIT_SYNTHESIS_ERRORS`` failure is stored for
        :meth:`lookup` to replay; ``subprocess.TimeoutExpired`` propagates and
        nothing is stored, so the candidate defers.
        """
        if self._deadline is not None and monotonic() >= self._deadline:
            msg = (
                f"the {self.budget_seconds:.0f}s harvest-facts budget is spent;"
                f" session {session_id} was not captured"
            )
            raise HarvestFactsUnavailableError(msg)
        captured_at = datetime.now(UTC)
        outcome: GitFacts | OSError | subprocess.CalledProcessError
        try:
            outcome = git_facts(worktree, default_branch)
        except GIT_SYNTHESIS_ERRORS as exc:
            outcome = exc
        self._captures[session_id] = _CapturedFacts(
            worktree=worktree,
            default_branch=default_branch,
            pid=handle.pid,
            start_time_ns=handle.start_time_ns,
            captured_at=captured_at,
            outcome=outcome,
        )

    def lookup(
        self,
        session_id: str,
        handle: LocalLivenessHandle,
        worktree: Path,
        default_branch: str,
    ) -> GitFacts:
        """Return *session_id*'s captured facts if still usable. Never runs git.

        Usable means captured for this same worktree, default branch, PID and
        start time, and aged in ``[0, HARVEST_FACTS_MAX_AGE_SECONDS)``: a
        negative age (the clock went backwards) fails closed too. Otherwise
        raises ``HarvestFactsUnavailableError`` naming the reason (``never
        captured``, ``identity changed`` or ``stale``). A stored git failure
        is re-raised.
        """
        entry = self._captures.get(session_id)
        if entry is None:
            msg = f"harvest facts for session {session_id} were never captured"
            raise HarvestFactsUnavailableError(msg)
        identity = (worktree, default_branch, handle.pid, handle.start_time_ns)
        captured = (
            entry.worktree,
            entry.default_branch,
            entry.pid,
            entry.start_time_ns,
        )
        if identity != captured:
            msg = (
                f"harvest facts for session {session_id}: identity changed since"
                " capture (worktree, default branch or liveness handle)"
            )
            raise HarvestFactsUnavailableError(msg)
        age = (datetime.now(UTC) - entry.captured_at).total_seconds()
        if not 0 <= age < HARVEST_FACTS_MAX_AGE_SECONDS:
            msg = f"harvest facts for session {session_id} are stale at age {age:.1f}s"
            raise HarvestFactsUnavailableError(msg)
        if isinstance(entry.outcome, Exception):
            raise entry.outcome
        return entry.outcome


def _harvest_via_git(
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    session_id: str,
    facts: Callable[[], GitFacts],
) -> AutoDevResult:
    return synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch=default_branch,
        plan_source="none",
        session_id=session_id,
        facts=facts(),
    )


def _harvest_via_opencode_log(
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    session_id: str,
    facts: Callable[[], GitFacts],
) -> AutoDevResult:
    # The sentinel comes from the JSONL log, never from git facts.
    del default_branch, facts
    return synthesize_opencode_result(
        task=task, worktree=worktree, session_id=session_id
    )


# Harvest-time result synthesizer per LocalLivenessHandle.backend (#2369).
# Each entry is normalized to (task, worktree, default_branch, session_id,
# facts), where *facts* returns the pre-captured git facts (#2565).
_HARVEST_SYNTHESIZERS: dict[
    LocalLivenessBackend,
    Callable[
        [TicketTask, Path, str, str, Callable[[], GitFacts]],
        AutoDevResult,
    ],
] = {
    AIDER_BACKEND: _harvest_via_git,
    cast("LocalLivenessBackend", OPENCODE_BACKEND): _harvest_via_opencode_log,
}


def harvest_uses_git(backend: LocalLivenessBackend | None) -> bool:
    """True iff harvesting *backend* builds its result from git facts.

    An unregistered backend falls back to git synthesis, so it counts;
    ``None`` (unproven) never synthesizes.
    """
    return (
        backend is not None
        and _HARVEST_SYNTHESIZERS.get(backend, _harvest_via_git) is _harvest_via_git
    )


def _log_exists(worktree: Path, relative_path: Path) -> bool:
    """True iff *relative_path* exists under *worktree*; ``OSError`` reads as absent."""
    with contextlib.suppress(OSError):
        return (worktree / relative_path).exists()
    return False


def _resolve_harvest_backend(
    backend: LocalLivenessBackend,
    session: Session,
    task: TicketTask,
    worktree: Path,
) -> LocalLivenessBackend | None:
    """:func:`prove_harvest_backend`, logging when it overrides a recorded aider.

    The single ``harvest_backend_overridden`` warning lives here, not in the
    pure rule, so the lockless pre-pass (which proves the backend too) does
    not log it a second time.
    """
    resolved = prove_harvest_backend(backend, session, task, worktree)
    if backend == AIDER_BACKEND and resolved == cast(
        "LocalLivenessBackend", OPENCODE_BACKEND
    ):
        _log.warning(
            "harvest_backend_overridden: session=%s ticket=%s recorded=aider"
            " effective=opencode (only .cw/opencode.log present; GitHub #2512)",
            session.id,
            task.ticket_id,
        )
    return resolved


def prove_harvest_backend(
    backend: LocalLivenessBackend,
    session: Session,
    task: TicketTask,
    worktree: Path,
) -> LocalLivenessBackend | None:
    """The backend that really launched *session*, or ``None`` if it cannot be proven.

    A ``local_liveness`` handle written before the ``backend`` field existed
    (#2369) migrates to an explicit ``"aider"`` that is byte-identical to a genuine
    aider handle, so a recorded ``"aider"`` is verified here, from facts outside
    the handle (GitHub #2512). Log *contents* are never read; only presence is
    probed. Rules, in order:

    1. Recorded ``opencode`` / ``codex``: returned unchanged.
    2. Recorded ``aider``. ``.cw/aider.log`` is opened before ``Popen`` by a
       genuine aider launch, so it is always left behind:

       - only ``.cw/opencode.log``: it was an opencode run -> ``"opencode"``;
       - only ``.cw/aider.log``: ``"aider"`` (a misconfigured ``local`` backend on
         a non-IMPL stage is still refused and paged by the stage guard);
       - both or neither: the spawn stage decides. ``session.stage`` is the stage
         the executor was chosen for (``_create_executor_session``); the row's
         stage may have advanced since, so it is only the fallback when the
         session carries none. IMPL -> ``"aider"`` (git synthesis is
         stage-correct there); any other stage -> ``None``.

    Pure apart from the two file-presence probes: no git, no logging, so the
    lockless harvest-facts pre-pass can call it (#2565).
    """
    if backend != AIDER_BACKEND:
        return backend
    has_opencode_log = _log_exists(worktree, OPENCODE_LOG_RELATIVE_PATH)
    has_aider_log = _log_exists(worktree, AIDER_LOG_RELATIVE_PATH)
    if has_opencode_log and not has_aider_log:
        return cast("LocalLivenessBackend", OPENCODE_BACKEND)
    if has_aider_log and not has_opencode_log:
        return AIDER_BACKEND
    return AIDER_BACKEND if (session.stage or task.stage) is Stage.IMPL else None


# Operator hint on the blocked result parked for a backend that could not be proven.
_UNPROVEN_BACKEND_NEXT_ACTIONS: list[str] = [LOCAL_EXECUTOR_FAILURE_ACTION]
# Under the 200-char cap the blocked-reason breadcrumb renders (#2512).
_UNPROVEN_BACKEND_DETAILS = (
    "The executor backend could not be proven from the liveness handle or the"
    " worktree launch logs, so no result was synthesized."
)


def _synthesize_harvest_sentinel(
    worktree: Path,
    task: TicketTask,
    default_branch: str,
    session_id: str,
    backend: LocalLivenessBackend | None,
    *,
    facts: Callable[[], GitFacts],
) -> AutoDevResult:
    """Synthesize the harvest sentinel with the synthesizer for *backend*.

    Dispatches through ``_HARVEST_SYNTHESIZERS`` on the resolved backend
    (opencode → JSONL log parse, aider → git-fact synthesis); an unregistered
    backend falls back to git synthesis. ``None`` (backend unproven, see
    :func:`_resolve_harvest_backend`) never synthesizes: it returns a blocked
    ``unexpected_error`` result at the row's own entry marker, which the
    existing routing parks BLOCKED_ON_USER and pages. A git/opencode failure on
    one candidate must not abort the entire harvest sweep — the fallback returns a
    blocked result at the row's entry marker (never a later one, which would walk
    the pointer, never ``stage2_impl`` on a non-IMPL row) and logs the exception.

    *facts* returns the git facts a git-backed synthesizer builds from (the
    in-lock ``HarvestFacts.lookup``, #2565): it runs no git, and a captured
    ``GIT_SYNTHESIS_ERRORS`` failure it re-raises parks here as above. A
    ``HarvestFactsUnavailableError`` (or a ``subprocess.TimeoutExpired``) is
    deliberately not caught: it propagates so the caller defers instead.
    """
    stage_reached = stage_entry_marker(task.stage.value)
    if backend is None:
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=UNEXPECTED_ERROR,
            details=_UNPROVEN_BACKEND_DETAILS,
            stage_reached=stage_reached,
            next_actions=_UNPROVEN_BACKEND_NEXT_ACTIONS.copy(),
        )
    synthesize = _HARVEST_SYNTHESIZERS.get(backend, _harvest_via_git)
    try:
        return synthesize(task, worktree, default_branch, session_id, facts)
    except GIT_SYNTHESIS_ERRORS:
        _log.warning(
            "harvest_synthesis_failed: session=%s ticket=%s backend=%s;"
            " parking at the row's stage (GitHub #2512)",
            session_id,
            task.ticket_id,
            backend,
            exc_info=True,
        )
        blocked = (
            make_opencode_blocked
            if backend == cast("LocalLivenessBackend", OPENCODE_BACKEND)
            else make_blocked
        )
        return blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=UNEXPECTED_ERROR,
            stage_reached=stage_reached,
        )
