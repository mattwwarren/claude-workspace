"""StageExecutor seam, ClaudeNativeExecutor, codex capability probe, and the
shared executor-direct completion door (RFC 0005 A2, RFC 0012 A2)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from cw.auto_dev_result import AutoDevResult
from cw.codex_review._const import _CODEX_VERSION_RE
from cw.config import load_state, save_state
from cw.exceptions import EmitSessionNotFoundError
from cw.models import (
    ClientConfig,
    CompletionReason,
    LastResultSource,
    SessionPurpose,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.reconcile import AUTO_DEV_LABEL_PREFIX
from cw.result import emit_result_locked
from cw.spawn import spawn_create_impl

if TYPE_CHECKING:
    from pathlib import Path

    from cw.native_daemon import NativeDaemonClient

_log = logging.getLogger(__name__)

# Session-level CodexExecutor pre-flight blocker reason code (RFC 0005 F1).
# Per-role failure reason codes (CODEX_TIMEOUT/CODEX_ERROR/
# CODEX_REVIEW_UNPARSEABLE/CODEX_MUST_FIX_FINDINGS) live in cw.codex_review.
CODEX_NOT_FOUND = "codex_not_found"

# Capability-probe diagnosis (distinct from cw.codex_review's per-role review
# failure-reason vocabulary): the binary is present but `codex --version`
# could not be confirmed.
CODEX_VERSION_UNKNOWN = "codex_version_unknown"

# Default subprocess timeout for a one-shot, user-invoked probe (e.g. `cw
# doctor`). Callers on a hot path (dispatch's pre-spawn gate) should pass a
# smaller explicit timeout — see the ``timeout_seconds`` parameter below.
_DEFAULT_PROBE_TIMEOUT_SECONDS = 10


class CodexCapabilityDiagnosis(NamedTuple):
    """Result of the shared codex capability probe (#1238).

    ``diagnosis`` is ``None`` when codex is capable (binary present and
    ``codex --version`` parsed), ``CODEX_NOT_FOUND`` when the binary is absent,
    or ``CODEX_VERSION_UNKNOWN`` when the binary is present but ``--version``
    failed/timed out/exited non-zero/produced an unparseable string. ``detail``
    carries the parsed version line on success or a short human-readable failure
    detail on the two failure branches.
    """

    diagnosis: str | None
    detail: str


def codex_capability_diagnosis(
    *, timeout_seconds: int = _DEFAULT_PROBE_TIMEOUT_SECONDS
) -> CodexCapabilityDiagnosis:
    """Probe codex CLI presence and ``codex --version`` (no live review).

    Mirrors ``doctor._check_claude_version``'s subprocess/timeout/parse shape as
    a single reusable helper. No version floor is enforced: capability requires
    only binary presence plus a successfully-parsed ``--version`` string. This
    is the single home of the probe logic — ``doctor._check_codex_capability``
    and dispatch's pre-spawn capability gate are thin call sites over it, so the
    subprocess/parse logic isn't duplicated across two already-oversized modules.

    ``timeout_seconds`` defaults to a one-shot-invocation-appropriate 10s
    (matching ``_check_claude_version``); dispatch's hot-path caller passes a
    smaller value since this runs synchronously inside the dispatch tick loop.
    """
    if shutil.which("codex") is None:
        return CodexCapabilityDiagnosis(CODEX_NOT_FOUND, "codex binary not found")
    try:
        proc = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return CodexCapabilityDiagnosis(CODEX_VERSION_UNKNOWN, "codex binary not found")
    except subprocess.TimeoutExpired:
        return CodexCapabilityDiagnosis(
            CODEX_VERSION_UNKNOWN, f"codex --version timed out ({timeout_seconds}s)"
        )

    output = proc.stdout or proc.stderr or ""
    version_line = output.splitlines()[0] if output else ""
    if proc.returncode != 0:
        return CodexCapabilityDiagnosis(
            CODEX_VERSION_UNKNOWN,
            f"codex --version exited {proc.returncode}: {version_line}",
        )
    if _CODEX_VERSION_RE.search(version_line) is None:
        return CodexCapabilityDiagnosis(
            CODEX_VERSION_UNKNOWN, f"could not parse version: {version_line}"
        )
    return CodexCapabilityDiagnosis(None, version_line)


@runtime_checkable
class StageExecutor(Protocol):
    """Protocol for executing a single pipeline stage (RFC 0005 A2).

    # Invariant: a ``StageExecutor.spawn()`` that blocks the calling thread must
    # not be on the shared ``dispatch_tick`` path unless its session carries a
    # ``surface_ref``-equivalent liveness handle (bound to process start-time)
    # that phantom/harvest detection can use for crash recovery. LocalExecutor
    # satisfies this via ``Session.local_liveness`` (RFC 0005 F3, #888): it
    # launches aider fire-and-forget, records the handle, and returns without
    # blocking, so reconcile/local can recover the session if cw dies mid-run.
    #
    # CodexExecutor is an accepted, documented exception (#1727): it no longer
    # blocks the caller (it hands the review to a ``cw.codex_background`` daemon
    # thread and returns) but still carries no liveness handle, so its session
    # is not crash-recoverable via harvest. Unchanged from when the call was
    # synchronous — a thread dies with its process exactly as a blocking call
    # did — so this is not a regression; closing it is Option A/B territory (a
    # real subprocess surface), out of scope. The blast radius is bounded, not
    # closed, from two sides: ``run_dispatch_loop``'s shutdown path
    # bounded-joins outstanding codex threads before DISPATCH_LOOP_EXITED
    # (deploy/restart/``--once``), and ``cw.reconcile.codex_boot`` flags any
    # codex session still ACTIVE at the next boot (the crash/SIGKILL path a
    # join cannot reach).
    """

    def spawn(
        self,
        *,
        stage: Stage,
        task: TicketTask,
        worktree: Path,
        client: ClientConfig,
        wall_clock_budget_seconds: int | None = None,
        parent: str | None = None,
    ) -> str:
        """Spawn the stage session; return the cw session id."""
        ...

    def stage_sentinel_schema(self, stage: Stage) -> dict[str, Any]:
        """Return the expected JSON schema for this stage's result sentinel."""
        ...


class ClaudeNativeExecutor:
    """StageExecutor backed by claude --bg via spawn_create_impl (RFC 0005 A2).

    # Why: A2 seam — wraps spawn_create_impl without modifying it.
    # --model forwarding uses client.model_copy to avoid double-flag.
    # stage_sentinel_schema bridges to AutoDevResult until A3 per-stage schemas.
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        native_daemon: NativeDaemonClient | None = None,
    ) -> None:
        self._config = config
        self._native_daemon = native_daemon

    def spawn(
        self,
        *,
        stage: Stage,
        task: TicketTask,
        worktree: Path,
        client: ClientConfig,
        wall_clock_budget_seconds: int | None = None,
        parent: str | None = None,
    ) -> str:
        effective_model = self._config.model or client.worker_model
        effective_client = client.model_copy(update={"worker_model": effective_model})
        return spawn_create_impl(
            client=effective_client,
            worktree=worktree,
            prompt=f"/auto-dev-{stage.value} {task.ticket_id} --headless",
            label=f"{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}",
            ticket_id=task.ticket_id,
            lane=task.lane,
            headless=True,
            purpose=SessionPurpose.IMPL,
            permission_mode=None,
            parent=parent,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
            native_daemon=self._native_daemon,
            task=task,
        )

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        # Why: per-stage result models do not exist until A3 (#614).
        # Bridge via the current monolith sentinel until then.
        return AutoDevResult.model_json_schema()


def _complete_session_via_door(
    sid: str,
    payload: dict[str, Any],
    *,
    guard_already_completed: bool = False,
) -> None:
    """Route SID's terminal write through the door (RFC 0012 D-A1/D-S3,
    source=EXECUTOR_DIRECT), then transition status to COMPLETED.

    Caller MUST already hold sessions_lock(). Shared by all four
    LocalExecutor/CodexExecutor direct-write sites (#1458) so the
    door-call + not-found-catch + status-transition shape isn't
    duplicated four times.

    Session-not-found (EmitSessionNotFoundError) is logged at debug and
    swallowed -- preserves the pre-migration silent no-op when SID has no
    matching session (R4).

    guard_already_completed=True re-checks SID's status before the door
    call and skips entirely if already COMPLETED -- the exception-handler
    idempotency guard the two ``except Exception:`` sites carried before
    this migration (R1). The two main-path sites (each the session's
    first and only completion write) pass the default False.

    Door refusal (a terminal result already recorded by another writer)
    is a normal, non-raising return -- the status transition below still
    runs regardless (R5): refusal affects only the last_result write, not
    the executor's status/event bookkeeping.

    Sets the full terminal record -- ``status``, ``completed_at``, and
    ``completed_reason`` -- matching the crash-completion precedent at
    ``reconcile/concierge.py``'s ``_LIVE_STATUSES`` handling (#2280 round 2).
    Every ``guard_already_completed=True`` call site is an ``except
    Exception:`` branch, so that flag doubles as the crashed/normal signal:
    True -> CRASHED, False -> NORMAL.
    """
    if guard_already_completed:
        state = load_state()
        target = next((s for s in state.sessions if s.id == sid), None)
        if target is None or target.status == SessionStatus.COMPLETED:
            return
    try:
        emit_result_locked(payload, sid, source=LastResultSource.EXECUTOR_DIRECT)
    except EmitSessionNotFoundError:
        _log.debug("executor_direct emit skipped: session %s not found", sid)
        return
    state = load_state()
    target = next((s for s in state.sessions if s.id == sid), None)
    if target is not None:
        target.status = SessionStatus.COMPLETED
        target.completed_at = datetime.now(UTC)
        target.completed_reason = (
            CompletionReason.CRASHED
            if guard_already_completed
            else CompletionReason.NORMAL
        )
        save_state(state)
