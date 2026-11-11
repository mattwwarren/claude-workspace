"""cw-run guards and gates for the codex IMPL stage (#3061, RFC 0018 C2).

Every function returns an :class:`~cw.codex_impl._result.ImplBlock` (or
``None``): the one IMPL-worded block value. The fix-loop breach builders in
:mod:`cw.codex_fix_loop` word their ``details`` and ``recovery_hint`` for a
REVIEW cycle ("requeue REVIEW", ``cw review settle``); :func:`fence_block`
keeps only a breach's ``reason`` (an existing vocabulary reason) and paths and
words the rest for IMPL, and no ``recovery_hint`` is ever surfaced.

Gate order, as :mod:`cw.codex_impl._run` calls them: the pre-turn guards
(:func:`check_plan_present`, :func:`check_on_branch`,
:func:`check_existing_branch_work`), then after the turn gate 1
(:func:`check_empty_diff`), the fence, gate 2 (:func:`run_scope_conformance`)
and the repo's quality gates (:func:`run_quality_gates`).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from cw._git import git_clean_env, git_output, run_git
from cw.auto_dev_result.schema._vocab import (
    EMPTY_DIFF_BLOCKER_REASON,
    PLAN_SCOPE_DRIFT_BLOCKER_REASON,
)
from cw.codex_fix_loop.commit import _scope_violations
from cw.codex_fix_loop.fence import scope_violation_breach
from cw.codex_fix_loop.gates import run_cycle_gates
from cw.codex_fix_loop.hook_failure import CommitHookFailedError, hook_failure_breach
from cw.codex_fix_loop.posted_text import redact_and_cap
from cw.codex_impl._result import (
    IMPL_FAILED,
    PLAN_MISSING,
    ImplBlock,
)
from cw.codex_review import (
    CODEX_ERROR,
    CODEX_FIX_DIRTY_START,
    CODEX_FIX_GROWTH_BUDGET,
    CODEX_FIX_HOOK_FAILED,
    CODEX_FIX_REVERTED_BRANCH,
    CODEX_FIX_SCOPE_DRIFT,
)
from cw.executor._repair_entry import is_adopted_repair_entry
from cw.worktree import _parse_numstat_totals

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from cw.auto_dev_result.schema._vocab import ScopeTier
    from cw.codex_fix_loop.baseline import CycleBaseline, DirtyStart
    from cw.codex_fix_loop.fence import FenceBreach
    from cw.codex_fix_loop.gates import GatePlan, GateScope
    from cw.local_runner import GitFacts
    from cw.models import TicketTask

SCOPE_SCRIPT_NAME: Final = "check_plan_scope_conformance.py"
#: ``# cw-script-version: N`` minimum for the scope-conformance script (the
#: version table in ``auto-dev-impl.md``).
MIN_SCOPE_SCRIPT_VERSION: Final = 3

SCRIPT_ABSENT_FRICTION: Final = "check_plan_scope_conformance: script absent, skipped"
REPAIR_SKIP_FRICTION: Final = "impl_scope_conformance_skipped_repair_entry"
UNPARSED_FRICTION_PREFIX: Final = "impl_scope_conformance_unparsed: "
GROWTH_FRICTION_PREFIX: Final = "impl_scope_growth: "
APPROVAL_STALE_PREFIX: Final = "scope_drift_approval_stale: "

_MARKER_HEAD_LINES: Final = 5
_MARKER_LINE: Final = re.compile(r"#\s*cw-script-version:\s*(\S*)\s*")
_MARKER_TOKEN: Final = re.compile(r"[0-9]{1,6}")
_APPROVED_HEAD: Final = re.compile(r"[0-9a-f]{40,64}")
_EXIT_DRIFT: Final = 1
_EXIT_USAGE: Final = 2
_MAX_LISTED: Final = 20
_STDERR_TAIL_CHARS: Final = 500
_SHA_PREFIX_CHARS: Final = 12
_GIT_OK: Final = 0
_TOUCHED_FILE: Final = "touched-files.txt"
_APPROVED_FILE: Final = "approved-extra.txt"

_STAGED_SET_REASON: Final = "staged_set"


def _capped(entries: Sequence[str]) -> list[str]:
    """``- <entry>`` lines, at most :data:`_MAX_LISTED`, then a remainder count."""
    lines = [f"- {entry}" for entry in entries[:_MAX_LISTED]]
    if len(entries) > _MAX_LISTED:
        lines.append(f"- ... and {len(entries) - _MAX_LISTED} more")
    return lines


# -- pre-turn guards -----------------------------------------------------------


def check_plan_present(plan_text: str | None, task: TicketTask) -> ImplBlock | None:
    """``plan_missing`` unless a plan exists or the row is an adopted repair entry."""
    if plan_text is not None or is_adopted_repair_entry(task):
        return None
    return ImplBlock(
        PLAN_MISSING,
        ".cw/plan.md is absent from the worktree and this row is not an adopted "
        "repair entry; cw synthesizes no plan (tracker-aware plan recovery is "
        "not available on the codex IMPL lane, #3530)",
    )


def check_existing_branch_work(facts: GitFacts, task: TicketTask) -> ImplBlock | None:
    """A fresh row whose branch is already ahead of its fork point is not fresh.

    Repair rows (``review_feedback`` set) legitimately start on a branch with
    commits, so they are never checked. An unresolvable fork point fails
    closed: cw cannot tell whether the branch carries prior work.
    """
    if task.review_feedback is not None:
        return None
    if not facts["fork_point"]:
        return ImplBlock(
            IMPL_FAILED,
            "the fork point against the default branch did not resolve, so cw "
            "cannot tell whether this branch already carries work",
        )
    if facts["commits"]:
        count = len(facts["commits"])
        return ImplBlock(
            IMPL_FAILED,
            f"existing_branch_work: the branch already has {count} commit(s) "
            f"ahead of its fork point {facts['fork_point'][:_SHA_PREFIX_CHARS]}; a "
            "fresh IMPL row starts from a branch with none. Inspect the branch "
            "(a failed earlier push leaves a local commit), then push or reset "
            "it before requeueing.",
        )
    return None


def check_on_branch(
    worktree: Path, expected: str, *, default_branch: str | None = None
) -> ImplBlock | None:
    """Block on a detached, mismatched, or configured default branch."""
    current = git_output(["branch", "--show-current"], cwd=worktree).strip()
    if not current:
        return ImplBlock(
            IMPL_FAILED,
            f"HEAD is detached; the IMPL turn must run on branch {expected!r}",
        )
    if default_branch is not None and default_branch in {current, expected}:
        return ImplBlock(
            IMPL_FAILED,
            "the IMPL turn cannot run on the configured default branch "
            f"{default_branch!r}",
        )
    if current != expected:
        return ImplBlock(
            IMPL_FAILED,
            f"the worktree is on branch {current!r}, but this row's branch is "
            f"{expected!r}",
        )
    return None


# -- gate 1 ------------------------------------------------------------------


def staged_totals(worktree: Path, base: str) -> tuple[int, int]:
    """``(files, lines)`` of the staged diff against *base*.

    The ``scope.files`` and ``scope.lines_actual`` of a post-turn park.
    """
    numstat = git_output(
        ["diff", "--cached", "--numstat", "--no-renames", base], cwd=worktree
    )
    return _parse_numstat_totals(numstat)


def check_empty_diff(worktree: Path, base: str) -> ImplBlock | None:
    """Gate 1: the staged net diff against the fork *base* must not be empty."""
    out = git_output(
        ["diff", "--cached", "--name-only", "--no-renames", base], cwd=worktree
    )
    if any(line for line in out.splitlines()):
        return None
    return ImplBlock(
        EMPTY_DIFF_BLOCKER_REASON,
        "the IMPL turn left no net change against the fork point "
        f"{base[:_SHA_PREFIX_CHARS]}: the staged diff is empty, so there is "
        "nothing to commit",
        status="empty_diff_blocked",
    )


# -- gate 2 ------------------------------------------------------------------


def _scope_script(worktree: Path) -> Path | None:
    """The scope-conformance script: repo-local first, then ``~/.claude/scripts``."""
    for root in (worktree, Path.home()):
        candidate = root / ".claude" / "scripts" / SCOPE_SCRIPT_NAME
        if candidate.is_file():
            return candidate
    return None


def _marker_block(script: Path) -> ImplBlock | None:
    """The strict ``cw-script-version`` marker rule; ``None`` when it holds."""
    try:
        head = script.read_text(encoding="utf-8").splitlines()[:_MARKER_HEAD_LINES]
    except (OSError, UnicodeDecodeError):
        head = []
    token = ""
    for line in head:
        match = _MARKER_LINE.fullmatch(line)
        if match is not None:
            token = match.group(1)
            break
    if _MARKER_TOKEN.fullmatch(token) and int(token) >= MIN_SCOPE_SCRIPT_VERSION:
        return None
    return ImplBlock(
        IMPL_FAILED,
        f"HEADLESS BLOCK: {script} has a missing or stale cw-script-version "
        f"marker (need >= {MIN_SCOPE_SCRIPT_VERSION} as a full "
        "`# cw-script-version: N` comment line in the first 5 lines); the "
        "scope-conformance script was not run",
    )


def approved_extra(worktree: Path, task: TicketTask) -> tuple[list[str], list[str]]:
    """``(approved extra files, friction)``, bound to the approval's head by ancestry.

    The operator's ``--scope-drift`` approval counts only while its recorded
    head is a full sha that is still an ancestor of ``HEAD``; otherwise the
    list is empty and a ``scope_drift_approval_stale`` friction says why.
    """
    approved = task.scope_drift_approved_extra_files
    if not approved:
        return [], []
    head = task.scope_drift_approved_head or ""
    if not _APPROVED_HEAD.fullmatch(head):
        return [], [f"{APPROVAL_STALE_PREFIX}the approval head {head!r} is not a sha"]
    result = run_git(
        ["merge-base", "--is-ancestor", head, "HEAD"],
        cwd=worktree,
        capture_output=True,
    )
    if result.returncode != _GIT_OK:
        why = f"the approved head {head[:_SHA_PREFIX_CHARS]} is not an ancestor of HEAD"
        return [], [f"{APPROVAL_STALE_PREFIX}{why}"]
    return sorted(approved), []


def repair_allowed_files(
    worktree: Path, *, fork_point: str, approved: Iterable[str]
) -> frozenset[str] | None:
    """Derive the bounded file fence for an adopted repair with no plan."""
    if not fork_point:
        return None
    try:
        names = git_output(
            ["diff", "--name-only", "--no-renames", f"{fork_point}...HEAD"],
            cwd=worktree,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    files = {line for line in names.splitlines() if line}
    files.update(approved)
    return frozenset(files) or None


def no_plan_sensitive_block(
    worktree: Path,
    *,
    touched: Iterable[str],
    allowed_files: frozenset[str],
    scope_tier: ScopeTier,
    default_branch: str,
) -> ImplBlock | None:
    """Run the shared sensitive-path check for a no-plan repair fence."""
    violations = _scope_violations(
        worktree,
        touched,
        allowed_files,
        scope_tier,
        default_branch,
    )
    if not violations:
        return None
    return fence_block(scope_violation_breach(violations, 0))


def _verdict(stdout: str) -> dict[str, Any] | None:
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _drift_block(verdict: dict[str, Any] | None, stderr: str) -> ImplBlock:
    """The exit-1 disposition: a valid verdict is drift, else a tooling failure."""
    if verdict is None or "triggered" not in verdict:
        tail = redact_and_cap(stderr.strip()[-_STDERR_TAIL_CHARS:])
        return ImplBlock(
            IMPL_FAILED,
            "the scope-conformance script exited 1 without a valid JSON verdict "
            f"(a tooling failure, not drift): {tail}",
        )
    extras = [redact_and_cap(str(path)) for path in verdict.get("extra_files") or []]
    allowed_extra = redact_and_cap(str(verdict.get("allowed_extra")))
    return ImplBlock(
        PLAN_SCOPE_DRIFT_BLOCKER_REASON,
        f"the delivered diff touches {len(extras)} file(s) beyond the plan's "
        f"Files Modified allowance (allowed extra: {allowed_extra}). "
        "extra_files:\n" + "\n".join(f"- {path}" for path in extras),
    )


def run_scope_conformance(
    worktree: Path,
    *,
    task: TicketTask,
    plan_text: str | None,
    plan_path: Path,
    touched: Iterable[str],
    scratch: Path,
) -> tuple[ImplBlock | None, list[str]]:
    """Gate 2: run the plan scope-conformance script on the turn's touched files.

    Returns ``(block, friction)``. A repair row with no plan (*plan_text*
    ``None``) skips the gate with friction. The script runs under
    ``sys.executable`` (it is stdlib-only, so no ``uv`` launch) with its two
    list files written under *scratch*, outside the worktree. Exit 0 passes
    (extras within the allowance add friction), exit 1 with a valid verdict is
    ``plan_scope_drift``, exit 2 is friction and never blocks, anything else
    is ``impl_failed``.
    """
    if plan_text is None:
        return None, [REPAIR_SKIP_FRICTION]
    script = _scope_script(worktree)
    if script is None:
        return None, [SCRIPT_ABSENT_FRICTION]
    stale = _marker_block(script)
    if stale is not None:
        return stale, []
    scratch.mkdir(parents=True, exist_ok=True)
    touched_file = scratch / _TOUCHED_FILE
    touched_file.write_text("\n".join(sorted(touched)) + "\n", encoding="utf-8")
    approved, friction = approved_extra(worktree, task)
    argv = [
        sys.executable,
        str(script),
        "--plan",
        str(plan_path),
        "--touched-files",
        str(touched_file),
    ]
    if approved:
        approved_file = scratch / _APPROVED_FILE
        approved_file.write_text("\n".join(approved) + "\n", encoding="utf-8")
        argv += ["--approved-extra-files", str(approved_file)]
    try:
        done = subprocess.run(
            argv,
            cwd=worktree,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return ImplBlock(
            IMPL_FAILED,
            "the scope-conformance script did not run: " + redact_and_cap(str(exc)),
        ), friction
    return _scope_outcome(done, friction)


def _scope_outcome(
    done: subprocess.CompletedProcess[str], friction: list[str]
) -> tuple[ImplBlock | None, list[str]]:
    verdict = _verdict(done.stdout)
    if done.returncode == _GIT_OK:
        extras = [
            redact_and_cap(str(p)) for p in (verdict or {}).get("extra_files") or []
        ]
        if extras:
            friction = [*friction, GROWTH_FRICTION_PREFIX + ", ".join(extras)]
        return None, friction
    if done.returncode == _EXIT_DRIFT:
        return _drift_block(verdict, done.stderr), friction
    if done.returncode == _EXIT_USAGE:
        note = UNPARSED_FRICTION_PREFIX + redact_and_cap(done.stderr.strip())
        return None, [*friction, note]
    tail = redact_and_cap(done.stderr.strip()[-_STDERR_TAIL_CHARS:])
    block = ImplBlock(
        IMPL_FAILED,
        f"the scope-conformance script exited {done.returncode}: {tail}",
    )
    return block, friction


# -- quality gates -------------------------------------------------------------


def run_quality_gates(
    plan: GatePlan,
    *,
    worktree: Path,
    baseline: CycleBaseline,
    scope: GateScope,
    tokens: list[str],
    touched: Iterable[str],
) -> ImplBlock | None:
    """Run the pinned repo gates on the staged changes; ``impl_failed`` on failure.

    The failure's ``output`` is already redacted and capped by the gate
    runner and is carried verbatim. Never ``codex_fix_gate_failed``: that is
    the REVIEW fix cycle's reason.
    """
    failure = run_cycle_gates(
        plan,
        worktree=worktree,
        review_artifacts=baseline.review_artifacts,
        scope=scope,
        tokens=tokens,
        touched=frozenset(touched),
    )
    if failure is None:
        return None
    if failure.kind == "unavailable":
        what = "the repo's quality gates could not be run: " + redact_and_cap(
            failure.why or "no reason recorded"
        )
    elif failure.kind == "red":
        what = (
            f"the repo's quality gate `{failure.gate}` failed with exit code "
            f"{failure.exit_code}"
        )
    else:
        what = (
            f"the repo's quality gate `{failure.gate}` passed but changed files "
            "(the gate dirtied the worktree)"
        )
    details = (
        f"the IMPL turn's changes failed a quality gate: {what}. "
        "Nothing was committed or pushed."
    )
    if failure.output:
        details += "\n\nLast lines of gate output:\n" + redact_and_cap(failure.output)
    return ImplBlock(IMPL_FAILED, details)


# -- breach and failure mappers --------------------------------------------------


def _bullets(breach: FenceBreach) -> list[str]:
    """The breach's own ``- ...`` detail lines, else its paths."""
    lines = [line for line in breach.details.splitlines() if line.startswith("- ")]
    return lines or _capped(breach.paths)


_FENCE_HEADLINES: Final[dict[str, str]] = {
    CODEX_FIX_SCOPE_DRIFT: (
        "the IMPL turn changed path(s) outside the plan's Files Modified "
        "manifest (plus any approved or merged files):"
    ),
    CODEX_FIX_REVERTED_BRANCH: (
        "the IMPL turn would restore the branch's changed files to their "
        "default-branch content (or empty the branch diff), undoing the work "
        "rather than adding to it:"
    ),
    CODEX_FIX_GROWTH_BUDGET: (
        "the IMPL repair turn added code beyond what the feedback justifies "
        "(the growth budget):"
    ),
    _STAGED_SET_REASON: (
        "the staged set does not equal the set of paths the turn was measured "
        "to touch (the turn may have committed or staged on its own); cw "
        "commits only measured paths:"
    ),
}


def fence_block(breach: FenceBreach) -> ImplBlock:
    """An IMPL block for a fence, revert, growth or staged-set breach.

    Keeps ``breach.reason`` (an existing vocabulary reason) and builds the
    text from it and the paths; ``recovery_hint`` and the REVIEW-worded
    ``details`` are never surfaced. The staged-set mismatch shares
    ``codex_fix_scope_drift`` with the fence but is not restorable.
    """
    key = breach.reason
    if key == CODEX_FIX_SCOPE_DRIFT and not breach.restorable:
        key = _STAGED_SET_REASON
    headline = _FENCE_HEADLINES.get(
        key, f"the IMPL turn was refused by the {breach.reason} guard:"
    )
    body = _bullets(breach) if key != _STAGED_SET_REASON else _capped(breach.paths)
    details = "\n".join(
        [
            headline,
            *body,
            "Nothing was committed or pushed.",
        ]
    )
    return ImplBlock(breach.reason, details)


def dirty_start_block(start: DirtyStart) -> ImplBlock:
    """The ``codex_fix_dirty_start`` block with IMPL text (no turn ran)."""
    sections = [
        "the worktree was not clean at IMPL start, so the turn's own edits "
        "could not be told apart from work already there. No turn ran, and cw "
        "staged, committed and pushed nothing."
    ]
    if start.markers:
        sections.append(
            "Unfinished operation(s):\n" + "\n".join(_capped(start.markers))
        )
    if start.unmerged:
        sections.append("Unmerged path(s):\n" + "\n".join(_capped(start.unmerged)))
    if start.dirty:
        sections.append(
            "Uncommitted changes (git status --porcelain):\n"
            + "\n".join(_capped(start.dirty))
        )
    return ImplBlock(CODEX_FIX_DIRTY_START, "\n\n".join(sections))


def commit_failure_block(exc: subprocess.CalledProcessError) -> ImplBlock:
    """A failed ``git commit``: a hook rejection or a plain git failure.

    *exc* is what :func:`~cw.codex_fix_loop.hook_failure.as_hook_failure`
    returned. A :class:`~cw.codex_fix_loop.hook_failure.CommitHookFailedError`
    parks ``codex_fix_hook_failed`` with the redacted, secret-withholding
    excerpt from ``hook_failure_breach``; anything else is ``codex_error``.
    """
    if isinstance(exc, CommitHookFailedError):
        breach = hook_failure_breach(exc, 0)
        details = (
            "the IMPL commit was rejected by a commit hook; cw runs no repair "
            f"turn for it. {breach.details}"
        )
        return ImplBlock(CODEX_FIX_HOOK_FAILED, details)
    tail = redact_and_cap(
        (exc.stderr or exc.stdout or "").strip()[-_STDERR_TAIL_CHARS:]
    )
    return ImplBlock(CODEX_ERROR, f"git commit failed (exit {exc.returncode}): {tail}")
