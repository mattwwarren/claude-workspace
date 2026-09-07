#!/usr/bin/env python3
"""
Review monitor: track PR review threads and nudge authors/reviewers.

Subcommands (to be added in subsequent tasks):
  register  — Start monitoring a PR
  drop      — Stop monitoring a PR
  complete  — Mark a PR as done
  status    — Show current monitor state
  check     — Run one monitoring cycle (resolve threads, detect deferrals, nudge)
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable

sys.path.insert(0, str(Path(__file__).parent))

from utils.runtime_paths import desktop_queue_dir, review_monitor_dir

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Central state directory — survives worktree cleanup
CENTRAL_STATE_DIR = review_monitor_dir()

# Legacy state file path (for migration — used in Task 2)
LEGACY_STATE_FILE = Path(".claude/review-monitor-state.json")

# How many lines around a changed line to consider "code changed" for a thread
CODE_CHANGE_WINDOW = 5

# Minimum time between nudge messages for the same PR
NUDGE_COOLDOWN = timedelta(hours=24)

# Business-hour configuration for stale-PR channel bumps
BUSINESS_TZ = ZoneInfo("America/New_York")
BUSINESS_START_HOUR = 8  # 8a ET
BUSINESS_END_HOUR = 18  # 6p ET
FIRST_WEEKEND_WEEKDAY = 5  # datetime.weekday(): Mon=0..Fri=4, Sat=5, Sun=6
STALE_REVIEW_THRESHOLD_MIN = 240  # 4 business hours
CHANNEL_BUMP_COOLDOWN = timedelta(hours=24)
AUTO_FIX_DAILY_CAP = 2

# Canonical clone path per repo. Monitored PRs must be registered against a
# stable clone, never an ephemeral agent worktree (those are created and torn
# down per task — a registered worktree path goes stale and breaks `check`'s
# git diff). Auto-fix agents cut their own worktree from the canonical clone,
# so the stored path only needs to be a valid, persistent checkout of `repo`.
CANONICAL_REPO_PATHS: dict[str, str] = {
    # "owner/repo": "/path/to/canonical/clone",
}


def _canonical_repo_path(repo: str, given: str) -> str:
    """Return the canonical clone path for *repo*, falling back to *given*.

    Normalizes away ephemeral agent-worktree paths at registration time.
    """
    return CANONICAL_REPO_PATHS.get(repo, given)


# Minimum wall-clock time between DM escalations for the same PR.
# Without this, _dm_escalation_reason returns "week_old"/"loop" on every cycle
# and the skill fires a DM each time. 4h matches the user-requested cadence.
DM_ESCALATION_COOLDOWN = timedelta(hours=4)

# GitHub login suffixes that identify automated accounts
BOT_LOGIN_SUFFIXES: tuple[str, ...] = ("[bot]", "-ai", "-bot")
KNOWN_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "sourcery-ai",
        "coderabbitai",
        "dependabot",
        "renovate",
        "github-actions",
        "codecov-commenter",
    }
)
# Bots whose findings BLOCK merge — must be treated as human-equivalent for
# auto-fix purposes even though the login matches a bot pattern.
MERGE_BLOCKING_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "sonarqubecloud",
        "sonarcloud",
        "sonarqube",
        "sonarcloud[bot]",
        "sonarqubecloud[bot]",
    }
)

# Patterns in review comments that indicate the author is deferring work
DEFERRAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bfollow[\s-]?up\b", re.IGNORECASE),
    re.compile(r"\bseparate\s+pr\b", re.IGNORECASE),
    re.compile(r"\bout\s+of\s+scope\b", re.IGNORECASE),
    re.compile(r"\bnext\s+sprint\b", re.IGNORECASE),
    re.compile(r"\bwill\s+address\s+later\b", re.IGNORECASE),
    re.compile(r"\btracking\s+in\b", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ThreadStatus:
    """Status of a single review thread."""

    file: str
    # GitHub reports ``line`` as null for outdated / moved review threads
    # (the comment's anchor no longer maps to a current line). None is valid.
    line: int | None
    resolved: bool = False
    replied: bool = False
    code_changed: bool = False
    deferred: bool = False

    @property
    def is_addressed(self) -> bool:
        """Return True if the thread has been addressed in any substantive way.

        Deferred alone does NOT count — the work is acknowledged but not done.
        """
        return self.resolved or self.replied or self.code_changed

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "file": self.file,
            "line": self.line,
            "resolved": self.resolved,
            "replied": self.replied,
            "code_changed": self.code_changed,
            "deferred": self.deferred,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThreadStatus:
        """Deserialize from a dictionary."""
        return cls(
            file=data["file"],
            line=data["line"],
            resolved=data.get("resolved", False),
            replied=data.get("replied", False),
            code_changed=data.get("code_changed", False),
            deferred=data.get("deferred", False),
        )


@dataclass
class CommentReviewRef:
    """A non-bot ``COMMENTED`` review on an author-role PR.

    GitHub records "Comment"-radio reviews as ``state: COMMENTED``, which does
    not move ``reviewDecision`` off ``REVIEW_REQUIRED``. We track them so the
    skill can classify them as a fallback change-request signal when no
    higher-priority attention state fires. Classification is persisted so we
    only spend tokens once per review.
    """

    review_id: str
    author: str
    submitted_at: str  # ISO-8601 from GitHub
    body: str  # truncated to bound state file size
    classification: str = (
        "unclassified"  # "unclassified" | "requests_changes" | "neutral"
    )
    classified_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "author": self.author,
            "submitted_at": self.submitted_at,
            "body": self.body,
            "classification": self.classification,
            "classified_at": self.classified_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommentReviewRef:
        return cls(
            review_id=data["review_id"],
            author=data["author"],
            submitted_at=data["submitted_at"],
            body=data["body"],
            classification=data.get("classification", "unclassified"),
            classified_at=data.get("classified_at"),
        )


@dataclass
class MonitoredPR:
    """A PR being actively monitored."""

    role: str
    """Our role on this PR: "reviewer" or "author"."""

    repo: str
    """GitHub repo in "owner/name" format."""

    repo_path: str
    """Absolute path to the local clone of this repo."""

    pr_number: int
    """PR number on GitHub."""

    last_seen_sha: str
    """HEAD SHA the last time we checked this PR. Advances every check cycle."""

    delta_base_sha: str = ""
    """SHA through which the reviewer delta review is confirmed complete.

    The delta diff is ``git diff <delta_base_sha>..<HEAD>``. Unlike
    ``last_seen_sha`` (which tracks observed HEAD and advances every cycle),
    this only advances when the skill's Step 3 *positively* acks the delta via
    the ``ack-delta`` subcommand — or when ``register`` re-anchors the PR.

    Decoupling the two prevents a lossy read: an unconsumed reviewer delta is
    no longer dropped just because ``check`` happened to observe it. Defaults
    to ``last_seen_sha`` for entries registered before this field existed.
    """

    registered_at: str = ""
    """ISO-8601 timestamp when this PR was registered."""

    last_checked_at: str = ""
    """ISO-8601 timestamp of the most recent check cycle."""

    last_nudge_at: str | None = None
    """ISO-8601 timestamp of the last nudge actually sent, or None if never.

    Set by ``record-nudge`` — which the Desktop schedule calls at *drain* time,
    not when the cron enqueues the nudge. Drives the 24h nudge cooldown.
    """

    nudge_count: int = 0
    """Total nudges actually sent for this PR (incremented by ``record-nudge``)."""

    our_review_id: str | None = None
    """GitHub review ID for the review we posted (reviewer role only)."""

    our_threads: list[str] = field(default_factory=list)
    """Thread IDs we own (reviewer: threads we opened; author: threads on our PR)."""

    thread_status: dict[str, ThreadStatus] = field(default_factory=dict)
    """Mapping from thread ID to its current ThreadStatus."""

    delta_findings: list[dict[str, Any]] = field(default_factory=list)
    """New findings discovered on HEAD commits since our last review."""

    status: str = "watching"
    """Current lifecycle status: "watching" | "complete" | "abandoned"."""

    slack_channel: str | None = None
    """Slack channel ID where this PR was announced (e.g. 'C0123456')."""

    slack_ts: str | None = None
    """Parent message ts for the PR announcement thread."""

    slack_last_seen_ts: str | None = None
    """Most recent thread message ts already surfaced; drives incremental reads."""

    last_notified_state: str | None = None
    """The author-attention state most recently local-pinged for
    (e.g. 'ready_to_approve')."""

    last_notified_at: str | None = None
    """ISO-8601 timestamp of the most recent local ping fired."""

    last_escalated_at: str | None = None
    """ISO-8601 timestamp of the most recent escalation actually sent.

    Set by ``mark-escalated`` at Desktop *drain* time, not at cron enqueue.
    """

    escalation_count: int = 0
    """Total DM escalations actually sent for this PR
    (incremented by ``mark-escalated``)."""

    auto_fix_attempts_today: int = 0
    """Count of agent auto-fix dispatches today (resets on date change)."""

    auto_fix_attempt_date: str | None = None
    """YYYY-MM-DD (UTC) the counter is scoped to."""

    last_auto_fix_at: str | None = None
    """ISO-8601 timestamp of the most recent auto-fix dispatch for this PR.

    Used to suppress redundant auto-fix runs while we're already waiting on a
    reviewer response. If ``last_auto_fix_at > state_entered_at``, we have
    already responded to the *current* attention_state instance — dispatching
    again would just re-investigate or re-request review. State transitions
    (``_ensure_state_entered_at`` bumps ``state_entered_at``) reset eligibility.
    """

    last_channel_bump_at: str | None = None
    """ISO-8601 timestamp of the most recent review-channel stale-review bump
    actually sent. Set by ``record-channel-bump`` at Desktop *drain* time."""

    channel_bump_count: int = 0
    """Total channel bumps actually sent for this PR
    (incremented by ``record-channel-bump``)."""

    state_entered_at: str | None = None
    """ISO-8601 timestamp when the current attention_state was first observed."""

    comment_reviews: dict[str, CommentReviewRef] = field(default_factory=dict)
    """Non-bot ``COMMENTED`` reviews observed on this PR, keyed by review_id.

    Reset when the author pushes a new commit, or pruned when a more recent
    formal review (``CHANGES_REQUESTED`` / ``APPROVED``) supersedes them.
    Classification verdicts persist across cycles to avoid re-spending tokens.
    """

    awaiting_rereview: bool = False
    """True when this author PR is ``ready_to_approve`` *and* a human reviewer
    has already engaged (left a ``COMMENTED`` / ``CHANGES_REQUESTED`` review).

    Distinguishes "waiting on a re-review" (reviewer has stale context, a
    delta-check is due) from "waiting on a first review". Recomputed every
    cycle; persisted only so ``cmd_status`` can display it without an extra
    GitHub call.
    """

    def __post_init__(self) -> None:
        """Set timestamps if they were not provided."""
        now = datetime.now(UTC).isoformat()
        if not self.registered_at:
            self.registered_at = now
        if not self.last_checked_at:
            self.last_checked_at = now
        if not self.delta_base_sha:
            self.delta_base_sha = self.last_seen_sha

    def all_threads_addressed(self) -> bool:
        """Return True if every tracked thread has been addressed."""
        return all(ts.is_addressed for ts in self.thread_status.values())

    def unaddressed_threads(self) -> list[str]:
        """Return the IDs of threads that have not yet been addressed."""
        return [tid for tid, ts in self.thread_status.items() if not ts.is_addressed]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "role": self.role,
            "repo": self.repo,
            "repo_path": self.repo_path,
            "pr_number": self.pr_number,
            "last_seen_sha": self.last_seen_sha,
            "delta_base_sha": self.delta_base_sha,
            "registered_at": self.registered_at,
            "last_checked_at": self.last_checked_at,
            "last_nudge_at": self.last_nudge_at,
            "nudge_count": self.nudge_count,
            "our_review_id": self.our_review_id,
            "our_threads": self.our_threads,
            "thread_status": {
                tid: ts.to_dict() for tid, ts in self.thread_status.items()
            },
            "delta_findings": self.delta_findings,
            "status": self.status,
            "slack_channel": self.slack_channel,
            "slack_ts": self.slack_ts,
            "slack_last_seen_ts": self.slack_last_seen_ts,
            "last_notified_state": self.last_notified_state,
            "last_notified_at": self.last_notified_at,
            "last_escalated_at": self.last_escalated_at,
            "escalation_count": self.escalation_count,
            "auto_fix_attempts_today": self.auto_fix_attempts_today,
            "auto_fix_attempt_date": self.auto_fix_attempt_date,
            "last_auto_fix_at": self.last_auto_fix_at,
            "last_channel_bump_at": self.last_channel_bump_at,
            "channel_bump_count": self.channel_bump_count,
            "state_entered_at": self.state_entered_at,
            "comment_reviews": {
                rid: ref.to_dict() for rid, ref in self.comment_reviews.items()
            },
            "awaiting_rereview": self.awaiting_rereview,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitoredPR:
        """Deserialize from a dictionary."""
        thread_status_raw: dict[str, Any] = data.get("thread_status", {})
        thread_status = {
            tid: ThreadStatus.from_dict(ts) for tid, ts in thread_status_raw.items()
        }
        comment_reviews_raw: dict[str, Any] = data.get("comment_reviews", {})
        comment_reviews = {
            rid: CommentReviewRef.from_dict(d) for rid, d in comment_reviews_raw.items()
        }
        return cls(
            role=data["role"],
            repo=data["repo"],
            repo_path=data["repo_path"],
            pr_number=data["pr_number"],
            last_seen_sha=data["last_seen_sha"],
            delta_base_sha=data.get("delta_base_sha", ""),
            registered_at=data.get("registered_at", ""),
            last_checked_at=data.get("last_checked_at", ""),
            last_nudge_at=data.get("last_nudge_at"),
            nudge_count=data.get("nudge_count", 0),
            our_review_id=data.get("our_review_id"),
            our_threads=data.get("our_threads", []),
            thread_status=thread_status,
            delta_findings=data.get("delta_findings", []),
            status=data.get("status", "watching"),
            slack_channel=data.get("slack_channel"),
            slack_ts=data.get("slack_ts"),
            slack_last_seen_ts=data.get("slack_last_seen_ts"),
            last_notified_state=data.get("last_notified_state"),
            last_notified_at=data.get("last_notified_at"),
            last_escalated_at=data.get("last_escalated_at"),
            escalation_count=data.get("escalation_count", 0),
            auto_fix_attempts_today=data.get("auto_fix_attempts_today", 0),
            auto_fix_attempt_date=data.get("auto_fix_attempt_date"),
            last_auto_fix_at=data.get("last_auto_fix_at"),
            last_channel_bump_at=data.get("last_channel_bump_at"),
            channel_bump_count=data.get("channel_bump_count", 0),
            state_entered_at=data.get("state_entered_at"),
            comment_reviews=comment_reviews,
            awaiting_rereview=data.get("awaiting_rereview", False),
        )


@dataclass
class MonitorState:
    """Top-level monitor state persisted to disk."""

    monitored: dict[str, MonitoredPR]
    """Active PRs keyed by "<repo>#<pr_number>"."""

    completed: dict[str, dict[str, Any]]
    """Completed/abandoned PRs keyed by "<repo>#<pr_number>"."""

    def complete_pr(self, key: str, reason: str) -> None:
        """Move a PR from monitored to completed.

        Args:
            key: The "<repo>#<pr_number>" key identifying the PR.
            reason: Why the PR is being completed (e.g. "merged", "abandoned").
        """
        if key not in self.monitored:
            logger.warning("complete_pr: key %r not in monitored", key)
            return
        pr_dict = self.monitored.pop(key).to_dict()
        pr_dict["completed_at"] = datetime.now(UTC).isoformat()
        pr_dict["reason"] = reason
        self.completed[key] = pr_dict

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "monitored": {k: v.to_dict() for k, v in self.monitored.items()},
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorState:
        """Deserialize from a dictionary."""
        monitored_raw: dict[str, Any] = data.get("monitored", {})
        monitored = {k: MonitoredPR.from_dict(v) for k, v in monitored_raw.items()}
        return cls(
            monitored=monitored,
            completed=data.get("completed", {}),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def state_path_for_repo(repo: str) -> Path:
    """Return central state path for a repo like 'owner/repo'."""
    safe_name = repo.replace("/", "--")
    return CENTRAL_STATE_DIR / f"{safe_name}.json"


def _merge_states(central: MonitorState, legacy: MonitorState) -> MonitorState:
    """Merge legacy state into central, keeping newer entries per PR."""
    for key, legacy_pr in legacy.monitored.items():
        if key not in central.monitored:
            central.monitored[key] = legacy_pr
        else:
            central_pr = central.monitored[key]
            if legacy_pr.last_checked_at > central_pr.last_checked_at:
                central.monitored[key] = legacy_pr
    for key, completed_data in legacy.completed.items():
        if key not in central.completed:
            central.completed[key] = completed_data
    return central


def load_state(repo: str) -> MonitorState:
    """Load monitor state for a specific repo from the central directory.

    If no central file exists but a legacy ``.claude/review-monitor-state.json``
    is present in the current directory, the legacy data is migrated
    automatically: it is merged into central (newer ``last_checked_at`` wins
    per PR), saved to the central location, and the legacy file is deleted.
    """
    state_file = state_path_for_repo(repo)
    central_state: MonitorState | None = None

    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            central_state = MonitorState.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
            logger.warning(
                "Corrupt monitor state file %s, starting fresh: %s", state_file, e
            )
            central_state = MonitorState(monitored={}, completed={})

    # Legacy migration: check for old per-project state file
    if LEGACY_STATE_FILE.exists():
        try:
            legacy_data = json.loads(LEGACY_STATE_FILE.read_text())
            legacy_state = MonitorState.from_dict(legacy_data)
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
            logger.warning(
                "Corrupt legacy state file %s, skipping migration: %s",
                LEGACY_STATE_FILE,
                e,
            )
            legacy_state = None

        if legacy_state is not None:
            central_state = (
                legacy_state
                if central_state is None
                else _merge_states(central_state, legacy_state)
            )
            # Persist merged state and remove legacy file
            save_state(central_state, repo)
            try:
                LEGACY_STATE_FILE.unlink()
                logger.info(
                    "Migrated legacy state from %s to central directory",
                    LEGACY_STATE_FILE,
                )
            except OSError as e:
                logger.warning(
                    "Could not delete legacy state file %s: %s", LEGACY_STATE_FILE, e
                )

    if central_state is None:
        return MonitorState(monitored={}, completed={})
    return central_state


def save_state(state: MonitorState, repo: str) -> None:
    """Save monitor state atomically to the central directory."""
    state_file = state_path_for_repo(repo)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = state_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(state.to_dict(), indent=2) + "\n")
    tmp_file.rename(state_file)


# ---------------------------------------------------------------------------
# GitHub / git helpers
# ---------------------------------------------------------------------------


def _run_gh(args: list[str], repo: str | None = None) -> str:
    """Run a gh CLI command and return stdout.

    Returns empty string on failure (FileNotFoundError or CalledProcessError).
    If *repo* is provided, adds ``-R repo`` to the command.
    """
    cmd = ["gh", *args]
    if repo is not None:
        cmd = ["gh", "-R", repo, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        logger.warning("gh CLI not found. Install it: https://cli.github.com/")
        return ""
    except subprocess.CalledProcessError as e:
        logger.warning("gh command failed: %s\n%s", " ".join(cmd), e.stderr.strip())
        return ""
    return result.stdout.strip()


def _run_git(args: list[str], cwd: str | None = None) -> str:
    """Run a git command and return stdout.

    Returns empty string on failure (FileNotFoundError or CalledProcessError).
    If *cwd* is provided, adds ``-C cwd`` to the command.
    """
    cmd = ["git", *args]
    if cwd is not None:
        cmd = ["git", "-C", cwd, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        logger.warning("git not found in PATH")
        return ""
    except subprocess.CalledProcessError as e:
        logger.warning("git command failed: %s\n%s", " ".join(cmd), e.stderr.strip())
        return ""
    return result.stdout.strip()


def is_deferral(text: str) -> bool:
    """Return True if *text* contains deferral language.

    Checks against all patterns in :data:`DEFERRAL_PATTERNS`.
    """
    return any(pat.search(text) for pat in DEFERRAL_PATTERNS)


def check_code_changed(
    file_path: str,
    line: int | None,
    changed_lines: dict[str, set[int]],
) -> bool:
    """Return True if any changed line falls within a window around *line*.

    The window is ``[line - CODE_CHANGE_WINDOW, line + CODE_CHANGE_WINDOW]``
    (inclusive on both ends).

    A *line* of None (an outdated / moved review thread, which GitHub anchors
    to no current line) has no window to test, so it never counts as touched.
    """
    lines = changed_lines.get(file_path)
    if lines is None or line is None:
        return False
    low = line - CODE_CHANGE_WINDOW
    high = line + CODE_CHANGE_WINDOW
    return any(low <= changed <= high for changed in lines)


def parse_diff_changed_lines(diff_output: str) -> dict[str, set[int]]:
    """Parse a unified diff and return added line numbers keyed by file path.

    Only lines that are *added* (``+`` prefix, not ``+++`` header) in the new
    version are included.  The file paths are taken from ``+++ b/<path>``
    headers.

    Returns
    -------
        Mapping of file path to set of new-file line numbers that were added.
    """
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    current_line: int = 0  # tracks position in the new file

    # Matches: @@ -old_start[,old_count] +new_start[,new_count] @@
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for raw_line in diff_output.splitlines():
        # New-file header — e.g. "+++ b/src/foo.py"
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]  # strip "+++ b/"
            result.setdefault(current_file, set())
            current_line = 0
            continue

        # Hunk header — reset the new-file line counter
        m = hunk_re.match(raw_line)
        if m:
            current_line = int(m.group(1))
            continue

        if current_file is None:
            continue

        if raw_line.startswith("+++"):
            # Skip the +++ header line itself (already handled above)
            continue

        if raw_line.startswith("+"):
            # Added line — record and advance
            result[current_file].add(current_line)
            current_line += 1
        elif raw_line.startswith("-"):
            # Removed line — does not advance new-file counter
            pass
        else:
            # Context line — advance new-file counter
            current_line += 1

    return result


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


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
) -> None:
    """Register or update a PR for monitoring.

    For updates: merges new threads (no duplicates), updates SHA.
    *thread_details* is a list of ``{"id": "PRRT_x", "file": "path", "line": N}``
    dicts used to build :class:`ThreadStatus` entries.

    *repo_path* is normalized to the canonical clone — a PR registered against
    an ephemeral agent worktree would break once that worktree is cleaned up.
    """
    threads = _normalize_thread_ids(threads)
    repo_path = _canonical_repo_path(repo, repo_path)
    state = load_state(repo)
    key = f"{repo}#{pr_number}"

    if key in state.monitored:
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


def cmd_drop(pr_number: int, repo: str) -> None:
    """Remove a PR from monitoring. No-op if not found."""
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key in state.monitored:
        del state.monitored[key]
        save_state(state, repo)


def cmd_complete(pr_number: int, repo: str, reason: str) -> None:
    """Mark a PR as complete and move it out of active monitoring.

    No-op if not found.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        logger.info("cmd_complete: %r not found in monitored, ignoring", key)
        return
    state.complete_pr(key, reason)
    save_state(state, repo)


def _nudge_activity_check(pr_number: int, repo: str) -> dict[str, Any] | None:
    """Return a blocking ``{"allowed": False, ...}`` if PR activity suppresses a nudge.

    No nudge if the PR was updated within the last 24h. Fails **closed**: an
    unreachable or unparseable gh response blocks the nudge rather than risking
    a premature one. Returns ``None`` when a nudge is not blocked on this basis.
    """
    updated_raw = _run_gh(
        ["pr", "view", str(pr_number), "--json", "updatedAt"], repo=repo
    )
    if not updated_raw:
        return {
            "allowed": False,
            "reason": "could not fetch PR activity — skipping nudge to be safe",
        }
    try:
        updated_at_str: str = json.loads(updated_raw)["updatedAt"]
        updated_at = datetime.fromisoformat(updated_at_str)
    except (json.JSONDecodeError, ValueError, KeyError):
        return {
            "allowed": False,
            "reason": "could not parse PR activity — skipping nudge to be safe",
        }
    if datetime.now(UTC) - updated_at < NUDGE_COOLDOWN:
        return {"allowed": False, "reason": "PR has recent activity"}
    return None


def cmd_nudge_ok(pr_number: int, repo: str) -> dict[str, Any]:
    """Return whether a nudge is allowed for the given PR.

    Returns ``{"allowed": True/False, "reason": "..."}``.
    Allowed when ALL hold:

    - *last_nudge_at* is ``None`` or 24+ hours ago (cooldown);
    - the PR has been monitored for 24+ hours (grace period — the author needs
      a fair chance to respond before a first nudge);
    - the PR has had no activity (commits or comments) in the last 24 hours.

    The activity check fails **closed**: if PR activity cannot be determined,
    the nudge is skipped rather than risk a premature one.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        return {"allowed": False, "reason": f"PR {key} not found in monitored"}
    pr = state.monitored[key]

    # Cooldown check
    if pr.last_nudge_at is not None:
        last = datetime.fromisoformat(pr.last_nudge_at)
        elapsed = datetime.now(UTC) - last
        if elapsed < NUDGE_COOLDOWN:
            remaining = NUDGE_COOLDOWN - elapsed
            return {
                "allowed": False,
                "reason": f"cooldown active, {remaining} remaining",
            }

    # Grace period — never nudge before the author has had a fair chance to
    # respond. registered_at ~= when our review was posted; a nudge 43 min after
    # a review reads as pressure, not a check-in.
    if pr.registered_at:
        try:
            registered = datetime.fromisoformat(pr.registered_at)
            since_registered = datetime.now(UTC) - registered
            if since_registered < NUDGE_COOLDOWN:
                return {
                    "allowed": False,
                    "reason": f"grace period — registered {since_registered} ago",
                }
        except ValueError:
            pass

    activity_block = _nudge_activity_check(pr_number, pr.repo)
    if activity_block is not None:
        return activity_block

    if pr.last_nudge_at is None:
        return {"allowed": True, "reason": "never nudged"}
    last_nudge = datetime.fromisoformat(pr.last_nudge_at)
    elapsed_nudge = datetime.now(UTC) - last_nudge
    return {"allowed": True, "reason": f"last nudge was {elapsed_nudge} ago"}


def cmd_record_nudge(pr_number: int, repo: str) -> None:
    """Record that a nudge was actually sent for the given PR right now.

    Called by the Desktop schedule at *drain* time — never by the cron at
    enqueue time. Advances the 24h cooldown and increments ``nudge_count``.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        logger.warning("cmd_record_nudge: %r not found in monitored", key)
        return
    pr = state.monitored[key]
    pr.last_nudge_at = datetime.now(UTC).isoformat()
    pr.nudge_count += 1
    save_state(state, repo)


# States that indicate the author (user) needs to take action on their own PR.
USER_ATTENTION_STATES: frozenset[str] = frozenset(
    {"ready_to_approve", "ci_failing", "merge_blocked"}
)

# Minimum gap between local ping and Slack escalation for the same state.
ESCALATION_GRACE = timedelta(minutes=15)


def cmd_mark_notified(pr_number: int, repo: str, state_value: str) -> None:
    """Record that a local ping has fired for *state_value* on this PR right now."""
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        logger.warning("cmd_mark_notified: %r not found in monitored", key)
        return
    pr = state.monitored[key]
    pr.last_notified_state = state_value
    pr.last_notified_at = datetime.now(UTC).isoformat()
    save_state(state, repo)


def cmd_mark_escalated(pr_number: int, repo: str) -> None:
    """Record that a DM escalation was actually sent for this PR right now.

    Called by the Desktop schedule at *drain* time. Advances the escalation
    cooldown and increments ``escalation_count``.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        logger.warning("cmd_mark_escalated: %r not found in monitored", key)
        return
    pr = state.monitored[key]
    pr.last_escalated_at = datetime.now(UTC).isoformat()
    pr.escalation_count += 1
    save_state(state, repo)


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


def cmd_catchup() -> dict[str, list[str]]:
    """Mark every currently-attention-required author PR as already notified.

    Used after a fresh deploy to avoid a first-cycle ping burst on the existing
    backlog. Does NOT fire any notifications; only updates state. The repo-less
    signature scans every state file in the central directory.

    Returns {"marked": [list of "<repo>#<pr>" keys updated]}.
    """
    marked: list[str] = []
    now_iso = datetime.now(UTC).isoformat()
    for repo in cmd_list_repos():
        state = load_state(repo)
        dirty = False
        for key, pr in state.monitored.items():
            if pr.role != "author":
                continue
            if pr.last_notified_state is not None:
                continue
            attention = _compute_attention_state(
                role=pr.role,
                status=pr.status,
                ci_ok=True,  # we don't have fresh check data; conservative assumption
                merge_blocked=False,
            )
            if attention is None:
                continue
            pr.last_notified_state = attention
            pr.last_notified_at = now_iso
            marked.append(key)
            dirty = True
        if dirty:
            save_state(state, repo)
    return {"marked": marked}


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


def cmd_mark_comment_review(
    pr_number: int, repo: str, review_id: str, classification: str
) -> dict[str, Any]:
    """Persist the skill classifier's verdict on a tracked comment review.

    ``classification`` must be either ``"requests_changes"`` or ``"neutral"``.
    A ``requests_changes`` verdict makes ``has_actionable_comment_review`` true
    on the next ``check``, promoting ``attention_state`` to ``changes_requested``
    via the fallback branch in ``_compute_attention_state``.
    """
    if classification not in ("requests_changes", "neutral"):
        return {"error": f"invalid classification {classification!r}"}
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    pr = state.monitored.get(key)
    if pr is None:
        return {"error": f"PR {key} not found in monitored"}
    ref = pr.comment_reviews.get(review_id)
    if ref is None:
        return {"error": f"review_id {review_id} not tracked on {key}"}
    ref.classification = classification
    ref.classified_at = datetime.now(UTC).isoformat()
    save_state(state, repo)
    return {
        "ok": True,
        "pr_number": pr_number,
        "review_id": review_id,
        "classification": classification,
    }


@functools.lru_cache(maxsize=1)
def _get_our_username() -> str:
    """Return the authenticated GitHub username.

    Calls ``gh api user --jq .login`` and returns the result stripped of
    surrounding whitespace.  Returns an empty string if the call fails.
    """
    return _run_gh(["api", "user", "--jq", ".login"]).strip()


def _extract_login(comment: dict[str, Any]) -> str:
    """Extract login from a review comment, handling both str and dict author fields."""
    author = comment.get("author", "")
    if isinstance(author, str):
        return author
    return (author or {}).get("login", "")


def _discover_author_threads(
    pr: MonitoredPR,
    threads: list[dict[str, Any]],
    our_username: str,
) -> None:
    """Discover threads opened by others on an author-role PR.

    Mutates *pr.our_threads* and *pr.thread_status* in place.
    """
    for thread in threads:
        tid: str = thread.get("id", "")
        if not tid:
            continue
        comments: list[dict[str, Any]] = thread.get("comments", [])
        if not comments:
            continue
        first_author: str = _extract_login(comments[0])
        if first_author != our_username and tid not in pr.our_threads:
            pr.our_threads.append(tid)
            if tid not in pr.thread_status:
                pr.thread_status[tid] = ThreadStatus(
                    file=thread.get("path", ""),
                    line=thread.get("line", 0),
                )


def _collect_deferred_threads_for_followup(
    pr: MonitoredPR, pr_number: int
) -> list[dict[str, Any]]:
    """Return deferred-thread metadata suitable for follow-up ticket creation.

    Called from ``cmd_check`` when a monitored PR transitions to MERGED.
    Looks up the deferred thread IDs tracked locally, fetches their bodies
    from GitHub once (single ``gh review view`` call), and returns one dict
    per thread for the skill to turn into a Linear ticket.

    Returns an empty list when no threads are deferred — the common case.
    """
    deferred_ids = {tid for tid, ts in pr.thread_status.items() if ts.deferred}
    if not deferred_ids:
        return []
    review_raw = _run_gh(["review", "view", str(pr_number), "--json"], repo=pr.repo)
    try:
        review_data: dict[str, Any] = json.loads(review_raw)
    except (json.JSONDecodeError, ValueError):
        return []
    our_username = _get_our_username()
    out: list[dict[str, Any]] = []
    for thread in review_data.get("threads") or []:
        tid = thread.get("id", "")
        if tid not in deferred_ids:
            continue
        comments: list[dict[str, Any]] = thread.get("comments", [])
        if not comments:
            continue
        reviewer_comment = comments[0].get("body", "")
        reviewer_author = _extract_login(comments[0])
        deferral_reply = ""
        for c in comments[1:]:
            body = c.get("body", "")
            if _extract_login(c) == our_username and is_deferral(body):
                deferral_reply = body
                break
        out.append(
            {
                "thread_id": tid,
                "file": thread.get("path", ""),
                "line": thread.get("line", 0),
                "reviewer": reviewer_author,
                "reviewer_comment": reviewer_comment,
                "deferral_reply": deferral_reply,
                "url": comments[0].get("url", ""),
            }
        )
    return out


def _update_thread_status(
    ts: ThreadStatus,
    thread: dict[str, Any],
    role: str,
    our_username: str,
) -> None:
    """Update a single ThreadStatus from the current GitHub thread data.

    Mutates *ts* in place.
    """
    comments: list[dict[str, Any]] = thread.get("comments", [])
    ts.resolved = bool(thread.get("isResolved", False))
    ts.replied = False
    ts.deferred = False

    if len(comments) <= 1:
        return

    subsequent = comments[1:]
    if role == "reviewer":
        ts.replied = any(_extract_login(c) != our_username for c in subsequent)
    else:
        reply_by_us = [c for c in subsequent if _extract_login(c) == our_username]
        ts.replied = bool(reply_by_us)
        if ts.replied:
            ts.deferred = any(is_deferral(c.get("body", "")) for c in reply_by_us)


def _apply_code_changes(
    pr: MonitoredPR,
    base_sha: str,
    new_sha: str,
) -> tuple[bool, str | None, list[str]]:
    """Run git diff and identify threads whose lines the new commits touched.

    Does NOT mark threads addressed. A line being touched is a *candidate*
    signal — the commit may have changed that line for an unrelated reason.
    The delta-review confirmation pass (skill side) decides whether the change
    actually addresses the thread's comment and, if so, calls the
    ``confirm-thread`` subcommand to set ``code_changed``.

    Returns ``(has_delta_diff, delta_diff_text, touched_thread_ids)``.
    """
    diff_output = _run_git(["diff", f"{base_sha}..{new_sha}"], cwd=pr.repo_path)
    changed_lines = parse_diff_changed_lines(diff_output)
    touched = [
        tid
        for tid, ts in pr.thread_status.items()
        if check_code_changed(ts.file, ts.line, changed_lines)
    ]
    if pr.role == "reviewer" and diff_output:
        return True, diff_output, touched
    return False, None, touched


def _apply_status_transitions(
    pr: MonitoredPR, changed: bool, is_draft: bool = False
) -> None:
    """Apply lifecycle status transitions to *pr* based on thread state and
    commit changes.

    Transitions:
    - ``watching`` → ``ready_to_approve`` when all threads are addressed.
    - ``ready_to_approve`` or ``approved`` → ``watching`` when new commits land.
    - Any → ``watching`` when the PR is currently a draft. Drafts are
      author-controlled WIP and must never appear in the channel-bump / DM
      escalation paths, even if they previously reached ``ready_to_approve``
      before being converted back to draft.

    Mutates *pr.status* in place.
    """
    if is_draft:
        if pr.status in ("ready_to_approve", "approved"):
            pr.status = "watching"
        return
    if pr.all_threads_addressed() and pr.status == "watching":
        pr.status = "ready_to_approve"
    if changed and pr.status in ("ready_to_approve", "approved"):
        pr.status = "watching"


def _refresh_comment_reviews(
    pr: MonitoredPR, repo: str, pr_number: int, sha_changed: bool, our_username: str
) -> bool:
    """Refresh ``pr.comment_reviews`` from GitHub.

    A tracked entry is a non-bot ``COMMENTED`` review submitted *after* the
    most recent state-resetting event:
      - The author pushed a new commit (``sha_changed == True``), or
      - A formal ``CHANGES_REQUESTED`` / ``APPROVED`` review landed (its
        ``submitted_at`` becomes the cutoff and stale comment-reviews drop).

    Persisted classification verdicts survive across cycles. New reviews are
    inserted with ``classification == "unclassified"`` for the skill to handle.

    Returns
    -------
        True when at least one human (non-bot, not ``our_username``) has left a
        ``COMMENTED`` or ``CHANGES_REQUESTED`` review on this PR. Why: a human
        review — formal or a bare comment — means a reviewer has already
        engaged and given feedback; once we address it they must look again
        (and for ``CHANGES_REQUESTED``, personally APPROVE, or branch
        protection blocks the merge). The caller uses this to label the PR
        "awaiting re-review" vs "awaiting first review". False on fetch error.
    """
    raw = _run_gh(["api", f"repos/{repo}/pulls/{pr_number}/reviews"])
    try:
        reviews: list[dict[str, Any]] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return False

    if sha_changed:
        # Author pushed something — any prior comment-review concerns may have
        # been addressed by the push. Drop and re-evaluate from scratch.
        pr.comment_reviews.clear()

    formal_cutoff = ""
    for r in reviews:
        if r.get("state") in ("CHANGES_REQUESTED", "APPROVED"):
            ts = r.get("submitted_at", "") or ""
            formal_cutoff = max(formal_cutoff, ts)

    if formal_cutoff:
        pr.comment_reviews = {
            rid: ref
            for rid, ref in pr.comment_reviews.items()
            if ref.submitted_at > formal_cutoff
        }

    _collect_new_comment_reviews(pr, reviews, formal_cutoff)
    return _has_engaged_human_reviewer(reviews, our_username)


def _collect_new_comment_reviews(
    pr: MonitoredPR, reviews: list[dict[str, Any]], formal_cutoff: str
) -> None:
    """Insert untracked non-bot ``COMMENTED`` reviews into ``pr.comment_reviews``.

    Skips bot authors, bodies emptied to carry only inline file comments, and
    reviews at/before ``formal_cutoff``. Already-tracked reviews are left as-is
    so their persisted classification survives.
    """
    for r in reviews:
        if r.get("state") != "COMMENTED":
            continue
        author = (r.get("user") or {}).get("login", "")
        if not author or is_bot_login(author):
            continue
        submitted_at = r.get("submitted_at", "") or ""
        if formal_cutoff and submitted_at <= formal_cutoff:
            continue
        body = (r.get("body") or "").strip()
        if not body:
            # Bare COMMENTED with no body is just the carrier for inline
            # file-level comments — those are tracked through threads.
            continue
        rid = str(r.get("id"))
        if rid in pr.comment_reviews:
            continue  # already tracked; preserve classification
        pr.comment_reviews[rid] = CommentReviewRef(
            review_id=rid,
            author=author,
            submitted_at=submitted_at,
            body=body[:2000],
        )


def _has_engaged_human_reviewer(
    reviews: list[dict[str, Any]], our_username: str
) -> bool:
    """Return True when a non-bot reviewer other than us left a non-APPROVED review.

    A human reviewer has engaged if anyone non-bot (other than us) left a
    ``COMMENTED`` or ``CHANGES_REQUESTED`` review. ``APPROVED`` is excluded —
    that reviewer is already satisfied and needs no further look.
    """
    return any(
        r.get("state") in ("COMMENTED", "CHANGES_REQUESTED")
        and (login := ((r.get("user") or {}).get("login") or ""))
        and login != our_username
        and not is_bot_login(login)
        for r in reviews
    )


_FAILED_CHECKRUN_CONCLUSIONS: frozenset[str] = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
)
_PENDING_CHECKRUN_STATUSES: frozenset[str] = frozenset(
    {"IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED"}
)
_BLOCKING_MERGE_STATES: frozenset[str] = frozenset({"DIRTY", "BEHIND", "BLOCKED"})


def _summarize_status_checks(rollup: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a ``statusCheckRollup`` list into a ``failing`` / ``pending`` summary.

    Rollup entries come in two shapes:
    - ``CheckRun`` (GitHub Actions): ``status`` + ``conclusion``.
    - ``StatusContext`` (legacy commit status / third-party): ``state``.

    Returns a dict with ``failing`` (list of {workflow,name,conclusion,url}),
    ``pending_count`` (int), and ``ok`` (bool).
    """
    failing: list[dict[str, str]] = []
    pending_count = 0
    for c in rollup:
        typename = c.get("__typename", "")
        if typename == "CheckRun":
            status = (c.get("status") or "").upper()
            conclusion = (c.get("conclusion") or "").upper()
            if status == "COMPLETED" and conclusion in _FAILED_CHECKRUN_CONCLUSIONS:
                failing.append(
                    {
                        "workflow": c.get("workflowName") or "",
                        "name": c.get("name") or "",
                        "conclusion": conclusion,
                        "url": c.get("detailsUrl") or "",
                    }
                )
            elif status in _PENDING_CHECKRUN_STATUSES:
                pending_count += 1
        else:
            state_str = (c.get("state") or "").upper()
            if state_str in ("FAILURE", "ERROR"):
                failing.append(
                    {
                        "workflow": "",
                        "name": c.get("context") or "",
                        "conclusion": state_str,
                        "url": c.get("targetUrl") or "",
                    }
                )
            elif state_str == "PENDING":
                pending_count += 1
    return {"failing": failing, "pending_count": pending_count, "ok": not failing}


def _resolve_change_request_source(
    attention_state: str | None,
    unaddressed_count: int,
    review_decision: str,
    has_actionable_comment_review: bool,
) -> str | None:
    """Resolve which signal earned the ``changes_requested`` state.

    Lets the skill route an auto-fix correctly — inline-thread reply vs.
    comment-review reply. Returns None when the PR is not in that state.
    """
    if attention_state != "changes_requested":
        return None
    if unaddressed_count > 0:
        return "inline"
    if review_decision == "CHANGES_REQUESTED":
        return "formal"
    if has_actionable_comment_review:
        return "comment"
    return None


def _collect_pending_comment_reviews(
    pr: MonitoredPR,
    *,
    unaddressed_count: int,
    review_decision: str,
    merge_blocked: bool,
    merge_state_status: str,
    ci_ok: bool,
) -> list[dict[str, Any]]:
    """Surface unclassified comment reviews — only as a fallback attention signal.

    Returned ONLY when no other signal would already trigger attention. Keeps
    the skill from burning classifier tokens on PRs with already-actionable
    inline/formal CRs. Note: merge_state_status == "BLOCKED" is NOT a
    higher-priority signal — _compute_attention_state reclassifies it as
    ready_to_approve (waiting on required reviews). Only DIRTY/BEHIND beats the
    fallback path.
    """
    code_fixable_merge_block = merge_blocked and merge_state_status in (
        "DIRTY",
        "BEHIND",
    )
    no_other_signal = (
        not code_fixable_merge_block
        and ci_ok
        and unaddressed_count == 0
        and review_decision != "CHANGES_REQUESTED"
    )
    if pr.role != "author" or not no_other_signal:
        return []
    return [
        {
            "review_id": ref.review_id,
            "author": ref.author,
            "submitted_at": ref.submitted_at,
            "body": ref.body,
        }
        for ref in pr.comment_reviews.values()
        if ref.classification == "unclassified"
    ]


def _complete_terminal_pr(
    state: MonitorState,
    key: str,
    pr: MonitoredPR,
    pr_number: int,
    pr_state: str,
    repo: str,
) -> dict[str, Any]:
    """Complete a MERGED/CLOSED PR and return its terminal check result."""
    deferred_threads_out: list[dict[str, Any]] = []
    # Only surface deferred follow-ups for actually-merged PRs. A CLOSED
    # (un-merged) PR is abandoned work — its deferrals went with it.
    if pr_state == "MERGED":
        deferred_threads_out = _collect_deferred_threads_for_followup(pr, pr_number)
    state.complete_pr(key, pr_state)
    save_state(state, repo)
    return {
        "pr_number": pr_number,
        "pr_state": pr_state,
        "completed": True,
        "reason": pr_state,
        "deferred_threads": deferred_threads_out,
    }


def _refresh_threads(
    pr: MonitoredPR, pr_number: int, our_username: str
) -> dict[str, dict[str, Any]]:
    """Fetch review threads from GitHub and update each tracked thread's status.

    For author-role PRs, also discovers new threads opened by others. Returns
    a ``{thread_id: status_dict}`` map of the tracked threads.
    """
    review_raw = _run_gh(["review", "view", str(pr_number), "--json"], repo=pr.repo)
    try:
        review_data: dict[str, Any] = json.loads(review_raw)
    except (json.JSONDecodeError, ValueError):
        review_data = {}
    threads: list[dict[str, Any]] = review_data.get("threads") or []

    if pr.role == "author":
        _discover_author_threads(pr, threads, our_username)

    thread_updates: dict[str, dict[str, Any]] = {}
    threads_by_id: dict[str, dict[str, Any]] = {t.get("id", ""): t for t in threads}
    for tid in pr.our_threads:
        thread = threads_by_id.get(tid)
        if thread is None:
            continue
        ts = pr.thread_status.setdefault(
            tid,
            ThreadStatus(file=thread.get("path", ""), line=thread.get("line", 0)),
        )
        _update_thread_status(ts, thread, pr.role, our_username)
        thread_updates[tid] = ts.to_dict()
    return thread_updates


def _detect_touched_threads(
    pr: MonitoredPR, *, delta_base_sha: str, new_sha: str
) -> tuple[bool, str | None, list[str]]:
    """Detect threads touched by commits since the delta baseline.

    The diff is computed from ``delta_base_sha`` (not ``last_seen_sha``): an
    unconsumed reviewer delta keeps surfacing every cycle until the skill acks
    it, so the delta is never lost if Step 3 misses a cycle.

    A touched thread is a candidate for resolution — the skill's delta
    confirmation pass verifies and calls ``confirm-thread`` to mark it. A stale
    repo_path (e.g. a cleaned-up worktree) is skipped, not fatal — the cycle
    still completes; delta detection resumes once the path is valid again.
    """
    if delta_base_sha != new_sha and pr.repo_path and Path(pr.repo_path).is_dir():
        return _apply_code_changes(pr, delta_base_sha, new_sha)
    return False, None, []


def _derive_check_signals(
    pr: MonitoredPR,
    pr_view: dict[str, Any],
    *,
    is_draft: bool,
    has_prior_human_review: bool,
) -> dict[str, Any]:
    """Compute the attention/escalation signals for a live (non-terminal) PR.

    Mutates ``pr`` (awaiting_rereview, state-entered timestamp, auto-fix
    counter) — the caller persists afterwards. Returns a dict keyed by the
    ``cmd_check`` result keys this step contributes.
    """
    rollup: list[dict[str, Any]] = pr_view.get("statusCheckRollup") or []
    ci_summary = _summarize_status_checks(rollup)
    merge_state_status: str = pr_view.get("mergeStateStatus") or "UNKNOWN"
    merge_blocked = merge_state_status in _BLOCKING_MERGE_STATES
    review_decision: str = pr_view.get("reviewDecision") or ""

    unaddressed_count = len(pr.unaddressed_threads())
    has_actionable_comment_review = any(
        ref.classification == "requests_changes" for ref in pr.comment_reviews.values()
    )
    reviewer_count = len(pr_view.get("reviewRequests") or [])
    attention_state = _compute_attention_state(
        role=pr.role,
        status=pr.status,
        ci_ok=ci_summary["ok"],
        merge_blocked=merge_blocked,
        merge_state_status=merge_state_status,
        unaddressed_count=unaddressed_count,
        review_decision=review_decision,
        has_actionable_comment_review=has_actionable_comment_review,
        is_draft=is_draft,
        reviewer_count=reviewer_count,
    )

    # "Awaiting re-review" only applies once the PR is otherwise mergeable and
    # just waiting on review — not while CI / threads still need author work.
    pr.awaiting_rereview = (
        has_prior_human_review and attention_state == "ready_to_approve"
    )
    _ensure_state_entered_at(pr, attention_state)
    _reset_auto_fix_counter_if_stale(pr)

    base_ref_name: str = pr_view.get("baseRefName") or ""
    auto_fix_ok, auto_fix_blocked_reason = _compute_auto_fix_ok(
        pr, is_draft, base_ref_name
    )
    return {
        "failing_checks": ci_summary["failing"],
        "pending_checks_count": ci_summary["pending_count"],
        "ci_ok": ci_summary["ok"],
        "merge_state_status": merge_state_status,
        "merge_blocked": merge_blocked,
        "attention_state": attention_state,
        "awaiting_rereview": pr.awaiting_rereview,
        "reviewer_count": reviewer_count,
        "needs_local_ping": attention_state is not None
        and pr.last_notified_state != attention_state,
        "needs_escalation": _compute_needs_escalation(pr, attention_state),
        "auto_fix_ok": auto_fix_ok,
        "auto_fix_blocked_reason": auto_fix_blocked_reason,
        "business_minutes_in_state": _business_minutes_in_state(pr)
        if attention_state
        else 0,
        "needs_channel_bump": _needs_channel_bump(pr, attention_state),
        "dm_escalation_reason": _dm_escalation_reason(pr, attention_state),
        "head_ref_name": pr_view.get("headRefName") or "",
        "base_ref_name": base_ref_name,
        "change_request_source": _resolve_change_request_source(
            attention_state,
            unaddressed_count,
            review_decision,
            has_actionable_comment_review,
        ),
        "pending_comment_reviews": _collect_pending_comment_reviews(
            pr,
            unaddressed_count=unaddressed_count,
            review_decision=review_decision,
            merge_blocked=merge_blocked,
            merge_state_status=merge_state_status,
            ci_ok=ci_summary["ok"],
        ),
    }


def cmd_check(pr_number: int, repo: str) -> dict[str, Any]:
    """Run one monitoring cycle for a single PR.

    Calls GitHub APIs and, when new commits are detected, ``git diff`` to
    update thread and code-change status.

    Returns a structured dict with the check results.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        return {"error": f"PR {key} not found in monitored"}

    pr = state.monitored[key]

    # 1. Fetch current PR state from GitHub
    pr_view_raw = _run_gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--json",
            "headRefOid,headRefName,baseRefName,isDraft,state,mergedAt,closedAt,statusCheckRollup,mergeStateStatus,reviewDecision,reviewRequests",
        ],
        repo=pr.repo,
    )
    try:
        pr_view: dict[str, Any] = json.loads(pr_view_raw)
    except (json.JSONDecodeError, ValueError):
        pr_view = {}

    pr_state: str = pr_view.get("state", "UNKNOWN")
    new_sha: str = pr_view.get("headRefOid", pr.last_seen_sha)

    # 2. Handle terminal states
    if pr_state in ("MERGED", "CLOSED"):
        return _complete_terminal_pr(state, key, pr, pr_number, pr_state, repo)

    # 3. Detect new commits
    old_sha = pr.last_seen_sha
    changed = new_sha != old_sha

    # 4-6. Fetch thread / review data and update tracked threads
    our_username = _get_our_username()
    thread_updates = _refresh_threads(pr, pr_number, our_username)

    # 7. Touched-thread detection (delta since the delta baseline + local repo).
    has_delta_diff, delta_diff, touched_threads = _detect_touched_threads(
        pr, delta_base_sha=pr.delta_base_sha, new_sha=new_sha
    )

    is_draft: bool = bool(pr_view.get("isDraft"))

    # 8. Status transitions
    _apply_status_transitions(pr, changed, is_draft=is_draft)

    # 8b. Refresh tracked comment reviews (fallback change-request signal).
    has_prior_human_review = False
    if pr.role == "author":
        has_prior_human_review = _refresh_comment_reviews(
            pr, repo, pr_number, sha_changed=changed, our_username=our_username
        )

    # 9. Persist updated state.
    #
    # ``last_seen_sha`` tracks observed HEAD — advance every cycle.
    #
    # ``delta_base_sha`` is the delta-review baseline. A reviewer delta is a
    # *lossy* read: Step 3's delta review is a best-effort LLM step, so the
    # baseline only advances once the skill positively acks it (``ack-delta``).
    # Advancing it here would drop an unprocessed delta permanently. With no
    # delta to consume (author PRs, or an empty reviewer diff) it advances now.
    pr.last_seen_sha = new_sha
    if not (pr.role == "reviewer" and has_delta_diff):
        pr.delta_base_sha = new_sha
    pr.last_checked_at = datetime.now(UTC).isoformat()
    save_state(state, repo)

    # 10. Build result dict
    signals = _derive_check_signals(
        pr, pr_view, is_draft=is_draft, has_prior_human_review=has_prior_human_review
    )
    save_state(state, repo)

    result: dict[str, Any] = {
        "pr_number": pr_number,
        "pr_state": pr_state,
        "changed": changed,
        "old_sha": old_sha,
        "new_sha": new_sha,
        "role": pr.role,
        "status": pr.status,
        "thread_updates": thread_updates,
        "all_addressed": pr.all_threads_addressed(),
        "unaddressed": pr.unaddressed_threads(),
        "touched_threads": touched_threads,
        "has_delta_diff": has_delta_diff,
        "slack_channel": pr.slack_channel,
        "slack_ts": pr.slack_ts,
        "slack_last_seen_ts": pr.slack_last_seen_ts,
        "auto_fix_attempts_today": _auto_fix_attempts_today(pr),
        "is_draft": is_draft,
        **signals,
    }
    if pr.role == "reviewer" and has_delta_diff:
        result["delta_diff"] = delta_diff
    return result


# Attention states that warrant immediate Slack escalation (no grace period).
IMMEDIATE_ESCALATION_STATES: frozenset[str] = frozenset({"ci_failing", "merge_blocked"})


def _compute_attention_state(
    role: str,
    status: str,
    ci_ok: bool,
    merge_blocked: bool,
    merge_state_status: str = "UNKNOWN",
    unaddressed_count: int = 0,
    review_decision: str = "",
    has_actionable_comment_review: bool = False,
    is_draft: bool = False,
    reviewer_count: int = -1,
) -> str | None:
    """Return the highest-priority author-attention state, or None.

    Precedence (most urgent first):
      merge_blocked (DIRTY/BEHIND) → ci_failing → changes_requested (inline/formal)
      → changes_requested (comment-review fallback) → no_reviewer → ready_to_approve
      → None

    ``no_reviewer`` fires when a non-draft author PR still needs review
    (``reviewDecision == "REVIEW_REQUIRED"``) but has zero reviewers requested —
    an orphaned PR nobody was ever asked to look at. The skill clears it by
    requesting the default team. ``reviewer_count`` defaults to -1 ("not
    supplied") so the state only triggers when a caller explicitly passes 0.

    ``changes_requested`` fires on three signals, in order:
      1. An unresolved inline review thread.
      2. Top-level ``reviewDecision == "CHANGES_REQUESTED"`` (the reviewer hit
         "Request changes" without leaving inline comments).
      3. **Fallback:** ``has_actionable_comment_review`` — a non-bot
         ``COMMENTED`` review whose body the skill's classifier flagged as
         actually requesting changes. Last code-actionable signal before the
         PR is otherwise just "waiting for a formal review".

    BLOCKED merge state with green CI and no change-request is reclassified as
    ready_to_approve — it means "waiting for required reviews / branch protection,"
    not a code problem the author can fix. This routes it to the channel-bump path
    instead of auto-fix dispatch.

    Drafts (``is_draft=True``) always return None — they are author-controlled
    WIP and must not enter any escalation path (channel bump, DM, auto-fix).
    The draft-promotion logic in Step 4e handles their lifecycle separately.
    """
    if role != "author" or is_draft:
        return None
    # Precedence chain, most urgent first. BLOCKED + ci_ok + no change-request
    # reclassifies as ready_to_approve — waiting for review, not a code problem.
    precedence: list[tuple[bool, str]] = [
        (merge_blocked and merge_state_status in ("DIRTY", "BEHIND"), "merge_blocked"),
        (not ci_ok, "ci_failing"),
        (
            unaddressed_count > 0 or review_decision == "CHANGES_REQUESTED",
            "changes_requested",
        ),
        (has_actionable_comment_review, "changes_requested"),
        (review_decision == "REVIEW_REQUIRED" and reviewer_count == 0, "no_reviewer"),
        (merge_state_status == "BLOCKED", "ready_to_approve"),
        (status == "ready_to_approve", "ready_to_approve"),
    ]
    for matched, attention_state in precedence:
        if matched:
            return attention_state
    return None


def _compute_needs_escalation(pr: MonitoredPR, attention_state: str | None) -> bool:
    """Return True when /review-monitor should fire a Slack-bot escalation.

    Rules:
      - No attention state → never.
      - Immediate-escalation states (ci_failing, merge_blocked) → fire once per
        state transition.
      - ready_to_approve → fire once the 15-min grace elapses after local ping,
        and only if we haven't already escalated for this state.
    """
    if attention_state is None:
        return False
    # no_reviewer self-heals — the skill requests the default team the same
    # cycle it surfaces, so the state clears before any DM is warranted.
    if attention_state == "no_reviewer":
        return False
    already_escalated_this_state = (
        pr.last_escalated_at is not None
        and pr.last_notified_at is not None
        and pr.last_escalated_at >= pr.last_notified_at
        and pr.last_notified_state == attention_state
    )
    if already_escalated_this_state:
        return False
    if attention_state in IMMEDIATE_ESCALATION_STATES:
        return True
    # Grace-based: require a prior local ping for THIS state, and enough elapsed time.
    if pr.last_notified_state != attention_state or pr.last_notified_at is None:
        return False
    last_notified = datetime.fromisoformat(pr.last_notified_at)
    return datetime.now(UTC) - last_notified >= ESCALATION_GRACE


def cmd_status(repo: str, as_json: bool = False) -> None:
    """Print the current monitor state.

    If *as_json* is True, print the full state as JSON.
    Otherwise print a human-readable table.
    """
    state = load_state(repo)
    if as_json:
        print(json.dumps(state.to_dict(), indent=2))
        return

    if not state.monitored:
        print("No PRs currently monitored.")
    else:
        print(f"{'PR':<20} {'ROLE':<10} {'STATUS':<12} {'THREADS':<10} {'REVIEW'}")
        print("-" * 70)
        for key, pr in sorted(state.monitored.items()):
            total = len(pr.thread_status)
            addressed = sum(1 for ts in pr.thread_status.values() if ts.is_addressed)
            threads_col = f"{addressed}/{total}" if total else "n/a"
            review_col = "re-review" if pr.awaiting_rereview else ""
            print(
                f"{key:<20} {pr.role:<10} {pr.status:<12}"
                f" {threads_col:<10} {review_col}"
            )

    print(f"\n{len(state.completed)} completed PR(s) in history.")


def cmd_list_repos() -> list[str]:
    """Return a list of repo names that have state files in the central directory."""
    if not CENTRAL_STATE_DIR.exists():
        return []
    repos = []
    for f in sorted(CENTRAL_STATE_DIR.glob("*.json")):
        repo_name = f.stem.replace("--", "/", 1)
        repos.append(repo_name)
    return repos


def cmd_status_all() -> MonitorState:
    """Load and merge state from all repo files in the central directory."""
    repos = cmd_list_repos()
    combined = MonitorState(monitored={}, completed={})
    for repo in repos:
        state = load_state(repo)
        combined.monitored.update(state.monitored)
        combined.completed.update(state.completed)
    return combined


# ---------------------------------------------------------------------------
# Auto-discover, auto-fix tracking, channel-bump
# ---------------------------------------------------------------------------


def _today_utc_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def is_bot_login(login: str) -> bool:
    """Return True when *login* looks like a bot whose findings can be silently skipped.

    Bots in ``MERGE_BLOCKING_BOT_LOGINS`` (sonarqube/sonarcloud) override and return
    False — their findings block merge and must be addressed like human review threads.
    """
    if not login:
        return False
    lower = login.lower()
    if lower in MERGE_BLOCKING_BOT_LOGINS:
        return False
    if lower in KNOWN_BOT_LOGINS:
        return True
    return any(lower.endswith(suffix) for suffix in BOT_LOGIN_SUFFIXES)


def _business_minutes_between(start: datetime, end: datetime) -> int:
    """Return whole minutes inside Mon-Fri
    ``BUSINESS_START_HOUR``..``BUSINESS_END_HOUR`` ET."""
    if end <= start:
        return 0
    start_local = start.astimezone(BUSINESS_TZ)
    end_local = end.astimezone(BUSINESS_TZ)
    total = 0
    cursor = start_local
    while cursor < end_local:
        day_start = cursor.replace(
            hour=BUSINESS_START_HOUR, minute=0, second=0, microsecond=0
        )
        day_end = cursor.replace(
            hour=BUSINESS_END_HOUR, minute=0, second=0, microsecond=0
        )
        if cursor.weekday() < FIRST_WEEKEND_WEEKDAY:
            window_start = max(cursor, day_start)
            window_end = min(end_local, day_end)
            if window_end > window_start:
                total += int((window_end - window_start).total_seconds() // 60)
        next_day = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cursor = next_day
    return total


def _ensure_state_entered_at(pr: MonitoredPR, attention_state: str | None) -> None:
    """Stamp ``state_entered_at`` whenever the attention_state changes."""
    if attention_state is None:
        pr.state_entered_at = None
        return
    if pr.last_notified_state != attention_state or pr.state_entered_at is None:
        # Either freshly entered, or first time we're tracking it under the new schema.
        pr.state_entered_at = datetime.now(UTC).isoformat()


def _reset_auto_fix_counter_if_stale(pr: MonitoredPR) -> None:
    today = _today_utc_str()
    if pr.auto_fix_attempt_date != today:
        pr.auto_fix_attempt_date = today
        pr.auto_fix_attempts_today = 0


def _auto_fix_attempts_today(pr: MonitoredPR) -> int:
    today = _today_utc_str()
    if pr.auto_fix_attempt_date != today:
        return 0
    return pr.auto_fix_attempts_today


def _compute_auto_fix_ok(
    pr: MonitoredPR, is_draft: bool, base_ref_name: str
) -> tuple[bool, str | None]:
    """Decide whether auto-fix is allowed for the given PR right now.

    Returns ``(ok, blocked_reason)``. ``blocked_reason`` is a short string
    suitable for skill output when ``ok`` is False; ``None`` when allowed.

    Rules, in order:
    1. Daily cap (``AUTO_FIX_DAILY_CAP``) — most-frequent block, cheapest check.
    2. Already addressed this state — ``last_auto_fix_at`` is after
       ``state_entered_at``, so we already dispatched an agent for this exact
       attention_state instance. Wait for the reviewer to respond; the state
       transition will reset ``state_entered_at`` and re-enable auto-fix.
    3. Stacked draft (isDraft=True AND base != main/master) — base-PR churn
       would force a wrong rebase target. Caller must skip.
    4. Plain draft (isDraft=True, base == main) — drafts are WIP by definition;
       skip unless the user re-registers explicitly.
    """
    if _auto_fix_attempts_today(pr) >= AUTO_FIX_DAILY_CAP:
        return False, "daily cap reached"
    if _auto_fix_already_addressed_state(pr):
        return False, "already addressed this state — waiting for reviewer"
    if is_draft and base_ref_name not in ("main", "master"):
        return False, f"draft stacked on {base_ref_name!r}"
    if is_draft:
        return False, "draft"
    return True, None


def _auto_fix_already_addressed_state(pr: MonitoredPR) -> bool:
    """Return True if we have already dispatched auto-fix for the current state
    instance.

    The attention_state has a ``state_entered_at`` timestamp that bumps on every
    transition. If we recorded an auto-fix after that bump, the current state
    instance has already been responded to — re-dispatching would just repeat
    work while we wait for the reviewer.
    """
    if not pr.last_auto_fix_at or not pr.state_entered_at:
        return False
    try:
        fixed = datetime.fromisoformat(pr.last_auto_fix_at)
        entered = datetime.fromisoformat(pr.state_entered_at)
    except ValueError:
        return False
    return fixed > entered


def _business_minutes_in_state(pr: MonitoredPR) -> int:
    if pr.state_entered_at is None:
        return 0
    try:
        entered = datetime.fromisoformat(pr.state_entered_at)
    except ValueError:
        return 0
    return _business_minutes_between(entered, datetime.now(UTC))


def _needs_channel_bump(pr: MonitoredPR, attention_state: str | None) -> bool:
    if attention_state != "ready_to_approve":
        return False
    if _business_minutes_in_state(pr) < STALE_REVIEW_THRESHOLD_MIN:
        return False
    if pr.last_channel_bump_at is None:
        return True
    try:
        last = datetime.fromisoformat(pr.last_channel_bump_at)
    except ValueError:
        return True
    return datetime.now(UTC) - last >= CHANNEL_BUMP_COOLDOWN


def _dm_escalation_reason(pr: MonitoredPR, attention_state: str | None) -> str | None:
    """Return a reason string when /review-monitor should fire a DM, else None.

    Reasons (priority order):
      - "loop"         — auto-fix cap hit today on a still-failing state
      - "week_old"     — author PR open ≥ 7 days

    Suppressed when ``last_escalated_at`` is within ``DM_ESCALATION_COOLDOWN``
    of now — prevents one DM per cycle while a long-lived condition persists.
    """
    if pr.role != "author":
        return None
    if pr.last_escalated_at:
        try:
            last = datetime.fromisoformat(pr.last_escalated_at)
            if datetime.now(UTC) - last < DM_ESCALATION_COOLDOWN:
                return None
        except ValueError:
            pass
    if (
        attention_state in ("ci_failing", "merge_blocked", "changes_requested")
        and _auto_fix_attempts_today(pr) >= AUTO_FIX_DAILY_CAP
    ):
        return "loop"
    if pr.registered_at:
        try:
            registered = datetime.fromisoformat(pr.registered_at)
            if datetime.now(UTC) - registered >= timedelta(days=7):
                return "week_old"
        except ValueError:
            pass
    return None


def cmd_discover(repo: str, days: int, repo_path: str) -> dict[str, Any]:
    """Find open PRs authored by the current user in *repo* and register them
    as author-role.

    Already-monitored PRs are skipped (idempotent).
    Returns ``{registered, skipped, errors}``.
    """
    state = load_state(repo)
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _run_gh(
        [
            "search",
            "prs",
            "--repo",
            repo,
            "--author",
            "@me",
            "--state",
            "open",
            "--created",
            f">={since}",
            "--json",
            "number,title",
            "--limit",
            "100",
        ],
    )
    try:
        prs: list[dict[str, Any]] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        prs = []

    registered: list[int] = []
    skipped: list[int] = []
    for pr_info in prs:
        pr_number = pr_info.get("number")
        if not isinstance(pr_number, int):
            continue
        key = f"{repo}#{pr_number}"
        if key in state.monitored:
            skipped.append(pr_number)
            continue
        sha_raw = _run_gh(
            [
                "pr",
                "view",
                str(pr_number),
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ],
            repo=repo,
        )
        sha = sha_raw.strip()
        cmd_register(
            pr_number=pr_number,
            role="author",
            repo=repo,
            repo_path=repo_path,
            sha=sha,
            review_id=None,
            threads=[],
            thread_details=None,
            slack_channel=None,
            slack_ts=None,
        )
        registered.append(pr_number)
    return {"registered": registered, "skipped": skipped, "repo": repo}


def _search_reviewed_prs(repo: str, days: int, our_username: str) -> list[int]:
    """Return numbers of open PRs the current user reviewed in the past *days*.

    PRs authored by the current user are excluded — you cannot be a reviewer on
    your own PR, and ``gh search --reviewed-by`` can still surface them (e.g. a
    review left before authorship changed). Those belong to ``discover``, not
    here.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _run_gh(
        [
            "search",
            "prs",
            "--repo",
            repo,
            "--reviewed-by",
            "@me",
            "--state",
            "open",
            "--updated",
            f">={since}",
            "--json",
            "number,author",
            "--limit",
            "100",
        ],
    )
    try:
        prs: list[dict[str, Any]] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    return [
        p["number"]
        for p in prs
        if isinstance(p.get("number"), int)
        and (p.get("author") or {}).get("login") != our_username
    ]


def _our_reviews(pr_number: int, repo: str, our_username: str) -> list[dict[str, Any]]:
    """Return the current user's reviews on a PR, oldest-first."""
    raw = _run_gh(
        [
            "api",
            f"repos/{repo}/pulls/{pr_number}/reviews",
            "--jq",
            f'[.[] | select(.user.login=="{our_username}")]',
        ],
    )
    try:
        result: list[dict[str, Any]] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    return result


def _our_unresolved_threads(
    pr_number: int, repo: str, our_username: str
) -> list[dict[str, Any]]:
    """Return unresolved review threads on a PR whose first comment is the
    current user's.

    Resolved threads, and threads opened by other reviewers, are excluded — only
    our own still-open review points are returned.
    """
    raw = _run_gh(["review", "view", str(pr_number), "--json"], repo=repo)
    try:
        data: dict[str, Any] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for thread in data.get("threads") or []:
        tid = thread.get("id", "")
        comments: list[dict[str, Any]] = thread.get("comments", [])
        if not tid or not comments:
            continue
        if thread.get("isResolved", False):
            continue
        if _extract_login(comments[0]) != our_username:
            continue
        out.append(thread)
    return out


def _recover_one_review(
    pr_number: int, repo: str, repo_path: str, our_username: str
) -> str:
    """Register one reviewed-but-unmonitored PR if it still needs watching.

    Returns the verdict ``"recovered"``, ``"already_approved"``, or
    ``"no_open_threads"``.
    """
    our_reviews = _our_reviews(pr_number, repo, our_username)
    if our_reviews and our_reviews[-1].get("state") == "APPROVED":
        # We already signed off — a lingering unresolved thread is the author's
        # to close, not a reason to re-open monitoring.
        return "already_approved"

    open_threads = _our_unresolved_threads(pr_number, repo, our_username)
    if not open_threads:
        return "no_open_threads"

    # Register at the SHA of our most recent review so the author's later
    # commits surface as a delta. Fall back to current HEAD if the review
    # commit can't be resolved (delta detection is then a no-op until the
    # next push, which is still correct).
    sha = (our_reviews[-1].get("commit_id") or "").strip() if our_reviews else ""
    if not sha:
        sha = _run_gh(
            [
                "pr",
                "view",
                str(pr_number),
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ],
            repo=repo,
        ).strip()

    cmd_register(
        pr_number=pr_number,
        role="reviewer",
        repo=repo,
        repo_path=repo_path,
        sha=sha,
        review_id=None,
        threads=[t["id"] for t in open_threads],
        thread_details=[
            {"id": t["id"], "file": t.get("path", ""), "line": t.get("line")}
            for t in open_threads
        ],
        slack_channel=None,
        slack_ts=None,
    )
    return "recovered"


def cmd_recover_reviews(repo: str, days: int, repo_path: str) -> dict[str, Any]:
    """Recover open PRs the current user reviewed but never registered for monitoring.

    Detects the "review left, register skipped" failure mode: a delta review,
    ship-it review, or manual review posted threads on a PR, but the PR never
    entered the monitor — the register call crashed, was skipped, or the review
    predates monitoring. Without recovery those PRs go un-tracked: no delta
    review, no nudge, no approval-on-resolution.

    Each recovered PR is registered as reviewer-role at the SHA of our most
    recent review, so commits the author pushes afterwards surface as a delta.

    Idempotent. A PR is skipped — not recovered — when it is already monitored,
    already in the completed history, our latest review on it is an approval, or
    it has no unresolved threads of ours. In every one of those cases there is
    nothing left for the monitor to do.

    Returns ``{recovered, skipped_monitored, skipped_completed,
    skipped_already_approved, skipped_no_open_threads, repo}``.
    """
    state = load_state(repo)
    our_username = _get_our_username()
    buckets: dict[str, list[int]] = {
        "recovered": [],
        "skipped_monitored": [],
        "skipped_completed": [],
        "skipped_already_approved": [],
        "skipped_no_open_threads": [],
    }
    for pr_number in _search_reviewed_prs(repo, days, our_username):
        key = f"{repo}#{pr_number}"
        if key in state.monitored:
            buckets["skipped_monitored"].append(pr_number)
        elif key in state.completed:
            # Already approved/merged through the monitor — finished, not lost.
            buckets["skipped_completed"].append(pr_number)
        else:
            verdict = _recover_one_review(pr_number, repo, repo_path, our_username)
            bucket = "recovered" if verdict == "recovered" else f"skipped_{verdict}"
            buckets[bucket].append(pr_number)
    return {**buckets, "repo": repo}


def cmd_record_auto_fix(pr_number: int, repo: str) -> dict[str, Any]:
    """Increment the per-day auto-fix attempt counter for a PR.

    Returns ``{attempts_today, remaining, capped}``.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        return {"error": f"PR {key} not found in monitored"}
    pr = state.monitored[key]
    _reset_auto_fix_counter_if_stale(pr)
    pr.auto_fix_attempts_today += 1
    pr.last_auto_fix_at = datetime.now(UTC).isoformat()
    save_state(state, repo)
    remaining = max(0, AUTO_FIX_DAILY_CAP - pr.auto_fix_attempts_today)
    return {
        "attempts_today": pr.auto_fix_attempts_today,
        "remaining": remaining,
        "capped": pr.auto_fix_attempts_today >= AUTO_FIX_DAILY_CAP,
    }


def cmd_record_channel_bump(pr_number: int, repo: str) -> dict[str, Any]:
    """Record that a stale-review channel bump was actually sent for this PR.

    Called by the Desktop schedule at *drain* time. Advances the 24h bump
    cooldown and increments ``channel_bump_count``.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        return {"error": f"PR {key} not found in monitored"}
    pr = state.monitored[key]
    pr.last_channel_bump_at = datetime.now(UTC).isoformat()
    pr.channel_bump_count += 1
    save_state(state, repo)
    return {
        "last_channel_bump_at": pr.last_channel_bump_at,
        "channel_bump_count": pr.channel_bump_count,
    }


def cmd_pending_channel_bumps() -> list[dict[str, Any]]:
    """Across all repos, return author PRs whose state warrants a channel bump now.

    Each entry: ``{repo, pr_number, business_minutes_in_state, last_channel_bump_at}``.
    The caller must have run ``check`` against each monitored PR first this cycle —
    this command reads the persisted ``state_entered_at`` and ``status`` fields only.
    """
    pending: list[dict[str, Any]] = []
    for repo in cmd_list_repos():
        state = load_state(repo)
        for _, pr in sorted(state.monitored.items()):
            if pr.role != "author":
                continue
            if pr.status != "ready_to_approve":
                continue
            if not _needs_channel_bump(pr, "ready_to_approve"):
                continue
            pending.append(
                {
                    "repo": pr.repo,
                    "pr_number": pr.pr_number,
                    "business_minutes_in_state": _business_minutes_in_state(pr),
                    "last_channel_bump_at": pr.last_channel_bump_at,
                }
            )
    return pending


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _add_lifecycle_subparsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register subcommands that create or change a PR's monitored lifecycle."""
    p_reg = subparsers.add_parser("register", help="Start monitoring a PR")
    p_reg.add_argument("pr_number", type=int)
    p_reg.add_argument("--role", required=True, choices=["reviewer", "author"])
    p_reg.add_argument("--repo", required=True)
    p_reg.add_argument("--repo-path", required=True)
    p_reg.add_argument("--sha", required=True)
    p_reg.add_argument("--review-id")
    p_reg.add_argument("--threads", nargs="*", default=[])
    p_reg.add_argument("--thread-details", help="JSON list of {id,file,line} objects")
    p_reg.add_argument(
        "--slack-channel", help="Slack channel ID for PR announcement thread"
    )
    p_reg.add_argument("--slack-ts", help="Parent ts for PR announcement thread")

    p_drop = subparsers.add_parser("drop", help="Stop monitoring a PR")
    p_drop.add_argument("pr_number", type=int)
    p_drop.add_argument("--repo", required=True)

    p_complete = subparsers.add_parser("complete", help="Mark a PR as done")
    p_complete.add_argument("pr_number", type=int)
    p_complete.add_argument("--repo", required=True)
    p_complete.add_argument("--reason", default="merged")

    p_set_status = subparsers.add_parser(
        "set-status", help="Set lifecycle status for a PR"
    )
    p_set_status.add_argument("pr_number", type=int)
    p_set_status.add_argument("--repo", required=True)
    p_set_status.add_argument(
        "--status",
        required=True,
        choices=["watching", "ready_to_approve", "approved"],
    )

    p_confirm_thread = subparsers.add_parser(
        "confirm-thread",
        help="Mark a thread addressed-by-code-change (delta-review confirmation pass)",
    )
    p_confirm_thread.add_argument("pr_number", type=int)
    p_confirm_thread.add_argument("--repo", required=True)
    p_confirm_thread.add_argument("--thread", required=True, dest="thread_id")

    p_ack_delta = subparsers.add_parser(
        "ack-delta",
        help=(
            "Acknowledge the reviewer delta through --sha has been processed"
            " (Step 3 close)"
        ),
    )
    p_ack_delta.add_argument("pr_number", type=int)
    p_ack_delta.add_argument("--repo", required=True)
    p_ack_delta.add_argument("--sha", required=True)


def _add_signal_subparsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register subcommands that record nudges, notifications, and cursors."""
    p_nudge_ok = subparsers.add_parser("nudge-ok", help="Check if a nudge is allowed")
    p_nudge_ok.add_argument("pr_number", type=int)
    p_nudge_ok.add_argument("--repo", required=True)

    p_record_nudge = subparsers.add_parser(
        "record-nudge", help="Record a nudge was sent"
    )
    p_record_nudge.add_argument("pr_number", type=int)
    p_record_nudge.add_argument("--repo", required=True)

    p_mark_cr = subparsers.add_parser(
        "mark-comment-review",
        help="Record the classifier's verdict on a tracked COMMENTED review",
    )
    p_mark_cr.add_argument("pr_number", type=int)
    p_mark_cr.add_argument("--repo", required=True)
    p_mark_cr.add_argument("--review-id", required=True, dest="review_id")
    p_mark_cr.add_argument(
        "--classification",
        required=True,
        choices=["requests_changes", "neutral"],
    )

    p_mark_notified = subparsers.add_parser(
        "mark-notified", help="Record that a local ping fired for a state"
    )
    p_mark_notified.add_argument("pr_number", type=int)
    p_mark_notified.add_argument("--repo", required=True)
    p_mark_notified.add_argument("--state", required=True, dest="state_value")

    p_mark_escalated = subparsers.add_parser(
        "mark-escalated", help="Record that a Slack-bot escalation fired"
    )
    p_mark_escalated.add_argument("pr_number", type=int)
    p_mark_escalated.add_argument("--repo", required=True)

    p_slack_cursor = subparsers.add_parser(
        "slack-thread-cursor", help="Print Slack channel+ts+last_seen for a PR"
    )
    p_slack_cursor.add_argument("pr_number", type=int)
    p_slack_cursor.add_argument("--repo", required=True)

    p_update_cursor = subparsers.add_parser(
        "update-slack-cursor", help="Advance the slack_last_seen_ts cursor"
    )
    p_update_cursor.add_argument("pr_number", type=int)
    p_update_cursor.add_argument("--repo", required=True)
    p_update_cursor.add_argument("--last-seen-ts", required=True)

    p_record_fix = subparsers.add_parser(
        "record-auto-fix",
        help="Increment the per-day auto-fix attempt counter for a PR",
    )
    p_record_fix.add_argument("pr_number", type=int)
    p_record_fix.add_argument("--repo", required=True)

    p_record_bump = subparsers.add_parser(
        "record-channel-bump",
        help="Record that a stale-review channel bump was posted for a PR",
    )
    p_record_bump.add_argument("pr_number", type=int)
    p_record_bump.add_argument("--repo", required=True)


def _add_query_subparsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register read-only query, discovery, and queue subcommands."""
    p_status = subparsers.add_parser("status", help="Show current monitor state")
    p_status.add_argument("--repo")
    p_status.add_argument("--all", dest="all_repos", action="store_true")
    p_status.add_argument("--json", dest="as_json", action="store_true")

    p_list = subparsers.add_parser("list-repos", help="List repos with state files")
    p_list.add_argument("--json", dest="as_json", action="store_true")

    p_check = subparsers.add_parser("check", help="Run one monitoring cycle for a PR")
    p_check.add_argument("pr_number", type=int)
    p_check.add_argument("--repo", required=True)

    subparsers.add_parser(
        "consume-pending",
        help="Scan /tmp/review-monitor/pending/ and register any PRs found",
    )
    subparsers.add_parser(
        "catchup",
        help=(
            "Mark every existing author-role attention PR as already notified"
            " (no pings fired)"
        ),
    )
    subparsers.add_parser(
        "pending-channel-bumps",
        help="Across all repos, list author PRs needing a stale-review channel bump",
    )

    p_discover = subparsers.add_parser(
        "discover",
        help="Auto-register open author PRs from the past N days for a repo",
    )
    p_discover.add_argument("--repo", required=True)
    p_discover.add_argument("--repo-path", required=True)
    p_discover.add_argument("--days", type=int, default=7)

    p_recover = subparsers.add_parser(
        "recover-reviews",
        help=(
            "Auto-register open PRs reviewed by you in the past N days"
            " that were never monitored"
        ),
    )
    p_recover.add_argument("--repo", required=True)
    p_recover.add_argument("--repo-path", required=True)
    p_recover.add_argument("--days", type=int, default=7)

    p_enqueue = subparsers.add_parser(
        "enqueue-action",
        help="Write one outbound action to the Desktop action queue",
    )
    p_enqueue.add_argument(
        "--type",
        required=True,
        dest="action_type",
        choices=sorted(DESKTOP_ACTION_TYPES),
    )
    p_enqueue.add_argument("--repo")
    p_enqueue.add_argument("--pr", type=int, dest="pr_number")
    p_enqueue.add_argument(
        "--payload", required=True, help="JSON object with the action's message data"
    )


def _build_argument_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        description="Review monitor: track PR review threads",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_lifecycle_subparsers(subparsers)
    _add_signal_subparsers(subparsers)
    _add_query_subparsers(subparsers)
    return parser


def _dispatch_status_all(as_json: bool) -> None:
    """Print merged status across all repos."""
    combined = cmd_status_all()
    if as_json:
        print(json.dumps(combined.to_dict(), indent=2))
        return
    if not combined.monitored:
        print("No PRs currently monitored across any repo.")
    else:
        print(f"{'PR':<40} {'ROLE':<10} {'STATUS':<12} {'THREADS ADDRESSED'}")
        print("-" * 80)
        for key, pr in sorted(combined.monitored.items()):
            total = len(pr.thread_status)
            addressed = sum(1 for ts in pr.thread_status.values() if ts.is_addressed)
            threads_col = f"{addressed}/{total}" if total else "n/a"
            print(f"{key:<40} {pr.role:<10} {pr.status:<12} {threads_col}")
    print(f"\n{len(combined.completed)} completed PR(s) in history.")


def _dispatch_pr_state_mutation(args: argparse.Namespace) -> None:
    """Dispatch the single-PR state mutations
    (set-status/confirm-thread/mark-*/cursor)."""
    if args.command == "set-status":
        cmd_set_status(pr_number=args.pr_number, repo=args.repo, status=args.status)
    elif args.command == "confirm-thread":
        cmd_confirm_thread(
            pr_number=args.pr_number, repo=args.repo, thread_id=args.thread_id
        )
    elif args.command == "mark-notified":
        cmd_mark_notified(
            pr_number=args.pr_number, repo=args.repo, state_value=args.state_value
        )
    elif args.command == "mark-escalated":
        cmd_mark_escalated(pr_number=args.pr_number, repo=args.repo)
    elif args.command == "update-slack-cursor":
        cmd_update_slack_cursor(
            pr_number=args.pr_number, repo=args.repo, last_seen_ts=args.last_seen_ts
        )


def _dispatch_discovery(args: argparse.Namespace) -> None:
    """Dispatch the repo-wide PR discovery commands (discover / recover-reviews)."""
    if args.command == "discover":
        result = cmd_discover(repo=args.repo, days=args.days, repo_path=args.repo_path)
    else:
        result = cmd_recover_reviews(
            repo=args.repo, days=args.days, repo_path=args.repo_path
        )
    print(json.dumps(result, indent=2))


def _dispatch_mutation(args: argparse.Namespace) -> None:
    """Dispatch register/drop/complete/nudge-ok/record-nudge/set-status/
    confirm-thread commands."""
    if args.command == "register":
        td = json.loads(args.thread_details) if args.thread_details else None
        cmd_register(
            pr_number=args.pr_number,
            role=args.role,
            repo=args.repo,
            repo_path=args.repo_path,
            sha=args.sha,
            review_id=args.review_id,
            threads=args.threads,
            thread_details=td,
            slack_channel=args.slack_channel,
            slack_ts=args.slack_ts,
        )
    elif args.command == "drop":
        cmd_drop(pr_number=args.pr_number, repo=args.repo)
    elif args.command == "complete":
        cmd_complete(pr_number=args.pr_number, repo=args.repo, reason=args.reason)
    elif args.command == "nudge-ok":
        print(
            json.dumps(cmd_nudge_ok(pr_number=args.pr_number, repo=args.repo), indent=2)
        )
    elif args.command == "record-nudge":
        cmd_record_nudge(pr_number=args.pr_number, repo=args.repo)
    elif args.command == "enqueue-action":
        result = cmd_enqueue_action(
            action=args.action_type,
            payload=json.loads(args.payload),
            repo=args.repo,
            pr_number=args.pr_number,
        )
        print(json.dumps(result, indent=2))
    else:
        _dispatch_pr_state_mutation(args)


_MUTATION_COMMANDS: frozenset[str] = frozenset(
    {
        "register",
        "drop",
        "complete",
        "nudge-ok",
        "record-nudge",
        "enqueue-action",
        "set-status",
        "confirm-thread",
        "mark-notified",
        "mark-escalated",
        "update-slack-cursor",
    }
)

# Query commands: each maps to a callable returning a JSON-serializable result
# that main() prints with indent=2. list-repos and status branch on flags and
# are handled separately.
_QUERY_COMMANDS: dict[str, Callable[[argparse.Namespace], Any]] = {
    "check": lambda a: cmd_check(pr_number=a.pr_number, repo=a.repo),
    "slack-thread-cursor": lambda a: cmd_slack_thread_cursor(
        pr_number=a.pr_number, repo=a.repo
    ),
    "consume-pending": lambda _: cmd_consume_pending(),
    "record-auto-fix": lambda a: cmd_record_auto_fix(
        pr_number=a.pr_number, repo=a.repo
    ),
    "record-channel-bump": lambda a: cmd_record_channel_bump(
        pr_number=a.pr_number, repo=a.repo
    ),
    "pending-channel-bumps": lambda _: cmd_pending_channel_bumps(),
    "mark-comment-review": lambda a: cmd_mark_comment_review(
        pr_number=a.pr_number,
        repo=a.repo,
        review_id=a.review_id,
        classification=a.classification,
    ),
    "catchup": lambda _: cmd_catchup(),
    "ack-delta": lambda a: cmd_ack_delta(pr_number=a.pr_number, repo=a.repo, sha=a.sha),
}


def _print_repo_list(*, as_json: bool) -> None:
    """Print the monitored-repo list as JSON or newline-separated names."""
    repos = cmd_list_repos()
    if as_json:
        print(json.dumps(repos))
    else:
        for r in repos:
            print(r)


def _dispatch_status_command(args: argparse.Namespace) -> None:
    """Run the ``status`` command for one repo or all repos.

    When neither ``--repo`` nor ``--all`` is supplied, auto-derive ``--repo``
    from ``gh repo view`` against the current working directory so callers
    inside a checkout don't have to pass it explicitly.
    """
    if args.all_repos:
        _dispatch_status_all(as_json=args.as_json)
        return
    repo = args.repo or _run_gh(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    if not repo:
        print(
            "Error: --repo is required when --all is not set "
            "(auto-derive via `gh repo view` failed — run inside a checkout "
            "or pass --repo owner/name)",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd_status(repo=repo, as_json=args.as_json)


def main() -> None:
    """CLI entry point."""
    parser = _build_argument_parser()
    args = parser.parse_args()
    command: str | None = args.command

    if command in _MUTATION_COMMANDS:
        _dispatch_mutation(args)
    elif command in ("discover", "recover-reviews"):
        _dispatch_discovery(args)
    elif command == "list-repos":
        _print_repo_list(as_json=args.as_json)
    elif command == "status":
        _dispatch_status_command(args)
    else:
        handler = _QUERY_COMMANDS.get(command or "")
        if handler is not None:
            print(json.dumps(handler(args), indent=2))


if __name__ == "__main__":
    main()
