"""Safe, dependency-light access to a repository's project configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_CONFIG_RELPATH = Path(".claude") / "project-config.yaml"


def load_project_config_dict(root: Path) -> dict[str, object] | None:
    """Read ``root/.claude/project-config.yaml`` as a dict, or return None."""
    path = root / PROJECT_CONFIG_RELPATH
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return raw if isinstance(raw, dict) else None
