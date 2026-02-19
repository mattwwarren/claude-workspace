"""Exception hierarchy for cw."""

from __future__ import annotations


class CwError(Exception):
    """Base exception for all cw errors."""


class ZellijError(CwError):
    """Error from Zellij operations."""


class WorktreeError(CwError):
    """Error from git worktree operations."""
