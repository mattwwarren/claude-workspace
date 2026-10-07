"""Boot-time pass over codex sessions orphaned by a crash (GitHub #1727).

Since RFC 0014 A2 (#2388), ``CodexExecutor.spawn()`` launches the review as a
detached ``cw codex run`` job carrying a ``Session.local_liveness`` handle; such
a session survives a serve restart and is skipped here (reconcile/local's codex
harvest branch owns it). What follows describes the pre-migration sessions this
pass still covers until B2 retires it: those whose review ran on a background
thread inside serve, where an ordinary process exit can land mid-review.
``run_dispatch_loop``'s shutdown path covers the exits we control by
bounded-joining those threads
(``cw.codex_background.join_outstanding_codex_threads``). A crash or ``SIGKILL``
is the case a join cannot reach at all: the process that owned the thread is
already gone, so there is nothing left to join and nothing recorded a failure.
What survives is a session still marked ``ACTIVE``, a task still ``RUNNING``,
and possibly a half-committed worktree.

This module is the other half of that pair: run once per process before the
first dispatch tick, it treats any live codex-origin headless ``DAEMON``
session as evidence of exactly that and dispositions it one of two ways
(#2285):

- **Requeue** (RUNNING -> PENDING, same stage) only when the lane's
  ``reap_policy`` resolves to ``auto`` (ADR-0006: a revert is a destructive
  act) AND the orphan is provably clean — no codex process is left writing in
  the worktree, the lane's codex fix loop is off, the worktree carries nothing
  uncommitted beyond ``.claude/review-verdict.md``, and HEAD still matches the
  review's recorded baseline. Emits ``TICKET_REQUEUED`` once the
  identity-checked revert has actually happened.
- **Park** for operator inspection in every other case, exactly as before —
  any uncertainty (a git error, a git timeout, an unresolvable ref) parks.

The pass runs while ``serve`` is starting, so it never touches the network
(the baseline is resolved from local refs only) and bounds every git call.

A codex process still running in the worktree — including one whose cwd is
the worktree's since-deleted directory — is a live writer. The pass never
signals one, under any ``reap_policy``: an orphan whose parent ``serve`` died
normally exits on its own once its stdout pipe breaks. When the process scan
finds a writer, or cannot tell (an unreadable candidate, a process racing
away mid-scan, a failed scan, no recorded worktree to scan), the pass parks
the task, leaves the ``Session`` ``ACTIVE``, proposes the reap through
reconcile's shared ``SESSION_REAP_PROPOSED`` emitter (ADR-0006 signal-only)
and names the pids, or "scan inconclusive", in the breadcrumbs. The next boot
or reconcile tick re-evaluates, and the operator can act on the proposal.

Only a scan that affirmatively finds no writer lets the orphaned ``Session``
record close to ``COMPLETED``/``CRASHED``, in both branches, and each close
records a ``SESSION_COMPLETED`` audit event (``crashed: True``, ``reason:
codex_orphaned_at_boot``) before it is persisted. Before #2285 it stayed
``ACTIVE`` forever: it held a client ceiling slot, and its stale
``cw-context.json`` made the next DAEMON-origin ``_write_hook_context`` into
the same worktree raise ``HookContextConflictError``.

Two existing primitives carry the task transition rather than a new path:

- ``cw.queue_rows._park_running_task_blocked_on_user`` — the shared
  "park this task for operator inspection and emit SESSION_NEEDS_ATTENTION"
  primitive already used by the dirty-worktree guard and the codex capability
  gate (#1238, #1257).
- ``cw.queue_rows._revert_claimed_task_to_pending`` — the shared
  RUNNING -> PENDING revert, which charges an unproductive attempt so a serve
  crash loop stays bounded by the global attempt ceiling.

The codex-origin test, ``cw.executor.resolve_executor_config(...).backend !=
CODEX_BACKEND``, is lifted from ``claim.py``'s capability gate.

This is a blast-radius bound, not a liveness handle: it does not make codex
sessions crash-recoverable in the RFC 0005 F3 sense (no PID/surface_ref is
persisted for external harvest). See the ``StageExecutor`` Protocol invariant
comment in ``cw.executor`` for the accepted gap this bounds.

Two sibling modules import this module's private helpers: ``cw.reconcile
.codex_reparks`` re-runs ``_resolve_orphan_action`` on reconcile ticks (#2307),
and ``cw.reconcile.local``'s codex harvest branch (RFC 0014 A1, #2387) reuses
the clean-requeue primitives (``_worktree_porcelain_clean_except_verdict``,
``_head_matches_pre_review_ref``, read through a :class:`CleanProbe` since
#2563) and the ``_PARK_REASON_*`` constants for a
codex session whose recorded PID has died. That branch skips the live-writer
scan (the recycled-PID guard already proved the process dead) and evaluates
every gate check rather than short-circuiting, so its audit event can report
each one. Changing these helpers' contracts changes both consumers.

``cw.codex_legacy_recovery`` (``cw codex migrate-legacy``, RFC 0014 B1,
#2389) is a third consumer, through the public ``live_writer_park`` and
``stale_snapshot_reason``: a legacy session carries no PID to prove dead, so
it runs this pass's live-writer scan first, and it revalidates its unlocked
snapshot with the same staleness check before acting.

Those three consumers decide under ``sessions_lock``, where no subprocess may
run (ADR-0019), so none of them runs the clean check's git there (#2563).
Their git checks run in a lockless pre-pass instead, through this module's
:class:`CleanProbes` seam: ``reconcile()`` and the legacy recovery *capture*
both primitives' answers per candidate (:meth:`CleanProbes.capture`) before
taking the lock, and the in-lock code only *consumes* them
(:meth:`CleanProbes.lookup`), never running git. A lookup that finds no probe,
a probe captured for another worktree or baseline, or one at least
``CLEAN_PROBE_MAX_AGE_SECONDS`` old raises :class:`CleanProbeUnavailableError`,
and the consumer *defers*: it leaves the candidate exactly as it found it for
the next tick, with nothing requeued, closed, parked, backoff-stamped or
audited off it. That stays fail-closed, because every requeue or park needs a
fresh hit, and it is not a clock-driven disposition (ADR-0014): an expired
probe only delays a re-check. A probe that captured a git error is real
evidence and still parks as ``git_error``. Capture-lockless, consume-in-lock,
defer-on-miss is the template for the remaining in-lock git (#2546, #2548).
Because there is no atomic worktree-generation check spanning the pre-pass and
the in-lock mutation, a consumed probe cannot authorize a clean requeue either:
that candidate defers for a fresh decision. The boot pass itself runs unlocked
and keeps calling the primitives live.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

import psutil

from cw._git import capture_head_sha, run_git
from cw.codex_background import (
    REVIEW_VERDICT_COMMENT_RELATIVE_PATH,
    _resolve_codex_fix_loop_enabled,
)
from cw.config import (
    load_clients,
    load_effective_config,
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import dev_queue_lock, load_dev_queue
from cw.events import record_event
from cw.models import (
    CODEX_BACKEND,
    CompletionReason,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionStatus,
)
from cw.queue_rows import (
    _park_running_task_blocked_on_user,
    _revert_claimed_task_to_pending,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    ProposedAction,
    ReapCandidate,
    _emit_reap_proposed,
    _is_headless,
    feature_branch_key,
    ticket_id_for_session,
)
from cw.reconcile.tasks import _resolve_task_policy

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cw.models import (
        ClientConfig,
        CwState,
        OrchestratorConfig,
        Session,
        Stage,
        TicketTask,
    )

_log = logging.getLogger(__name__)

# Short reason code stamped as the task's disposition and carried as the
# SESSION_NEEDS_ATTENTION payload's ``paused_status``.
CODEX_ORPHANED_AT_BOOT_DISPOSITION = "codex_review_orphaned_at_boot"

# TICKET_REQUEUED ``reason`` for a provably-clean orphan put back to PENDING.
CODEX_ORPHAN_CLEAN_REQUEUE_REASON = "codex_orphan_clean_requeue_at_boot"

# SESSION_COMPLETED ``reason`` for an orphaned session this pass closes, and
# its ``disposition`` values (the task transition decided on).
CODEX_ORPHAN_CLOSE_REASON = "codex_orphaned_at_boot"
_CLOSE_DISPOSITION_REQUEUED = "requeued"
_CLOSE_DISPOSITION_PARKED = "parked"

_ORPHAN_BREADCRUMBS = (
    "ACTIVE codex-origin session found at process start; its background review"
    " thread did not survive the prior process exit (crash/SIGKILL) — inspect"
    " the worktree for a partial commit or orphaned scratch dir before"
    " reclaiming."
)

# Why a park, not a requeue — appended to _ORPHAN_BREADCRUMBS.
_PARK_REASON_REAP_POLICY_NOT_AUTO = (
    "the lane's reap_policy does not authorize automatic requeue"
)
_PARK_REASON_FIX_LOOP_ENABLED = "the lane's codex fix loop is enabled"
_PARK_REASON_DIRTY_WORKTREE = (
    "the worktree carries uncommitted changes beyond the review verdict"
)
_PARK_REASON_GIT_ERROR = "the worktree's git state could not be established"
_PARK_REASON_HEAD_MOVED = "HEAD has moved since the review's recorded baseline"
_PARK_REASON_CODEX_PROCESS_RUNNING = "a codex process is still running in the worktree"
# Leads every park reason for a scan that could not tell whether a codex
# writer is left, so the breadcrumbs say so in the same words each time.
_SCAN_INCONCLUSIVE = "scan inconclusive"
_PARK_REASON_PROCESS_SCAN_INCONCLUSIVE = (
    f"{_SCAN_INCONCLUSIVE}: the process table could not rule out a lingering"
    " codex writer"
)
_PARK_REASON_NO_WORKTREE_PATH = (
    f"{_SCAN_INCONCLUSIVE}: no worktree path is recorded, so a lingering codex"
    " writer cannot be ruled out"
)

# Why a boot snapshot went stale before its session could be closed.
_STALE_SESSION_GONE = "it is no longer in sessions.json"

# Bounds every git call below: this pass blocks process start, so one hung
# git must not wedge the dispatch loop before its first tick.
_GIT_SUBPROCESS_TIMEOUT_SECONDS: float = 10.0
# Porcelain v1: two status characters and a space precede the path.
_GIT_PORCELAIN_PATH_OFFSET = 3
_GIT_PORCELAIN_RENAME_SEPARATOR = " -> "
# psutil name() of the exec'd codex binary (codex_runner spawns it directly,
# no shell or interpreter wrapper).
_CODEX_PROCESS_NAME = "codex"
# What Linux reports (via /proc/<pid>/cwd, which psutil passes through) for a
# process whose cwd directory has been removed: "<path> (deleted)".
_DELETED_CWD_SUFFIX = " (deleted)"

# How old a captured clean probe may be when it is consumed under the lock
# (#2563). Strict ``<``: a probe exactly this old is stale. Expiry only defers
# the candidate to the next tick, so the value is a freshness bound, not a
# disposition clock.
CLEAN_PROBE_MAX_AGE_SECONDS: float = 120.0
# Wall-clock budget for one lockless capture pass. Well under the TTL, so git
# calls run serially for later candidates can never age the early probes past
# it; a candidate reached after the budget is spent gets no probe and defers.
CAPTURE_BUDGET_SECONDS: float = 60.0


@dataclass(frozen=True)
class _OrphanDisposition:
    """What the boot pass does with one orphan.

    ``close_session`` is False only while a codex writer may still be alive in
    the worktree: closing the record then would free its ceiling slot and its
    hook-context guard for a new spawn that races the writer. The session is
    then left ACTIVE and its reap proposed instead, for an operator to
    authorize (ADR-0006 signal-only).
    """

    should_requeue: bool
    reason: str
    close_session: bool = True


def _park(reason: str) -> _OrphanDisposition:
    return _OrphanDisposition(should_requeue=False, reason=reason)


def _park_writer_may_be_live(reason: str) -> _OrphanDisposition:
    return _OrphanDisposition(should_requeue=False, reason=reason, close_session=False)


def _worktree_porcelain_clean_except_verdict(worktree: Path) -> bool | None:
    """Return whether *worktree* is clean apart from the review verdict file.

    Tri-state: ``True`` clean (nothing, or only
    ``REVIEW_VERDICT_COMMENT_RELATIVE_PATH``, is pending), ``False`` dirty,
    ``None`` when git could not answer — kept distinct from ``False`` so the
    park reason says ``git_error`` rather than misreporting dirt. Parse shape
    mirrors ``cw.codex_fix_loop.commit._porcelain_changed_paths``, except a rename
    contributes BOTH sides: ``git mv tracked.md .claude/review-verdict.md``
    touches a tracked file, which must never read as clean.
    """
    try:
        completed = run_git(
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=worktree,
            capture_output=True,
            check=False,
            timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    allowed = str(REVIEW_VERDICT_COMMENT_RELATIVE_PATH)
    for line in completed.stdout.splitlines():
        entry = line[_GIT_PORCELAIN_PATH_OFFSET:]
        paths = entry.split(_GIT_PORCELAIN_RENAME_SEPARATOR, 1)
        if any(path != allowed for path in paths):
            return False
    return True


def _head_matches_pre_review_ref(
    worktree: Path, task: TicketTask, clients: dict[str, ClientConfig]
) -> bool | None:
    """Return whether *worktree*'s HEAD still equals the review's baseline.

    The baseline is ``task.stage_base_ref`` (HEAD as stamped when the review
    spawn succeeded). When that was never stamped, fall back to the local
    ``origin/<feature branch>`` tracking ref, naming the branch with the same
    ``feature_branch_key`` dispatch provisioned the worktree from. Local refs
    only: this runs before the first dispatch tick, so it never fetches.
    ``None`` when neither can be established — HEAD unreadable, no tracking
    ref, or git timing out — which the caller parks as a git error.
    """
    head = capture_head_sha(
        worktree, strict=False, timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS
    )
    if not head:
        return None
    if task.stage_base_ref:
        return head == task.stage_base_ref
    branch = feature_branch_key(task.client, task.ticket_id, clients)
    origin_sha = capture_head_sha(
        worktree,
        ref=f"origin/{branch}",
        strict=False,
        timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
    )
    if not origin_sha:
        return None
    return head == origin_sha


class CleanProbeUnavailableError(Exception):
    """No usable clean probe for a candidate: missing, mismatched or stale.

    A plain ``Exception``, deliberately not a ``CwError``, so no caller's broad
    ``except CwError`` swallows it: each consumer catches it by name and
    defers the candidate to the next tick (see the module docstring).
    """


@dataclass(frozen=True)
class CleanProbe:
    """One candidate's clean-check git answers, captured before the lock.

    ``clean`` and ``head_matches`` are exactly what
    ``_worktree_porcelain_clean_except_verdict`` and
    ``_head_matches_pre_review_ref`` returned, ``None`` still meaning git
    could not answer. ``worktree`` and ``baseline`` (what HEAD was compared
    against, see ``_baseline_ref``) are the identity a consumer re-checks.
    ``captured_at`` is stamped before the git calls, so the probe's age is
    measured conservatively from the start of the observation.
    """

    worktree: Path
    client: str
    ticket_id: str
    baseline: str
    clean: bool | None
    head_matches: bool | None
    captured_at: datetime


# How a clean-check consumer obtains a candidate's probe: live git
# (``probe_clean_state``), a lockless capture (``CleanProbes.capture``), or an
# in-lock lookup that never runs git (``CleanProbes.lookup``).
type ProbeSource = Callable[[Path, TicketTask, dict[str, ClientConfig]], CleanProbe]


def _baseline_ref(task: TicketTask, clients: dict[str, ClientConfig]) -> str:
    """The ref ``_head_matches_pre_review_ref`` compares HEAD against.

    Pure (no git): the stamped ``stage_base_ref``, else the local
    ``origin/<feature branch>`` tracking ref's name.
    """
    if task.stage_base_ref:
        return task.stage_base_ref
    return f"origin/{feature_branch_key(task.client, task.ticket_id, clients)}"


def _claim_identity(task: TicketTask) -> tuple[str | None, str | None]:
    """Return the task identities that bind a clean probe to its claim."""
    return task.session_id, task.codex_orphan_session_id


def probe_clean_state(
    worktree: Path, task: TicketTask, clients: dict[str, ClientConfig]
) -> CleanProbe:
    """Run both clean-check primitives live and record their answers.

    Never short-circuits: the harvest gate reports every check. Runs git, so
    it must never be called with ``sessions_lock`` held.
    """
    captured_at = datetime.now(UTC)
    clean = _worktree_porcelain_clean_except_verdict(worktree)
    head_matches = _head_matches_pre_review_ref(worktree, task, clients)
    return CleanProbe(
        worktree=worktree,
        client=task.client,
        ticket_id=task.ticket_id,
        baseline=_baseline_ref(task, clients),
        clean=clean,
        head_matches=head_matches,
        captured_at=captured_at,
    )


class CleanProbes:
    """Clean probes captured lockless, keyed by claim identity.

    One pass captures (:meth:`capture`, runs git, lockless only), then the
    in-lock consumers look up (:meth:`lookup`, never runs git). With
    *budget_seconds* set, captures stop once that much monotonic time has
    passed since construction. The private key includes ``client``,
    ``ticket_id``, ``session_id`` and ``codex_orphan_session_id``.
    """

    def __init__(self, *, budget_seconds: float | None = None) -> None:
        self.budget_seconds = budget_seconds
        self._deadline = (
            None if budget_seconds is None else monotonic() + budget_seconds
        )
        self._probes: dict[tuple[str, str, str | None, str | None], CleanProbe] = {}

    @property
    def captured_keys(self) -> frozenset[tuple[str, str]]:
        """The client/ticket pairs of probes captured so far.

        The private store also includes both claim identity fields; this
        compatibility view is only used for capture-pass diagnostics.
        """
        return frozenset((client, ticket_id) for client, ticket_id, *_ in self._probes)

    def capture(
        self, worktree: Path, task: TicketTask, clients: dict[str, ClientConfig]
    ) -> CleanProbe:
        """Probe *task*'s worktree live and keep the result. Lockless only.

        Raises ``CleanProbeUnavailableError`` before running any git once the
        budget is spent.
        """
        if self._deadline is not None and monotonic() >= self._deadline:
            msg = (
                f"the {self.budget_seconds:.0f}s clean-probe budget is spent;"
                f" {task.client}/{task.ticket_id} was not probed"
            )
            raise CleanProbeUnavailableError(msg)
        probe = probe_clean_state(worktree, task, clients)
        self._probes[task.client, task.ticket_id, *_claim_identity(task)] = probe
        return probe

    def lookup(
        self, worktree: Path, task: TicketTask, clients: dict[str, ClientConfig]
    ) -> CleanProbe:
        """Return *task*'s captured probe if it is still usable. Never runs git.

        Usable means captured for this same worktree and baseline, and aged in
        ``[0, CLEAN_PROBE_MAX_AGE_SECONDS)``: a negative age (the clock went
        backwards) fails closed too. Otherwise raises
        ``CleanProbeUnavailableError``.
        """
        who = f"{task.client}/{task.ticket_id}"
        probe = self._probes.get((task.client, task.ticket_id, *_claim_identity(task)))
        if probe is None:
            msg = f"no clean probe was captured for {who}"
            raise CleanProbeUnavailableError(msg)
        if (probe.worktree, probe.baseline) != (worktree, _baseline_ref(task, clients)):
            msg = f"the clean probe for {who} no longer matches its worktree/baseline"
            raise CleanProbeUnavailableError(msg)
        age = (datetime.now(UTC) - probe.captured_at).total_seconds()
        if not 0 <= age < CLEAN_PROBE_MAX_AGE_SECONDS:
            msg = f"the clean probe for {who} is unusable at age {age:.1f}s"
            raise CleanProbeUnavailableError(msg)
        return probe


def lookup_probe(probes: CleanProbes | None) -> ProbeSource:
    """The in-lock probe source: *probes*' lookup, or an always-miss one.

    ``None`` means nothing was captured, so every lookup misses and the
    consumer defers: fail closed.
    """
    return (probes if probes is not None else CleanProbes()).lookup


# Everything the scan can raise while reading or normalizing a candidate:
# psutil's own errors, OS errors, and ``Path.resolve`` failing on a symlink
# loop (``RuntimeError``) or an embedded NUL (``ValueError``). Any of them
# makes the scan inconclusive, never "no writer" (#2285 review round 4).
_SCAN_ERRORS: tuple[type[Exception], ...] = (
    psutil.Error,
    OSError,
    RuntimeError,
    ValueError,
)


def _cwd_is_worktree(cwd: str, worktree: Path, target: Path) -> bool:
    """Whether a process *cwd* is *worktree*, in either form the kernel reports.

    A process can sit in a directory that has since been removed (a deleted
    or re-provisioned worktree); its cwd then reads ``<path> (deleted)``, and
    it is still a live writer the pass must see.
    """
    plain = cwd.removesuffix(_DELETED_CWD_SUFFIX)
    return plain == str(worktree) or Path(plain).resolve() == target


def _codex_processes_in(worktree: Path) -> list[int] | None:
    """Return the pid of every process named ``codex`` whose cwd is *worktree*.

    A cwd scan is the only signal available: no PID is persisted for the
    codex child (see the module docstring). Never raises, and fails closed:
    ``None`` — inconclusive, which the caller parks on exactly as on a found
    writer — whenever the scan cannot tell. That is a failed listing, any
    entry racing away or erroring mid-scan, or a candidate psutil could not
    read: ``process_iter`` reports an ``AccessDenied`` attribute as ``None``,
    so a process whose name is ``None`` may be codex, and a codex whose cwd
    is ``None`` may sit in the worktree. Only ``[]`` means no writer.
    """
    try:
        target = worktree.resolve()
        processes = list(psutil.process_iter(["name", "cwd"]))
    except _SCAN_ERRORS:
        return None
    pids: list[int] = []
    for process in processes:
        try:
            info = process.info
            name = info.get("name")
            if name is None:
                return None
            if name != _CODEX_PROCESS_NAME:
                continue
            cwd = info.get("cwd")
            if cwd is None:
                return None
            if _cwd_is_worktree(cwd, worktree, target):
                pids.append(process.pid)
        except _SCAN_ERRORS:
            return None
    return pids


def _format_pids(pids: Sequence[int]) -> str:
    return ", ".join(f"pid {pid}" for pid in pids)


def live_writer_park(worktree: Path) -> _OrphanDisposition | None:
    """Park while a codex writer is, or may be, alive in *worktree*.

    ``None`` only when the scan affirmatively finds no writer. Nothing is ever
    signalled: a found writer and an inconclusive scan both leave the session
    ACTIVE for its reap to be proposed.
    """
    pids = _codex_processes_in(worktree)
    if pids is None:
        return _park_writer_may_be_live(_PARK_REASON_PROCESS_SCAN_INCONCLUSIVE)
    if pids:
        return _park_writer_may_be_live(
            f"{_PARK_REASON_CODEX_PROCESS_RUNNING} ({_format_pids(pids)})"
        )
    return None


def _park_reason_for_git(
    clean: bool | None, head_matches: Callable[[], bool | None]
) -> str | None:
    """The git park ladder, shared by live git and a captured probe.

    *head_matches* is lazy so the live boot path keeps its short-circuit: it
    never asks for HEAD once the tree is dirty or unreadable.
    """
    if clean is None:
        return _PARK_REASON_GIT_ERROR
    if not clean:
        return _PARK_REASON_DIRTY_WORKTREE
    matches = head_matches()
    if matches is None:
        return _PARK_REASON_GIT_ERROR
    if not matches:
        return _PARK_REASON_HEAD_MOVED
    return None


def _git_park_reason(
    worktree: Path,
    task: TicketTask,
    clients: dict[str, ClientConfig],
    probe: CleanProbe | None = None,
) -> str | None:
    """Return the park reason the worktree's git state implies, or None.

    With no *probe* the primitives run live, exactly as the boot pass always
    has; with one, its captured answers go through the same ladder.
    """
    if probe is None:
        return _park_reason_for_git(
            _worktree_porcelain_clean_except_verdict(worktree),
            lambda: _head_matches_pre_review_ref(worktree, task, clients),
        )
    return _park_reason_for_git(probe.clean, lambda: probe.head_matches)


def _resolve_orphan_action(
    worktree: Path | None,
    task: TicketTask,
    client: ClientConfig,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    probe_source: ProbeSource | None = None,
) -> _OrphanDisposition:
    """Decide requeue vs. park, and whether the session may close, for one orphan.

    The live-writer check runs first and under every policy, since it alone
    decides whether the session may be closed, and so the git checks observe
    a worktree nothing is still writing to. Past it, the reap policy is
    resolved with the same resolver ``reconcile.tasks`` gates its reverts on
    (ADR-0006): anything but ``reap_policy: auto`` parks; under ``auto`` the
    remaining gates run cheapest and most decisive first, and the first
    failing gate parks with its own reason.

    *probe_source* supplies the git answers (see ``_gate_clean_requeue``);
    omitted, the boot pass's live git runs. Raises
    ``CleanProbeUnavailableError`` when a lookup source misses at the git gate.
    """
    if worktree is None:
        # No path to scan is a scan that cannot run, so it is inconclusive. A
        # path that is recorded but no longer on disk still gets its scan (see
        # _cwd_is_worktree).
        return _park_writer_may_be_live(_PARK_REASON_NO_WORKTREE_PATH)
    live_writer = live_writer_park(worktree)
    if live_writer is not None:
        return live_writer
    policy = _resolve_task_policy(task.client, task.lane, clients, config)
    return _gate_clean_requeue(
        worktree,
        task,
        client,
        clients,
        config,
        auto=policy is ReapPolicy.AUTO,
        probe_source=probe_source,
    )


def _gate_clean_requeue(
    worktree: Path,
    task: TicketTask,
    client: ClientConfig,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    *,
    auto: bool,
    probe_source: ProbeSource | None = None,
) -> _OrphanDisposition:
    """With no writer left, requeue only a provably clean orphan under ``auto``.

    *probe_source* is consulted only once the cheap policy and fix-loop gates
    pass, so a missing probe is irrelevant whenever git would not be reached.
    """
    if not auto:
        return _park(_PARK_REASON_REAP_POLICY_NOT_AUTO)
    if _resolve_codex_fix_loop_enabled(client, task, config):
        return _park(_PARK_REASON_FIX_LOOP_ENABLED)
    probe = None if probe_source is None else probe_source(worktree, task, clients)
    git_reason = _git_park_reason(worktree, task, clients, probe)
    if git_reason is not None:
        return _park(git_reason)
    return _OrphanDisposition(
        should_requeue=True, reason=CODEX_ORPHAN_CLEAN_REQUEUE_REASON
    )


def _propose_reap(state: CwState, session: Session, ticket_id: str, lane: str) -> None:
    """Propose the reap through reconcile's shared ``_emit_reap_proposed``.

    ``park_blocked_on_user`` because that is what this pass did: ``cw
    orchestrate run`` authorizes a reap only for ``revert_task`` and
    ``crash_complete``, and must not crash-complete a session whose writer is
    still alive, so it leaves this one for the operator. The shared emitter
    owns the ``reap_proposed_at`` dedup and records the event before it
    persists that stamp, so a failed emit leaves the next boot free to retry.
    """
    candidate = ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
        ticket_id=ticket_id,
        reap_reason=ReapReason.CODEX_ORPHAN_LIVE_WRITER,
        lane=lane,
        client=session.client,
        worktree_path=session.worktree_path,
    )
    _emit_reap_proposed(
        state,
        [candidate],
        native_live=_deps.get_native_daemon_client().list_live_session_short_ids(),
    )


def _close_audit_payload(
    session: Session,
    ticket_id: str,
    disposition: _OrphanDisposition,
    *,
    close_reason: str = CODEX_ORPHAN_CLOSE_REASON,
) -> dict[str, object]:
    """SESSION_COMPLETED payload for a session this pass closes.

    The crashed, nothing-salvaged shape reconcile's phantom sweep emits, plus
    why and how it was closed. ``disposition`` is the task transition decided
    on; the identity-checked transition itself confirms with TICKET_REQUEUED or
    SESSION_NEEDS_ATTENTION when it lands. ``crashed: True`` also keeps the
    dispatch consumer from completing the task off this event. *close_reason*
    names the pass that closed it: the boot pass's default, or
    ``cw.reconcile.codex_reparks``'s reconcile-tick reason (#2307).
    """
    return {
        "session_id": session.id,
        "session_name": session.name,
        "client": session.client,
        "ticket_id": ticket_id,
        "crashed": True,
        "salvaged": False,
        "reason": close_reason,
        "disposition": (
            _CLOSE_DISPOSITION_REQUEUED
            if disposition.should_requeue
            else _CLOSE_DISPOSITION_PARKED
        ),
        "detail": disposition.reason,
    }


def _close_session_audited(
    state: CwState,
    session: Session,
    ticket_id: str,
    disposition: _OrphanDisposition,
    *,
    close_reason: str = CODEX_ORPHAN_CLOSE_REASON,
) -> None:
    """Record the closure's audit event, then close and persist the session.

    Audit before effect, the settle ledger's ordering (#2232): a failed event
    write raises before anything is mutated, so a session is never closed
    without its audit trail and the next boot retries the whole disposition.
    Lock-agnostic: the caller holds ``sessions_lock`` (see *close_reason* on
    :func:`_close_audit_payload`).
    """
    record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        _close_audit_payload(
            session, ticket_id, disposition, close_reason=close_reason
        ),
        correlation_id=ticket_id,
    )
    session.status = SessionStatus.COMPLETED
    session.completed_at = datetime.now(UTC)
    session.completed_reason = CompletionReason.CRASHED
    save_state(state)


def _row_still_bound(ticket_id: str, client_name: str, session_id: str) -> bool:
    """Whether the task row is still RUNNING under *session_id*.

    The same predicate the identity-checked row transitions re-verify
    (``expected_session_id`` in ``cw.queue_rows``). Caller holds
    ``dev_queue_lock``.
    """
    return any(
        task.ticket_id == ticket_id
        and task.client == client_name
        and task.status is QueueItemStatus.RUNNING
        and task.session_id == session_id
        for task in load_dev_queue().tasks
    )


def stale_snapshot_reason(
    session: Session, snapshot: Session, ticket_id: str
) -> str | None:
    """Why the locked record is no longer the orphan *snapshot* saw, or None.

    Still the same session (found by id, and the same incarnation: a ``cw
    resume`` keeps the id but moves ``surface_ref`` and stamps
    ``resumed_at``), still live, and still bound to the task row.
    """
    if (session.surface_ref, session.resumed_at) != (
        snapshot.surface_ref,
        snapshot.resumed_at,
    ):
        return "it was resumed since the snapshot"
    if session.status not in _LIVE_STATUSES:
        return f"it is now {session.status}"
    if not _row_still_bound(ticket_id, snapshot.client, snapshot.id):
        return "its task row no longer belongs to it"
    return None


def _leave_untouched(snapshot: Session, ticket_id: str, why: str) -> bool:
    _log.warning(
        "codex_boot: leaving session %s (%s/%s) untouched: %s",
        snapshot.id,
        snapshot.client,
        ticket_id,
        why,
    )
    return False


def _close_or_propose_reap(
    snapshot: Session, ticket_id: str, lane: str, disposition: _OrphanDisposition
) -> bool:
    """Close the session, or propose its reap; False if the snapshot went stale.

    Caller holds ``sessions_lock``. The disposition was decided from an
    unlocked snapshot, so the record is re-checked here, with the dev-queue
    lock held too, before anything is written: a newer session that replaced
    the orphan in the meantime is left untouched rather than overwritten as
    CRASHED.
    """
    with dev_queue_lock():
        state = load_state()
        session = next((s for s in state.sessions if s.id == snapshot.id), None)
        if session is None:
            return _leave_untouched(snapshot, ticket_id, _STALE_SESSION_GONE)
        stale = stale_snapshot_reason(session, snapshot, ticket_id)
        if stale is not None:
            return _leave_untouched(snapshot, ticket_id, stale)
        if disposition.close_session:
            _close_session_audited(state, session, ticket_id, disposition)
        else:
            _propose_reap(state, session, ticket_id, lane)
    return True


def _requeue_clean_orphan(
    *, session_id: str, ticket_id: str, client_name: str, stage: Stage
) -> None:
    """Revert the task to PENDING; report it only if the revert happened."""
    if not _revert_claimed_task_to_pending(
        client_name, ticket_id, expected_session_id=session_id
    ):
        _log.warning(
            "codex_boot: %s/%s no longer belongs to session %s; requeue skipped",
            client_name,
            ticket_id,
            session_id,
        )
        return
    # Same-stage PENDING revert, so the payload mirrors dispatch/routing's
    # provider_overload_retry shape (from_stage == to_stage, no ``regressed``
    # key) rather than crud.py's always-regressed one.
    record_event(
        OrchestratorEventType.TICKET_REQUEUED,
        {
            "ticket_id": ticket_id,
            "client": client_name,
            "from_stage": stage,
            "to_stage": stage,
            "reason": CODEX_ORPHAN_CLEAN_REQUEUE_REASON,
            "session_id": session_id,
        },
    )


def _close_orphaned_session_and_dispose(
    *,
    snapshot: Session,
    ticket_id: str,
    stage: Stage,
    lane: str,
    disposition: _OrphanDisposition,
) -> bool:
    """Close the orphaned Session record if allowed, then requeue or park its task.

    A record left ACTIVE with no writer behind it holds a ceiling slot and
    trips the next spawn's hook-context conflict guard, so it closes unless
    ``disposition`` says a writer may still be alive. Returns False, having
    touched nothing, when the record no longer matches *snapshot* (see
    ``_close_or_propose_reap``). The task transition also re-verifies
    ``expected_session_id`` under the dev-queue lock, so a row re-claimed
    since the caller's snapshot is left alone.

    Two files, two writes, session first, on purpose: there is no
    cross-file transaction, and this is the order a crash between them can
    recover from. A closed session whose task is still RUNNING is picked up
    by reconcile's ``revert_completed_silent_tasks`` backstop; a task already
    parked or requeued (its ``session_id`` cleared) behind a still-ACTIVE
    session would be skipped by every later boot's identity check, which is
    the stranded-session bug #2285 closes.
    """
    # Why not mutate_state: dev_queue_lock is nested inside this sessions_lock
    # window (mirrors cli/spawn.py:_spawn_complete_impl's identical nesting) so
    # the session close and the task transition land under one lock scope.
    with sessions_lock():
        # Proposal before the park it proposes (ADR-0006 invariant 3). A raise
        # here leaves the task claimed, so the next boot re-finds the orphan.
        if not _close_or_propose_reap(snapshot, ticket_id, lane, disposition):
            return False
        if disposition.should_requeue:
            _requeue_clean_orphan(
                session_id=snapshot.id,
                ticket_id=ticket_id,
                client_name=snapshot.client,
                stage=stage,
            )
        else:
            # A park that leaves the session ACTIVE links the row back to it
            # (#2307), since the park clears the row's own session_id:
            # cw.reconcile.codex_reparks follows that link on reconcile ticks
            # and closes the session once the writer is gone. Every other
            # park closed the session above, so there is nothing to link.
            _park_running_task_blocked_on_user(
                ticket_id=ticket_id,
                client_name=snapshot.client,
                expected_session_id=snapshot.id,
                disposition=CODEX_ORPHANED_AT_BOOT_DISPOSITION,
                breadcrumbs=f"{_ORPHAN_BREADCRUMBS} ({disposition.reason}).",
                codex_orphan_session_id=(
                    None if disposition.close_session else snapshot.id
                ),
            )
    return True


def reap_orphaned_codex_sessions_at_boot() -> int:
    """Requeue or park every live codex-origin session found at process start.

    Returns the number of orphans acted on (requeued or parked). Never raises
    on an unresolvable session (unknown client, no matching dev-queue row,
    unparseable name) — this runs on the boot path, where refusing to start is
    strictly worse than skipping one ambiguous session.
    """
    # Deferred for import-cycle reasons: cw.executor imports cw.reconcile at
    # module level (executor/core.py), and cw.reconcile imports this module, so
    # a module-level import here would hit a partially initialized
    # cw.executor. This module's only remaining function-level import; its
    # former cw.dispatch.claim deferrals moved to cw.queue_rows (#2613).
    from cw.executor import resolve_executor_config

    state = load_state()
    # Keyed by (ticket_id, client), not ticket_id alone: ticket numbering is
    # per-client, so a claude-workspace ticket 21 and another client's ticket 21
    # are different tasks. Keying on ticket_id alone would let one client's row
    # shadow the other's and park the wrong client's live session. Matches
    # _park_running_task_blocked_on_user's own (ticket_id, client) key exactly.
    task_by_ticket = {
        (task.ticket_id, task.client): task for task in load_dev_queue().tasks
    }
    clients = load_clients()
    config = load_effective_config()

    # Counts every orphan acted on -- requeued or parked.
    parked = 0
    for session in state.sessions:
        if (
            session.status not in _LIVE_STATUSES
            or session.origin is not SessionOrigin.DAEMON
            or not _is_headless(session)
            # RFC 0014 A2 (#2388): a session carrying a liveness handle is a
            # detached ``cw codex run`` job that outlives serve. It is not an
            # orphan — reconcile/local's codex harvest branch (A1) owns it,
            # and only once its PID is dead. This sweep is left with the
            # pre-migration, handle-less sessions until B2 retires it.
            or session.local_liveness is not None
        ):
            continue
        ticket_id = ticket_id_for_session(session.name)
        if ticket_id is None:
            continue
        task = task_by_ticket.get((ticket_id, session.client))
        client = clients.get(session.client)
        if task is None or client is None:
            continue
        # Identity, not coincidence of (ticket_id, client): an earlier boot's
        # orphan can linger in state as an ACTIVE record long after its task was
        # parked, recovered, and re-dispatched onto a fresh session. Without this
        # check that zombie re-matches on every later boot and disposes of
        # whatever healthy review now owns the row. This is a cheap early-exit
        # against the snapshot read above, not the safety guarantee itself — the
        # row could still be re-claimed between here and the transition below,
        # so the same identity is re-verified atomically under the lock via
        # expected_session_id (#1727 round 5, #2285).
        if task.session_id != session.id:
            continue
        if resolve_executor_config(task.stage, task, client).backend != CODEX_BACKEND:
            continue
        if _dispose_orphan(session, ticket_id, task, client, clients, config):
            parked += 1
    return parked


def _dispose_orphan(
    session: Session,
    ticket_id: str,
    task: TicketTask,
    client: ClientConfig,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> bool:
    """Decide and apply one orphan's disposition; False if it could not land.

    An I/O failure recording an audit event or persisting state is logged
    and swallowed here, per orphan: this runs on the boot path, and one
    orphan's failed write must neither stop ``serve`` from starting nor the
    remaining orphans from being dispositioned. The session's own writes
    precede the task transition, so a failure there leaves the task claimed
    and the next boot retries.
    """
    disposition = _resolve_orphan_action(
        session.worktree_path, task, client, clients, config
    )
    _log_disposition(session, ticket_id, disposition)
    try:
        return _close_orphaned_session_and_dispose(
            snapshot=session,
            ticket_id=ticket_id,
            stage=task.stage,
            lane=task.lane,
            disposition=disposition,
        )
    except OSError:
        _log.exception(
            "codex_boot: could not dispose of orphaned session %s (%s/%s);"
            " leaving it for the next boot",
            session.id,
            session.client,
            ticket_id,
        )
        return False


def _log_disposition(
    session: Session, ticket_id: str, disposition: _OrphanDisposition
) -> None:
    if disposition.should_requeue:
        _log.warning(
            "codex_boot: session %s (%s/%s) was still ACTIVE at process start;"
            " worktree and HEAD are unchanged since the review began --"
            " requeuing the task for a fresh attempt",
            session.id,
            session.client,
            ticket_id,
        )
        return
    _log.warning(
        "codex_boot: session %s (%s/%s) was still ACTIVE at process start;"
        " parking the task for operator inspection (%s)%s",
        session.id,
        session.client,
        ticket_id,
        disposition.reason,
        "" if disposition.close_session else "; leaving the session ACTIVE",
    )
