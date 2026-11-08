"""Wire-key constants for dict crossings that no type checker can follow.

A leaf of the ``cw.models.tasks`` package.
"""

from __future__ import annotations

# The two wire keys the #2102 plan-approval binding travels under. Each names a
# Pydantic field on the models below/beside it -- `plan_draft_fingerprint` on
# `AutoDevResult` (read out of `Session.last_result`, an untyped dict) and
# `plan_approved_fingerprint` on `TicketTask` (written into the approve result
# dict and into `spawn.py`'s `queue_metadata`). Both crossings are dict
# subscripts, which no type checker relates back to the field, so the literals
# are defined once here and asserted equal to the field names in
# `tests/test_plan_approval_fingerprint_binding.py`: a rename then fails CI
# instead of silently severing the transport.
PLAN_DRAFT_FINGERPRINT_KEY = "plan_draft_fingerprint"
PLAN_APPROVED_FINGERPRINT_KEY = "plan_approved_fingerprint"
PLAN_PROMOTED_KEY = "plan_promoted"
SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY = "scope_drift_approved_extra_files"
SCOPE_DRIFT_APPROVED_HEAD_KEY = "scope_drift_approved_head"
MUST_FIX_OVERRIDE_KEY = "must_fix_override"

# The single recognised key of the per-lane / per-ticket
# ``park_on_abandoned_exit`` maps (#2135). Defined HERE, not in
# ``cw.reconcile.abandoned_exit`` where the resolver that reads it lives:
# models sits below reconcile in the import graph, so reconcile can import
# this and not the other way round. It was briefly defined in both places,
# which is one rename away from a lane map that validates at config-load time
# and then silently resolves to the default-off floor at Stop time.
PARK_ON_ABANDONED_EXIT_KEY = "park_on_abandoned_exit"
