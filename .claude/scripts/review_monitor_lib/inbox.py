"""Ship-it pending inbox consumption and the Desktop outbound-action queue."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from utils.runtime_paths import desktop_queue_dir

from review_monitor_lib.lifecycle import cmd_register

logger = logging.getLogger(__name__)


# Shared handoff directory for ship-it → review-monitor file-drop registrations.
# Located in /tmp so macOS auto-purges it on machines without the monitor (3d).
PENDING_INBOX_DIR = Path("/tmp/review-monitor/pending")  # noqa: S108  # cross-process contract: ship-it.md + cron hardcode this literal path

# Purge pending files older than this even if they couldn't be consumed.
PENDING_STALE_AFTER = timedelta(hours=24)

# Outbound-action queue drained by the Claude Desktop schedule. The cron monitor
# performs only review-based actions itself (approve, delta-review post); every
# external message — nudge, channel bump, DM escalation, cron-failure alert — is
# written here as one JSON file for Desktop to pick up and send.
DESKTOP_QUEUE_DIR = desktop_queue_dir()
DESKTOP_ACTION_TYPES = frozenset(
    {"nudge", "channel_bump", "dm_escalation", "cron_failure"}
)
# Per-PR actions get one file per (repo, pr); the rest are batched singletons.
_PER_PR_ACTIONS = frozenset({"nudge", "dm_escalation"})


def cmd_consume_pending() -> dict[str, Any]:
    """Scan the pending-inbox directory and register each PR via cmd_register.

    Returns a summary dict:
      {"consumed": [...keys...], "skipped": [...filenames...],
      "purged": [...filenames...]}

    Successfully-registered files are deleted. Files that fail validation are
    kept until PENDING_STALE_AFTER and then purged without registering.
    """
    summary: dict[str, list[str]] = {"consumed": [], "skipped": [], "purged": []}
    if not PENDING_INBOX_DIR.exists():
        return summary

    now = datetime.now(UTC)
    for path in sorted(PENDING_INBOX_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("cmd_consume_pending: cannot read %s: %s", path, e)
            if _file_is_stale(path, now):
                path.unlink(missing_ok=True)
                summary["purged"].append(path.name)
            else:
                summary["skipped"].append(path.name)
            continue

        try:
            pr_number = int(data["pr"])
            repo = str(data["repo"])
            slack_channel = str(data["slack_channel"])
            slack_ts = str(data["slack_ts"])
            sha = str(data.get("sha", ""))
            repo_path = str(data.get("repo_path", ""))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("cmd_consume_pending: invalid payload in %s: %s", path, e)
            if _file_is_stale(path, now):
                path.unlink(missing_ok=True)
                summary["purged"].append(path.name)
            else:
                summary["skipped"].append(path.name)
            continue

        cmd_register(
            pr_number=pr_number,
            role="author",
            repo=repo,
            repo_path=repo_path,
            sha=sha,
            slack_channel=slack_channel,
            slack_ts=slack_ts,
        )
        path.unlink(missing_ok=True)
        summary["consumed"].append(f"{repo}#{pr_number}")
    return summary


def _file_is_stale(path: Path, now: datetime) -> bool:
    """Return True when *path*'s mtime is older than PENDING_STALE_AFTER."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return True
    return now - mtime >= PENDING_STALE_AFTER


def _desktop_queue_filename(
    action: str, repo: str | None, pr_number: int | None
) -> str:
    """Deterministic filename — one pending file per (repo, pr, action).

    Per-PR actions key on repo+pr; batched/singleton actions (channel_bump,
    cron_failure) get a single fixed name. Deterministic names mean a re-enqueue
    refreshes the existing file rather than piling up one per cron cycle.
    """
    if action in _PER_PR_ACTIONS:
        repo_slug = (repo or "").replace("/", "_")
        return f"{action}-{repo_slug}-{pr_number}.json"
    return f"{action}.json"


def cmd_enqueue_action(
    action: str,
    payload: dict[str, Any],
    repo: str | None = None,
    pr_number: int | None = None,
) -> dict[str, Any]:
    """Write (or refresh) one outbound-action file in the Desktop action queue.

    The cron monitor sends nothing externally — it enqueues here for the Claude
    Desktop schedule to drain. Filenames are deterministic per (repo, pr,
    action), so re-enqueuing the same action each cycle refreshes the payload
    rather than piling up duplicates. ``queued_at`` is preserved across
    refreshes; ``sent_at`` stays ``null`` until the consumer drains it. The
    real cooldowns advance only when the consumer calls ``record-*`` at drain.
    """
    if action not in DESKTOP_ACTION_TYPES:
        msg = (
            f"unknown action type {action!r};"
            f" expected one of {sorted(DESKTOP_ACTION_TYPES)}"
        )
        raise ValueError(msg)
    if action in _PER_PR_ACTIONS and (not repo or pr_number is None):
        msg = f"action {action!r} requires both --repo and --pr"
        raise ValueError(msg)

    DESKTOP_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    path = DESKTOP_QUEUE_DIR / _desktop_queue_filename(action, repo, pr_number)
    now = datetime.now(UTC).isoformat()

    # Preserve the original queued_at across cycle refreshes.
    queued_at = now
    if path.exists():
        try:
            queued_at = json.loads(path.read_text()).get("queued_at", now)
        except (json.JSONDecodeError, OSError):
            queued_at = now

    entry = {
        "action": action,
        "repo": repo,
        "pr_number": pr_number,
        "queued_at": queued_at,
        "refreshed_at": now,
        "sent_at": None,
        "payload": payload,
    }
    # Atomic write so a mid-cycle refresh never hands the consumer a torn file.
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(entry, indent=2))
    tmp.replace(path)
    return {"enqueued": str(path), "action": action, "queued_at": queued_at}
