"""PR registration lifecycle subcommands and canonical repo paths."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, cast

from review_monitor_lib.models import MonitoredPR, ThreadStatus
from review_monitor_lib.state import load_state, save_state
from review_monitor_lib.threads import _apply_status_transitions

logger = logging.getLogger(__name__)


# Canonical clone path per repo. Monitored PRs must be registered against a
# stable clone, never an ephemeral agent worktree (those are created and torn
# down per task — a registered worktree path goes stale and breaks `check`'s
# git diff). Auto-fix agents cut their own worktree from the canonical clone,
# so the stored path only needs to be a valid, persistent checkout of `repo`.
CANONICAL_REPO_PATHS: dict[str, str] = {
    # "owner/repo": "/path/to/canonical/clone",
}

# Env var an operator can set to recover machine-local canonical repo paths
# (e.g. after `install-skills.sh` replaces a locally-edited CANONICAL_REPO_PATHS
# with this repo's empty tracked copy) without editing this file.
CANONICAL_REPO_PATHS_ENV = "CW_CANONICAL_REPO_PATHS"


def _canonical_repo_paths_override() -> dict[str, str]:
    """Parse CANONICAL_REPO_PATHS_ENV, a JSON object of repo -> path strings.

    Any parse or shape failure is logged and treated as no override — never
    partially applied. A per-entry empty path is dropped (with its own
    warning) rather than silently falling through to the tracked dict/given
    path; every rejection is logged, never silent.
    """
    raw = os.environ.get(CANONICAL_REPO_PATHS_ENV)
    if raw is None:
        return {}
    if raw == "":
        logger.warning(
            "_canonical_repo_paths_override: %s is set but empty; using default",
            CANONICAL_REPO_PATHS_ENV,
        )
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            "_canonical_repo_paths_override: invalid JSON in %s (%s); "
            "ignoring override",
            CANONICAL_REPO_PATHS_ENV,
            e,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "_canonical_repo_paths_override: %s must be a JSON object of "
            "repo->path strings, got %s; ignoring override",
            CANONICAL_REPO_PATHS_ENV,
            type(parsed).__name__,
        )
        return {}
    bad_value = next(
        ((k, v) for k, v in parsed.items() if not isinstance(v, str)), None
    )
    if bad_value is not None:
        key, value = bad_value
        logger.warning(
            "_canonical_repo_paths_override: %s value for %r is %s, expected "
            "str; ignoring override",
            CANONICAL_REPO_PATHS_ENV,
            key,
            type(value).__name__,
        )
        return {}
    parsed_str = cast("dict[str, str]", parsed)
    overrides: dict[str, str] = {}
    for key, value in parsed_str.items():
        if value == "":
            logger.warning(
                "_canonical_repo_paths_override: %s entry %r is empty; "
                "ignoring this entry",
                CANONICAL_REPO_PATHS_ENV,
                key,
            )
            continue
        overrides[key] = value
    return overrides


def _canonical_repo_path(repo: str, given: str) -> str:
    """Return the canonical clone path for *repo*, falling back to *given*.

    Normalizes away ephemeral agent-worktree paths at registration time.
    Consults the CANONICAL_REPO_PATHS_ENV env-var override before the
    tracked CANONICAL_REPO_PATHS dict.
    """
    return _canonical_repo_paths_override().get(repo) or CANONICAL_REPO_PATHS.get(
        repo, given
    )


def _normalize_thread_ids(threads: list[str] | None) -> list[str]:
    """Flatten ``--threads`` values, splitting any comma-joined string into IDs.

    ``--threads`` is space-separated (``nargs="*"``), but a caller may pass one
    comma-joined string (``"PRRT_a,PRRT_b"``). Split it so a malformed
    invocation never lands as a single bogus thread ID in ``our_threads``.
    """
    normalized: list[str] = []
    for raw in threads or []:
        normalized.extend(t.strip() for t in raw.split(",") if t.strip())
    return normalized


def cmd_register(
    pr_number: int,
    role: str,
    repo: str,
    repo_path: str,
    sha: str,
    review_id: str | None = None,
    threads: list[str] | None = None,
    thread_details: list[dict[str, Any]] | None = None,
    slack_channel: str | None = None,
    slack_ts: str | None = None,
) -> dict[str, Any]:
    """Register or update a PR for monitoring.

    For updates: merges new threads (no duplicates), updates SHA.
    *thread_details* is a list of ``{"id": "PRRT_x", "file": "path", "line": N}``
    dicts used to build :class:`ThreadStatus` entries.

    *repo_path* is normalized to the canonical clone — a PR registered against
    an ephemeral agent worktree would break once that worktree is cleaned up.

    Returns ``{"registered": True, "key": ..., "sha": ..., "updated": bool}``
    once the state is saved; ``updated`` is True when the PR was already
    monitored (a re-anchor). A state read or write failure raises ``OSError``.
    """
    threads = _normalize_thread_ids(threads)
    repo_path = _canonical_repo_path(repo, repo_path)
    state = load_state(repo, strict=True)
    key = f"{repo}#{pr_number}"
    updated = key in state.monitored

    if updated:
        pr = state.monitored[key]
        # An older registration may still point at a stale worktree — heal it.
        pr.repo_path = repo_path
        # Update SHA. Re-registering at a SHA is an explicit re-anchor (e.g. the
        # Step 0.6 recovery): reset the delta baseline too, so the next delta is
        # computed from this SHA rather than a stale one.
        pr.last_seen_sha = sha
        pr.delta_base_sha = sha
        # Merge threads (avoid duplicates)
        for tid in threads or []:
            if tid not in pr.our_threads:
                pr.our_threads.append(tid)
        # Build ThreadStatus for any new thread_details entries
        for detail in thread_details or []:
            tid = detail["id"]
            if tid not in pr.thread_status:
                pr.thread_status[tid] = ThreadStatus(
                    file=detail["file"],
                    line=detail["line"],
                )
        if slack_channel is not None:
            pr.slack_channel = slack_channel
        if slack_ts is not None:
            pr.slack_ts = slack_ts
    else:
        # Build initial thread_status from thread_details
        thread_status: dict[str, ThreadStatus] = {}
        for detail in thread_details or []:
            thread_status[detail["id"]] = ThreadStatus(
                file=detail["file"],
                line=detail["line"],
            )
        pr = MonitoredPR(
            role=role,
            repo=repo,
            repo_path=repo_path,
            pr_number=pr_number,
            last_seen_sha=sha,
            our_review_id=review_id,
            our_threads=list(threads or []),
            thread_status=thread_status,
            slack_channel=slack_channel,
            slack_ts=slack_ts,
        )
        state.monitored[key] = pr

    save_state(state, repo)
    return {"registered": True, "key": key, "sha": sha, "updated": updated}


def cmd_ack_delta(pr_number: int, repo: str, sha: str) -> dict[str, Any]:
    """Acknowledge that the reviewer delta through *sha* has been processed.

    The skill's Step 3 calls this at close — after both the thread-confirmation
    pass (3a) and the regression scan (3b) — passing the ``new_sha`` from the
    same ``check`` result it reviewed. This positively advances
    ``delta_base_sha``: baseline advancement becomes a confirmed action ("I
    processed the delta") rather than a side effect of observation. Commits
    pushed *after* that ``check`` stay above the baseline and surface as a
    fresh delta next cycle.

    Returns a JSON-able status dict; an ``error`` key if the PR is not tracked.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        return {"error": f"PR {key} not found in monitored"}
    pr = state.monitored[key]
    pr.delta_base_sha = sha
    pr.last_checked_at = datetime.now(UTC).isoformat()
    save_state(state, repo)
    return {"pr_number": pr_number, "delta_base_sha": sha, "acked": True}


def cmd_drop(pr_number: int, repo: str) -> dict[str, Any]:
    """Remove a PR from monitoring. No-op if not found.

    Returns ``{"dropped": bool, "key": ...}``; ``dropped`` is False (not an
    error) when the PR was not monitored.
    """
    state = load_state(repo, strict=True)
    key = f"{repo}#{pr_number}"
    dropped = key in state.monitored
    if dropped:
        del state.monitored[key]
        save_state(state, repo)
    return {"dropped": dropped, "key": key}


def cmd_complete(pr_number: int, repo: str, reason: str) -> dict[str, Any]:
    """Mark a PR as complete and move it out of active monitoring.

    No-op if not found. Returns ``{"completed": bool, "key": ..., "reason": ...}``;
    ``completed`` is False (not an error) when the PR was not monitored.
    """
    state = load_state(repo, strict=True)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        logger.info("cmd_complete: %r not found in monitored, ignoring", key)
        return {"completed": False, "key": key, "reason": reason}
    state.complete_pr(key, reason)
    save_state(state, repo)
    return {"completed": True, "key": key, "reason": reason}


def cmd_slack_thread_cursor(pr_number: int, repo: str) -> dict[str, Any]:
    """Return Slack thread cursor info so a session can call the Slack MCP
    read_thread tool.

    Returns ``{"slack_channel": str|None, "slack_ts": str|None,
    "slack_last_seen_ts": str|None}``.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        return {"error": f"PR {key} not found in monitored"}
    pr = state.monitored[key]
    return {
        "slack_channel": pr.slack_channel,
        "slack_ts": pr.slack_ts,
        "slack_last_seen_ts": pr.slack_last_seen_ts,
    }


def cmd_update_slack_cursor(pr_number: int, repo: str, last_seen_ts: str) -> None:
    """Advance ``slack_last_seen_ts`` after the session surfaces new thread messages."""
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        logger.warning("cmd_update_slack_cursor: %r not found in monitored", key)
        return
    state.monitored[key].slack_last_seen_ts = last_seen_ts
    save_state(state, repo)


def cmd_set_status(pr_number: int, repo: str, status: str) -> None:
    """Set the lifecycle status of a monitored PR.

    Valid values: "watching", "ready_to_approve", "approved".
    Logs a warning and returns without error if the PR is not found.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        logger.warning("cmd_set_status: %r not found in monitored", key)
        return
    state.monitored[key].status = status
    save_state(state, repo)


def cmd_confirm_thread(pr_number: int, repo: str, thread_id: str) -> None:
    """Mark a tracked thread as addressed-by-code-change, then re-run transitions.

    Called by the delta-review confirmation pass once it has verified that a
    new commit's changes actually address the thread's review comment. Setting
    ``code_changed`` feeds ``all_threads_addressed()``, so this also re-applies
    the status transition (``watching`` → ``ready_to_approve`` if every thread
    is now addressed). Prints a JSON summary so the caller sees the resulting
    status without a follow-up ``check``.

    Logs a warning and returns without error if the PR or thread is not found.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    pr = state.monitored.get(key)
    if pr is None:
        logger.warning("cmd_confirm_thread: %r not found in monitored", key)
        return
    ts = pr.thread_status.get(thread_id)
    if ts is None:
        logger.warning(
            "cmd_confirm_thread: thread %r not tracked on %r", thread_id, key
        )
        return
    ts.code_changed = True
    _apply_status_transitions(pr, changed=False)
    save_state(state, repo)
    print(
        json.dumps(
            {
                "confirmed": thread_id,
                "status": pr.status,
                "all_addressed": pr.all_threads_addressed(),
                "unaddressed": pr.unaddressed_threads(),
            }
        )
    )
