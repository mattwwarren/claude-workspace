"""Status/stage/scope vocabulary for the ``<<<AUTO_DEV_RESULT`` sentinel.

The closed-enum ``Literal`` aliases (:data:`SchemaVersion`, :data:`Status`,
:data:`StageReached`, :data:`ScopeTier`, :data:`PlanSource`), the derived
frozensets that classify them, the blocker-reason registry, and the two
classifier functions that read it. Depends on nothing else in this package —
the root of the ``_vocab -> _validators -> _models -> _result`` layering.

Spec: ``docs/headless-contract.md`` (§4 enum, §8 versioning). Package split:
issue #2193.
"""

from __future__ import annotations

from typing import Literal

from cw.models import QueueItemStatus

# Pinned logger name for every record the schema package emits. Deliberately a
# literal, not ``__name__``: the pre-split ``schema.py`` logged under this fixed
# name, and anything filtering/configuring logging by exact logger name must keep
# seeing it after the package split (#2193).
_LOGGER_NAME = "cw.auto_dev_result"

# Accepted sentinel schema versions. Single source of truth: parse.py derives
# SUPPORTED_SCHEMA_VERSIONS (its pre-Pydantic gate) from this Literal via
# get_args, so a version bump edits exactly one place (#1535 drift class).
SchemaVersion = Literal[1, 2, 3, 4, 5, 6, 7, 8]

Status = Literal[
    "shipped",
    # RFC 0005 B2 intermediate stage-success status (#699). PR-less: IMPL
    # pushes a branch but does not create a PR (FINALIZE does). Accepted under
    # all supported schema versions (same rollout exception as _V4_STATUSES)
    # until the auto-dev-impl producer skill bumps its emitted schema_version.
    "stage_complete",
    "plan_pending_approval",
    "review_pending_approval",
    "merge_gate_blocked",
    # PR created but awaiting CI / merge gate (#899). Non-null pr required;
    # parse boundary coerces status=blocked+non-null pr to this status.
    "merge_pending",
    "scope_exceeded",
    "forbidden_area",
    "blocked",
    "no_op",
    "ambiguities_pending_resolution",
    "premises_pending_verification",
    # #1870: the branch has zero commits ahead of origin/<default_branch> at
    # IMPL/REVIEW exit, or as measured by the dispatch-level empty-diff gate --
    # never a clean pass. Post-branch (unlike scope_exceeded/forbidden_area), so
    # it carries a non-null branch and may carry a blocker. Accepted under all
    # supported schema versions (same rollout exception as _V4_STATUSES) until
    # the producer skills bump their emitted schema_version to 6.
    "empty_diff_blocked",
    # #1862: this ticket already has an open, unmerged PR from an earlier
    # dispatch, so the run refuses rather than re-implementing on top of work
    # already in review. Distinct from no_op (nothing is complete -- the PR is
    # unmerged) and from blocked (nothing is broken -- the PR is healthy, just
    # not this session's to duplicate). May be reported pre-branch (the Stage 0
    # intake self-check) or post-branch (discovered mid-IMPL on a resume), so
    # it is NOT a _PRE_BRANCH_STATUSES member and may carry a blocker naming
    # the discovered PR. Accepted under all supported schema versions (same
    # rollout exception as _V4_STATUSES) until the producer skills bump their
    # emitted schema_version to 7.
    "stale_dispatch",
]
# Statuses introduced after v1. Emitting one under schema_version=1 is a
# producer bug — it would silently degrade for downstream tools that key off
# the version field.
_V2_STATUSES: frozenset[str] = frozenset({"no_op"})
# Lowest schema_version that may legally carry a v2-introduced status.
_MIN_V2_SCHEMA_VERSION = 2
# Statuses introduced in v4 (issue #191). Per rollout exception (issue #316),
# accepted under all supported schema versions (v2, v3, v4) until the producer
# skill bumps its emitted schema_version to v4.
_V4_STATUSES: frozenset[str] = frozenset(
    {"ambiguities_pending_resolution", "premises_pending_verification"}
)
# Named status sets for the B2 stage-advance decision table (RFC 0005 B2).
# Placement: next to PAUSED_FOR_USER_INPUT_STATUSES per R9.
SCOPE_GATED_APPROVAL_STATUSES: frozenset[str] = frozenset(
    {"plan_pending_approval", "review_pending_approval"}
)
# Public alias for consumers that need to check whether a status indicates the
# session is paused waiting for human input (issue #129). Includes the v4
# ambiguity/premises statuses plus the approval-pending states (#633).
# DRY: SCOPE_GATED_APPROVAL_STATUSES is composed in here (RFC 0005 B2).
PAUSED_FOR_USER_INPUT_STATUSES: frozenset[str] = (
    _V4_STATUSES | SCOPE_GATED_APPROVAL_STATUSES
)
STAGE_SUCCESS_STATUSES: frozenset[str] = frozenset({"shipped", "stage_complete"})
STAGE_FAILURE_STATUSES: frozenset[str] = frozenset(
    {
        "blocked",
        "merge_gate_blocked",
        "scope_exceeded",
        "forbidden_area",
        "empty_diff_blocked",
        "stale_dispatch",
    }
)
# blocker.reason (open enum, §4.2) paired with the empty_diff_blocked status
# above. Named rather than inlined because two producers write it -- the
# codex-review synthesis path and the auto-dev-review producer skill -- and a
# typo in either would be invisible to the closed-enum status check. See #1870.
EMPTY_DIFF_BLOCKER_REASON: Literal["empty_diff_no_commits"] = "empty_diff_no_commits"
# blocker.reason (open enum, §4.2) paired with the stale_dispatch status above.
# Named for the same reason EMPTY_DIFF_BLOCKER_REASON is: two producers write
# it (the auto-dev-intake Stage 0 self-check and the auto-dev-plan Stage 1
# resume-path check), and a typo in either would be invisible to the
# closed-enum status check. Deliberately distinct from
# cw.dev_queue.lifecycle._PRE_DISPATCH_STALE_PR_REASON, which names the
# *code-side* gate's park -- that one is never emitted by an agent. See #1862.
STALE_DISPATCH_BLOCKER_REASON: Literal["pr_already_open"] = "pr_already_open"
# Blocker reasons at Stage.FINALIZE eligible for automatic regress to IMPL.
# "agent_block" covers prep-pr gate failures (diff-cover, etc.) that a fresh
# impl session can fix by adding missing tests. Reasons absent here (e.g.
# "no_result_emitted") stay BLOCKED_ON_USER. Open enum per §4.2 — add reasons
# as the producer skill evolves. See GitHub #770.
FINALIZE_REGRESS_BLOCKER_REASONS: frozenset[str] = frozenset({"agent_block"})
# Blocker reasons that mean "we can't reach the operator/a dependency right
# now", not "this leg is broken" (RFC 0011 A1). Distinct axis from
# FINALIZE_REGRESS_BLOCKER_REASONS above -- self-heals nothing, just tags the
# park so the attention layer and (later, A4) auto-resume can tell it apart
# from a genuine `blocked`. push_auth_failed (#1049) is retro-classified as
# the first instance. dependency_unmerged (#2260) is the second: a ticket
# split into a dependency chain (the #2233/#2213 precedent) whose downstream
# leg can't proceed until the upstream PR merges -- an unreachable dependency,
# not a broken leg.
OPERATOR_UNAVAILABLE_BLOCKER_REASONS: frozenset[str] = frozenset(
    {"push_auth_failed", "operator_unavailable", "dependency_unmerged"}
)
# blocker.reason emitted when a stage finds a destructive directive (delete a
# remote branch, force-push/rewrite shared history, discard work, close or
# reopen a ticket) sourced from a tracker comment rather than from the
# operator directly. Named here because both the producer skills and this
# registry reference it; see .claude/commands/auto-dev.md's "Comment
# provenance rule" for the gate itself (#2097).
DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON: Literal[
    "destructive_directive_requires_operator"
] = "destructive_directive_requires_operator"
# blocker.reason emitted at FINALIZE when the diagnosed root cause of a gate
# failure is state on origin/main (or another external dependency) that
# predates this branch's own changes -- a corrupted released CHANGELOG
# section, a required external PR, an infrastructure gate -- so a fresh
# IMPL session has nothing on the branch to fix. Distinct from
# OPERATOR_UNAVAILABLE_BLOCKER_REASONS (RFC 0011 A1): that axis means "we
# can't reach the operator/a dependency right now", self-healing nothing but
# the reachability; this means the block is real and needs an operator to
# actually act (fix main, land the dependency), not just become reachable.
# Deliberately absent from FINALIZE_REGRESS_BLOCKER_REASONS -- see that
# constant's docstring. See GitHub #2320.
EXTERNAL_STATE_BLOCKER_REASON: Literal["external_state_block"] = "external_state_block"
# Prefix reserving the explicit freeform half of the open enum (#2097). A
# reason starting with it is *declared* experimental/producer-local, so it is
# never warned about even though it is absent from KNOWN_BLOCKER_REASONS.
FREEFORM_BLOCKER_REASON_PREFIX: Literal["x_"] = "x_"
# Every blocker.reason cw or a producer skill is known to emit today (#2097).
# ADVISORY, NOT A GATE: blocker.reason stays an open enum per
# docs/headless-contract.md §4.2 -- an unrecognized reason parses and is
# surfaced verbatim, it just logs a warning and is flagged in the operator's
# view, so an *invented* reason that looks like a documented routing code
# (the #2097 incident) is visible instead of silently authoritative.
# Grouped by source; each group's owning module/doc is the place to add to.
KNOWN_BLOCKER_REASONS: frozenset[str] = (
    # .claude/commands/auto-dev.md's `blocker.reason` Values table. The
    # doc-conformance test in tests/test_agent_comment_provenance.py parses
    # that table and asserts every row appears here, so the two cannot drift.
    frozenset(
        {
            "impl_not_pushed",
            "impl_failed",
            "review_blocked",
            "plan_deviation",
            "review_operator_actionable",
            "plan_scope_drift",
            "plan_unreviewable",
            "plan_unsound",
            "ambiguity_scan_unconverged",
            "deferred_stub_unresolved",
            "scope_tier_stale",
            "fix_loop_pending_dispatch",
            "agent_block",
            "tool_denied",
            DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON,
            EXTERNAL_STATE_BLOCKER_REASON,
        }
    )
    # Documented outside that table: docs/headless-contract.md §4.2, the
    # stage skills' own exit clauses, and the local/opencode runner prompts.
    | frozenset(
        {
            "automerge_not_armed",
            "local_main_diverged_from_origin",
            "plan_ambiguous",
            "plan_missing",
            "plan_missing_context",
            "prior_pipeline_pr_open",
        }
    )
    # cw-side synthetic reasons. The literals are owned by
    # cw.auto_dev_result.parse (BLOCKER_REASON_*), cw.codex_review._const, and
    # cw.dev_queue.lifecycle -- restated rather than imported because parse and
    # lifecycle both import THIS module, so importing back would cycle.
    # tests/test_auto_dev_result.py pins the parse-side copies in lockstep.
    | frozenset(
        {
            "codex_must_fix_findings",
            "codex_must_fix_mechanically_rejected",
            "codex_review_partial",
            "codex_review_unparseable",
            "multiple_result_blocks",
            "no_result_emitted",
            "pr_already_open_pre_dispatch",
            "schema_version_unsupported",
            "status_unknown",
            "validation_failed",
        }
    )
    # Constants defined above in this module.
    | frozenset({EMPTY_DIFF_BLOCKER_REASON, STALE_DISPATCH_BLOCKER_REASON})
    | FINALIZE_REGRESS_BLOCKER_REASONS
    | OPERATOR_UNAVAILABLE_BLOCKER_REASONS
)
# Max automatic FINALIZE→IMPL regressions per ticket; prevents ping-pong.
FINALIZE_REGRESS_CAP: int = 2
SCOPE_TIER_SMALL: Literal["small"] = "small"
SCOPE_TIER_LARGE: Literal["large"] = "large"
# Unresolved-provenance sentinel for PlanSource, e.g. when no stage has yet
# classified how a ticket's plan originated.
PLAN_SOURCE_NONE: Literal["none"] = "none"

# AutoDevResult statuses that represent terminal outcomes the dev-queue should
# never auto-retry. A phantom or stalled session that emitted one of these
# before crashing must be salvaged (dispositioned by the sentinel) rather than
# mislabeled crashed/timed-out and re-dispatched.
#
# This is the single source of truth; both reconcile.py and cli.py import it
# so the two cannot drift apart. See GitHub issues #372 and #431.
SALVAGE_TERMINAL_STATUSES: frozenset[str] = (
    frozenset(
        {
            "shipped",
            "no_op",
            "plan_pending_approval",
            "review_pending_approval",
            "merge_gate_blocked",
            # PR created, awaiting CI/merge — do not re-dispatch (#899).
            "merge_pending",
            "scope_exceeded",
            "forbidden_area",
            # #1870. Explicit member: this set is hand-maintained and does NOT
            # derive from STAGE_FAILURE_STATUSES, so a crashed worker whose last
            # sentinel reported an empty diff would otherwise be mislabeled
            # crashed and re-dispatched onto the same empty branch.
            "empty_diff_blocked",
            # #1862. Explicit member for the same reason: a crashed worker
            # whose last sentinel reported an already-open PR would otherwise
            # be re-dispatched onto the exact ticket the sentinel just refused.
            "stale_dispatch",
        }
    )
    | PAUSED_FOR_USER_INPUT_STATUSES
)

# Stage-advance success statuses that are NOT terminal salvage targets — i.e.
# they must advance the pipeline to the next stage rather than be dispositioned
# as a terminal outcome. Today this is exactly {stage_complete}: the PR-less
# intermediate stage-success status (#699). "shipped" is excluded because it is
# in SALVAGE_TERMINAL_STATUSES (terminal-salvage already handles it). Used by the
# reconcile phantom path to route an exited worker's emitted advance sentinel
# through apply_staged_decision instead of reverting it as a crash (#716).
INTERMEDIATE_ADVANCE_STATUSES: frozenset[str] = (
    STAGE_SUCCESS_STATUSES - SALVAGE_TERMINAL_STATUSES
)

# Salvage-routing hold set (#1566): every status whose live dispatch
# Rule 1/2/5/3b routes to BLOCKED_ON_USER rather than completing the task.
# SCOPE_GATED_APPROVAL_STATUSES is not its own term here -- it is already a
# subset of PAUSED_FOR_USER_INPUT_STATUSES (see test_paused_is_superset_of_
# scope_gated). Composed from the SAME frozensets dispatch/routing.py's Rule
# 1/2/5 membership tests read, and the "merge_pending" literal Rule 3b
# matches, so a status added to one side cannot silently drift the other.
SALVAGE_HOLD_STATUSES: frozenset[str] = (
    STAGE_FAILURE_STATUSES
    | PAUSED_FOR_USER_INPUT_STATUSES
    | frozenset({"merge_pending"})
)


def is_known_blocker_reason(reason: str) -> bool:
    """Return True if *reason* is registered or declared freeform (#2097).

    Single source of truth for the "should this reason be flagged to the
    operator" question: the :class:`Blocker` warn-only validator, dispatch
    routing's ``SESSION_NEEDS_ATTENTION`` breadcrumb, and
    ``cw dev-queue tasks``' REASON column all read it, so the ``x_`` freeform
    carve-out cannot be honored at one site and missed at another.

    Never a gate — ``blocker.reason`` remains an open enum (§4.2). A False
    answer means "surface it as unrecognized," never "reject it."
    """
    return reason in KNOWN_BLOCKER_REASONS or reason.startswith(
        FREEFORM_BLOCKER_REASON_PREFIX
    )


def queue_status_for_terminal_sentinel(status: Status) -> QueueItemStatus:
    """Classify a terminal sentinel status as a hold or a completion.

    Single source of truth for "does this status need a human before the
    ticket can move again," consumed by the reconcile salvage path
    (``cw.reconcile._shared._queue_status_for_salvaged``) so a worker that
    dies mid-sentinel is dispositioned the same way a live observer would
    have routed it (#1566). Live dispatch's Rule 1/2/5/3b
    (``cw.dispatch.routing._route_staged_decision``) does not call this
    function -- it has its own independent branch that reads the same
    underlying frozensets. The two are kept in sync by
    ``test_salvage_dispatch_hold_membership_is_single_source_of_truth``, not
    by a shared call site.

    Deliberately narrower than dispatch's routing table -- it answers only
    "is this a hold," not how to get there. It does NOT reproduce Rule 3 /
    ``_route_stage_success``'s stage-advance semantics or its
    ``_park_finalize_hold`` / ``_park_signoff_gate`` branches (salvage has no
    live task to advance -- the worker is dead), nor Rule 5a's FINALIZE-regress
    branch (salvage never regresses). Disposition computation
    (``_hold_aware_disposition``) and event emission stay dispatch/salvage-
    caller concerns.
    """
    if status in SALVAGE_HOLD_STATUSES:
        return QueueItemStatus.BLOCKED_ON_USER
    return QueueItemStatus.COMPLETED


# NOTE: stage1_pre_flight (StageReached) and "none" (PlanSource) are NOT
# gated by schema_version. Spec §8 says enum additions require a version
# bump (v3), and v3 IS the official home for these values, BUT the producer
# skill emits them under v2 today (see #103). One-time rollout exception:
# accept under v2 AND v3 until the skill bumps. When skill emits v3, this
# exception can be removed and a `_V3_STAGES`/`_V3_PLAN_SOURCES` gate added.
#
# Also accepted ungated: "github_issue_existing" (PlanSource). The producer
# emits this for GitHub-sourced runs (the post-Linear analog of
# "linear_existing"); the parser previously rejected every such run as
# validation_failed (see #190). Treated identically to "linear_existing" —
# pure producer-side relabeling, no consumer behavior change. Accepted under
# v2 and v3 (the producer emits at v2 today per captured payloads).
StageReached = Literal[
    "stage1_pre_flight",
    "stage1_plan",
    "stage2_impl",
    "stage3_review",
    "stage4a_merge_gate",
    "stage4b_pr_create",
    "stage5_post_create",
]
# Short-form stage codes emitted by the auto-dev producer's resume-detection
# substates (e.g. ``s5_ci_pending`` instead of ``stage5_post_create``).
# ``AutoDevResult._normalize_stage_reached`` maps these to their nearest
# full-form canonical equivalent before Pydantic validates the Literal.
# Unknown values pass through unchanged and fail the Literal check loudly.
# See issue #292 for the root-cause analysis.
_STAGE_REACHED_ALIASES: dict[str, str] = {
    "pre_flight": "stage1_pre_flight",
    "s1_drafting": "stage1_plan",
    "s1_pending_ambiguity_resolution": "stage1_plan",
    "s1_pending_human_approval": "stage1_plan",
    "s1_plan_approved": "stage1_plan",
    "s2_implementing": "stage2_impl",
    "s3_review_pending": "stage3_review",
    "s3_fix_loop": "stage3_review",
    "s4_pr_open": "stage5_post_create",
    "s5_ci_pending": "stage5_post_create",
    "s5_ci_passed": "stage5_post_create",
    "s5_ci_failed": "stage5_post_create",
    "merged": "stage5_post_create",
}
# Canonical StageReached values (mirrors the Literal above) — used to short-
# circuit normalization so a valid value is never re-coerced.
_STAGE_REACHED_CANONICAL: frozenset[str] = frozenset(
    {
        "stage1_pre_flight",
        "stage1_plan",
        "stage2_impl",
        "stage3_review",
        "stage4a_merge_gate",
        "stage4b_pr_create",
        "stage5_post_create",
    }
)
# Tolerant fallback for a near-miss the producer emits WITHIN a known stage
# number (e.g. ``stage4_pr_creation`` instead of ``stage4b_pr_create``).
# stage_reached is informational (routing keys on ``status``), so a stray label
# must not fail the whole sentinel and discard completed work (#748). A value
# with a ``stage<1-5>`` prefix that is neither canonical nor a known alias is
# coerced to that stage's canonical value with a WARNING. Values with no
# ``stage<1-5>`` prefix (genuine garbage) still fall through and reject, to keep
# catching malformed payloads. The stage4 prefix maps to ``stage4b_pr_create``
# (the PR-creation substage) since that is where the observed drift occurs.
_STAGE_NUMBER_FALLBACK: dict[str, str] = {
    "stage1": "stage1_plan",
    "stage2": "stage2_impl",
    "stage3": "stage3_review",
    "stage4": "stage4b_pr_create",
    "stage5": "stage5_post_create",
}
ScopeTier = Literal["small", "large"]
PlanSource = Literal[
    "linear_existing",
    "github_issue_existing",
    "generated",
    "free_text",
    "none",
]
