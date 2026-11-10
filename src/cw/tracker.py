"""Shared project-config.yaml utilities.

Two layers: ``load_project_config_dict`` is the generic "read
``.claude/project-config.yaml`` as a dict, safe-degrade to None on any
failure" primitive shared by every consumer of that file — ``resolve_tracker``
below (so ``spawn.py``/``session.py`` share one ``tracking.primary.system``
resolution rather than each duplicating the YAML walk ``doctor.py`` also
needs), ``cw.review_strategy.resolve_review_strategy`` (RFC 0010 P4), and
``cw.doctor``'s config checks. ``resolve_tracker`` itself stays here as the
tracker-specific resolution built on top of that shared primitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from cw import project_config

if TYPE_CHECKING:
    from pathlib import Path

PROJECT_CONFIG_RELPATH = project_config.PROJECT_CONFIG_RELPATH
load_project_config_dict = project_config.load_project_config_dict

# Canonical tracker-system identifier for GitHub Issues. Used at spawn/session
# chokepoints to decide whether to withhold Linear MCP tools from headless workers.
TRACKER_GITHUB_ISSUES = "github-issues"

#: The Linear tracker-system identifier (lifted from ``review_debt_filing``).
TRACKER_LINEAR: Literal["linear"] = "linear"


class TrackerClass(StrEnum):
    """Tri-state tracker polarity for RFC 0018 S3 (#3055).

    ``GITHUB`` is ``github-issues``; ``OTHER`` is any other non-empty system
    (Linear, ...); ``UNSET`` is no resolvable ``tracking.primary.system``.
    The existing binary GitHub-vs-not sites keep their own polarity; this is
    for new code that must tell "not GitHub" apart from "unknown".
    """

    GITHUB = "github"
    OTHER = "other"
    UNSET = "unset"


@dataclass(frozen=True)
class TrackerKind:
    """A resolved tracker: its class and, unless UNSET, its system string."""

    tracker_class: TrackerClass
    system: str | None


def tracker_kind(root: Path) -> TrackerKind:
    """Classify ``<root>``'s tracker as GITHUB, OTHER(system) or UNSET.

    Built on ``resolve_tracker``: ``None`` and the empty string are UNSET.
    """
    system = resolve_tracker(root)
    if not system:
        return TrackerKind(TrackerClass.UNSET, None)
    if system == TRACKER_GITHUB_ISSUES:
        return TrackerKind(TrackerClass.GITHUB, system)
    return TrackerKind(TrackerClass.OTHER, system)


def resolve_tracker(root: Path) -> str | None:
    """Return tracking.primary.system from <root>/.claude/project-config.yaml, or None.

    Returns None when the file is absent, unparseable, or the key is missing —
    callers treat None as "unknown tracker, don't restrict."
    """
    raw = load_project_config_dict(root)
    if raw is None:
        return None
    tracking = raw.get("tracking")
    if not isinstance(tracking, dict):
        return None
    primary = tracking.get("primary")
    if not isinstance(primary, dict):
        return None
    system = primary.get("system")
    return system if isinstance(system, str) else None
