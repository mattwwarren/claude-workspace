"""Data models for the review monitor: thread status, comment reviews, PRs, state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


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
