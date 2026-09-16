"""Shared validator helpers for the sentinel models.

Predicate and guard functions used by field validators across
:mod:`cw.auto_dev_result.schema._models` and
:mod:`cw.auto_dev_result.schema._result` — extracted here so neither submodule
has to import the other. ``_is_resolved_premise`` is additionally imported
directly by :mod:`cw.auto_dev_result._premises_resolution`.

Package split: issue #2193.
"""

from __future__ import annotations

from typing import Any


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


def _is_blank(s: str) -> bool:
    """Return True iff *s* is empty or whitespace-only."""
    return not s.strip()
