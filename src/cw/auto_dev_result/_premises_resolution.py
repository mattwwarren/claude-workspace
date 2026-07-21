"""Parse-boundary coercion: downgrade fully-resolved premises (issue #1325).

Problem (evidence: ticket #1238, session cf6e1493/0c07d358, 2026-07-18): a
Stage-1 plan sentinel whose every `premises` item carries `verified: true`
and a `resolution` mapping onto an existing adopted/binding resolution still
parked the ticket at `premises_pending_verification` -- the model-level A5
invariant (schema.py, section 4.4) keys on the array being non-empty, not on
whether its items are still open.

Relationship to #1192: docs/headless-contract.md:293's #1192 producer note
already excludes *self-verified-this-session* premises (docs/--help/source
evidence gathered in the same run) from the emitted array at the plan-stage
producer-skill level. This coercion is defense-in-depth for the case #1192
does NOT cover: a premise resolved by a PRE-EXISTING binding resolution the
producer maps onto (the ticket's own evidence: `resolves: comment 1
Resolution 11/12/13`), not fresh in-session verification. #1192 already
means most self-verified premises never reach this coercion as
premises_pending_verification items in the first place; this coercion's
primary remaining purpose is the pre-existing-resolution case.

Scope (R1, pre-flight resolution on issue #1325): parser-side fix only. The
producer-prompt companion change (.claude/commands/auto-dev-plan.md Step
4a/4c partition language to recognize premises closed by a pre-existing
binding resolution) is deferred to #1411.
"""

from __future__ import annotations

from typing import Any

from cw.auto_dev_result._warn import _warn_once
from cw.auto_dev_result.schema import _is_resolved_premise


def _downgrade_resolved_premises(
    payload: dict[str, Any],
    raw_status: str,
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop resolved premises; downgrade to stage_complete if none remain.

    Only meaningful when *raw_status* is 'premises_pending_verification' and
    the array is a non-empty list -- an already-empty/missing array is the
    #430/#962 producer-glitch shape and is left untouched here (a no-op);
    the caller runs the existing _coerce_empty_pending_array placeholder
    injection AFTER this function, gated on status still being
    'premises_pending_verification', so that glitch behavior is unchanged.

    Three outcomes:
    - No resolved items: no-op. Array and status untouched.
    - Some (not all) resolved: those items are dropped; the array keeps
      only the still-open premises; status stays
      'premises_pending_verification'.
    - All items resolved: array becomes []; status is rewritten to
      'stage_complete'; the stale 'user_verify_premises' next_action (if
      present) is dropped -- no other next_actions entries are touched
      (open-vocabulary pass-through, docs/headless-contract.md §4.3).

    Every dropped resolved item is recorded informationally in
    friction_highlights (existing list[str] field, no schema change) --
    the mechanism the ticket's own proposed fix names for a human-auditable
    trail of premises the parser settled without a park.
    """
    raw = payload.get("premises")
    if not isinstance(raw, list) or not raw:
        return

    resolved: list[dict[str, Any]] = []
    unresolved: list[Any] = []
    for item in raw:
        if isinstance(item, dict) and _is_resolved_premise(item):
            resolved.append(item)
        else:
            unresolved.append(item)

    if not resolved:
        return

    fh = payload.get("friction_highlights")
    if not isinstance(fh, list):
        fh = []
        payload["friction_highlights"] = fh
    for item in resolved:
        claim = item.get("claim") or item.get("premise") or "(no claim text)"
        resolution = item.get("resolution")
        fh.append(f"premise resolved (issue #1325): {claim} — resolution: {resolution}")

    _warn_once(
        "auto-dev: %s sentinel dropped %d resolved premises item(s) at parse "
        "boundary (ticket=%s, schema_version=%s); see #1325",
        raw_status,
        len(resolved),
        payload.get("ticket_id", "unknown"),
        payload.get("schema_version"),
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
