"""One fix cycle's invoke-scope-check-commit step for the codex fix loop.

Refuses to start a cycle on a dirty worktree (#2633,
:mod:`cw.codex_fix_loop.baseline`), builds the write-capable ``codex exec``
argv and the fix prompt (with the operator's binding constraints, #2633),
runs the fix invocation, measures the cycle against its clean-start baseline,
rejects a cycle whose changes touch a sensitive out-of-scope path, breach the
scope fence / revert guard (:mod:`cw.codex_fix_loop.fence`), or trip the
switchable growth and constraint guards (:mod:`cw.codex_fix_loop.growth`,
:mod:`cw.codex_fix_loop.constraints`), and commits (then pushes and verifies,
#2354) exactly the paths the cycle was measured to touch. A failure at any
step parks through the builders in :mod:`cw.codex_fix_loop.park`.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw._git import git_output, run_git
from cw.codex_fix_loop.baseline import (
    CycleBaseline,
    DirtyStart,
    capture_cycle_baseline,
    cycle_touched_paths,
)
from cw.codex_fix_loop.constraints import constraint_breach, render_section
from cw.codex_fix_loop.fence import (
    StagedSetMismatchError,
    check_fix_fence,
    cycle_start_breach,
    scope_violation_breach,
    staged_set_breach,
)
from cw.codex_fix_loop.growth import DEFAULT_GROWTH_BUDGET_LINES, check_growth_budget
from cw.codex_fix_loop.hook_failure import (
    CommitHookFailedError,
    as_hook_failure,
    hook_failure_breach,
)
from cw.codex_fix_loop.park import _park_fence_breach, _park_fix_failure
from cw.codex_fix_loop.push import push_and_verify_head
from cw.codex_review import (
    FIX_CYCLE_COMMIT_PREFIX,
    _classify_codex_failure,
    _load_sensitive_hits,
    _reasoning_effort_argv,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult, Review, ScopeTier
    from cw.codex_fix_loop.constraints import OperatorConstraints
    from cw.codex_fix_loop.convergence import _OpenFindingKey
    from cw.codex_fix_loop.fence import FenceBreach
    from cw.codex_fix_loop.snapshot import _PersistedSnapshot
    from cw.codex_review import _SensitiveHit
    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.executor_diagnostics import ExecutorFailureCategory
    from cw.models import TicketTask
    from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

    _Park = tuple[AutoDevResult, ReviewVerdict | None]

_log = logging.getLogger(__name__)


def _build_fix_codex_argv(
    *, model: str | None, reasoning_effort: str | None
) -> list[str]:
    """Return the ``codex exec`` argv for a fix invocation (write-capable).

    Structurally distinct from ``codex_review._build_generic_codex_argv``: it
    hardcodes ``--sandbox workspace-write`` (the fix edits files, so read-only
    would be wrong) and omits ``--output-schema``/``-o`` entirely — a fix
    invocation mutates the worktree, it does not emit a structured document.
    """
    argv = [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        *_reasoning_effort_argv(reasoning_effort),
    ]
    if model:
        argv += ["-m", model]
    return argv


# Standing fix-scope rules (#2485, #2492): every fix prompt carries them, so a
# reviewer finding of the form "this out-of-scope code is wrong" is resolved by
# removing that code rather than improving it, and a narrow finding never
# turns into a rewrite or a revert of the branch's earlier commits.
_FIX_SCOPE_RULES = (
    "## Scope Rules\n"
    "- If a finding says code is out of scope or not in the approved plan, "
    "resolve it by removing that code (restoring the file to its "
    "default-branch content, or deleting a file the branch added), never by "
    "improving it.\n"
    "- Do not revert, re-add, or rewrite work from earlier commits on this "
    "branch beyond what a finding explicitly requires.\n"
    "- Do not add new configuration flags, environment variables, or opt-in "
    "toggles unless a finding explicitly requires one."
)


def _render_allowed_files(allowed_files: frozenset[str]) -> str:
    """Render the scope fence as a prompt section (#2485)."""
    listing = "\n".join(f"- {path}" for path in sorted(allowed_files))
    return (
        "## Files You May Change\n"
        "Only these paths may differ from the default branch after your fix. "
        "A change to any other path is rejected and parks the ticket:\n"
        f"{listing}"
    )


def _build_fix_prompt(
    open_findings: list[Finding],
    *,
    plan_text: str | None,
    ticket_text: str | None,
    cycle: int,
    allowed_files: frozenset[str] | None = None,
    constraints_section: str | None = None,
) -> str:
    """Render the fix-invocation prompt for one cycle's open MUST_FIX findings.

    Only MUST_FIX findings ever reach the fix loop's ``open_findings`` tracker,
    so no severity filtering is needed here — every rendered finding is a
    MUST_FIX one. Plan/ticket context is inlined when present for the same
    reason the review path inlines it: a fix pass should read the same
    authoritative context regardless of runtime, not go hunting for ``.cw/*``.
    That is a consistency choice, NOT a capability workaround — this very
    function's invocation runs under ``--sandbox workspace-write`` (see
    :func:`_build_fix_codex_argv`), which by construction can reach the
    worktree (#1709). The scope rules and, when a plan manifest exists, the
    file fence (#2485) follow the context, so the fix sees the same limits
    :func:`~cw.codex_fix_loop.fence.check_fix_fence` enforces afterwards.
    ``constraints_section`` (#2633, the operator's binding resolutions) sits
    between the scope rules and the fence; without it the prompt is
    byte-identical to the pre-#2633 one.
    """
    parts = [
        f"# Codex Fix Cycle {cycle}",
        (
            "Resolve every MUST_FIX review finding listed below by making the "
            "minimal change on the current worktree, then stop. Do not refactor "
            "unrelated code and do not create a commit — cw commits your changes."
        ),
    ]
    if ticket_text:
        parts.append(f"## Ticket Context\n{ticket_text}")
    if plan_text:
        parts.append(f"## Approved Plan\n{plan_text}")
    parts.append(_FIX_SCOPE_RULES)
    if constraints_section is not None:
        parts.append(constraints_section)
    if allowed_files is not None:
        parts.append(_render_allowed_files(allowed_files))
    parts.append("## MUST_FIX Findings")
    for index, finding in enumerate(open_findings, start=1):
        loc = finding.file
        if finding.line_start is not None:
            loc = f"{loc}:{finding.line_start}"
        parts.append(
            f"### {index}. {loc}\n{finding.summary}\n\n"
            f"Suggested fix: {finding.suggested_fix}"
        )
    return "\n\n".join(parts)


def _fix_commit_summary(findings: list[Finding]) -> str:
    """Return a non-empty commit-message tail summarizing the cycle's findings."""
    count = len(findings)
    plural = "" if count == 1 else "s"
    return f"{count} MUST_FIX finding{plural}"


def _staged_paths(worktree: Path) -> frozenset[str]:
    """Return the paths ``git diff --cached --name-only --no-renames`` prints."""
    out = git_output(["diff", "--cached", "--name-only", "--no-renames"], cwd=worktree)
    return frozenset(line for line in out.splitlines() if line)


def _git_commit(worktree: Path, message: str) -> None:
    """Run ``git commit``, raising ``CalledProcessError`` with its output captured.

    ``git_output`` (``check_output``) never captures stderr, so a commit
    hook's lint output used to reach only the driver log (#2633).
    """
    argv = ["commit", "-m", message]
    result = run_git(argv, cwd=worktree, capture_output=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            ["git", *argv],
            output=result.stdout,
            stderr=result.stderr,
        )


def _commit_fix_cycle(
    worktree: Path,
    cycle: int,
    findings: list[Finding],
    *,
    measured_paths: frozenset[str],
) -> str | None:
    """Commit the worktree changes a fix cycle produced; return the new sha.

    A no-op fix (``git status --porcelain`` empty) is tolerated: no commit is
    created, a WARNING is logged, and ``None`` is returned — the cycle still
    counts toward the cap. ``git commit`` is retried exactly once, re-staging
    first, before giving up — any git failure surviving the retry raises
    ``CalledProcessError``, which the caller treats identically to a
    fix-invocation failure, or :class:`CommitHookFailedError` when a commit
    hook is installed (#2633; the caller parks ``codex_fix_hook_failed``).

    Commits only measured paths (#2633): after staging, the staged set must
    equal *measured_paths* (the set the cycle was measured to touch against
    its clean-start baseline), else :class:`StagedSetMismatchError` is raised
    and nothing is committed. A clean tree with a non-empty measured set (the
    fix invocation committed on its own) raises the same way. The check runs
    once, before the first commit attempt; the hook-rewrite retry re-stages
    what the hook rewrote and is not re-checked.

    A real commit is then pushed and its origin tip verified (#2354) via
    :func:`~cw.codex_fix_loop.push.push_and_verify_head`, so a fix cycle's
    work never exists only in the local worktree. A failed push or tip
    mismatch raises ``CalledProcessError`` too, and parks the same way.
    """
    status = git_output(["status", "--porcelain"], cwd=worktree)
    if not status.strip():
        if measured_paths:
            raise StagedSetMismatchError(measured_paths, frozenset())
        _log.warning(
            "codex fix cycle %d produced no changes; skipping commit "
            "(cycle still counts toward the cap)",
            cycle,
        )
        return None
    git_output(["add", "-A"], cwd=worktree)
    staged = _staged_paths(worktree)
    if staged != measured_paths:
        raise StagedSetMismatchError(measured_paths, staged)
    message = f"{FIX_CYCLE_COMMIT_PREFIX} {cycle} — {_fix_commit_summary(findings)}"
    try:
        _git_commit(worktree, message)
    except subprocess.CalledProcessError:
        # why: a repo-local pre-commit hook that REWRITES files (e.g.
        # ruff-format) exits non-zero on the run where it changes something —
        # standard "hook modified files, nothing was committed" behavior, not
        # a real failure. Without a re-stage-and-retry, that legitimate
        # rewrite looks identical to a genuine commit failure and strands a
        # correct, fully-written fix uncommitted in the worktree. Retried
        # exactly once: a second failure is a real error and must surface,
        # not be swallowed.
        _log.warning(
            "codex fix cycle %d commit failed (possible pre-commit hook "
            "rewrite); re-staging and retrying once",
            cycle,
        )
        git_output(["add", "-A"], cwd=worktree)
        try:
            _git_commit(worktree, message)
        except subprocess.CalledProcessError as exc:
            failure = as_hook_failure(worktree, exc, cycle)
            if failure is exc:
                raise
            raise failure from exc
    sha = git_output(["rev-parse", "HEAD"], cwd=worktree).strip()
    push_and_verify_head(worktree, sha)
    return sha


def _scope_violations(
    worktree: Path,
    paths: Iterable[str],
    cycle0_files: frozenset[str] | set[str],
    scope_tier: ScopeTier,
) -> list[_SensitiveHit]:
    """Return sensitive-registry hits among this cycle's out-of-scope paths.

    *paths* is the cycle's touched set, measured once against its clean-start
    baseline (#2633) — not ``git status`` against ``HEAD``, which read a
    staged merge's files as the cycle's own. A path is out of scope if it was
    not part of the cycle-0 reviewed diff's file set. Only out-of-scope paths
    are checked against the sensitive-files registry (via
    ``_load_sensitive_hits``, the single source of truth for that match) — an
    in-scope sensitive edit is always allowed, and an out-of-scope
    non-sensitive addition is always allowed. Both conditions must hold for a
    path to appear in the returned list.
    """
    out_of_scope = sorted(p for p in paths if p not in cycle0_files)
    if not out_of_scope:
        return []
    return _load_sensitive_hits(worktree, out_of_scope, scope_tier)


@dataclass(frozen=True)
class _CycleContext:
    """The per-cycle values every park builder needs, bundled once."""

    task: TicketTask
    worktree: Path
    session_id: str
    cycle: int
    cycle0_review: Review
    open_findings: dict[_OpenFindingKey, AcceptedFinding]
    verdict: ReviewVerdict
    snapshot: _PersistedSnapshot
    had_real_commit: bool
    reasoning_effort: str | None

    def park_breach(self, breach: FenceBreach) -> _Park:
        """Park the cycle uncommitted for a guard's *breach*."""
        return _park_fence_breach(
            task=self.task,
            worktree=self.worktree,
            session_id=self.session_id,
            cycle=self.cycle,
            breach=breach,
            cycle0_review=self.cycle0_review,
            open_findings=self.open_findings,
            verdict=self.verdict,
            snapshot=self.snapshot,
            had_real_commit=self.had_real_commit,
        )

    def park_failure(
        self,
        category: ExecutorFailureCategory,
        *,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        left_edits: bool = False,
    ) -> _Park:
        """Park the cycle on a failed fix invocation or git step."""
        return _park_fix_failure(
            task=self.task,
            worktree=self.worktree,
            session_id=self.session_id,
            cycle=self.cycle,
            category=category,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            verdict=self.verdict,
            snapshot=self.snapshot,
            reasoning_effort=self.reasoning_effort,
            left_edits=left_edits,
        )

    def park_git_error(self, exc: subprocess.CalledProcessError) -> _Park:
        """Park a git failure as ``runtime_error`` (``codex_error``)."""
        return self.park_failure(
            "runtime_error",
            stdout=exc.stdout or "",
            stderr=exc.stderr or str(exc),
            exit_code=exc.returncode,
        )


def _begin_cycle(ctx: _CycleContext) -> CycleBaseline | _Park:
    """Return the cycle's clean-start baseline, or the park refusing the cycle.

    Runs BEFORE the fix prompt is built or codex is invoked (#2633): a dirty
    tree parks ``codex_fix_dirty_start`` with nothing staged; a git failure
    while probing parks ``codex_error`` (fail closed).
    """
    try:
        start = capture_cycle_baseline(ctx.worktree)
    except subprocess.CalledProcessError as exc:
        return ctx.park_git_error(exc)
    if isinstance(start, DirtyStart):
        return ctx.park_breach(cycle_start_breach(start, ctx.cycle))
    return start


def _left_edits(worktree: Path) -> bool:
    """Whether a failed invocation left the tree dirty (unmeasurable → False).

    Uses the clean-start check itself, so "dirty" means exactly what would
    refuse the next cycle. A git failure here keeps the pre-#2633 park.
    """
    try:
        return isinstance(capture_cycle_baseline(worktree), DirtyStart)
    except subprocess.CalledProcessError:
        return False


def _park_invocation_failure(ctx: _CycleContext, result: CodexRunResult) -> _Park:
    """Park a failed fix invocation, noting edits it left behind (#2633)."""
    return ctx.park_failure(
        _classify_codex_failure(result),
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.returncode,
        left_edits=_left_edits(ctx.worktree),
    )


@dataclass(frozen=True)
class _GrowthGuards:
    """The switchable heuristic guards' settings for one run (#2633)."""

    enabled: bool
    budget_lines: int
    constraints: OperatorConstraints | None


def _growth_breach(
    ctx: _CycleContext,
    baseline: CycleBaseline,
    *,
    findings: list[Finding],
    allowed_files: frozenset[str] | None,
    guards: _GrowthGuards,
) -> FenceBreach | None:
    """Return the growth-budget or constraint breach, unless switched off.

    The growth budget runs only with a plan manifest (like the fence); the
    constraint check only when the operator posted binding constraints.
    """
    if not guards.enabled:
        return None
    if allowed_files is not None:
        breach = check_growth_budget(
            ctx.worktree,
            baseline,
            open_findings=findings,
            budget_lines=guards.budget_lines,
            cycle=ctx.cycle,
        )
        if breach is not None:
            return breach
    if guards.constraints is None:
        return None
    return constraint_breach(ctx.worktree, baseline, guards.constraints, ctx.cycle)


def _cycle_breach(
    ctx: _CycleContext,
    baseline: CycleBaseline,
    *,
    findings: list[Finding],
    cycle0_files: frozenset[str],
    scope_tier: ScopeTier,
    default_branch: str,
    allowed_files: frozenset[str] | None,
    guards: _GrowthGuards,
) -> tuple[FenceBreach | None, frozenset[str]]:
    """Measure the cycle once and return its first guard breach plus the paths.

    The sensitive-path scope violation is checked before the fence and revert
    guard, as before #2633; the switchable growth and constraint guards last.
    """
    touched = cycle_touched_paths(ctx.worktree, baseline)
    violations = _scope_violations(ctx.worktree, touched, cycle0_files, scope_tier)
    if violations:
        return scope_violation_breach(violations, ctx.cycle), frozenset(touched)
    breach = check_fix_fence(
        ctx.worktree,
        default_branch=default_branch,
        allowed_files=allowed_files,
        finding_files=frozenset(f.file for f in findings),
        cycle=ctx.cycle,
        touched=touched,
    )
    if breach is None:
        breach = _growth_breach(
            ctx, baseline, findings=findings, allowed_files=allowed_files, guards=guards
        )
    return breach, frozenset(touched)


def _park_commit_failure(
    ctx: _CycleContext,
    exc: subprocess.CalledProcessError | StagedSetMismatchError,
    baseline: CycleBaseline,
) -> _Park:
    """Park a failed commit step: staged-set mismatch, hook failure or git error."""
    if isinstance(exc, StagedSetMismatchError):
        breach = staged_set_breach(
            exc.measured, exc.staged, ctx.cycle, start_head=baseline.head_sha
        )
        return ctx.park_breach(breach)
    if isinstance(exc, CommitHookFailedError):
        return ctx.park_breach(hook_failure_breach(exc, ctx.cycle))
    return ctx.park_git_error(exc)


def _run_fix_and_commit(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    model: str | None,
    reasoning_effort: str | None,
    timeout_seconds: int | None,
    session_id: str,
    cycle: int,
    plan_text: str | None,
    ticket_text: str | None,
    verdict: ReviewVerdict,
    cycle0_files: frozenset[str],
    scope_tier: ScopeTier,
    cycle0_review: Review,
    snapshot: _PersistedSnapshot,
    had_real_commit_so_far: bool,
    default_branch: str,
    allowed_files: frozenset[str] | None,
    growth_budget_lines: int = DEFAULT_GROWTH_BUDGET_LINES,
    growth_guard_enabled: bool = True,
    constraints: OperatorConstraints | None = None,
) -> tuple[_Park | None, str | None]:
    """Run one cycle's fix invocation and commit; return ``(park, commit_sha)``.

    ``park`` is ``None`` iff the fix invocation and commit both succeeded —
    the caller should proceed to re-review, using ``commit_sha`` (the new
    commit sha, or ``None`` if the cycle's commit was a tolerated no-op) to
    update its cross-cycle real-commit tracker (#1723). A non-``None`` ``park``
    is the terminal park result for a dirty start (#2633), a failed fix
    invocation, a scope violation (an out-of-scope change that also matches
    the sensitive-files registry), a fence breach (#2485 scope fence, #2492
    revert guard), a growth-budget or operator-constraint breach (#2633,
    skipped when ``growth_guard_enabled`` is False), a staged-set mismatch, or
    a failed commit — ``commit_sha`` is always ``None`` alongside it.
    *constraints* also reach the fix prompt regardless of the switch.
    """
    ctx = _CycleContext(
        task=task,
        worktree=worktree,
        session_id=session_id,
        cycle=cycle,
        cycle0_review=cycle0_review,
        open_findings=open_findings,
        verdict=verdict,
        snapshot=snapshot,
        had_real_commit=had_real_commit_so_far,
        reasoning_effort=reasoning_effort,
    )
    baseline = _begin_cycle(ctx)
    if not isinstance(baseline, CycleBaseline):
        return baseline, None
    findings = [af.finding for af in open_findings.values()]
    prompt = _build_fix_prompt(
        findings,
        plan_text=plan_text,
        ticket_text=ticket_text,
        cycle=cycle,
        allowed_files=allowed_files,
        constraints_section=render_section(constraints),
    )
    argv = _build_fix_codex_argv(model=model, reasoning_effort=reasoning_effort)
    result = runner.run(worktree, argv, timeout_seconds, stdin=prompt)
    if result.timed_out or result.returncode != 0:
        return _park_invocation_failure(ctx, result), None
    breach, touched = _cycle_breach(
        ctx,
        baseline,
        findings=findings,
        cycle0_files=cycle0_files,
        scope_tier=scope_tier,
        default_branch=default_branch,
        allowed_files=allowed_files,
        guards=_GrowthGuards(growth_guard_enabled, growth_budget_lines, constraints),
    )
    if breach is not None:
        return ctx.park_breach(breach), None
    try:
        sha = _commit_fix_cycle(worktree, cycle, findings, measured_paths=touched)
    except (subprocess.CalledProcessError, StagedSetMismatchError) as exc:
        return _park_commit_failure(ctx, exc, baseline), None
    return None, sha
