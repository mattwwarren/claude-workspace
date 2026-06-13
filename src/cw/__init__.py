"""claude-workspace: Multi-session workspace orchestrator for Claude Code."""

from importlib.metadata import PackageNotFoundError, version


def _resolve_version() -> str:
    """Single source of truth: the installed distribution's version (built from
    pyproject.toml). Never hardcode here — it drifts (see v1.1.1)."""
    try:
        return version("claude-workspace")
    except PackageNotFoundError:  # running from a source tree with no installed dist
        return "0.0.0+unknown"


__version__ = _resolve_version()
