"""Runtime path helpers shared across Claude and Codex integrations."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Return the tracked global-claude repo root."""
    return Path(__file__).resolve().parents[2]


def claude_home() -> Path:
    """Return the Claude config home, honoring local overrides."""
    override = os.environ.get("GLOBAL_CLAUDE_HOME") or os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def codex_home() -> Path:
    """Return the Codex config home, honoring local overrides."""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def settings_json_path() -> Path:
    """Return the canonical settings.json path for shared automation."""
    override = os.environ.get("GLOBAL_CLAUDE_SETTINGS_PATH")
    if override:
        return Path(override).expanduser()
    return claude_home() / "settings.json"


def todos_dir() -> Path:
    """Return the TodoWrite state directory."""
    override = os.environ.get("GLOBAL_CLAUDE_TODOS_DIR")
    if override:
        return Path(override).expanduser()
    return claude_home() / "todos"


def review_monitor_dir() -> Path:
    """Return the central review-monitor state directory."""
    override = os.environ.get("GLOBAL_CLAUDE_REVIEW_MONITOR_DIR")
    if override:
        return Path(override).expanduser()
    return claude_home() / "review-monitor"


def desktop_queue_dir() -> Path:
    """Desktop comms-queue dir — must be reachable by the sandboxed Cowork drain.

    Lives under a connected project folder (not ``~/.claude``, which the
    sandbox cannot read) so the host cron writes and the drain reads the
    same place. Visible (non-dot) so the drain can enumerate it.
    """
    override = os.environ.get("GLOBAL_CLAUDE_DESKTOP_QUEUE_DIR")
    if override:
        return Path(override).expanduser()
    # Default points at a Claude Desktop connected-project folder. Override via
    # GLOBAL_CLAUDE_DESKTOP_QUEUE_DIR to match your own project name.
    return (
        Path.home()
        / "Documents"
        / "Claude"
        / "Projects"
        / "My Project"
        / "review-monitor"
        / "desktop-queue"
    )


def review_monitor_script_path() -> Path:
    """Return the review-monitor script path, favoring the checked-out repo."""
    override = os.environ.get("GLOBAL_CLAUDE_REVIEW_MONITOR_SCRIPT")
    if override:
        return Path(override).expanduser()

    repo_script = repo_root() / "scripts" / "review_monitor.py"
    if repo_script.exists():
        return repo_script

    return claude_home() / "scripts" / "review_monitor.py"
