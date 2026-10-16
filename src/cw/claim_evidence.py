"""Claim-evidence classification for the unproductive-attempt ceiling (#1750).

The dispatch attempt ceiling exists to catch the #1653 pathology: a task that
crashloops — dead session after dead session, no sentinel, no progress — must
eventually stop being redispatched. Charging *every* claim against that ceiling
also catches the #1727 pathology by mistake: a ticket making genuine forward
progress (impl commits, a review that surfaced real MUST_FIX findings, an
operator resolution actually consumed) burns the same budget and gets parked at
``attempt_cap_blocked`` while working correctly.

This module is the single place that answers "did this claim produce evidence
of progress?". It is deliberately a **new sibling module** under
``cw.dispatch`` rather than logic grown inside ``routing.py`` or a second copy
under ``cw.auto_dev_result`` — per the operator's binding resolution on #1750
(R3), and because a second independent construction of :class:`ClaimEvidence`
was the round-1 review's MUST_FIX.

``extract_claim_evidence`` is the **single schema-owned extractor**. It reads a
plain dict so that both producers reach the identical code path:

* ``dispatch/routing.py`` passes the raw persisted ``last_result`` dict.
* ``reconcile/phantom/_mutations.py`` and ``reconcile/stalled/_mutations.py``
  pass a validated model's ``model_dump(mode="json")``.

Every field is read with a defensive ``.get()`` and a type check rather than by
indexing: ``last_result`` reaches us from a persisted JSON blob, and a
``BlockedResult``-shaped payload legitimately carries none of these keys. An
absent or malformed key means "no evidence", never an exception — an extractor
that raised here would take down the dispatch tick.

#2613 relocated this module verbatim from ``cw.dispatch.productivity`` to the
top-level ``cw.claim_evidence`` leaf: it imports only the sentinel vocabulary
(``cw.auto_dev_result``, which loads none of the dispatch/reconcile cycle), so
living below ``cw.dispatch`` (whose package ``__init__`` imports
``cw.reconcile``) lets the two reconcile producers import it at module scope.
``cw.dispatch.productivity`` remains as a re-export shim for the old import path.
"""

from __future__ import annotations

from dataclasses import dataclass

from cw.auto_dev_result import (
    OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
    PAUSED_FOR_USER_INPUT_STATUSES,
)

__all__ = ["ClaimEvidence", "extract_claim_evidence", "is_unproductive"]

# Why: ``auto_adopt_clean_plan`` (``reconcile/gate_recipes.py``, default-on) can
# release a ``plan_pending_approval`` park with no human into a same-stage PLAN
# re-run (``dev_queue/approval.py``'s unreviewed-plan arm), so the ceiling stays
# the bound on an adopt -> re-run -> re-park cycle with a changing fingerprint
# (#2695 R5). Defined locally; the shared vocabulary is not mutated.
_CHARGED_HUMAN_INPUT_PARKS: frozenset[str] = frozenset({"plan_pending_approval"})
# ambiguities_pending_resolution, premises_pending_verification and
# review_pending_approval: each is answered by a person (or the orchestrator
# session, ADR-0020), who is then the rate limiter. review_pending_approval's
# recipe advances the stage rather than re-queuing it, so no cycle exists.
_UNCHARGED_PARK_STATUSES: frozenset[str] = (
    PAUSED_FOR_USER_INPUT_STATUSES - _CHARGED_HUMAN_INPUT_PARKS
)


@dataclass(frozen=True)
class ClaimEvidence:
    """The four signals that exempt a finished claim from the ceiling.

    Three are progress signals; the fourth (``human_input_park``, #2695) marks
    a park whose release waits on a person rather than on another dispatch.

    Frozen so a hoisted instance can be threaded to several routing branches
    without any of them mutating what the others see.
    """

    had_commits: bool
    """The claim pushed at least one commit (``AutoDevResult.commits``)."""

    had_findings: bool
    """The claim's review surfaced real findings (must_fix_initial/should_fix).

    This is the #1727 signal: a review claim that parks the task *because* it
    found genuine MUST_FIX items did the work it was dispatched to do.
    """

    resolution_consumed: bool
    """The claim consumed an operator resolution, with provenance attached.

    STRICT per resolution R1: a bare ``resolution_consumed: true`` carries no
    trail a later reader can check, so it does not count. Only a boolean
    accompanied by a non-empty ``resolution_evidence`` object credits the
    claim. The #1750 plan's first Adopted Assumption noted no producer emitted
    these keys yet, leaving this signal structurally correct but dormant;
    #1896 wired the producer -- ``auto-dev-plan.md``'s Step 1c.0 settlement
    mechanism now emits both keys on a plan-stage pause when the round settled
    at least one item, and ``AutoDevResult`` (``cw.auto_dev_result.schema``)
    rejects a coercible non-bool ``resolution_consumed`` at the schema layer
    (a ``StrictBool`` field), not here -- this function's ``is not True``
    identity check alone would not catch a producer bug emitting
    ``"resolution_consumed": "true"`` as a string, since every sentinel
    round-trips through schema validation before reaching this extractor.

    Residual trust boundary: this function and the schema-layer strictness
    verify shape/type only, never truth. A sentinel that emits
    ``resolution_consumed: true`` with a well-formed but fabricated or stale
    ``resolution_evidence`` will be credited -- nothing downstream can check
    the claim against what actually happened. This is an accepted property of
    a trusted-producer design (the producer is this repo's own
    ``auto-dev-plan.md`` pipeline), not a defect to fix.
    """

    human_input_park: bool
    """The sentinel is a human-input park that does not charge (#2695).

    True for ``ambiguities_pending_resolution``,
    ``premises_pending_verification`` and ``review_pending_approval``, and for
    a ``blocker.reason`` in ``OPERATOR_UNAVAILABLE_BLOCKER_REASONS``. False for
    ``plan_pending_approval`` (see ``_CHARGED_HUMAN_INPUT_PARKS``) and for the
    #2154 plan-stage hard-exits, which keep their bound.
    """


def _has_commits(payload: dict[str, object]) -> bool:
    commits = payload.get("commits")
    return isinstance(commits, list) and len(commits) > 0


def _has_findings(payload: dict[str, object]) -> bool:
    review = payload.get("review")
    if not isinstance(review, dict):
        return False
    # bool is a subclass of int, but must_fix_initial/should_fix are declared
    # ints on the Review model; a bool here would be producer corruption, and
    # counting it as a finding is the conservative (over-count) direction.
    counts = (review.get("must_fix_initial"), review.get("should_fix"))
    return any(isinstance(count, int) and count > 0 for count in counts)


def _has_consumed_resolution(payload: dict[str, object]) -> bool:
    if payload.get("resolution_consumed") is not True:
        return False
    evidence = payload.get("resolution_evidence")
    return isinstance(evidence, dict) and len(evidence) > 0


def _is_human_input_park(payload: dict[str, object]) -> bool:
    # isinstance guards: an unhashable persisted value (a list) must degrade
    # to False, not raise TypeError on the set membership test.
    status = payload.get("status")
    if isinstance(status, str) and status in _UNCHARGED_PARK_STATUSES:
        return True
    blocker = payload.get("blocker")
    if not isinstance(blocker, dict):
        return False
    reason = blocker.get("reason")
    return isinstance(reason, str) and reason in OPERATOR_UNAVAILABLE_BLOCKER_REASONS


def extract_claim_evidence(payload: dict[str, object] | None) -> ClaimEvidence:
    """Read the four ceiling-exempting signals off a sentinel-shaped payload.

    Accepts ``None`` (no sentinel was ever emitted — the crashloop case) and
    degrades any non-dict payload to all-False rather than raising.
    """
    if not isinstance(payload, dict):
        return ClaimEvidence(
            had_commits=False,
            had_findings=False,
            resolution_consumed=False,
            human_input_park=False,
        )
    return ClaimEvidence(
        had_commits=_has_commits(payload),
        had_findings=_has_findings(payload),
        resolution_consumed=_has_consumed_resolution(payload),
        human_input_park=_is_human_input_park(payload),
    )


def is_unproductive(evidence: ClaimEvidence) -> bool:
    """True when a claim left behind none of the four signals.

    OR-combination: any single signal exempts the claim. A human-input park
    (#2695) is not progress, but its release waits on a person, so charging it
    would park a ticket for the operator's response time. The ceiling
    is a crashloop guard, so the bias is deliberately toward *not* charging —
    over-charging parks healthy tickets (#1727), while under-charging only
    delays a park the concierge and per-stage caps still bound.
    """
    return not (
        evidence.had_commits
        or evidence.had_findings
        or evidence.resolution_consumed
        or evidence.human_input_park
    )
