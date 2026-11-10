"""Repo review engine REVIEW guard for the opencode and local pre-flights (#3216).

With ``review.engine`` configured, a REVIEW round must run the repo's engine.
claude-native and codex can (:func:`cw.review_convergence.engine_supported`);
opencode and local never do, so their pre-flight refuses the row as
``review_engine_unsupported_backend`` before any agent runs, instead of
reviewing with the built-in roster. The codex lane refuses a missing or
invalid manifest at the start of its background run instead
(``cw.codex_review._engine_start``), so the dispatch tick adds no git read.

:func:`engine_block_details` only decides and words the block; the executor
builds the blocked result with its own runner's ``make_blocked``. Leaf module:
no logging, no events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import Stage
from cw.review_convergence import engine_supported
from cw.review_engine.constants import engine_refusal_details

if TYPE_CHECKING:
    from cw.models import ClientConfig

_BACKEND_UNSUPPORTED = "backend_unsupported"


def engine_block_details(
    client: ClientConfig, stage: Stage, backend: str
) -> str | None:
    """The refusal text when this REVIEW spawn must fail closed; else ``None``.

    Blocks only a REVIEW-stage spawn on a client with ``review.engine`` set
    whose *backend* cannot run an engine round. Any other stage, a client with
    no engine, and claude-native or codex all return ``None``.
    """
    if stage != Stage.REVIEW or client.review.engine is None:
        return None
    if engine_supported(backend):
        return None
    return engine_refusal_details(
        backend, _BACKEND_UNSUPPORTED, f"skill={client.review.engine.skill}"
    )
