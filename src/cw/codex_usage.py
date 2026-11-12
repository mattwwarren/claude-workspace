"""Per-run usage accounting for codex stage turns (#3063, RFC 0018 C4).

The stage driver builds one :class:`UsageAccumulator` per run. It rides inside
the ``runner`` object already threaded to every launch site as a
:class:`RecordingRunner`, re-labelled per site with
:func:`cw.codex_runner.relabeled`, so a launch site changes by one expression
and a new site cannot silently skip recording
(``tests/test_codex_usage_launch_sites.py`` pins the table). Completion
receives the accumulator as an explicit ``usage=`` argument and persists a
:class:`~cw.models.usage.UsageSnapshot`.

Lock discipline (ADR-0019, R10): everything here is pure in-memory work. Each
turn is parsed and recorded inside ``RecordingRunner.run`` and the snapshot is
priced in :meth:`UsageAccumulator.snapshot`, both before any lock is taken.
The module imports neither ``cw.executor`` nor ``cw.config``.

``cost_usd`` stays the producer's own figure: the priced estimate lands in
``Session.cost_breakdown`` only, keyed by the configured model string.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Final

from cw.codex_review._audit_events import parse_turn_usage
from cw.codex_review._const import _JSON_FLAG
from cw.models.usage import (
    USAGE_UNAVAILABLE_FRICTION_PREFIX,
    USAGE_UNAVAILABLE_WARNING,
    USAGE_UNKNOWN_MODEL,
    SessionUsage,
    TokenTotals,
    TurnUsage,
    UsageSnapshot,
    UsageStage,
    UsageState,
    UsageUnavailable,
    UsageUnavailableReason,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.models.orchestrator_config.stage import ModelPrice

__all__ = [
    "NO_USAGE",
    "RecordingRunner",
    "UsageAccumulator",
    "price_totals",
]

_log = logging.getLogger(__name__)

_PER_MILLION: Final = 1_000_000
_MODEL_FLAG: Final = "-m"


def price_totals(totals: TokenTotals, price: ModelPrice) -> float:
    """Return the USD estimate for *totals* under *price*.

    ``cached_input_tokens`` is a subset of ``input_tokens``: the uncached part
    is billed at the input price and the cached part at the cached price.
    Reasoning is a subset of output, so it adds nothing, and
    ``cache_write_input_tokens`` is not priced.
    """
    uncached = max(totals.input_tokens - totals.cached_input_tokens, 0)
    return (
        uncached * price.input_usd_per_mtok
        + totals.cached_input_tokens * price.cached_input_usd_per_mtok
        + totals.output_tokens * price.output_usd_per_mtok
    ) / _PER_MILLION


def _add_turn(totals: TokenTotals, usage: TurnUsage) -> None:
    """Fold one turn's present fields into *totals* (``None`` counts as zero)."""
    totals.input_tokens += usage.input_tokens or 0
    totals.cached_input_tokens += usage.cached_input_tokens or 0
    totals.cache_write_input_tokens += usage.cache_write_input_tokens or 0
    totals.output_tokens += usage.output_tokens or 0
    totals.reasoning_tokens += usage.reasoning_tokens or 0
    totals.turns += 1


def _fully_priceable(usage: TurnUsage) -> bool:
    """True when input, cached and output are all present on the turn."""
    return (
        usage.input_tokens is not None
        and usage.cached_input_tokens is not None
        and usage.output_tokens is not None
    )


class UsageAccumulator:
    """Collects one run's per-turn usage; pure in-memory, never locks."""

    def __init__(
        self,
        session_id: str,
        ticket_id: str,
        prices: Mapping[str, ModelPrice],
        *,
        enabled: bool = True,
    ) -> None:
        self._session_id = session_id
        self._ticket_id = ticket_id
        self._prices = dict(prices)
        self._enabled = enabled
        # Runner threads may record concurrently (sharded review roles).
        self._mutex = threading.Lock()
        self._by_stage: dict[UsageStage, TokenTotals] = {}
        self._by_model: dict[str, TokenTotals] = {}
        self._unpriceable_models: set[str] = set()
        self._partial_turns = 0
        self._unavailable_turns = 0
        self._first_reason: UsageUnavailableReason | None = None
        self._dirty = False

    @classmethod
    def disabled(cls) -> UsageAccumulator:
        """Return an accumulator that records and warns about nothing."""
        return cls("", "", {}, enabled=False)

    @property
    def dirty(self) -> bool:
        """True when a turn was recorded since the last :meth:`snapshot`."""
        return self._dirty

    def record_turn(
        self,
        kind: UsageStage,
        role: str,
        model: str,
        parsed: TurnUsage | UsageUnavailable,
        *,
        completed: bool,
    ) -> None:
        """Record one turn's parsed usage under *kind* and *model*.

        Tokens that are present are always recorded, even from a failed turn:
        the spend happened. A turn with no usable usage is the unavailable
        marker only when it *completed* (exit 0, no timeout) and is not a
        ``turn.failed`` stream, because a failed turn legitimately has none.
        """
        if not self._enabled:
            return
        with self._mutex:
            self._dirty = True
            if isinstance(parsed, TurnUsage):
                self._record_tokens(kind, model, parsed)
                return
            if completed and parsed.reason is not UsageUnavailableReason.TURN_FAILED:
                self._record_unavailable(kind, role, parsed.reason)

    def _record_tokens(self, kind: UsageStage, model: str, usage: TurnUsage) -> None:
        _add_turn(self._by_stage.setdefault(kind, TokenTotals()), usage)
        _add_turn(self._by_model.setdefault(model, TokenTotals()), usage)
        if not _fully_priceable(usage):
            self._partial_turns += 1
            self._unpriceable_models.add(model)

    def _record_unavailable(
        self, kind: UsageStage, role: str, reason: UsageUnavailableReason
    ) -> None:
        self._unavailable_turns += 1
        if self._first_reason is None:
            self._first_reason = reason
        _log.warning(
            USAGE_UNAVAILABLE_WARNING,
            role,
            self._session_id,
            self._ticket_id,
            kind.value,
            reason.value,
        )

    def snapshot(self) -> UsageSnapshot | None:
        """Return what completion persists, or ``None`` when nothing was recorded.

        Clears :attr:`dirty`. Pricing happens here, in memory, so the caller
        takes it before ``sessions_lock``.
        """
        with self._mutex:
            self._dirty = False
            if not self._by_model and not self._unavailable_turns:
                return None
            usage = SessionUsage(
                state=self._state(),
                by_stage={k: v.model_copy() for k, v in self._by_stage.items()},
                by_model={k: v.model_copy() for k, v in self._by_model.items()},
                unavailable_turns=self._unavailable_turns,
                unavailable_reason=self._first_reason,
            )
            return UsageSnapshot(usage, self._cost_breakdown(usage))

    def _state(self) -> UsageState:
        if not self._by_model:
            return UsageState.UNAVAILABLE
        if self._unavailable_turns or self._partial_turns:
            return UsageState.PARTIAL
        return UsageState.RECORDED

    def _cost_breakdown(self, usage: SessionUsage) -> dict[str, float] | None:
        """Dollar figures for fully priceable models with a configured price."""
        costs = {
            model: price_totals(totals, self._prices[model])
            for model, totals in usage.by_model.items()
            if model != USAGE_UNKNOWN_MODEL
            and model in self._prices
            and model not in self._unpriceable_models
        }
        return costs or None

    def friction_token(self) -> str | None:
        """Return the ``friction_highlights`` token for unavailable turns, if any."""
        if self._first_reason is None:
            return None
        return (
            f"{USAGE_UNAVAILABLE_FRICTION_PREFIX}{self._unavailable_turns} completed "
            "codex turn(s) reported no token usage "
            f"(reason={self._first_reason.value})"
        )

    def recording_runner(
        self, kind: UsageStage, runner: CodexRunner
    ) -> RecordingRunner:
        """Wrap *runner* so each ``--json`` turn is recorded under *kind*."""
        return RecordingRunner(runner, self, kind, kind.value)


#: Records nothing and warns about nothing: the default for callers with no
#: stage run to account to (the ``cw review confirm`` CLI paths).
NO_USAGE: Final = UsageAccumulator.disabled()


def _model_from_argv(argv: list[str]) -> str:
    """Return the value after ``-m``, or the unknown-model key."""
    if _MODEL_FLAG in argv:
        index = argv.index(_MODEL_FLAG)
        if index + 1 < len(argv):
            return argv[index + 1]
    return USAGE_UNKNOWN_MODEL


class RecordingRunner:
    """A ``CodexRunner`` that records each ``--json`` turn into an accumulator.

    Records only when ``--json`` is in argv, which skips the capability probe
    and the degrade-and-retry reruns (both launch without it) by construction.
    """

    def __init__(
        self,
        inner: CodexRunner,
        accumulator: UsageAccumulator,
        kind: UsageStage,
        role: str,
    ) -> None:
        self._inner = inner
        self._accumulator = accumulator
        self._kind = kind
        self._role = role

    @property
    def inner(self) -> CodexRunner:
        """The runner that actually launches the turn."""
        return self._inner

    @property
    def accumulator(self) -> UsageAccumulator:
        """The accumulator this runner records into."""
        return self._accumulator

    @property
    def kind(self) -> UsageStage:
        """The usage stage this runner records under."""
        return self._kind

    def relabel(self, kind: UsageStage | None, role: str | None) -> RecordingRunner:
        """Return a runner over the same inner runner under a new label.

        Wraps the inner runner, never ``self``, so a turn is recorded once. A
        new *kind* with no *role* resets the role to the kind's value.
        """
        new_kind = kind or self._kind
        if role is not None:
            new_role = role
        elif kind is not None:
            new_role = kind.value
        else:
            new_role = self._role
        return RecordingRunner(self._inner, self._accumulator, new_kind, new_role)

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        result = self._inner.run(worktree, argv, timeout_seconds, stdin=stdin)
        if _JSON_FLAG in argv:
            self._accumulator.record_turn(
                self._kind,
                self._role,
                _model_from_argv(argv),
                parse_turn_usage(result.stdout),
                completed=result.returncode == 0 and not result.timed_out,
            )
        return result
