"""PR cross-references carried in a blocker's free-text ``details`` (#1713).

Extracted from the flat ``dispatch/routing.py`` by #1728. Two Rule 5 blocker
reasons name another PR that this ticket's fate depends on, and neither is
carried in a structured sentinel field -- the producer only ever writes them
into ``blocker.details`` prose. This module owns the reason literals and the
regex that reads them back out. ``reconcile/tasks.py`` reaches both constants
through the package facade via its documented deferred import.

Imports ``re`` and nothing else: no back-dependency on ``routing/__init__.py``,
and no ``record_event``/``_stage_regress``/``_stage_advance_unchecked`` call,
which is why it was safe to move out (see the package ``__init__``'s
"Monkeypatch coupling" note).
"""

from __future__ import annotations

import re

# Rule 5 blocker.reason literals the routing table reason-keys on directly
# (GitHub #1713). Deliberately local literals, not an import of
# cw.auto_dev_result.parse.BLOCKER_REASON_PRIOR_PIPELINE_PR_OPEN: that
# constant is the *parser's* BlockedResult reason code (a synthetic result for
# a sentinel the parser itself could not extract), a different producer/
# context from the routing table's read of a well-formed AutoDevResult's
# blocker.reason -- textually identical value, deliberately separate constant,
# same precedent as dev_queue.lifecycle._SALVAGE_NO_SENTINEL_DISPOSITION vs.
# reconcile._shared._NEEDS_SALVAGE_REASON.
_AUTOMERGE_NOT_ARMED_REASON = "automerge_not_armed"
_PRIOR_PIPELINE_PR_OPEN_REASON = "prior_pipeline_pr_open"

# Matches "PR #<N>" in a prior_pipeline_pr_open blocker.details string (see
# .claude/commands/auto-dev-finalize.md's template: "PR #<number>
# (<headRefName>) is open and shares files..."). The producer contract
# (same doc, line ~122: "When multiple open PRs overlap, list all
# overlapping PRs in `details`") documents that details may legitimately
# name MORE THAN ONE overlapping PR when a row is blocked on several at
# once -- _extract_blocked_on_pr below scans every match and fails closed
# (returns None) unless exactly one is found, rather than silently picking
# the first and mismatching the release condition against a still-open
# second PR.
_BLOCKING_PR_NUMBER_RE = re.compile(r"PR #(\d+)")


def _extract_blocked_on_pr(details: object) -> int | None:
    """Extract the blocking PR number from a prior_pipeline_pr_open blocker.

    Regex-only (R3 precedent, mirrors ``_marker_version``): no structured
    field carries this reference (GitHub #1713 root-cause chain, Variant B) --
    the producer only ever emits it inside ``blocker.details`` free text.
    Fails closed (returns ``None``) on a malformed/absent ``details`` or on
    ANY count of matches other than exactly one, rather than raising or
    guessing. A malformed/absent details degrades to "no cross-reference"
    instead of crashing dispatch routing; an ambiguous multi-PR block (the
    producer contract permits ``details`` to name more than one overlapping
    PR) degrades to the same "no cross-reference" blind spot the ticket's
    orphaned-reference case already accepts, rather than releasing the row
    the moment ONE of several blocking PRs merges while another
    file-overlapping PR is still open.
    """
    if not isinstance(details, str):
        return None
    matches = _BLOCKING_PR_NUMBER_RE.findall(details)
    if len(matches) != 1:
        return None
    return int(matches[0])
