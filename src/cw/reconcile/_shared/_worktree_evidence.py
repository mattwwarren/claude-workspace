"""Worktree evidence: unsaved work, headless marker and subagent-spawn stamp.

Reads what a session's worktree says about it -- unsaved work that must
route the task to the operator, the ``cw-context.json`` headless marker, and
the unresolved agent-spawn stamp the subagent-await checks key on. Imports
nothing from its siblings. Split out of the flat ``reconcile/_shared.py``
(#2214).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw.config import get_client
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
    extract_unresolved_spawn_count,
)
from cw.reconcile import _deps
from cw.worktree import unsaved_work_reason

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import Session


def _is_headless(session: Session) -> bool:
    """Return True if session's worktree has a headless cw-context.json.

    Fail-open: returns False when worktree_path is None, or when the context
    file is missing or unreadable — a deleted worktree must not be falsely
    flagged as headless. Mirrors cli.py signal_stop at line 1003-1005.
    """
    if session.worktree_path is None:
        return False
    context_path = session.worktree_path / ".claude" / "cw-context.json"
    try:
        context = json.loads(context_path.read_text())
        return bool(context.get("headless")) if isinstance(context, dict) else False
    except (OSError, json.JSONDecodeError):
        return False


def _worktree_dirty_reason_by_path(
    client_name: str, worktree_path: Path | None
) -> str | None:
    """Return why the worktree at *worktree_path* has unsaved work, or None.

    Uses worktree_path (always set on DAEMON sessions) instead of
    session.branch (always None on DAEMON sessions, making the branch-based
    check a production no-op). Fail-safe *direction*: a None/empty
    worktree_path, an unresolvable checked-out branch, or any other error all
    return None (not dirty) — the opposite direction from
    unsaved_work_reason's own inner fail-safe (which leans toward "has
    unsaved work" on a git-level error), preserved here unchanged since the
    three direct unit tests on this outer wrapper pin it.
    """
    if not worktree_path:
        return None
    try:
        branch = _deps.checked_out_branch(worktree_path)
        if not branch:
            return None
        client = get_client(client_name)
        return unsaved_work_reason(client, branch, wt_path=worktree_path)
    except Exception:  # noqa: BLE001 — fail-safe on any error (client lookup or git)
        return None


def _read_agent_spawn_stamp_context(
    worktree_path: Path | None,
) -> dict[str, Any] | None:
    """Return the parsed ``.claude/cw-context.json`` for *worktree_path*, or None.

    Shared read half of :func:`_read_unresolved_subagent_spawn` and
    :func:`_unresolved_subagent_spawn_age_seconds` (#2012) so the two readers
    of the same on-disk stamp cannot drift onto different fail-open rules.

    Fail-open (``None``) on a None path, a missing worktree, a missing or
    pre-v5 context, malformed JSON, a non-dict payload, or any other error.
    Both callers translate that ``None`` into their own "no evidence" answer.

    Reads the file directly rather than via ``cw.cli._hook_io``: reconcile must
    not import from ``cw.cli`` (the dependency runs the other way).
    """
    if not worktree_path:
        return None
    try:
        context_path = worktree_path / HOOK_CONTEXT_RELATIVE_PATH
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — fail-safe on any error; mirrors _worktree_dirty_reason_by_path
        return None
    return context if isinstance(context, dict) else None


def _unresolved_subagent_spawn_age_seconds(
    worktree_path: Path | None, now: datetime
) -> float | None:
    """Return how long *worktree_path*'s unresolved subagent spawn has run.

    Age sibling of :func:`_read_unresolved_subagent_spawn` (#2012), reading the
    ``last_stamped_at`` half of the same ``agent_spawn_stamp`` payload that
    ``cw agent-spawn-pre`` already writes — purely additive on the read side,
    no new writer. ``last_stamped_at`` advances only when the count *increases*
    (see :func:`cw.cli.agent_spawn_stamp._adjust_unresolved_count`), so it
    answers "when did the oldest outstanding spawn begin", which is exactly the
    quantity a deadline needs.

    Returns ``None`` — meaning "no bound available" — when there is no
    outstanding spawn, when the context is unreadable, or when
    ``last_stamped_at`` is missing or unparseable. Every one of those is the
    same fail-open direction as ``_read_unresolved_subagent_spawn``'s ``False``:
    the caller must treat "no bound" as "do not suppress", never as "suppress
    forever" — the unbounded-suppression bug this exists to close.

    A naive ``last_stamped_at`` is read as UTC (matching the writer, which
    stamps ``datetime.now(UTC).isoformat()``) so an old tz-less stamp cannot
    raise on the subtraction. The result may be negative if the stamp is in the
    future relative to *now*; callers compare against a positive deadline, for
    which a negative age correctly reads as "not yet exceeded".
    """
    context = _read_agent_spawn_stamp_context(worktree_path)
    if context is None or extract_unresolved_spawn_count(context) <= 0:
        return None
    stamp = context.get(AGENT_SPAWN_STAMP_KEY)
    raw = (
        stamp.get(AGENT_SPAWN_LAST_STAMPED_AT_KEY) if isinstance(stamp, dict) else None
    )
    if not isinstance(raw, str):
        return None
    try:
        stamped_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamped_at.tzinfo is None:
        stamped_at = stamped_at.replace(tzinfo=UTC)
    return (now - stamped_at).total_seconds()


def _read_unresolved_subagent_spawn(worktree_path: Path | None) -> bool:
    """Return True iff *worktree_path* carries an unresolved subagent spawn.

    Reads the ``agent_spawn_stamp`` counter the ``cw agent-spawn-pre`` /
    ``cw agent-spawn-post`` hook pair maintains in the worktree's
    ``.claude/cw-context.json`` (#1646). A count above zero means a subagent
    spawn started and its matching Post hook never fired — the worker died or
    hung mid-spawn.

    Fail-open in one direction only, mirroring ``_worktree_dirty_reason_by_path``:
    a None path, a missing worktree, a missing or pre-v5 context, malformed
    JSON, a non-dict payload, a non-dict stamp, a non-int count, or any other
    error all return False. Reporting an unresolved spawn on ambiguous evidence
    would park healthy tickets under a reason that also overrides
    ``reap_policy: auto`` — strictly worse than missing one crash's precision.

    Reads the file via :func:`_read_agent_spawn_stamp_context` rather than
    ``cw.cli._hook_io``: reconcile must not import from ``cw.cli`` (the
    dependency runs the other way). The shared path constant and the
    count-extraction logic both come from ``cw.models``
    (:func:`cw.models.extract_unresolved_spawn_count`) so this reader and
    ``cw.cli.agent_spawn_stamp``'s write-side reader cannot drift onto
    different literals or validation rules for the same on-disk shape (#1646
    review finding).

    Deliberately **age-blind**, and unchanged by #2012: its callers (the
    phantom sweep) ask "did this worker die mid-spawn", for which any
    outstanding stamp is evidence no matter how old. The age-bounded question
    belongs to :func:`_unresolved_subagent_spawn_age_seconds`.
    """
    context = _read_agent_spawn_stamp_context(worktree_path)
    if context is None:
        return False
    return extract_unresolved_spawn_count(context) > 0
