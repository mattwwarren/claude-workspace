"""The Stop hook's ``agent_spawn_stamp`` snapshot and clear (#1947, #2229).

The mutate functions ``signal_stop`` hands to ``_write_cw_context_locked``: a
live snapshot of the hook payload's ``background_tasks`` count on a deferred
turn, the clear-to-zero on a drained one, and the resolved-shape check that
lets a drained turn skip the locked write. Imports ``_constants``. Split out
of the flat ``cli/stop_hook.py`` (#2496).

#2688 adds :func:`_summarize_background_tasks`: the deferral snapshot also
persists a *structural* summary of the ``background_tasks`` entries under
``agent_spawn_stamp.entries``, because nothing records what an idle named
teammate's entry looks like and the deferred reap rule (#2710) cannot be
written honestly without one. The summary is key names, value type names,
and short scalar values for an allow-list of structural keys; every other
value is dropped outright, never truncated, so no free text reaches disk.
``entries`` is diagnostic-only (no reader) and is written only by this hook,
never by spawn, so ``CW_CONTEXT_SCHEMA_VERSION`` is not bumped for it.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime

from cw.cli.stop_hook._constants import _LOGGER_NAME
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_SESSION_ID_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    extract_unresolved_spawn_count,
)

logger = logging.getLogger(_LOGGER_NAME)

# The diagnostic sub-key of ``agent_spawn_stamp`` the deferral snapshot writes
# (#2688). No reader: it exists for an operator, and for #2710's capture.
_ENTRIES_KEY = "entries"

# Bounds on the summary. Only the first _MAX_ENTRIES entries are summarised;
# the true list length travels separately as ``unresolved_count``.
_MAX_ENTRIES = 8
_MAX_KEYS_PER_ENTRY = 12
_MAX_KEY_NAME_CHARS = 48
_MAX_VALUE_CHARS = 64

# The allow-list: the only keys whose *values* are ever persisted, matched
# exactly (case-sensitive). ``type`` and ``status`` are the only structural
# keys observed so far (``{"type": "subagent", "status": "running", ...}``);
# the rest are conservative guesses. Free-text keys (``name``,
# ``description``, ``prompt``, ``message``, ``summary``, ``output``,
# ``result``) are deliberately absent. #2710 widens or narrows this set once a
# teammate entry is captured on a guard-off lane.
_ALLOWED_KIND_STATUS_KEYS = frozenset(
    {"type", "kind", "status", "state", "taskKind", "task_kind"}
)
_ALLOWED_ID_KEYS = frozenset({"id", "task_id", "taskId", "agent_id", "agentId"})
_ALLOWED_TIMESTAMP_KEYS = frozenset(
    {
        "started_at",
        "startedAt",
        "created_at",
        "createdAt",
        "updated_at",
        "updatedAt",
        "ended_at",
        "endedAt",
        "done_at",
        "doneAt",
        "timestamp",
        "ts",
    }
)
_ALLOWED_VALUE_KEYS = (
    _ALLOWED_KIND_STATUS_KEYS | _ALLOWED_ID_KEYS | _ALLOWED_TIMESTAMP_KEYS
)


def _recordable_scalar(value: object) -> bool:
    """True when *value* is a short str, a bool/int, or a finite float."""
    if isinstance(value, str):
        return len(value) <= _MAX_VALUE_CHARS
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, int)


def _summarize_entry(item: object) -> dict[str, object]:
    """Summarise one ``background_tasks`` entry as structure, never content.

    A non-dict item is just its type name. A dict yields its key names
    (str keys of 1.._MAX_KEY_NAME_CHARS chars; allow-listed keys are picked
    first so ``type``/``status`` survive the key cap, then stored sorted),
    each kept key's value type name, and the value itself only for an
    allow-listed key holding a :func:`_recordable_scalar`. ``extra_keys``
    counts the keys not recorded, and appears only when there are any.
    Nested containers are never descended into. Uses only ``isinstance``,
    ``len``, ``sorted`` and ``type(...).__name__``, so it cannot raise on
    any JSON-shaped input, which matters because a raise inside the locked
    write would silently drop the whole snapshot.
    """
    if not isinstance(item, dict):
        return {"entry_type": type(item).__name__}
    names = [
        key
        for key in item
        if isinstance(key, str) and 0 < len(key) <= _MAX_KEY_NAME_CHARS
    ]
    picked = sorted(names, key=lambda key: (key not in _ALLOWED_VALUE_KEYS, key))
    keys = sorted(picked[:_MAX_KEYS_PER_ENTRY])
    summary: dict[str, object] = {
        "entry_type": "dict",
        "keys": keys,
        "types": {key: type(item[key]).__name__ for key in keys},
        "values": {
            key: item[key]
            for key in keys
            if key in _ALLOWED_VALUE_KEYS and _recordable_scalar(item[key])
        },
    }
    if len(item) > len(keys):
        summary["extra_keys"] = len(item) - len(keys)
    return summary


def _summarize_background_tasks(raw: object) -> list[dict[str, object]]:
    """Return the structural summary of a Stop payload's ``background_tasks``.

    A non-list yields ``[]``. Only the first :data:`_MAX_ENTRIES` items are
    summarised, each by :func:`_summarize_entry`. Evaluated once in
    ``signal_stop``, outside the locked mutate function.
    """
    if not isinstance(raw, list):
        return []
    return [_summarize_entry(item) for item in raw[:_MAX_ENTRIES]]


def _snapshot_agent_spawn_stamp(
    context: dict[str, object],
    count: int,
    *,
    entries: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Overwrite ``agent_spawn_stamp`` with a live snapshot of *count*.

    #1947: replaces the removed ``PostToolUse:Agent`` decrement. Unlike
    ``agent_spawn_stamp._adjust_unresolved_count`` this is a *set*, not a
    delta -- the Stop hook payload's own ``background_tasks`` list is already
    the harness's authoritative live count for this turn, so there is nothing
    to accumulate against. ``last_stamped_at`` refreshes on every snapshot,
    even when the count is unchanged. At count > 0 it IS load-bearing: it bounds
    the #2012 distress-suppression deadline (``reconcile/liveness.py``,
    ``doctor/wedge/session_liveness.py``), which is why the deferral snapshot
    never skips a write. At count 0 nothing reads it.

    #2688: when *count* > 0 and *entries* (the
    :func:`_summarize_background_tasks` output) is non-empty, they are stored
    under ``entries``, and every count > 0 snapshot logs one INFO line with
    them. A count-0 call (the clear path) writes no ``entries`` and logs
    nothing, so a clear drops any stale summary. ``entries`` is a diagnostic
    sub-key written only after spawn, absent from every freshly spawned
    context and read by nothing, so the context schema version is unchanged.

    #2715: a count > 0 snapshot also copies the context's own top-level
    ``session_id`` (the cw session id the spawn wrote) into the stamp, when
    it is a non-empty string. It names the context owner when this snapshot
    was written, and only ``cw.reconcile.shipped_release`` reads it, to refuse
    to stop a session on a stamp another session left behind. The clear path
    writes none, and ``cw agent-spawn-pre`` rebuilds the stamp without it, so
    a later spawn drops it (the shipped release then holds). Diagnostic-sized
    like ``entries``, so the context schema version is unchanged.
    """
    stamp: dict[str, object] = {
        AGENT_SPAWN_UNRESOLVED_COUNT_KEY: count,
        AGENT_SPAWN_LAST_STAMPED_AT_KEY: datetime.now(UTC).isoformat(),
    }
    if count > 0:
        session_id = context.get(AGENT_SPAWN_SESSION_ID_KEY)
        if isinstance(session_id, str) and session_id:
            stamp[AGENT_SPAWN_SESSION_ID_KEY] = session_id
        if entries:
            stamp[_ENTRIES_KEY] = entries
        logger.info(
            "agent_spawn_stamp snapshot: unresolved_count=%d entries=%s",
            count,
            json.dumps(entries or [], sort_keys=True, default=str),
        )
    context[AGENT_SPAWN_STAMP_KEY] = stamp
    return context


def _clear_agent_spawn_stamp(context: dict[str, object]) -> dict[str, object]:
    """Zero ``agent_spawn_stamp`` -- the counterpart of
    :func:`_snapshot_agent_spawn_stamp`.

    Runs on every Stop whose ``background_tasks`` is empty/absent -- i.e. every
    turn that is NOT deferring for pending background work -- unless the stamp
    is already the resolved shape (:func:`_agent_spawn_stamp_is_clear`, #2229).
    This is what retires a snapshot written by a prior deferred turn once the
    harness's own accounting shows nothing outstanding -- see
    :func:`signal_stop`.

    #1947 review: logs when this actually retires a nonzero count -- this
    write otherwise vanishes silently (same fail-open contract as every
    other hook write here), but the field it mutates is the sole disk
    evidence gating BLOCKED_ON_USER vs a PENDING revert on the phantom
    sweep, so a transition worth an operator's attention deserves a trail.
    """
    prior_count = extract_unresolved_spawn_count(context)
    if prior_count > 0:
        logger.info(
            "agent_spawn_stamp cleared: unresolved_count %d -> 0 "
            "(background_tasks drained)",
            prior_count,
        )
    return _snapshot_agent_spawn_stamp(context, 0)


def _agent_spawn_stamp_is_clear(context: dict[str, object]) -> bool:
    """True only when the stamp is exactly the resolved shape: a dict whose
    ``unresolved_count`` is a non-bool ``int`` equal to 0 (#2229).

    Mirrors :func:`cw.models.extract_unresolved_spawn_count` but is stricter:
    that helper collapses every malformed shape (absent, non-dict, ``"0"``,
    ``False``, negative) to 0, whereas the clear write must still normalize
    those to ``{0, <now>}``. Pure ``isinstance`` logic, so it cannot raise.
    """
    stamp = context.get(AGENT_SPAWN_STAMP_KEY)
    if not isinstance(stamp, dict):
        return False
    count = stamp.get(AGENT_SPAWN_UNRESOLVED_COUNT_KEY)
    return isinstance(count, int) and not isinstance(count, bool) and count == 0
