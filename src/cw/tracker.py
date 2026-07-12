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

from pathlib import Path

import yaml

# Repo-relative path to the per-client tracker config the auto-dev skills read.
PROJECT_CONFIG_RELPATH = Path(".claude") / "project-config.yaml"

# Canonical tracker-system identifier for GitHub Issues. Used at spawn/session
# chokepoints to decide whether to withhold Linear MCP tools from headless workers.
TRACKER_GITHUB_ISSUES = "github-issues"


def load_project_config_dict(root: Path) -> dict[str, object] | None:
    """Read <root>/.claude/project-config.yaml as a dict, or None on any failure.

    Consolidates the safe-read walk (missing file, unparseable YAML, non-dict
    root) shared by every ``.claude/project-config.yaml`` consumer — callers
    then do their own ``.get(key)`` + type-narrowing for the section they need
    (e.g. ``resolve_review_strategy``'s ``review_strategy`` block, ``cw
    doctor``'s checks). Returns the raw root mapping unfiltered; a caller that
    also needs "absent" distinguished from "present but wrong shape" for a
    sub-key gets that from the returned dict directly.
    """
    path = root / PROJECT_CONFIG_RELPATH
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return raw if isinstance(raw, dict) else None


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
