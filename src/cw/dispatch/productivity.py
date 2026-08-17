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
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ClaimEvidence", "extract_claim_evidence", "is_unproductive"]


@dataclass(frozen=True)
class ClaimEvidence:
    """The three progress signals a finished claim can leave behind.

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
    claim. See the #1750 plan's first Adopted Assumption: no producer emits
    these keys yet, so this signal is structurally correct but dormant until a
    fast-follow wires the sentinel schema.
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


def extract_claim_evidence(payload: dict[str, object] | None) -> ClaimEvidence:
    """Read the three progress signals off a sentinel-shaped payload.

    Accepts ``None`` (no sentinel was ever emitted — the crashloop case) and
    degrades any non-dict payload to all-False rather than raising.
    """
    if not isinstance(payload, dict):
        return ClaimEvidence(
            had_commits=False, had_findings=False, resolution_consumed=False
        )
    return ClaimEvidence(
        had_commits=_has_commits(payload),
        had_findings=_has_findings(payload),
        resolution_consumed=_has_consumed_resolution(payload),
    )


def is_unproductive(evidence: ClaimEvidence) -> bool:
    """True when a claim left behind none of the three progress signals.

    OR-combination: any single signal makes the claim productive. The ceiling
    is a crashloop guard, so the bias is deliberately toward *not* charging —
    over-charging parks healthy tickets (#1727), while under-charging only
    delays a park the concierge and per-stage caps still bound.
    """
    return not (
        evidence.had_commits or evidence.had_findings or evidence.resolution_consumed
    )
