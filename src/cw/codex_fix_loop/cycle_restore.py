"""Snapshot a refused fix cycle's edits, then return the worktree to its start (#2967).

A fix cycle that parks uncommitted (a guard breach, a commit-hook rejection, a
failed invocation) used to leave its edits staged in the worktree. The next
requeued cycle then met the clean-start refusal (``codex_fix_dirty_start``,
#2633) and the ``next:`` requeue could never make progress.

:func:`snapshot_and_restore` makes every non-committing exit leave a clean tree:

1. It saves the cycle's edits as a binary patch under the cycle worktree's
   ``.cw/codex_fix_refused/`` (git-excluded, and alive exactly as long as the
   worktree the operator is told to inspect), and reads the file back to check
   the bytes landed.
2. Only then does it restore the cycle's touched paths to the cycle-start
   tree, and it verifies the result with :func:`capture_cycle_baseline`.

The order is the safety property: no snapshot means no restore, and a restore
that cannot be verified is reported as such. The function never raises; every
failure is a :class:`CycleRestore` with ``restored=False`` and a reason, and
the caller keeps the pre-#2967 "left staged" hint.

The restore is path-scoped (``git restore --source=<tree> --staged
--worktree``), never ``git reset --hard`` (which would revert a tracked,
modified cw review artifact) and never ``git clean`` (ignored files and
untracked review artifacts stay).

Only :mod:`cw.codex_fix_loop.commit` calls this module; :mod:`fence` and
:mod:`baseline` must not import it. Patch content, diff text and hook output
are never logged and never placed in park text, only the worktree-relative
patch path.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from cw._git import git_output, run_git
from cw.codex_fix_loop.baseline import (
    CycleBaseline,
    capture_cycle_baseline,
    unstage_review_artifacts,
)
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger(__name__)

# Worktree-relative home of the saved patches (`.cw/` is git-excluded in every
# cw worktree, `worktree/_lifecycle.py::_CW_EXCLUDE_PATTERNS`).
PATCH_DIR = PurePosixPath(".cw") / "codex_fix_refused"
# Paths handed to one `git restore` call, so a huge cycle cannot overrun argv.
RESTORE_CHUNK_SIZE = 200
# Distinct patch names tried (`-2`, `-3`, ...) before giving up on a collision.
MAX_PATCH_NAME_ATTEMPTS = 100
_SHORT_SHA_LENGTH = 12
_PATCH_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%f"
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_DIFF_ARGV = (
    "diff",
    "--cached",
    "--binary",
    "--full-index",
    "--no-renames",
    "--no-color",
    "--no-ext-diff",
)
# `git check-ignore` exit codes: 0 ignored, 1 not ignored, else a git failure.
_IGNORED = 0
_NOT_IGNORED = 1

HEAD_MOVED_WHY = "HEAD moved since the cycle started"
_NOT_IGNORED_WHY = "the `.cw/` directory is not git-ignored in this worktree"
_STILL_DIRTY_WHY = "the worktree was still not clean after the restore"
_NAME_EXHAUSTED_WHY = "no unused patch file name was available"
_READBACK_WHY = "the patch file read back different from what was written"


@dataclass(frozen=True)
class CycleRestore:
    """What :func:`snapshot_and_restore` did for one refused cycle.

    ``restored`` means the patch is saved AND the worktree verifiably matches
    the cycle-start commit. ``patch`` is the worktree-relative patch path (set
    whenever a patch was saved, restored or not). ``why_not`` names why the
    restore did not complete. ``silent`` marks a skip that needs no extra park
    text (HEAD moved: a real commit exists, the old hint still applies).
    """

    restored: bool
    patch: str | None
    why_not: str | None
    head_short: str = ""
    silent: bool = False


class _NotRestoredError(Exception):
    """An internal step refused to continue; carries the human reason."""

    def __init__(self, why: str) -> None:
        self.why = why
        super().__init__(why)


@dataclass(frozen=True)
class _Snapshot:
    """A patch written and verified on disk, plus the paths it covers."""

    patch: str
    paths: tuple[str, ...]


def _git_failure_why(exc: subprocess.CalledProcessError) -> str:
    """Name the failed git subcommand and exit code, never its output."""
    cmd = [str(part) for part in exc.cmd] if isinstance(exc.cmd, list | tuple) else []
    sub = next((part for part in cmd[1:] if not part.startswith("-")), "command")
    return f"`git {sub}` failed (exit {exc.returncode})"


def _head_sha(worktree: Path) -> str:
    return git_output(["rev-parse", "HEAD"], cwd=worktree).strip()


def _stage_and_list(worktree: Path, baseline: CycleBaseline) -> tuple[str, ...]:
    """Stage the cycle's edits and return every path differing from the baseline."""
    git_output(["add", "-A"], cwd=worktree)
    unstage_review_artifacts(worktree, baseline.review_artifacts)
    result = run_git(
        ["diff", "--cached", "--name-only", "--no-renames", "-z", baseline.tree_sha],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=False,
    )
    # text=False yields bytes at runtime; run_git is annotated for the str case.
    raw = cast("bytes", result.stdout)
    return tuple(os.fsdecode(part) for part in raw.split(b"\0") if part)


def _diff_bytes(worktree: Path, baseline: CycleBaseline) -> bytes:
    """Return the staged binary diff against the baseline tree, as bytes."""
    result = run_git(
        [*_DIFF_ARGV, baseline.tree_sha],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=False,
    )
    # text=False yields bytes at runtime; run_git is annotated for the str case.
    return cast("bytes", result.stdout)


def _patch_name(cycle: int, reason: str, attempt: int) -> str:
    stamp = datetime.now(UTC).strftime(_PATCH_TIMESTAMP_FORMAT)
    safe_reason = _UNSAFE_NAME_CHARS.sub("_", reason)
    suffix = "" if attempt == 1 else f"-{attempt}"
    return f"cycle-{cycle}-{safe_reason}-{stamp}{suffix}.patch"


def _require_ignored(worktree: Path, rel: str) -> None:
    """Raise unless git ignores *rel*: writing it would recreate a dirty tree."""
    result = run_git(
        ["check-ignore", "-q", "--", rel], cwd=worktree, capture_output=True
    )
    if result.returncode == _IGNORED:
        return
    if result.returncode == _NOT_IGNORED:
        raise _NotRestoredError(_NOT_IGNORED_WHY)
    raise subprocess.CalledProcessError(
        result.returncode, ["git", "check-ignore"], stderr=result.stderr
    )


def _write_exclusive(worktree: Path, cycle: int, reason: str, data: bytes) -> str:
    """Create a patch file nobody else wrote, verify it, return its relative path."""
    directory = worktree / PATCH_DIR
    directory.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, MAX_PATCH_NAME_ATTEMPTS + 1):
        rel = str(PATCH_DIR / _patch_name(cycle, reason, attempt))
        target = worktree / rel
        try:
            handle = target.open("xb")
        except FileExistsError:
            continue
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if target.read_bytes() != data:
            target.unlink(missing_ok=True)
            raise _NotRestoredError(_READBACK_WHY)
        return rel
    raise _NotRestoredError(_NAME_EXHAUSTED_WHY)


def _save_patch(
    worktree: Path,
    baseline: CycleBaseline,
    paths: tuple[str, ...],
    *,
    cycle: int,
    reason: str,
) -> _Snapshot:
    """Write the cycle's diff to a verified patch file, or raise."""
    data = _diff_bytes(worktree, baseline)
    # Guard on the directory's first path: the whole dir shares one ignore rule.
    _require_ignored(worktree, str(PATCH_DIR / "x.patch"))
    return _Snapshot(_write_exclusive(worktree, cycle, reason, data), paths)


def _restore_paths(
    worktree: Path, baseline: CycleBaseline, paths: tuple[str, ...]
) -> None:
    """Restore *paths* (index and worktree) to the baseline tree, in chunks."""
    for start in range(0, len(paths), RESTORE_CHUNK_SIZE):
        chunk = paths[start : start + RESTORE_CHUNK_SIZE]
        git_output(
            [
                "--literal-pathspecs",
                "restore",
                f"--source={baseline.tree_sha}",
                "--staged",
                "--worktree",
                "--",
                *chunk,
            ],
            cwd=worktree,
        )


def _restore_and_verify(
    worktree: Path, baseline: CycleBaseline, snapshot: _Snapshot
) -> CycleRestore:
    """Restore the snapshot's paths, then prove the tree is the clean start."""
    _restore_paths(worktree, baseline, snapshot.paths)
    after = capture_cycle_baseline(worktree)
    short = baseline.head_sha[:_SHORT_SHA_LENGTH]
    if isinstance(after, CycleBaseline) and after.head_sha == baseline.head_sha:
        return CycleRestore(True, snapshot.patch, None, head_short=short)
    return CycleRestore(False, snapshot.patch, _STILL_DIRTY_WHY, head_short=short)


def _log_outcome(result: CycleRestore, *, cycle: int, reason: str) -> None:
    if result.restored:
        _log.info(
            "codex fix cycle %d (%s): saved rejected changes to %s and restored "
            "worktree to %s",
            cycle,
            reason,
            result.patch,
            result.head_short,
        )
        return
    _log.warning(
        "codex fix cycle %d (%s): refused-cycle restore skipped: %s",
        cycle,
        reason,
        result.why_not,
    )


def _attempt(
    worktree: Path, baseline: CycleBaseline, *, cycle: int, reason: str
) -> CycleRestore | None:
    """Run the snapshot-then-restore steps; raise only before a patch exists."""
    if _head_sha(worktree) != baseline.head_sha:
        return CycleRestore(False, None, HEAD_MOVED_WHY, silent=True)
    paths = _stage_and_list(worktree, baseline)
    if not paths:
        return None
    snapshot = _save_patch(worktree, baseline, paths, cycle=cycle, reason=reason)
    try:
        return _restore_and_verify(worktree, baseline, snapshot)
    except (subprocess.CalledProcessError, OSError) as exc:
        why = (
            _git_failure_why(exc)
            if isinstance(exc, subprocess.CalledProcessError)
            else f"{type(exc).__name__} while restoring"
        )
        short = baseline.head_sha[:_SHORT_SHA_LENGTH]
        return CycleRestore(False, snapshot.patch, why, head_short=short)


def snapshot_and_restore(
    worktree: Path, baseline: CycleBaseline, *, cycle: int, reason: str
) -> CycleRestore | None:
    """Save a refused cycle's edits as a patch, then restore the cycle-start tree.

    Returns ``None`` when the cycle changed nothing (no patch is written), else a
    :class:`CycleRestore`. Never raises: a git or filesystem failure becomes a
    ``restored=False`` result and nothing is restored unless the patch was
    saved and read back first.
    """
    try:
        result = _attempt(worktree, baseline, cycle=cycle, reason=reason)
    except _NotRestoredError as exc:
        result = CycleRestore(False, None, exc.why)
    except subprocess.CalledProcessError as exc:
        result = CycleRestore(False, None, _git_failure_why(exc))
    except OSError as exc:
        result = CycleRestore(False, None, f"{type(exc).__name__} saving the patch")
    if result is not None:
        _log_outcome(result, cycle=cycle, reason=reason)
    return result


def _restored_text(restore: CycleRestore) -> str:
    rel = restore.patch
    return (
        f"Saved the rejected changes to `{rel}` in the cycle worktree; restored "
        f"the worktree to cycle-start commit `{restore.head_short}`; a plain "
        f"requeue starts clean. To inspect or keep them: `git apply --stat {rel}`, "
        f"or from a clean tree at that commit `git apply --index {rel}`."
    )


def _not_restored_text(restore: CycleRestore) -> str:
    if restore.patch is not None:
        return (
            f"Saved the rejected changes to `{restore.patch}` but could not "
            f"restore the worktree: {restore.why_not}."
        )
    return (
        "cw neither saved nor discarded the rejected changes (no restore was "
        f"attempted): {restore.why_not}."
    )


def failure_note(restore: CycleRestore | None) -> str | None:
    """Return the sentence a failure park appends to its headline, or ``None``."""
    if restore is None or restore.silent:
        return None
    return _restored_text(restore) if restore.restored else _not_restored_text(restore)


def apply_to_breach(breach: FenceBreach, restore: CycleRestore | None) -> FenceBreach:
    """Return *breach* with its recovery hint rewritten for what the restore did.

    A non-restorable breach, an empty cycle (``None``) and a silent skip come
    back untouched. After a verified restore :data:`LEFT_STAGED_HINT` (which
    may sit mid-string) is replaced by the patch text; otherwise the old hint
    stays and one sentence naming the reason is appended.
    """
    if not breach.restorable or restore is None or restore.silent:
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
