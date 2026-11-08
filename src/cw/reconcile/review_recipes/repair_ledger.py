"""Compare-and-set persistence of a proven clean-sha remap (#2921).

The daemon writes into a worktree it does not own, so a write is guarded by a
pre-image: the ledger on disk must still equal the exact ledger the fence read
before the remap is saved. The repo has no ledger lock protocol, so this is
best effort; the window is one re-read on a COMPLETED row, and the write is
atomic (:func:`cw.review_convergence.save_ledger`).

Called only after the post-push fence has passed, so a refused or failed
write leaves the PR unarmed and the ledger byte-identical.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.review_convergence import load_ledger, save_ledger

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_convergence import ConvergenceLedger


def persist_remap(
    path: Path, before: ConvergenceLedger, after: ConvergenceLedger
) -> str | None:
    """Save *after* over *before* at *path*; None on success, else why not.

    Re-loads the file and requires it to equal *before* first. A missing,
    unreadable, changed or unwritable ledger returns a reason and writes
    nothing (a failed write is atomic, so the prior file survives).
    """
    try:
        current = load_ledger(path)
    except (OSError, ValueError) as exc:
        return f"ledger {path} unreadable before the remap write ({type(exc).__name__})"
    if current is None:
        return f"ledger {path} vanished before the remap write"
    if current != before:
        return f"ledger {path} changed since the fence read it; remap not written"
    try:
        save_ledger(path, after)
    except OSError as exc:
        return f"ledger {path} write failed ({type(exc).__name__}: {exc})"
    return None
