"""Per-repo state persistence under the central review-monitor directory."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from utils.runtime_paths import review_monitor_dir

from review_monitor_lib.models import MonitorState

logger = logging.getLogger(__name__)


# Central state directory — survives worktree cleanup
CENTRAL_STATE_DIR = review_monitor_dir()

# Legacy state file path (for migration — used in Task 2)
LEGACY_STATE_FILE = Path(".claude/review-monitor-state.json")


def state_path_for_repo(repo: str) -> Path:
    """Return central state path for a repo like 'owner/repo'."""
    safe_name = repo.replace("/", "--")
    return CENTRAL_STATE_DIR / f"{safe_name}.json"


def _merge_states(central: MonitorState, legacy: MonitorState) -> MonitorState:
    """Merge legacy state into central, keeping newer entries per PR."""
    for key, legacy_pr in legacy.monitored.items():
        if key not in central.monitored:
            central.monitored[key] = legacy_pr
        else:
            central_pr = central.monitored[key]
            if legacy_pr.last_checked_at > central_pr.last_checked_at:
                central.monitored[key] = legacy_pr
    for key, completed_data in legacy.completed.items():
        if key not in central.completed:
            central.completed[key] = completed_data
    return central


def _load_json_state(path: Path, *, strict: bool, label: str) -> MonitorState | None:
    """Read and parse one state file, honoring the ``strict`` OSError contract.

    Returns ``None`` when *path* does not exist. Corrupt-but-readable content
    always degrades to ``None`` with a warning; an unreadable file re-raises
    under ``strict`` and degrades to ``None`` otherwise. Shared by both state
    files ``load_state`` reads (central and legacy) so the strict guard covers
    each identically — see #2189 round 2, where a copy of this check lived
    only on the central-file read and the legacy read stayed silent.
    """
    if not path.exists():
        return None
    try:
        return MonitorState.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
        if strict and isinstance(e, OSError):
            raise
        logger.warning("Corrupt %s state file %s, starting fresh: %s", label, path, e)
        return None


def load_state(repo: str, *, strict: bool = False) -> MonitorState:
    """Load monitor state for a specific repo from the central directory.

    If no central file exists but a legacy ``.claude/review-monitor-state.json``
    is present in the current directory, the legacy data is migrated
    automatically: it is merged into central (newer ``last_checked_at`` wins
    per PR), saved to the central location, and the legacy file is deleted.

    An unreadable central or legacy file degrades to empty state with a
    warning, which suits read-only callers. A caller that goes on to *write*
    the state passes ``strict=True`` so an ``OSError`` reading either file
    propagates instead — otherwise the write would replace whatever the
    unreadable file held. Corrupt but readable content starts fresh either way.
    """
    state_file = state_path_for_repo(repo)
    central_state = _load_json_state(state_file, strict=strict, label="monitor")

    legacy_state = _load_json_state(LEGACY_STATE_FILE, strict=strict, label="legacy")
    if legacy_state is not None:
        central_state = (
            legacy_state
            if central_state is None
            else _merge_states(central_state, legacy_state)
        )
        # Persist merged state and remove legacy file
        save_state(central_state, repo)
        try:
            LEGACY_STATE_FILE.unlink()
            logger.info(
                "Migrated legacy state from %s to central directory",
                LEGACY_STATE_FILE,
            )
        except OSError as e:
            logger.warning(
                "Could not delete legacy state file %s: %s", LEGACY_STATE_FILE, e
            )

    if central_state is None:
        return MonitorState(monitored={}, completed={})
    return central_state


def save_state(state: MonitorState, repo: str) -> None:
    """Save monitor state atomically to the central directory."""
    state_file = state_path_for_repo(repo)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = state_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(state.to_dict(), indent=2) + "\n")
    tmp_file.rename(state_file)


def cmd_list_repos() -> list[str]:
    """Return a list of repo names that have state files in the central directory."""
    if not CENTRAL_STATE_DIR.exists():
        return []
    repos = []
    for f in sorted(CENTRAL_STATE_DIR.glob("*.json")):
        repo_name = f.stem.replace("--", "/", 1)
        repos.append(repo_name)
    return repos
