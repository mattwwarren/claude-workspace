"""The ``blocker.details`` wording of the stage pre-flight parks (#3709).

The clean-review and repair-entry texts moved here verbatim from the deleted
``cw.executor._clean_review`` and ``cw.executor._repair_entry`` modules; the
engine text is built by the executor from ``cw.review_engine.constants`` and
passed in, so this leaf never imports it. ``stage_unsupported`` has one template
for every harness, naming the harness and the stage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models.capabilities import in_enum_order

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cw.models.enums import Stage

_CLEAN_REVIEW_DETAILS = (
    "review.convergence: clean is set, but this lane's REVIEW executor is"
    " backend={backend}, whose REVIEW lane writes no convergence ledger, so"
    " finalize Step 4.0b would park the ticket. Blocked before any agent runs."
    " To proceed: {remediation}. A bare"
    " cw dev-queue requeue will not clear this; change the config first."
)
_REMEDIATION = (
    "set review.convergence: blocking for {client_ref}, or give the lane a"
    " claude-native, codex or opencode REVIEW executor"
)
_REPAIR_ENTRY_DETAILS = (
    "This row is an IMPL spawn carrying repair feedback"
    " (queue_metadata.review_feedback), and backend={backend} does not read that"
    " feedback or apply the repair-only scope. Blocked before any agent runs. The"
    " repair entry is specified for the claude-native impl path; #2983 decided"
    " fail-closed for opencode and local adopted rows (RFC 0018 B1, #3059). To"
    " proceed: run this ticket on the claude-native executor, or re-add it without"
    " --review-feedback-file or with cw dev-queue add --adopt-pr."
)
_STAGE_UNSUPPORTED_DETAILS = (
    "This {stage_upper} spawn is routed to harness={harness}, which does not"
    " declare the {stage} stage (declared: {declared}). Blocked before any agent"
    " runs. To proceed: route this stage to a harness that declares it (change"
    " the lane's executors.{stage}), then cw dev-queue requeue."
)


def remediation(client_ref: str) -> str:
    """The ways out of a clean-mode REVIEW-lane block, worded once.

    *client_ref* names the client in the caller's voice ("this client" for the
    executor block, a client-and-file reference for ``cw doctor``). Every
    blocked backend, an unrecognized one included, gets the same two config
    options.
    """
    return _REMEDIATION.format(client_ref=client_ref)


def clean_review_details(harness: str) -> str:
    """Details of a ``clean_review_unsupported_backend`` park on *harness*."""
    return _CLEAN_REVIEW_DETAILS.format(
        backend=harness, remediation=remediation("this client")
    )


def repair_entry_details(harness: str) -> str:
    """Details of a ``repair_entry_unsupported_backend`` park on *harness*."""
    return _REPAIR_ENTRY_DETAILS.format(backend=harness)


def stage_unsupported_details(
    harness: str, stage: Stage, declared: Iterable[Stage]
) -> str:
    """Details of a ``stage_unsupported`` park: *harness* lacks *stage*."""
    names = ", ".join(s.value for s in in_enum_order(declared)) or "none"
    return _STAGE_UNSUPPORTED_DETAILS.format(
        stage_upper=stage.value.upper(),
        harness=harness,
        stage=stage.value,
        declared=names,
    )
