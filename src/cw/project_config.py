"""Safe, dependency-light access to a repository's project configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - downstream repo without PyYAML
    yaml = None  # type: ignore[assignment]

PROJECT_CONFIG_RELPATH = Path(".claude") / "project-config.yaml"


def load_project_config_dict(
    root: Path, *, yaml_module: Any = None
) -> dict[str, object] | None:
    """Read ``root/.claude/project-config.yaml`` as a dict, or return None."""
    path = root / PROJECT_CONFIG_RELPATH
    parser = yaml if yaml_module is None else yaml_module
    if parser is None or not path.exists():
        return None
    try:
        raw = parser.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, parser.YAMLError):
        return None
    return raw if isinstance(raw, dict) else None
