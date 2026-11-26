"""The UI-evidence gate of FINALIZE Step 4d, as Python (#3543).

The claude-native finalize doc runs this gate as a Bash fence. Codex FINALIZE has
no shell to run it, so cw does: :func:`find_ui_files` lists the non-test
frontend files in the shipped diff, and :func:`check_ui_evidence` judges the PR
body against them.

The split lets the caller know whether anything depends on the PR body before
it reads it. Neither function blocks: a gate that fires holds the auto-merge arm
and the run still ships (the doc's headless rule).

No codex imports: any executor can call this.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw._git import LOCAL_GIT_TIMEOUT_SECONDS, git_output
from cw.project_config import load_project_config_dict

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "UI_EVIDENCE_MISSING",
    "UI_EVIDENCE_UNREADABLE",
    "UiEvidence",
    "UiFiles",
    "check_ui_evidence",
    "find_ui_files",
]

_log = logging.getLogger(__name__)

UI_EVIDENCE_MISSING: Final = "ui_evidence_missing"
UI_EVIDENCE_UNREADABLE: Final = "ui_evidence_unreadable"
UI_PATHS_REGEX_INVALID: Final = "ui_paths_regex_invalid"

_CONFIG_KEY: Final = "ui_paths_regex"
_DEFAULT_UI_PATHS: Final = r"^frontends/.*\.(tsx|ts)$"
_TEST_PATHS: Final = re.compile(r"(__tests__|\.test\.|\.spec\.)")
_MEDIA: Final = re.compile(r"<img|!\[.*\]\(.*\)|user-attachments|\.webm|\.mp4")


@dataclass(frozen=True)
class UiFiles:
    """The UI files in a diff; *unreadable* when the diff could not be computed."""

    paths: tuple[str, ...] = ()
    friction: tuple[str, ...] = ()
    unreadable: bool = False


@dataclass(frozen=True)
class UiEvidence:
    """The gate's verdict: *hold* the arm, and why (friction tokens)."""

    hold: bool = False
    friction: tuple[str, ...] = ()


def _ui_regex(worktree: Path) -> tuple[re.Pattern[str], tuple[str, ...]]:
    """The worktree's ``ui_paths_regex`` (or the default) and its friction."""
    config = load_project_config_dict(worktree)
    value = config.get(_CONFIG_KEY) if config is not None else None
    if isinstance(value, str):
        try:
            return re.compile(value), ()
        except re.error:
            return re.compile(_DEFAULT_UI_PATHS), (UI_PATHS_REGEX_INVALID,)
    return re.compile(_DEFAULT_UI_PATHS), ()


def find_ui_files(worktree: Path, *, fork_point: str, head_sha: str) -> UiFiles:
    """The non-test files matching ``ui_paths_regex`` in ``fork_point...head_sha``.

    The regex is read from the worktree's own project config. An empty fork
    point or a failing ``git diff`` is ``unreadable``: cw cannot say there are
    no UI files, so the caller holds the arm.
    """
    pattern, friction = _ui_regex(worktree)
    if not fork_point:
        return UiFiles(friction=friction, unreadable=True)
    try:
        out = git_output(
            ["diff", "--name-only", "-z", f"{fork_point}...{head_sha}"],
            cwd=worktree,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
            errors="surrogateescape",
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        _log.warning("ui evidence: git diff failed for the shipped diff")
        return UiFiles(friction=friction, unreadable=True)
    paths = tuple(
        path
        for path in out.split("\0")
        if path and pattern.search(path) and not _TEST_PATHS.search(path)
    )
    return UiFiles(paths=paths, friction=friction)


def check_ui_evidence(files: UiFiles, *, body: str | None) -> UiEvidence:
    """Judge the PR *body* against *files*; pure.

    *body* is the media-check text (cw's own regions already stripped), or
    ``None`` when the PR body could not be read. The verdict carries
    ``files.friction`` in every case.
    """
    if files.unreadable:
        return _hold(files, UI_EVIDENCE_UNREADABLE)
    if not files.paths:
        return UiEvidence(friction=files.friction)
    if body is None:
        return _hold(files, UI_EVIDENCE_UNREADABLE)
    if _MEDIA.search(body) is None:
        return _hold(files, UI_EVIDENCE_MISSING)
    return UiEvidence(friction=files.friction)


def _hold(files: UiFiles, token: str) -> UiEvidence:
    return UiEvidence(hold=True, friction=(*files.friction, token))
