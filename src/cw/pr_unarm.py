"""Disarm a PR's auto-merge and verify it by read-back (leaf module).

Extracted from the recipe engine (``cw.reconcile.review_recipes.repair_engine``)
so the red-shipped-PR recovery (#2945) and the repair recipes share ONE
implementation. It reads ``gh.fetch_pr_head`` and ``gh.disarm_pr_auto_merge``
through the ``cw.gh`` package namespace, so a test patch on that namespace
(``tests/_changelog_repair_helpers.py::FakeGhHead.install``) reaches it. It
imports nothing from ``cw.reconcile`` or ``cw.dev_queue``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw import gh

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_READ_TIMEOUT = 15
DEFAULT_DISARM_TIMEOUT = 30


class UnarmOutcome(NamedTuple):
    """Whether the PR is verified unarmed, whether it was armed, and why."""

    ok: bool
    was_armed: bool | None
    detail: str


def ensure_pr_unarmed(
    pr_ref: str,
    *,
    cwd: Path,
    head: gh.PrHead | None = None,
    read_timeout: int = DEFAULT_READ_TIMEOUT,
    disarm_timeout: int = DEFAULT_DISARM_TIMEOUT,
) -> UnarmOutcome:
    """Read the PR's armed state, disarm if armed, verify by reading back.

    *head* reuses a read the caller already made. The read-back wins over
    the disarm exit code (the ``arm_automerge`` rule, #1140): a non-zero exit
    with an unarmed read-back is success. Fails closed (``ok`` False) on an
    unreadable PR, a disarm that could not run or timed out, or a read-back
    that is unreadable or still armed. Never raises.
    """
    current = (
        head
        if head is not None
        else gh.fetch_pr_head(pr_ref, cwd=cwd, timeout=read_timeout)
    )
    if current is None:
        return UnarmOutcome(False, None, "PR head unreadable; armed state unknown")
    if not current.auto_merge_armed:
        return UnarmOutcome(True, False, "auto-merge not armed")
    result = gh.disarm_pr_auto_merge(pr_ref, cwd=cwd, timeout=disarm_timeout)
    if result is None:
        return UnarmOutcome(
            False,
            True,
            "gh pr merge --disable-auto did not run or timed out; still ARMED",
        )
    after = gh.fetch_pr_head(pr_ref, cwd=cwd, timeout=read_timeout)
    if after is None:
        return UnarmOutcome(
            False,
            True,
            f"disarm exit {result.returncode}; read-back unreadable,"
            " may still be ARMED",
        )
    if after.auto_merge_armed:
        return UnarmOutcome(
            False, True, f"disarm exit {result.returncode}; read-back still ARMED"
        )
    return UnarmOutcome(True, True, f"disarmed (exit {result.returncode})")
