"""Safe, dependency-light access to a repository's project configuration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

yaml: ModuleType | None
try:
    import yaml
except ImportError:  # pragma: no cover - downstream repo without PyYAML
    yaml = None

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


class PrepareConfigError(ValueError):
    """``tests.prepare`` is present but is not a usable shell command string."""


def read_tests_prepare(root: Path) -> str | None:
    """Return the stripped ``tests.prepare`` command of *root*, or None when unset.

    None covers an absent file, an absent or null ``tests`` block, an absent or
    null ``prepare``, and a blank value. A non-mapping ``tests`` block, a
    non-string ``prepare``, or a file that is not valid UTF-8 raises
    :class:`PrepareConfigError`, so a caller can tell "absent" from "malformed".
    """
    try:
        config = load_project_config_dict(root)
    except ValueError as exc:
        msg = f"{PROJECT_CONFIG_RELPATH} is not valid UTF-8: {exc}"
        raise PrepareConfigError(msg) from exc
    if config is None:
        return None
    tests = config.get("tests")
    if tests is None:
        return None
    if not isinstance(tests, dict):
        msg = "`tests` in .claude/project-config.yaml must be a mapping"
        raise PrepareConfigError(msg)
    prepare = tests.get("prepare")
    if prepare is None:
        return None
    if not isinstance(prepare, str):
        msg = (
            "`tests.prepare` must be a single shell command string, got"
            f" {type(prepare).__name__}"
        )
        raise PrepareConfigError(msg)
    return prepare.strip() or None
