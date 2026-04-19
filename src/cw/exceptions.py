"""Exception hierarchy for cw."""

from __future__ import annotations


class CwError(Exception):
    """Base exception for all cw errors."""


class WorktreeError(CwError):
    """Error from git worktree operations."""
