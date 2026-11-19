"""Producer-side redaction and splitting for prose enqueued to the outbox (#3707).

Wraps the planner the CLI post path already uses (:func:`cw.tracker_post.plan_parts`)
and returns the whole :class:`~cw.tracker_post.PartPlan`, so the producer keeps
``redacted`` (the turn-side ``plan_turn_parts`` drops it). Outside
``src/cw/outbox/`` because the store package imports no tracker code.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from cw.redact_post_text import git_toplevel, redaction_anchors, resolve_home
from cw.tracker_post import plan_parts

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.tracker_post import PartPlan


def plan_prose_parts(text: str, client: ClientConfig, worktree: Path) -> PartPlan:
    """Redact and split *text* for *client*, anchored to *worktree*.

    Raises ``ValueError`` when the text cannot be planned (the caller turns that
    into a ``plan_refused`` skip). Parts carry ``(part i of n)`` labels when
    split, and never an agent provenance marker: the drain appends that.
    """
    return plan_parts(
        text,
        home=resolve_home(os.environ),
        worktrees=redaction_anchors(
            client, git_toplevel(worktree), None, (str(worktree),)
        ),
        single=False,
    )
