"""One fix cycle's invoke-scope-check-commit step for the codex fix loop.

Builds the write-capable ``codex exec`` argv and the fix prompt, runs the fix
invocation, rejects a cycle whose changes touch a sensitive out-of-scope path,
and commits (then pushes and verifies, #2354) whatever the cycle produced. A
failure at any step parks through the builders in :mod:`cw.codex_fix_loop.park`.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from cw._git import git_output
from cw.codex_fix_loop.park import _park_fix_failure, _park_scope_violation
from cw.codex_fix_loop.push import push_and_verify_head
from cw.codex_review import (
    _classify_codex_failure,
    _load_sensitive_hits,
    _reasoning_effort_argv,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult, Review, ScopeTier
    from cw.codex_fix_loop.convergence import _OpenFindingKey
    from cw.codex_fix_loop.snapshot import _PersistedSnapshot
    from cw.codex_review import _SensitiveHit
    from cw.codex_runner import CodexRunner
    from cw.models import TicketTask
    from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

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


def _build_fix_prompt(
    open_findings: list[Finding],
    *,
    plan_text: str | None,
    ticket_text: str | None,
    cycle: int,
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
    worktree (#1709). The prompt ends with an explicit minimal-fix instruction.
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


def _commit_fix_cycle(
    worktree: Path, cycle: int, findings: list[Finding]
) -> str | None:
    """Commit the worktree changes a fix cycle produced; return the new sha.

    A no-op fix (``git status --porcelain`` empty) is tolerated: no commit is
    created, a WARNING is logged, and ``None`` is returned — the cycle still
    counts toward the cap. ``git commit`` is retried exactly once, re-staging
    first, before giving up — any git failure surviving the retry raises
    ``CalledProcessError``, which the caller treats identically to a
    fix-invocation failure.

    A real commit is then pushed and its origin tip verified (#2354) via
    :func:`~cw.codex_fix_loop.push.push_and_verify_head`, so a fix cycle's
    work never exists only in the local worktree. A failed push or tip
    mismatch raises ``CalledProcessError`` too, and parks the same way.
    """
    status = git_output(["status", "--porcelain"], cwd=worktree)
    if not status.strip():
        _log.warning(
            "codex fix cycle %d produced no changes; skipping commit "
            "(cycle still counts toward the cap)",
            cycle,
        )
        return None
    git_output(["add", "-A"], cwd=worktree)
    message = f"fix(review): codex fix cycle {cycle} — {_fix_commit_summary(findings)}"
    try:
        git_output(["commit", "-m", message], cwd=worktree)
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
        git_output(["commit", "-m", message], cwd=worktree)
    sha = git_output(["rev-parse", "HEAD"], cwd=worktree).strip()
    push_and_verify_head(worktree, sha)
    return sha


def _porcelain_changed_paths(worktree: Path) -> list[str]:
    """Return every path with a pending change per ``git status --porcelain``.

    Rename-aware: a porcelain rename line (``R  old -> new``) contributes only
    the post-``->`` (destination) path. Untracked (``??``) and
    modified/added/deleted paths are all included via the same fixed-offset
    slice — porcelain v1's two-character status code is always followed by a
    single space, so the path always starts at index 3 regardless of which
    status letters precede it. ``--untracked-files=all`` is required so a
    wholly-new directory is reported as its individual file paths rather than
    collapsed to a single ``?? some/dir/`` entry — the scope/sensitivity check
    below needs the actual file path, not its containing directory.
    """
    status = git_output(
        ["status", "--porcelain", "--untracked-files=all"], cwd=worktree
    )
    paths: list[str] = []
    for line in status.splitlines():
        if not line:
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry)
    return paths


def _scope_violations(
    worktree: Path,
    cycle0_files: frozenset[str] | set[str],
    scope_tier: ScopeTier,
) -> list[_SensitiveHit]:
    """Return sensitive-registry hits among this cycle's out-of-scope paths.

    A path is out of scope if it was not part of the cycle-0 reviewed diff's
    file set. Only out-of-scope paths are checked against the sensitive-files
    registry (via ``_load_sensitive_hits``, the single source of truth for
    that match) — an in-scope sensitive edit is always allowed, and an
    out-of-scope non-sensitive addition is always allowed. Both conditions
    must hold for a path to appear in the returned list.
    """
    out_of_scope = [
        p for p in _porcelain_changed_paths(worktree) if p not in cycle0_files
    ]
    if not out_of_scope:
        return []
    return _load_sensitive_hits(worktree, out_of_scope, scope_tier)


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
) -> tuple[tuple[AutoDevResult, ReviewVerdict | None] | None, str | None]:
    """Run one cycle's fix invocation and commit; return ``(park, commit_sha)``.

    ``park`` is ``None`` iff the fix invocation and commit both succeeded —
    the caller should proceed to re-review, using ``commit_sha`` (the new
    commit sha, or ``None`` if the cycle's commit was a tolerated no-op) to
    update its cross-cycle real-commit tracker (#1723). A non-``None`` ``park``
    is the terminal park result for a failed fix invocation, a scope violation
    (an out-of-scope change that also matches the sensitive-files registry),
    or a failed commit — ``commit_sha`` is always ``None`` alongside it.
    """
    findings = [af.finding for af in open_findings.values()]
    prompt = _build_fix_prompt(
        findings, plan_text=plan_text, ticket_text=ticket_text, cycle=cycle
    )
    argv = _build_fix_codex_argv(model=model, reasoning_effort=reasoning_effort)
    result = runner.run(worktree, argv, timeout_seconds, stdin=prompt)
    if result.timed_out or result.returncode != 0:
        return (
            _park_fix_failure(
                task=task,
                worktree=worktree,
                session_id=session_id,
                cycle=cycle,
                category=_classify_codex_failure(result),
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                verdict=verdict,
                snapshot=snapshot,
                reasoning_effort=reasoning_effort,
            ),
            None,
        )
    violations = _scope_violations(worktree, cycle0_files, scope_tier)
    if violations:
        return (
            _park_scope_violation(
                task=task,
                worktree=worktree,
                session_id=session_id,
                cycle=cycle,
                violations=violations,
                cycle0_review=cycle0_review,
                open_findings=open_findings,
                verdict=verdict,
                snapshot=snapshot,
                had_real_commit=had_real_commit_so_far,
            ),
            None,
        )
    try:
        sha = _commit_fix_cycle(
            worktree=worktree,
            cycle=cycle,
            findings=findings,
        )
    except subprocess.CalledProcessError as exc:
        return (
            _park_fix_failure(
                task=task,
                worktree=worktree,
                session_id=session_id,
                cycle=cycle,
                category="runtime_error",
                stdout=exc.stdout or "",
                stderr=exc.stderr or str(exc),
                exit_code=exc.returncode,
                verdict=verdict,
                snapshot=snapshot,
                reasoning_effort=reasoning_effort,
            ),
            None,
        )
    return None, sha
