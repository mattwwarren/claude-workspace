"""The tracker seam and the tracker-mode decision (#3041, R7/R9).

:class:`DebtTracker` is the narrow structural Protocol the library files
through. ``cw.sprint.GhClient`` satisfies it, so this package never imports
``cw.sprint``; tests inject ``tests._tool_fakes.FakeGh``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from cw.project_config import load_project_config_dict
from cw.review_debt_filing._constants import TRACKER_LINEAR
from cw.review_debt_filing._models import TrackerMode
from cw.tracker import TRACKER_GITHUB_ISSUES

if TYPE_CHECKING:
    from pathlib import Path

    from cw.gh import GhFailure, IssueCreateResult, LabelledIssuesLookup


class DebtTracker(Protocol):
    """The ``gh`` calls review-debt filing makes, each with a ``timeout``."""

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: list[str],
        milestone: int | None = None,
        timeout: int | None = None,
    ) -> IssueCreateResult:
        """Create an issue; a typed result tells the failure kinds apart."""
        ...

    def ensure_label(
        self,
        name: str,
        color: str,
        description: str,
        *,
        timeout: int | None = None,
    ) -> GhFailure | None:
        """Create or update a label; None on success."""
        ...

    def list_open_issues_by_label(
        self, label: str, *, timeout: int | None = None
    ) -> LabelledIssuesLookup:
        """Return the open issues carrying *label*, with their bodies."""
        ...

    def fetch_issue_body(
        self, ticket_id: str, *, timeout: int | None = None
    ) -> str | None:
        """Return an issue's body, or None on any failure."""
        ...

    def update_issue_body(
        self, number: int, body: str, *, timeout: int | None = None
    ) -> bool:
        """Replace an issue's body. True on success."""
        ...

    def post_issue_comment(
        self, ticket_id: str, body: str, *, timeout: int | None = None
    ) -> bool:
        """Post an agent-authored comment. True on success."""
        ...


def tracker_mode(system: str | None) -> TrackerMode:
    """Strict polarity (R9): exactly ``github-issues`` or ``linear``, else skip."""
    if system == TRACKER_GITHUB_ISSUES:
        return TrackerMode.GITHUB
    if system == TRACKER_LINEAR:
        return TrackerMode.MCP
    return TrackerMode.SKIP_NO_TRACKER


def auto_create_issues(workspace: Path) -> bool:
    """``tracking.primary.auto_create_issues`` under *workspace*, strictly.

    Only a literal YAML ``true`` files; a missing file, a missing key or any
    non-bool value means emit-only (plan Adopted Assumption 6), because filing
    is a visible side effect a client must opt into.
    """
    raw = load_project_config_dict(workspace)
    tracking = raw.get("tracking") if raw is not None else None
    primary = tracking.get("primary") if isinstance(tracking, dict) else None
    value = primary.get("auto_create_issues") if isinstance(primary, dict) else None
    return value is True
