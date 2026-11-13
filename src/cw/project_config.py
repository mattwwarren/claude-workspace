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


class ProjectConfigError(ValueError):
    """The project config file exists but cannot be read or parsed (strict mode)."""


def load_project_config_dict(
    root: Path, *, yaml_module: Any = None, strict: bool = False
) -> dict[str, object] | None:
    """Read ``root/.claude/project-config.yaml`` as a dict, or return None.

    Default (lenient) mode returns None for an unreadable or unparseable file,
    which every existing consumer relies on. ``strict=True`` raises
    :class:`ProjectConfigError` instead, so a caller that must fail closed can
    tell "no config" (None) from "config present but unusable".
    """
    path = root / PROJECT_CONFIG_RELPATH
    parser = yaml if yaml_module is None else yaml_module
    if parser is None or not path.exists():
        return None
    try:
        raw = parser.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, parser.YAMLError) as exc:
        if strict:
            msg = f"{PROJECT_CONFIG_RELPATH} could not be read or parsed: {exc}"
            raise ProjectConfigError(msg) from exc
        return None
    return raw if isinstance(raw, dict) else None


class PrepareConfigError(ValueError):
    """``tests.prepare`` is present but is not a usable shell command string."""


def read_tests_prepare(root: Path) -> str | None:
    """Return the stripped ``tests.prepare`` command of *root*, or None when unset.

    None covers an absent file, an absent or null ``tests`` block, an absent or
    null ``prepare``, and a blank value. A file that exists but cannot be read,
    parsed, or decoded as UTF-8, a non-mapping ``tests`` block, and a non-string
    ``prepare`` raise :class:`PrepareConfigError`, so a caller can tell "absent"
    from "malformed or unreadable" and fail closed on the latter.
    """
    try:
        config = load_project_config_dict(root, strict=True)
    except ProjectConfigError as exc:
        raise PrepareConfigError(str(exc)) from exc
    except UnicodeDecodeError as exc:
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
