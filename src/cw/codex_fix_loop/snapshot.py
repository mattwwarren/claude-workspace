"""Per-cycle ``ReviewVerdict`` snapshots for the codex fix loop (#1485, #1763).

Every cycle's full verdict (findings intact) is persisted under the diagnostics
bundle dir as it completes, stamped ``is_terminal_snapshot=False``; each true
exit path later re-writes exactly the one file its returned ``Blocker.details``
was rendered from with ``is_terminal_snapshot=True``. The pointer naming a
cycle's snapshot file is threaded into ``friction_highlights`` via
:func:`_with_snapshot_pointer`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from cw.executor_diagnostics import append_diagnostics_pointer, diagnostics_bundle_dir
from cw.review_findings import write_review_verdict

if TYPE_CHECKING:
    from cw.review_findings import ReviewVerdict

_log = logging.getLogger(__name__)


def _verdict_snapshot_filename(cycle: int) -> str:
    """Return the persisted-verdict filename for *cycle* (0 = the initial pass)."""
    return f"cycle{cycle}-review-verdict.json"


class _PersistedSnapshot(NamedTuple):
    """The latest persisted per-cycle verdict snapshot.

    Carries the ``friction_highlights`` *pointer* alongside the *cycle* index
    it was written for, so an exit path can re-open that exact file to stamp
    the terminal marker (#1763) instead of re-deriving the cycle from loop
    state that has already moved on.
    """

    pointer: str
    cycle: int


def _persist_cycle_snapshot(
    verdict: ReviewVerdict, *, session_id: str, cycle: int
) -> _PersistedSnapshot:
    """Persist *cycle*'s full verdict (findings intact) and return a pointer
    to it plus the cycle it was written for.

    The persisted copy is always stamped ``is_terminal_snapshot=False``
    explicitly rather than inheriting the model default: a fix-loop persist is
    by construction not final at the moment it is written, since the loop may
    still run another cycle. Terminality is stamped later, by
    :func:`_finalize_snapshot`, from the exit path that actually knows the
    disposition. The caller's in-memory *verdict* is never mutated.

    Mirrors ``persist_diagnostics_bundle``'s never-raise contract: a write
    failure is logged and swallowed rather than blocking the fix loop.
    """
    bundle = diagnostics_bundle_dir(session_id)
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        write_review_verdict(
            verdict.model_copy(update={"is_terminal_snapshot": False}),
            bundle / _verdict_snapshot_filename(cycle),
        )
    except OSError:
        _log.warning(
            "cycle-%d findings snapshot write failed for session %s",
            cycle,
            session_id,
        )
    pointer = append_diagnostics_pointer(
        f"cycle-{cycle} MUST_FIX findings snapshot persisted "
        f"({_verdict_snapshot_filename(cycle)})",
        session_id=session_id,
    )
    return _PersistedSnapshot(pointer=pointer, cycle=cycle)


def _finalize_snapshot(verdict: ReviewVerdict, *, session_id: str, cycle: int) -> None:
    """Re-persist *cycle*'s snapshot marked as this session's terminal one.

    Called from each true fix-loop exit path with the SAME verdict object that
    the returned ``Blocker.details``/``AutoDevResult`` is derived from — before
    any exit-path rewrite of ``verdict.review``, so the file on disk keeps
    agreeing with the persist that produced it and differs from its
    intermediate version in exactly one field.

    Same never-raise contract as :func:`_persist_cycle_snapshot`.
    """
    try:
        write_review_verdict(
            verdict.model_copy(update={"is_terminal_snapshot": True}),
            diagnostics_bundle_dir(session_id) / _verdict_snapshot_filename(cycle),
        )
    except OSError:
        _log.warning(
            "cycle-%d terminal findings snapshot write failed for session %s",
            cycle,
            session_id,
        )


def _with_snapshot_pointer(highlights: list[str], snapshot_pointer: str) -> list[str]:
    """Append *snapshot_pointer* to a copy of *highlights*."""
    return [*highlights, snapshot_pointer]
