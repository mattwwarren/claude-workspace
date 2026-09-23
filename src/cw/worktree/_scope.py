"""Branch-diff scope measurement and the #1487 scope-mismatch guard.

Measures a worktree's real ``git diff`` against ``origin/<default_branch>``
and corrects a self-reported :class:`~cw.auto_dev_result.AutoDevResult`
``scope`` against it.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, TypedDict

from cw.auto_dev_result import _PRE_IMPL_STAGES
from cw.config import load_effective_clients
from cw.worktree._git import _checked_out_branch, _run_git

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult

_log = logging.getLogger(__name__)


# git diff --numstat line shape: "<added>\t<removed>\t<file>".
_NUMSTAT_MIN_COLS = 3
# How far a self-reported scope number may drift from the measured one before
# the correction is worth a WARNING. Both-non-zero values within this ratio are
# corrected silently (rounding/whitespace-level noise); anything beyond it — and
# every zero-vs-non-zero pair, which has no meaningful ratio — is the #1393
# fabricated-scope class and gets logged. 2.0 is deliberately loose: the
# observed failure was ~6x on files and ~10x on lines.
_SCOPE_MISMATCH_RATIO_THRESHOLD = 2.0


def _has_commits_beyond_base(wt_path: Path, default_branch: str) -> bool:
    """Return True iff the worktree has commits beyond origin/<default_branch>.

    Runs git log origin/<default_branch>..HEAD in the worktree cwd. Returns False on
    any failure — conservative default so uncertainty never triggers salvage.

    # Why: salvage is a side-effecting external write. A false positive
    # (salvaging a session with no real commits) is worse than a false
    # negative (missing a salvageable session). Fail safe to False.
    """
    if not wt_path.exists():
        return False
    try:
        result = _run_git(
            "log",
            f"origin/{default_branch}..HEAD",
            "--oneline",
            cwd=wt_path,
            check=False,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def _parse_numstat_totals(numstat_output: str) -> tuple[int, int]:
    """Return ``(files, lines)`` totals parsed from ``git diff --numstat`` output.

    *lines* is added + removed. Binary files (rendered as ``-\t-\tpath``) are
    skipped entirely — they have no line count, and counting them as a changed
    file while contributing zero lines would make the two totals disagree about
    the same diff.

    Shared by :func:`compute_branch_diff_scope` and ``local_runner._git_facts``
    so every producer of ``scope.files`` / ``scope.lines_actual`` counts the
    same way (#1487).
    """
    files = 0
    lines = 0
    for line in numstat_output.splitlines():
        parts = line.split("\t")
        if len(parts) >= _NUMSTAT_MIN_COLS:
            with contextlib.suppress(ValueError):  # binary files show '-'
                lines += int(parts[0]) + int(parts[1])
                files += 1
    return files, lines


class _BranchDiffScope(TypedDict):
    branch: str
    merge_base: str
    files: int
    lines_actual: int


def _resolve_merge_base(worktree_path: Path, default_branch: str) -> str | None:
    """Return the merge-base of ``origin/<default_branch>`` and HEAD, or None."""
    try:
        base = _run_git(
            "merge-base",
            f"origin/{default_branch}",
            "HEAD",
            cwd=worktree_path,
            check=False,
        )
    except OSError:
        return None
    if base.returncode != 0:
        return None
    return base.stdout.strip() or None


def compute_branch_diff_scope(
    worktree_path: Path, default_branch: str
) -> _BranchDiffScope | None:
    """Measure a worktree's real diff scope against ``origin/<default_branch>``.

    Resolves the merge-base fresh on every call, so the answer is immune to the
    stale-merge-base inflation that produced #1393's 18-files/1567-lines
    self-report for a 3-file/152-line branch.

    Returns ``None`` — never raises — when the measurement cannot be trusted:
    the path is absent, HEAD is detached or the path is not a worktree,
    ``origin/<default_branch>`` is unresolvable, or git itself cannot run.
    Callers must treat ``None`` as "unverifiable", not as "zero".
    """
    branch = _checked_out_branch(worktree_path) if worktree_path.exists() else None
    if branch is None:
        return None
    merge_base = _resolve_merge_base(worktree_path, default_branch)
    if merge_base is None:
        return None
    try:
        numstat = _run_git(
            "diff",
            "--numstat",
            f"{merge_base}..HEAD",
            cwd=worktree_path,
            check=False,
        )
    except OSError:
        return None
    if numstat.returncode != 0:
        return None
    files, lines_actual = _parse_numstat_totals(numstat.stdout)
    return _BranchDiffScope(
        branch=branch,
        merge_base=merge_base,
        files=files,
        lines_actual=lines_actual,
    )


def _scope_mismatch_is_gross(reported: int, measured: int) -> bool:
    """Return True when *reported* diverges from *measured* enough to warn."""
    if reported == measured:
        return False
    if reported == 0 or measured == 0:
        return True
    return max(reported, measured) / min(reported, measured) > (
        _SCOPE_MISMATCH_RATIO_THRESHOLD
    )


def _reconcile_scope_field(
    field: str,
    *,
    reported: int,
    measured: int,
    result: AutoDevResult,
    measured_branch: str,
) -> int | None:
    """Return the corrected value for one scope field, or None when it already agrees.

    Logs one WARNING per grossly-divergent field. The self-reported vs. measured
    branch names ride along as cross-check context: a self-report measured in the
    wrong tree is the usual root cause, and the operator reading the log needs to
    see it. The branch itself is never written back — this guard owns
    ``scope.files``/``scope.lines_actual`` only.
    """
    if reported == measured:
        return None
    if _scope_mismatch_is_gross(reported, measured):
        _log.warning(
            "scope_mismatch_corrected: ticket=%s field=scope.%s reported=%d "
            "measured=%d (self-reported branch=%s, measured branch=%s)",
            result.ticket_id,
            field,
            reported,
            measured,
            result.branch,
            measured_branch,
        )
    return measured


def reconcile_result_scope(
    result: AutoDevResult,
    *,
    worktree_path: Path | None,
    default_branch: str,
) -> AutoDevResult:
    """Correct a self-reported ``scope`` against the worktree's real git diff (#1487).

    Producers of an :class:`AutoDevResult` report ``scope.files`` /
    ``scope.lines_actual`` from their own accounting, which can be fabricated or
    computed against a stale merge-base (#1393). This measures the branch
    independently via :func:`compute_branch_diff_scope` and overwrites both
    fields when they disagree, WARNING on a gross divergence.

    Fail-open by design — the result is returned unchanged when there is nothing
    to measure against (a pre-impl exit, no worktree, or an unverifiable git
    state). Overwrite-with-WARNING rather than a hard block is safe because no
    downstream consumer gates on these fields: ``dispatch.routing`` reads only
    ``scope.tier``.
    """
    if result.stage_reached in _PRE_IMPL_STAGES or worktree_path is None:
        return result
    measured = compute_branch_diff_scope(worktree_path, default_branch)
    if measured is None:
        _log.warning(
            "scope_verification_unavailable: ticket=%s worktree=%s could not be "
            "measured against origin/%s; self-reported files=%s lines_actual=%s "
            "left uncorrected",
            result.ticket_id,
            worktree_path,
            default_branch,
            result.scope.files,
            result.scope.lines_actual,
        )
        return result

    updates: dict[str, int] = {}
    corrected_files = _reconcile_scope_field(
        "files",
        reported=result.scope.files,
        measured=measured["files"],
        result=result,
        measured_branch=measured["branch"],
    )
    if corrected_files is not None:
        updates["files"] = corrected_files
    # lines_actual is None only at pre-impl stages, which returned above; the
    # guard keeps the arithmetic total-typed rather than asserting the invariant.
    reported_lines = result.scope.lines_actual
    if reported_lines is not None:
        corrected_lines = _reconcile_scope_field(
            "lines_actual",
            reported=reported_lines,
            measured=measured["lines_actual"],
            result=result,
            measured_branch=measured["branch"],
        )
        if corrected_lines is not None:
            updates["lines_actual"] = corrected_lines

    if not updates:
        return result
    return result.model_copy(update={"scope": result.scope.model_copy(update=updates)})


def resolve_scope_guard_default_branch(client_name: str, *, log_context: str) -> str:
    """Resolve *client_name*'s ``default_branch`` for scope verification (#1487).

    Falls back to ``"main"`` on any resolution failure — the client key is
    absent from an otherwise-valid ``clients.yaml`` (a silent ``dict.get()``
    miss) and a config-load failure (malformed YAML, unreadable file, etc. —
    any exception from :func:`cw.config.load_effective_clients`) are both
    logged identically here, so every scope-guard caller gets the same
    diagnostic breadcrumb regardless of which failure occurred. This guard
    must never cost a caller its sentinel over a client-config problem.
    """
    try:
        client_cfg = load_effective_clients().get(client_name)
    except Exception:  # noqa: BLE001 — fail-safe: any config-load error falls back to "main"
        client_cfg = None
    if client_cfg is not None:
        return client_cfg.default_branch
    _log.warning(
        "scope_verification_client_unresolved: %s client=%s; measuring against 'main'",
        log_context,
        client_name,
    )
    return "main"
