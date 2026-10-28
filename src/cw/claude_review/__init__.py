"""The Claude confirm round: one ``claude -p`` review pass per role (#2791).

Converge-to-clean review mode (``review.convergence: clean``) needs an
independent confirm round. On a codex lane that round runs on Claude: the
detached ``cw codex run`` driver calls :func:`run_claude_review`, and the same
pass is exposed as ``cw review confirm --backend claude``. Nothing here is
lane-specific.

Submodules, each importing only the ones above it:

- ``_runner`` -- the ``claude -p`` seam (:class:`ClaudeRunner`,
  :class:`RealClaudeRunner`, :class:`FakeClaudeRunner`), the fixed posture
  argv (:func:`build_claude_review_argv`) and the ``_which_claude`` seam.
- ``_roles`` -- :func:`run_claude_roles`, the per-role loop with the
  shared deadline and the worktree-unchanged check (G4).
- ``core`` -- :func:`run_claude_review` and :class:`ClaudeReviewRound`.
"""

from __future__ import annotations

from cw.claude_review._roles import (
    ClaudeRolesOutcome,
    WorktreeMutation,
    run_claude_roles,
)
from cw.claude_review._runner import (
    CLAUDE_BINARY,
    CLAUDE_REVIEW_TOOLS,
    ClaudeRunner,
    ClaudeRunResult,
    FakeClaudeRunner,
    RealClaudeRunner,
    _which_claude,
    build_claude_review_argv,
)
from cw.claude_review.core import (
    CLAUDE_BACKEND_DETAILS_PREFIX,
    CLAUDE_REVIEWER_LABEL,
    ClaudeReviewRound,
    run_claude_review,
)

__all__ = [
    "CLAUDE_BACKEND_DETAILS_PREFIX",
    "CLAUDE_BINARY",
    "CLAUDE_REVIEWER_LABEL",
    "CLAUDE_REVIEW_TOOLS",
    "ClaudeReviewRound",
    "ClaudeRolesOutcome",
    "ClaudeRunResult",
    "ClaudeRunner",
    "FakeClaudeRunner",
    "RealClaudeRunner",
    "WorktreeMutation",
    "_which_claude",
    "build_claude_review_argv",
    "run_claude_review",
    "run_claude_roles",
]
