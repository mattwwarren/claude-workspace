"""Codex harvest outcome, reasoning effort and sandbox-mode enums (#3739)."""

from __future__ import annotations

from enum import StrEnum


class CodexHarvestOutcome(StrEnum):
    """What one codex gate-audit-close attempt did (RFC 0014 A1/B1, #2389).

    Returned by ``cw.reconcile.local.act_on_codex_harvest_candidate`` and its
    sweep wrapper so a caller never re-derives the result from state.

    - ``REQUEUED``: the identity-checked revert happened and was reported.
    - ``PARKED``: a RUNNING row still bound to the session was parked.
    - ``AUDIT_FAILED``: the ``SESSION_COMPLETED`` audit write raised
      ``OSError``; nothing was changed.
    - ``NO_ROW``: the sweep wrapper found no matching row or client config;
      the session is untouched.
    - ``TRANSITION_LOST``: the session was closed, but its row no longer
      belonged to it, so there was no revert or park to report.
    - ``PROBE_UNAVAILABLE``: the lockless clean probe was missing or stale
      (#2563); the session is untouched and the next tick retries.
    """

    REQUEUED = "requeued"
    PARKED = "parked"
    AUDIT_FAILED = "audit_failed"
    NO_ROW = "no_row"
    TRANSITION_LOST = "transition_lost"
    PROBE_UNAVAILABLE = "probe_unavailable"


class ReasoningEffort(StrEnum):
    """Codex ``model_reasoning_effort`` levels a codex stage may pin.

    Emitted as ``-c model_reasoning_effort=<value>`` on every codex reviewer
    and fix invocation (see ``StageExecutorConfig.reasoning_effort``). A closed
    set so a typo fails at config load instead of surfacing mid-run as a
    degraded review role. Not every model supports every level; an
    unsupported one is a codex runtime error, not a config error.
    """

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class SandboxMode(StrEnum):
    """Codex ``--sandbox`` modes a codex reviewer or fix role may run under.

    The report/fix boundary: a reviewer reports what is wrong and a fix role
    repairs it, so the reviewer defaults to ``read-only`` and the fix loop to
    ``workspace-write`` (``StageExecutorConfig.reviewer_sandbox_mode`` /
    ``fix_sandbox_mode``, #1767). ``danger-full-access`` is deliberately not a
    member: no cw role needs writes outside the worktree, so a typo or an
    experiment cannot reach it through config.
    """

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


# The one place the per-role default sandbox modes are written (#1767).
DEFAULT_REVIEWER_SANDBOX_MODE = SandboxMode.READ_ONLY


DEFAULT_FIX_SANDBOX_MODE = SandboxMode.WORKSPACE_WRITE
