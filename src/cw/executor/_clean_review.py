"""Clean-mode REVIEW guard shared by the opencode and local pre-flights (#2797).

Under ``review.convergence: clean`` finalize Step 4.0b (``cw review converge
verify-tip``) refuses a ticket whose worktree holds no converged convergence
ledger. Only the claude-native and codex REVIEW lanes write that ledger
(:func:`cw.review_convergence.clean_mode_supported`). An opencode or ``local``
(aider) REVIEW lane would run a full review and still park every ticket at
finalize, and a requeue cannot clear it, so both pre-flights fail closed here,
before any agent runs.

:func:`clean_review_block_details` only decides and words the block. Each
executor builds the blocked result with its own runner's ``make_blocked``,
because the two builders live in different runner modules.
:data:`CLEAN_REVIEW_GUARDED_BACKENDS` is the guard's coverage, which the
``review-convergence`` doctor check reads so its "blocked before any agent
runs" claim cannot drift from what this guard actually blocks. Leaf module:
no logging, no events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import LOCAL_BACKEND, OPENCODE_BACKEND, Stage
from cw.review_convergence import clean_mode_supported

if TYPE_CHECKING:
    from cw.models import ClientConfig

#: Backend-neutral ``blocker.reason``; the backend is named in ``details``.
CLEAN_REVIEW_UNSUPPORTED_BACKEND = "clean_review_unsupported_backend"

#: Backends whose pre-flight calls :func:`clean_review_block_details`. Wiring
#: one into clean mode (#2806 for opencode) needs no edit here:
#: ``clean_mode_supported`` short-circuits first.
CLEAN_REVIEW_GUARDED_BACKENDS: frozenset[str] = frozenset(
    {OPENCODE_BACKEND, LOCAL_BACKEND}
)

_DETAILS = (
    "review.convergence: clean is set, but this lane's REVIEW executor is"
    " backend={backend}, whose REVIEW lane writes no convergence ledger, so"
    " finalize Step 4.0b would park the ticket. Blocked before any agent runs."
    " To proceed: {remediation}. A bare"
    " cw dev-queue requeue will not clear this; change the config first."
)
_REMEDIATION = (
    "set review.convergence: blocking for {client_ref}, or give the lane a"
    " claude-native or codex REVIEW executor{wait}"
)
_OPENCODE_WAIT = ", or wait for #2806 (wires opencode into clean mode)"


def remediation(client_ref: str, backend: str) -> str:
    """The ways out of a clean-mode REVIEW-lane block, worded once.

    *client_ref* names the client in the caller's voice ("this client" for the
    executor block, a client-and-file reference for ``cw doctor``). Only an
    opencode lane gets the ``#2806`` wait option; every other backend, an
    unrecognized one included, gets the two config options alone.
    """
    wait = _OPENCODE_WAIT if backend == OPENCODE_BACKEND else ""
    return _REMEDIATION.format(client_ref=client_ref, wait=wait)


def clean_review_block_details(
    client: ClientConfig, stage: Stage, backend: str
) -> str | None:
    """Remediation text when this REVIEW spawn must fail closed; ``None`` otherwise.

    Blocks only a REVIEW-stage spawn on a ``review.convergence: clean`` client
    whose *backend* cannot write the convergence ledger. A blocking client,
    any other stage, and a supported backend (claude-native, codex) all
    return ``None``.
    """
    if stage != Stage.REVIEW or client.review.convergence != "clean":
        return None
    if clean_mode_supported(backend):
        return None
    return _DETAILS.format(
        backend=backend, remediation=remediation("this client", backend)
    )
