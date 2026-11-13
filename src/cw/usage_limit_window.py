"""Inspect and clear per-client usage-limit back-off windows (#3547).

The inspect half (:func:`usage_limit_window_views`) feeds ``cw dev-queue
status`` and ``cw doctor``. It reads through the dispatch loop's own unlocked
reader, ``load_usage_limited_until``, so there is one parse of the sidecar.

The clear half backs ``cw dev-queue usage-limit clear``. The plan
(:func:`plan_usage_limit_clear`) is built from unlocked reads, before the
caller resolves the actor and runs the account probe. The apply
(:func:`apply_usage_limit_clear`) then takes ``dispatch_state_lock`` (window pop
and clear mark, in ``clear_usage_limited_until``) and, as a separate later
acquisition, ``dev_queue_lock`` (row stamps). Both are rank STATE and are never
nested. The ``dispatch.usage_limit_cleared`` audit event is recorded after both
are released: state first, then the event.

``next_eligible_at`` is shared by the usage-limit requeue, the spawn-error
backoff and the worktree-occupied deferral, and a row does not record which
one wrote it. So every future stamp on the client's PENDING rows is released,
except a gate-block wait (``gate_block.until == next_eligible_at``), which is
the gate's own. ``spawn_error_count`` and ``gate_block`` are never touched.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dev_queue import dev_queue_lock, list_tickets, load_dev_queue, save_dev_queue
from cw.dispatch_state import load_usage_limit_armed_at, load_usage_limited_until
from cw.dispatch_state_marks import clear_usage_limited_until
from cw.events import _inbox_lock, inbox_path, read_events, record_event
from cw.exceptions import CwError
from cw.lane_pause import iso_z
from cw.models import OrchestratorEvent, OrchestratorEventType, QueueItemStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from cw.models import TicketTask

logger = logging.getLogger(__name__)

OPERATOR_TRIGGER = "operator"
_NONE = "none"
USAGE_LIMIT_HEADER = "Usage-limit windows:"


def display_instant(dt: datetime | None) -> str:
    """*dt* as ``iso_z``, or ``none``: the form every operator-facing line uses."""
    return _NONE if dt is None else iso_z(dt)


@dataclass(frozen=True)
class UsageLimitWindowView:
    """One active usage-limit window, for ``status`` and ``doctor``.

    ``armed_at`` is the FLEET-LEVEL last arm time (#2276), not a per-client
    one. ``source`` is the provenance of the latest ``USAGE_LIMIT_ARMED`` event
    whose ``client`` and ``until`` match, or None when none does.
    """

    client: str
    until: datetime
    armed_at: datetime | None
    source: str | None

    def to_json(self) -> dict[str, object]:
        return {
            "client": self.client,
            "until": iso_z(self.until),
            "armed_at": None if self.armed_at is None else iso_z(self.armed_at),
            "source": self.source,
        }


def _armed_sources() -> dict[tuple[str, str], str]:
    """``(client, until-iso) -> source`` from the arm events, latest wins."""
    sources: dict[tuple[str, str], str] = {}
    for event in read_events(event_types=[OrchestratorEventType.USAGE_LIMIT_ARMED]):
        client = event.payload.get("client")
        until = event.payload.get("until")
        source = event.payload.get("source")
        if (
            isinstance(client, str)
            and isinstance(until, str)
            and isinstance(source, str)
        ):
            sources[(client, until)] = source
    return sources


def usage_limit_window_views(client: str | None = None) -> list[UsageLimitWindowView]:
    """The active windows (optionally one client's), sorted by client."""
    windows = load_usage_limited_until()
    names = sorted(name for name in windows if client is None or name == client)
    if not names:
        return []
    armed_at = load_usage_limit_armed_at()
    sources = _armed_sources()
    return [
        UsageLimitWindowView(
            client=name,
            until=windows[name],
            armed_at=armed_at,
            source=sources.get((name, windows[name].isoformat())),
        )
        for name in names
    ]


def render_usage_limit_lines(views: Sequence[UsageLimitWindowView]) -> list[str]:
    """The ``status`` text block: a header and one line per window, or nothing."""
    if not views:
        return []
    return [
        USAGE_LIMIT_HEADER,
        *(
            f"  {v.client}: until {iso_z(v.until)} (armed-at"
            f" {display_instant(v.armed_at)}, fleet-level; source"
            f" {v.source or _NONE})"
            for v in views
        ),
    ]


@dataclass(frozen=True)
class StampedRow:
    """A PENDING row whose wait the clear would release, and its new stamp."""

    ticket_id: str
    stamp: datetime
    restore_to: datetime | None


@dataclass(frozen=True)
class ClearPlan:
    """What a clear of *client* would change, from unlocked reads."""

    client: str
    window_until: datetime | None
    rows: tuple[StampedRow, ...]

    @property
    def is_empty(self) -> bool:
        return self.window_until is None and not self.rows


@dataclass(frozen=True)
class ClearResult:
    """What a clear changed: the removed deadline and the released rows."""

    client: str
    old_until: datetime | None
    rows_released: tuple[str, ...]


class UsageLimitClearAuditError(CwError):
    """The clear landed but its audit event could not be recorded.

    Carries the :class:`ClearResult`, so the CLI can print what was already
    cleared before it exits non-zero.
    """

    __slots__ = ("result",)

    def __init__(self, message: str, *, result: ClearResult) -> None:
        super().__init__(message)
        self.result = result


def _gate_owned(task: TicketTask) -> bool:
    record = task.gate_block
    return record is not None and task.next_eligible_at == record.until


def _releasable(task: TicketTask, client: str, now: datetime) -> bool:
    """A PENDING row of *client* holding a future, non-gate wait."""
    stamp = task.next_eligible_at
    return (
        task.client == client
        and task.status is QueueItemStatus.PENDING
        and stamp is not None
        and stamp > now
        and not _gate_owned(task)
    )


def _restore_target(task: TicketTask, now: datetime) -> datetime | None:
    """A gate wait layered under the released one is put back; else None."""
    record = task.gate_block
    if record is not None and record.until > now:
        return record.until
    return None


def _stamped_rows(
    tasks: Iterable[TicketTask], client: str, now: datetime
) -> tuple[StampedRow, ...]:
    return tuple(
        StampedRow(task.ticket_id, task.next_eligible_at, _restore_target(task, now))
        for task in tasks
        if _releasable(task, client, now) and task.next_eligible_at is not None
    )


def plan_usage_limit_clear(client: str, *, now: datetime | None = None) -> ClearPlan:
    """The window and the waiting rows a clear of *client* would release."""
    at = now or datetime.now(UTC)
    return ClearPlan(
        client=client,
        window_until=load_usage_limited_until().get(client),
        rows=_stamped_rows(list_tickets(client), client, at),
    )


def _clear_window(
    client: str, cleared_at: datetime, *, expected_until: datetime | None
) -> datetime | None:
    try:
        return clear_usage_limited_until(
            client, cleared_at=cleared_at, expected_until=expected_until
        )
    except OSError as exc:
        msg = f"could not clear the usage-limit window for {client}: {exc}"
        raise CwError(msg) from exc


def _release_rows(plan: ClearPlan, now: datetime) -> tuple[list[str], Exception | None]:
    """Release the planned rows that still qualify, under ``dev_queue_lock``.

    Re-checked under the lock, so a row that changed since the plan is left
    alone. Returns the released ticket ids (none when the save failed) and the
    save error, if any.
    """
    if not plan.rows:
        return [], None
    wanted = {row.ticket_id for row in plan.rows}
    released: list[str] = []
    try:
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if task.ticket_id in wanted and _releasable(task, plan.client, now):
                    task.next_eligible_at = _restore_target(task, now)
                    released.append(task.ticket_id)
            if released:
                save_dev_queue(store)
    except Exception as exc:  # noqa: BLE001 — queue load/validation must not lose the audit event.
        return [], exc
    return released, None


def _event_payload(
    result: ClearResult,
    *,
    actor: str,
    forced: bool,
    probe_verdict: str,
    cleared_at: datetime,
) -> dict[str, object]:
    armed_at = load_usage_limit_armed_at()
    return {
        "clients_affected": [result.client],
        "sessions_affected": 0,
        "detected_at": None if armed_at is None else armed_at.isoformat(),
        "cleared_at": cleared_at.isoformat(),
        "trigger": OPERATOR_TRIGGER,
        "cleared_by": actor,
        "old_until": None if result.old_until is None else result.old_until.isoformat(),
        "rows_released": list(result.rows_released),
        "forced": forced,
        "probe": probe_verdict,
    }


def _append_audit_fallback(payload: dict[str, object]) -> None:
    """Append the audit event directly if the normal writer fails.

    The clear has already committed its state changes by this point. The
    fallback uses the same append-only inbox, its own lock, and an fsync so a
    transient failure in the normal call cannot leave an un-audited clear.
    """
    event = OrchestratorEvent(
        type=OrchestratorEventType.USAGE_LIMIT_CLEARED,
        payload=payload,
    )
    path = inbox_path()
    with _inbox_lock():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(event.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _record_cleared(result: ClearResult, payload: dict[str, object]) -> None:
    """Record the audit event; on failure log it and raise the audit error."""
    try:
        record_event(OrchestratorEventType.USAGE_LIMIT_CLEARED, payload)
    except Exception as exc:  # noqa: BLE001 — every event-writer failure gets a durable fallback.
        error = exc
        fallback_error: Exception | None = None
        try:
            _append_audit_fallback(payload)
        except Exception as fallback_exc:  # noqa: BLE001 — surface fallback failure too.
            fallback_error = fallback_exc
    else:
        return
    event_name = OrchestratorEventType.USAGE_LIMIT_CLEARED.value
    logger.error(
        "usage-limit clear for %s landed (was until %s; released %s) but the "
        "%s audit event could not be recorded: %s",
        result.client,
        payload["old_until"],
        list(result.rows_released),
        event_name,
        error if fallback_error is None else f"{error}; fallback: {fallback_error}",
    )
    msg = (
        f"usage-limit window for {result.client} was cleared (was until"
        f" {display_instant(result.old_until)}; released"
        f" {len(result.rows_released)} row(s)) but the {event_name} audit event"
        f" could not be recorded: {error}"
    )
    if fallback_error is not None:
        msg += f"; durable fallback audit also failed: {fallback_error}"
    raise UsageLimitClearAuditError(msg, result=result) from error


def apply_usage_limit_clear(
    plan: ClearPlan,
    *,
    actor: str,
    forced: bool,
    probe_verdict: str,
    now: datetime | None = None,
) -> ClearResult:
    """Clear *plan*'s window and rows, then record one audit event.

    Order: the window pop and clear mark (``dispatch_state_lock``), then the
    row release (``dev_queue_lock``), then the event, outside both locks. If
    the row save fails after the window was cleared, the event is still
    recorded (with the rows actually released, none) before the ``CwError``,
    so ``old_until`` and the actor are never lost. If the event write fails,
    :class:`UsageLimitClearAuditError` is raised. Never touches
    ``usage_limit_armed_at``.
    """
    cleared_at = now or datetime.now(UTC)
    old_until = _clear_window(plan.client, cleared_at, expected_until=plan.window_until)
    released, row_error = _release_rows(plan, cleared_at)
    result = ClearResult(plan.client, old_until, tuple(released))
    payload = _event_payload(
        result,
        actor=actor,
        forced=forced,
        probe_verdict=probe_verdict,
        cleared_at=cleared_at,
    )
    _record_cleared(result, payload)
    if row_error is not None:
        msg = (
            f"usage-limit window for {plan.client} was cleared (was until"
            f" {display_instant(old_until)}) but releasing its waiting rows"
            f" failed: {row_error}; re-run the clear to release them"
        )
        raise CwError(msg) from row_error
    return result
