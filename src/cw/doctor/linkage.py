"""Session-state linkage, workspace, and worktree health checks for ``cw doctor``.

Covers parent/worker linkage drift, on-demand reconciliation, per-client git
directory and dispatch-repo HEAD checks, dev-queue cross-repo row detection, and
non-terminal session worktree-path verification.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

from cw.doctor import _deps
from cw.doctor._shared import CheckResult
from cw.exceptions import CwError
from cw.models import TERMINAL_SESSION_STATUSES, SessionStatus
from cw.pr_hydrate import _parse_pr_url, _repo_slug_mismatch
from cw.reconcile import reconcile
from cw.worktree import _git_dir, get_head_branch

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, CwState, Session


def _check_linkage(state: CwState) -> list[CheckResult]:
    """Detect parent/worker linkage drift in session state.

    Returns one :class:`CheckResult` per drift type:

    * ``linkage/dangling-worker`` — an orchestrator's ``worker_session_ids``
      references a session ID absent from state.
    * ``linkage/dangling-parent`` — a worker's ``parent_session_id`` points at
      a session absent from state.
    * ``linkage/asymmetric`` — one side knows about the link but the other
      side doesn't (forward-only or reverse-only reference).

    All three results are always returned; each is ``ok=True`` when no drift
    of that type is detected.
    """
    # Index built once: O(1) membership (via .keys()) and lookup throughout.
    session_by_id: dict[str, Session] = {s.id: s for s in state.sessions}

    # --- dangling-worker: orchestrator.worker_session_ids → missing session ---
    dangling_worker_msgs: list[str] = [
        f"orchestrator {sess.id!r} references missing worker {wid!r}"
        " — remove the stale ID from worker_session_ids"
        for sess in state.sessions
        for wid in sess.worker_session_ids
        if wid not in session_by_id
    ]

    if dangling_worker_msgs:
        dw_detail = "; ".join(dangling_worker_msgs)
        dw_result = CheckResult("linkage/dangling-worker", ok=False, detail=dw_detail)
    else:
        dw_result = CheckResult("linkage/dangling-worker", ok=True, detail="")

    # --- dangling-parent: worker.parent_session_id → missing session ---
    dangling_parent_msgs: list[str] = [
        f"worker {sess.id!r} references missing parent {sess.parent_session_id!r}"
        " — clear parent_session_id or restore the parent session"
        for sess in state.sessions
        if sess.parent_session_id is not None
        and sess.parent_session_id not in session_by_id
    ]

    if dangling_parent_msgs:
        dp_detail = "; ".join(dangling_parent_msgs)
        dp_result = CheckResult("linkage/dangling-parent", ok=False, detail=dp_detail)
    else:
        dp_result = CheckResult("linkage/dangling-parent", ok=True, detail="")

    # --- asymmetric: one side of the link is missing ---
    # Build a map: parent_id → {set of worker IDs that claim it as parent}
    claimed_by: dict[str, set[str]] = {}
    for sess in state.sessions:
        parent_id = sess.parent_session_id
        if parent_id is not None and parent_id in session_by_id:
            claimed_by.setdefault(parent_id, set()).add(sess.id)

    # Forward check: orchestrator lists a worker, but worker doesn't claim it back.
    # Workers already caught as dangling are skipped (wid not in session_by_id).
    fwd_msgs: list[str] = []
    for sess in state.sessions:
        for wid in sess.worker_session_ids:
            worker = session_by_id.get(wid)
            if worker is None or worker.parent_session_id == sess.id:
                continue
            fwd_msgs.append(
                f"orchestrator {sess.id!r} lists worker {wid!r},"
                f" but worker's parent_session_id is {worker.parent_session_id!r}"
                " — update parent_session_id on the worker"
            )

    # Reverse check: worker claims this session as parent, but session
    # doesn't list the worker in its worker_session_ids.
    rev_msgs: list[str] = [
        f"worker {wid!r} claims parent {sess.id!r},"
        f" but {sess.id!r}'s worker_session_ids does not include it"
        " — add the worker ID to worker_session_ids"
        for sess in state.sessions
        for wid in claimed_by.get(sess.id, set())
        if wid not in sess.worker_session_ids
    ]

    asym_msgs = fwd_msgs + rev_msgs

    if asym_msgs:
        asym_detail = "; ".join(asym_msgs)
        asym_result = CheckResult("linkage/asymmetric", ok=False, detail=asym_detail)
    else:
        asym_result = CheckResult("linkage/asymmetric", ok=True, detail="")

    return [dw_result, dp_result, asym_result]


def _check_reconcile() -> CheckResult:
    """Run reconciliation and describe the outcome as a check result."""
    try:
        reconcile_report = reconcile()
    except CwError as exc:
        return CheckResult(
            "reconciliation",
            ok=False,
            detail=f"reconcile failed: {exc}",
        )
    reaped = len(reconcile_report.phantom_session_ids)
    reverted = len(reconcile_report.reverted_ticket_ids)
    if reaped == 0 and reverted == 0:
        return CheckResult("reconciliation", ok=True, detail="no phantoms")
    return CheckResult(
        "reconciliation",
        ok=True,
        detail=(
            f"reaped {reaped} session(s), reverted {reverted} ticket(s); "
            f"ids: {reconcile_report.phantom_session_ids}"
        ),
    )


def _check_workspace_paths() -> list[CheckResult]:
    """Verify each client's effective git directory exists."""
    try:
        clients = _deps.load_clients()
    except Exception:  # noqa: BLE001 — any load_clients() error yields []; _check_config_file already surfaces the parse failure as its own CheckResult, so this check must not also crash or double-report
        return []  # _check_config_file() already surfaces parse errors
    results = []
    for name, client in clients.items():
        git_dir = _git_dir(client)
        if not git_dir.exists():
            results.append(
                CheckResult(
                    f"workspace/{name}",
                    ok=False,
                    detail=f"path does not exist: {git_dir}",
                )
            )
    return results


def _check_dispatch_repo_head(
    clients: dict[str, ClientConfig],
) -> list[CheckResult]:
    """Check each client's dispatch repo HEAD is on its default branch."""
    results: list[CheckResult] = []
    for name, client in clients.items():
        try:
            branch = get_head_branch(client)
        except OSError as exc:
            results.append(
                CheckResult(
                    f"dispatch-repo-head/{name}",
                    ok=True,
                    warn=True,
                    detail=f"could not read HEAD: {exc}",
                )
            )
            continue
        if branch is None:
            git_dir = _git_dir(client)
            default = client.default_branch
            results.append(
                CheckResult(
                    f"dispatch-repo-head/{name}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"repo HEAD is detached, expected '{default}'"
                        f" — run: git -C {git_dir} checkout {default}"
                    ),
                )
            )
        elif branch != client.default_branch:
            git_dir = _git_dir(client)
            default = client.default_branch
            results.append(
                CheckResult(
                    f"dispatch-repo-head/{name}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"repo HEAD is on '{branch}', expected '{default}'"
                        f" — run: git -C {git_dir} checkout {default}"
                    ),
                )
            )
    return results


def _check_cross_repo_rows(
    clients: dict[str, ClientConfig],
) -> list[CheckResult]:
    """Advisory warn-only check: dev-queue rows whose client resolves to a
    different github repo than the row's ``pr_url`` (GitHub #1198).

    Every result is ``ok=True, warn=True`` so it never flips
    ``DoctorReport.ok`` (mirrors ``_check_dispatch_repo_head``). A broken queue
    is already surfaced by ``_check_dev_queue``, so a load failure here degrades
    to ``[]`` rather than double-reporting.
    """
    try:
        store = _deps.load_dev_queue()
    except (OSError, json.JSONDecodeError, ValidationError):
        return []
    results: list[CheckResult] = []
    for task in store.tasks:
        if task.pr_url is None:
            continue
        client = clients.get(task.client)
        if client is None:
            continue
        parsed = _parse_pr_url(task.pr_url)
        if parsed is None:
            continue
        pr_repo = parsed[0]
        client_repo = _repo_slug_mismatch(pr_repo, client.workspace_path)
        if client_repo is None:
            continue
        results.append(
            CheckResult(
                f"cross-repo/{task.ticket_id}",
                ok=True,
                warn=True,
                detail=(
                    f"row {task.ticket_id} client {task.client!r} workspace repo"
                    f" {client_repo!r} != pr_url repo {pr_repo!r} — a"
                    " worker-dispatching recipe would run in the wrong workspace"
                ),
            )
        )
    return results


def _check_worktree_paths_sessions(
    state: CwState | None = None,
) -> list[CheckResult]:
    """Verify each non-terminal session's worktree_path exists. Read-only, warn-only.

    Terminal sessions (COMPLETED, TIMED_OUT) have their worktrees cleaned up
    as part of normal lifecycle — a missing path is expected, not a fault.
    Only non-terminal sessions with a missing worktree path emit a warn.
    """
    if state is None:
        return []
    wt_paths: list[tuple[str, Path, SessionStatus]] = [
        (s.id, s.worktree_path, s.status)
        for s in state.sessions
        if s.worktree_path is not None
    ]
    total_checked = len(wt_paths)
    results: list[CheckResult] = []
    for session_id, wt, status in wt_paths:
        if status in TERMINAL_SESSION_STATUSES:
            continue
        if not wt.exists():
            results.append(
                CheckResult(
                    f"worktree/{session_id}",
                    ok=True,
                    warn=True,
                    detail=f"path does not exist: {wt}",
                )
            )
    missing_count = len(results)
    results.append(
        CheckResult(
            "worktree/summary",
            ok=True,
            warn=False,
            detail=(
                f"{total_checked} sessions checked, {missing_count} missing worktrees"
            ),
        )
    )
    return results
