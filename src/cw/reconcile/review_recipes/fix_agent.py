"""The fix_agent recipe: spawn the auto-dev-review fix loop as a cw session (#2017).

A narrow, single-call-site wrapper around spawn_create_impl, modeled on
address_review.py's _dispatch_address_review -- NOT an RFC-0010 detect/act
recipe (no dev-queue candidate, no dev_queue_lock, no fired_at latch).

Its single caller is ``cw.reconcile.fix_dispatch``'s post-lock dispatch phase,
which runs on a reconcile tick in a process that is never resident in the
ticket's worktree (#2017 R21). It is deliberately NOT called from the REVIEW
session that produced the action list: ``cw.spawn._write_hook_context`` refuses
any DAEMON spawn into a worktree whose ``cw-context.json`` names a still-live
session, and a review session dispatching into its own worktree is exactly that
case. The review session's responsibility ends at recording a
:class:`~cw.models.PendingFixDispatch` on its queue row and exiting.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cw.config import load_state
from cw.events import record_event
from cw.exceptions import CwError, HookContextConflictError
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    TERMINAL_SESSION_STATUSES,
    OrchestratorEventType,
    SessionPurpose,
)
from cw.worktree import _git_dir, _run_git, create_worktree, worktree_path_for

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig


def _refuse_if_worktree_references_live_session(
    client: ClientConfig, branch: str
) -> None:
    """Raise if the target worktree's hook context names a non-terminal session.

    A read-only pre-check, and a deliberate duplication of
    ``cw.spawn._write_hook_context``'s DAEMON-branch interpretation rather than
    a helper extracted from it: R21.1 forbids editing that guard at all, and
    extraction would edit it. The duplication is safe by construction because
    this is only a fast-fail -- ``spawn_create_impl``'s own unmodified call to
    that guard remains the authoritative enforcement at the end of
    :func:`dispatch_fix_agent`. A divergence between the two can therefore only
    ever produce a slower refusal, never an unsafe spawn.

    Its value is R22: it moves the refusal ahead of the fetch/merge, so a
    dispatch that cannot succeed leaves the worktree byte-identical.
    """
    context_path = worktree_path_for(client, branch) / HOOK_CONTEXT_RELATIVE_PATH
    if not context_path.exists():
        return
    try:
        prior = json.loads(context_path.read_text(encoding="utf-8"))
        prior_session_id: str | None = prior.get("session_id")
    except (OSError, json.JSONDecodeError):
        return
    if prior_session_id is None:
        return
    prior_sess = load_state().find_by_name_or_id(prior_session_id)
    if prior_sess is None or prior_sess.status in TERMINAL_SESSION_STATUSES:
        return
    msg = (
        f"dispatch_fix_agent: {context_path} references live session "
        f"{prior_session_id!r} (status: {prior_sess.status}). Refusing to "
        "dispatch the fix agent into a worktree another session still holds."
    )
    raise HookContextConflictError(msg, conflicting_session_id=prior_session_id)


def dispatch_fix_agent(
    *,
    client: ClientConfig,
    branch: str,
    prompt: str,
    label: str,
    ticket_id: str,
    lane: str,
    parent: str,
) -> str:
    """Provision the ticket's worktree, refresh it against main, dispatch the fix agent.

    Provisioning reuses ``create_worktree(client, branch)`` -- the same
    branch-keyed helper ``dispatch/claim.py`` calls, resolving to the same
    per-ticket worktree path every pipeline stage reuses
    (``allow_dirty_reuse=True``). Since the async redesign the review stage no
    longer removes that worktree, so this call almost always hits
    ``create_worktree``'s idempotent-reuse branch rather than provisioning
    anything.

    Order is load-bearing (R22): the two pure reads -- the live-session
    pre-check and the HEAD verification -- both run before ``fetch``/``merge``,
    the only mutating steps. A precondition failure therefore leaves the
    worktree untouched and needs no compensating restore.

    The HEAD verification confirms HEAD landed on the expected
    ``origin/<branch>`` commit (replacing an agent eyeballing ``git log
    --oneline -1``); the merge of ``origin/<default_branch>`` puts the fix on
    top of any sibling PR that merged mid-pipeline -- without it a later push
    would silently ship a branch missing main's commits (CI passes because it
    runs branch-HEAD, not the branch-merged-with-main state). On conflict the
    merge is aborted and a :exc:`CwError` names the conflicting files -- never
    force, never auto-resolve.

    ``prompt`` is the action-list TEXT, carried here from the queue row's
    :class:`~cw.models.PendingFixDispatch`. It is deliberately not a path: the
    worktree that would hold such a file is not a durable surface (R21.4).

    ``headless`` is deliberately NOT a parameter: this dispatch is always
    ``headless=False``. A headless session that never emits ``AUTO_DEV_RESULT``
    (the fix agent never does) defers forever in the Stop hook since ADR-0014
    (``_handle_headless_no_sentinel``).

    Passes NO ``task=`` kwarg (mirrors address_review's "Resolution 6: no
    dev-queue correlation") -- ``task.attempts`` and lane occupancy are
    untouched.

    Unlike ``_dispatch_address_review``, does NOT catch ``CwError``: the caller
    (``cw.reconcile.fix_dispatch``) distinguishes a transient
    :exc:`HookContextConflictError` (retry next tick) from a hard failure
    (clear the latch and escalate), and can only do so if both reach it.
    """
    _refuse_if_worktree_references_live_session(client, branch)
    worktree = create_worktree(client, branch, allow_dirty_reuse=True)

    expected_sha = _run_git(
        "rev-parse", f"origin/{branch}", cwd=_git_dir(client)
    ).stdout.strip()
    actual_sha = _run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    if actual_sha != expected_sha:
        msg = (
            f"dispatch_fix_agent: worktree HEAD ({actual_sha}) does not "
            f"match origin/{branch} ({expected_sha}) after create_worktree "
            "-- refusing to dispatch the fix agent against unexpected "
            "branch state."
        )
        raise CwError(msg)

    _run_git("fetch", "origin", cwd=_git_dir(client))
    merge_result = _run_git(
        "merge",
        f"origin/{client.default_branch}",
        "--no-edit",
        cwd=worktree,
        check=False,
    )
    if merge_result.returncode != 0:
        _raise_merge_conflict(client, branch, worktree)

    # Function-local import breaks the cw.spawn <-> cw.reconcile cycle
    # (address_review.py does the identical thing for the same reason).
    from cw.spawn import spawn_create_impl

    session_id = spawn_create_impl(
        client=client,
        worktree=worktree,
        prompt=prompt,
        label=label,
        headless=False,
        ticket_id=ticket_id,
        lane=lane,
        parent=parent,
        purpose=SessionPurpose.FIX,
    )
    # R24 MUST_FIX: mirrors dispatch/claim.py's own post-spawn emission, so a
    # fix session is as auditable as any other dispatched session. "client" is
    # the client NAME, not the ClientConfig -- every other event payload's
    # "client" key is a plain string, and the object would not serialize.
    record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {
            "ticket_id": ticket_id,
            "client": client.name,
            "session_id": session_id,
            "lane": lane,
        },
        correlation_id=ticket_id,
    )
    return session_id


def _raise_merge_conflict(client: ClientConfig, branch: str, worktree: Path) -> None:
    """Abort the conflicted merge and raise, naming the conflicting files.

    R24 SHOULD_FIX: the abort's own exit status is checked. A failed abort
    leaves the worktree mid-merge, so the error must say so rather than
    repeating the clean-tree claim the successful path makes -- an operator
    acting on "worktree left clean" against a half-merged tree is exactly the
    wrong next move.
    """
    conflicts = _run_git(
        "diff",
        "--name-only",
        "--diff-filter=U",
        cwd=worktree,
        check=False,
    ).stdout.strip()
    abort_result = _run_git("merge", "--abort", cwd=worktree, check=False)
    base = (
        f"dispatch_fix_agent: merging origin/{client.default_branch} "
        f"into {branch} conflicted"
    )
    if abort_result.returncode != 0:
        msg = (
            f"{base} AND the abort itself failed (exit "
            f"{abort_result.returncode}); worktree state is NOT verified "
            f"clean and may be mid-merge. Conflicting files:\n{conflicts}"
        )
    else:
        msg = (
            f"{base}; merge aborted, worktree left clean. "
            f"Conflicting files:\n{conflicts}"
        )
    raise CwError(msg)
