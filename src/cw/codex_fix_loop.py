"""Codex fix-loop adapter for CodexExecutor's REVIEW stage (#1392).

Wraps :func:`cw.codex_review.run_review` in a bounded fix loop: after an
initial (cycle 0) review pass that surfaces blocking MUST_FIX findings, cw runs
up to :data:`_MAX_FIX_CYCLES` cycles of ``codex exec --sandbox workspace-write``
fix invocations, committing each cycle's real changes and re-running the full
per-role review pass to see which findings cleared. The loop exits clean the
moment no MUST_FIX finding remains open, or parks the ticket when the cap (or the
shared wall-clock budget) is exhausted.

This is the multi-pass counterpart to ``run_review``'s single pass (#1236) built
on the executor-neutral finding contract (#1237). ``CodexExecutor.spawn()``'s
Step 3 delegates to :func:`run_review_with_fix_loop` instead of ``run_review``.

Every cycle's full ``ReviewVerdict`` (findings intact) is persisted under the
diagnostics bundle dir as it completes, and a pointer naming that cycle's
specific snapshot FILE is threaded into ``friction_highlights`` on every exit
path, so whichever cycle's verdict actually produced the terminal disposition —
not just cycle 0's — stays discoverable from the sentinel (#1485, #1739, #1763).

The pointer is an out-of-band signal, so the snapshots also carry an in-band
one: each per-cycle persist stamps ``is_terminal_snapshot=False`` (a fix-loop
persist is never final at the moment it is written), and each true exit path
re-writes exactly the one file its returned ``Blocker.details`` was rendered
from with ``is_terminal_snapshot=True`` (#1763). An operator reading a snapshot
straight off disk can then tell whether its ``rejected_must_fix`` is the set the
reported blocker cites, instead of assuming cycle 0's file is authoritative and
reading a legitimately-empty one (#1729). Two exit paths deliberately finalize
nothing: a mechanically-rejected cycle-0 verdict never enters the loop (no
snapshot is ever written), and an unparseable rereview's park details come from
``_format_failures_detail`` rather than any persisted verdict.

Cross-cycle finding identity is tracked by ``review_debt.fingerprint_v1``
(#1837) so a finding that survives every cycle — or flaps out and back, or
gets re-raised a few lines further down after a fix moved it — is counted
exactly once. From cycle 1 onward the re-review covers only the delta since
the previous reviewed head, and the admission gate in
``codex_fix_loop_convergence`` decides which newly-appearing MUST_FIX findings
this cycle actually caused; the rest are recorded in the verdict's debt ledger
rather than restarting the loop.

The terminal published ``Review`` is reconstructed here rather than read
from any single ``derive_review_counts`` call: ``must_fix_initial`` is cycle 0's
pre-defer snapshot, ``deferred`` is the cross-cycle survivor count, and
``fix_cycles_used`` is the loop's own cycle counter — three values no single
formula pass over one loop-exit-state finding list can produce together.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import TYPE_CHECKING, NamedTuple

from cw.codex_fix_loop_convergence import (
    _OpenFindingKey,
    _survivors_only_verdict,
    _track_open_findings,
)
from cw.codex_review import (
    _CATEGORY_TO_REASON,
    _MIN_ROLE_TIMEOUT_SECONDS,
    _TRANSIENT_FAILURE_REASONS,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_FIX_SCOPE_VIOLATION,
    CODEX_MUST_FIX_FINDINGS,
    _capture_diff,
    _classify_codex_failure,
    _load_sensitive_hits,
    _load_ticket_context,
    _prepare_review_pass,
    make_codex_blocked,
    render_verdict_comment,
    run_codex_roles,
    run_review,
    synthesize_codex_review_result,
)
from cw.executor_diagnostics import (
    append_diagnostics_pointer,
    build_executor_failure,
    diagnostics_bundle_dir,
    persist_diagnostics_bundle,
)
from cw.local_runner import resolve_tier
from cw.review_debt import dedupe_debt
from cw.review_findings import write_review_verdict

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult, Health, Review, ScopeTier
    from cw.codex_review import _SensitiveHit
    from cw.codex_review._context import _ReviewPassInputs
    from cw.codex_runner import CodexRunner
    from cw.executor_diagnostics import ExecutorFailureCategory
    from cw.models import TicketTask
    from cw.review_findings import (
        AcceptedFinding,
        DebtRecord,
        Finding,
        ReviewVerdict,
    )

_log = logging.getLogger(__name__)

# Maximum fix cycles attempted before parking a still-blocking review.
_MAX_FIX_CYCLES = 5
# Cycle at/after which Health.fix_loop_escalated is set — a loop that needed
# this many passes is operator-attention-worthy even when it eventually clears.
_ESCALATE_AT_CYCLE = 3
# Coarse per-fix-cycle wall-clock floor: a cycle needs at least one fix
# invocation plus one re-review role turn, so it must be able to afford two
# per-role floors. Never start a fix cycle with less remaining budget.
_FIX_CYCLE_FLOOR_SECONDS = 2 * _MIN_ROLE_TIMEOUT_SECONDS

_MUST_FIX = "MUST_FIX"


def _verdict_snapshot_filename(cycle: int) -> str:
    """Return the persisted-verdict filename for *cycle* (0 = the initial pass)."""
    return f"cycle{cycle}-review-verdict.json"


class _PersistedSnapshot(NamedTuple):
    """The latest persisted per-cycle verdict snapshot.

    Carries the ``friction_highlights`` *pointer* alongside the *cycle* index
    it was written for, so an exit path can re-open that exact file to stamp
    the terminal marker (#1763) instead of re-deriving the cycle from loop
    state that has already moved on.
    """

    pointer: str
    cycle: int


def _persist_cycle_snapshot(
    verdict: ReviewVerdict, *, session_id: str, cycle: int
) -> _PersistedSnapshot:
    """Persist *cycle*'s full verdict (findings intact) and return a pointer
    to it plus the cycle it was written for.

    The persisted copy is always stamped ``is_terminal_snapshot=False``
    explicitly rather than inheriting the model default: a fix-loop persist is
    by construction not final at the moment it is written, since the loop may
    still run another cycle. Terminality is stamped later, by
    :func:`_finalize_snapshot`, from the exit path that actually knows the
    disposition. The caller's in-memory *verdict* is never mutated.

    Mirrors ``persist_diagnostics_bundle``'s never-raise contract: a write
    failure is logged and swallowed rather than blocking the fix loop.
    """
    bundle = diagnostics_bundle_dir(session_id)
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        write_review_verdict(
            verdict.model_copy(update={"is_terminal_snapshot": False}),
            bundle / _verdict_snapshot_filename(cycle),
        )
    except OSError:
        _log.warning(
            "cycle-%d findings snapshot write failed for session %s",
            cycle,
            session_id,
        )
    pointer = append_diagnostics_pointer(
        f"cycle-{cycle} MUST_FIX findings snapshot persisted "
        f"({_verdict_snapshot_filename(cycle)})",
        session_id=session_id,
    )
    return _PersistedSnapshot(pointer=pointer, cycle=cycle)


def _finalize_snapshot(verdict: ReviewVerdict, *, session_id: str, cycle: int) -> None:
    """Re-persist *cycle*'s snapshot marked as this session's terminal one.

    Called from each true fix-loop exit path with the SAME verdict object that
    the returned ``Blocker.details``/``AutoDevResult`` is derived from — before
    any exit-path rewrite of ``verdict.review``, so the file on disk keeps
    agreeing with the persist that produced it and differs from its
    intermediate version in exactly one field.

    Same never-raise contract as :func:`_persist_cycle_snapshot`.
    """
    try:
        write_review_verdict(
            verdict.model_copy(update={"is_terminal_snapshot": True}),
            diagnostics_bundle_dir(session_id) / _verdict_snapshot_filename(cycle),
        )
    except OSError:
        _log.warning(
            "cycle-%d terminal findings snapshot write failed for session %s",
            cycle,
            session_id,
        )


def _with_snapshot_pointer(highlights: list[str], snapshot_pointer: str) -> list[str]:
    """Append *snapshot_pointer* to a copy of *highlights*."""
    return [*highlights, snapshot_pointer]


def _build_fix_codex_argv(*, model: str | None) -> list[str]:
    """Return the ``codex exec`` argv for a fix invocation (write-capable).

    Structurally distinct from ``codex_review._build_generic_codex_argv``: it
    hardcodes ``--sandbox workspace-write`` (the fix edits files, so read-only
    would be wrong) and omits ``--output-schema``/``-o`` entirely — a fix
    invocation mutates the worktree, it does not emit a structured document.
    """
    argv = ["codex", "exec", "--sandbox", "workspace-write"]
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
    """
    status = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=worktree, text=True
    )
    if not status.strip():
        _log.warning(
            "codex fix cycle %d produced no changes; skipping commit "
            "(cycle still counts toward the cap)",
            cycle,
        )
        return None
    subprocess.check_output(["git", "add", "-A"], cwd=worktree, text=True)
    message = f"fix(review): codex fix cycle {cycle} — {_fix_commit_summary(findings)}"
    try:
        subprocess.check_output(
            ["git", "commit", "-m", message], cwd=worktree, text=True
        )
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
        subprocess.check_output(["git", "add", "-A"], cwd=worktree, text=True)
        subprocess.check_output(
            ["git", "commit", "-m", message], cwd=worktree, text=True
        )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
    ).strip()


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
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=worktree,
        text=True,
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


def _finalize_review(
    *,
    cycle0_review: Review,
    final_verdict: ReviewVerdict,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    cycle_count: int,
    had_real_commit: bool,
) -> Review:
    """Reconstruct the terminal published ``Review`` from authoritative sources.

    ``should_fix`` and ``agents_run`` are taken from the final cycle's own
    correctly-derived ``Review``. ``must_fix_initial`` comes from cycle 0's
    snapshot (captured before any defer stamping, so trivially correct).
    ``deferred`` is the cross-cycle survivor count. ``fix_cycles_used`` is the
    loop's own cycle counter — set explicitly here rather than inherited from
    ``final_verdict.review`` because ``synthesize_codex_review_result`` does not
    thread the cycle index through its internal ``consolidate_verdict`` call.
    ``had_real_commit`` is the loop's OR-across-cycles real-commit tracker
    (#1723) — true iff at least one fix cycle actually committed a change.
    """
    return final_verdict.review.model_copy(
        update={
            "must_fix_initial": cycle0_review.must_fix_initial,
            "deferred": len(open_findings),
            "fix_cycles_used": cycle_count,
            "had_real_commit": had_real_commit,
        }
    )


def _apply_escalation(health: Health, cycle: int) -> Health:
    """Return *health* with ``fix_loop_escalated`` set when at/past the threshold."""
    if cycle >= _ESCALATE_AT_CYCLE:
        return health.model_copy(update={"fix_loop_escalated": True})
    return health


def _remaining_budget(deadline: float | None) -> float | None:
    """Return seconds left before *deadline*, or ``None`` for an unlimited run."""
    return None if deadline is None else deadline - time.monotonic()


def _budget_seconds(remaining: float | None) -> int | None:
    """Coerce a float remaining-budget into the int ``run_codex_roles`` expects."""
    return None if remaining is None else max(int(remaining), 0)


def _fix_timeout(remaining: float | None) -> int | None:
    """Per-fix-invocation timeout, floored at the per-role minimum."""
    return None if remaining is None else max(int(remaining), _MIN_ROLE_TIMEOUT_SECONDS)


def _park_fix_failure(
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    cycle: int,
    category: ExecutorFailureCategory,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    verdict: ReviewVerdict | None,
    snapshot: _PersistedSnapshot,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Park the ticket on a failed fix invocation, persisting a diagnostics bundle.

    Reuses ``codex_review``'s category→reason map and transient-reason set so a
    timeout parks retry-eligible while a hard error parks for the operator, and
    writes the typed ``ExecutorFailure`` bundle under ``reviewer_role`` =
    ``fix-cycle-N`` (mirroring ``_persist_codex_role_diagnostics``).

    Why: unlike ``_clean_exit``/``_park_scope_violation``, this function does
    NOT stamp a finalized ``review`` onto the returned ``verdict`` (#1705) —
    it never calls ``_finalize_review`` and has no ``cycle0_review``/
    ``open_findings`` in scope to build one from. Enriching it would need a
    signature change plus updates to both call sites in
    ``_run_fix_and_commit``, which exceeds #1705's one-line-stamp scope; the
    operator explicitly deferred it as a candidate fast-follow ticket rather
    than expanding that diff (see #1705 Decisions #2).
    """
    if verdict is not None:
        _finalize_snapshot(verdict, session_id=session_id, cycle=snapshot.cycle)
    reason = _CATEGORY_TO_REASON[category]
    failure = build_executor_failure(
        category=category,
        executor_name="codex",
        session_id=session_id,
        argv=_build_fix_codex_argv(model=None),
        stdout_excerpt=stdout,
        stderr_excerpt=stderr,
        reviewer_role=f"fix-cycle-{cycle}",
        exit_code=exit_code,
    )
    persist_diagnostics_bundle(
        session_id=session_id, role_slug=f"fix-cycle-{cycle}", failure=failure
    )
    detail = append_diagnostics_pointer(
        f"codex fix cycle {cycle} failed ({reason})", session_id=session_id
    )
    transient = reason in _TRANSIENT_FAILURE_REASONS
    blocked = make_codex_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=reason,
        details=detail,
        retry_eligible=True if transient else None,
    )
    blocked = blocked.model_copy(
        update={
            "friction_highlights": _with_snapshot_pointer(
                blocked.friction_highlights, snapshot.pointer
            )
        }
    )
    return blocked, verdict


def _park_survivors(
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    reason: str,
    verdict: ReviewVerdict,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    cycle0_review: Review,
    cycle_count: int,
    retry_eligible: bool | None,
    snapshot: _PersistedSnapshot,
    had_real_commit: bool,
) -> tuple[AutoDevResult, ReviewVerdict]:
    """Park a still-blocking review (cap or budget exhausted) with survivor detail.

    Builds the terminal ``Review`` and the survivor-only verdict, renders the
    verdict comment into ``Blocker.details``, and sets ``fix_loop_escalated`` on
    the health block when the cycle count reached the escalation threshold.

    Finalizes the persisted snapshot from the ORIGINAL *verdict* argument, not
    the ``survivors`` object rebuilt below: ``_survivors_only_verdict``'s update
    dict never touches ``rejected``/``rejected_must_fix``, so the two agree on
    the field #1763 is about, and the file on disk stays the one
    ``_persist_cycle_snapshot`` wrote rather than a loop-exit reconstruction.
    """
    _finalize_snapshot(verdict, session_id=session_id, cycle=snapshot.cycle)
    review = _finalize_review(
        cycle0_review=cycle0_review,
        final_verdict=verdict,
        open_findings=open_findings,
        cycle_count=cycle_count,
        had_real_commit=had_real_commit,
    )
    survivors = _survivors_only_verdict(verdict, open_findings, review)
    blocked = make_codex_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=reason,
        # Literal True: reached only after the fix loop has actually engaged
        # (survivors are cross-cycle-tracked open findings), so the fix loop
        # was, by construction, enabled for this run.
        details=render_verdict_comment(survivors, fix_loop_enabled=True),
        retry_eligible=retry_eligible,
    )
    health = blocked.health.model_copy(
        update={"fix_loop_escalated": cycle_count >= _ESCALATE_AT_CYCLE}
    )
    patched = blocked.model_copy(
        update={
            "review": review,
            "health": health,
            "friction_highlights": _with_snapshot_pointer(
                blocked.friction_highlights, snapshot.pointer
            ),
        }
    )
    return patched, survivors


def _park_scope_violation(
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    cycle: int,
    violations: list[_SensitiveHit],
    cycle0_review: Review,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    verdict: ReviewVerdict,
    snapshot: _PersistedSnapshot,
    had_real_commit: bool,
) -> tuple[AutoDevResult, ReviewVerdict]:
    """Park a fix cycle whose commit would touch a sensitive out-of-scope path.

    The gate is AND-only: ``_scope_violations`` only ever returns hits already
    computed over the out-of-scope subset, so both conditions (out of the
    cycle-0 reviewed diff's scope, and a sensitive-registry match) hold for
    every listed path — the details string says so explicitly rather than
    leaving it implicit. Follows ``_park_survivors``'s pattern verbatim:
    reconstruct the terminal ``Review`` via ``_finalize_review``, build the
    ``Blocker`` via ``make_blocked``, then patch review/health onto the
    result. ``had_real_commit`` is the pre-this-cycle OR-across-cycles
    real-commit tracker (#1723) — this cycle's own commit never landed (that
    is why it is being parked), so the caller's already-accumulated value is
    what is forwarded, not a fresh computation.

    ORDERING (#1763): the snapshot is finalized FIRST, from the verdict as
    persisted, because the rebind below replaces ``verdict.review`` with the
    loop's reconstructed cross-cycle ``Review``. Finalizing after the rebind
    would rewrite the on-disk file's ``review`` block with counts the
    intermediate persist never had.
    """
    _finalize_snapshot(verdict, session_id=session_id, cycle=snapshot.cycle)
    review = _finalize_review(
        cycle0_review=cycle0_review,
        final_verdict=verdict,
        open_findings=open_findings,
        cycle_count=cycle,
        had_real_commit=had_real_commit,
    )
    verdict = verdict.model_copy(update={"review": review})
    lines = [f"- {hit.path} ({hit.category}): {hit.reason}" for hit in violations]
    details = "\n".join(
        [
            f"codex fix cycle {cycle} touched path(s) that are both out of the "
            "cycle-0 reviewed diff's scope AND match the sensitive-files "
            "registry:",
            *lines,
        ]
    )
    blocked = make_codex_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=CODEX_FIX_SCOPE_VIOLATION,
        details=details,
        retry_eligible=None,
    )
    health = blocked.health.model_copy(
        update={"fix_loop_escalated": cycle >= _ESCALATE_AT_CYCLE}
    )
    patched = blocked.model_copy(
        update={
            "review": review,
            "health": health,
            "friction_highlights": _with_snapshot_pointer(
                blocked.friction_highlights, snapshot.pointer
            ),
        }
    )
    return patched, verdict


def _clean_exit(
    result: AutoDevResult,
    verdict: ReviewVerdict,
    cycle0_review: Review,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    cycle: int,
    snapshot: _PersistedSnapshot,
    session_id: str,
    had_real_commit: bool,
) -> tuple[AutoDevResult, ReviewVerdict]:
    """Return the clean-exit result with the terminal review + escalation patched.

    Stamps the finalized ``review`` onto the returned *verdict* too (not just
    the returned ``AutoDevResult``) — #1705 bug #2: without this, the
    ``ReviewVerdict`` that reaches ``render_verdict_comment`` at the
    executor's Step 4b still carries the terminal ``_rereview()`` pass's own
    ``fix_cycles_used=0``, numerically indistinguishable from a genuinely
    clean first pass.

    "Clean" is a pre-existing misnomer for one branch this function also
    serves: a cycle-N rereview that mechanically-rejects a MUST_FIX with zero
    other open findings arrives here already blocked
    (``codex_must_fix_mechanically_rejected``), because ``blocking`` is False
    while ``rejected_must_fix`` is not (#1714/#1729). That is exactly the case
    #1763's terminal marker exists for, so the snapshot is finalized here —
    BEFORE the ``verdict.review`` rebind below, for the same reason spelled out
    in :func:`_park_scope_violation`.
    """
    _finalize_snapshot(verdict, session_id=session_id, cycle=snapshot.cycle)
    review = _finalize_review(
        cycle0_review=cycle0_review,
        final_verdict=verdict,
        open_findings=open_findings,
        cycle_count=cycle,
        had_real_commit=had_real_commit,
    )
    verdict = verdict.model_copy(update={"review": review})
    health = _apply_escalation(result.health, cycle)
    patched = result.model_copy(
        update={
            "review": review,
            "health": health,
            "friction_highlights": _with_snapshot_pointer(
                result.friction_highlights, snapshot.pointer
            ),
        }
    )
    return patched, verdict


def _run_fix_and_commit(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    model: str | None,
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
    argv = _build_fix_codex_argv(model=model)
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
            ),
            None,
        )
    return None, sha


def _rereview(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    remaining: float | None,
    session_id: str,
    previous_reviewed_sha: str,
    prior_open_findings: list[Finding],
) -> tuple[AutoDevResult, ReviewVerdict | None, _ReviewPassInputs]:
    """Run a per-role review pass over the delta since the last cycle (#1837).

    Reviews ``previous_reviewed_sha..HEAD``, not the whole branch: a full
    rescan surfaces fresh findings on code no fix cycle touched, which is the
    treadmill this ticket exists to stop. ``prior_open_findings`` is inlined
    so the reviewers still see what remains unresolved from earlier cycles.

    Returns the prepared inputs alongside the usual pair — the caller needs
    this cycle's ``delta_diff``/``delta_changed_files`` to run the admission
    gate, and they are captured here.
    """
    prepared = _prepare_review_pass(
        task,
        worktree,
        default_branch,
        runner=runner,
        session_id=session_id,
        delta_from_sha=previous_reviewed_sha,
        prior_open_findings=prior_open_findings,
    )
    documents, failures, metrics_by_role = run_codex_roles(
        runner=runner,
        worktree=worktree,
        roles=prepared.roles,
        prompts_by_role=prepared.prompts_by_role,
        model=model,
        wall_clock_budget_seconds=_budget_seconds(remaining),
        session_id=session_id,
    )
    result, verdict = synthesize_codex_review_result(
        task=task,
        worktree=worktree,
        documents=documents,
        failures=failures,
        diff=prepared.delta_diff if prepared.delta_diff is not None else prepared.diff,
        reviewed_sha=prepared.reviewed_sha,
        session_id=session_id,
        default_branch=default_branch,
        # Literal True: _rereview is only ever called from inside the
        # already-entered fix loop (run_review_with_fix_loop's for-loop).
        fix_loop_enabled=True,
        metrics_by_role=metrics_by_role,
        capability=prepared.capability,
        agent_spec_status=prepared.agent_spec_status,
        # #1814: re-fetched and re-applied every cycle, not carried over from
        # cycle 0 — an operator can void a finding mid-loop, and a fix cycle
        # can rewrite the code out from under a void's content anchor.
        voided_findings=prepared.voided_findings,
    )
    if verdict is not None:
        verdict = verdict.model_copy(
            update={"previous_reviewed_sha": previous_reviewed_sha}
        )
    return result, verdict, prepared


def _stamp_debt(
    verdict: ReviewVerdict, debt_ledger: dict[tuple[str, str], DebtRecord]
) -> ReviewVerdict:
    """Stamp the run's accumulated debt onto *verdict*.

    Applied after every tracker update so whichever exit path returns this
    verdict carries the ledger as of that moment — the loop has several exits
    and threading a stamp onto each one separately is how one gets missed.
    """
    return verdict.model_copy(update={"debt": dedupe_debt(list(debt_ledger.values()))})


def run_review_with_fix_loop(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
    fix_loop_enabled: bool,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Run the initial review pass plus a bounded MUST_FIX fix loop.

    Drop-in replacement for :func:`cw.codex_review.run_review` (identical
    signature and return shape — both now take ``fix_loop_enabled``, though
    this function's own semantics extend beyond just threading it through to
    the renderer: it also gates whether the fix loop itself engages). One
    shared wall-clock deadline spans the initial pass, every fix invocation,
    and every re-review. A non-blocking or unparseable cycle-0 verdict passes
    straight through with zero fix invocations attempted. When
    ``fix_loop_enabled`` is False and cycle 0 blocks, returns cycle 0's tuple
    unchanged with zero fix cycles attempted.
    """
    deadline = (
        None
        if wall_clock_budget_seconds is None
        else time.monotonic() + wall_clock_budget_seconds
    )
    result, verdict = run_review(
        runner=runner,
        task=task,
        worktree=worktree,
        default_branch=default_branch,
        model=model,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        session_id=session_id,
        fix_loop_enabled=fix_loop_enabled,
    )
    if verdict is None or not verdict.blocking or not fix_loop_enabled:
        return result, verdict

    cycle0_review = verdict.review
    _, _, cycle0_changed = _capture_diff(worktree, default_branch)
    cycle0_files = frozenset(cycle0_changed)
    scope_tier = resolve_tier(task.scope_hint)
    plan_text, ticket_text = _load_ticket_context(worktree)
    # #1837: one ledger object threaded through every cycle, accumulating the
    # findings the loop records instead of acting on.
    debt_ledger: dict[tuple[str, str], DebtRecord] = {}
    open_findings = _track_open_findings(
        {},
        verdict.accepted,
        # Cycle 0 has no prior head to restrict a "genuinely new" finding
        # against, so the admission gate never runs on this call and every
        # MUST_FIX the full-PR pass found still blocks.
        delta_diff=None,
        delta_changed_files=None,
        debt_ledger=debt_ledger,
        previous_reviewed_sha=None,
        reviewed_sha=verdict.reviewed_sha,
        worktree=worktree,
        ticket_id=task.ticket_id,
    )
    verdict = _stamp_debt(verdict, debt_ledger)
    snapshot = _persist_cycle_snapshot(verdict, session_id=session_id, cycle=0)
    # #1723: true iff at least one fix cycle so far produced a real commit
    # (OR'd across cycles) — distinguishes a genuine fix from a fix loop
    # that converged purely because every cycle's fix invocation was a no-op.
    had_real_commit = False

    for cycle in range(1, _MAX_FIX_CYCLES + 1):
        remaining = _remaining_budget(deadline)
        if remaining is not None and remaining < _FIX_CYCLE_FLOOR_SECONDS:
            return _park_survivors(
                task=task,
                worktree=worktree,
                session_id=session_id,
                reason=CODEX_BUDGET_EXHAUSTED,
                verdict=verdict,
                open_findings=open_findings,
                cycle0_review=cycle0_review,
                cycle_count=cycle - 1,
                retry_eligible=True,
                snapshot=snapshot,
                had_real_commit=had_real_commit,
            )
        park, commit_sha = _run_fix_and_commit(
            runner=runner,
            task=task,
            worktree=worktree,
            open_findings=open_findings,
            model=model,
            timeout_seconds=_fix_timeout(remaining),
            session_id=session_id,
            cycle=cycle,
            plan_text=plan_text,
            ticket_text=ticket_text,
            verdict=verdict,
            cycle0_files=cycle0_files,
            scope_tier=scope_tier,
            cycle0_review=cycle0_review,
            snapshot=snapshot,
            had_real_commit_so_far=had_real_commit,
        )
        if park is not None:
            return park
        had_real_commit = had_real_commit or commit_sha is not None
        previous_reviewed_sha = verdict.reviewed_sha
        result, verdict, prepared = _rereview(
            runner=runner,
            task=task,
            worktree=worktree,
            default_branch=default_branch,
            model=model,
            remaining=_remaining_budget(deadline),
            session_id=session_id,
            previous_reviewed_sha=previous_reviewed_sha,
            prior_open_findings=[af.finding for af in open_findings.values()],
        )
        if verdict is None:
            # No cycle-N snapshot was persisted (the persist call below is
            # never reached) and this park's details come from
            # `_format_failures_detail`, not from any persisted verdict — so
            # nothing is finalized here (#1763).
            return (
                result.model_copy(
                    update={
                        "friction_highlights": _with_snapshot_pointer(
                            result.friction_highlights, snapshot.pointer
                        )
                    }
                ),
                None,
            )
        open_findings = _track_open_findings(
            open_findings,
            verdict.accepted,
            delta_diff=prepared.delta_diff,
            delta_changed_files=prepared.delta_changed_files,
            debt_ledger=debt_ledger,
            previous_reviewed_sha=previous_reviewed_sha,
            reviewed_sha=verdict.reviewed_sha,
            worktree=worktree,
            ticket_id=task.ticket_id,
        )
        verdict = _stamp_debt(verdict, debt_ledger)
        snapshot = _persist_cycle_snapshot(verdict, session_id=session_id, cycle=cycle)
        if not open_findings:
            return _clean_exit(
                result,
                verdict,
                cycle0_review,
                open_findings,
                cycle,
                snapshot,
                session_id=session_id,
                had_real_commit=had_real_commit,
            )

    return _park_survivors(
        task=task,
        worktree=worktree,
        session_id=session_id,
        reason=CODEX_MUST_FIX_FINDINGS,
        verdict=verdict,
        open_findings=open_findings,
        cycle0_review=cycle0_review,
        cycle_count=_MAX_FIX_CYCLES,
        retry_eligible=None,
        snapshot=snapshot,
        had_real_commit=had_real_commit,
    )
