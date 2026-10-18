"""Lane pause cause, resume hint and the breaker predicates they share (#2696).

One place for what a paused lane looks like to an operator, so ``cw dev-queue
status``, ``cw lane ls`` and ``cw dev-queue show`` render the same cause and
the same ``cw lane resume`` hint, and so the dispatch breaker
(:mod:`cw.dispatch.lane_circuit`) and the ticket view agree on when a lane's
breaker has tripped.

A leaf: pydantic, the stdlib and :mod:`cw._util` only at runtime; ``cw.models``
types are imported under ``TYPE_CHECKING``, so ``cw.models.ticket_view`` can
import :class:`LanePauseView` at runtime without a cycle.

Timestamps follow one rule everywhere: whole seconds, UTC, ``Z`` suffix.
:func:`utc_now_seconds` truncates at write time, so a JSON dump of a stamp
needs no rendering rule; :func:`iso_z` formats a stamp for text and for event
payload strings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from cw._util import format_compact_age

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.models import ClientConfig, ConcurrencyOverrides, LaneConcurrencyOverride


class LanePauseSource(StrEnum):
    """Who holds a lane paused.

    ``circuit_breaker`` and ``operator`` are persisted in
    ``LaneConcurrencyOverride.pause_source``. ``legacy`` is a pause recorded
    before #2696 (``paused=True`` with no source): operator-held, never
    auto-resumed. ``declared`` is a ``paused: true`` in ``clients.yaml``.
    """

    CIRCUIT_BREAKER = "circuit_breaker"
    OPERATOR = "operator"
    LEGACY = "legacy"
    DECLARED = "declared"


class LanePauseTrigger(StrEnum):
    """Which breaker counter tripped a circuit-breaker pause."""

    SPAWN_ERRORS = "spawn_errors"
    WORKTREE_ERRORS = "worktree_errors"


# A worktree setup error (``WorktreeError``) trips the lane only at this
# multiple of ``lane_circuit_breaker_threshold`` (R3): worktree faults are
# often ticket-local, so they get more slack than a broken spawn backend.
WORKTREE_BREAKER_MULTIPLIER = 3

_TRIGGER_TEXT: dict[LanePauseTrigger | None, str] = {
    LanePauseTrigger.SPAWN_ERRORS: "spawn errors",
    LanePauseTrigger.WORKTREE_ERRORS: "worktree setup errors",
    None: "spawn errors",
}
_HELD_AUTO_RESUME_TEXT: dict[LanePauseSource, str] = {
    LanePauseSource.OPERATOR: "no (operator-held)",
    LanePauseSource.LEGACY: "no (legacy pause is operator-held)",
    LanePauseSource.DECLARED: "no (declared in clients.yaml)",
}
_AUTO_RESUME_OFF = "off (lane_circuit_auto_resume_minutes=0)"
_AUTO_RESUME_NO_SCHEDULE = "no schedule (tripped before auto-resume was enabled)"
_UNKNOWN_SINCE = "unknown"


def worktree_breaker_threshold(threshold: int) -> int:
    """The worktree-error trip point for a lane breaker threshold of *threshold*."""
    return WORKTREE_BREAKER_MULTIPLIER * threshold


def breaker_tripped(override: LaneConcurrencyOverride, *, threshold: int) -> bool:
    """Whether either breaker counter of *override* is at its trip point."""
    return (
        override.consecutive_spawn_errors >= threshold
        or override.consecutive_worktree_errors >= worktree_breaker_threshold(threshold)
    )


def utc_now_seconds() -> datetime:
    """The current UTC time truncated to whole seconds (the stamp rule)."""
    return datetime.now(UTC).replace(microsecond=0)


def iso_z(dt: datetime) -> str:
    """*dt* as ISO-8601 UTC, whole seconds, ``Z`` suffix."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LanePauseView(BaseModel):
    """Why one lane is paused and how to lift it. Output-only, never persisted."""

    model_config = ConfigDict(extra="forbid")

    client: str
    lane: str
    source: LanePauseSource
    trigger: LanePauseTrigger | None
    since: datetime | None
    trip_streak: int
    recent_errors: list[str]
    auto_resume_at: datetime | None
    auto_resume_enabled: bool
    resume_command: str


# The values ``LaneConcurrencyOverride.pause_source``/``pause_trigger`` persist.
# Looked up rather than constructed so an absent (legacy) or unknown value
# degrades to legacy / no trigger instead of raising.
_PERSISTED_SOURCES: dict[str | None, LanePauseSource] = {
    source.value: source
    for source in (LanePauseSource.CIRCUIT_BREAKER, LanePauseSource.OPERATOR)
}
_PERSISTED_TRIGGERS: dict[str | None, LanePauseTrigger] = {
    trigger.value: trigger for trigger in LanePauseTrigger
}


def build_lane_pause_view(
    client: str,
    lane: str,
    override: LaneConcurrencyOverride | None,
    *,
    auto_resume_enabled: bool,
) -> LanePauseView:
    """The pause view of *client*/*lane*; the caller guarantees it is paused.

    An override with ``paused=True`` names the source (``legacy`` when it
    records none); otherwise the pause comes from ``clients.yaml``. With
    *auto_resume_enabled* false the view never promises a resume time.
    """
    resume_command = f"cw lane resume {client} {lane}"
    if override is None or not override.paused:
        return LanePauseView(
            client=client,
            lane=lane,
            source=LanePauseSource.DECLARED,
            trigger=None,
            since=None,
            trip_streak=0,
            recent_errors=[],
            auto_resume_at=None,
            auto_resume_enabled=auto_resume_enabled,
            resume_command=resume_command,
        )
    source = _PERSISTED_SOURCES.get(override.pause_source, LanePauseSource.LEGACY)
    breaker = source == LanePauseSource.CIRCUIT_BREAKER
    return LanePauseView(
        client=client,
        lane=lane,
        source=source,
        trigger=_PERSISTED_TRIGGERS.get(override.pause_trigger) if breaker else None,
        since=override.paused_at,
        trip_streak=override.circuit_trip_streak if breaker else 0,
        recent_errors=list(override.recent_spawn_errors),
        auto_resume_at=(
            override.circuit_auto_resume_at if breaker and auto_resume_enabled else None
        ),
        auto_resume_enabled=auto_resume_enabled,
        resume_command=resume_command,
    )


def paused_lane_views(
    clients: Mapping[str, ClientConfig],
    overrides: ConcurrencyOverrides,
    *,
    auto_resume_enabled: bool,
) -> list[LanePauseView]:
    """Every paused lane, sorted by (client, lane).

    The union of every override with ``paused=True`` -- even for a client or
    lane missing from ``clients.yaml``, matching the override-driven
    ``[PAUSED]`` lane marker -- and every lane of *clients* (the effective
    clients, so an override's ``paused=False`` already lifted a declared
    pause) that is paused with no override-held pause, the ``declared`` case.
    """
    views: dict[tuple[str, str], LanePauseView] = {}
    for key, override in overrides.lanes.items():
        if override.paused is True:
            client, _, lane = key.partition("/")
            views[(client, lane)] = build_lane_pause_view(
                client, lane, override, auto_resume_enabled=auto_resume_enabled
            )
    for name, client_cfg in clients.items():
        for lane_cfg in client_cfg.effective_lanes:
            if lane_cfg.paused and (name, lane_cfg.name) not in views:
                views[(name, lane_cfg.name)] = build_lane_pause_view(
                    name,
                    lane_cfg.name,
                    overrides.lanes.get(f"{name}/{lane_cfg.name}"),
                    auto_resume_enabled=auto_resume_enabled,
                )
    return [views[key] for key in sorted(views)]


def _since_text(since: datetime | None, now: datetime) -> str:
    if since is None:
        return _UNKNOWN_SINCE
    return f"{iso_z(since)} ({format_compact_age(now - since)} ago)"


def _first_line(view: LanePauseView, now: datetime) -> str:
    if view.source == LanePauseSource.CIRCUIT_BREAKER:
        return (
            f"paused since {_since_text(view.since, now)} by circuit_breaker"
            f" ({_TRIGGER_TEXT[view.trigger]}, trip #{view.trip_streak})"
        )
    if view.source == LanePauseSource.OPERATOR:
        return f"paused since {_since_text(view.since, now)} by operator"
    if view.source == LanePauseSource.LEGACY:
        return "paused: legacy, since unknown (no pause metadata recorded)"
    return "paused: declared in clients.yaml"


def _auto_resume_text(view: LanePauseView, now: datetime) -> str:
    if view.source != LanePauseSource.CIRCUIT_BREAKER:
        return _HELD_AUTO_RESUME_TEXT[view.source]
    at = view.auto_resume_at
    if at is None:
        if view.auto_resume_enabled:
            return _AUTO_RESUME_NO_SCHEDULE
        return _AUTO_RESUME_OFF
    if now < at:
        return f"{iso_z(at)} (in {format_compact_age(at - now)})"
    return f"{iso_z(at)} (due; resumes on the next dispatch tick)"


def render_lane_pause_lines(view: LanePauseView, *, now: datetime) -> list[str]:
    """The text block for *view*: a cause line, then sub-lines indented by 2.

    Error bullets indent by 4. Callers prefix every line to nest the block.
    """
    lines = [_first_line(view, now)]
    if view.recent_errors:
        lines.append("  last spawn errors (oldest first):")
        lines.extend(f"    - {error}" for error in view.recent_errors)
    lines.append(f"  auto-resume: {_auto_resume_text(view, now)}")
    resume = view.resume_command
    if view.source == LanePauseSource.DECLARED:
        resume += " (overrides clients.yaml), or remove paused: true from clients.yaml"
    lines.append(f"  resume: {resume}")
    return lines
