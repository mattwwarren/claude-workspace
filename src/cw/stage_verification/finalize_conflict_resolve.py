"""Step 4c.5's semantic auto-resolve for the codex FINALIZE create path (#3542).

Reached only on a run that created the PR, when the post-push mergeability
check parked ``merge_conflict_post_push`` and the CHANGELOG-union repair made
no merge. cw runs everything itself, so no codex turn gets write access: one
``git merge --no-edit origin/<base>``, the deterministic
``classify_merge_conflict.py`` (the decision is the script's, "never agent
judgement"), the commit, the ``resolve``-path quality gates, the plain push
and one re-check. Exactly one attempt: never a rebase, a force-push or a
second merge.

The merge and the commit run repository hooks, so cw puts no kill timer on
them (ADR-0014); the other git calls are bounded. A git call that raises is
handled like a failed one.

Every refusal returns the caller's original park with a note saying what was
tried. A failed gate resets the merge away only when this run made it
(``HEAD^1`` is the pre-merge sha on the expected branch); otherwise the note
says the merge is left local and what to run by hand. :class:`ResolveOutcome`
``head`` is the merge sha only once it is pushed and verified on origin.

Imports no ``cw.codex_*`` module (the stage-verification ratchet).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

from cw._git import LOCAL_GIT_TIMEOUT_SECONDS, git_clean_env, git_output, run_git
from cw.merge_probe import merge_tree
from cw.stage_verification.baseline import unmerged_paths
from cw.stage_verification.finalize_gates import check_branch, has_tracked_changes
from cw.stage_verification.finalize_pr import (
    NOTE_LINE_CAP,
    check_mergeability,
    local_only_merge,
    push_ahead,
)
from cw.stage_verification.finalize_result import (
    AGENT_BLOCK,
    MERGE_CONFLICT_POST_PUSH,
    FinalizeBlock,
)
from cw.stage_verification.impl_feedback import fetch_default_branch
from cw.stage_verification.impl_gates import resolve_guard_script
from cw.stage_verification.posted_text import redact_and_cap, redacted_tail

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from cw.stage_verification.finalize_gates import FinalizeGateRun

__all__ = [
    "CLASSIFIER_ABSENT_FRICTION",
    "CLASSIFIER_SCRIPT_NAME",
    "POST_PUSH_UNVERIFIED",
    "SEMANTIC_RESOLVED",
    "SEMANTIC_UNVERIFIED",
    "ResolveOutcome",
    "resolve_post_push_conflict",
]

CLASSIFIER_SCRIPT_NAME: Final = "classify_merge_conflict.py"
MIN_CLASSIFIER_VERSION: Final = 1
CLASSIFIER_ABSENT_FRICTION: Final = "classify_merge_conflict: script absent, skipped"
#: The re-check read MERGEABLE after a classifier-resolved merge.
SEMANTIC_RESOLVED: Final = "semantic_merge_conflict_auto_resolved"
#: The re-check was not blocking but not MERGEABLE either (Step 4c.5 item 7
#: divergence, recorded in the runbook).
SEMANTIC_UNVERIFIED: Final = "semantic_merge_conflict_auto_resolved_unverified"
POST_PUSH_UNVERIFIED: Final = "post_push_mergeability_unverified"
CONFLICTED_FILES_RELATIVE: Final = ".cw/conflicted-files.txt"
AUTO_RESOLVED_TRAILER: Final = "Auto-Resolved-By: classify_merge_conflict.py (#1850)"

_CLASSIFIER_TIMEOUT: Final = 120
_NOT_ATTEMPTED: Final = "semantic auto-resolve not attempted"
_ATTEMPTED: Final = "semantic auto-resolve attempted"
_GIT_ERRORS: Final = (subprocess.SubprocessError, OSError)


@dataclass(frozen=True)
class ResolveOutcome:
    """The resolve attempt: the block (``None`` proceeds), friction and new head."""

    block: FinalizeBlock | None
    friction: list[str] = field(default_factory=list)
    head: str | None = None


@dataclass(frozen=True)
class _Attempt:
    """One run's fixed inputs."""

    worktree: Path
    park: FinalizeBlock
    base_branch: str
    branch: str
    pre_merge: str
    pr_url: str


def _capped(text: str) -> str:
    """*text* for a one-line park note: redacted, then capped at the note width."""
    return redact_and_cap(text, max_line_chars=NOTE_LINE_CAP)


def _noted(block: FinalizeBlock, note: str) -> FinalizeBlock:
    return replace(block, details=f"{block.details}; {note}")


def _git(worktree: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """A bounded local git call that never raises on a non-zero exit."""
    return run_git(
        args, cwd=worktree, capture_output=True, timeout=LOCAL_GIT_TIMEOUT_SECONDS
    )


def _git_hooked(worktree: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """A git call that runs repository hooks: no kill timer (ADR-0014)."""
    return run_git(args, cwd=worktree, capture_output=True)


def _output(done: subprocess.CompletedProcess[str]) -> str:
    return redacted_tail(f"{done.stdout or ''}\n{done.stderr or ''}")


# -- preconditions -----------------------------------------------------------


def _local_head(worktree: Path) -> str:
    try:
        return git_output(["rev-parse", "HEAD"], cwd=worktree).strip()
    except (subprocess.CalledProcessError, OSError):
        return ""


def _precondition(worktree: Path, head_sha: str) -> str | None:
    """Why the merge must not start, before any fetch."""
    head = _local_head(worktree)
    if head != head_sha:
        return f"local HEAD {head or '(unreadable)'} is not the PR head {head_sha}"
    if has_tracked_changes(worktree):
        return (
            "the worktree has tracked modifications (a tolerated path or review "
            "artifact counts)"
        )
    return None


def _z_paths(worktree: Path, args: list[str]) -> list[str]:
    """The paths a ``-z`` git read lists, unquoted; non-UTF-8 bytes survive."""
    out = git_output(args, cwd=worktree, errors="surrogateescape")
    return [p for p in out.split("\0") if p]


def _clobbered(worktree: Path, path: str, tracked: frozenset[str]) -> str | None:
    """The untracked entry writing *path* would destroy: itself or a parent."""
    for parent in reversed(PurePosixPath(path).parents[:-1]):
        name = str(parent)
        entry = worktree / name
        blocks = entry.is_symlink() or not entry.is_dir()
        if name not in tracked and os.path.lexists(entry) and blocks:
            return name
    if path not in tracked and os.path.lexists(worktree / path):
        return path
    return None


def _merge_writes(worktree: Path, base_branch: str) -> list[str] | None:
    """Every path a merge of ``origin/<base>`` may write; ``None`` if unknown.

    The base side's changes since the merge base, plus every path that differs
    between HEAD and the tree ``git merge-tree`` says the merge writes. Only the
    second names a path a directory rename moves a branch-side file to.
    """
    base = f"origin/{base_branch}"
    diff = ["diff", "-z", "--name-only", "--no-renames"]
    merged = merge_tree(worktree, "HEAD", base)
    if merged.tree is None:
        return None
    changed = _z_paths(worktree, [*diff, f"HEAD...{base}"])
    written = _z_paths(worktree, [*diff, "HEAD", merged.tree])
    return list(dict.fromkeys([*changed, *written]))


def _untracked_clobbers(worktree: Path, base_branch: str) -> list[str] | None:
    """Untracked (or ignored) entries a merge of ``origin/<base>`` would write over.

    Covers every path :func:`_merge_writes` names, and every parent of it that
    is not a directory. ``git merge`` silently overwrites an ignored file, and
    ``merge --abort`` or a later revert would delete it. ``None`` when git could
    not answer (fail closed).
    """
    try:
        writes = _merge_writes(worktree, base_branch)
        tracked = frozenset(_z_paths(worktree, ["ls-files", "-z"]))
    except (subprocess.CalledProcessError, OSError):
        return None
    if writes is None:
        return None
    found = (_clobbered(worktree, path, tracked) for path in writes)
    return list(dict.fromkeys(name for name in found if name is not None))


def _printable(text: str) -> str:
    """*text* with the lone surrogates of non-UTF-8 path bytes escaped."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _fetch_and_check(attempt: _Attempt) -> str | None:
    """Fetch the base and refuse an untracked-file clobber; ``None`` when clear."""
    failed = fetch_default_branch(attempt.worktree, attempt.base_branch)
    if failed is not None:
        return failed
    clobbers = _untracked_clobbers(attempt.worktree, attempt.base_branch)
    if clobbers is None:
        return f"the paths origin/{attempt.base_branch} changes could not be listed"
    if clobbers:
        names = _capped(_printable(", ".join(clobbers)))
        return (
            f"merging origin/{attempt.base_branch} would write over {names}, "
            "untracked or ignored in the worktree"
        )
    return None


# -- the merge and its abort ---------------------------------------------------


def _abort_note(worktree: Path) -> str:
    """``git merge --abort`` when a merge is in progress; a note if it failed."""
    try:
        probe = _git(worktree, ["rev-parse", "-q", "--verify", "MERGE_HEAD"])
        if probe.returncode != 0:
            return ""
        done = _git(worktree, ["merge", "--abort"])
    except _GIT_ERRORS as exc:
        why = redacted_tail(str(exc))
    else:
        if done.returncode == 0:
            return ""
        why = _output(done)
    return (
        f"; git merge --abort failed ({why}): the worktree is mid-merge,"
        ' run "git merge --abort" by hand'
    )


def _abort_park(
    attempt: _Attempt, note: str, conflicted: Sequence[str]
) -> FinalizeBlock:
    """Abort the merge, then the original park with *note* and the file list."""
    aborted = _abort_note(attempt.worktree)
    block = _noted(attempt.park, note)
    if conflicted:
        block = _noted(block, f"conflicted files: {_capped(', '.join(conflicted))}")
    return replace(block, details=block.details + aborted)


def _write_conflicted(worktree: Path, conflicted: Sequence[str]) -> Path:
    target = worktree / CONFLICTED_FILES_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"{p}\n" for p in conflicted), encoding="utf-8")
    return target


@dataclass(frozen=True)
class _Resolved:
    """The classifier's exit-0 result."""

    files: tuple[str, ...]
    categories: str


def _parse_resolved(stdout: str) -> _Resolved | None:
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("safe") is not True:
        return None
    files = data.get("resolved_files")
    cats = data.get("categories")
    if not isinstance(files, list) or not files or not isinstance(cats, dict):
        return None
    if not all(isinstance(f, str) and f for f in files):
        return None
    text = ", ".join(f"{k}={v}" for k, v in cats.items())
    return _Resolved(tuple(files), text)


def _classify(
    attempt: _Attempt, conflicted: Sequence[str]
) -> _Resolved | FinalizeBlock:
    """Run the classifier once; its resolution, or the block to return."""
    script = resolve_guard_script(
        attempt.worktree, CLASSIFIER_SCRIPT_NAME, MIN_CLASSIFIER_VERSION
    )
    if script.path is None:
        return _abort_park(
            attempt, f"{_NOT_ATTEMPTED}: {CLASSIFIER_SCRIPT_NAME} is absent", conflicted
        )
    if not script.marker_ok:
        aborted = _abort_note(attempt.worktree)
        return FinalizeBlock(
            AGENT_BLOCK,
            "Step 4c.5: HEADLESS BLOCK — "
            f"{CLASSIFIER_SCRIPT_NAME} at {script.path} — missing/stale "
            f"cw-script-version marker (need >= {MIN_CLASSIFIER_VERSION})" + aborted,
        )
    listing = _write_conflicted(attempt.worktree, conflicted)
    argv = [
        sys.executable,
        str(script.path),
        "resolve",
        "--conflicted-files",
        str(listing),
        "--json",
    ]
    try:
        done = subprocess.run(
            argv,
            cwd=attempt.worktree,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            timeout=_CLASSIFIER_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _abort_park(
            attempt,
            f"{_ATTEMPTED} — did not finish: {redacted_tail(str(exc))}",
            conflicted,
        )
    if done.returncode != 0:
        return _abort_park(
            attempt, f"{_ATTEMPTED} — refused: {_output(done)}", conflicted
        )
    resolved = _parse_resolved(done.stdout)
    if resolved is None:
        return _abort_park(
            attempt, f"{_ATTEMPTED} — unreadable result: {_output(done)}", conflicted
        )
    return resolved


def _stage_and_commit(attempt: _Attempt, resolved: _Resolved) -> str | None:
    """Stage the resolution and commit the merge; ``None`` on success, else why."""
    added = _git(attempt.worktree, ["add", "--", *resolved.files])
    if added.returncode != 0:
        return _output(added)
    still = unmerged_paths(attempt.worktree)
    if still:
        return _capped(f"still unmerged: {', '.join(still)}")
    done = _git_hooked(
        attempt.worktree,
        [
            "commit",
            "-m",
            f"Merge origin/{attempt.base_branch} "
            f"(semantic auto-resolve: {resolved.categories})",
            "--trailer",
            AUTO_RESOLVED_TRAILER,
        ],
    )
    return None if done.returncode == 0 else _output(done)


def _commit(
    attempt: _Attempt, resolved: _Resolved, conflicted: Sequence[str]
) -> FinalizeBlock | None:
    """Commit the merge with the doc's message, or abort it and park."""
    try:
        why = _stage_and_commit(attempt, resolved)
    except _GIT_ERRORS as exc:
        why = redacted_tail(str(exc))
    if why is None:
        return None
    return _abort_park(attempt, f"{_ATTEMPTED} — commit failed: {why}", conflicted)


# -- gates and the revert ------------------------------------------------------


def _revert(attempt: _Attempt) -> str | None:
    """Reset the merge this run made; ``None`` on success, else why not."""
    if check_branch(attempt.worktree, attempt.branch) is not None:
        return f"the worktree no longer has {attempt.branch!r} checked out"
    parent = _git(attempt.worktree, ["rev-parse", "HEAD^1"])
    if parent.returncode != 0 or parent.stdout.strip() != attempt.pre_merge:
        return f"HEAD^1 is not the pre-merge commit {attempt.pre_merge}"
    reset = _git(attempt.worktree, ["reset", "--hard", attempt.pre_merge])
    if reset.returncode != 0:
        return f"git reset --hard {attempt.pre_merge} failed: {_output(reset)}"
    return None


def _left_local_note(attempt: _Attempt, why: str, merge_sha: str) -> str:
    """What a refused revert left behind and the command to finish it by hand.

    Not passed through the posted-text caps: a cut command is worse than a long
    line, and only *why* carries subprocess output (already capped).
    """
    head = _local_head(attempt.worktree)
    moved = "" if head == merge_sha else f" (HEAD is now {head or '(unreadable)'})"
    worktree = shlex.quote(str(attempt.worktree))
    return (
        f"NOT reverted ({redact_and_cap(why)}): the merge commit"
        f" {merge_sha or '(unreadable)'} is left in the local worktree only{moved}."
        f" Confirm {attempt.branch} is checked out in {attempt.worktree} and"
        " inspect it; if it is this run's merge, run"
        f' "git -C {worktree} reset --hard {attempt.pre_merge}" by hand'
        " (commits above the merge would be lost), then requeue finalize"
    )


def _gate_park(
    attempt: _Attempt,
    gated: FinalizeGateRun,
    categories: str,
    conflicted: Sequence[str],
    *,
    merge_sha: str,
) -> FinalizeBlock:
    """The gate-failure park: reverted, or a note naming the merge left local.

    *merge_sha* is the commit this run made, read before the gates ran.
    """
    block = gated.block
    if gated.failed_gate:
        what = (
            f"gate {gated.failed_gate} failed: {gated.failed_output or '(no output)'}"
        )
    else:
        details = block.details if block is not None else "no reason recorded"
        what = f"the quality gates could not run: {_capped(details)}"
    try:
        why = _revert(attempt)
    except _GIT_ERRORS as exc:
        why = f"the revert did not finish: {redacted_tail(str(exc))}"
    note = f"semantic auto-resolve resolved conflicts ({categories}) but {what}"
    if why is None:
        parked = _noted(attempt.park, f"{note}; reverted")
    else:
        left = _left_local_note(attempt, why, merge_sha)
        parked = replace(_noted(attempt.park, f"{note}; {left}"), retry_eligible=False)
    return _noted(parked, f"conflicted files: {_capped(', '.join(conflicted))}")


# -- push and re-check -----------------------------------------------------------


def _publish(
    attempt: _Attempt, *, classifier_ran: bool, conflicted: Sequence[str]
) -> ResolveOutcome:
    """Push the merge plainly, then re-check mergeability once."""
    merge_sha = _local_head(attempt.worktree)
    pushed = push_ahead(attempt.worktree, branch=attempt.branch, head_sha=merge_sha)
    if pushed is not None:
        site = "semantic-resolve push" if classifier_ran else "merge push"
        merge = (
            "semantic auto-resolve merge"
            if classifier_ran
            else f"origin/{attempt.base_branch} merge"
        )
        prefixed = replace(pushed, details=f"Step 4c.5 {site}: {pushed.details}")
        block = local_only_merge(
            prefixed, merge=merge, branch=attempt.branch, head_sha=merge_sha
        )
        return ResolveOutcome(block)
    token = SEMANTIC_UNVERIFIED if classifier_ran else POST_PUSH_UNVERIFIED
    recheck, friction = check_mergeability(
        attempt.worktree, attempt.pr_url, unverified_token=token
    )
    if recheck is not None:
        if recheck.reason == MERGE_CONFLICT_POST_PUSH:
            recheck = _noted(
                recheck,
                f"still not mergeable after one merge of origin/{attempt.base_branch}",
            )
            if conflicted:
                recheck = _noted(
                    recheck, f"conflicted files: {_capped(', '.join(conflicted))}"
                )
        return ResolveOutcome(recheck, friction, merge_sha)
    if classifier_ran and SEMANTIC_UNVERIFIED not in friction:
        friction = [*friction, SEMANTIC_RESOLVED]
    return ResolveOutcome(None, friction, merge_sha)


# -- the attempt -----------------------------------------------------------------


def _after_conflict(
    attempt: _Attempt,
    conflicted: Sequence[str],
    *,
    ignore_paths: Sequence[str],
    run_gates: Callable[[], FinalizeGateRun],
) -> ResolveOutcome:
    """Classify, commit, gate and publish a conflicting merge."""
    # Exact repo-relative match, no globs (the merge gate's own rule).
    ignored = [p for p in conflicted if p in ignore_paths]
    if ignored:
        note = (
            f"{_NOT_ATTEMPTED}: {_capped(', '.join(ignored))} must be "
            "regenerated, not hand-resolved"
        )
        return ResolveOutcome(_abort_park(attempt, note, conflicted))
    resolved = _classify(attempt, conflicted)
    if isinstance(resolved, FinalizeBlock):
        absent = resolved.reason == MERGE_CONFLICT_POST_PUSH and (
            f"{CLASSIFIER_SCRIPT_NAME} is absent" in resolved.details
        )
        return ResolveOutcome(resolved, [CLASSIFIER_ABSENT_FRICTION] if absent else [])
    committed = _commit(attempt, resolved, conflicted)
    if committed is not None:
        return ResolveOutcome(committed)
    merge_sha = _local_head(attempt.worktree)
    gated = run_gates()
    if gated.block is not None:
        parked = _gate_park(
            attempt, gated, resolved.categories, conflicted, merge_sha=merge_sha
        )
        return ResolveOutcome(parked)
    return _publish(attempt, classifier_ran=True, conflicted=conflicted)


def _merge_base(
    attempt: _Attempt,
) -> subprocess.CompletedProcess[str] | FinalizeBlock:
    """The one ``git merge`` of the base; the park when it did not finish."""
    argv = ["merge", "--no-edit", f"origin/{attempt.base_branch}"]
    try:
        return _git_hooked(attempt.worktree, argv)
    except _GIT_ERRORS as exc:
        note = f"{_ATTEMPTED} — git merge did not finish: {redacted_tail(str(exc))}"
        return _abort_park(attempt, note, ())


def resolve_post_push_conflict(
    worktree: Path,
    *,
    park: FinalizeBlock,
    base_branch: str,
    branch: str,
    head_sha: str,
    pr_url: str,
    ignore_paths: Sequence[str],
    run_gates: Callable[[], FinalizeGateRun],
) -> ResolveOutcome:
    """One semantic auto-resolve attempt for the ``merge_conflict_post_push`` *park*.

    *head_sha* is the PR head (local HEAD must equal it). *ignore_paths* are
    the client's ``merge_gate_ignore_paths``: a conflict in one is never
    resolved. *run_gates* runs the ``resolve``-path quality gates on the merge.
    """
    why = _precondition(worktree, head_sha)
    attempt = _Attempt(worktree, park, base_branch, branch, head_sha, pr_url)
    if why is None:
        why = _fetch_and_check(attempt)
    if why is not None:
        return ResolveOutcome(_noted(park, f"{_NOT_ATTEMPTED}: {_capped(why)}"))
    merged = _merge_base(attempt)
    if isinstance(merged, FinalizeBlock):
        return ResolveOutcome(merged)
    if merged.returncode == 0:
        return _publish(attempt, classifier_ran=False, conflicted=())
    conflicted = unmerged_paths(worktree)
    if not conflicted:
        note = (
            f"{_NOT_ATTEMPTED}: git merge failed with no conflicted files: "
            f"{_output(merged)}"
        )
        return ResolveOutcome(_abort_park(attempt, note, ()))
    return _after_conflict(
        attempt, conflicted, ignore_paths=ignore_paths, run_gates=run_gates
    )
