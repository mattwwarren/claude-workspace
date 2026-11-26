"""The Step 4.0b delta-confirm round of the codex FINALIZE fence (#3542).

When ``cw review converge verify-tip`` refuses the tip with
``commits_after_clean_round`` and ``delta_confirm_eligible: true`` (only
merge-resolution commits since the clean round), the doc runs one independent
delta confirmation instead of parking at once. :func:`confirm_resolution`
calls ``cw review converge confirm-job delta`` through the caller's
``run_cli`` (``finalize_gates.run_cw_cli``), so this module imports no CLI and
no ``cw.codex_*`` module, and never runs ``converge finalize``.

Exit 75 is pending: the call is repeated with ``--job-id`` when one was
printed, else restarted after :data:`PENDING_PAUSE_SECONDS`. There is no
deadline (ADR-0014). ``verify-tip`` is re-run only after exit 0 ``clean``,
exit 1 or a call that could not finish, and then it alone decides.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Final

from cw.review_convergence import DELTA_CONFIRM_ELIGIBLE_KEY

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from pathlib import Path

    RunCli = Callable[[Path, list[str]], subprocess.CompletedProcess[str] | None]

__all__ = [
    "DELTA_CONFIRM_ELIGIBLE_KEY",
    "JOB_ID_PREFIX",
    "PENDING_PAUSE_SECONDS",
    "RUNNING_EXIT",
    "confirm_resolution",
    "delta_eligible",
]

#: Restated from ``cw.confirmation_jobs`` (which imports ``cw.cli``); a
#: lockstep test pins both.
RUNNING_EXIT: Final = 75
#: The stderr prefix ``cw.cli.review.confirm_job`` writes the job id after.
JOB_ID_PREFIX: Final = "CONFIRM_JOB_ID="
#: The pause before restarting a pending start that printed no job id.
PENDING_PAUSE_SECONDS: Final = 5.0

COMMITS_AFTER_CLEAN_ROUND: Final = "commits_after_clean_round"

#: Park outcomes (the doc's tokens).
REFUSED: Final = "refused"
BACKEND_UNAVAILABLE: Final = "backend_unavailable"
ROUND_FAILED: Final = "round_failed"
FINDINGS: Final = "findings"
STOPPED: Final = "stopped"
STILL_STALE: Final = "still_stale"

_EXIT_REFUSED: Final = 1
_EXIT_BACKEND_DOWN: Final = 3
_DECISION_CLEAN: Final = "clean"
_DECISION_CONTINUE: Final = "continue"
_STOP_PREFIX: Final = "stop_"
_VERIFY_TIP: Final = ("review", "converge", "verify-tip")


def _json_object(text: str) -> dict[str, object] | None:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def delta_eligible(stdout: str) -> bool:
    """True when a refused ``verify-tip`` names a delta-confirm-eligible tip."""
    data = _json_object(stdout)
    if data is None:
        return False
    return (
        data.get("reason") == COMMITS_AFTER_CLEAN_ROUND
        and data.get(DELTA_CONFIRM_ELIGIBLE_KEY) is True
    )


def _job_id(stderr: str) -> str:
    for line in reversed(stderr.splitlines()):
        if line.startswith(JOB_ID_PREFIX):
            return line.removeprefix(JOB_ID_PREFIX).strip()
    return ""


def _await_round(
    worktree: Path, *, ticket_id: str, client_name: str, run_cli: RunCli
) -> subprocess.CompletedProcess[str] | None:
    """Start the delta job and wait (no deadline) until it is not pending."""
    start = [
        "review",
        "converge",
        "confirm-job",
        "delta",
        "--ticket-id",
        ticket_id,
        "-c",
        client_name,
    ]
    args = start
    while True:
        done = run_cli(worktree, args)
        if done is None or done.returncode != RUNNING_EXIT:
            return done
        job_id = _job_id(done.stderr)
        if job_id:
            args = [*start, "--job-id", job_id]
        else:
            args = start
            time.sleep(PENDING_PAUSE_SECONDS)


def _decision_outcome(stdout: str) -> str | None:
    """Exit 0: ``None`` for ``clean`` (verify-tip decides), else the park token."""
    data = _json_object(stdout)
    decision = data.get("decision") if data is not None else None
    if decision == _DECISION_CLEAN:
        return None
    if decision == _DECISION_CONTINUE:
        return FINDINGS
    if isinstance(decision, str) and decision.startswith(_STOP_PREFIX):
        return STOPPED
    return ROUND_FAILED


def _tip_accepted(worktree: Path, run_cli: RunCli) -> bool:
    done = run_cli(worktree, list(_VERIFY_TIP))
    return done is not None and done.returncode == 0


def confirm_resolution(
    worktree: Path, *, ticket_id: str, client_name: str, run_cli: RunCli
) -> str | None:
    """Run the delta-confirm round; ``None`` means the tip is now accepted.

    Otherwise the doc's park outcome: ``refused``, ``backend_unavailable``,
    ``round_failed``, ``findings``, ``stopped`` or ``still_stale``.
    """
    done = _await_round(
        worktree, ticket_id=ticket_id, client_name=client_name, run_cli=run_cli
    )
    if done is None or done.returncode == _EXIT_REFUSED:
        # An interrupted job may still have recorded its round: verify-tip decides.
        return None if _tip_accepted(worktree, run_cli) else REFUSED
    if done.returncode == _EXIT_BACKEND_DOWN:
        return BACKEND_UNAVAILABLE
    if done.returncode != 0:
        return ROUND_FAILED
    outcome = _decision_outcome(done.stdout)
    if outcome is not None:
        return outcome
    return None if _tip_accepted(worktree, run_cli) else STILL_STALE
