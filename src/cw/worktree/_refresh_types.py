"""Option, result and report types for the reuse refresh (#2213, #2238).

:class:`RefreshOutcome`, :class:`RefreshResult`, :class:`ReuseRefreshReport`
and :class:`ReuseRefreshOptions` are shared by the occupancy check, the
fast-forward, the refresh orchestration and ``create_worktree``, so they live
in this runtime dependency-free leaf. It also holds :data:`_LOGGER_NAME`, the
one logger name the reuse-refresh submodules share.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cw.native_daemon import NativeDaemonClient

# Every reuse-refresh submodule logs under the pre-split ``_refresh`` module
# name, so operator log filters survive the #2569 split.
_LOGGER_NAME = "cw.worktree._refresh"


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

    ``create_worktree`` returns only a path. A caller that opts into the
    refresh and needs more than that passes one of these in, as
    ``refresh=ReuseRefreshOptions(report=...)``:

    - ``notes``: one single-line entry per refresh FAILURE the caller cannot
      otherwise see, each naming the worktree and the reason -- the fetch
      failed (with git's reason), git refused the fast-forward, the branch
      diverged from origin, an OS error aborted the refresh, or a submodule
      sync failed. A caller with a friction surface prints them. Designed
      non-actions add nothing: branch absent from origin with no commits of
      its own, already equal or ahead, or a worktree that is occupied. One
      exception (#2328): a branch absent from origin that DOES have commits
      of its own also gets a note, even though leaving it untouched is the
      designed action -- the reader (a fix_agent prompt, a later pipeline
      stage) needs to know it may be building on a stale base.
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


@dataclass(frozen=True)
class ReuseRefreshOptions:
    """A caller's opt-in to ``create_worktree``'s reuse refresh (#2238).

    Passing one of these as ``refresh=`` IS the opt-in; ``refresh=None`` (the
    default) is path resolution only. The fields used to be four loose
    ``create_worktree`` kwargs, three of which were silently ignored unless a
    fourth flag was set. Grouping them makes "a report, ticket or daemon
    without the refresh" unrepresentable, so there is nothing to validate.

    - ``report``: the optional :class:`ReuseRefreshReport` out-parameter.
      ``None`` is for a caller with no friction surface (the dispatch claim);
      ``create_worktree`` then refreshes into a throwaway report. The options
      object is frozen but the report it holds is not: it is the caller's own
      object, filled in before ``create_worktree`` returns or raises.
    - ``ticket_id``: the ticket the caller is working, for the
      ``worktree.fast_forwarded`` audit event only (payload ``ticket_id`` and
      ``correlation_id``). It has no other effect.
    - ``native_daemon``: the caller's own
      :class:`~cw.native_daemon.NativeDaemonClient` for the occupancy check's
      roster read, so an injected fake is consulted instead of the host's real
      roster. ``None`` uses :func:`~cw.native_daemon.get_native_daemon_client`.
    """

    report: ReuseRefreshReport | None = None
    ticket_id: str | None = None
    native_daemon: NativeDaemonClient | None = None
