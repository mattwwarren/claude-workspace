"""Headless sentinel parse, scope verification, reconstruction and harvest.

Finds the ``AUTO_DEV_RESULT`` sentinel a headless Stop resolves (the
transcript parse with its #799 ``worktree_path`` fallback, or the #536
reconstruction of an emitted ``last_result``), corrects its self-reported
scope against git (#1487), and pushes a freshly re-parsed one through the emit
door (#1457). Imports ``_constants``. Split out of the flat
``cli/stop_hook.py`` (#2496).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.auto_dev_result import AutoDevResult
from cw.cli._sentinels import _parse_sentinel_from_transcript
from cw.cli.stop_hook._constants import _LOGGER_NAME
from cw.exceptions import EmitSessionNotFoundError, EmitValidationError
from cw.models import LastResultSource
from cw.result import emit_result_locked, reconstruct_staged_sentinel
from cw.worktree import reconcile_result_scope, resolve_scope_guard_default_branch

if TYPE_CHECKING:
    from cw.auto_dev_result import BlockedResult
    from cw.models import Session

logger = logging.getLogger(_LOGGER_NAME)


def _parse_headless_sentinel(
    session: Session,
    cwd_value: str,
    claude_session_id: object,
    ticket_id_value: object = None,
) -> AutoDevResult | BlockedResult | None:
    """Parse the transcript sentinel for a headless Stop hook.

    Issue #799: when EnterWorktree shifts the hook cwd to a nested worktree,
    ``cwd_value`` derives the wrong Claude project dir. Retry with the session's
    recorded ``worktree_path`` — the directory whose project dir holds the actual
    transcript. Returns ``None`` when neither location yields a parseable sentinel.

    ``ticket_id_value`` is the Stop hook's own ``cw-context.json`` ticket id. It
    is forwarded to both scans as the expected identity (#2515) so the
    ``worktree_path`` fallback does not depend on that directory carrying its
    own ``cw-context.json`` -- the fallback scan fails closed without an identity.

    Extracted out of :func:`signal_stop` (rather than inlined) so the #536
    emit-precedence gate could be added there without pushing the function
    over its PLR0912 branch-count ceiling.
    """
    csid = claude_session_id if isinstance(claude_session_id, str) else None
    expected_ticket_id = (
        ticket_id_value
        if isinstance(ticket_id_value, str) and ticket_id_value
        else None
    )
    parsed = _parse_sentinel_from_transcript(
        cwd_value, csid, ticket_id=expected_ticket_id
    )
    # Rescan only a *different* directory: when the hook cwd already equals the
    # recorded worktree_path, the same transcript was just read (equal strings
    # encode to the same project dir), so a second pass repeats it byte for
    # byte. A per-call skip, not a cache -- nothing outlives this invocation.
    # A sentinel landing in the few ms between the two scans is caught by the
    # next Stop, exactly like one landing just after the second scan (deferring
    # is the fail-safe direction, ADR-0003).
    if (
        parsed is None
        and session.worktree_path is not None
        and str(session.worktree_path) != cwd_value
    ):
        parsed = _parse_sentinel_from_transcript(
            str(session.worktree_path), csid, ticket_id=expected_ticket_id
        )
    if isinstance(parsed, AutoDevResult):
        parsed = _verify_headless_scope(parsed, session)
    return parsed


def _verify_headless_scope(result: AutoDevResult, session: Session) -> AutoDevResult:
    """Correct a headless sentinel's self-reported scope against git facts (#1487).

    This is the last point before ``signal_stop`` writes ``last_result``, so a
    fabricated or stale-merge-base scope corrected here never reaches the queue.
    An unresolvable client falls back to ``main`` — the Stop hook must never
    raise, and losing the sentinel would cost far more than measuring against
    the wrong base.
    """
    default_branch = resolve_scope_guard_default_branch(
        session.client, log_context=f"session={session.id}"
    )
    return reconcile_result_scope(
        result,
        worktree_path=session.worktree_path,
        default_branch=default_branch,
    )


def _reconstruct_emitted_sentinel(
    session: Session,
) -> AutoDevResult | BlockedResult | None:
    """Reconstruct the authoritative sentinel from an emitted ``last_result``.

    ``_has_terminal_sentinel`` only confirms a ``"status"`` key is present —
    it does not guarantee the dict matches the schema (e.g. a stale/foreign
    shape). Returns ``None`` on a validation failure so the caller falls back
    to the transcript parse instead of raising out of the Stop hook, which must
    never block claude from exiting.

    #1762 redirected the body onto the shared
    ``cw.result.reconstruct_staged_sentinel`` and widened the return type from
    ``AutoDevResult`` alone to the full discriminated union the door itself
    validates against: a worker can die holding a parser-synthesized
    ``BlockedResult``, which the narrower check rejected — sending an
    already-authoritative emitted result back through a transcript re-parse
    that #536's emit precedence exists to skip.
    """
    reconstructed = reconstruct_staged_sentinel(session.last_result)
    if reconstructed is None:
        logger.warning(
            "session=%s emitted last_result failed sentinel validation, "
            "falling back to transcript parse",
            session.id,
        )
    return reconstructed


def _handle_headless_no_sentinel() -> bool:
    """Resolve a sentinel-less headless Stop hook: always defer.

    Historically this checked the resolved headless wall-clock budget and, on
    expiry, marked the session TIMED_OUT, reverted its task, and stopped the
    daemon — killing whatever the worker was mid-way through. That
    process-kill timeout is removed: a Stop hook with no sentinel simply
    defers, unconditionally. A later Stop hook can still land the sentinel; a
    genuinely dead worker is caught by the phantom sweep (roster absence —
    evidence, not a timer); a quiet-but-live worker surfaces to the operator
    via the liveness distress signal. Returns True — the caller must stop
    processing.
    """
    return True


def _harvest_last_result_through_door(
    session_id: str, sentinel: AutoDevResult | BlockedResult
) -> None:
    """Push a freshly re-parsed Stop-hook sentinel through the emit door.

    RFC 0012 A1 (#1457): the Stop-hook harvest write no longer assigns
    ``session.last_result`` directly -- it routes through
    ``emit_result_locked`` (the same first-writer-wins arbitration ``cw
    result emit`` uses, RFC 0012 S2, #1456) so a session that already has a
    terminal result recorded from another writer can't be silently
    clobbered by a late transcript re-parse.

    Best-effort: a validation failure, missing session, or state read/write
    failure is logged and swallowed, never raised -- the Stop hook must never
    block claude from exiting. There is no fallback write; a failure here just
    means ``last_result`` stays whatever it already was. A failed
    ``session.result_emitted`` audit append is not among these: the door
    already logs it and still persists the result (#2465), so it never reaches
    this handler. A refusal (terminal result already present) is not logged
    again here -- ``emit_result_locked`` already emits its own warning on
    refusal.
    """
    try:
        emit_result_locked(
            sentinel.model_dump(mode="json"),
            session_id,
            source=LastResultSource.STOP_HOOK_HARVEST,
        )
    except OSError as exc:
        logger.warning(
            "stop-hook harvest state read/write failed for session %s; "
            "allowing the Stop hook to exit: %s",
            session_id,
            exc,
        )
    except (EmitValidationError, EmitSessionNotFoundError) as exc:
        logger.warning(
            "stop-hook harvest write rejected by door for session %s: %s",
            session_id,
            exc,
        )
