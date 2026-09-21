"""Git worktree operations for isolated session workspaces."""

from __future__ import annotations

import contextlib
import enum
import hashlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, TypedDict, assert_never

from cw.auto_dev_result import _PRE_IMPL_STAGES
from cw.config import load_effective_clients, load_state
from cw.events import record_event
from cw.exceptions import (
    BranchHeldByWorktreeError,
    MissingWorkspaceError,
    StaleWorktreeError,
    WorktreeError,
    WorktreeOccupiedError,
)
from cw.models import OrchestratorEventType, SessionStatus
from cw.native_daemon import get_native_daemon_client

if TYPE_CHECKING:
    from cw.auto_dev_result import AutoDevResult
    from cw.models import ClientConfig, TicketTask

_log = logging.getLogger(__name__)


# The native spawn backend (``spawn_create_impl`` / ``claude --bg`` with
# ``cwd=``) has no path-length restriction beyond OS limits (PATH_MAX ~4096).
# The 64-char threshold is a conservative trigger: for any realistic workspace
# path the default candidate exceeds this cap, so the hash-fallback base
# (``~/.cw/wt/``) is used in practice — keeping paths short and predictable.
_WORKTREE_NAME_CAP = 64
_HASH_BASE_SEGMENTS = (".cw", "wt")
# Git porcelain v1 format: "XY path" — 2-char status prefix + 1 space = 3 chars.
_GIT_PORCELAIN_PATH_OFFSET = 3
# XY field value for untracked files in porcelain v1.
_GIT_PORCELAIN_UNTRACKED = "??"
# 8 hex chars = 32 bits. For a single-user tool with a handful of
# clients the collision probability is negligible; raising this value
# pushes the hashed base closer to _WORKTREE_NAME_CAP and reduces
# headroom for the branch slug, so increase with care.
_WORKSPACE_HASH_CHARS = 8
# Pattern appended to $GIT_COMMON_DIR/info/exclude so ephemeral per-session
# .cw/ artifacts are invisible to git status without touching .gitignore.
_CW_EXCLUDE_PATTERN = ".cw/"
# cw-managed per-session scratch files live under this prefix. They are written
# fresh each spawn and must not count as real uncommitted work.
# Mirrored in worktree_gc._CW_SCRATCH_PREFIX (duplicated per D5 to avoid
# importing private names cross-module).
_CW_SCRATCH_PREFIX = ".claude/"
# git diff --numstat line shape: "<added>\t<removed>\t<file>".
_NUMSTAT_MIN_COLS = 3
# How far a self-reported scope number may drift from the measured one before
# the correction is worth a WARNING. Both-non-zero values within this ratio are
# corrected silently (rounding/whitespace-level noise); anything beyond it — and
# every zero-vs-non-zero pair, which has no meaningful ratio — is the #1393
# fabricated-scope class and gets logged. 2.0 is deliberately loose: the
# observed failure was ~6x on files and ~10x on lines.
_SCOPE_MISMATCH_RATIO_THRESHOLD = 2.0
# Matches git's "fatal: '<branch>' is already used by worktree at '<path>'"
# line so create_worktree can name the colliding worktree in a targeted error
# (#2034) instead of surfacing git's bare stderr.
_WORKTREE_HELD_BY_RE = re.compile(r"already used by worktree at '([^']+)'")
# Why: git's stderr when ``fetch origin <branch>`` names a branch the remote
# does not have. A never-pushed feature branch is an expected state (a PLAN
# stage provisions the worktree before IMPL ever pushes), so
# ``fetch_feature_branch`` downgrades exactly this failure to DEBUG (#2213).
# git localizes its messages: under a non-English locale the marker misses and
# the failure degrades to the ordinary WARNING -- noise only, never a bug.
_MISSING_REMOTE_REF_MARKER = "couldn't find remote ref"
# Abbreviated-SHA width for fast-forward log lines.
_SHA_LOG_CHARS = 12


def slugify_branch(branch: str) -> str:
    """Convert a branch name to a worktree-safe slug.

    Collapses any run of disallowed characters into a single hyphen, then
    strips leading/trailing hyphens. The allowed charset (``[A-Za-z0-9._-]``)
    matches ``claude -w``'s worktree-name validator — anything outside this
    set (path separators, ``#``, spaces, unicode) becomes ``-``.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")


def _git_dir(client: ClientConfig) -> Path:
    """Return the directory to use as git cwd for a client.

    Worktree-mode clients use ``repo_path`` (the real clone);
    legacy clients use ``workspace_path``.
    """
    return client.repo_path or client.workspace_path


def resolve_worktree_base(client: ClientConfig) -> Path:
    """Return the worktree base directory for a client.

    Uses ``client.worktree_base`` if set, otherwise defaults to
    ``<git_dir.parent>/.worktrees/<git_dir.name>``.
    """
    if client.worktree_base is not None:
        return client.worktree_base
    ws = _git_dir(client)
    return ws.parent / ".worktrees" / ws.name


def effective_worktree_bases(client: ClientConfig) -> frozenset[Path]:
    """Return all directories that may contain cw-managed worktrees for *client*.

    ``worktree_path_for`` silently redirects to a hash-derived base under
    ``~/.cw/wt/`` when the default sibling path would exceed
    ``_WORKTREE_NAME_CAP``. GC must search *both* to avoid silently skipping
    worktrees created when that fallback was in effect.

    When ``client.worktree_base`` is set explicitly only that directory is
    returned — the user chose a location and there is no hash fallback.
    """
    if client.worktree_base is not None:
        return frozenset({client.worktree_base})
    return frozenset({resolve_worktree_base(client), _hashed_worktree_base(client)})


def _hashed_worktree_base(client: ClientConfig) -> Path:
    """Return a short hash-derived worktree base for a client.

    Used as a fallback when the default sibling layout would exceed
    ``_WORKTREE_NAME_CAP``. The hash seeds from the *resolved* git
    directory so symlinks and non-canonical paths collapse to the
    same digest — ``create_worktree`` and ``remove_worktree`` must
    agree on the location across invocations.
    """
    git_dir = _git_dir(client).resolve()
    digest = hashlib.sha256(str(git_dir).encode("utf-8")).hexdigest()
    return Path.home().joinpath(*_HASH_BASE_SEGMENTS, digest[:_WORKSPACE_HASH_CHARS])


def worktree_path_for(client: ClientConfig, branch: str) -> Path:
    """Return the full worktree path for a branch.

    Falls back to a hash-derived short base under ``~/.cw/wt/`` when the
    default layout would produce a path longer than the 64-char path-length
    threshold. An explicit ``client.worktree_base`` is always honoured, even
    if it produces a path over the threshold — user choice wins over the
    safety net.
    """
    slug = slugify_branch(branch)
    base = resolve_worktree_base(client)
    candidate = base / slug
    if client.worktree_base is not None or len(str(candidate)) <= _WORKTREE_NAME_CAP:
        return candidate
    return _hashed_worktree_base(client) / slug


def _run_git(
    *args: str,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given directory.

    Strips ``GIT_*`` from the environment so cw's git operations target
    the client repo at *cwd* and never inherit a parent process's repo
    selection. Without this, running cw from inside a git hook (e.g. a
    pre-commit pytest run) would leak ``GIT_DIR`` / ``GIT_INDEX_FILE``
    into the subprocess and produce confusing "Not a directory" errors.
    """
    cmd = ["git", *args]
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            cwd=str(cwd),
            env=clean_env,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else str(e)
        msg = f"Git command failed: {' '.join(cmd)}\n{stderr}"
        raise WorktreeError(msg) from e


def _ref_exists(ref: str, git_cwd: Path) -> bool:
    """Return True if *ref* resolves to a valid object in *git_cwd*."""
    result = _run_git("rev-parse", "--verify", ref, cwd=git_cwd, check=False)
    return result.returncode == 0


def check_not_main_checkout(worktree_path: Path, client: ClientConfig) -> None:
    """Raise WorktreeError if *worktree_path* resolves to the client's main checkout.

    Guards against the #300 regression: a degenerate path where a worktree
    resolves to the main checkout, causing git commits to land there instead of
    the intended branch worktree.  Uses Path.resolve() to catch symlinks.
    """
    main_checkout = _git_dir(client)
    if worktree_path.resolve() == main_checkout.resolve():
        msg = (
            f"Refusing to operate on main checkout: worktree path {worktree_path} "
            f"resolves to the same location as the client's main checkout "
            f"({main_checkout}). A prior 'git worktree add' likely targeted "
            f"the main repo directory instead of a new branch worktree."
        )
        raise WorktreeError(msg)


def _checked_out_branch(wt_path: Path) -> str | None:
    """Return the branch checked out in *wt_path*, or None.

    None means *wt_path* is not a registered git worktree or is in
    detached-HEAD state (``git branch --show-current`` prints nothing or
    exits non-zero), or git itself could not be invoked. Never raises — the
    idempotent-reuse guard in :func:`create_worktree` treats every None as a
    refuse-to-reuse signal, so swallowing an ``OSError`` here (e.g. a missing
    git binary) is correct: the worktree cannot be trusted either way.
    """
    try:
        result = _run_git("branch", "--show-current", cwd=wt_path, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def resolve_task_worktree(
    task: TicketTask, client_cfg: ClientConfig | None
) -> Path | None:
    """Resolve the on-disk worktree for *task*, or None (#2123).

    ``task.worktree_path`` wins when stamped (USER-origin rows, tests). It is
    ``None`` for every dispatch-driven row -- dispatch stamps ``worktree_path``
    on the Session, never the TicketTask (see ``queue_peek.py``) -- so any
    consumer that reads only that field sees ``None`` on the dominant
    production path. Fall back to the branch-derived worktree
    :func:`worktree_path_for` computes for the feature branch, using the same
    read-only primitives ``create_worktree`` consults to decide reuse, and
    trust it only when the checked-out branch matches: a stale or foreign
    checkout must not lend its state to this ticket.

    Lives here rather than in either consumer because both
    ``dev_queue.lifecycle._local_plan_path`` and
    ``dispatch.review_gates._should_gate_for_review_staleness`` need it, and
    neither package may import the other. Returning the worktree *directory*
    (not a file under it) is what lets the two consumers ask different
    questions of the same resolution.
    """
    if task.worktree_path is not None:
        return task.worktree_path
    if client_cfg is None:
        return None
    branch = f"{client_cfg.feature_branch_prefix}/{task.ticket_id}"
    wt_path = worktree_path_for(client_cfg, branch)
    if not wt_path.exists() or _checked_out_branch(wt_path) != branch:
        return None
    return wt_path


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


def _register_cw_exclude(git_cwd: Path) -> None:
    """Idempotently append .cw/ to $GIT_COMMON_DIR/info/exclude.

    Uses git rev-parse --git-common-dir so the write targets the shared
    object-store directory even when called from within a worktree. Never
    touches the committed .gitignore. Logs a warning and returns on any
    git or I/O failure rather than propagating — exclude registration is
    advisory and must not abort worktree creation.
    """
    try:
        result = _run_git("rev-parse", "--git-common-dir", cwd=git_cwd)
        common_dir_str = result.stdout.strip()
        if not common_dir_str:
            _log.warning(
                "_register_cw_exclude: empty --git-common-dir output in %s", git_cwd
            )
            return
        common_dir = (
            Path(common_dir_str)
            if Path(common_dir_str).is_absolute()
            else git_cwd / common_dir_str
        )
        exclude_path = common_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text() if exclude_path.exists() else ""
        if _CW_EXCLUDE_PATTERN in existing.splitlines():
            return
        separator = "" if not existing or existing.endswith("\n") else "\n"
        with exclude_path.open("a") as fh:
            fh.write(f"{separator}{_CW_EXCLUDE_PATTERN}\n")
    except (WorktreeError, OSError) as exc:
        _log.warning("_register_cw_exclude: failed for %s: %s", git_cwd, exc)


def _resolve_branch_start_point(client: ClientConfig, git_cwd: Path) -> str:
    """Resolve the start-point for a new branch in *client*'s repository.

    Three-level fallback matching the convention in ``_unpushed_commits_detail``:

    1. ``origin/<default_branch>`` — authoritative remote ref; independent of
       the operator's current checkout.
    2. ``<default_branch>`` — local fallback for offline / bare-clone scenarios.
    3. Raise :exc:`WorktreeError` — never fall back to HEAD, which is exactly
       the bug this prevents (#710).
    """
    origin_ref = f"origin/{client.default_branch}"
    result = _run_git("rev-parse", "--verify", origin_ref, cwd=git_cwd, check=False)
    if result.returncode == 0:
        # Why: origin/<default_branch> is already current because dispatch's freshness
        # gate fetched it earlier this tick; interactive cw start accepts a
        # possibly-one-fetch-stale origin ref — better than HEAD-based base.
        return origin_ref

    local_ref = client.default_branch
    result = _run_git("rev-parse", "--verify", local_ref, cwd=git_cwd, check=False)
    if result.returncode == 0:
        return local_ref

    msg = (
        f"Cannot resolve a start-point for new branch in {client.name}: "
        f"neither {origin_ref!r} nor {local_ref!r} exists. "
        f"Ensure the repository has a remote or local {local_ref!r} branch."
    )
    raise WorktreeError(msg)


def _parse_worktree_holder_path(message: str) -> Path | None:
    """Extract the colliding worktree's path from a git worktree-add failure.

    Returns ``None`` when *message* does not match the known
    "already used by worktree at '<path>'" shape — callers must treat that as
    "not this specific collision" and re-raise the original error unchanged.
    """
    match = _WORKTREE_HELD_BY_RE.search(message)
    return Path(match.group(1)) if match else None


def _branch_held_error(
    client: ClientConfig, branch: str, holder: Path
) -> BranchHeldByWorktreeError:
    """Build the diagnostic for a foreign worktree already holding *branch* (#2034).

    Names the holder, states whether it looks clean or dirty (per
    :func:`worktree_has_unsaved_work`'s ``wt_path`` override), and gives the
    exact removal command either way — never removes the holder itself.
    """
    if worktree_has_unsaved_work(client, branch, wt_path=holder):
        state_msg = (
            "has uncommitted or unpushed changes; verify before removing "
            f"with `git worktree remove {holder}`"
        )
    else:
        state_msg = (
            f"appears clean; safe to remove with `git worktree remove {holder}` "
            "if it is no longer needed"
        )
    msg = (
        f"Branch {branch!r} is already checked out in another worktree at "
        f"{holder}. This is likely an orphaned harness agent worktree "
        f"(see #2017) that cw did not create and cannot verify is finished "
        f"with. It was NOT removed automatically. That worktree {state_msg}."
    )
    return BranchHeldByWorktreeError(msg, holder_path=holder)


class FetchOutcome(enum.Enum):
    """What one ``git fetch origin <branch>`` established about the remote (#2213).

    Three states, because the two non-success outcomes need opposite handling:

    - ``FETCHED``: the tracking ref is fresh, so it is safe to fast-forward from.
    - ``BRANCH_ABSENT``: origin has no such branch. An expected, benign state for
      a fresh ticket (a PLAN stage provisions the worktree before IMPL ever
      pushes): there is nothing to refresh and nothing worth reporting.
    - ``FAILED``: the fetch itself failed (offline, auth, no ``origin``, missing
      workspace). The tracking ref is stale or unknown, so it must NOT be
      fast-forwarded from, and the failure is worth reporting.
    """

    FETCHED = "fetched"
    BRANCH_ABSENT = "branch_absent"
    FAILED = "failed"


@dataclass(frozen=True)
class FetchResult:
    """One ``git fetch origin <branch>``: what happened (:class:`FetchOutcome`) and why.

    ``reason`` is ``None`` for ``FETCHED``. For ``BRANCH_ABSENT`` and ``FAILED`` it
    is one line: git's exit status and first stderr line (``rc=128: fatal: ...``),
    or the workspace / OS problem that stopped git from running. That first line
    is what tells an operator an auth failure (``Permission denied``) from a
    network one (``Could not resolve hostname``) from a missing remote (``does
    not appear to be a git repository``), so a "not refreshed" is legible
    (#2213).
    """

    outcome: FetchOutcome
    reason: str | None = None


# One distinct fetch failure: ``(client name, outcome, reason)``. The caller-owned
# dedup set of these (``warned_fetch_fail``) is what lets a repeat of the SAME
# failure stay quiet while a DIFFERENT one for the same client -- an auth error
# after a network error -- still gets reported (#2213).
type FetchWarningKey = tuple[str, FetchOutcome, str]


class RefreshOutcome(enum.Enum):
    """What the reuse refresh did with a reused worktree, in the caller's terms (#2213).

    The refresh can decline to move a worktree for two reasons that call for
    OPPOSITE handling, and one flag used to carry both. They are separate
    members so a caller cannot conflate them:

    - ``REFRESHED``: fast-forwarded to a freshly fetched ``origin/<branch>``.
    - ``NOT_REFRESHED``: the tree is the caller's to use, it is just not (known
      to be) up to date -- dirty, diverged from origin, git refused the
      fast-forward, the fetch failed, the remote branch is absent, or it already
      matches origin. **Proceed with it.**
    - ``OCCUPIED_BY_LIVE_SESSION``: a live cw session in persisted state, a live
      daemon-roster worker, or an INDETERMINATE read of either (fail closed, an
      un-normalizable path included) means another worker may be operating in
      the tree. **Every caller that would spawn into it, dispatch against it or
      mutate it must abort.** ``create_worktree`` does not return this; it
      raises :exc:`~cw.exceptions.WorktreeOccupiedError` instead, so the refusal
      cannot be ignored.

    Anything that dispatches on this enum does so exhaustively (``match`` with
    ``assert_never``), so a new member is a type error rather than a silent
    "proceed".
    """

    REFRESHED = "refreshed"
    NOT_REFRESHED = "not_refreshed"
    OCCUPIED_BY_LIVE_SESSION = "occupied_by_live_session"


@dataclass(frozen=True)
class RefreshResult:
    """The refresh helper's verdict: a :class:`RefreshOutcome` and a one-line reason."""

    outcome: RefreshOutcome
    reason: str


@dataclass
class ReuseRefreshReport:
    """What the reuse refresh learned, for callers that must act on it (#2213).

    ``create_worktree`` returns only a path. A caller that opts into
    ``refresh_on_reuse`` and needs more than that passes one of these in:

    - ``notes``: one single-line entry per refresh FAILURE the caller cannot
      otherwise see, each naming the worktree and the reason -- the fetch
      failed (with git's reason), git refused the fast-forward, the branch
      diverged from origin, or an OS error aborted the refresh. A caller with a
      friction surface prints them. Designed non-actions add nothing: branch
      absent from origin, already equal or ahead, or a worktree that is
      occupied.
    - ``outcome`` / ``reason``: the refresh's verdict (:class:`RefreshOutcome`)
      and why, or ``None`` when no refresh ran. It is filled in BEFORE
      ``create_worktree`` returns or raises, so a caller that catches
      :exc:`~cw.exceptions.WorktreeOccupiedError` can still read it.

    The report does not carry the occupancy refusal to the caller: that is the
    exception. Reading ``outcome`` is for logging and friction surfaces, never
    for deciding whether it is safe to go on.
    """

    notes: list[str] = field(default_factory=list)
    outcome: RefreshOutcome | None = None
    reason: str | None = None


def _record_fast_forward(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    *,
    ticket_id: str | None,
    old_sha: str,
    new_sha: str,
) -> None:
    """Record one ``worktree.fast_forwarded`` audit event for a HEAD that moved.

    The reuse refresh can move a worktree's ``HEAD`` on its own, so an operator
    asking "why is my worktree at a different commit than I left it?" gets a
    durable answer: client, ticket, branch, path and the FULL before/after SHAs
    (the log line abbreviates them). ``correlation_id`` is *ticket_id* when
    known. Audit-only: not forwarded to the operator-attention channel.

    Only the write is guarded, and only for ``OSError`` (a full disk, an
    unwritable inbox, a lock failure): the fast-forward has already happened,
    so a lost audit line is logged at WARNING and must not turn it into
    ``NOT_REFRESHED`` or raise. Anything else is a bug and propagates.
    """
    payload = {
        "client": client.name,
        "ticket_id": ticket_id,
        "branch": branch,
        "worktree_path": str(wt_path),
        "old_sha": old_sha,
        "new_sha": new_sha,
    }
    try:
        record_event(
            OrchestratorEventType.WORKTREE_FAST_FORWARDED,
            payload,
            correlation_id=ticket_id,
        )
    except OSError as exc:
        _log.warning(
            "create_worktree: could not record the worktree.fast_forwarded audit "
            "event (client=%s, ticket=%s, path=%s, %s -> %s): %s",
            client.name,
            ticket_id,
            wt_path,
            old_sha[:_SHA_LOG_CHARS],
            new_sha[:_SHA_LOG_CHARS],
            _first_line(str(exc)) or type(exc).__name__,
        )


def _ff_reused_worktree(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    target: str,
    report: ReuseRefreshReport,
    *,
    ticket_id: str | None,
) -> RefreshResult:
    """Fast-forward *wt_path* to *target* with ``merge --ff-only``, never raising.

    First re-runs the FULL occupancy predicate (:func:`_reuse_occupancy`:
    expected branch, unsaved work, cw state, daemon roster), immediately before
    the first mutating git call. The caller's occupancy gate ran before a
    network fetch that can take a while, so a session or worker may have
    started, or the tree been dirtied, in the meantime. If anything changed the
    fast-forward is abandoned: ``OCCUPIED_BY_LIVE_SESSION`` when a live occupant
    appeared, ``NOT_REFRESHED`` when the tree merely changed. This narrows the
    window but does not eliminate it: a session can still start between this
    check and the merge. That remaining window is accepted because the only
    alternative -- holding a lock across the network fetch (or across the
    check-then-merge) -- is worse: it would stall every other claim behind a slow
    remote.

    ``--ff-only`` cannot destroy work: it refuses (rc != 0, worktree untouched)
    when local uncommitted changes overlap files the merge must update, and
    carries non-overlapping local modifications through. A refusal is logged,
    noted on *report*, returned as ``NOT_REFRESHED``, and the worktree is left
    exactly as it was. A completed fast-forward is ``REFRESHED``.

    A fast-forward that actually MOVES ``HEAD`` (the SHA after differs from the
    SHA before) leaves one ``worktree.fast_forwarded`` audit event
    (:func:`_record_fast_forward`), carrying *ticket_id* (``None`` when the
    caller has none). Nothing is recorded when nothing moved (a merge that says
    "Already up to date"), and a failed audit write never undoes or
    reclassifies the completed fast-forward.
    """
    verdict = _occupancy_verdict(
        client,
        branch,
        wt_path,
        action=(
            "reused worktree occupied after the fetch; fast-forward abandoned, "
            "using worktree as-is"
        ),
    )
    if verdict is not None:
        return verdict
    old_sha = _run_git("rev-parse", "HEAD", cwd=wt_path, check=False).stdout.strip()
    merge = _run_git("merge", "--ff-only", target, cwd=wt_path, check=False)
    if merge.returncode != 0:
        reason = _first_line(merge.stderr) or f"git exited {merge.returncode}"
        _log.warning(
            "create_worktree: fast-forward of %s refused (client=%s, path=%s): %s",
            branch,
            client.name,
            wt_path,
            reason,
        )
        report.notes.append(
            f"fast-forward of {branch} in reused worktree {wt_path} was refused "
            f"by git ({reason}); it was not refreshed and may be behind origin"
        )
        return RefreshResult(
            RefreshOutcome.NOT_REFRESHED, f"git refused the fast-forward: {reason}"
        )
    new_sha = _run_git("rev-parse", "HEAD", cwd=wt_path, check=False).stdout.strip()
    _log.info(
        "create_worktree: fast-forwarded reused worktree %s "
        "(client=%s, path=%s) %s -> %s",
        branch,
        client.name,
        wt_path,
        old_sha[:_SHA_LOG_CHARS],
        new_sha[:_SHA_LOG_CHARS],
    )
    if new_sha != old_sha:
        _record_fast_forward(
            client,
            branch,
            wt_path,
            ticket_id=ticket_id,
            old_sha=old_sha,
            new_sha=new_sha,
        )
    return RefreshResult(
        RefreshOutcome.REFRESHED,
        f"fast-forwarded {branch} {old_sha[:_SHA_LOG_CHARS]} -> "
        f"{new_sha[:_SHA_LOG_CHARS]}",
    )


_NON_TERMINAL_SESSION_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.ACTIVE, SessionStatus.IDLE, SessionStatus.BACKGROUNDED}
)


# What ``cw.config.load_state`` can really raise reading sessions.json:
# ``OSError`` (open/read, the pre-migration backup copy) and ``ValueError`` --
# the parent of ``json.JSONDecodeError`` and ``UnicodeDecodeError`` (a corrupt
# file), of pydantic's ``ValidationError`` (a wrong-shaped file), and of the
# ``int()`` schema-version coercion. It raises no project-specific state error.
# Anything outside this set is a bug and must propagate (#2213).
_STATE_READ_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError)


def live_session_worktree_paths() -> frozenset[Path] | None:
    """Return worktree paths of non-terminal sessions in cw state, or None.

    This helper REPORTS what it can determine; it does not decide what an
    indeterminate answer means -- EACH CALLER DECIDES that. It returns a
    frozenset of paths when the session state was read, or ``None`` when it is
    indeterminate: the state could not be read or parsed (``OSError`` /
    ``ValueError`` -- see :data:`_STATE_READ_ERRORS`; logged at WARNING). Any
    other exception is a bug, not a corrupt file, and propagates.

    The two callers deliberately treat ``None`` in OPPOSITE directions:

    - The reuse refresh (:func:`live_home_reason`, reached through
      :func:`_reuse_occupancy`, #2213) fails CLOSED. ``None`` means "cannot
      rule out a live session", i.e. occupied: ``create_worktree`` raises
      :exc:`~cw.exceptions.WorktreeOccupiedError` and no caller spawns into,
      dispatches against or fast-forwards the tree. Reading "unknown" as "free"
      would rewrite (or spawn a second worker into) a live worker's tree.
    - The worktree GC (``cw.worktree_gc._live_worktree_paths``) fails OPEN,
      unchanged from before #2213. ``None`` contributes nothing, so a corrupted
      state file never blocks garbage collection; GC keeps running with this
      live-session guard disabled for the run (the WARNING above is the trace).

    The split is deliberate. Do not "fix" either posture into consistency with
    the other.

    Returns the paths exactly as recorded (unresolved): the GC compares them
    against git-listed paths, and the refresh normalizes before comparing.

    Lives here rather than in ``cw.worktree_gc`` because that module imports
    ``cw.dev_queue``, whose requeue/lifecycle modules import this one -- a
    top-level import of it from here would be a cycle.
    """
    try:
        state = load_state()
    except _STATE_READ_ERRORS as exc:
        _log.warning("live-path guard: failed to load session state: %s", exc)
        return None
    live: set[Path] = set()
    for session in state.sessions:
        if (
            session.status in _NON_TERMINAL_SESSION_STATUSES
            and session.worktree_path is not None
        ):
            live.add(session.worktree_path)
    return frozenset(live)


def _normalize_path(path: Path) -> Path:
    """Return *path* fully resolved, or raise ``OSError`` if it cannot be.

    A bare non-strict ``Path.resolve()`` is not enough. Since Python 3.13 it
    swallows a symlink loop and hands back the path only partly resolved, and it
    does not surface access errors either; the result would compare unequal to
    the real home and read as "not occupied" -- the fail-open direction this
    guard exists to prevent (#2213). ``stat()`` follows the whole chain first
    and reports the truth: ``ELOOP``, ``EACCES``, ``ENOTDIR``,
    ``ENAMETOOLONG`` and every other ``OSError`` propagate, and the caller
    reads that as "cannot rule out occupancy". There is no ``OSError`` for which
    "assume it is free" is the safe answer.

    The one exception is ``FileNotFoundError``. A path that does not exist is a
    fact, not a failure to look: it cannot be the existing worktree being
    reused, and a stale record of a deleted worktree must not veto every future
    refresh. It falls through to ``resolve()``, which normalizes what exists.
    """
    with contextlib.suppress(FileNotFoundError):
        path.stat()
    return path.resolve()


def live_home_reason(wt_path: Path) -> str | None:
    """Return why a live session or daemon worker may be homed on *wt_path*.

    The one liveness predicate for a worktree, public because two paths must
    agree on it: the same-branch reuse refresh (:func:`_reuse_occupancy`) and
    the dispatch claim's stale-worktree handler (``cw.dispatch.claim``), which
    must not force-remove a wrong-branch tree a live worker is homed on (#2213).
    Both fail closed.

    Consults BOTH sources and reports occupied when either says so:

    - cw's persisted session state (:func:`live_session_worktree_paths`), and
    - the daemon roster's live workers, each recorded with the ``cwd`` it was
      spawned in (``NativeDaemonClient.list_live_worker_cwds``) -- a worker can
      be live in the roster before, or after, cw state reflects it.

    Fails closed: an unreadable state file, an unreadable roster, or ANY path
    that cannot be normalized reads as "cannot rule out a live session", never
    as "free". Every path is compared after normalization
    (:func:`_normalize_path`) on both sides, so a symlinked or non-canonical
    spelling of the same directory still matches, and a path that cannot be
    normalized -- a symlink loop, a permission error, a component that is not a
    directory, an over-long name -- reads as occupied rather than as a path that
    merely differs.
    """
    sessions = live_session_worktree_paths()
    if sessions is None:
        return "session state unreadable, cannot rule out a live session"
    workers = get_native_daemon_client().list_live_worker_cwds()
    if workers is None:
        return "daemon roster unreadable, cannot rule out a live session"
    try:
        target = _normalize_path(wt_path)
        session_homes = {_normalize_path(path) for path in sessions}
        worker_homes = {_normalize_path(path) for path in workers}
    except OSError as exc:
        return f"a path cannot be resolved ({exc}), cannot rule out a live session"
    if target in session_homes:
        return "a live session is homed on this worktree"
    if target in worker_homes:
        return "a live daemon worker is homed on this worktree"
    return None


class _Occupancy(NamedTuple):
    """The reuse refresh's occupancy verdict, split by what it lets a caller do.

    - ``live``: a live session or daemon worker may be homed on the worktree
      (:func:`live_home_reason`), or the state or roster could not be read
      (fail closed). Nothing may touch the tree.
    - ``local``: an unexpected checked-out branch, or unsaved work. The refresh
      must not move HEAD, but the worktree is still the caller's to use: the
      staged pipeline reuses one per-ticket worktree that legitimately carries
      churn (e.g. ``uv.lock``) from a prior stage.

    Both halves are always evaluated. Unsaved work is exactly what a live worker
    leaves behind, so it must never mask ``live``.
    """

    live: str | None
    local: str | None

    @property
    def reason(self) -> str | None:
        """The most serious reason the refresh is refused, or None if free."""
        return self.live if self.live is not None else self.local


def _reuse_occupancy(client: ClientConfig, branch: str, wt_path: Path) -> _Occupancy:
    """Return whether *wt_path* is occupied (must not be moved), and why.

    The single predicate the reuse refresh consults, both up front and again
    immediately before it mutates (see :func:`_ff_reused_worktree`). Local
    reads only -- no network. Occupied means any of:

    - ``local``: the checked-out branch is not *branch* (or HEAD is detached);
      or :func:`unsaved_work_reason` reports uncommitted, untracked or unpushed
      work (checked regardless of the caller's ``allow_dirty_reuse``, which
      only tolerates such work, it does not license moving HEAD under it); or
    - ``live``: :func:`live_home_reason` -- a live session in cw state or a
      live worker in the daemon roster is homed on *wt_path*, or either could
      not be read (fail closed: this gates a mutation). The dev-queue RUNNING
      half of the GC guard is deliberately not consulted: at dispatch-claim time
      the task being claimed is itself RUNNING, so it would veto the very path
      this refresh serves, and a live session for that task already appears in
      the state half.
    """
    current = _checked_out_branch(wt_path)
    local: str | None = None
    if current != branch:
        found = current or "(none / detached HEAD / not a worktree)"
        local = f"expected branch {branch!r} but found {found}"
    else:
        unsaved = unsaved_work_reason(client, branch, wt_path=wt_path)
        if unsaved is not None:
            local = f"unsaved work ({unsaved})"
    return _Occupancy(live=live_home_reason(wt_path), local=local)


def _occupancy_verdict(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    *,
    action: str,
) -> RefreshResult | None:
    """Return the stopping result when the refresh must not go on, else ``None``.

    Logs *action* at DEBUG naming the path and reason. The verdict keeps the two
    kinds of refusal apart (see :class:`_Occupancy`): a live occupant is
    ``OCCUPIED_BY_LIVE_SESSION`` (the caller must abort), while an unexpected
    branch or unsaved work is ``NOT_REFRESHED`` (the tree is still the caller's
    to use). A live occupant wins when both hold, because unsaved work is exactly
    what a live worker leaves behind.
    """
    occupancy = _reuse_occupancy(client, branch, wt_path)
    reason = occupancy.reason
    if reason is None:
        return None
    _log.debug(
        "create_worktree: %s (client=%s, path=%s): %s",
        action,
        client.name,
        wt_path,
        reason,
    )
    outcome = (
        RefreshOutcome.OCCUPIED_BY_LIVE_SESSION
        if occupancy.live is not None
        else RefreshOutcome.NOT_REFRESHED
    )
    return RefreshResult(outcome, reason)


def _fetch_gate(
    client: ClientConfig, branch: str, wt_path: Path, report: ReuseRefreshReport
) -> RefreshResult | None:
    """Fetch ``origin/<branch>``; return a stopping result, or ``None`` if it landed.

    Handles :class:`FetchOutcome` exhaustively: only ``FETCHED`` lets the refresh
    go on. ``BRANCH_ABSENT`` (never pushed) stops quietly: an expected state,
    not friction. ``FAILED`` leaves the tracking ref at whatever it was before,
    so fast-forwarding "to origin" would really move HEAD to stale state: it
    stops, and is reported with git's reason. A member this function does not
    know is a type error (and, at runtime, an ``AssertionError``), never a silent
    "fetched".
    """
    fetch = fetch_feature_branch(client, branch)
    outcome = fetch.outcome
    match outcome:
        case FetchOutcome.FETCHED:
            return None
        case FetchOutcome.BRANCH_ABSENT:
            return RefreshResult(
                RefreshOutcome.NOT_REFRESHED, f"origin/{branch} does not exist yet"
            )
        case FetchOutcome.FAILED:
            reason = fetch.reason or "no reason reported"
            _log.debug(
                "create_worktree: fetch of origin/%s failed (%s); fast-forward "
                "skipped, using worktree as-is (client=%s, path=%s)",
                branch,
                reason,
                client.name,
                wt_path,
            )
            report.notes.append(
                f"fetch of origin/{branch} failed while refreshing reused "
                f"worktree {wt_path} ({reason}); it was not fast-forwarded and "
                "may be behind origin"
            )
            return RefreshResult(
                RefreshOutcome.NOT_REFRESHED,
                f"fetch of origin/{branch} failed: {reason}",
            )
        case _:
            assert_never(outcome)


def _refresh_from_tracking_ref(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    report: ReuseRefreshReport,
    *,
    ticket_id: str | None,
) -> RefreshResult:
    """Classify HEAD against the freshly fetched ``origin/<branch>`` and act on it.

    The target is the branch's own ``refs/remotes/origin/<branch>``, NOT
    :func:`_resolve_remote_ref`: that ladder is upstream-first, and a
    misconfigured ``@{u}`` of ``origin/<default>`` (the #2114 failure mode) would
    fast-forward a feature branch onto main. Target absent (a fetch can succeed
    without creating the tracking ref, under a narrow ``remote.origin.fetch``
    refspec): nothing to move.

    Equal or ahead: nothing to do (unpushed commits kept). Diverged (remote
    history rewritten since the worktree pushed): WARNING, untouched --
    reconciling is not this function's job. Behind: the fast-forward, after the
    occupancy re-check (:func:`_ff_reused_worktree`).
    """
    target = f"refs/remotes/origin/{branch}"
    if not _ref_exists(target, wt_path):
        return RefreshResult(
            RefreshOutcome.NOT_REFRESHED, f"{target} does not exist after the fetch"
        )
    relation = _ff_relation("HEAD", target, wt_path)
    match relation:
        case "equal" | "ahead":
            return RefreshResult(
                RefreshOutcome.NOT_REFRESHED,
                f"already up to date with origin/{branch} ({relation})",
            )
        case "diverged":
            _log.warning(
                "create_worktree: reused worktree diverged from origin/%s; "
                "leaving untouched (client=%s, path=%s)",
                branch,
                client.name,
                wt_path,
            )
            report.notes.append(
                f"reused worktree {wt_path} has diverged from origin/{branch}; it "
                "was left untouched and not refreshed"
            )
            return RefreshResult(
                RefreshOutcome.NOT_REFRESHED, f"diverged from origin/{branch}"
            )
        case "behind":
            return _ff_reused_worktree(
                client, branch, wt_path, target, report, ticket_id=ticket_id
            )
        case _:
            assert_never(relation)


def _refresh_reused_worktree_steps(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    report: ReuseRefreshReport,
    *,
    ticket_id: str | None,
) -> RefreshResult:
    """The ordered steps of :func:`_refresh_reused_worktree`, which see OSError."""
    verdict = _occupancy_verdict(
        client, branch, wt_path, action="not refreshing reused worktree"
    )
    if verdict is not None:
        return verdict
    stopped = _fetch_gate(client, branch, wt_path, report)
    if stopped is not None:
        return stopped
    return _refresh_from_tracking_ref(
        client, branch, wt_path, report, ticket_id=ticket_id
    )


def _refresh_reused_worktree(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    report: ReuseRefreshReport,
    *,
    ticket_id: str | None,
) -> RefreshResult:
    """Best-effort fetch, then fast-forward a *behind, unoccupied* reused worktree.

    Called from :func:`create_worktree` only when ``refresh_on_reuse`` is set
    (#2213), after its branch-identity and unsaved-work guards. Closes the
    asymmetry between the first-time path (which fetches) and the reuse path
    (which used to return the worktree untouched): a per-ticket worktree reused
    across pipeline stages could sit on a stale HEAD while ``origin/<branch>``
    had moved on.

    Returns a :class:`RefreshResult` (also recorded on *report*), whose
    :class:`RefreshOutcome` says what the caller may do next:

    - ``REFRESHED``: fast-forwarded. Proceed.
    - ``NOT_REFRESHED``: the tree is the caller's, just not up to date. Proceed.
    - ``OCCUPIED_BY_LIVE_SESSION``: another worker may be operating in it. The
      caller must NOT spawn into it, dispatch against it or mutate it.
      :func:`create_worktree` turns this into
      :exc:`~cw.exceptions.WorktreeOccupiedError`; this helper only reports it.

    Order of operations:

    1. **Occupancy gate, local reads only, no network**
       (:func:`_reuse_occupancy`). A live session in cw state, a live worker in
       the daemon roster homed here, an unreadable state or roster, or a path
       that cannot be normalized (fail closed) is ``OCCUPIED_BY_LIVE_SESSION``.
       The checked-out branch not being the expected one, or unsaved work, is
       ``NOT_REFRESHED``. Either way a DEBUG log names the path and the reason
       and nothing else happens (no fetch, no move). (``create_worktree``'s own
       identity guard *raises* on a wrong branch before this helper is reached;
       the predicate repeats the branch check because step 4 re-runs it.)
    2. ``git fetch`` of ``origin/<branch>`` via :func:`fetch_feature_branch`,
       which returns a :class:`FetchResult`. This is a network call: it can be
       slow, or fail. Only ``FETCHED`` proceeds (see :func:`_fetch_gate`); a
       failed fetch is ``NOT_REFRESHED`` and reported with git's reason.
    3. Target and relation: see :func:`_refresh_from_tracking_ref`.
    4. Behind: the full occupancy predicate is re-run immediately before
       ``merge --ff-only`` (a session may have started during the fetch; see
       :func:`_ff_reused_worktree`), then the fast-forward. A fast-forward that
       moved HEAD records one ``worktree.fast_forwarded`` audit event carrying
       *ticket_id*; no other path records anything.

    *report* is the caller-supplied surface (see :class:`ReuseRefreshReport`).
    Every FAILURE the caller cannot otherwise see -- a failed fetch, a diverged
    branch, a fast-forward git refused, an ``OSError`` -- appends exactly one
    note naming the worktree and the reason. Designed non-actions (occupied,
    branch absent, equal or ahead) add none.

    Never raises for a git or OS failure and never resets, ``checkout -f``s or
    deletes. Anything that is not an ``OSError`` is a bug and propagates.
    """
    try:
        result = _refresh_reused_worktree_steps(
            client, branch, wt_path, report, ticket_id=ticket_id
        )
    except OSError as exc:
        reason = _first_line(str(exc)) or type(exc).__name__
        _log.warning(
            "create_worktree: refresh of reused worktree failed "
            "(client=%s, path=%s): %s",
            client.name,
            wt_path,
            reason,
        )
        report.notes.append(
            f"refresh of reused worktree {wt_path} failed with an OS error "
            f"({reason}); it was not refreshed and may be behind origin"
        )
        result = RefreshResult(
            RefreshOutcome.NOT_REFRESHED, f"OS error during the refresh: {reason}"
        )
    report.outcome = result.outcome
    report.reason = result.reason
    return result


def _raise_if_occupied(result: RefreshResult, branch: str, wt_path: Path) -> None:
    """Turn ``OCCUPIED_BY_LIVE_SESSION`` into :exc:`WorktreeOccupiedError`.

    Exhaustive over :class:`RefreshOutcome`: the two "proceed" outcomes return,
    the occupied one raises, and a member this function does not know is a type
    error (``assert_never``), never a silent "proceed".
    """
    outcome = result.outcome
    match outcome:
        case RefreshOutcome.OCCUPIED_BY_LIVE_SESSION:
            msg = (
                f"Refusing to reuse worktree at {wt_path} for branch {branch!r}: "
                f"another worker may be operating in it ({result.reason}). The "
                "worktree was not moved or otherwise touched. Retry once the "
                "occupant is gone."
            )
            raise WorktreeOccupiedError(msg, path=wt_path, reason=result.reason)
        case RefreshOutcome.REFRESHED | RefreshOutcome.NOT_REFRESHED:
            return
        case _:
            assert_never(outcome)


def create_worktree(
    client: ClientConfig,
    branch: str,
    *,
    force: bool = False,
    allow_dirty_reuse: bool = False,
    refresh_on_reuse: bool = False,
    refresh_report: ReuseRefreshReport | None = None,
    ticket_id: str | None = None,
) -> Path:
    """Create a git worktree for the given branch.

    Returns the worktree path. Idempotent: returns the existing path when it is
    already a worktree on *branch*. A pre-existing directory checked out on a
    *different* branch (or not a worktree at all) is treated as stale and
    raises :exc:`StaleWorktreeError` rather than being reused (see below).

    By default reuse is path resolution only: no fetch, no fast-forward.

    *refresh_on_reuse* (#2213) opts a caller into a best-effort refresh of a
    reused worktree, for callers whose purpose is a fresh per-ticket worktree
    (dispatch claim, fix-agent dispatch). It has side effects a path-resolution
    call would not suggest:

    - **Network:** it runs ``git fetch origin <branch>``, which can be slow or
      fail. A failed fetch skips the fast-forward entirely and uses the
      worktree as-is (a fast-forward from the stale tracking ref a failed
      fetch leaves behind would be a move to stale state); it never raises out
      of this function.
    - **Moves HEAD** only for a strict fast-forward (``merge --ff-only``) of a
      worktree that is unoccupied, clean, already on *branch*, and strictly
      behind a freshly fetched ``origin/<branch>``. The full occupancy check
      runs once before the fetch and again immediately before the merge.

    **"The refresh did not move it" means two different things**
    (:class:`RefreshOutcome`), and they are handled oppositely:

    - **Occupied -- ABORT, raised.** A live cw session in persisted state, a live
      daemon-roster worker, or an indeterminate read of either (unreadable
      state or roster, or any path that cannot be resolved -- fail closed, any
      ``OSError`` reads as occupied) means another worker may be operating in
      the tree. This function RAISES :exc:`~cw.exceptions.WorktreeOccupiedError`
      (carrying ``path`` and ``reason``) rather than returning the path, so a
      caller cannot spawn into, dispatch against or mutate the tree by
      forgetting a check. The worktree is not touched and not removed. It is
      not a :exc:`StaleWorktreeError`: never remove an occupied worktree.
    - **Not refreshed -- PROCEED, returned.** The tree is the caller's to use as
      it is, just not (known to be) up to date: unsaved work (uncommitted,
      untracked or unpushed -- what ``allow_dirty_reuse`` tolerates), the fetch
      failed (the tracking ref is stale, so it is never fast-forwarded from),
      the branch has diverged from origin (WARNING), ``--ff-only`` itself
      refuses (WARNING), ``origin/<branch>`` does not exist, or it already
      matches origin. This function returns the path; the reason is on
      *refresh_report* and, for failures, in a note. A wrong branch is
      stricter still: the identity guard above raises :exc:`StaleWorktreeError`
      before any refresh.

    *refresh_report* (#2213) is an out-parameter (:class:`ReuseRefreshReport`)
    for callers that want more than the path. Its ``notes`` receive one line per
    refresh FAILURE (fetch failed, with git's reason; fast-forward refused by
    git; diverged; an OS error), each naming the worktree and the reason. Its
    ``outcome`` and ``reason`` are the verdict, filled in before this function
    returns or raises. The occupancy refusal does not depend on it: it is the
    exception. Ignored unless *refresh_on_reuse* is set.

    *ticket_id* (#2213) names the ticket the caller is working, for the audit
    event only: a refresh that actually moves HEAD records one
    ``worktree.fast_forwarded`` event (see ``docs/events.md``) with it as the
    payload's ``ticket_id`` and the ``correlation_id`` (``None`` when the
    caller has no ticket). It has no other effect and is ignored unless
    *refresh_on_reuse* is set. The audit write is best-effort: an ``OSError``
    from it is logged and never changes the outcome.

    See :func:`_refresh_reused_worktree`.

    When no existing worktree is reused, the branch itself is resolved via a
    three-way check (#2032): a local ``refs/heads/<branch>`` is used as-is; if
    absent, ``origin/<branch>`` is fetched and checked next so a branch whose
    local ref was deleted (e.g. a fix-loop's ``git branch -D`` reset) resumes
    its real pushed history instead of silently starting over; only when
    neither exists is a brand-new branch created from the client's default
    branch.

    *allow_dirty_reuse* relaxes the unsaved-work refusal **only** (the
    branch-identity check still fires). The staged pipeline reuses one
    per-ticket worktree across stages, where a prior stage legitimately leaves
    uncommitted churn (e.g. ``uv.lock``); without this the FINALIZE-stage
    reuse trips the guard and parks the ticket (#712). Cross-ticket protection
    is unaffected — a foreign branch at the path is still refused.
    """
    wt_path = worktree_path_for(client, branch)
    git_cwd = _git_dir(client)

    check_not_main_checkout(wt_path, client)

    if wt_path.exists():
        # Idempotent reuse is only safe when the existing worktree is still on
        # the branch we were asked for. A stale worktree left by a prior failed
        # dispatch (crash before reconcile's TIMED_OUT cleanup, see #404) can
        # carry a different branch — and thus a prior run's commits — into the
        # new session. Silently reusing it feeds the worker the wrong context
        # and has caused cross-ticket isolation breaches (#402). Refuse on
        # mismatch: the dispatch loop reverts the task to PENDING and reconcile
        # removes the stale tree so the retry starts clean.
        current_branch = _checked_out_branch(wt_path)
        if current_branch != branch:
            found = current_branch or "(none / detached HEAD / not a worktree)"
            msg = (
                f"Refusing to reuse stale worktree at {wt_path}: expected "
                f"branch {branch!r} but found {found}. Remove it with "
                f"`git worktree remove --force {wt_path}`, then re-dispatch."
            )
            raise StaleWorktreeError(msg)
        if not allow_dirty_reuse and worktree_has_unsaved_work(client, branch):
            msg = (
                f"Refusing to reuse worktree at {wt_path} for branch {branch!r}: "
                f"it has unsaved work (uncommitted changes or unpushed commits). "
                f"Commit or push the work, then re-dispatch."
            )
            raise StaleWorktreeError(msg)
        if refresh_on_reuse:
            refresh = _refresh_reused_worktree(
                client,
                branch,
                wt_path,
                refresh_report if refresh_report is not None else ReuseRefreshReport(),
                ticket_id=ticket_id,
            )
            # Occupied means another worker may be using the tree: no path
            # is handed back. Every other outcome is the caller's to use.
            _raise_if_occupied(refresh, branch, wt_path)
        return wt_path

    wt_path.parent.mkdir(parents=True, exist_ok=True)

    # Three-way branch resolution: local ref / remote ref / neither (#2032).
    # refs/heads/ and refs/remotes/origin/ are checked explicitly so a
    # same-named tag never matches either.
    if _ref_exists(f"refs/heads/{branch}", git_cwd):
        # Local branch exists — create worktree from it.
        args = ["worktree", "add", str(wt_path), branch]
    else:
        # Local ref absent — before assuming the branch doesn't exist at
        # all, check the remote. A prior `git branch -D` (e.g.
        # auto-dev-review.md's fix-loop reset) leaves exactly this state
        # while origin still has the branch's real history.
        # The fetch result is deliberately unused: this only precedes the
        # local ref-exists check below, and ``_fetch_default_branch`` has
        # already logged a failure's reason.
        fetch_feature_branch(client, branch)
        if _ref_exists(f"refs/remotes/origin/{branch}", git_cwd):
            # Branch exists on the remote — resume its pushed history.
            args = ["worktree", "add", "-b", branch, str(wt_path), f"origin/{branch}"]
        else:
            # Branch doesn't exist locally or on the remote — create new
            # branch from the client's default branch.
            start_point = _resolve_branch_start_point(client, git_cwd)
            args = ["worktree", "add", "-b", branch, str(wt_path), start_point]

    if force:
        args.insert(2, "--force")

    try:
        _run_git(*args, cwd=git_cwd)
    except WorktreeError as exc:
        holder = _parse_worktree_holder_path(str(exc))
        if holder is None:
            raise
        raise _branch_held_error(client, branch, holder) from exc
    _register_cw_exclude(git_cwd)

    # Initialize submodules if the repo uses them
    if (git_cwd / ".gitmodules").exists():
        _run_git(
            "submodule",
            "update",
            "--init",
            "--recursive",
            cwd=wt_path,
            check=False,
        )

    return wt_path


def remove_worktree(
    client: ClientConfig,
    branch: str,
    *,
    force: bool = False,
) -> None:
    """Remove a git worktree for the given branch."""
    wt_path = worktree_path_for(client, branch)

    if not wt_path.exists():
        return

    args = ["worktree", "remove", str(wt_path)]
    if force:
        args.append("--force")

    _run_git(*args, cwd=_git_dir(client))


def _commits_ahead(base: str, wt_path: Path) -> int | None:
    """Return the number of commits in ``<base>..HEAD``, or None if *base* is
    not resolvable (``git log`` exits non-zero)."""
    result = _run_git("log", f"{base}..HEAD", "--oneline", cwd=wt_path, check=False)
    if result.returncode != 0:
        return None
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _own_remote_ref(branch: str, wt_path: Path) -> str | None:
    """Return ``origin/<checked-out branch>`` when that ref exists, else None.

    The checked-out branch is preferred over the caller-supplied *branch*
    (#2050/#2053: the two can differ); *branch* is the fallback when the
    checkout cannot be resolved. ``rev-parse --verify`` answers "is this
    branch pushed" exactly, independent of tracking configuration (#2114).
    """
    current = _checked_out_branch(wt_path) or branch
    ref = f"origin/{current}"
    verify = _run_git("rev-parse", "--verify", "--quiet", ref, cwd=wt_path, check=False)
    return ref if verify.returncode == 0 else None


def _upstream_ref(wt_path: Path) -> str | None:
    """Return the checked-out branch's ``@{u}`` tracking ref, or None."""
    upstream = _run_git(
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{u}",
        cwd=wt_path,
        check=False,
    )
    if upstream.returncode != 0:
        return None
    return upstream.stdout.strip() or None


def _resolve_remote_ref(branch: str, wt_path: Path) -> str | None:
    """Resolve the verified remote ref for *branch*, upstream-first (#2145).

    Prefers the checked-out branch's configured upstream (``@{u}``) over the
    guessed ``origin/<branch>`` name -- the two differ whenever the branch
    was pushed under one name and later checked out locally under another
    (#2145). The guess is used only as a fallback, and only once confirmed
    to actually resolve (:func:`_ref_exists`) -- an invented ref is never
    returned as-is.

    Deliberately the OPPOSITE priority from :func:`_unpushed_commits_detail`'s
    ladder, which checks the own-name guess first (#2114) because there a
    misconfigured ``@{u}`` pointing at the default branch is the failure
    mode being defended against. Here ``@{u}`` is the trusted, explicitly
    configured answer and the guess is the fallback -- do not unify these
    two ladders; they answer different questions.
    """
    upstream = _upstream_ref(wt_path)
    if upstream is not None and _ref_exists(upstream, wt_path):
        return upstream
    guess = f"origin/{branch}"
    if _ref_exists(guess, wt_path):
        return guess
    return None


def _unpushed_commits_detail(
    client: ClientConfig, branch: str, wt_path: Path
) -> str | None:
    """Describe *branch*'s unpushed commits, or return None when it is pushed.

    Base-ref ladder, first resolvable level wins:

    0. ``origin/<checked-out branch>`` when that ref exists — the exact
       answer to "is this branch pushed". This level is what #2114 added:
       the ``@{u}`` level below trusts whatever tracking ref is configured,
       and when that is ``origin/<default_branch>`` every commit the feature
       branch contains reads as unpushed, so a clean, fully-pushed worktree
       parked ``dirty_worktree`` forever.
    1. The worktree's own ``@{u}`` upstream (a branch pushed under another
       name, #2050/#2053).
    2. ``origin/<default_branch>`` — no own remote ref, no upstream.
    3. Local ``<default_branch>`` — offline / bare-clone fallback.

    Levels 1-3 cannot distinguish "pushed under a name we cannot see" from
    "never pushed", so their message says which base was measured against;
    the caller surfaces it in the park breadcrumb. Returns a description
    conservatively on subprocess failure or when no base ref resolves.
    """
    try:
        own = _own_remote_ref(branch, wt_path)
        if own is not None:
            ahead = _commits_ahead(own, wt_path)
            if ahead is not None:
                return None if ahead == 0 else f"{ahead} commit(s) not on {own}"
        candidates = (
            _upstream_ref(wt_path),
            f"origin/{client.default_branch}",
            client.default_branch,
        )
        for base in (b for b in candidates if b is not None):
            ahead = _commits_ahead(base, wt_path)
            if ahead is None:
                continue
            if ahead == 0:
                return None
            return (
                f"{ahead} commit(s) ahead of {base} and no origin/<branch> ref"
                " for the checked-out branch (cannot prove they are pushed)"
            )
    except (WorktreeError, OSError) as exc:
        _log.warning(
            "worktree_has_unsaved_work: log check failed for %s/%s: %s",
            client.name,
            branch,
            exc,
        )
        # Fail-safe: treat as having unsaved work.
        return f"unpushed-commit check failed: {exc}"
    # All refs unresolvable — conservative fail-safe.
    return "no base ref resolvable (offline or bare clone)"


def _uncommitted_changes_detail(
    client: ClientConfig, branch: str, wt_path: Path
) -> str | None:
    """Describe uncommitted changes in *wt_path*, or return None when clean.

    Filters out cw's own artifacts (``.claude/``) — these are written fresh
    each session and would otherwise trip the dirty check on every retry.
    Porcelain format: "XY path" (2-char status + space + path). Rename
    entries ("R  old -> new") pass through unchanged; cw artifacts never
    appear as renames so they will still be caught by path check.
    """
    try:
        status = _run_git("status", "--porcelain", cwd=wt_path, check=False)
    except (WorktreeError, OSError) as exc:
        _log.warning(
            "worktree_has_unsaved_work: status check failed for %s/%s: %s",
            client.name,
            branch,
            exc,
        )
        # Fail-safe: treat as having unsaved work so we don't silently destroy.
        return f"status check failed: {exc}"
    lines = [
        line
        for line in status.stdout.splitlines()
        if not (
            len(line) > _GIT_PORCELAIN_PATH_OFFSET
            and line[_GIT_PORCELAIN_PATH_OFFSET:].startswith(_CW_SCRATCH_PREFIX)
        )
    ]
    if not lines:
        return None
    return f"{len(lines)} uncommitted path(s)"


def unsaved_work_reason(
    client: ClientConfig, branch: str, *, wt_path: Path | None = None
) -> str | None:
    """Return why the worktree for *branch* has unsaved work, or None if clean.

    "Unsaved" means either:
    - uncommitted changes (``git status --porcelain`` is non-empty), OR
    - unpushed commits (``git log <base>..HEAD`` is non-empty, see
      :func:`_unpushed_commits_detail` for the base-ref ladder).

    The returned string names which predicate fired, the base ref it was
    measured against, and the count — a park that says only
    ``dirty_worktree`` on a visibly clean tree cost real operator time
    before the predicate was read (#2114). Returns None when the worktree
    path does not exist (nothing to lose).

    *wt_path* defaults to the branch's canonical ``worktree_path_for(client,
    branch)`` location. Passing it explicitly lets a caller check a *foreign*
    (non-canonical) worktree's dirty state instead — e.g. a worktree
    collision's holder path, which cw did not create and does not track
    (#2034).

    Never raises — every git error is swallowed and logged at WARNING level
    so that a git failure cannot block a cleanup sweep; it is reported as a
    reason instead (fail-safe toward "has unsaved work").
    """
    if wt_path is None:
        wt_path = worktree_path_for(client, branch)
    if not wt_path.exists():
        return None
    uncommitted = _uncommitted_changes_detail(client, branch, wt_path)
    if uncommitted is not None:
        return uncommitted
    return _unpushed_commits_detail(client, branch, wt_path)


def worktree_has_unsaved_work(
    client: ClientConfig, branch: str, *, wt_path: Path | None = None
) -> bool:
    """Return True if the worktree for *branch* has unsaved work.

    Boolean view of :func:`unsaved_work_reason`; see it for the contract.
    """
    return unsaved_work_reason(client, branch, wt_path=wt_path) is not None


def _first_line(text: str) -> str:
    """Return the first line of *text* ('' when empty) for one-line log fields."""
    lines = text.strip().splitlines()
    return lines[0] if lines else ""


def _fetch_default_branch(
    client_name: str,
    default_branch: str,
    git_dir: Path,
    warned_fetch_fail: set[FetchWarningKey] | None = None,
    *,
    quiet_missing_ref: bool = False,
) -> FetchResult:
    """Fetch origin/<default_branch> and report what happened, and why.

    Returns a :class:`FetchResult`: :attr:`FetchOutcome.FETCHED` on success,
    :attr:`FetchOutcome.BRANCH_ABSENT` when origin has no such branch (git's
    ``couldn't find remote ref``), and :attr:`FetchOutcome.FAILED` for every
    other failure. For the two non-success outcomes ``reason`` is one line: git's
    exit status and first stderr line (``rc=128: fatal: ...``), or the workspace
    or OS problem that kept git from running, so a caller can tell auth from
    network from a missing remote. The outcome does not depend on
    *quiet_missing_ref*; only the log level does: a branch absent from origin is
    logged at DEBUG instead of WARNING when it is set, and then does not touch
    *warned_fetch_fail*. Every other failure still WARNs.

    *warned_fetch_fail* is a caller-owned set of :data:`FetchWarningKey`
    (client, outcome, reason) that dedups the WARNING per distinct failure: a
    repeat of the same failure for the same client stays quiet, but a different
    one (an auth error after a network error) is new information and warns
    again, so silence never reads as "the earlier problem persists". ``None``
    always warns.
    """
    if not git_dir.exists():
        _log.warning(
            "freshness_check_skip: workspace missing for %s (%s)",
            client_name,
            git_dir,
        )
        return FetchResult(FetchOutcome.FAILED, f"workspace missing: {git_dir}")
    try:
        result = _run_git(
            "fetch", "origin", default_branch, "--quiet", cwd=git_dir, check=False
        )
    except (WorktreeError, FileNotFoundError, PermissionError) as exc:
        _log.warning(
            "freshness_check_skip: %s (%s): %s",
            client_name,
            git_dir,
            exc,
        )
        return FetchResult(
            FetchOutcome.FAILED, _first_line(str(exc)) or type(exc).__name__
        )
    if result.returncode == 0:
        return FetchResult(FetchOutcome.FETCHED)
    stderr = result.stderr.strip()
    first_line = _first_line(stderr)
    reason = (
        f"rc={result.returncode}: {first_line}"
        if first_line
        else f"rc={result.returncode}"
    )
    branch_absent = _MISSING_REMOTE_REF_MARKER in stderr
    outcome = FetchOutcome.BRANCH_ABSENT if branch_absent else FetchOutcome.FAILED
    if branch_absent and quiet_missing_ref:
        _log.debug(
            "freshness_check_skip: fetch failed for %s (rc=%d): %s",
            client_name,
            result.returncode,
            first_line,
        )
        return FetchResult(outcome, reason)
    warn_key: FetchWarningKey = (client_name, outcome, reason)
    if warned_fetch_fail is None or warn_key not in warned_fetch_fail:
        _log.warning(
            "freshness_check_skip: fetch failed for %s (rc=%d): %s",
            client_name,
            result.returncode,
            first_line,
        )
        if warned_fetch_fail is not None:
            warned_fetch_fail.add(warn_key)
    return FetchResult(outcome, reason)


def fetch_feature_branch(client: ClientConfig, branch_name: str) -> FetchResult:
    """Fetch origin/<branch_name> into the client's git directory.

    Resolves the stale-local-ref problem described in GitHub issue #381:
    when the impl agent pushes commits from an isolation worktree, the
    parent worktree's local ref for the feature branch is not updated.
    Calling this before computing ``git diff FORK_POINT...origin/<branch>``
    for reviewer prompts ensures the diff reflects the actual pushed state.

    Returns a :class:`FetchResult` and never raises for a fetch error: outcome
    ``FETCHED`` on success, ``BRANCH_ABSENT`` when origin has no such branch,
    ``FAILED`` for anything else, with git's reason carried on the result. The
    two non-success outcomes are distinct because they need opposite handling
    (#2213): a branch that is not on origin is an expected state for a
    never-pushed feature branch (logged at DEBUG, not WARNING), while a failed
    fetch means the tracking ref is stale.
    """
    return _fetch_default_branch(
        client.name, branch_name, _git_dir(client), quiet_missing_ref=True
    )


def _get_behind_count(
    client_name: str, default_branch: str, git_dir: Path
) -> tuple[str, str, int] | None:
    """Get (local_sha, origin_sha, behind_count). Returns None on failure."""
    try:
        local_sha = _run_git("rev-parse", default_branch, cwd=git_dir).stdout.strip()
        origin_sha = _run_git(
            "rev-parse", f"origin/{default_branch}", cwd=git_dir
        ).stdout.strip()
        behind_count = int(
            _run_git(
                "rev-list",
                "--count",
                f"{default_branch}..origin/{default_branch}",
                cwd=git_dir,
            ).stdout.strip()
        )
    except (WorktreeError, ValueError):
        _log.warning(
            "is_main_behind_origin: rev-parse/rev-list failed for %s", client_name
        )
        return None
    else:
        return (local_sha, origin_sha, behind_count)


def is_main_behind_origin(
    client: ClientConfig,
    warned_fetch_fail: set[FetchWarningKey] | None = None,
) -> tuple[bool, str, str, int]:
    """Check whether the client's local default branch is behind origin.

    Fetches ``origin/<default_branch>`` then compares local and remote SHAs.

    Args:
        client: Client configuration.
        warned_fetch_fail: Caller-owned set of :data:`FetchWarningKey`
            ``(client, outcome, reason)`` entries that have already received a
            fetch-failure WARNING in this run. Suppresses a repeat of the SAME
            failure for the same client across ticks; a different failure for
            that client still warns. Pass ``None`` (default) to always log
            (correct for one-shot callers).

    Returns:
        A 4-tuple ``(is_stale, local_sha, origin_sha, behind_count)`` where
        *is_stale* is ``True`` when the local branch is behind the remote.
        On any fetch or parse failure returns ``(False, "", "", 0)`` and logs
        a WARNING — the caller should treat failure as non-stale.
    """
    git_dir = _git_dir(client)
    default_branch = client.default_branch

    fetch = _fetch_default_branch(
        client.name, default_branch, git_dir, warned_fetch_fail=warned_fetch_fail
    )
    outcome = fetch.outcome
    match outcome:
        case FetchOutcome.FETCHED:
            pass
        case FetchOutcome.BRANCH_ABSENT | FetchOutcome.FAILED:
            # A default branch absent from origin is not benign here (unlike a
            # feature branch): the freshness check cannot tell, so not stale.
            return (False, "", "", 0)
        case _:
            assert_never(outcome)

    counts = _get_behind_count(client.name, default_branch, git_dir)
    if counts is None:
        return (False, "", "", 0)

    local_sha, origin_sha, behind_count = counts
    return (behind_count > 0, local_sha, origin_sha, behind_count)


def _ff_relation(
    local_ref: str, remote_ref: str, cwd: Path
) -> Literal["equal", "behind", "ahead", "diverged"]:
    """Classify *local_ref*'s directional relationship to *remote_ref*.

    Two ``merge-base --is-ancestor`` probes: "behind" means *local_ref* is a
    strict ancestor of *remote_ref* (fast-forward is safe), "ahead" the
    reverse. A probe error (e.g. an unresolvable ref, rc 128) reads as
    "not an ancestor", so any failure classifies as "diverged" and can never
    trigger a mutation.
    """
    # Two merge-base --is-ancestor calls for directional classification.
    local_behind = _run_git(
        "merge-base",
        "--is-ancestor",
        local_ref,
        remote_ref,
        cwd=cwd,
        check=False,
    )
    remote_behind = _run_git(
        "merge-base",
        "--is-ancestor",
        remote_ref,
        local_ref,
        cwd=cwd,
        check=False,
    )
    # returncode 0 means the first arg is a reachable ancestor of the second.
    is_local_ancestor = local_behind.returncode == 0  # local ≤ remote → behind
    is_remote_ancestor = remote_behind.returncode == 0  # remote ≤ local → ahead

    if is_local_ancestor and is_remote_ancestor:
        return "equal"
    if is_local_ancestor:
        return "behind"
    if is_remote_ancestor:
        return "ahead"
    return "diverged"


def check_main_ff_safety(
    client: ClientConfig,
) -> Literal["equal", "behind", "ahead", "diverged", "detached"]:
    """Classify local main's relationship to origin for dispatch auto-ff.

    Returns one of:
      "behind"   — local main is strictly behind origin; fast-forward is safe
      "equal"    — local main matches origin; no action needed
      "ahead"    — local main has unpushed commits; operator action required
      "diverged" — local main has both new commits and is behind; needs reconciliation
      "detached" — HEAD is detached; fast-forward would be unsafe

    Operative outcomes from the dispatch path (when stale=True is already
    established): "behind" triggers auto-ff; "diverged" and "detached" fall
    through to TICKET_NEEDS_SYNC + warn. "equal" and "ahead" exist for
    defensive completeness but are not reachable from the stale=True path.
    """
    git_dir = _git_dir(client)
    default_branch = client.default_branch

    # Detached HEAD check — symbolic-ref exits non-zero when detached.
    # Prior art: _checked_out_branch() at line 148; fast_forward_main() below.
    sym = _run_git("symbolic-ref", "--short", "HEAD", cwd=git_dir, check=False)
    if sym.returncode != 0:
        return "detached"

    return _ff_relation(default_branch, f"origin/{default_branch}", git_dir)


def get_head_branch(client: ClientConfig) -> str | None:
    """Return the symbolic branch name of HEAD, or None if detached or on error.

    Callers in dispatch.py import this as ``cw.dispatch.get_head_branch`` so
    tests can patch it without reaching into worktree internals.
    """
    git_dir = _git_dir(client)
    result = _run_git("symbolic-ref", "--short", "HEAD", cwd=git_dir, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_main_checkout_dirty(client: ClientConfig) -> bool:
    """Return True if the main checkout has uncommitted tracked changes.

    Uses the same porcelain filter as fast_forward_main — untracked files
    (``??`` prefix) are ignored because git pull --ff-only is safe with them.
    Returns False on any git error so transient failures never block dispatch.
    """
    git_dir = _git_dir(client)
    try:
        status_out = _run_git("status", "--porcelain", cwd=git_dir).stdout
    except WorktreeError:
        return False
    status_lines = [
        line for line in status_out.splitlines() if line[:2] != _GIT_PORCELAIN_UNTRACKED
    ]
    return bool(status_lines)


def fast_forward_main(
    client: ClientConfig, *, ignore_untracked: bool = False
) -> tuple[str, str]:
    """Fast-forward the client's local default branch to origin.

    Runs ``git pull --ff-only origin <default_branch>`` in the client's git
    directory.  Raises :exc:`MissingWorkspaceError` if the workspace directory
    does not exist, or :exc:`WorktreeError` if the pull fails (non-zero exit)
    or if the checkout is not on ``default_branch`` or has uncommitted changes
    — both conditions risk mutating the index unexpectedly (#428).

    Returns:
        ``(before_sha, after_sha)`` — the SHA before and after the pull.
        When already up to date both values are equal.
    """
    git_dir = _git_dir(client)
    if not git_dir.exists():
        msg = f"workspace missing for {client.name} ({git_dir})"
        raise MissingWorkspaceError(msg)
    default_branch = client.default_branch

    # Guard 1: ensure the checkout is on the expected default branch.
    current_branch = _run_git(
        "symbolic-ref", "--short", "HEAD", cwd=git_dir
    ).stdout.strip()
    if current_branch != default_branch:
        msg = (
            f"Refusing to fast-forward {client.name}: HEAD is on "
            f"'{current_branch}', expected '{default_branch}'. "
            f"Switch to '{default_branch}' before refreshing."
        )
        raise WorktreeError(msg)

    # Guard 2: ensure the working tree is clean (or only has untracked files).
    status_out = _run_git("status", "--porcelain", cwd=git_dir).stdout
    status_lines = status_out.splitlines()
    if ignore_untracked:
        # Why: dispatch auto-ff may run against a workspace with untracked runtime
        # artifacts (.claude/scheduled_tasks.lock etc.); git pull --ff-only is
        # safe with untracked files because ff-only never rewrites the working tree.
        status_lines = [
            line for line in status_lines if line[:2] != _GIT_PORCELAIN_UNTRACKED
        ]
    if status_lines:
        msg = (
            f"Refusing to fast-forward {client.name}: working tree is dirty "
            f"(git status --porcelain reported changes). "
            f"Commit or stash changes before refreshing."
        )
        raise WorktreeError(msg)

    before_sha = _run_git("rev-parse", default_branch, cwd=git_dir).stdout.strip()
    _run_git("pull", "--ff-only", "origin", default_branch, cwd=git_dir)
    after_sha = _run_git("rev-parse", default_branch, cwd=git_dir).stdout.strip()
    return (before_sha, after_sha)
