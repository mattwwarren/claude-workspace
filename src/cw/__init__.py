"""claude-workspace: Multi-session workspace orchestrator for Claude Code."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed distribution's version, which is
    # built from pyproject.toml. Never hardcode here — it drifts (see v1.1.1).
    __version__ = version("claude-workspace")
except PackageNotFoundError:  # running from a source tree with no installed dist
    __version__ = "0.0.0+unknown"
