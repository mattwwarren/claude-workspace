"""Main-branch freshness preflight gate for the dispatch loop.

Part of the ``cw.dispatch.gating`` package split (#2503): the per-client
check that the local default branch is current with origin, the auto
fast-forward path with its non-main-head / diverged / dirty / detached
refusals, and the ``ticket.needs_sync`` + ``dispatch.tick`` skip events for a
freshness-gated client.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from cw._text import redact
from cw.dispatch.gating.context_json import _LOGGER_NAME
from cw.events import record_event
from cw.exceptions import (
    MissingWorkspaceError,
    WorktreeError,
)
from cw.models import (
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.worktree import (
    check_main_ff_safety,
    fast_forward_main,
    get_head_branch,
    is_main_behind_origin,
    is_main_checkout_dirty,
)
from cw.worktree._freshness import (
    _clear_main_freshness_failure_reason,
    _get_main_freshness_failure_reason,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        ClientConfig,
        DevQueueStore,
    )
    from cw.worktree import FetchWarningKey
from cw.dispatch.claim import _lane_occupants_for_client, _lane_stats_for_client

_log = logging.getLogger(_LOGGER_NAME)


FRESHNESS_NON_MAIN_HEAD = "non_main_head"


FRESHNESS_MAIN_BEHIND = "main_behind_origin"


FRESHNESS_MAIN_DIRTY_CHECKOUT = "main_dirty_checkout"


FRESHNESS_MAIN_DIVERGED = "main_diverged_from_origin"


FRESHNESS_MAIN_DETACHED = "main_detached_head"


_FRESHNESS_FAILURE_MESSAGE_MAX = 120
_HTTP_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s]+@")
_FRESHNESS_FAILURE_CODES = frozenset(
    {
        "dirty_tree",
        "diverged",
        "detached_head",
        "not_a_work_tree",
        "fetch_failed",
        "auth_failed",
        "unknown",
    }
)


def _resolve_freshness(
    client: ClientConfig,
    *,
    auto_ff: bool,
    warned_fetch_fail: set[FetchWarningKey] | None,
) -> tuple[bool, str | None]:
    """Run the freshness gate for a client, returning (stale, freshness_detail).

    Checks whether the client's local default branch is behind origin.  When
    ``auto_ff`` is set and the branch is safely behind, attempts a
    fast-forward and clears the stale flag on success.  On any freshness-check
    error, logs and treats the client as fresh so a transient network issue
    never blocks the whole loop.

    The fetch inside ``is_main_behind_origin`` is bounded by the tick's fetch
    gate (#2910): an expiry or a gate skip is a failed fetch, so freshness is
    unknown; no ``ticket.needs_sync`` is recorded and the client still
    dispatches with an additive ``fetch_failed`` warning detail. The auto
    fast-forward is a local ``merge --ff-only origin/<default>`` with no timer
    (``fetch_first=False``): the gate's fetch already updated that tracking
    ref, and a mutating command is never bounded.

    Returns ``(False, None)`` when fresh (or successfully fast-forwarded) and
    ``(False, "<reason>: <message>")`` when a fail-open freshness fetch failed.
    Returns ``(True, "non_main_head")`` when the dispatch repo's HEAD is on a
    non-default branch — ``fast_forward_main`` is skipped entirely to avoid a
    spurious WorktreeError.  Returns ``(True, "main_behind_origin")`` for all
    other stale conditions.
    """
    try:
        _clear_main_freshness_failure_reason()
        stale, local_sha, origin_sha, behind_count = is_main_behind_origin(
            client, warned_fetch_fail=warned_fetch_fail
        )
        fetch_failure_reason = _get_main_freshness_failure_reason()
        failure_detail = (
            _format_freshness_failure(
                _classify_freshness_failure(
                    fetch_failure_reason or "", fallback="fetch_failed"
                ),
                _first_git_message(fetch_failure_reason or ""),
            )
            if fetch_failure_reason is not None
            else None
        )
    except Exception as exc:  # noqa: BLE001
        # Defense-in-depth: _fetch_default_branch now handles
        # FileNotFoundError/PermissionError internally; this catches
        # other unexpected OS errors (e.g., git not on PATH, network
        # issues raising RuntimeError from the adapter).
        _log.warning(
            "dispatch_tick: freshness check failed for %s; proceeding",
            client.name,
        )
        return (
            False,
            _format_freshness_failure("unknown", _first_git_message(str(exc))),
        )

    if stale:
        # Guard: detect non-default HEAD before attempting auto-ff.
        # When HEAD != default_branch, fast_forward_main would raise WorktreeError
        # and log a confusing message. Bail early with a distinct detail key so
        # the operator WARN can surface the specific remedy.
        head_branch = get_head_branch(client)
        if head_branch is not None and head_branch != client.default_branch:
            return (True, FRESHNESS_NON_MAIN_HEAD)

    if stale and auto_ff:
        ff_safety = check_main_ff_safety(client)
        if ff_safety == "detached":
            return (
                True,
                _format_freshness_failure(
                    "detached_head", "main checkout HEAD is detached"
                ),
            )
        # "ahead" is theoretically unreachable here: stale=True requires
        # is_main_behind_origin to return behind_count>0, which means local
        # is behind origin — not ahead. The guard is kept for defensive
        # completeness (worktree.py:check_main_ff_safety documents this).
        if ff_safety in ("ahead", "diverged"):
            return (
                True,
                _format_freshness_failure(
                    "diverged", "local main cannot fast-forward to origin"
                ),
            )
        if ff_safety == "behind" and is_main_checkout_dirty(client):
            return (
                True,
                _format_freshness_failure(
                    "dirty_tree", "main checkout has uncommitted changes"
                ),
            )
        if ff_safety == "behind":
            try:
                fast_forward_main(client, ignore_untracked=True, fetch_first=False)
                _log.info(
                    "auto-ff: %s/main: %s..%s (%d commits)",
                    client.name,
                    local_sha[:8],
                    origin_sha[:8],
                    behind_count,
                )
                stale = False
            except (WorktreeError, MissingWorkspaceError) as exc:
                failure_message = _first_git_message_from_exception(exc)
                failure_detail = _format_freshness_failure(
                    _classify_freshness_failure(failure_message, fallback="unknown"),
                    failure_message,
                )
                _log.warning(
                    "auto-ff: fast-forward failed for %s: %s",
                    client.name,
                    exc,
                )
            # Why: no git-level lock — concurrent dispatch loops are safe;
            # git merge --ff-only is idempotent when already current.
    return (
        stale,
        failure_detail or (FRESHNESS_MAIN_BEHIND if stale else None),
    )


def _format_freshness_failure(code: str, message: str) -> str:
    """Format a stable freshness failure code and a bounded first message."""
    clean_message = _first_git_message(message)[:_FRESHNESS_FAILURE_MESSAGE_MAX]
    return f"{code}: {clean_message}" if clean_message else code


def _first_git_message(message: str) -> str:
    """Return one line of Git/error output, omitting the fetch exit prefix."""
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    if first_line.startswith("rc=") and ": " in first_line:
        prefix, _, git_message = first_line.partition(": ")
        if prefix[3:].isdigit():
            first_line = git_message
    first_line = _HTTP_CREDENTIALS.sub(r"\1<redacted>@", first_line)
    return redact(first_line)


def _first_git_message_from_exception(exc: Exception) -> str:
    """Prefer the underlying Git stderr over cw's command wrapper message."""
    cause_stderr = getattr(exc.__cause__, "stderr", None)
    if isinstance(cause_stderr, str) and cause_stderr.strip():
        return _first_git_message(cause_stderr)
    return _first_git_message(str(exc))


def _classify_freshness_failure(
    message: str,
    *,
    fallback: str,
) -> str:
    """Map Git and worktree diagnostics to the ticket's stable reason codes."""
    normalized = message.lower()
    if any(
        marker in normalized
        for marker in (
            "authentication failed",
            "permission denied",
            "access denied",
            "could not read from remote repository",
            "could not read username",
            "repository not found",
        )
    ):
        return "auth_failed"
    if any(
        marker in normalized
        for marker in (
            "not a git repository",
            "gitfile does not point to a valid repository",
            "invalid gitfile format",
            "must be run in a work tree",
            "workspace missing",
        )
    ):
        return "not_a_work_tree"
    if "not possible to fast-forward" in normalized or "diverg" in normalized:
        return "diverged"
    if "working tree is dirty" in normalized or "uncommitted changes" in normalized:
        return "dirty_tree"
    if "detached head" in normalized:
        return "detached_head"
    return fallback


def _emit_stale_skip(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    pending_count: int,
    running_count: int,
    cap: int,
    emit: Callable[[str], None] | None,
    warned_stale: set[tuple[str, str]] | None,
    freshness_detail: str | None = None,
) -> None:
    """Emit TICKET_NEEDS_SYNC + dispatch.tick for a freshness-gated client.

    Records one TICKET_NEEDS_SYNC per pending task (de-duplicating the
    operator WARN via ``warned_stale``), then a single dispatch.tick with
    ``skip_reason=FRESHNESS_GATE`` and ``freshness_detail`` set to the
    provided value (legacy details or a structured auto-ff failure code and
    capped message).
    """
    stale_tasks = [
        {"ticket_id": t.ticket_id, "client": client.name, "lane": t.lane}
        for t in queue_snapshot.tasks
        if t.client == client.name and t.status == QueueItemStatus.PENDING
    ]
    # Fetch branch name once for the non-main-head WARN (not per ticket).
    non_main_branch: str | None = None
    if freshness_detail == FRESHNESS_NON_MAIN_HEAD:
        non_main_branch = get_head_branch(client)
    for payload in stale_tasks:
        record_event(OrchestratorEventType.TICKET_NEEDS_SYNC, payload)
        if emit is not None:
            ticket_key = (client.name, payload["ticket_id"])
            if warned_stale is None or ticket_key not in warned_stale:
                emit(
                    _freshness_warning_line(
                        client,
                        payload["ticket_id"],
                        freshness_detail,
                        non_main_branch,
                    )
                )
                if warned_stale is not None:
                    warned_stale.add(ticket_key)
    lane_occupants = _lane_occupants_for_client(client, queue_snapshot)
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client.name,
            "claimed": 0,
            "pending": pending_count,
            "running": running_count,
            "cap": cap,
            "skip_reason": DispatchSkipReason.FRESHNESS_GATE,
            "freshness_detail": freshness_detail,
            "blocked_branch": non_main_branch,
            "lanes": _lane_stats_for_client(
                client, queue_snapshot, occupants=lane_occupants
            ),
            "lane_occupants": lane_occupants,
            "occupied": sum(len(v) for v in lane_occupants.values()),
        },
    )


def _is_failure_detail(detail: str | None) -> bool:
    """Return whether *detail* carries a stable auto-ff/fetch failure code."""
    return detail is not None and detail.partition(":")[0] in _FRESHNESS_FAILURE_CODES


def _freshness_warning_line(
    client: ClientConfig,
    ticket_id: str,
    detail: str | None,
    non_main_branch: str | None,
) -> str:
    """Render one operator warning while preserving the legacy ticket wording."""
    prefix = f"WARN {client.name}/{ticket_id}:"
    if detail == FRESHNESS_NON_MAIN_HEAD:
        branch = non_main_branch or "(detached)"
        warning = (
            f"{prefix} repo HEAD is on '{branch}', expected '{client.default_branch}'"
            f" — run: git -C {client.workspace_path} checkout {client.default_branch}"
        )
    elif detail == FRESHNESS_MAIN_DIRTY_CHECKOUT:
        warning = (
            f"{prefix} main checkout has uncommitted changes, ticket skipped"
            f" — commit or stash changes in {client.workspace_path}"
        )
    elif detail == FRESHNESS_MAIN_DETACHED:
        warning = (
            f"{prefix} main checkout has a detached HEAD, ticket skipped"
            f" — run: git -C {client.workspace_path} checkout {client.default_branch}"
        )
    elif detail == FRESHNESS_MAIN_DIVERGED:
        warning = _diverged_freshness_warning(client, prefix)
    elif _is_failure_detail(detail):
        code, _, message = (detail or "").partition(": ")
        if code == "diverged":
            warning = _diverged_freshness_warning(client, prefix, message=message)
        else:
            suffix = f": {message}" if message else ""
            warning = (
                f"{prefix} main behind origin, ticket skipped"
                f" — auto-ff failed ({code}){suffix}"
            )
    else:
        warning = f"{prefix} main behind origin, ticket skipped"
    return warning


def _diverged_freshness_warning(
    client: ClientConfig, prefix: str, *, message: str = ""
) -> str:
    """Keep the inspect-first instruction on both legacy and structured details."""
    suffix = f" (auto-ff failed: {message})" if message else ""
    return (
        f"{prefix} main has diverged from origin, ticket skipped"
        " — inspect before touching it: git -C"
        f" {client.workspace_path} log origin/{client.default_branch}..HEAD --oneline —"
        " do NOT auto-rebase or reset; stray commits may need manual triage"
        f"{suffix}"
    )
