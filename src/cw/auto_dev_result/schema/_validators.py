"""Shared validator helpers for the sentinel models.

Predicate and guard functions used by field validators across
:mod:`cw.auto_dev_result.schema._models` and
:mod:`cw.auto_dev_result.schema._result` — extracted here so neither submodule
has to import the other. ``_is_resolved_premise`` and
``_is_no_impact_premise`` are additionally imported directly by
:mod:`cw.auto_dev_result._premises_resolution`.

Package split: issue #2193.
"""

from __future__ import annotations

from typing import Any, NewType

from cw._predicates import is_positive_real_int, parse_repo_pr_ref
from cw.auto_dev_result.schema._vocab import IMPACT_REASON_KEY

# GitHub #2321 — a same-repo PR number a dependency park waits on. Defined here
# (not in _models) so the pure validator below and the model share it without a cycle.
DependsOnPr = NewType("DependsOnPr", int)


def parse_depends_on_pr(value: object) -> DependsOnPr | None:
    """Return *value* as a :data:`DependsOnPr` iff it is a positive real int.

    ``type(...) is int`` rejects ``bool`` (a subclass of int), floats, digit
    strings and every other non-int. Zero and negatives are not PR numbers.
    """
    if is_positive_real_int(value):
        return DependsOnPr(value)
    return None


# GitHub #3634 — an explicit ``owner/repo#N`` ref a dependency park waits on.
DependsOnPrRef = NewType("DependsOnPrRef", str)


def parse_depends_on_pr_ref(value: object) -> DependsOnPrRef | None:
    """Return *value* as a canonical :data:`DependsOnPrRef`, or None.

    Accepts only a string ``"owner/repo#N"`` (N positive, no leading zero, no
    ``.``/``..`` segment); a bare number, a ``#N`` shorthand and every non-str
    are rejected.
    """
    parsed = parse_repo_pr_ref(value)
    if parsed is None:
        return None
    return DependsOnPrRef(f"{parsed[0]}#{parsed[1]}")


def _reject_empty_string_items(v: list[str], field_name: str) -> list[str]:
    """Raise if any item in *v* is empty/whitespace-only (issue #1130).

    Shared by AutoDevResult's commits/friction_highlights/next_actions
    multi-field validator and Health.shortcuts. Mirrors the indexed-message
    shape of _reject_empty_question_ambiguities/_reject_empty_claim_premises,
    adapted for bare string items rather than dict items.
    """
    for idx, item in enumerate(v):
        if _is_blank(item):
            msg = (
                f"{field_name}[{idx}] is an empty/whitespace-only string "
                f"(got {item!r}); every item must be a non-empty, non-whitespace "
                "string. Drop the empty item (see #1130)."
            )
            raise ValueError(msg)
    return v


def _has_usable_question(item: dict[str, Any]) -> bool:
    """Return True iff *item* carries a non-empty, non-whitespace question string."""
    q = item.get("question")
    return isinstance(q, str) and bool(q.strip())


def _has_usable_premise_text(item: dict[str, Any]) -> bool:
    """Return True iff *item* carries non-empty text under 'claim' or 'premise'."""
    for key in ("claim", "premise"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return True
    return False


def _is_resolved_premise(item: dict[str, Any]) -> bool:
    """Return True iff *item* is fully resolved (issue #1325).

    Resolved means BOTH: `verified` is the JSON boolean `True` -- strict, no
    truthy-string/int tolerance ("true", 1, etc. do NOT count; asymmetric
    risk favors under-matching over silently skipping a real human
    checkpoint) -- AND `resolution` is present as a non-empty, non-whitespace
    string naming the adopted/binding resolution the premise maps onto. The
    producer's own `resolves` key (quoted verbatim in issue #1325's evidence)
    is deliberately NOT accepted as an alias -- narrower is safer for a gate
    that removes a human checkpoint; only the documented `resolution` key
    (docs/headless-contract.md §4.4) counts. Both conditions independently
    gate: verified-only or resolution-only leaves the premise parked.
    """
    if item.get("verified") is not True:
        return False
    resolution = item.get("resolution")
    return isinstance(resolution, str) and bool(resolution.strip())


def _is_no_impact_premise(item: dict[str, Any]) -> bool:
    """Return True iff *item* is impact-exempt (issue #2432).

    Exempt means BOTH: `impact` normalizes (strip+lower) to exactly
    "none" -- independent of `verified`/`resolution` (contrast
    `_is_resolved_premise` above -- an impact-exempt item is dropped
    regardless of its `verified` value) -- AND `impact_reason` is
    present as a non-empty, non-whitespace string, mirroring
    `_is_resolved_premise`'s `resolution` requirement (#2432, R5). Both
    conditions independently gate: an `impact: none` item with no (or a
    blank) `impact_reason` is NOT exempt at this predicate -- it can
    still settle into `self_verified`/`deferred` upstream via a
    well-formed `Verified:` value; this predicate governs only this
    coercion's drop decision.
    """
    impact = item.get("impact")
    if not (isinstance(impact, str) and impact.strip().lower() == "none"):
        return False
    reason = item.get(IMPACT_REASON_KEY)
    return isinstance(reason, str) and bool(reason.strip())


def _is_blank(s: str) -> bool:
    """Return True iff *s* is empty or whitespace-only."""
    return not s.strip()
