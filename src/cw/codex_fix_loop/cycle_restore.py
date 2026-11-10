"""Preserve a refused fix cycle's edits in a git ref, then reset the worktree (#2967).

A fix cycle that parks uncommitted (a guard breach, a commit-hook rejection, a
failed invocation) used to leave its edits staged in the worktree. The next
requeued cycle then met the clean-start refusal (``codex_fix_dirty_start``,
#2633) and the ``next:`` requeue could never make progress.

:func:`preserve_and_reset` runs the git-native sequence the operator directed:

1. ``git add -A`` over the cycle's own paths (review artifacts stay unstaged).
2. ``git stash create`` writes a commit object holding the edits. It touches
   neither the shared stash reflog nor the worktree.
3. ``git update-ref refs/cw/refused-cycles/<ticket>/cycle-<N>-<reason>-<stamp>``
   pins that commit so the edits survive pruning and are named in the park.
4. One ``codex_fix.cycle_preserved`` event names the ref and the paths.
5. ``git reset --hard HEAD`` drops the cycle's uncommitted edits. It resets to
   the HEAD at that moment, not a recorded cycle-start SHA, so an orchestrator
   merge that moved HEAD is not reverted. The cycle-start tree is clean by
   construction (``codex_fix_dirty_start`` refuses otherwise), so nothing
   else is lost. Untracked and ignored paths (``.cw/``) are untouched. A
   tracked, modified review artifact is reverted in the worktree, but its
   content is part of the pinned commit, so it is recoverable from the ref.

Nothing is reset unless step 3 succeeded. Once the ref is written, every
later failure (the reset, the rev-parse, the clean re-check, as a
``CalledProcessError`` or an ``OSError``) returns a :class:`CycleRestore`
with ``restored=False`` that keeps the ref and the dirty paths, so the park
still names the recovery command. The function never raises.

Only :mod:`cw.codex_fix_loop.commit` calls this module; :mod:`fence` and
:mod:`baseline` must not import it. The park text carries the ref name and the
dirty path names, never diff text or hook output.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cw._git import git_output
from cw._lock_guard import LockRank, lock_guard
from cw.codex_fix_loop.baseline import (
    CycleBaseline,
    capture_cycle_baseline,
    unstage_review_artifacts,
)
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach
from cw.events import record_event
from cw.models.enums import OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Iterator

_log = logging.getLogger(__name__)

REF_ROOT = "refs/cw/refused-cycles"
MAX_NAMED_PATHS = 20
_SHORT_SHA_LENGTH = 12
_STAMP_FORMAT = "%Y%m%dT%H%M%S%f"
_UNSAFE_REF_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_CLEAN_WHY = "the worktree was still not clean after the reset"
_NO_STASH_WHY = "git stash create saved nothing for a non-empty diff"


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


@contextlib.contextmanager
def _worktree_lock(worktree: Path) -> Iterator[None]:
    """Serialize preserve/reset operations for one worktree."""
    lock_path = Path(
        git_output(
            ["rev-parse", "--git-path", "codex-fix-restore.lock"], cwd=worktree
        ).strip()
    )
    if not lock_path.is_absolute():
        lock_path = worktree / lock_path
    with (
        lock_guard("codex_fix_restore", lock_path, LockRank.LEAF),
        lock_path.open("a+") as handle,
    ):
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _cycle_paths(worktree: Path, review_artifacts: frozenset[str]) -> tuple[str, ...]:
    """Stage the cycle's edits, keep review artifacts out, return the staged paths."""
    git_output(["add", "-A"], cwd=worktree)
    unstage_review_artifacts(worktree, review_artifacts)
    out = git_output(["diff", "--cached", "--name-only", "--no-renames"], cwd=worktree)
    return tuple(sorted(line for line in out.splitlines() if line))


def _ref_name(ticket: str, cycle: int, reason: str) -> str:
    stamp = datetime.now(UTC).strftime(_STAMP_FORMAT)
    safe_ticket = _UNSAFE_REF_CHARS.sub("-", ticket) or "ticket"
    safe_reason = _UNSAFE_REF_CHARS.sub("-", reason)
    return f"{REF_ROOT}/{safe_ticket}/cycle-{cycle}-{safe_reason}-{stamp}"


def _failure_why(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return f"git exited {exc.returncode}"
    return type(exc).__name__


def _reset_after_pin(
    worktree: Path, *, ref: str, paths: tuple[str, ...]
) -> CycleRestore:
    """Reset to HEAD and verify the tree is clean. Never raises once *ref* exists."""
    try:
        git_output(["reset", "--hard", "HEAD"], cwd=worktree)
        head = git_output(["rev-parse", "HEAD"], cwd=worktree).strip()
        if not isinstance(capture_cycle_baseline(worktree), CycleBaseline):
            return CycleRestore(False, ref, _CLEAN_WHY, dirty_paths=paths)
    except (subprocess.CalledProcessError, OSError) as exc:
        return CycleRestore(False, ref, _failure_why(exc), dirty_paths=paths)
    return CycleRestore(
        True, ref, None, head_short=head[:_SHORT_SHA_LENGTH], dirty_paths=paths
    )


def _record_preserved(
    *, ticket: str, client: str, cycle: int, reason: str, result: CycleRestore
) -> None:
    """Record the one durable audit event for a pinned ref (#2967)."""
    payload = {
        "ticket_id": ticket,
        "client": client,
        "cycle": cycle,
        "reason": reason,
        "ref": result.ref,
        "paths": list(result.dirty_paths),
        "restored": result.restored,
        "head": result.head_short,
    }
    try:
        record_event(OrchestratorEventType.CODEX_FIX_CYCLE_PRESERVED, payload)
    except OSError:
        _log.warning("failed to record codex_fix.cycle_preserved event", exc_info=True)


def _attempt(
    worktree: Path,
    *,
    review_artifacts: frozenset[str],
    ticket: str,
    cycle: int,
    reason: str,
) -> CycleRestore | None:
    paths = _cycle_paths(worktree, review_artifacts)
    if not paths:
        return None
    sha = git_output(["stash", "create"], cwd=worktree).strip()
    if not sha:
        return CycleRestore(False, None, _NO_STASH_WHY, dirty_paths=paths)
    ref = _ref_name(ticket, cycle, reason)
    try:
        git_output(["update-ref", ref, sha], cwd=worktree)
    except subprocess.CalledProcessError as exc:
        # Nothing was pinned, so nothing is reset.
        return CycleRestore(False, None, _failure_why(exc), dirty_paths=paths)
    return _reset_after_pin(worktree, ref=ref, paths=paths)


def preserve_and_reset(
    worktree: Path,
    *,
    review_artifacts: frozenset[str],
    ticket: str,
    client: str,
    cycle: int,
    reason: str,
) -> CycleRestore | None:
    """Pin a refused cycle's edits under a ref, then reset the worktree to HEAD.

    Returns ``None`` when the cycle changed nothing. Never raises: a git
    failure before the ref is written becomes ``restored=False`` with no ref
    and no reset.
    """
    try:
        with _worktree_lock(worktree):
            result = _attempt(
                worktree,
                review_artifacts=review_artifacts,
                ticket=ticket,
                cycle=cycle,
                reason=reason,
            )
    except subprocess.CalledProcessError as exc:
        _log.warning(
            "codex fix cycle %d (%s): refused-cycle restore skipped: git exited %s: %s",
            cycle,
            reason,
            exc.returncode,
            (exc.stderr or "").strip() or type(exc).__name__,
        )
        return CycleRestore(False, None, f"git exited {exc.returncode}")
    except OSError as exc:
        _log.warning(
            "codex fix cycle %d (%s): refused-cycle restore skipped: %s",
            cycle,
            reason,
            type(exc).__name__,
        )
        return CycleRestore(False, None, type(exc).__name__)
    if result is None:
        return None
    # Recorded after the worktree lock is released: the event bus takes its own
    # leaf lock, and ADR-0019 forbids acquiring one while another leaf is held.
    if result.ref is not None:
        _record_preserved(
            ticket=ticket, client=client, cycle=cycle, reason=reason, result=result
        )
    if result.restored:
        _log.info(
            "codex fix cycle %d (%s): saved rejected changes to %s and reset "
            "worktree to %s",
            cycle,
            reason,
            result.ref,
            result.head_short,
        )
    else:
        _log.warning(
            "codex fix cycle %d (%s): refused-cycle restore incomplete: %s",
            cycle,
            reason,
            result.why_not,
        )
    return result


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
    if restore.ref is not None:
        return (
            f"Saved the rejected changes to ref `{restore.ref}` but could not "
            f"reset the worktree: {restore.why_not}.{clause} To inspect or keep "
            f"them: `git stash apply {restore.ref}`."
        )
    return (
        "cw neither saved nor discarded the rejected changes (no reset was "
        f"performed): {restore.why_not}.{clause}"
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
