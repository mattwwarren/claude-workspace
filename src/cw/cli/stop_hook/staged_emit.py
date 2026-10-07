"""Staged ``cw result emit`` bookkeeping for the Stop hook (#2458).

The lock-free ``staged_emit_result`` peek and its context-flag clear, the
partial-route outcome stamp/restore on ``session.last_result``, and the
``sentinel_unroutable`` page dedup flag. Both ``session.last_result`` merges
the result-door guard allowlists live here. Imports ``_constants``. Split out
of the flat ``cli/stop_hook.py`` (#2496).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw._hook_context import _write_cw_context_locked
from cw.cli.stop_hook._constants import (
    _SENTINEL_UNROUTABLE_PAGED_KEY,
    _STAGED_ROUTE_RESCUED_KEY,
    _STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY,
)
from cw.config import save_state
from cw.models import STAGED_EMIT_RESULT_KEY
from cw.reconcile import _stamp_sentinel_partial_route_consumed

if TYPE_CHECKING:
    from cw.models import CwState, Session


def _maybe_clear_staged_emit_result(
    has_staged_emit_source: bool, cwd_value: str
) -> None:
    """Clear the staged-emit-result context flag on a ``rescued=None`` bail.

    #2458 fix cycle 5, Action 1: shared by both bail branches in
    ``_resolve_and_complete_headless_session`` below -- left set, a later
    background_tasks-pending Stop re-peeks True and re-reaches the same bail
    every turn, re-firing that bail's event each time (a spurious
    ``SENTINEL_STAGE_MISMATCH`` re-derivation, or a repeat ``sentinel_
    unroutable`` page). Gated on *has_staged_emit_source* so the common
    ordinary Stop-hook turn for a session that never called ``cw result
    emit`` never pays a context-lock write for a no-op clear.
    """
    if has_staged_emit_source:
        _write_cw_context_locked(cwd_value, _clear_staged_emit_result_marker)


def _restore_staged_route_outcome(session: Session) -> tuple[bool, bool]:
    """Restore ``(rescued, task_already_terminal)`` stamped by a prior
    ``complete_session=False`` partial route (#2458 fix cycle 5, Action 2).

    The completing call's ``already_routed`` short-circuit skips
    ``_apply_sentinel_to_task`` entirely, so without this the eventual
    ``SESSION_COMPLETED`` payload would silently drop a #918 late-sentinel
    rescue it never re-derived.
    """
    staged_result = session.last_result
    if isinstance(staged_result, dict):
        return (
            bool(staged_result.get(_STAGED_ROUTE_RESCUED_KEY, False)),
            bool(staged_result.get(_STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY, False)),
        )
    return False, False


def _stamp_staged_route_outcome(
    state: CwState, session: Session, *, rescued: bool, task_already_terminal: bool
) -> None:
    """Merge the consumed flag and this call's routing outcome, then persist.

    #2458 fix cycle 5, Actions 2 and 3: the consumed flag (round 4) lets a
    later completing call skip re-deriving an already-routed sentinel; the
    outcome alongside it (this cycle) lets that same later call restore
    ``rescued``/``task_already_terminal`` via
    :func:`_restore_staged_route_outcome` instead of leaving them at their
    init-False defaults. Caller guards this with ``not already_routed`` so a
    repeat partial-route Stop skips the redundant merge and fleet-wide
    ``save_state``.
    """
    _stamp_sentinel_partial_route_consumed(session)
    existing = session.last_result
    if isinstance(existing, dict):
        session.last_result = {
            **existing,
            _STAGED_ROUTE_RESCUED_KEY: rescued,
            _STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY: task_already_terminal,
        }
    save_state(state)


def _sentinel_unroutable_already_paged(session: Session) -> bool:
    """True when a prior bail already paged ``sentinel_unroutable`` (#2458 fix cycle 6).

    Reads ``_SENTINEL_UNROUTABLE_PAGED_KEY``. Computed *before* this call's own
    bail decides whether to stamp the flag -- see
    :func:`_maybe_stamp_sentinel_unroutable_paged` -- so a fresh
    ``_HeadlessResolution`` field can carry the pre-stamp state through to
    ``signal_stop``'s outside-the-lock ``_sentinel_unroutable`` check without
    that check racing its own just-written stamp.
    """
    last_result = session.last_result
    return (
        isinstance(last_result, dict)
        and last_result.get(_SENTINEL_UNROUTABLE_PAGED_KEY) is True
    )


def _maybe_stamp_sentinel_unroutable_paged(
    state: CwState, session: Session, *, already_paged: bool, pageable: bool
) -> None:
    """Merge ``_SENTINEL_UNROUTABLE_PAGED_KEY`` in once, the first time this
    bail is pageable (#2458 fix cycle 6).

    No-op when already paged (nothing to add) or not pageable (the #2135 park
    ran, or nothing is actually staged -- ``holds_staged_emit_result`` is
    False -- in which case ``_sentinel_unroutable`` will refuse to page on its
    own conditions regardless, so stamping here would be a needless
    fleet-wide ``save_state`` for a page that was never going to fire).
    """
    if already_paged or not pageable:
        return
    existing = session.last_result
    if isinstance(existing, dict):
        session.last_result = {**existing, _SENTINEL_UNROUTABLE_PAGED_KEY: True}
        save_state(state)


def _peek_staged_emit_result(context: dict[str, object]) -> bool:
    """Lock-free peek: does this session hold a staged emit_cli result? (#2458)

    Reads the ``staged_emit_result`` flag ``cw result emit`` stamps into this
    worktree's own ``cw-context.json`` (#2458) -- never ``load_state()``,
    whose fleet-wide ``sessions.json`` this peek exists specifically to avoid
    loading on every Stop-hook fire with pending ``background_tasks``. A
    stale answer cannot corrupt anything: True just means the caller takes
    the lock and re-makes every decision from a freshly loaded session; False
    means it defers exactly as it did before #2458, which a later Stop or the
    idle sweep recovers from.
    """
    if not context.get("headless"):
        return False
    return bool(context.get(STAGED_EMIT_RESULT_KEY))


def _clear_staged_emit_result_marker(context: dict[str, object]) -> dict[str, object]:
    """Drop ``STAGED_EMIT_RESULT_KEY`` -- the counterpart of the stamp
    ``cw.result._stamp_staged_emit_result`` writes.

    Called once a ``complete_session=False`` partial route has consumed the
    staged result (see ``_resolve_and_complete_headless_session``), so a
    later ``_peek_staged_emit_result`` on the same still-deferred session
    answers False instead of re-triggering a re-derivation of an
    already-routed sentinel.
    """
    return {k: v for k, v in context.items() if k != STAGED_EMIT_RESULT_KEY}
