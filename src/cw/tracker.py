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

from typing import TYPE_CHECKING

from cw import project_config

if TYPE_CHECKING:
    from pathlib import Path

PROJECT_CONFIG_RELPATH = project_config.PROJECT_CONFIG_RELPATH
load_project_config_dict = project_config.load_project_config_dict

# Canonical tracker-system identifier for GitHub Issues. Used at spawn/session
# chokepoints to decide whether to withhold Linear MCP tools from headless workers.
TRACKER_GITHUB_ISSUES = "github-issues"


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
