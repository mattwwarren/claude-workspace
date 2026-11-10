"""FINALIZE guard for the opencode executor (RFC 0018 B1, #3059).

The FINALIZE prompt for opencode points at the claude-native
``auto-dev-finalize.md`` command file. That flow runs steps an opencode
subprocess cannot execute (the Skill tool for ``/prep-pr``, Agent subagents,
hooks), and no real opencode FINALIZE run has verified it. Mock verification
would prove only that the fake behaves, so the executor fails closed instead
(R1): the spawn is refused in pre-flight, before the binary probe and before
any agent runs.

Revisit when a real opencode FINALIZE run can be exercised.

Leaf module: no logging, no events (same shape as ``_repair_entry`` and
``_clean_review``).
"""

from __future__ import annotations

from cw.models import Stage

#: Backend-neutral ``blocker.reason``; the backend is named in ``details``.
FINALIZE_UNSUPPORTED_BACKEND = "finalize_unsupported_backend"

_DETAILS = (
    "This FINALIZE spawn is routed to backend={backend}, whose hand-written FINALIZE"
    " prompt points at the claude-native auto-dev-finalize.md. That doc runs steps"
    " (the Skill tool for /prep-pr, Agent subagents, hooks) the backend cannot run,"
    " and no real run has verified it. Blocked before any agent runs (RFC 0018 B1,"
    " #3059). To proceed: run FINALIZE on the claude-native executor (change the"
    " lane's executors.finalize), then cw dev-queue requeue."
)


def finalize_block_details(stage: Stage, backend: str) -> str | None:
    """Details text when this spawn must fail closed; ``None`` otherwise.

    Only the opencode pre-flight calls it. Codex handles FINALIZE through
    ``CODEX_REVIEW_ONLY``, and local is IMPL-only.
    """
    if stage != Stage.FINALIZE:
        return None
    return _DETAILS.format(backend=backend)
