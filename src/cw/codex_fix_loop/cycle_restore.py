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
4. While the worktree lock remains held, ``git reset --hard HEAD`` drops the
   cycle's uncommitted edits, resetting to the HEAD at that moment (not a
   recorded cycle-start SHA). Untracked and ignored paths (``.cw/``) are untouched.
5. One ``codex_fix.cycle_preserved`` event durably records the authorized reset
   intent before the destructive reset is attempted.

Nothing is reset unless the ref and the audit event both succeed. If the audit
event cannot be recorded, the ref is retained and the worktree stays dirty so
the park names the missing audit event. Once the reset is authorized, every
later failure (the reset, the rev-parse, or the clean re-check, as a
``CalledProcessError`` or an ``OSError``) is handled without raising and keeps
the preserved ref and dirty paths.

Only :mod:`cw.codex_fix_loop.commit` and :mod:`cw.codex_impl._run` call this
module; :mod:`fence` and :mod:`baseline` must not import it. The outcome record
and park text live in :mod:`restore_report`, re-exported here. The park text
carries the ref name and the dirty path names, never diff text or hook output.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cw._git import git_output
from cw._lock_guard import LockRank, lock_guard
from cw.codex_fix_loop.baseline import (
    CycleBaseline,
    best_effort_dirty_paths,
    capture_cycle_baseline,
    unstage_review_artifacts,
)
from cw.codex_fix_loop.restore_report import CycleRestore, apply_to_breach, failure_note
from cw.events import record_event
from cw.exceptions import CwError
from cw.models.enums import OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "REF_ROOT",
    "CycleRestore",
    "apply_to_breach",
    "failure_note",
    "preserve_and_reset",
]

_log = logging.getLogger(__name__)

REF_ROOT = "refs/cw/refused-cycles"
_SHORT_SHA_LENGTH = 12
_STAMP_FORMAT = "%Y%m%dT%H%M%S%f"
_UNSAFE_REF_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_CLEAN_WHY = "the worktree was still not clean after the reset"
_NO_STASH_WHY = "git stash create saved nothing for a non-empty diff"
_RESET_PENDING_WHY = "reset pending after ref pin"
_AUDIT_WHY = "could not record the refused-cycle audit event"
_STEP_COLLECT = "collect cycle paths"
_STEP_STASH = "git stash create"
_STEP_UPDATE_REF = "git update-ref"
_STEP_AUDIT = "record audit event"
_STEP_LOCK = "acquire restore lock"


@contextlib.contextmanager
def _worktree_lock(worktree: Path) -> Iterator[None]:
    """Serialize the complete preserve/audit/reset transaction.

    The OS flock stays held while the audit event is emitted. The guard is
    scoped only around acquisition because recording the event takes another
    leaf lock; the physical worktree reservation remains active until the
    context exits.
    """
    lock_path = Path(
        git_output(
            ["rev-parse", "--git-path", "codex-fix-restore.lock"], cwd=worktree
        ).strip()
    )
    if not lock_path.is_absolute():
        lock_path = worktree / lock_path
    handle = lock_path.open("a+")
    try:
        with lock_guard("codex_fix_restore", lock_path, LockRank.LEAF):
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


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


def _step_failed(exc: Exception, step: str, paths: tuple[str, ...]) -> CycleRestore:
    """A git failure before the ref exists: nothing pinned, nothing reset."""
    return CycleRestore(
        False, None, _failure_why(exc), dirty_paths=paths, failed_step=step
    )


def _reset_after_pin(
    worktree: Path, *, ref: str, paths: tuple[str, ...]
) -> CycleRestore:
    """Reset to HEAD and verify the tree is clean. Never raises once *ref* exists."""
    head = ""
    try:
        git_output(["reset", "--hard", "HEAD"], cwd=worktree)
        head = git_output(["rev-parse", "HEAD"], cwd=worktree).strip()
        if not isinstance(capture_cycle_baseline(worktree), CycleBaseline):
            return CycleRestore(
                False,
                ref,
                _CLEAN_WHY,
                head_short=head[:_SHORT_SHA_LENGTH],
                dirty_paths=paths,
            )
    except (subprocess.CalledProcessError, OSError) as exc:
        if not head:
            with contextlib.suppress(subprocess.CalledProcessError, OSError):
                head = git_output(["rev-parse", "HEAD"], cwd=worktree).strip()
        return CycleRestore(
            False,
            ref,
            _failure_why(exc),
            head_short=head[:_SHORT_SHA_LENGTH],
            dirty_paths=paths,
        )
    return CycleRestore(
        True, ref, None, head_short=head[:_SHORT_SHA_LENGTH], dirty_paths=paths
    )


def _record_preserved(
    worktree: Path,
    *,
    ticket: str,
    client: str,
    cycle: int,
    reason: str,
    result: CycleRestore,
) -> bool:
    """Record the ``codex_fix.cycle_preserved`` audit event before the reset runs."""
    try:
        head = git_output(["rev-parse", "HEAD"], cwd=worktree).strip()
        payload = {
            "ticket_id": ticket,
            "client": client,
            "cycle": cycle,
            "reason": reason,
            "ref": result.ref,
            "paths": list(result.dirty_paths),
            # The event is emitted before reset while the worktree lock is held;
            # it records the authorized reset intent, not an outcome known later.
            "reset_requested": True,
            "head": head[:_SHORT_SHA_LENGTH],
        }
        record_event(OrchestratorEventType.CODEX_FIX_CYCLE_PRESERVED, payload)
    except (
        subprocess.CalledProcessError,
        CwError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _log.warning("failed to record codex_fix.cycle_preserved event", exc_info=True)
        return False
    return True


def _attempt(
    worktree: Path,
    *,
    review_artifacts: frozenset[str],
    ticket: str,
    cycle: int,
    reason: str,
) -> CycleRestore | None:
    try:
        paths = _cycle_paths(worktree, review_artifacts)
    except (subprocess.CalledProcessError, OSError) as exc:
        return _step_failed(exc, _STEP_COLLECT, best_effort_dirty_paths(worktree))
    if not paths:
        return None
    try:
        sha = git_output(["stash", "create"], cwd=worktree).strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        return _step_failed(exc, _STEP_STASH, paths)
    if not sha:
        return CycleRestore(False, None, _NO_STASH_WHY, dirty_paths=paths)
    ref = _ref_name(ticket, cycle, reason)
    try:
        git_output(["update-ref", ref, sha], cwd=worktree)
    except (subprocess.CalledProcessError, OSError) as exc:
        # Nothing was pinned, so nothing is reset.
        return _step_failed(exc, _STEP_UPDATE_REF, paths)
    return CycleRestore(False, ref, _RESET_PENDING_WHY, dirty_paths=paths)


def _preserve_locked(
    worktree: Path,
    *,
    review_artifacts: frozenset[str],
    ticket: str,
    client: str,
    cycle: int,
    reason: str,
) -> CycleRestore | None:
    """Run the pin, audit and reset steps; the caller holds the worktree lock."""
    result = _attempt(
        worktree,
        review_artifacts=review_artifacts,
        ticket=ticket,
        cycle=cycle,
        reason=reason,
    )
    if result is None or result.ref is None:
        return result
    if not _record_preserved(
        worktree,
        ticket=ticket,
        client=client,
        cycle=cycle,
        reason=reason,
        result=result,
    ):
        _log.warning(
            "codex fix cycle %d (%s): refused-cycle restore skipped: %s",
            cycle,
            reason,
            _AUDIT_WHY,
        )
        return dataclasses.replace(result, why_not=_AUDIT_WHY, failed_step=_STEP_AUDIT)
    return _reset_after_pin(worktree, ref=result.ref, paths=result.dirty_paths)


def _log_outcome(cycle: int, reason: str, result: CycleRestore) -> None:
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
            result = _preserve_locked(
                worktree,
                review_artifacts=review_artifacts,
                ticket=ticket,
                client=client,
                cycle=cycle,
                reason=reason,
            )
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = ""
        if isinstance(exc, subprocess.CalledProcessError):
            stderr = (exc.stderr or "").strip()
        detail = f"{_failure_why(exc)}: {stderr}" if stderr else _failure_why(exc)
        _log.warning(
            "codex fix cycle %d (%s): refused-cycle restore skipped: %s",
            cycle,
            reason,
            detail,
        )
        return _step_failed(exc, _STEP_LOCK, best_effort_dirty_paths(worktree))
    if result is not None:
        _log_outcome(cycle, reason, result)
    return result
