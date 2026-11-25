"""The codex PLAN origin-sync pre-flight (#3465; Step P1 and P3 on the codex lane).

Runs first in the PLAN stage and emits no event. It refuses a run whose
default branch is behind or diverged from ``origin``, the same health gate the
claude-native intake applies (``auto-dev-intake.md`` Step P1), so the plan is
not written against a stale primary checkout.

The fetch is bounded by :data:`ORIGIN_FETCH_TIMEOUT_SECONDS` through the
existing :class:`~cw.worktree.TickFetchGate` seam. Expiry kills only that one
``git fetch`` child and fails open with a WARNING (ADR-0014 Clarification
#3885, extended to this consumer by #3465).
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw._git import LOCAL_GIT_TIMEOUT_SECONDS, git_output
from cw.local_runner import make_blocked
from cw.worktree import FetchOutcome, TickFetchGate, fetch_default_branch

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.models import ClientConfig
    from cw.worktree import FetchResult

_log = logging.getLogger(__name__)

#: The bound on the one ``git fetch`` (ADR-0014 Clarification #3885, #3465).
ORIGIN_FETCH_TIMEOUT_SECONDS: Final = 60.0
#: Re-reads of an ``ahead=0, behind>0`` state, one sleep each, no re-fetch.
WAIT_ATTEMPTS: Final = 3
WAIT_INTERVAL_SECONDS: Final = 30
#: The blocker reason Step P1 names (``auto-dev-intake.md``).
DIVERGED_REASON: Final = "local_main_diverged_from_origin"
_STAGE_REACHED: Final = "stage1_pre_flight"
_NEXT_ACTIONS: Final = ["sync_local_main"]


def _message(branch: str) -> str:
    """The Step P3 sentinel's ``message`` for *branch*.

    For ``main`` it is the claude-native text verbatim
    (``auto-dev-intake-appendix.md``); any other default branch is named.
    """
    return (
        f"Local {branch} is not in sync with origin/{branch}; "
        "pipeline aborted before impl"
    )


def _recovery_hint(branch: str) -> str:
    """The Step P3 sentinel's ``recovery_hint`` for *branch* (``main`` verbatim)."""
    return (
        f"Orchestrator: push or rebase local {branch} "
        f"(git push origin {branch} / git pull --ff-only), then re-dispatch"
    )


@dataclass(frozen=True)
class OriginSync:
    """The default branch against its origin ref: shas and the two counts."""

    local_sha: str
    origin_sha: str
    ahead: int
    behind: int

    @property
    def in_sync(self) -> bool:
        return self.ahead == 0 and self.behind == 0


def fetch_origin(client: ClientConfig) -> FetchResult:
    """One bounded ``git fetch origin <default_branch>`` (the seam's only timeout)."""
    gate = TickFetchGate(
        warned=set(),
        per_call_s=ORIGIN_FETCH_TIMEOUT_SECONDS,
        budget_s=ORIGIN_FETCH_TIMEOUT_SECONDS,
    )
    with gate.tick():
        return fetch_default_branch(client)


def read_origin_sync(worktree: Path, branch: str) -> OriginSync | None:
    """The local and origin shas and counts of *branch*; ``None`` when unreadable."""
    try:
        local = git_output(
            ["rev-parse", "--verify", branch],
            cwd=worktree,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        ).strip()
        origin = git_output(
            ["rev-parse", "--verify", f"origin/{branch}"],
            cwd=worktree,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        ).strip()
        counts = git_output(
            ["rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"],
            cwd=worktree,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        ).split()
        ahead, behind = int(counts[0]), int(counts[1])
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
        ValueError,
        IndexError,
    ) as exc:
        _log.warning(
            "codex plan: origin sync not read for %s (%s)", branch, type(exc).__name__
        )
        return None
    return OriginSync(local_sha=local, origin_sha=origin, ahead=ahead, behind=behind)


def _block(
    ticket_id: str, worktree: Path, branch: str, sync: OriginSync
) -> AutoDevResult:
    details = (
        f"local_main={sync.local_sha}, origin_main={sync.origin_sha}, "
        f"ahead={sync.ahead}, behind={sync.behind}"
    )
    result = make_blocked(
        ticket_id=ticket_id,
        worktree=worktree,
        reason=DIVERGED_REASON,
        details=details,
        retry_eligible=True,
        retry_delay_seconds=None,
        stage_reached=_STAGE_REACHED,
        next_actions=list(_NEXT_ACTIONS),
    )
    blocker = result.blocker
    if blocker is not None:
        result = result.model_copy(
            update={
                "blocker": blocker.model_copy(
                    update={
                        "message": _message(branch),
                        "recovery_hint": _recovery_hint(branch),
                    }
                )
            }
        )
    _log.info("codex plan: %s not in sync with origin (%s)", branch, details)
    return result


def origin_sync_block(
    *,
    client: ClientConfig,
    worktree: Path,
    ticket_id: str,
    fetch: Callable[[ClientConfig], FetchResult] = fetch_origin,
    read: Callable[[Path, str], OriginSync | None] = read_origin_sync,
    sleep: Callable[[float], None] = time.sleep,
) -> AutoDevResult | None:
    """The pre-flight block for a diverged or behind default branch, else ``None``.

    A fetch that did not establish fresh refs, or an unreadable ref, fails open.
    Ahead, or diverged, blocks at once. Behind only is re-read after each
    :data:`WAIT_INTERVAL_SECONDS` wait, up to :data:`WAIT_ATTEMPTS` times,
    without another fetch, so a sibling's fast-forward can still clear it.
    """
    # stays on client.default_branch: primary-checkout health gate (Step P1),
    # not a ticket base
    branch = client.default_branch
    fetched = fetch(client)
    if fetched.outcome is not FetchOutcome.FETCHED:
        _log.warning(
            "codex plan: origin sync skipped for %s (%s)",
            branch,
            fetched.reason or fetched.outcome.value,
        )
        return None
    sync = read(worktree, branch)
    if sync is None or sync.in_sync:
        return None
    attempts = 0
    while sync.ahead == 0 and sync.behind > 0 and attempts < WAIT_ATTEMPTS:
        sleep(WAIT_INTERVAL_SECONDS)
        attempts += 1
        sync = read(worktree, branch)
        if sync is None:
            return None
        if sync.in_sync:
            return None
    return _block(ticket_id, worktree, branch, sync)


__all__ = [
    "DIVERGED_REASON",
    "ORIGIN_FETCH_TIMEOUT_SECONDS",
    "WAIT_ATTEMPTS",
    "WAIT_INTERVAL_SECONDS",
    "OriginSync",
    "fetch_origin",
    "origin_sync_block",
    "read_origin_sync",
]
