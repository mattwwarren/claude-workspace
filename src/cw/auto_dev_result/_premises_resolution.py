"""Parse-boundary coercion: downgrade exempt premises (issues #1325, #2432).

Two kinds of premise item are *exempt* -- settled without a human -- and are
dropped from a `premises_pending_verification` sentinel's array here:

- **Resolved (#1325).** Problem (evidence: ticket #1238, session
  cf6e1493/0c07d358, 2026-07-18): a Stage-1 plan sentinel whose every
  `premises` item carries `verified: true` and a `resolution` mapping onto an
  existing adopted/binding resolution still parked the ticket at
  `premises_pending_verification` -- the model-level A5 invariant
  (schema.py, section 4.4) keys on the array being non-empty, not on whether
  its items are still open. Predicate: `_is_resolved_premise`.
- **Impact-exempt (#2432).** A premise whose truth value changes no code
  path in the plan: `impact` normalized to exactly "none" AND a non-empty
  `impact_reason` string, regardless of `verified`. Predicate:
  `_is_no_impact_premise`.

Relationship to #1192/#2432 producer notes: docs/headless-contract.md §4.4
already excludes *self-verified-this-session* premises and validly-exempting
`Impact: NONE` premises from the emitted array at the plan-stage
producer-skill level. This coercion is defense-in-depth for items that reach
the wire anyway -- most notably a premise resolved by a PRE-EXISTING binding
resolution the producer maps onto (the #1325 evidence: `resolves: comment 1
Resolution 11/12/13`), which the producer partition does not cover.

Scope (R1, pre-flight resolution on issue #1325): parser-side fix only. The
producer-prompt companion change (.claude/commands/auto-dev-plan.md Step
4a/4c partition language to recognize premises closed by a pre-existing
binding resolution) is deferred to #1411.
"""

from __future__ import annotations

from typing import Any

from cw.auto_dev_result._warn import _warn_once
from cw.auto_dev_result.schema import _is_no_impact_premise, _is_resolved_premise


def _downgrade_exempt_premises(
    payload: dict[str, Any],
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop exempt premises; downgrade to stage_complete if none remain.

    Called only when the raw status is 'premises_pending_verification', and
    only meaningful when the array is a non-empty list -- an
    already-empty/missing array is the #430/#962 producer-glitch shape and
    is left untouched here (a no-op); the caller runs the existing
    _coerce_empty_pending_array placeholder injection AFTER this function,
    gated on status still being 'premises_pending_verification', so that
    glitch behavior is unchanged.

    Each item is sorted once into `resolved` (`_is_resolved_premise`,
    checked first), `impact_exempt` (`_is_no_impact_premise`), or
    `unresolved` (everything else). The two exempt lists are kept separate so
    each count is reported independently in the log line, and unioned for
    the array-rewrite decision.

    Three outcomes:
    - No exempt items: no-op. Array and status untouched.
    - Some (not all) exempt: those items are dropped; the array keeps only
      the still-open premises; status stays 'premises_pending_verification'.
    - All items exempt: array becomes []; status is rewritten to
      'stage_complete'; the stale 'user_verify_premises' next_action (if
      present) is dropped -- no other next_actions entries are touched
      (open-vocabulary pass-through, docs/headless-contract.md §4.3).

    Every dropped item is recorded informationally in friction_highlights
    (existing list[str] field, no schema change), citing its claim plus its
    `resolution` (#1325) or `impact_reason` (#2432) text, and one
    deduped WARNING reports both counts.
    """
    raw = payload.get("premises")
    if not isinstance(raw, list) or not raw:
        return

    resolved: list[dict[str, Any]] = []
    impact_exempt: list[dict[str, Any]] = []
    unresolved: list[Any] = []
    for item in raw:
        if isinstance(item, dict) and _is_resolved_premise(item):
            resolved.append(item)
        elif isinstance(item, dict) and _is_no_impact_premise(item):
            impact_exempt.append(item)
        else:
            unresolved.append(item)

    dropped = resolved + impact_exempt
    if not dropped:
        return

    fh = payload.get("friction_highlights")
    if not isinstance(fh, list):
        fh = []
        payload["friction_highlights"] = fh
    for item in resolved:
        fh.append(
            f"premise resolved (issue #1325): {_claim_text(item)} — "
            f"resolution: {item.get('resolution')}"
        )
    for item in impact_exempt:
        fh.append(
            f"premise no-impact (issue #2432): {_claim_text(item)} — "
            f"impact_reason: {item.get('impact_reason')}"
        )

    _warn_once(
        "premises: dropped %d item(s) — %d resolved (see #1325), "
        "%d impact-exempt (see #2432)",
        len(dropped),
        len(resolved),
        len(impact_exempt),
        warned_blocks=warned_blocks,
        block_key=block_key,
    )
    payload["premises"] = unresolved

    if not unresolved:
        payload["status"] = "stage_complete"
        next_actions = payload.get("next_actions")
        if isinstance(next_actions, list):
            payload["next_actions"] = [
                a for a in next_actions if a != "user_verify_premises"
            ]


def _claim_text(item: dict[str, Any]) -> object:
    return item.get("claim") or item.get("premise") or "(no claim text)"
