"""The codex lane's engine check at the start of a background review (#3216).

Run once, before any codex call, from ``codex_background._review_and_confirm``
(R16): the dispatch tick itself gains no git read. With ``review.engine`` set,
the lane reads the skill's ``cw-engine.json`` from the default branch and
either returns the engine to run or refuses the row:

- no manifest, an invalid one, or one whose ``backends`` lacks ``codex``:
  ``review_engine_unsupported_backend`` (``retry_eligible: false``);
- an unresolvable default branch or skill: ``agent_block`` with the
  ``engine_unresolvable`` friction (``retry_eligible: true``), as on
  claude-native.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_review._engine_round import (
    EngineRoundSpec,
    engine_unresolvable_blocked,
    manifest_refusal_blocked,
)
from cw.review_engine.context import EngineContext
from cw.review_engine.manifest_source import (
    ManifestStatus,
    read_default_branch_manifest,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.models import ClientConfig, TicketTask


def engine_round_spec(
    client: ClientConfig, task: TicketTask, worktree: Path
) -> EngineRoundSpec | AutoDevResult | None:
    """The engine this codex lane runs, a refusal, or ``None`` with no engine."""
    engine = client.review.engine
    if engine is None:
        return None
    context = EngineContext(
        skill=engine.skill,
        guarded_paths=tuple(engine.guarded_paths),
        ticket_id=task.ticket_id,
        client=client.name,
        default_branch=client.default_branch,
    )
    read = read_default_branch_manifest(worktree, client.default_branch, engine.skill)
    if read.status == ManifestStatus.LOADED:
        return EngineRoundSpec(context=context)
    if read.status == ManifestStatus.UNRESOLVABLE:
        return engine_unresolvable_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            skill=engine.skill,
            cause=read.cause,
        )
    return manifest_refusal_blocked(
        ticket_id=task.ticket_id, worktree=worktree, read=read
    )
