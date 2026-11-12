"""Usage-accounting models for stage turns (#3063, RFC 0018 C4).

A DAG leaf: pydantic and the standard library only, so ``cw.models.session``,
``cw.codex_runner``, ``cw.codex_usage`` and the harvest path can all import it
without a cycle.

``SessionUsage`` is the persisted record (``Session.usage``): per-stage and
per-model token totals plus an availability state, so a turn whose output
carried no usage is visibly *unavailable* rather than silently zero. The
``NamedTuple`` types are the in-memory values the parser and accumulator pass
around; they never reach disk.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, NamedTuple

from pydantic import BaseModel, Field


class UsageStage(StrEnum):
    """The kind of turn a usage record belongs to (finer than ``Stage``)."""

    PLAN = "plan"
    PLAN_STATION = "plan_station"
    IMPL = "impl"
    FINALIZE = "finalize"
    REVIEW_ROLE = "review_role"
    ENGINE_AGGREGATE = "engine_aggregate"
    FIX = "fix"
    TRACKER_IO = "tracker_io"
    SESSION = "session"


class UsageState(StrEnum):
    """Whether a session's recorded usage is complete."""

    RECORDED = "recorded"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class UsageUnavailableReason(StrEnum):
    """Why a turn (or a whole backend) produced no token usage."""

    NO_OUTPUT = "no_output"
    NO_USAGE_BLOCK = "no_usage_block"
    USAGE_FIELDS_NULL = "usage_fields_null"
    TURN_FAILED = "turn_failed"
    BACKEND_OUTPUT_UNPINNED = "backend_output_unpinned"


#: Model key for a turn whose argv carried no ``-m``: tokens are recorded, no
#: price lookup is attempted.
USAGE_UNKNOWN_MODEL: Final = "unknown"

#: Human text for ``BACKEND_OUTPUT_UNPINNED``: opencode and local (aider) have no
#: pinned output shape to read usage from; the reader is follow-up #3552.
BACKEND_OUTPUT_UNPINNED_DETAIL: Final = (
    "no pinned output shape for this backend; see #3552"
)

#: One shared ``%``-format for the unavailable warning, used by the codex
#: accumulator and the opencode/local harvest stamp.
USAGE_UNAVAILABLE_WARNING: Final = (
    "usage_unavailable: role=%s session=%s ticket=%s stage=%s reason=%s"
)

#: Prefix of the ``friction_highlights`` token naming unavailable usage.
USAGE_UNAVAILABLE_FRICTION_PREFIX: Final = "usage_unavailable: "


class TokenTotals(BaseModel):
    """Summed token counts for one stage or one model."""

    input_tokens: int = 0
    # A subset of ``input_tokens`` (the cached part), never added on top.
    cached_input_tokens: int = 0
    # Reported by codex, kept as its own field, never priced.
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    # A subset of ``output_tokens``.
    reasoning_tokens: int = 0
    turns: int = 0


class SessionUsage(BaseModel):
    """Per-stage and per-model token totals for one session (``Session.usage``)."""

    state: UsageState
    by_stage: dict[UsageStage, TokenTotals] = Field(default_factory=dict)
    by_model: dict[str, TokenTotals] = Field(default_factory=dict)
    unavailable_turns: int = 0
    unavailable_reason: UsageUnavailableReason | None = None

    @property
    def total_tokens(self) -> int | None:
        """Input plus output over ``by_model``; ``None`` when nothing was counted."""
        if not self.by_model:
            return None
        return sum(t.input_tokens + t.output_tokens for t in self.by_model.values())


class TurnUsage(NamedTuple):
    """One turn's parsed usage; each field ``None`` when the stream omitted it."""

    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None

    @property
    def absent(self) -> bool:
        """True when none of the four reported fields is present.

        ``cache_write_input_tokens`` is not one of them: a turn that reports
        only cache writes has still told us nothing about its spend.
        """
        return (
            self.input_tokens is None
            and self.cached_input_tokens is None
            and self.output_tokens is None
            and self.reasoning_tokens is None
        )


class UsageUnavailable(NamedTuple):
    """The parser's explicit "no usable usage" result."""

    reason: UsageUnavailableReason


class UsageSnapshot(NamedTuple):
    """What the completion door persists: the usage record and priced dollars."""

    usage: SessionUsage
    cost_breakdown: dict[str, float] | None
