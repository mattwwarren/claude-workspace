"""Snapshot a refused fix cycle's edits, then return the worktree to its start (#2967).

A fix cycle that parks uncommitted (a guard breach, a commit-hook rejection, a
failed invocation) used to leave its edits staged in the worktree. The next
requeued cycle then met the clean-start refusal (``codex_fix_dirty_start``,
#2633) and the ``next:`` requeue could never make progress.

:func:`snapshot_and_restore` makes every non-committing exit recoverable and,
leave a clean tree:

1. It saves the cycle's edits as a binary patch under the cycle worktree's
   ``.cw/codex_fix_refused/`` (git-excluded, and alive exactly as long as the
   worktree the operator is told to inspect). The patch is written to a temp
   file, fsynced, read back, and only then linked exclusively to its final
   name, so a partial patch never exists under the final name.
2. Only then does it restore the cycle's touched paths to the
   cycle-start tree. Before every chunk is touched it re-checks that each path
   still holds the content the snapshot recorded, and re-reads HEAD. A changed
   path or a moved HEAD aborts the restore, keeps the patch, and leaves the
   worktree as it is. The restore is verified with :func:`capture_cycle_baseline`.

The order is the safety property: no durable snapshot means no restore, and a
restore that cannot be verified is reported as such. The function never raises;
every failure is a :class:`CycleRestore` with ``restored=False`` and a reason,
and the caller keeps the pre-#2967 "left staged" hint.

The restore is path-scoped (``git restore --source=<tree> --staged
--worktree``), never ``git reset --hard`` (which would revert a tracked,
modified cw review artifact) and never ``git clean`` (ignored files and
untracked review artifacts stay).

Only :mod:`cw.codex_fix_loop.commit` calls this module; :mod:`fence` and
:mod:`baseline` must not import it. Patch content, diff text and hook output
are never logged and never placed in park text, only the worktree-relative
patch path and the dirty path names.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from cw._git import git_output, run_git
from cw.codex_fix_loop.baseline import (
    CycleBaseline,
    capture_cycle_baseline,
    unstage_review_artifacts,
)
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach

if TYPE_CHECKING:
    from collections.abc import Iterator

_log = logging.getLogger(__name__)

# Worktree-relative home of the saved patches (`.cw/` is git-excluded in every
# cw worktree, `worktree/_lifecycle.py::_CW_EXCLUDE_PATTERNS`).
PATCH_DIR = PurePosixPath(".cw") / "codex_fix_refused"
# Paths handed to one `git restore` call, so a huge cycle cannot overrun argv.
RESTORE_CHUNK_SIZE = 200
# Distinct patch names tried (`-2`, `-3`, ...) before giving up on a collision.
MAX_PATCH_NAME_ATTEMPTS = 100
# Dirty paths named in park text before the rest are summarised as a count.
MAX_NAMED_PATHS = 20
_SHORT_SHA_LENGTH = 12
_PATCH_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%f"
_TEMP_SUFFIX = ".tmp"
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
_EMPTY_PATCH_WHY = "the cycle touched paths but its diff came out empty"
_FINAL_HEAD_READ_WHY = "could not verify HEAD after restore"
# Named reason for a HEAD move between the snapshot and the restore (#2967 review).
_HEAD_MOVED_RESTORE_TAG = "cycle_restore_head_moved"


@dataclass(frozen=True)
class CycleRestore:
    """What :func:`snapshot_and_restore` did for one refused cycle.

    ``restored`` means the patch is saved AND the worktree verifiably matches
    the cycle-start commit. ``patch`` is the worktree-relative patch path (set
    whenever a patch was saved, restored or not). ``why_not`` names why the
    restore did not complete. ``silent`` marks a skip that needs no extra park
    text (HEAD moved: a real commit exists, the old hint still applies).
    ``dirty_paths`` names the paths the cycle changed, set whenever the cycle
    touched anything and the restore did not complete.
    """

    restored: bool
    patch: str | None
    why_not: str | None
    head_short: str = ""
    silent: bool = False
    dirty_paths: tuple[str, ...] = ()


class _NotRestoredError(Exception):
    """An internal step refused to continue; carries the human reason."""

    def __init__(self, why: str) -> None:
        self.why = why
        super().__init__(why)


class _PublicationCleanupError(_NotRestoredError):
    """The published final name could not be durably removed after failure."""

    def __init__(self, patch: str, cleanup_error: OSError) -> None:
        why = (
            "could not durably remove the published patch after a publication "
            f"failure: {type(cleanup_error).__name__}: {cleanup_error}"
        )
        super().__init__(why)
        self.patch = patch


@dataclass(frozen=True)
class _Snapshot:
    """A patch written and verified on disk, plus what the restore must check."""

    patch: str
    paths: tuple[str, ...]
    digests: dict[str, str | None]


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


def _digest(worktree: Path, rel: str) -> str | None:
    """Content fingerprint of one worktree path; ``None`` when absent."""
    path = worktree / rel
    if path.is_symlink():
        return "link:" + str(path.readlink())
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return None


def _digests(worktree: Path, paths: tuple[str, ...]) -> dict[str, str | None]:
    return {rel: _digest(worktree, rel) for rel in paths}


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


def _write_durable(path: Path, data: bytes) -> None:
    """Create *path* exclusively, write and fsync it, and read it back."""
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if path.read_bytes() != data:
        raise _NotRestoredError(_READBACK_WHY)


def _fsync_directory(path: Path) -> None:
    """Make a directory-entry publication durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_patch(path: Path, data: bytes) -> None:
    """Raise when a published patch does not read back byte-identically."""
    if path.read_bytes() != data:
        raise _NotRestoredError(_READBACK_WHY)


def _write_exclusive(worktree: Path, cycle: int, reason: str, data: bytes) -> str:
    """Atomically publish a durable, collision-safe patch; return its path."""
    directory = worktree / PATCH_DIR
    directory.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, MAX_PATCH_NAME_ATTEMPTS + 1):
        rel = str(PATCH_DIR / _patch_name(cycle, reason, attempt))
        target = worktree / rel
        temp = directory / f".{target.name}.{uuid.uuid4().hex}{_TEMP_SUFFIX}"
        published_target: Path | None = None
        try:
            _write_durable(temp, data)
            try:
                os.link(temp, target)
            except FileExistsError:
                temp.unlink(missing_ok=True)
                continue
            published_target = target
            # The final name is linked only after the complete temp file has
            # been fsynced. If a later publication step fails, the final name
            # is removed by the exception handler below.
            _fsync_directory(directory)
            temp.unlink()
            _fsync_directory(directory)
            _verify_patch(target, data)
        except (
            _NotRestoredError,
            subprocess.CalledProcessError,
            OSError,
        ) as original_exc:
            publication_cleanup_error: OSError | None = None
            if published_target is not None:
                try:
                    published_target.unlink()
                except OSError as cleanup_exc:
                    publication_cleanup_error = cleanup_exc
                else:
                    try:
                        _fsync_directory(directory)
                    except OSError as cleanup_exc:
                        publication_cleanup_error = cleanup_exc
            temp_cleanup_error: OSError | None = None
            try:
                temp.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                temp_cleanup_error = cleanup_exc
            if published_target is not None and publication_cleanup_error is not None:
                raise _PublicationCleanupError(rel, publication_cleanup_error) from (
                    publication_cleanup_error
                )
            if temp_cleanup_error is not None:
                raise temp_cleanup_error from original_exc
            raise
        return rel
    raise _NotRestoredError(_NAME_EXHAUSTED_WHY)


def _save_patch(
    worktree: Path,
    baseline: CycleBaseline,
    paths: tuple[str, ...],
    *,
    cycle: int,
    reason: str,
) -> str:
    """Write the cycle's diff to a verified patch file, or raise."""
    data = _diff_bytes(worktree, baseline)
    if paths and not data:
        raise _NotRestoredError(_EMPTY_PATCH_WHY)
    # Guard on the directory's first path: the whole dir shares one ignore rule.
    _require_ignored(worktree, str(PATCH_DIR / "x.patch"))
    return _write_exclusive(worktree, cycle, reason, data)


def _head_moved_why(start: str, now: str) -> str:
    return (
        f"{_HEAD_MOVED_RESTORE_TAG}: HEAD moved from {start[:_SHORT_SHA_LENGTH]} "
        f"to {now[:_SHORT_SHA_LENGTH]} during the restore"
    )


def _assert_head(worktree: Path, baseline: CycleBaseline) -> None:
    """Raise unless HEAD still names the cycle-start commit."""
    now = _head_sha(worktree)
    if now != baseline.head_sha:
        raise _NotRestoredError(_head_moved_why(baseline.head_sha, now))


def _assert_content(worktree: Path, snapshot: _Snapshot) -> None:
    """Raise unless every snapshotted path still holds the snapshotted content."""
    for rel in snapshot.paths:
        if _digest(worktree, rel) != snapshot.digests.get(rel):
            why = f"`{rel}` changed after the snapshot"
            raise _NotRestoredError(why)


def _restore_paths(
    worktree: Path, baseline: CycleBaseline, snapshot: _Snapshot
) -> None:
    """Restore the snapshot's paths (index and worktree) to the baseline tree.

    Every chunk is checked against the snapshot immediately before it is
    touched, and HEAD is re-read before each chunk. Paths are restored in
    chunks.
    """
    for start in range(0, len(snapshot.paths), RESTORE_CHUNK_SIZE):
        _assert_head(worktree, baseline)
        chunk = snapshot.paths[start : start + RESTORE_CHUNK_SIZE]
        _assert_content(
            worktree,
            _Snapshot(
                snapshot.patch,
                chunk,
                {path: snapshot.digests[path] for path in chunk},
            ),
        )
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
    _restore_paths(worktree, baseline, snapshot)
    _assert_head(worktree, baseline)
    after = capture_cycle_baseline(worktree)
    short = baseline.head_sha[:_SHORT_SHA_LENGTH]
    if isinstance(after, CycleBaseline) and after.head_sha != baseline.head_sha:
        return CycleRestore(
            False,
            snapshot.patch,
            _head_moved_why(baseline.head_sha, after.head_sha),
            head_short=short,
            dirty_paths=snapshot.paths,
        )
    try:
        observed_head = _head_sha(worktree)
    except subprocess.CalledProcessError as exc:
        why = f"{_FINAL_HEAD_READ_WHY}: {_git_failure_why(exc)}"
        raise _NotRestoredError(why) from exc
    if observed_head != baseline.head_sha:
        return CycleRestore(
            False,
            snapshot.patch,
            _head_moved_why(baseline.head_sha, observed_head),
            head_short=short,
            dirty_paths=snapshot.paths,
        )
    if isinstance(after, CycleBaseline):
        return CycleRestore(True, snapshot.patch, None, head_short=short)
    return CycleRestore(
        False,
        snapshot.patch,
        _STILL_DIRTY_WHY,
        head_short=short,
        dirty_paths=snapshot.paths,
    )


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


def _failure_why(exc: Exception, *, saving: bool) -> str:
    if isinstance(exc, _NotRestoredError):
        return exc.why
    if isinstance(exc, subprocess.CalledProcessError):
        return _git_failure_why(exc)
    verb = "saving the patch" if saving else "while restoring"
    return f"{type(exc).__name__} {verb}"


def _attempt(
    worktree: Path, baseline: CycleBaseline, *, cycle: int, reason: str
) -> CycleRestore | None:
    """Run the snapshot-then-restore steps; a failure after listing names the paths."""
    paths: tuple[str, ...] = ()
    patch: str | None = None
    try:
        with _worktree_lock(worktree):
            if _head_sha(worktree) != baseline.head_sha:
                return CycleRestore(False, None, HEAD_MOVED_WHY, silent=True)
            paths = _stage_and_list(worktree, baseline)
            if not paths:
                return None
            digests = _digests(worktree, paths)
            patch = _save_patch(worktree, baseline, paths, cycle=cycle, reason=reason)
            return _restore_and_verify(
                worktree, baseline, _Snapshot(patch, paths, digests)
            )
    except _PublicationCleanupError as exc:
        return CycleRestore(
            False,
            exc.patch,
            _failure_why(exc, saving=False),
            head_short=baseline.head_sha[:_SHORT_SHA_LENGTH],
            dirty_paths=paths,
        )
    except (_NotRestoredError, subprocess.CalledProcessError, OSError) as exc:
        short = baseline.head_sha[:_SHORT_SHA_LENGTH] if patch is not None else ""
        return CycleRestore(
            False,
            patch,
            _failure_why(exc, saving=patch is None),
            head_short=short,
            dirty_paths=paths,
        )


@contextlib.contextmanager
def _worktree_lock(worktree: Path) -> Iterator[None]:
    """Serialize snapshot/restore operations for one worktree."""
    lock_path = Path(
        git_output(
            ["rev-parse", "--git-path", "codex-fix-restore.lock"], cwd=worktree
        ).strip()
    )
    if not lock_path.is_absolute():
        lock_path = worktree / lock_path
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
    if restore.patch is not None:
        return (
            f"Saved the rejected changes to `{restore.patch}` but could not "
            f"restore the worktree: {restore.why_not}.{clause}"
        )
    return (
        "cw neither saved nor discarded the rejected changes (no restore was "
        f"attempted): {restore.why_not}.{clause}"
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
