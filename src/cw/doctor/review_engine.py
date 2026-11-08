"""Doctor warning for a repo review engine on a non-Claude-native lane (#3039).

A ``review.engine`` runs only in the Claude-native review stage. For every
client that sets one, each lane whose pipeline has a REVIEW stage but whose
REVIEW executor is not claude-native gets one ``review-engine/<client>/<lane>``
warning: the engine is inert there, and the lane runs the built-in roster.

Config-only, not gated on ``is_clean`` (an engine implies clean, so the
setting itself is already validated). Output carries names only: no summary
text, no skill file text, no paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.doctor._shared import CheckResult
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.models import CLAUDE_NATIVE_BACKEND, Stage, TicketTask

if TYPE_CHECKING:
    from cw.models import ClientConfig

_WARNING = (
    "review.engine is set, but lane '{lane}' runs REVIEW on backend={backend}; "
    "the engine runs only in the claude-native review stage, so this lane runs "
    "the built-in reviewer roster instead."
)


def _lane_backend(name: str, client: ClientConfig, lane: str) -> str | None:
    """The REVIEW backend of *lane*, or ``None`` when the lane has no REVIEW stage."""
    # ticket_id="" is the documented "no associated ticket" sentinel.
    task = TicketTask(ticket_id="", client=name, lane=lane, stage=Stage.REVIEW)
    if Stage.REVIEW not in resolve_pipeline_stages(task, client):
        return None
    return resolve_executor_config(Stage.REVIEW, task, client).backend


def _check_review_engine(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """One warning per non-claude-native review lane of each client with an engine."""
    results: list[CheckResult] = []
    for name, client in clients.items():
        if client.review.engine is None:
            continue
        for lane in client.effective_lanes:
            backend = _lane_backend(name, client, lane.name)
            if backend is None or backend == CLAUDE_NATIVE_BACKEND:
                continue
            results.append(
                CheckResult(
                    name=f"review-engine/{name}/{lane.name}",
                    ok=True,
                    warn=True,
                    detail=_WARNING.format(lane=lane.name, backend=backend),
                )
            )
    return results
