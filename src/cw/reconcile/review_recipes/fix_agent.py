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
import logging
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.exceptions import (
    CwError,
    HookContextConflictError,
    RemoteRefUnresolvedError,
    WorktreeOccupiedError,
)
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    TERMINAL_SESSION_STATUSES,
    OrchestratorEventType,
    SessionPurpose,
)
from cw.native_daemon import get_native_daemon_client
from cw.session_retention import find_session_by_id
from cw.worktree import (
    ReuseRefreshReport,
    _git_dir,
    _ref_exists,
    _run_git,
    _upstream_ref,
    create_worktree,
    worktree_path_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.native_daemon import NativeDaemonClient

_log = logging.getLogger("cw.reconcile.review_recipes")


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
    prior_sess = find_session_by_id(prior_session_id)
    if prior_sess is None or prior_sess.status in TERMINAL_SESSION_STATUSES:
        return
    msg = (
        f"dispatch_fix_agent: {context_path} references live session "
        f"{prior_session_id!r} (status: {prior_sess.status}). Refusing to "
        "dispatch the fix agent into a worktree another session still holds."
    )
    raise HookContextConflictError(msg, conflicting_session_id=prior_session_id)


def _resolve_fix_remote_ref(
    branch: str, remote_branch: str | None, worktree: Path
) -> str | None:
    """Return the first candidate remote ref whose TIP equals *worktree*'s HEAD.

    Three rungs, in priority order: the impl sentinel's reported branch (when
    one was reported), the checked-out branch's configured ``@{u}``, and the
    templated ``origin/<branch>`` guess (#2145's fallback).

    Selection is on tip-equality, not existence (#2209). A candidate that
    resolves but points somewhere other than HEAD is SKIPPED and the ladder
    keeps walking — it neither wins nor raises. That is the whole divergence
    from :func:`cw.worktree._resolve_remote_ref`, which stops at the first ref
    that merely exists and so cannot express "exists but stale, keep going":
    under git's default ``branch.autoSetupMerge`` a cw dispatch worktree's
    ``@{u}`` is ``origin/<default_branch>``, which always exists and is never
    the fix branch, and treating that as the answer reproduced the exact
    review -> failed dispatch -> review loop #2209 exists to end.

    ``None`` only once no rung's tip matches — the caller turns that into a
    :exc:`RemoteRefUnresolvedError`.
    """
    head_sha = _run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    upstream = _upstream_ref(worktree)
    candidates = [
        f"origin/{remote_branch}" if remote_branch else None,
        upstream,
        f"origin/{branch}",
    ]
    for candidate in candidates:
        if candidate is None or not _ref_exists(candidate, worktree):
            continue
        if _run_git("rev-parse", candidate, cwd=worktree).stdout.strip() == head_sha:
            return candidate
    return None


def dispatch_fix_agent(
    *,
    client: ClientConfig,
    branch: str,
    prompt: str,
    label: str,
    ticket_id: str,
    lane: str,
    parent: str,
    remote_branch: str | None = None,
    native_daemon: NativeDaemonClient | None = None,
) -> str:
    """Provision the ticket's worktree, refresh it against main, dispatch the fix agent.

    Provisioning reuses ``create_worktree(client, branch)`` -- the same
    branch-keyed helper ``dispatch/claim.py`` calls, resolving to the same
    per-ticket worktree path every pipeline stage reuses
    (``allow_dirty_reuse=True``). Since the async redesign the review stage no
    longer removes that worktree, so this call almost always hits
    ``create_worktree``'s idempotent-reuse branch rather than provisioning
    anything.

    Order is load-bearing (R22): the live-session pre-check is a pure read that
    runs before anything touches the worktree. ``create_worktree`` is then
    called with ``refresh_on_reuse=True`` (#2213): a network ``git fetch`` of
    ``origin/<branch>`` (can be slow; a failed fetch skips the fast-forward and
    uses the worktree as-is), then a fast-forward of the reused worktree to it
    -- only when the worktree is unoccupied (no unsaved work, no live session
    in cw state or worker in the daemon roster homed on it, occupancy
    re-checked immediately before the merge), clean, on the expected branch
    and strictly behind; otherwise it is left exactly as it is. Never a
    reset. A fast-forward that moves HEAD records one ``worktree.fast_forwarded``
    audit event carrying *ticket_id* (see ``docs/events.md``).

    **"The refresh did not move the worktree" means two different things, and
    only one of them stops this dispatch.**

    - *Occupied -- abort.* A live cw session, a live daemon-roster worker, or an
      indeterminate read of either (fail closed) means another worker may be
      operating in the tree. ``create_worktree`` RAISES
      :exc:`~cw.exceptions.WorktreeOccupiedError` for it, and this function
      converts that straight away into the existing transient
      :exc:`HookContextConflictError`, before any later step can act on the
      worktree: the HEAD verification, ``git fetch``, the merge of
      ``origin/<default_branch>`` into it, the hook-context write and the spawn.
      Refusing only the fast-forward and then merging into and spawning onto a
      tree a live worker is using would defeat the guard's whole purpose. The
      conflict is the transient one (retried next tick, escalated by
      ``cw.reconcile.fix_dispatch`` once it stops being transient), so nothing
      is lost by skipping.
    - *Not refreshed -- proceed.* Unsaved work (this path legitimately reuses a
      worktree carrying a prior stage's churn), a failed fetch, a diverged
      branch, a fast-forward git refused, an OS error or a branch absent from
      origin all leave the tree the caller's to use as it is. The dispatch goes
      on, and each failure is named in a friction note (below).

    Past the occupancy refusal the HEAD verification runs, before
    ``fetch``/``merge``, the only other mutating steps. A precondition failure
    therefore leaves the worktree untouched except for the fast-forward, which
    strictly advances HEAD and needs no compensating restore. Unlike
    ``create_worktree``, this caller has a friction surface (the prompt prefix),
    so each refresh failure -- fetch failed (with git's reason), fast-forward
    refused, diverged, an OS error -- reported through the report's ``notes`` is
    named there, worktree and reason, alongside the log line.

    The HEAD verification IS the ref resolution since #2209:
    :func:`_resolve_fix_remote_ref` walks a three-rung ladder -- the impl
    sentinel's *remote_branch*, then the branch's ``@{u}`` (#2145), then the
    templated ``origin/<branch>`` guess -- and returns the first candidate
    whose TIP equals this worktree's HEAD, skipping any that resolve but are
    stale. Nothing matching raises :exc:`RemoteRefUnresolvedError`, which the
    caller parks on rather than retrying. ``remote_branch`` is the branch the
    impl session's sentinel reported having pushed, which need not be the
    templated name; ``branch`` stays the local/worktree key either way, because
    every other pipeline stage provisions this ticket's worktree under it.
    (This replaces an agent eyeballing ``git log --oneline -1``.) The merge of
    ``origin/<default_branch>`` puts the fix on
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

    ``native_daemon`` (#2213 round 7) is this dispatch's own
    :class:`~cw.native_daemon.NativeDaemonClient`, threaded into
    ``create_worktree``'s occupancy check instead of that check defaulting to
    :func:`~cw.native_daemon.get_native_daemon_client` several calls down.
    This function is the entry point (``cw.reconcile.fix_dispatch`` has none in
    scope today), so it defaults to the real client when omitted -- the same
    shape as :func:`cw.session.start_session` -- and tests inject
    :class:`~cw.native_daemon.FakeNativeDaemonClient` here directly.

    ``parent`` is resolved via :func:`cw.session_retention.find_session_by_id`
    (cw id, ``claude_session_id``, or an archived session -- #2149) rather
    than passed straight through to ``spawn_create_impl``. When it cannot be
    resolved at all, this is NOT treated as a hard failure: a friction note is
    prepended to *prompt* and a warning is logged, but the fix agent still
    spawns with ``parent=None``. Losing parent lineage is strictly better than
    losing the fix-loop handoff outright -- the caller's broad ``except
    CwError`` would otherwise clear the latch and page the operator for a
    problem the fix agent itself doesn't need the parent link to solve.
    """
    resolved_parent = find_session_by_id(parent)
    effective_parent: str | None = None
    effective_prompt = prompt
    if resolved_parent is not None:
        effective_parent = resolved_parent.id
    else:
        effective_prompt = (
            f"_Friction note: parent session {parent!r} could not be resolved "
            "(checked hot and archived state) -- this fix agent has no "
            "recorded parent session lineage._\n\n"
        ) + prompt
        _log.warning(
            "dispatch_fix_agent: could not resolve parent %r (checked hot and "
            "archived state); spawning with parent=None",
            parent,
        )

    daemon = native_daemon or get_native_daemon_client()
    _refuse_if_worktree_references_live_session(client, branch)
    refresh = ReuseRefreshReport()
    try:
        worktree = create_worktree(
            client,
            branch,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=refresh,
            ticket_id=ticket_id,
            native_daemon=daemon,
        )
    except WorktreeOccupiedError as exc:
        # A live session or worker may be homed on this worktree. Every step
        # below mutates it (fetch, merge, hook-context write, spawn), so none
        # may run: skip the whole dispatch, worktree untouched, and let the
        # caller's transient-conflict handling retry.
        msg = (
            f"dispatch_fix_agent: worktree {exc.path} for {branch} may be held "
            f"by a live session or daemon worker ({exc.reason}). "
            "Refusing to fetch, merge or dispatch the fix agent into it; the "
            "worktree was not touched."
        )
        raise HookContextConflictError(msg) from exc
    effective_prompt = (
        "".join(f"_Friction note: {note}_\n\n" for note in refresh.notes)
        + effective_prompt
    )

    if _resolve_fix_remote_ref(branch, remote_branch, worktree) is None:
        upstream = _upstream_ref(worktree)
        reported_clause = (
            f"reported remote branch origin/{remote_branch} does not resolve; "
            if remote_branch
            else ""
        )
        msg = (
            f"dispatch_fix_agent: cannot determine remote ref for {branch} "
            f"-- {reported_clause}configured upstream is {upstream!r} and it "
            f"does not resolve, and origin/{branch} does not resolve either."
            if upstream is not None
            else f"dispatch_fix_agent: cannot determine remote ref for "
            f"{branch} -- {reported_clause}no upstream configured, and "
            f"origin/{branch} does not resolve either."
        )
        raise RemoteRefUnresolvedError(msg)

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
        prompt=effective_prompt,
        label=label,
        headless=False,
        ticket_id=ticket_id,
        lane=lane,
        parent=effective_parent,
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
