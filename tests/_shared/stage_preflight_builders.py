"""Plain builders shared by the stage pre-flight tests (RFC 0019 H2, #3709).

Functions, not fixtures: nothing is registered in ``tests/conftest.py``
(precedent ``tests/_shared/bindings_builders.py``). The Phase 0 capture of the
old guard parks and the parity replay in ``tests/test_stage_preflight.py``
build their clients and rows from the same code here, so the two sides of the
parity table cannot drift apart.

- :func:`review_client` / :func:`engine_client`: ``ReviewConfig`` clients.
- :func:`repair_row`: an IMPL row carrying repair feedback, adopted or not.
- :data:`CONFIGS`, :func:`client_for`, :func:`task_for`: the config matrix.
- :func:`synthetic_capabilities`: a declaration with chosen fields overridden.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cw.executor._capabilities import CODEX_CAPABILITIES
from cw.models import ClientConfig, Stage, TicketTask
from cw.models.client import ReviewConfig
from tests._adopt_pr_helpers import make_repair_feedback

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models.capabilities import HarnessCapabilities

#: Client name every builder uses.
CLIENT_NAME: Final = "test"

#: Ticket id every row builder uses.
TICKET_ID: Final = "T-1"

#: The adoption branch :func:`repair_row` sets by default.
ADOPTED_BRANCH: Final = "dev/2944"

#: The repo engine skill :func:`engine_client` configures.
ENGINE_SKILL: Final = "repo-review"

#: The config matrix shared by the old-code capture and the parity replay.
#: ``blocking`` and ``clean`` differ in ``review.convergence``; ``engine`` adds
#: ``review.engine``; ``feedback`` is a row with repair feedback and no branch;
#: ``adopted`` is the same row with an adoption branch.
CONFIGS: Final = ("blocking", "clean", "engine", "feedback", "adopted")


def review_client(worktree: Path, convergence: str) -> ClientConfig:
    """A client whose ``review.convergence`` is *convergence*."""
    review = ReviewConfig.model_validate({"convergence": convergence})
    return ClientConfig(name=CLIENT_NAME, workspace_path=worktree, review=review)


def engine_client(worktree: Path) -> ClientConfig:
    """A clean-mode client with ``review.engine.skill`` set to ``repo-review``."""
    review = ReviewConfig.model_validate(
        {"convergence": "clean", "engine": {"skill": ENGINE_SKILL}}
    )
    return ClientConfig(name=CLIENT_NAME, workspace_path=worktree, review=review)


def repair_row(
    stage: Stage, *, branch: str | None = ADOPTED_BRANCH, feedback: bool = True
) -> TicketTask:
    """A row at *stage* carrying repair feedback; adopted when *branch* is set.

    ``branch=None`` gives a feedback row that is not an adoption row;
    ``feedback=False`` gives a row with no feedback at all.
    """
    payload: dict[str, object] = {
        "ticket_id": TICKET_ID,
        "client": CLIENT_NAME,
        "stage": stage,
    }
    if branch is not None:
        payload["branch"] = branch
    if feedback:
        payload["review_feedback"] = make_repair_feedback()
    return TicketTask.model_validate(payload)


def client_for(config: str, worktree: Path) -> ClientConfig:
    """The client for one :data:`CONFIGS` entry."""
    if config == "clean":
        return review_client(worktree, "clean")
    if config == "engine":
        return engine_client(worktree)
    return review_client(worktree, "blocking")


def task_for(config: str, stage: Stage) -> TicketTask:
    """The row for one :data:`CONFIGS` entry at *stage*."""
    if config == "feedback":
        return repair_row(stage, branch=None)
    if config == "adopted":
        return repair_row(stage)
    return TicketTask(ticket_id=TICKET_ID, client=CLIENT_NAME, stage=stage)


def synthetic_capabilities(**overrides: object) -> HarnessCapabilities:
    """The codex declaration with *overrides* applied (hypothetical harnesses).

    The model is frozen with ``extra="forbid"``, so overrides go through
    ``model_copy(update=...)`` on a real declaration.
    """
    return CODEX_CAPABILITIES.model_copy(update=overrides)
