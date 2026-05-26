"""PR event watcher daemon with tick-based polling loop."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from cw.atomic import atomic_write_text
from cw.config import (
    load_clients,
    load_orchestrator_config,
    load_state,
    pr_watcher_dir,
    review_monitor_dir,
    state_dir,
)
from cw.events import record_event
from cw.models import OrchestratorEventType, SessionStatus
from cw.orchestrate import retire_merged_prs
from cw.pr_responder import clear_completed_pr_sessions, respond_to_pr_events

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, OrchestratorEvent

# CI-failure keywords to look for in delta_findings messages
_CI_KEYWORDS = frozenset(
    {"ci", "check", "failure", "failed", "failing", "test", "build"}
)


class WatcherSnapshot(BaseModel):
    """Persisted last-seen state per client, to enable delta detection."""

    pr_states: dict[str, str] = Field(default_factory=dict)
    # key: "repo#pr_number"
    ci_fail_prs: set[str] = Field(default_factory=set)
    review_prs: set[str] = Field(default_factory=set)
    mergeable_prs: set[str] = Field(default_factory=set)


class ThrottleStore(BaseModel):
    """Prevents duplicate session dispatches per (client, pr_number, role) tuple."""

    active_dispatches: dict[str, str] = Field(default_factory=dict)
    # key: "client|repo#pr|role"  →  session_id

    def is_throttled(
        self, client_name: str, pr_key: str, role: str, state: CwState
    ) -> bool:
        """Return True if a non-completed session for this key exists."""
        dispatch_key = f"{client_name}|{pr_key}|{role}"
        session_id = self.active_dispatches.get(dispatch_key)
        if session_id is None:
            return False
        for session in state.sessions:
            if session.id == session_id and session.status != SessionStatus.COMPLETED:
                return True
        return False

    def mark_dispatched(
        self, client_name: str, pr_key: str, role: str, session_id: str
    ) -> None:
        """Record that a session was dispatched for this (client, pr_key, role)."""
        dispatch_key = f"{client_name}|{pr_key}|{role}"
        self.active_dispatches[dispatch_key] = session_id

    def clear_completed(self, state: CwState) -> None:
        """Remove dispatch entries whose sessions are now completed."""
        completed_ids = {
            s.id for s in state.sessions if s.status == SessionStatus.COMPLETED
        }
        self.active_dispatches = {
            k: v for k, v in self.active_dispatches.items() if v not in completed_ids
        }


def _load_snapshot(client_name: str) -> WatcherSnapshot:
    """Load a WatcherSnapshot for a client from disk, or return an empty one."""
    watcher_dir = pr_watcher_dir()
    watcher_dir.mkdir(parents=True, exist_ok=True)
    path = watcher_dir / f"{client_name}.json"
    if not path.exists():
        return WatcherSnapshot()
    return WatcherSnapshot.model_validate_json(path.read_text())


def _save_snapshot(client_name: str, snapshot: WatcherSnapshot) -> None:
    """Persist a WatcherSnapshot for a client atomically."""
    watcher_dir = pr_watcher_dir()
    watcher_dir.mkdir(parents=True, exist_ok=True)
    path = watcher_dir / f"{client_name}.json"
    atomic_write_text(path, snapshot.model_dump_json(indent=2))


def _load_throttle() -> ThrottleStore:
    """Load the global ThrottleStore from disk."""
    path = state_dir() / "pr_dispatch_throttle.json"
    if not path.exists():
        return ThrottleStore()
    return ThrottleStore.model_validate_json(path.read_text())


def _save_throttle(store: ThrottleStore) -> None:
    """Persist the global ThrottleStore atomically."""
    state_dir().mkdir(parents=True, exist_ok=True)
    path = state_dir() / "pr_dispatch_throttle.json"
    atomic_write_text(path, store.model_dump_json(indent=2))


def _load_monitor_files() -> list[dict[str, Any]]:
    """Load all MonitorState JSON files from the review-monitor directory.

    Returns a list of raw dicts, each corresponding to one file's contents.
    """
    monitor_dir = review_monitor_dir()
    if not monitor_dir.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in monitor_dir.glob("*.json"):
        try:
            raw: dict[str, Any] = json.loads(path.read_text())
            result.append(raw)
        except (json.JSONDecodeError, OSError):
            continue
    return result


def _has_ci_failure(delta_findings: list[dict[str, Any]]) -> bool:
    """Return True if any delta_finding contains CI/failure keywords."""
    for finding in delta_findings:
        message = str(finding.get("message", "")).lower()
        if any(kw in message for kw in _CI_KEYWORDS):
            return True
    return False


def _count_unresolved_threads(thread_status: dict[str, Any]) -> int:
    """Count unresolved threads from a thread_status dict."""
    count = 0
    for thread_data in thread_status.values():
        resolved = False
        if isinstance(thread_data, dict):
            resolved = bool(thread_data.get("resolved", False))
        if not resolved:
            count += 1
    return count


def _all_threads_addressed(thread_status: dict[str, Any]) -> bool:
    """Return True if all threads are marked resolved."""
    if not thread_status:
        return True
    return all(
        bool(v.get("resolved", False)) if isinstance(v, dict) else False
        for v in thread_status.values()
    )


_DEFAULT_CHANNEL_URL = "http://127.0.0.1:8788/pr-event"


def _post_to_channel(
    event_type: str,
    repo: str,
    pr_number: int,
    payload: dict[str, Any],
) -> None:
    """Post a PR event to the channel server. Best-effort: logs on failure."""
    url = os.environ.get("CW_PR_EVENTS_URL", _DEFAULT_CHANNEL_URL)
    body = json.dumps(
        {
            "repo": repo,
            "pr_number": pr_number,
            "event_type": event_type,
            "payload": payload,
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=2)  # noqa: S310
    except Exception:  # noqa: BLE001
        # Best-effort: channel server may not be running; never block PR watching.
        # Justification: (1) failure modes are connection refused/timeout/HTTP error,
        # (2) logged at debug with exc_info=True for traceability,
        # (3) non-critical — PR watching must continue regardless,
        # (4) tests verify resilience (test_channel_server_down_does_not_raise).
        logger.debug(
            "_post_to_channel failed url=%s event_type=%s",
            url,
            event_type,
            exc_info=True,
        )


def watch_prs_for_client(
    client: ClientConfig,
    snapshot: WatcherSnapshot,
) -> tuple[list[OrchestratorEvent], WatcherSnapshot]:
    """Scan review-monitor files for this client and emit delta events.

    Args:
        client: The client config whose workspace path filters PRs.
        snapshot: Previously-saved snapshot of last-seen PR states.

    Returns:
        A tuple of (new events emitted, updated snapshot).
    """
    workspace_str = str(client.workspace_path)

    monitor_files = _load_monitor_files()

    events: list[OrchestratorEvent] = []
    new_snapshot = WatcherSnapshot(
        pr_states=dict(snapshot.pr_states),
        ci_fail_prs=set(snapshot.ci_fail_prs),
        review_prs=set(snapshot.review_prs),
        mergeable_prs=set(snapshot.mergeable_prs),
    )

    for file_data in monitor_files:
        active_prs: dict[str, Any] = file_data.get("active", {})
        for pr_data in active_prs.values():
            if not isinstance(pr_data, dict):
                continue

            repo_path = str(pr_data.get("repo_path", ""))
            if not repo_path.startswith(workspace_str):
                continue

            repo = str(pr_data.get("repo", ""))
            pr_number = pr_data.get("pr_number", 0)
            status = str(pr_data.get("status", "watching"))
            role = str(pr_data.get("role", "author"))
            thread_status: dict[str, Any] = pr_data.get("thread_status", {})
            delta_findings: list[dict[str, Any]] = pr_data.get("delta_findings", [])

            pr_key = f"{repo}#{pr_number}"
            prev_status = new_snapshot.pr_states.get(pr_key)

            # --- PR_MERGED: status transitioned to complete or abandoned ---
            if status in ("complete", "abandoned") and prev_status not in (
                "complete",
                "abandoned",
            ):
                event = record_event(
                    OrchestratorEventType.PR_MERGED,
                    {
                        "client": client.name,
                        "repo": repo,
                        "pr_number": pr_number,
                        "role": role,
                        "status": status,
                    },
                )
                events.append(event)
                _post_to_channel(
                    "merged", repo, pr_number, {"status": status, "role": role}
                )
                new_snapshot.pr_states[pr_key] = status
                continue

            # Update state for watching PRs
            new_snapshot.pr_states[pr_key] = status

            if status != "watching":
                continue

            unresolved_now = _count_unresolved_threads(thread_status)

            # --- PR_REVIEW_RECEIVED: new unresolved threads since last tick ---
            if unresolved_now > 0 and pr_key not in new_snapshot.review_prs:
                event = record_event(
                    OrchestratorEventType.PR_REVIEW_RECEIVED,
                    {
                        "client": client.name,
                        "repo": repo,
                        "pr_number": pr_number,
                        "role": role,
                        "unresolved_threads": unresolved_now,
                    },
                )
                events.append(event)
                _post_to_channel(
                    "review_received",
                    repo,
                    pr_number,
                    {"unresolved_threads": unresolved_now, "role": role},
                )
                new_snapshot.review_prs.add(pr_key)

            # --- PR_MERGEABLE: all threads addressed (and we've seen review before) ---
            if (
                pr_key in new_snapshot.review_prs
                and pr_key not in new_snapshot.mergeable_prs
                and _all_threads_addressed(thread_status)
                and unresolved_now == 0
                and thread_status  # at least one thread existed
            ):
                event = record_event(
                    OrchestratorEventType.PR_MERGEABLE,
                    {
                        "client": client.name,
                        "repo": repo,
                        "pr_number": pr_number,
                        "role": role,
                    },
                )
                events.append(event)
                _post_to_channel("mergeable", repo, pr_number, {"role": role})
                new_snapshot.mergeable_prs.add(pr_key)

            # --- PR_CI_FAILED: delta_findings mention CI failure ---
            if (
                pr_key not in new_snapshot.ci_fail_prs
                and delta_findings
                and _has_ci_failure(delta_findings)
            ):
                event = record_event(
                    OrchestratorEventType.PR_CI_FAILED,
                    {
                        "client": client.name,
                        "repo": repo,
                        "pr_number": pr_number,
                        "role": role,
                        "findings_count": len(delta_findings),
                    },
                )
                events.append(event)
                _post_to_channel(
                    "ci_failed",
                    repo,
                    pr_number,
                    {"findings_count": len(delta_findings), "role": role},
                )
                new_snapshot.ci_fail_prs.add(pr_key)

    return events, new_snapshot


def run_watcher_tick(*, once: bool = False) -> None:
    """Run the PR watcher daemon loop.

    Args:
        once: If True, run a single tick and return. Otherwise loop forever.
    """
    config = load_orchestrator_config()
    interval = config.tick_interval_seconds

    while True:
        clients = load_clients()
        state = load_state()
        throttle = _load_throttle()
        throttle.clear_completed(state)
        _save_throttle(throttle)

        for client_name, client in clients.items():
            snapshot = _load_snapshot(client_name)
            _events, updated_snapshot = watch_prs_for_client(client, snapshot)
            _save_snapshot(client_name, updated_snapshot)

        # Respond to queued PR events and clean up completed dispatch records
        state = load_state()
        clear_completed_pr_sessions(state)
        respond_to_pr_events()

        # Retire merged PRs: close sessions, drop dispatch entries, emit events.
        try:
            retire_merged_prs()
        except Exception:  # pragma: no cover - defensive in long-running loop
            logger.exception("retire_merged_prs failed")

        if once:
            return

        time.sleep(interval)
