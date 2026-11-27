"""Impl-bypass plan check: can a requeue read the approved plan (#3739)."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.dev_queue.lifecycle import _plan_body_signoff_ok
from cw.gh import fetch_approved_plan_comment
from cw.models import resolve_feature_branch
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.worktree import _checked_out_branch, _git_dir, worktree_path_for

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask


def _impl_bypass_worktree_path(task: TicketTask, client_cfg: ClientConfig) -> Path:
    """Worktree path the impl-bypass guard would find for ``task``.

    Single source for the branch-name/worktree-path formula shared by
    :func:`_impl_bypass_plan_available` (which uses it to check for
    ``.cw/plan.md``) and the guard's own error message in
    :func:`_apply_requeue_stage` (which cites the same path in its refusal
    text) -- keeps the two from drifting independently. See GitHub #1681.
    """
    branch = resolve_feature_branch(task.ticket_id, client_cfg, task)
    return worktree_path_for(client_cfg, branch)


class _ImplBypassPlanCheck(NamedTuple):
    """Verdict from :func:`_impl_bypass_plan_available` (GitHub #1906).

    A named 3-tuple rather than a bare ``bool`` so the caller can build an
    honest refusal message without re-deriving tracker state. ``tracker_checked``
    is True only when :func:`fetch_approved_plan_comment` (GitHub-only) was
    actually called -- i.e. the tracker was ``None`` (unresolvable, fail-open)
    or positively ``github-issues``. It is False when a *known, non-GitHub*
    tracker (e.g. ``linear``) caused the call to be skipped -- that case must
    not be described as "no reviewed plan comment ... was found on the
    tracker", since the tracker was never actually asked.
    """

    available: bool
    tracker_checked: bool
    tracker: str | None


def _impl_bypass_plan_available(
    task: TicketTask,
    client_cfg: ClientConfig,
    *,
    allow_tracker_fallback: bool = True,
) -> _ImplBypassPlanCheck:
    """Verdict on whether an approved plan is available for a forward bypass
    to IMPL.

    Local-first, tracker-fallback -- the inverse order of
    :func:`cw.dev_queue.lifecycle._plan_is_reviewed` (tracker-first,
    ``.cw/plan.md``-fallback). That predicate is an approval-freshness gate,
    so it asks the tracker first and would always pay for a network call on
    the common path here. This predicate instead computes the real worktree
    path via :func:`worktree_path_for` + :func:`_checked_out_branch` -- the
    same read-only primitives :func:`cw.worktree.create_worktree` uses to
    decide reuse-vs-rebuild (and that ``lifecycle._local_plan_path`` now
    shares for its own fallback, since dispatch stamps ``worktree_path`` on
    the Session but never on the ``TicketTask``) -- so the common "reused
    worktree, no rebuild" case is resolved with zero network cost. Only falls through to
    :func:`fetch_approved_plan_comment` when the local read fails to prove
    the plan is there (worktree missing, foreign branch, or no/unmarked
    ``.cw/plan.md``). See GitHub #1681.

    :func:`fetch_approved_plan_comment` is GitHub-only (``src/cw/gh/``) --
    calling it against a Linear-tracked (or other non-GitHub) client silently
    returns ``None`` (a ``gh`` call against a non-GitHub-shaped ticket id
    just fails), which previously masqueraded as "the tracker was checked and
    had no reviewed comment" (#1906). The fallthrough is now gated on the
    resolved tracker: fail-open (attempt the GitHub fetch, matching prior
    behavior) when ``resolve_tracker`` returns ``None`` (unresolvable/absent
    config -- verified backward-compatible against every existing test, all
    of which resolve ``tracker=None``); skip the call entirely, honestly,
    when the tracker is *positively* known to be non-GitHub.

    ``allow_tracker_fallback`` (GitHub #1286): when False, a local miss
    returns unavailable immediately, without calling ``resolve_tracker`` or
    ``fetch_approved_plan_comment`` at all. The manual CLI call site
    (``cw dev-queue requeue --stage impl``, via :func:`_apply_requeue_stage`)
    keeps the default ``True`` -- an explicit, one-off operator command can
    afford a network round trip. The automatic dispatch call site
    (``cw.dispatch.claim._spawn_claimed_task``) passes ``False``: it runs on
    every claimed task, every tick, so paying for a tracker fetch on the
    common "no plan yet" path would tax the hot per-claim loop for no benefit
    -- Stage 1 is about to run and post the plan anyway.
    """
    branch = resolve_feature_branch(task.ticket_id, client_cfg, task)
    wt_path = _impl_bypass_worktree_path(task, client_cfg)
    if wt_path.exists() and _checked_out_branch(wt_path) == branch:
        plan_path = wt_path / ".cw" / "plan.md"
        try:
            body = plan_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            body = None
        if body is not None and _plan_body_signoff_ok(body):
            return _ImplBypassPlanCheck(True, tracker_checked=False, tracker=None)

    if not allow_tracker_fallback:
        return _ImplBypassPlanCheck(False, tracker_checked=False, tracker=None)

    tracker = resolve_tracker(client_cfg.workspace_path)
    if tracker is not None and tracker != TRACKER_GITHUB_ISSUES:
        return _ImplBypassPlanCheck(False, tracker_checked=False, tracker=tracker)

    tracker_body = fetch_approved_plan_comment(task.ticket_id, cwd=_git_dir(client_cfg))
    available = tracker_body is not None and _plan_body_signoff_ok(tracker_body)
    return _ImplBypassPlanCheck(available, tracker_checked=True, tracker=tracker)
