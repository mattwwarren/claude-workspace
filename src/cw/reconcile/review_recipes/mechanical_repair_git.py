"""Merge, command and verify steps of the repair_mechanical_conflict recipe (#2917).

Post-lock only: every function here runs a subprocess, so none may run under
``sessions_lock`` or ``dev_queue_lock``.

- Every git call goes through ``repair_git._git`` (bounded, ``OSError`` /
  timeout turned into :class:`~cw.reconcile.review_recipes.repair_git.RepairStopError`).
  The merge and the commit are mutating calls under the
  ``cw-mechanical-repair`` identity. No git argv here uses ``worktree
  remove`` / ``prune``, ``reset``, ``clean``, ``restore``, ``checkout`` or
  ``stash``: the scratch tree is discarded by ``repair_git._remove_scratch``,
  so an unfinished merge needs no ``merge --abort``.
- Scope is decided by ONE matcher, :func:`path_in_scope` over
  ``PurePosixPath.full_match``, for the conflict set, the in-merge unmerged
  paths, the staging classification and the post-repair verify. Git
  pathspecs are only ever given explicit paths (``--literal-pathspecs``), so
  git's glob semantics can never disagree with it.
- :func:`run_declared_command` is the one seam that runs an operator command:
  an argv list (no shell), its own process group, stdin closed, merged
  output captured. A timeout kills the whole group (a grandchild holding the
  pipe would otherwise hang the post-lock drain). That is a bounded-call
  deadline over the recipe's own un-pushed scratch child: it ends in
  ``repair_failed`` / ``check_failed`` and touches no queue row, session or
  ticket worktree.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

from cw.reconcile.review_recipes.repair_git import (
    _T_LOCAL,
    _T_REBASE,
    _TAIL_CHARS,
    RepairStopError,
    _diff_fingerprint,
    _failed,
    _git,
    _range_diff_stat,
    _split_z,
    _tail,
    _unmerged_paths,
)
from cw.repair_disposition import RepairDisposition

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from pathlib import Path

_logger = logging.getLogger("cw.reconcile.review_recipes")

MECHANICAL_IDENTITY = "cw-mechanical-repair"
STEP_REPAIR = "repair"
STEP_CHECK = "check"
_COMMAND_NAMES = {STEP_REPAIR: "repair command", STEP_CHECK: "verify command"}
_STEP_DISPOSITIONS = {
    STEP_REPAIR: RepairDisposition.REPAIR_FAILED,
    STEP_CHECK: RepairDisposition.CHECK_FAILED,
}

# ``git merge`` exits 1 when it stopped on conflicts.
_MERGE_CONFLICTED = 1
# ``git merge-base --is-ancestor`` exits 1 for "not an ancestor"; any other
# non-zero exit is an error.
_NOT_AN_ANCESTOR = 1
_MARKER_PREFIXES = ("<<<<<<< ", ">>>>>>> ")
_ERROR_PATH_SAMPLE = 20
_LOG_PATH_SAMPLE = 10
# ``git status --porcelain=v1 -z`` entries are ``XY <path>``.
_STATUS_CODE_END = 2
_STATUS_PATH_START = 3
_STATUS_UNTRACKED = "??"
_STATUS_CLEAN = " "
_STATUS_UNMERGED = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})
# Bound on the output drain after the group kill (a descendant that escaped
# the group and holds the pipe open must not hang the serial post-lock drain).
_KILL_DRAIN_SECONDS = 5.0


class CommandOutcome(NamedTuple):
    """A declared command's exit (None when it timed out) and its output tail."""

    returncode: int | None
    tail: str
    timed_out: bool = False


def _decode_tail(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")[-_TAIL_CHARS:]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the command's whole process group; it may already be gone."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def _drain_after_kill(proc: subprocess.Popen[bytes]) -> CommandOutcome:
    """The timed-out command's output, drained for at most ``_KILL_DRAIN_SECONDS``.

    The group kill cannot reach a descendant that left the group (a double
    fork plus ``setsid``, a daemonizing tool) and kept the inherited stdout
    open, so an unbounded drain could block the serial post-lock drain
    forever. On a second timeout the pipe is closed WITHOUT reading it and
    whatever was already captured is the tail.
    """
    try:
        output, _ = proc.communicate(timeout=_KILL_DRAIN_SECONDS)
    except subprocess.TimeoutExpired as exc:
        if proc.stdout is not None:
            with contextlib.suppress(OSError, ValueError):
                proc.stdout.close()
        return CommandOutcome(None, _decode_tail(exc.output), timed_out=True)
    return CommandOutcome(None, _decode_tail(output), timed_out=True)


def run_declared_command(
    argv: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> CommandOutcome:
    """Run *argv* (no shell) in *cwd*, bounded by *timeout* seconds.

    The command leads its own session, so its whole process group is killed on
    every exit path: a timeout (before the output is drained, itself bounded,
    see :func:`_drain_after_kill`), an exception or interrupt while waiting
    (re-raised), and a normal exit that left background children behind.
    Raises ``OSError`` when the command cannot be launched at all.
    """
    with subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    ) as proc:
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            return _drain_after_kill(proc)
        except BaseException:
            # Why: the command leads its own session, so a SIGINT of the loop
            # never reaches it; without this kill it (and its children) would
            # outlive the scratch tree removed by the caller's ``finally``.
            _kill_group(proc)
            raise
        # A clean exit can still leave background children in the group.
        _kill_group(proc)
    return CommandOutcome(proc.returncode, _decode_tail(output))


def resolve_argv(argv: Sequence[str], repo: Path) -> list[str]:
    """*argv* with a relative ``argv[0]`` containing ``/`` anchored at *repo*.

    The client workspace, never the scratch tree, supplies the command, so a
    branch cannot swap the script it is repaired and validated by. Absolute
    paths and bare ``PATH`` names are left as they are.
    """
    first, *rest = argv
    if "/" in first and not PurePosixPath(first).is_absolute():
        first = str(repo / first)
    return [first, *rest]


def path_in_scope(globs: Sequence[str]) -> Callable[[str], bool]:
    """The recipe's one scope matcher: anchored, repo-relative globs.

    ``**`` crosses directories and ``*`` does not
    (``PurePosixPath.full_match``).
    """
    patterns = tuple(globs)

    def _matches(path: str) -> bool:
        pure = PurePosixPath(path)
        return any(pure.full_match(pattern) for pattern in patterns)

    return _matches


def sample_paths(paths: Collection[str], limit: int = _ERROR_PATH_SAMPLE) -> str:
    """The sorted *paths*, the first *limit* of them, then ``(+N more)``."""
    ordered = sorted(paths)
    if not ordered:
        return "(none)"
    shown = ", ".join(ordered[:limit])
    extra = len(ordered) - limit
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _command_failed(
    name: str, argv0: str, outcome: CommandOutcome, timeout: int
) -> str:
    """The ``error`` text of a failed declared command (argv0 only, never argv)."""
    if outcome.timed_out:
        return f"{name} {argv0} timed out after {timeout}s: {_tail(outcome.tail)}"
    return f"{name} {argv0} exit {outcome.returncode}: {_tail(outcome.tail)}"


def _log_finished(
    client: str, ticket: str, kind: str, argv0: str, outcome: CommandOutcome | None
) -> None:
    """Log one declared command's end; argv0 only, never the argv or env."""
    _logger.info(
        "mechanical_repair_command_finished client=%s ticket=%s step=%s argv0=%s"
        " returncode=%s timed_out=%s",
        client,
        ticket,
        kind,
        argv0,
        None if outcome is None else outcome.returncode,
        outcome is not None and outcome.timed_out,
    )


def _run_step(
    kind: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    client: str,
    ticket: str,
) -> None:
    """Run one declared command; raise its step's disposition unless it passed."""
    name = _COMMAND_NAMES[kind]
    disposition = _STEP_DISPOSITIONS[kind]
    argv0 = argv[0]
    try:
        outcome = run_declared_command(argv, cwd=cwd, env=env, timeout=timeout)
    except OSError as exc:
        _log_finished(client, ticket, kind, argv0, None)
        detail = f"{name} {argv0} could not run: {exc}"
        raise RepairStopError(disposition, detail) from exc
    _log_finished(client, ticket, kind, argv0, outcome)
    if outcome.timed_out or outcome.returncode != 0:
        raise RepairStopError(
            disposition, _command_failed(name, argv0, outcome, timeout)
        )


def run_repair(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    client: str,
    ticket: str,
) -> None:
    """The declared repair command, mid-merge in the scratch tree."""
    _run_step(
        STEP_REPAIR,
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        client=client,
        ticket=ticket,
    )


def run_check(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    client: str,
    ticket: str,
) -> None:
    """The declared verify command, at the committed merge in the scratch tree."""
    _run_step(
        STEP_CHECK,
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        client=client,
        ticket=ticket,
    )


def _start_merge(
    scratch: Path, default_ref: str, in_scope: Callable[[str], bool]
) -> frozenset[str]:
    """Merge *default_ref* into the scratch HEAD, stopping on its conflicts.

    Returns the unmerged paths, every one of which must be in scope (a
    defence in depth over the pre-merge conflict set): a git-visible conflict
    outside the globs, CHANGELOG.md included, parks ``out_of_scope_conflict``.
    """
    stop = RepairDisposition.MERGE_FAILED
    merged = _git(
        scratch,
        "merge",
        "--no-commit",
        "--no-ff",
        default_ref,
        timeout=_T_REBASE,
        stop=stop,
        mutating=True,
        identity=MECHANICAL_IDENTITY,
    )
    if merged.returncode == 0:
        raise RepairStopError(
            RepairDisposition.CONFLICT_UNEXPLAINED,
            f"git merge of {default_ref} applied cleanly; no conflict to repair",
        )
    if merged.returncode != _MERGE_CONFLICTED:
        raise RepairStopError(stop, _failed(merged, "git merge"))
    unmerged = _unmerged_paths(scratch, stop=stop)
    if not unmerged:
        raise RepairStopError(stop, _failed(merged, "git merge"))
    if not all(in_scope(path) for path in unmerged):
        outside = [path for path in unmerged if not in_scope(path)]
        raise RepairStopError(
            RepairDisposition.OUT_OF_SCOPE_CONFLICT,
            "the merge left unmerged path(s) outside the declared globs:"
            f" {sample_paths(outside)}",
        )
    return unmerged


class _StatusSplit(NamedTuple):
    stage: list[str]
    outside: list[str]
    ignored: list[str]


def _classify_status(
    entries: list[str], in_scope: Callable[[str], bool]
) -> _StatusSplit:
    """Sort ``git status`` entries into stage / out-of-scope / ignored.

    Untracked files are staged when in scope and ignored otherwise. A tracked
    path with a worktree change, or still unmerged, is staged when in scope
    and out of scope otherwise. An index-only change is the merge's own work.
    """
    split = _StatusSplit([], [], [])
    for entry in entries:
        code = entry[:_STATUS_CODE_END]
        path = entry[_STATUS_PATH_START:]
        if code == _STATUS_UNTRACKED:
            (split.stage if in_scope(path) else split.ignored).append(path)
        elif code in _STATUS_UNMERGED or code[1] != _STATUS_CLEAN:
            (split.stage if in_scope(path) else split.outside).append(path)
    return split


def _stage_scoped(
    scratch: Path, in_scope: Callable[[str], bool], *, client: str, ticket: str
) -> None:
    """Stage the repair command's in-scope changes by explicit path.

    A tracked file it changed outside the globs parks ``repair_out_of_scope``;
    an untracked one is never staged, only logged.
    """
    stop = RepairDisposition.MERGE_FAILED
    status = _git(
        scratch,
        "status",
        "--porcelain=v1",
        "-z",
        "--no-renames",
        "--untracked-files=all",
        timeout=_T_LOCAL,
        stop=stop,
    )
    if status.returncode != 0:
        raise RepairStopError(stop, _failed(status, "git status"))
    split = _classify_status(_split_z(status.stdout), in_scope)
    if split.outside:
        raise RepairStopError(
            RepairDisposition.REPAIR_OUT_OF_SCOPE,
            "the repair command changed tracked path(s) outside the declared"
            f" globs: {sample_paths(split.outside)}",
        )
    if split.ignored:
        _logger.info(
            "mechanical_repair_untracked_ignored client=%s ticket=%s count=%d"
            " sample=%s",
            client,
            ticket,
            len(split.ignored),
            ",".join(sorted(split.ignored)[:_LOG_PATH_SAMPLE]),
        )
    if not split.stage:
        return
    added = _git(
        scratch,
        "--literal-pathspecs",
        "add",
        "-A",
        "--",
        *sorted(set(split.stage)),
        timeout=_T_LOCAL,
        stop=stop,
    )
    if added.returncode != 0:
        raise RepairStopError(stop, _failed(added, "git add"))


def _has_markers(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_bytes().decode("utf-8", errors="replace")
    return any(line.startswith(_MARKER_PREFIXES) for line in text.splitlines())


def _scan_markers(scratch: Path, paths: Collection[str]) -> None:
    """Park ``repair_incomplete`` if a formerly conflicted path keeps markers."""
    left = [path for path in sorted(paths) if _has_markers(scratch / path)]
    if left:
        raise RepairStopError(
            RepairDisposition.REPAIR_INCOMPLETE,
            f"conflict markers remain in: {sample_paths(left)}",
        )


def _commit_merge(scratch: Path, message: str) -> None:
    """Commit the repaired merge (no hooks, the mechanical identity)."""
    stop = RepairDisposition.MERGE_FAILED
    committed = _git(
        scratch,
        "commit",
        "--no-verify",
        "-m",
        message,
        timeout=_T_LOCAL,
        stop=stop,
        mutating=True,
        identity=MECHANICAL_IDENTITY,
    )
    if committed.returncode != 0:
        raise RepairStopError(stop, _failed(committed, "git commit"))


def _changed_names_z(scratch: Path, since: str, until: str) -> list[str]:
    stop = RepairDisposition.VERIFY_FAILED
    listed = _git(
        scratch,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        since,
        until,
        timeout=_T_LOCAL,
        stop=stop,
    )
    if listed.returncode != 0:
        raise RepairStopError(stop, _failed(listed, "git diff --name-only"))
    return _split_z(listed.stdout)


def _scoped_diff(scratch: Path, since: str, until: str, paths: list[str]) -> str:
    stop = RepairDisposition.VERIFY_FAILED
    diffed = _git(
        scratch,
        "--literal-pathspecs",
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        since,
        until,
        "--",
        *paths,
        timeout=_T_LOCAL,
        stop=stop,
    )
    if diffed.returncode != 0:
        raise RepairStopError(stop, _failed(diffed, "git diff"))
    return diffed.stdout


def verify_descends_from(
    scratch: Path, new_sha: str, *, expected_sha: str, default_ref: str
) -> None:
    """The repaired head descends from the reviewed tip AND the default branch.

    Why: the repair command runs with the scratch tree as its cwd and can move
    HEAD (``reset --soft``, ``checkout``, ``commit --amend``) before the merge
    commit. The recipe then force-pushes *new_sha* with a lease, and the
    path-level out-of-glob fingerprint alone would not notice replaced
    history. The fence's ancestry check runs only after the push, so this is
    the check that can still park before anything is pushed.
    """
    for name, ancestor in (
        ("the reviewed tip", expected_sha),
        ("the default branch", default_ref),
    ):
        checked = _git(
            scratch,
            "merge-base",
            "--is-ancestor",
            ancestor,
            new_sha,
            timeout=_T_LOCAL,
            stop=RepairDisposition.MERGE_FAILED,
        )
        if checked.returncode == _NOT_AN_ANCESTOR:
            detail = (
                f"the repaired head {new_sha} does not descend from {name}"
                f" ({ancestor}): the repair command rewrote history"
            )
            raise RepairStopError(RepairDisposition.VERIFY_FAILED, detail)
        if checked.returncode != 0:
            raise RepairStopError(
                RepairDisposition.MERGE_FAILED,
                _failed(checked, "git merge-base --is-ancestor"),
            )


def verify_outside_scope_identical(
    scratch: Path,
    in_scope: Callable[[str], bool],
    merge_base: str,
    expected_sha: str,
    default_ref: str,
    new_sha: str,
    *,
    client: str,
    ticket: str,
) -> None:
    """Only in-scope paths changed: the branch's out-of-glob diff is unchanged.

    The out-of-glob paths of ``merge_base..expected_sha`` (what the branch
    changed) and of ``default_ref..new_sha`` (what the repaired branch
    changes against main) must be the same set, and their ``-U0`` diffs must
    have the same line-number-independent fingerprint.
    """
    before = sorted(
        p
        for p in _changed_names_z(scratch, merge_base, expected_sha)
        if not in_scope(p)
    )
    after = sorted(
        p for p in _changed_names_z(scratch, default_ref, new_sha) if not in_scope(p)
    )
    problem = None
    if before != after:
        problem = (
            "the out-of-glob path set changed across the merge: branch"
            f" [{sample_paths(before)}], repaired [{sample_paths(after)}]"
        )
    elif before and _diff_fingerprint(
        _scoped_diff(scratch, merge_base, expected_sha, before)
    ) != _diff_fingerprint(_scoped_diff(scratch, default_ref, new_sha, after)):
        problem = "the out-of-glob diff changed across the merge"
    if problem is not None:
        diagnostic = _range_diff_stat(
            scratch, merge_base, expected_sha, default_ref, new_sha
        )
        raise RepairStopError(
            RepairDisposition.VERIFY_FAILED, f"{problem}; range-diff: {diagnostic}"
        )
    _logger.info(
        "mechanical_repair_scope_verified client=%s ticket=%s out_of_glob_paths=%d",
        client,
        ticket,
        len(before),
    )
