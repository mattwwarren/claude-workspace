"""Declared gate autofix helpers for the codex fix loop (#3412).

A fix cycle's gate that fails red may declare an autofix (a formatter, say).
:mod:`cw.codex_fix_loop.gates` runs it, re-checks the SAME gate command and
parks only if that still fails. This module holds the pure pieces:

- :func:`declared_autofixes` reads the opt-in, exact-name
  ``quality_gates.autofix.<gate-name>`` map of ``.claude/project-config.yaml``.
  It never raises: a missing, malformed, wrongly-typed or non-UTF-8 file means
  "no config-declared autofix", so a config problem can never change a plan's
  availability or crash cycle start.
- :func:`changed_paths` is the path set that differs between two tree objects,
  optionally narrowed to one ``--diff-filter`` (``"D"``: the paths an autofix
  deleted, which the gate runner rejects, #3427).
- :func:`ignored_on_disk` is the git-ignored set on disk, snapshotted just
  before an autofix runs, so a revert never deletes a file that existed
  before it (#3420).
- :func:`revert_paths` restores a chosen path set to an earlier tree object.
- :func:`revert_to_tree` undoes everything since an earlier tree: the targeted
  :func:`revert_paths` first, then a ``git read-tree --reset -u`` fallback that
  keeps review artifacts and preserved ignored files intact, and raises
  ``CalledProcessError`` only when neither restores the tree (#3423).

**Path spelling.** ``cw.codex_fix_loop.baseline.cycle_touched_paths`` reads
``git diff-tree --name-only`` without ``-z``, so a special or non-ASCII path
arrives C-quoted (``"caf\\303\\251.py"``). The fence's ``touched`` set keeps
that spelling. :func:`changed_paths` therefore has two modes: ``c_quoted=True``
reproduces that spelling, so a subset check against ``touched`` compares like
with like; the default reads ``-z`` and returns the REAL path, which is what
:func:`revert_paths` needs (a quoted path would match nothing in ``ls-tree`` or
``restore`` and silently under-revert). Every ``-z`` read decodes with
``surrogateescape`` (#3419), so a non-UTF-8 name survives as a string that
re-encodes to its original bytes as git argv or a filesystem path.

This module imports nothing from :mod:`cw.codex_fix_loop.gates`.
"""

from __future__ import annotations

import contextlib
import logging
import stat
import subprocess
from typing import TYPE_CHECKING

from cw._git import git_output
from cw.codex_fix_loop.baseline import staged_cycle_tree
from cw.project_config import load_project_config_dict

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_log = logging.getLogger(__name__)

# Paths per git invocation: far below ARG_MAX, large enough that a typical
# cycle is one call.
_CHUNK = 200
_LITERAL = "--literal-pathspecs"
# Decode handler for every ``-z`` path read (#3419); see the module docstring.
_SURROGATE = "surrogateescape"
_UNRESTORED = "worktree could not be restored to the pre-autofix tree"
_FALLBACK_LOG = "codex_fix_gate_autofix_revert_fallback %s outcome=%s"
_INCOMPLETE_LOG = "codex_fix_gate_autofix_revert_incomplete %s worktree=%s"

# One snapshotted review artifact: (relative path, bytes, st_mode).
_ArtifactSnapshot = list[tuple[str, bytes, int]]


def declared_autofixes(worktree: Path) -> dict[str, str]:
    """Return ``{gate name: autofix command}`` from the project config.

    Never raises for any file content or read failure: ``OSError`` and
    ``ValueError`` (which covers ``UnicodeDecodeError`` from a non-UTF-8 file
    the shared loader does not catch) both give ``{}``. ``quality_gates`` and
    ``autofix`` must be mappings; keys must be ``str`` and values non-blank
    ``str`` (stripped). Anything else is dropped.
    """
    try:
        raw = load_project_config_dict(worktree)
    except (OSError, ValueError):
        return {}
    if raw is None:
        return {}
    quality_gates = raw.get("quality_gates")
    if not isinstance(quality_gates, dict):
        return {}
    block = quality_gates.get("autofix")
    if not isinstance(block, dict):
        return {}
    return {
        name: command.strip()
        for name, command in block.items()
        if isinstance(name, str)
        and name
        and isinstance(command, str)
        and command.strip()
    }


def changed_paths(
    worktree: Path,
    tree_a: str,
    tree_b: str,
    *,
    c_quoted: bool = False,
    diff_filter: str | None = None,
) -> frozenset[str]:
    """Return every path that differs between two tree objects.

    ``--no-renames``: a rename reports both sides (and, under
    ``diff_filter="D"``, its old path as deleted). By default the real path
    (``-z``); with *c_quoted* the spelling ``cycle_touched_paths`` uses.
    *diff_filter* is passed as ``--diff-filter``.

    Returned real paths may contain surrogate escapes (non-UTF-8 filenames).
    Use them only as git argv or filesystem paths; never log them with ``%s``
    or serialize them (JSON, events, comments).
    """
    argv = ["diff-tree", "-r", "--name-only", "--no-renames"]
    if diff_filter is not None:
        argv.append(f"--diff-filter={diff_filter}")
    if not c_quoted:
        argv.append("-z")
    out = git_output(
        [*argv, tree_a, tree_b],
        cwd=worktree,
        errors="strict" if c_quoted else _SURROGATE,
    )
    separator = "\n" if c_quoted else "\0"
    return frozenset(line for line in out.split(separator) if line)


def ignored_on_disk(worktree: Path) -> frozenset[str]:
    """Return the git-ignored paths on disk; an ignored dir is one ``dir/`` entry.

    Taken just before an autofix runs: a path the revert finds "added" since
    the pre-autofix tree, yet already on disk, can only have been ignored then
    (``staged_cycle_tree`` stages everything else), so this is the set a revert
    must never delete (#3420).

    Returned strings may contain surrogate escapes (non-UTF-8 filenames). Use
    them only as git argv or filesystem paths; never log them with ``%s`` or
    serialize them (JSON, events, comments).
    """
    out = git_output(
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        ],
        cwd=worktree,
        errors=_SURROGATE,
    )
    return frozenset(line for line in out.split("\0") if line)


def _chunks(paths: list[str]) -> Iterator[list[str]]:
    for start in range(0, len(paths), _CHUNK):
        yield paths[start : start + _CHUNK]


def _present_in_tree(worktree: Path, tree: str, paths: list[str]) -> set[str]:
    """Return which of *paths* exist in *tree* (real names, ``-z``)."""
    out = git_output(
        [_LITERAL, "ls-tree", "-r", "--name-only", "-z", tree, "--", *paths],
        cwd=worktree,
        errors=_SURROGATE,
    )
    return {line for line in out.split("\0") if line}


def _is_preserved(path: str, preserve: frozenset[str]) -> bool:
    """Whether *path* is a preserved entry or lies under a preserved ``dir/``."""
    return path in preserve or any(
        entry.endswith("/") and path.startswith(entry) for entry in preserve
    )


def _revert_chunk(
    worktree: Path, tree: str, paths: list[str], preserve: frozenset[str]
) -> None:
    present = _present_in_tree(worktree, tree, paths)
    restorable = [path for path in paths if path in present]
    if restorable:
        git_output(
            [
                _LITERAL,
                "restore",
                f"--source={tree}",
                "--staged",
                "--worktree",
                "--",
                *restorable,
            ],
            cwd=worktree,
        )
    added = [path for path in paths if path not in present]
    if added:
        # --quiet: git rm echoes each removed path raw (non-UTF-8 included).
        git_output(
            [
                _LITERAL,
                "rm",
                "-f",
                "--cached",
                "--ignore-unmatch",
                "--quiet",
                "--",
                *added,
            ],
            cwd=worktree,
        )
        for path in added:
            if _is_preserved(path, preserve):
                continue
            with contextlib.suppress(OSError):
                (worktree / path).unlink()


def revert_paths(
    worktree: Path,
    tree: str,
    paths: frozenset[str],
    *,
    preserve: frozenset[str] = frozenset(),
) -> None:
    """Restore each of *paths* (real names) to its state in *tree*.

    A path in *tree* is restored in the index and the worktree; a path absent
    from it (added since) is removed from both. ``git restore`` fails for a
    path absent from the source tree, which is why the two are split. Paths
    outside the set are not touched, and an empty set runs no git command.

    An added path in *preserve*, or under a *preserve* entry ending ``/``, is
    removed from the index only and stays on disk: it is a file that was
    git-ignored before the autofix and only looks added because the autofix
    rewrote the ignore rules (#3420).
    """
    for chunk in _chunks(sorted(paths)):
        _revert_chunk(worktree, tree, chunk, preserve)


def _targeted_revert(
    worktree: Path,
    review_artifacts: frozenset[str],
    tree: str,
    preserve: frozenset[str],
    context: str,
) -> bool:
    """Rung 1: revert the changed paths; whether the tree is back to *tree*."""
    try:
        current = staged_cycle_tree(worktree, review_artifacts)
        revert_paths(
            worktree, tree, changed_paths(worktree, tree, current), preserve=preserve
        )
        if staged_cycle_tree(worktree, review_artifacts) == tree:
            return True
        _log.warning(_INCOMPLETE_LOG, context, worktree)
    except subprocess.CalledProcessError:
        _log.warning(_INCOMPLETE_LOG, context, worktree, exc_info=True)
    return False


def _snapshot_artifacts(
    worktree: Path, review_artifacts: frozenset[str]
) -> _ArtifactSnapshot:
    """Bytes and mode of every review artifact that is a regular file on disk."""
    snapshot: _ArtifactSnapshot = []
    for relpath in sorted(review_artifacts):
        path = worktree / relpath
        if path.is_file() and not path.is_symlink():
            snapshot.append((relpath, path.read_bytes(), path.stat().st_mode))
    return snapshot


def _rewrite_artifacts(
    worktree: Path, snapshot: _ArtifactSnapshot, context: str
) -> None:
    """Best effort: put each snapshotted artifact back; an ``OSError`` only logs.

    Runs in a ``finally``, so it must never raise over the real git error.
    """
    for relpath, data, mode in snapshot:
        path = worktree / relpath
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(stat.S_IMODE(mode))
        except OSError:
            _log.warning(
                "codex_fix_gate_autofix_revert_fallback %s artifact_rewrite_failed",
                context,
                exc_info=True,
            )


def _drop_preserve_from_index(worktree: Path, preserve: frozenset[str]) -> None:
    """Remove preserved paths from the index so ``read-tree -u`` keeps them.

    After an ignore-rule rewrite, ``git add -A`` staged the previously ignored
    files; ``read-tree --reset -u`` deletes an index entry absent from the
    target tree from disk. Preserved paths were untracked when snapshotted, so
    none is in the target tree and dropping their entries is safe.
    """
    for chunk in _chunks(sorted(preserve)):
        git_output(
            [
                _LITERAL,
                "rm",
                "-r",
                "-f",
                "--cached",
                "--ignore-unmatch",
                "--quiet",
                "--",
                *chunk,
            ],
            cwd=worktree,
        )


def _fallback_read_tree(
    worktree: Path,
    review_artifacts: frozenset[str],
    tree: str,
    preserve: frozenset[str],
    context: str,
) -> None:
    """Rung 2: ``read-tree --reset -u`` to *tree*; raise if that cannot restore it.

    It resets index and worktree for tracked paths, including the
    directory-to-file type change ``git restore`` cannot undo. It would also
    reset a tracked-modified review artifact (its index entry equals the tree
    while the file is dirty) and delete a staged preserved file, so artifacts
    are snapshotted and rewritten and preserved paths leave the index first.
    """
    snapshot = _snapshot_artifacts(worktree, review_artifacts)
    try:
        try:
            _drop_preserve_from_index(worktree, preserve)
            git_output(["read-tree", "--reset", "-u", tree], cwd=worktree)
        finally:
            _rewrite_artifacts(worktree, snapshot, context)
        restored = staged_cycle_tree(worktree, review_artifacts) == tree
    except subprocess.CalledProcessError:
        _log.warning(_FALLBACK_LOG, context, "failed", exc_info=True)
        raise
    if not restored:
        _log.warning(_FALLBACK_LOG, context, "failed")
        raise subprocess.CalledProcessError(
            1, ["git", "read-tree", "--reset", "-u", tree], stderr=_UNRESTORED
        )
    _log.warning(_FALLBACK_LOG, context, "restored")


def revert_to_tree(
    worktree: Path,
    review_artifacts: frozenset[str],
    tree: str,
    *,
    preserve: frozenset[str] = frozenset(),
    context: str,
) -> None:
    """Undo everything that changed since *tree* (a rejected autofix, #3423).

    The targeted :func:`revert_paths` runs first. If it raises
    ``CalledProcessError`` or leaves the tree different, a WARNING
    ``codex_fix_gate_autofix_revert_incomplete`` is logged and a
    ``git read-tree --reset -u`` fallback runs, keeping review artifacts and
    *preserve* (see :func:`ignored_on_disk`) intact. *context* is the caller's
    preformatted identity (ticket, session, cycle, gate) for those log lines.

    Raises ``CalledProcessError`` only when neither the targeted revert nor the
    read-tree fallback restores the tree. ``OSError`` (git missing) is not
    caught.
    """
    if _targeted_revert(worktree, review_artifacts, tree, preserve, context):
        return
    _fallback_read_tree(worktree, review_artifacts, tree, preserve, context)
