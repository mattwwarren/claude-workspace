"""Orchestrator development-queue commands (``dev-queue`` group).

This package was split out of a single ``dev_queue.py`` CLI module (#1319); the
public ``from cw.cli.dev_queue import X`` surface is preserved here via
re-exports. The ``dev_queue`` click group lives in ``_group`` and each command
submodule decorates it via ``@dev_queue.command(...)`` — importing the submodules
below is what registers those commands. Submodules:

- ``_group`` — the ``dev_queue`` group object plus the ``_WAIT_EXIT_*`` /
  status-rendering constants shared across submodules.
- ``_plan_marker`` — pure builders/matchers for the ``approve --post-marker``
  plan-approved audit comment (bare and draft-fingerprint-bound forms, #2194),
  used by ``approve``.
- ``_settle`` — the CLI-layer row-lag settle (#2692) approve and requeue run
  first; also the read-only live-session note for the requeue no-op.
- ``_tracker_post`` — the tracker-posting policy shared by ``resolve --post`` and
  ``approve --post-marker``: the GitHub-or-unknown tracker gate, the ``gh``
  working directory and the post-outcome echo (#2732).
- ``_requeue_echo`` — the requeue no-op echo, the inferred ``--regress``
  announcement and the ``TICKET_REQUEUED`` event (#2692), used by ``crud``.
- ``adopted_prs`` — the read-only ``adopted-prs`` command: ``<number> <branch>``
  lines for live adopted rows, backing finalize Step 4a (#2949).
- ``approve`` — the ``approve`` command (plan/review/operator-signoff gate
  clearing, ``--post-marker``, ``--scope-drift``), split out of ``crud``
  (#2337).
- ``crud`` — queue mutation commands (add, move, requeue, unblock, remove,
  cancel, clear, prune).
- ``fix_handoff`` — ``record-fix-handoff``, run by the review stage's fix loop
  to record the fix-agent handoff on the row from ``cw-context.json`` (#2651).
- ``plan_decisions`` — ``record-plan-decisions``, run by the plan stage to
  record the approved plan's decided sections on the row (#2984).
- ``recover`` — the ``recover-red-pr`` command: disarm a shipped red PR and
  regress its COMPLETED row to impl with head-pinned CI-failure feedback (#2945).
- ``resolve`` — record the operator's answers to a plan park on the row, then
  requeue it through ``crud``'s ``requeue`` (#2702).
- ``status`` — aggregate status table + lane breakdown rendering.
- ``run`` — dispatch-driving commands (run, serve, plan).
- ``wait`` — the sentinel-aware ``dev-queue wait`` loop and its emit helpers.
- ``_wait_state`` — clock-free per-ticket ``wait`` state (hand-off and probe
  throttle bookkeeping) plus the ``WAITING`` progress line and exit-5 stuck
  output (#2693), used by ``wait``.
- ``tasks`` — task inspection (tasks) + repo refresh (refresh-all) + attention
  helpers shared with ``status``.
- ``show`` — the one-ticket view (``show``) and the best-effort row echo the
  mutating verbs print after their confirmation (#2691).
"""

from __future__ import annotations

from cw.cli.dev_queue import (  # noqa: F401  (command registration side effects)
    adopted_prs,
    approve,
    crud,
    fix_handoff,
    plan_decisions,
    recover,
    resolve,
    run,
    show,
    status,
    tasks,
    wait,
)
from cw.cli.dev_queue._group import (
    _WAIT_EXIT_ATTENTION,
    _WAIT_EXIT_BLOCKED,
    _WAIT_EXIT_FAILED,
    _WAIT_EXIT_HUMAN_GATE,
    _WAIT_EXIT_STUCK,
    _WAIT_EXIT_TIMEOUT,
)

__all__ = [
    "_WAIT_EXIT_ATTENTION",
    "_WAIT_EXIT_BLOCKED",
    "_WAIT_EXIT_FAILED",
    "_WAIT_EXIT_HUMAN_GATE",
    "_WAIT_EXIT_STUCK",
    "_WAIT_EXIT_TIMEOUT",
]
