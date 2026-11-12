"""Outcome record and operator-facing text for a refused-cycle restore (#2967, #3268).

:class:`CycleRestore` is what :func:`cw.codex_fix_loop.cycle_restore.preserve_and_reset`
returns. The functions here turn it into the park sentence and the rewritten
recovery hint. The text carries only ref names and path names, never diff text
or hook output. This module imports neither ``cycle_restore`` nor ``baseline``,
so the git-driving code can move without touching these strings.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach

MAX_NAMED_PATHS = 20


@dataclass(frozen=True)
class CycleRestore:
    """What :func:`preserve_and_reset` did for one refused cycle.

    ``restored`` means the edits are pinned under ``ref`` AND the worktree was
    reset AND it verifiably reads clean. ``why_not`` names why it did not
    complete. ``dirty_paths`` names the cycle's paths when the cycle touched
    anything.
    """

    restored: bool
    ref: str | None
    why_not: str | None
    head_short: str = ""
    dirty_paths: tuple[str, ...] = ()
    failed_step: str | None = None


def _restored_text(restore: CycleRestore) -> str:
    return (
        f"Saved the rejected changes to ref `{restore.ref}` and reset the "
        f"worktree to `{restore.head_short}`; a plain requeue starts clean. To "
        f"inspect or keep them: `git stash apply {restore.ref}`."
    )


def _dirty_clause(paths: tuple[str, ...]) -> str:
    if not paths:
        return ""
    shown = paths[:MAX_NAMED_PATHS]
    more = len(paths) - len(shown)
    names = ", ".join(f"`{rel}`" for rel in shown)
    suffix = f" (+{more} more)" if more else ""
    return f" Dirty paths: {names}{suffix}."


def _not_restored_text(restore: CycleRestore) -> str:
    clause = _dirty_clause(restore.dirty_paths)
    step = f" Failed step: {restore.failed_step}." if restore.failed_step else ""
    if restore.ref is not None:
        return (
            f"Saved the rejected changes to ref `{restore.ref}` but could not "
            f"reset the worktree: {restore.why_not}.{step}{clause} To inspect or keep "
            f"them: `git stash apply {restore.ref}`."
        )
    return (
        "cw neither saved nor discarded the rejected changes (no reset was "
        f"performed): {restore.why_not}.{step}{clause}"
    )


def failure_note(restore: CycleRestore | None) -> str | None:
    """Return the sentence a failure park appends to its headline, or ``None``."""
    if restore is None:
        return None
    return _restored_text(restore) if restore.restored else _not_restored_text(restore)


def apply_to_breach(breach: FenceBreach, restore: CycleRestore | None) -> FenceBreach:
    """Return *breach* with its recovery hint rewritten for what the restore did.

    A non-restorable breach and an empty cycle (``None``) come back untouched.
    After a verified restore :data:`LEFT_STAGED_HINT` (which may sit mid-string)
    is replaced; otherwise the old hint stays and one sentence names the reason.
    """
    if not breach.restorable or restore is None:
        return breach
    hint = breach.recovery_hint
    if restore.restored:
        text = _restored_text(restore)
        hint = (
            hint.replace(LEFT_STAGED_HINT, text)
            if LEFT_STAGED_HINT in hint
            else f"{hint} {text}"
        )
    else:
        hint = f"{hint} {_not_restored_text(restore)}"
    return dataclasses.replace(breach, recovery_hint=hint)
