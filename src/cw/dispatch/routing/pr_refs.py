"""PR cross-references carried in a blocker's free-text ``details`` (#1713).

Extracted from the flat ``dispatch/routing.py`` by #1728. Two Rule 5 blocker
reasons name another PR that this ticket's fate depends on, and neither is
carried in a structured sentinel field -- the producer only ever writes them
into ``blocker.details`` prose. This module owns the regex that reads them
back out. The two reason literals themselves moved to the ``cw.queue_rows``
leaf (#2613) so ``reconcile/tasks.py`` can import them at module scope; the
routing package ``__init__`` imports them from there.

Imports ``re`` and nothing else: no back-dependency on ``routing/__init__.py``,
and no ``record_event``/``_stage_regress``/``_stage_advance_unchecked`` call,
which is why it was safe to move out (see the package ``__init__``'s
"Monkeypatch coupling" note).
"""

from __future__ import annotations

import re

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
