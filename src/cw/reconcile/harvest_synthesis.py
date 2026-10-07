"""Per-backend result synthesis for the local harvest sweep (#2369, #2512).

Split out of ``cw.reconcile.local`` (GitHub #2565) to keep that module under
the size ceiling. ``local.py`` imports from here; nothing here imports from
``local.py``.

A dead ``local_liveness`` process leaves no sentinel, so the harvest builds
one from what the backend left behind: git facts for aider, the JSONL log for
opencode. A recorded ``aider`` backend is verified against the worktree's
launch logs and the spawn stage first (:func:`_resolve_harvest_backend`); an
unproven backend parks the row instead of guessing.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from typing import TYPE_CHECKING, cast

from cw.local_runner import (
    AIDER_LOG_RELATIVE_PATH,
    LOCAL_EXECUTOR_FAILURE_ACTION,
    UNEXPECTED_ERROR,
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
    from cw.models import LocalLivenessBackend, Session, TicketTask

# The warnings below were emitted from ``cw.reconcile.local`` before the split,
# and operators filter on that logger name. Pinned, never ``__name__``, so the
# move does not silently rename it (the ``cw.cli.stop_hook._constants`` rule).
_LOGGER_NAME = "cw.reconcile.local"
_log = logging.getLogger(_LOGGER_NAME)

AIDER_BACKEND: LocalLivenessBackend = "aider"


def _harvest_via_git(
    task: TicketTask, worktree: Path, default_branch: str, session_id: str
) -> AutoDevResult:
    return synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch=default_branch,
        plan_source="none",
        session_id=session_id,
    )


def _harvest_via_opencode_log(
    task: TicketTask, worktree: Path, default_branch: str, session_id: str
) -> AutoDevResult:
    del default_branch  # the sentinel comes from the JSONL log, not git facts
    return synthesize_opencode_result(
        task=task, worktree=worktree, session_id=session_id
    )


# Harvest-time result synthesizer per LocalLivenessHandle.backend (#2369).
# Each entry is normalized to (task, worktree, default_branch, session_id).
_HARVEST_SYNTHESIZERS: dict[
    LocalLivenessBackend,
    Callable[[TicketTask, Path, str, str], AutoDevResult],
] = {
    AIDER_BACKEND: _harvest_via_git,
    cast("LocalLivenessBackend", OPENCODE_BACKEND): _harvest_via_opencode_log,
}


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
    """
    if backend != AIDER_BACKEND:
        return backend
    has_opencode_log = _log_exists(worktree, OPENCODE_LOG_RELATIVE_PATH)
    has_aider_log = _log_exists(worktree, AIDER_LOG_RELATIVE_PATH)
    if has_opencode_log and not has_aider_log:
        _log.warning(
            "harvest_backend_overridden: session=%s ticket=%s recorded=aider"
            " effective=opencode (only .cw/opencode.log present; GitHub #2512)",
            session.id,
            task.ticket_id,
        )
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
        return synthesize(task, worktree, default_branch, session_id)
    except (OSError, subprocess.CalledProcessError):
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
