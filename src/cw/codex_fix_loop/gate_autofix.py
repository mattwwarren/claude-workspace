"""Declared gate autofix helpers for the codex fix loop (#3412).

A fix cycle's gate that fails red may declare an autofix (a formatter, say).
:mod:`cw.codex_fix_loop.gates` runs it, re-checks the SAME gate command and
parks only if that still fails. This module holds the pure pieces:

- :func:`declared_autofixes` reads the opt-in, exact-name
  ``quality_gates.autofix.<gate-name>`` map of ``.claude/project-config.yaml``.
  It never raises: a missing, malformed, wrongly-typed or non-UTF-8 file means
  "no config-declared autofix", so a config problem can never change a plan's
  availability or crash cycle start.
- :func:`changed_paths` is the path set that differs between two tree objects.
- :func:`revert_paths` restores a chosen path set to an earlier tree object.

**Path spelling.** ``cw.codex_fix_loop.baseline.cycle_touched_paths`` reads
``git diff-tree --name-only`` without ``-z``, so a special or non-ASCII path
arrives C-quoted (``"caf\\303\\251.py"``). The fence's ``touched`` set keeps
that spelling. :func:`changed_paths` therefore has two modes: ``c_quoted=True``
reproduces that spelling, so a subset check against ``touched`` compares like
with like; the default reads ``-z`` and returns the REAL path, which is what
:func:`revert_paths` needs (a quoted path would match nothing in ``ls-tree`` or
``restore`` and silently under-revert).

This module imports nothing from :mod:`cw.codex_fix_loop.gates`.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from cw._git import git_output
from cw.project_config import load_project_config_dict

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# Paths per git invocation: far below ARG_MAX, large enough that a typical
# cycle is one call.
_CHUNK = 200
_LITERAL = "--literal-pathspecs"


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
    worktree: Path, tree_a: str, tree_b: str, *, c_quoted: bool = False
) -> frozenset[str]:
    """Return every path that differs between two tree objects.

    ``--no-renames``: a rename reports both sides. By default the real path
    (``-z``); with *c_quoted* the spelling ``cycle_touched_paths`` uses.
    """
    argv = ["diff-tree", "-r", "--name-only", "--no-renames"]
    if not c_quoted:
        argv.append("-z")
    out = git_output([*argv, tree_a, tree_b], cwd=worktree)
    separator = "\n" if c_quoted else "\0"
    return frozenset(line for line in out.split(separator) if line)


def _chunks(paths: list[str]) -> Iterator[list[str]]:
    for start in range(0, len(paths), _CHUNK):
        yield paths[start : start + _CHUNK]


def _present_in_tree(worktree: Path, tree: str, paths: list[str]) -> set[str]:
    """Return which of *paths* exist in *tree* (real names, ``-z``)."""
    out = git_output(
        [_LITERAL, "ls-tree", "-r", "--name-only", "-z", tree, "--", *paths],
        cwd=worktree,
    )
    return {line for line in out.split("\0") if line}


def _revert_chunk(worktree: Path, tree: str, paths: list[str]) -> None:
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
        git_output(
            [_LITERAL, "rm", "-f", "--cached", "--ignore-unmatch", "--", *added],
            cwd=worktree,
        )
        for path in added:
            with contextlib.suppress(OSError):
                (worktree / path).unlink()


def revert_paths(worktree: Path, tree: str, paths: frozenset[str]) -> None:
    """Restore each of *paths* (real names) to its state in *tree*.

    A path in *tree* is restored in the index and the worktree; a path absent
    from it (added since) is removed from both. ``git restore`` fails for a
    path absent from the source tree, which is why the two are split. Paths
    outside the set are not touched, and an empty set runs no git command.
    """
    for chunk in _chunks(sorted(paths)):
        _revert_chunk(worktree, tree, chunk)
