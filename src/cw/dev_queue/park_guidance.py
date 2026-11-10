"""Why a parked row sits where it does: the prose a refusal prepends (#2692).

Split from :mod:`cw.dev_queue.next_commands` (#2765), which owns the rule table
that selects commands. This module owns only the disposition -> guidance text
table and the ``resolve`` command the plan-stage entries name. Dispositions
without an entry fall through to the rule commands alone.
"""

from __future__ import annotations

from cw.dev_queue.dispositions import (
    AWAITING_OPERATOR_DISPOSITION,
    BRANCH_STALENESS_GATE_DISPOSITION,
    GATE_TIMEOUT_DISPOSITION,
    REVIEW_ENGINE_MODIFIED_DISPOSITION,
    REVIEW_ENGINE_UNSUPPORTED_DISPOSITION,
    REVIEW_HEALTH_GATE_DISPOSITION,
    REVIEW_STALENESS_GATE_DISPOSITION,
    STALE_DISPATCH_DISPOSITION,
)
from cw.dev_queue.gates import (
    AMBIGUITIES_PENDING_RESOLUTION_STATUS,
    MERGE_GATE_BLOCKED_STATUS,
    PREMISES_PENDING_VERIFICATION_STATUS,
)

RESOLVE_COMMAND = "cw dev-queue resolve --file <decisions.md>"

# Why a parked row with this disposition sits where it does, prepended to the
# refusal's commands. Dispositions without an entry fall through to the rule
# commands alone.
PARK_GUIDANCE: dict[str, str] = {
    AMBIGUITIES_PENDING_RESOLUTION_STATUS: (
        "The worker parked on open ambiguities. Answer them on the ticket, then"
        f" re-run the stage. At the plan stage, `{RESOLVE_COMMAND}` records"
        " the answers on the row and requeues it, with"
        " no tracker comment needed."
    ),
    PREMISES_PENDING_VERIFICATION_STATUS: (
        "The worker parked on unverified premises. Verify them and record the"
        " results on the ticket, then re-run the stage. At the plan stage,"
        f" `{RESOLVE_COMMAND}` records the verification"
        " results on the row and requeues it."
    ),
    REVIEW_ENGINE_MODIFIED_DISPOSITION: (
        "The branch changed the repo review engine's skill or a guarded path, so"
        " the engine round is parked. Inspect the touched files, then run"
        " `cw review engine allow` for this HEAD with a reason, and requeue the"
        " row with `cw dev-queue requeue`. A new commit needs a new allow."
    ),
    REVIEW_ENGINE_UNSUPPORTED_DISPOSITION: (
        "The client sets review.engine, but this REVIEW lane cannot run it: an"
        " opencode or local lane never can, and a codex lane needs a valid"
        " cw-engine.json beside SKILL.md on the default branch. Fix the lane"
        " config (claude-native or codex), or add or repair cw-engine.json"
        " (`cw review engine validate --kind manifest`), then"
        " `cw dev-queue requeue`."
    ),
    GATE_TIMEOUT_DISPOSITION: (
        "The reuse-path quality gate was cut off by the foreground timeout, not"
        " failed; the PR's auto-merge was left as found and nothing was regressed"
        " to impl. Confirm required CI is green at the PR head. In the row's"
        " worktree (the sentinel's `worktree_path`), at that head, run the"
        " timed-out gate by hand with no time limit. If it passes on a clean"
        " tree, run `prep_pr_state.py gate-pass record <gate>` there, then"
        " requeue the row with `cw dev-queue requeue`."
    ),
    REVIEW_HEALTH_GATE_DISPOSITION: (
        "Review health is degraded. This clears by re-running review, not by approving."
    ),
    REVIEW_STALENESS_GATE_DISPOSITION: (
        "The review artifacts predate the branch head. Re-running review clears"
        " it; approving does not."
    ),
    BRANCH_STALENESS_GATE_DISPOSITION: (
        "The branch conflicts with the default branch. Merge the default branch"
        " into it once and resolve the conflict (a merge, never a rebase or"
        " force-push), then re-run the stage; approving would ship a tree that"
        " cannot merge."
    ),
    MERGE_GATE_BLOCKED_STATUS: (
        "A prior pipeline PR for this ticket is still open. Merge or close it,"
        " then re-run the stage."
    ),
    STALE_DISPATCH_DISPOSITION: (
        "An earlier dispatch left an open, unmerged PR for this ticket. Land or"
        " close it, then re-run the stage."
    ),
    AWAITING_OPERATOR_DISPOSITION: (
        "The park was an operator-unavailability hold. Re-running the stage, or"
        " draining held tickets, releases it."
    ),
}
