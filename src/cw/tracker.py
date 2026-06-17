"""Shared tracker-resolution utilities for spawn and session chokepoints.

Centralises the ``tracking.primary.system`` read so that ``spawn.py`` and
``session.py`` share one implementation rather than each duplicating the YAML
walk that ``doctor.py`` already owned.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# Repo-relative path to the per-client tracker config the auto-dev skills read.
PROJECT_CONFIG_RELPATH = Path(".claude") / "project-config.yaml"


def resolve_tracker(root: Path) -> str | None:
    """Return tracking.primary.system from <root>/.claude/project-config.yaml, or None.

    Returns None when the file is absent, unparseable, or the key is missing —
    callers treat None as "unknown tracker, don't restrict."
    """
    path = root / PROJECT_CONFIG_RELPATH
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    tracking = raw.get("tracking")
    if not isinstance(tracking, dict):
        return None
    primary = tracking.get("primary")
    if not isinstance(primary, dict):
        return None
    system = primary.get("system")
    return system if isinstance(system, str) else None
