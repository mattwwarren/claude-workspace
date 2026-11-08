"""Shared git-subprocess primitives.

Keep this module free of imports from other ``cw.*`` modules. It is a leaf by
construction so that even the import-disciplined modules — notably
:mod:`cw.review_finding_dispositions`, which may import nothing from ``cw`` at
module scope — can reach it from a function body without risking a cycle.

``cw._git`` is the shared util ``native_daemon._spawn_clean_env``'s docstring
named as the eventual home for the triplicated ``_git_clean_env`` helper
(``spawn.py``, ``native_daemon.py``, ``worktree_gc.py``). Those three copies
are deliberately left in place here: consolidating them is a separate change
with its own blast radius. What lives here is the copy every NEW git
subprocess call should use.

Every ``git`` subprocess outside those three copies (and the worktree
package's own ``cw.worktree._git``) runs through :func:`run_git` or
:func:`git_output` (#2264). Neither accepts ``env=``, so a call site cannot
forget the ``GIT_*`` strip or override it: routing a call through the seam IS
the strip.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Default bound, in seconds, on a local git read or write (#2773). One home
#: for the review-approval predicate and the repair recipes' ``_T_LOCAL``.
LOCAL_GIT_TIMEOUT_SECONDS: Final = 60

#: A full commit SHA: 40 hex (SHA-1) or 64 hex (SHA-256 object format). Match
#: with ``fullmatch``. Shorter hex-looking strings stay ambiguous with branch
#: names and are not matched.
FULL_COMMIT_ID_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

#: git's stderr when ``fetch origin <branch>`` names a branch the remote does
#: not have (a never-pushed branch). ``cw.worktree`` downgrades exactly this
#: failure to DEBUG (#2213) and ``cw review converge verify-tip`` keeps it as
#: ``head_not_origin_tip`` rather than ``tip_check_fetch_failed`` (#2910). It
#: lives in this leaf so ``cw.cli`` need not import ``cw.worktree`` internals.
#: git localizes its messages: under a non-English locale the marker misses and
#: a never-pushed branch is classified ``tip_check_fetch_failed``, whose release
#: (a plain finalize requeue) cannot fix a missing push -- the wrong route. The
#: disposition stays fail-closed: nothing demotes or mutates. Tests that depend
#: on the marker pin ``LC_ALL=C`` for this reason.
MISSING_REMOTE_REF_MARKER: Final = "couldn't find remote ref"


def git_clean_env() -> dict[str, str]:
    """Return ``os.environ`` with every ``GIT_*`` variable stripped.

    ``cw`` can run inside a git hook, where ``GIT_DIR``, ``GIT_WORK_TREE`` and
    ``GIT_INDEX_FILE`` point at the hook's repository rather than the path the
    command was handed (#766). A ``git`` subprocess that inherits them reads a
    DIFFERENT repository than its ``cwd``/``-C`` argument names — and does so
    silently, returning a plausible sha or diff for the wrong tree. Stripping
    them makes the path the caller passed the only thing that decides which
    repository answers.

    Mirrors ``spawn._git_clean_env`` and ``worktree_gc._git_clean_env``; see
    the module docstring for why those copies still exist. New callers should
    reach for :func:`run_git` or :func:`git_output` first, which apply this
    env themselves rather than trusting every call site to pass it.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def run_git(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    check: bool = False,
    capture_output: bool = False,
    text: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git *argv`` with ``GIT_*`` stripped from the inherited environment.

    Every keyword is named explicitly — no ``**kwargs`` passthrough — so a
    caller structurally cannot pass ``env=``: doing so is a ``TypeError`` from
    Python itself, not a runtime check this function has to remember to make.
    ``check``/``capture_output`` default to ``subprocess.run``'s own defaults;
    ``text`` defaults to ``True`` because every caller wants ``str``.
    """
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=git_clean_env(),
    )


def git_output(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float | None = None,
) -> str:
    """``subprocess.check_output(["git", *argv])`` with ``GIT_*`` stripped.

    Always decodes as text — hardcoded rather than a parameter, since no
    caller wants bytes. ``check_output`` raises ``CalledProcessError`` on a
    non-zero exit by contract, so there is no ``check`` to expose.
    """
    return subprocess.check_output(
        ["git", *argv],
        cwd=cwd,
        timeout=timeout,
        text=True,
        env=git_clean_env(),
    )


def git_hooks_dir(repo: Path, *, timeout: float | None = None) -> Path:
    """Return the hooks directory git runs hooks from for *repo*.

    Resolves ``git rev-parse --git-path hooks`` against *repo*, since git
    prints a relative ``core.hooksPath`` as is (and ``.git/hooks`` otherwise).
    Captures stderr so advisory probes stay quiet. Raises whatever
    :func:`run_git` raises (``CalledProcessError`` for a
    non-repo path, ``OSError``, ``TimeoutExpired`` when *timeout* elapses).
    """
    printed = Path(
        run_git(
            ["rev-parse", "--git-path", "hooks"],
            cwd=repo,
            timeout=timeout,
            check=True,
            capture_output=True,
        ).stdout.strip()
    )
    return printed if printed.is_absolute() else repo / printed


def capture_head_sha(
    worktree: Path,
    *,
    ref: str = "HEAD",
    strict: bool = True,
    timeout: float | None = None,
) -> str:
    """Return the sha *ref* (default ``HEAD``) resolves to in *worktree*.

    *ref* is any revision ``git rev-parse`` accepts; the codex boot reaper
    passes ``origin/<branch>`` to compare a worktree's HEAD against its local
    tracking ref (#2285).

    *timeout* bounds the ``git`` call in seconds (``None``, the default, is
    unbounded). A timeout is one more way for ``git`` to fail, so it follows
    *strict* exactly as the other failures below do: ``strict=True`` raises
    ``subprocess.TimeoutExpired``, ``strict=False`` returns ``""``. The boot
    reaper passes one because it runs before the dispatch loop's first tick,
    where a hung ``git`` would keep dispatch from starting at all.

    One implementation for two deliberately different callers (#2232). The
    review pass's diff capture must FAIL LOUDLY when it cannot resolve the
    commit it is about to review — a blank sha there would silently record a
    disposition against nothing. ``cw review dispositions`` must NOT: its job
    is to show the operator what the ledger holds, and an unreadable
    repository must not cost them the listing. That difference is this
    parameter rather than two copies of the subprocess call.

    ``strict=True`` (the default) propagates whatever ``git`` failed with — a
    ``CalledProcessError`` for a non-zero exit, an ``OSError`` for a missing
    binary or a vanished directory. ``strict=False`` returns ``""`` for both,
    which :func:`cw.review_finding_dispositions.disposition_drifted` reads as
    "no question to answer" so the caller's staleness column degrades to
    unknown rather than to a confident "not stale".
    """
    try:
        completed = run_git(
            ["rev-parse", ref],
            cwd=worktree,
            capture_output=True,
            check=strict,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        if strict:
            raise
        return ""
    if completed.returncode != 0:
        # Only reachable with strict=False; check=True already raised above.
        return ""
    return completed.stdout.strip()


#: ``git merge-base --is-ancestor`` exit codes: 0 means *old* is an ancestor
#: of *new*, 1 means it is not. Anything else (128 for an unknown object, a
#: non-repo path) is a git error.
_IS_ANCESTOR_RC: Final = 0
_NOT_ANCESTOR_RC: Final = 1

#: Default bound, in seconds, on the ancestry and range reads below.
_ANCESTRY_TIMEOUT_SECONDS: Final = 10.0

#: Default cap on the ``git log --oneline`` lines :func:`commit_range_summary`
#: returns, so a park message stays readable on a long foreign push.
COMMIT_RANGE_SUMMARY_LIMIT: Final = 20


def is_ancestor(
    repo: Path,
    old: str,
    new: str,
    *,
    timeout: float = _ANCESTRY_TIMEOUT_SECONDS,
) -> bool | None:
    """Return whether commit *old* is an ancestor of commit *new* in *repo*.

    Tri-state and policy-free (RFC 0016 R10): ``True`` when *old* is an
    ancestor of (or equal to) *new*, ``False`` when it is not, and ``None``
    on any git error (an object absent locally, a non-repo path, a missing
    binary, a timeout). Callers that gate a pin advance on this answer must
    fail closed on BOTH ``False`` and ``None``; the existing ancestry probes
    each encode their own caller's failure policy, which is why this one
    encodes none.
    """
    try:
        completed = run_git(
            ["merge-base", "--is-ancestor", old, new],
            cwd=repo,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode == _IS_ANCESTOR_RC:
        return True
    if completed.returncode == _NOT_ANCESTOR_RC:
        return False
    return None


def commit_range_summary(
    repo: Path,
    old: str,
    new: str,
    *,
    limit: int = COMMIT_RANGE_SUMMARY_LIMIT,
    timeout: float = _ANCESTRY_TIMEOUT_SECONDS,
) -> str:
    """Return ``git log --oneline old..new`` in *repo*, capped at *limit* lines.

    Over the cap, the kept lines are followed by an ``(N more)`` suffix line.
    On any git error this returns an ``unavailable: <reason>`` string rather
    than raising: the summary decorates an operator-facing park message and
    must never stop the park itself.
    """
    try:
        completed = run_git(
            ["log", "--oneline", f"{old}..{new}"],
            cwd=repo,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"
    if completed.returncode != 0:
        reason = completed.stderr.strip() or f"git exit {completed.returncode}"
        return f"unavailable: {reason}"
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) <= limit:
        return "\n".join(lines)
    kept = lines[:limit]
    kept.append(f"({len(lines) - limit} more)")
    return "\n".join(kept)
