"""The courier: post, verify and snapshot turns for the outbox drain (O2b, RFC 0019).

A tracker-IO turn per drain entry, through the same codex runner the legacy
turn uses. The turns are:

* verify (only on a re-check of an UNVERIFIED entry): list-only, read-only;
* post: list first, post only the missing marker parts, read back;
* snapshot x2 (``PER_SERVER``/``NONE`` only): a before and an after read of
  the ticket around the post, compared by :func:`compare_snapshots`.

A ``PER_TOOL`` (declared) server takes one post turn. A plugin app (no
``mcp_servers`` entry, so ``PER_SERVER``) takes three: its full tool surface is
not narrowed by cw, so the delta is the guard (R8, R19). Nothing is retried:
a timeout or an unparseable result is UNVERIFIED (R9).

Dispositions map one-to-one onto the outbox engine's release, hold and
unverified edges. The reason table (:func:`_disposition`) is the one place a
courier reason becomes a disposition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cw.models.capabilities import ToolNarrowing
from cw.outbox_needs_you import UnreachableCause
from cw.tracker_io._argv import (
    TrackerIoConfigError,
    TurnKind,
    build_tracker_io_argv,
    turn_tools,
)
from cw.tracker_io._dedup import bodies, expected_markers
from cw.tracker_io._prompt import (
    RESULT_SCHEMA,
    parse_turn_result,
    render_post_prompt,
    render_snapshot_prompt,
    render_verify_prompt,
)
from cw.tracker_io._snapshot_check import (
    SNAPSHOT_SCHEMA,
    IssueSnapshot,
    compare_snapshots,
    parse_snapshot,
)
from cw.tracker_io._turn import (
    _RESULT_NAME,
    _SCHEMA_NAME,
    TURN_TIMEOUT_SECONDS,
    TrackerIoLayerError,
    TrackerIoOutcome,
    _refused,
    courier_preflight,
    other_servers_for,
    transport_refusal,
    turn_scratch,
)
from cw.tracker_io._verify import (
    PostedComment,
    PublicState,
    TrackerIoReason,
    verify_turn_result,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.models import ClientConfig
    from cw.tracker_io._dedup import CourierPart
    from cw.tracker_io._prompt import TurnResult
    from cw.tracker_io._turn import CourierTarget

_log = logging.getLogger(__name__)

_UNREACHABLE_REASONS: frozenset[TrackerIoReason] = frozenset(
    {
        TrackerIoReason.MCP_SERVER_UNNAMED,
        TrackerIoReason.TRACKER_UNSUPPORTED,
        TrackerIoReason.TRACKER_MCP_NOT_ENABLED,
        TrackerIoReason.CODEX_UNAVAILABLE,
    }
)
_FAILED_REASONS: frozenset[TrackerIoReason] = frozenset(
    {TrackerIoReason.PLAN_REFUSED, TrackerIoReason.SCRATCH_UNWRITABLE}
)


class CourierDisposition(StrEnum):
    """What the drain does with the entry after the courier's turns."""

    DELIVERED = "delivered"
    UNVERIFIED = "unverified"
    UNREACHABLE = "unreachable"
    FAILED = "failed"


@dataclass(frozen=True)
class CourierContext:
    """The runner and client the courier drives."""

    runner: CodexRunner
    client: ClientConfig


@dataclass(frozen=True)
class CourierResult:
    """The courier's verdict for one entry.

    *layers_unreadable* lists the config layers that made a pre-launch refusal
    (R22), so the engine can name the file in its gate record (R25). *turns* is
    the number of launched turns; a pre-launch refusal costs none.
    """

    disposition: CourierDisposition
    reason: TrackerIoReason | None
    already_present: bool = False
    posted: tuple[PostedComment, ...] = ()
    turns: int = 0
    layers_unreadable: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Turn:
    """One turn's launch: the runner result, or the refusal that stopped it."""

    run: CodexRunResult | None
    refusal: TrackerIoOutcome | None
    layers_unreadable: tuple[str, ...] = ()

    @property
    def launched(self) -> int:
        """1 when the runner ran (a counted turn), else 0."""
        return 0 if self.run is None else 1


@dataclass(frozen=True)
class _SnapshotRead:
    """A snapshot turn's read: the parsed snapshot, or why it could not be used."""

    snapshot: IssueSnapshot | None
    refusal: TrackerIoOutcome | None
    turn: _Turn


def _disposition(outcome: TrackerIoOutcome) -> CourierDisposition:
    """The one reason-to-disposition table (O2b, pinned by the courier tests)."""
    reason = outcome.reason
    if reason in _UNREACHABLE_REASONS:
        return CourierDisposition.UNREACHABLE
    if reason is TrackerIoReason.TOOL_UNAVAILABLE:
        if outcome.public_state is PublicState.NONE:
            return CourierDisposition.UNREACHABLE
        return CourierDisposition.UNVERIFIED
    if reason in _FAILED_REASONS:
        return CourierDisposition.FAILED
    if outcome.public_state is PublicState.NONE:
        return CourierDisposition.FAILED
    return CourierDisposition.UNVERIFIED


def courier_cause(result: CourierResult) -> UnreachableCause:
    """The gate-record cause for an UNREACHABLE result (O2b, R25).

    The one reason-to-cause table. An unreadable config layer is named by its
    own cause (the record lists the files); a missing realization is G1's.
    """
    reason = result.reason
    if reason is TrackerIoReason.TOOL_UNAVAILABLE:
        return UnreachableCause.TOOL_UNAUTHENTICATED
    if reason is TrackerIoReason.MCP_SERVER_UNNAMED:
        if result.layers_unreadable:
            return UnreachableCause.CONFIG_UNREADABLE
        return UnreachableCause.NO_REALIZATION
    if reason is TrackerIoReason.TRACKER_MCP_NOT_ENABLED:
        return UnreachableCause.MCP_NOT_ENABLED
    if reason is TrackerIoReason.TRACKER_UNSUPPORTED:
        return UnreachableCause.TRACKER_UNSUPPORTED
    return UnreachableCause.CODEX_UNAVAILABLE


def courier_mismatch(result: CourierResult) -> bool:
    """True when an UNVERIFIED result is a read-back or snapshot-delta mismatch."""
    return result.reason is TrackerIoReason.READBACK_MISMATCH


def _settle(
    outcome: TrackerIoOutcome,
    *,
    turns: int,
    already_present: bool = False,
    layers: tuple[str, ...] = (),
) -> CourierResult:
    if outcome.verified:
        return CourierResult(
            CourierDisposition.DELIVERED,
            None,
            already_present=already_present,
            posted=outcome.posted,
            turns=turns,
            layers_unreadable=layers,
        )
    return CourierResult(
        _disposition(outcome),
        outcome.reason,
        posted=outcome.posted,
        turns=turns,
        layers_unreadable=layers,
    )


def _run_turn(
    context: CourierContext,
    target: CourierTarget,
    *,
    schema: object,
    prompt: str,
    allowlist: tuple[str, ...],
    write_capable: bool,
    parts_total: int,
) -> _Turn:
    """Launch one courier turn in a fresh scratch directory (the only run site).

    A refusal before the runner is called (unreadable layer, bad argv, scratch)
    is returned as ``refusal`` and is not counted as a turn.
    """
    try:
        with turn_scratch(schema) as scratch:
            try:
                argv = build_tracker_io_argv(
                    server=target.server,
                    tools=allowlist,
                    schema_path=scratch / _SCHEMA_NAME,
                    output_path=scratch / _RESULT_NAME,
                    other_servers=other_servers_for(
                        scratch, write_capable=write_capable
                    ),
                    narrowing=target.narrowing,
                )
            except TrackerIoLayerError as exc:
                return _Turn(None, _refused(exc.reason, parts_total), exc.paths)
            except TrackerIoConfigError as exc:
                return _Turn(None, _refused(exc.reason, parts_total))
            try:
                run = context.runner.run(
                    scratch, argv, TURN_TIMEOUT_SECONDS, stdin=prompt
                )
            except OSError:
                _log.warning("tracker_io_codex_unavailable", exc_info=True)
                return _Turn(
                    None, _refused(TrackerIoReason.CODEX_UNAVAILABLE, parts_total)
                )
            return _Turn(run, None)
    except TrackerIoConfigError as exc:
        return _Turn(None, _refused(exc.reason, parts_total))


def _judge_result(
    run: CodexRunResult, ticket_id: str, emitted: Sequence[str]
) -> tuple[TrackerIoOutcome, TurnResult | None]:
    """Transport check, then the read-back verification (no relaxed check)."""
    refusal = transport_refusal(run, len(emitted))
    if refusal is not None:
        return refusal, None
    result = parse_turn_result(run.output_file_content)
    verified = verify_turn_result(ticket_id, emitted, result)
    outcome = TrackerIoOutcome(
        verified.public_state,
        verified.reason,
        len(emitted),
        verified.posted,
        verified.marker_survived,
        emitted=tuple(emitted),
    )
    return outcome, result


def _judge_verify(
    run: CodexRunResult, ticket_id: str, emitted: Sequence[str]
) -> tuple[TrackerIoOutcome | None, bool]:
    """Judge a verify turn. ``None`` outcome means not every marker is on the ticket.

    The bool is true when the verify reported ``already_present`` (all found).
    """
    refusal = transport_refusal(run, len(emitted))
    if refusal is not None:
        return refusal, False
    result = parse_turn_result(run.output_file_content)
    if result is not None and result.status == "ok":
        return None, False
    outcome, _ = _judge_result(run, ticket_id, emitted)
    return outcome, result is not None and result.status == "already_present"


def _read_snapshot(
    context: CourierContext,
    target: CourierTarget,
    ticket_id: str,
    markers: Sequence[str],
) -> _SnapshotRead:
    """One snapshot turn (read-only): the parsed snapshot or the refusal."""
    total = len(markers)
    turn = _run_turn(
        context,
        target,
        schema=SNAPSHOT_SCHEMA,
        prompt=render_snapshot_prompt(
            ticket_id,
            markers,
            server=target.server,
            tools=(target.get_tool, target.tools[1]),
            app=target.narrowing is not ToolNarrowing.PER_TOOL,
        ),
        allowlist=(target.get_tool, target.tools[1]),
        write_capable=True,
        parts_total=total,
    )
    if turn.run is None:
        return _SnapshotRead(None, turn.refusal, turn)
    refusal = transport_refusal(turn.run, total)
    if refusal is not None:
        return _SnapshotRead(None, refusal, turn)
    snapshot = parse_snapshot(turn.run.output_file_content)
    if snapshot is None:
        return _SnapshotRead(
            None, _refused(TrackerIoReason.RESULT_UNPARSEABLE, total), turn
        )
    if snapshot.status == "tool_unavailable":
        return _SnapshotRead(
            None, _refused(TrackerIoReason.TOOL_UNAVAILABLE, total), turn
        )
    if snapshot.status != "ok":
        return _SnapshotRead(
            None, _refused(TrackerIoReason.TURN_REPORTED_FAILURE, total), turn
        )
    return _SnapshotRead(snapshot, None, turn)


def _post_turn(
    context: CourierContext,
    target: CourierTarget,
    ticket_id: str,
    emitted: Sequence[str],
    markers: Sequence[str],
) -> _Turn:
    return _run_turn(
        context,
        target,
        schema=RESULT_SCHEMA,
        prompt=render_post_prompt(
            ticket_id,
            emitted,
            markers,
            server=target.server,
            tools=target.tools,
            app=target.narrowing is not ToolNarrowing.PER_TOOL,
        ),
        allowlist=turn_tools(TurnKind.POST, target.tools),
        write_capable=True,
        parts_total=len(emitted),
    )


def _post_once(
    context: CourierContext,
    target: CourierTarget,
    ticket_id: str,
    emitted: Sequence[str],
    markers: Sequence[str],
    *,
    turns: int,
) -> CourierResult:
    """A declared server: one post turn, verified by its read-back.

    *turns* counts the turns already launched this entry (a recheck verify).
    """
    turn = _post_turn(context, target, ticket_id, emitted, markers)
    turns += turn.launched
    if turn.run is None:
        return _settle(
            turn.refusal or _refused(TrackerIoReason.PLAN_REFUSED),
            turns=turns,
            layers=turn.layers_unreadable,
        )
    outcome, result = _judge_result(turn.run, ticket_id, emitted)
    return _settle(
        outcome,
        turns=turns,
        already_present=result is not None and result.status == "already_present",
    )


def _post_with_delta(
    context: CourierContext,
    target: CourierTarget,
    ticket_id: str,
    parts: Sequence[CourierPart],
    emitted: Sequence[str],
    markers: Sequence[str],
    *,
    turns: int,
) -> CourierResult:
    """A plugin app: before snapshot, post, after snapshot, then the delta.

    *turns* counts the turns already launched this entry (a recheck verify).
    """
    before = _read_snapshot(context, target, ticket_id, markers)
    turns += before.turn.launched
    if before.snapshot is None:
        return _settle(
            before.refusal or _refused(TrackerIoReason.PLAN_REFUSED),
            turns=turns,
            layers=before.turn.layers_unreadable,
        )
    expected = expected_markers(parts, before.snapshot.markers)
    post = _post_turn(context, target, ticket_id, emitted, markers)
    turns += post.launched
    if post.run is None:
        return _settle(
            post.refusal or _refused(TrackerIoReason.PLAN_REFUSED),
            turns=turns,
            layers=post.layers_unreadable,
        )
    outcome, result = _judge_result(post.run, ticket_id, emitted)
    if not outcome.verified:
        return _settle(outcome, turns=turns)
    after = _read_snapshot(context, target, ticket_id, markers)
    turns += after.turn.launched
    if after.snapshot is None:
        # The post already landed, so this is UNVERIFIED whatever the reason.
        after_reason = (
            after.refusal.reason
            if after.refusal is not None
            else TrackerIoReason.RESULT_UNPARSEABLE
        )
        return CourierResult(
            CourierDisposition.UNVERIFIED,
            after_reason,
            posted=outcome.posted,
            turns=turns,
        )
    delta = compare_snapshots(
        before.snapshot,
        after.snapshot,
        ticket_id=ticket_id,
        expected_markers=expected,
    )
    if delta is not None:
        _log.warning(
            "tracker_io_snapshot_delta_rejected ticket=%s: %s", ticket_id, delta
        )
        return CourierResult(
            CourierDisposition.UNVERIFIED,
            TrackerIoReason.READBACK_MISMATCH,
            posted=outcome.posted,
            turns=turns,
        )
    return CourierResult(
        CourierDisposition.DELIVERED,
        None,
        already_present=result is not None and result.status == "already_present",
        posted=outcome.posted,
        turns=turns,
    )


def courier_drain(
    ticket_id: str,
    parts: Sequence[CourierPart],
    *,
    operator_authored: bool,
    recheck: bool,
    context: CourierContext,
) -> CourierResult:
    """Drive the courier turns for one entry and return its verdict (O2b).

    *recheck* runs a read-only verify turn first (an UNVERIFIED entry whose
    earlier post may have landed). *operator_authored* selects the provenance
    rule (R21). Never retries.
    """
    emitted = bodies(parts, operator_authored=operator_authored)
    markers = tuple(part.marker_line for part in parts)
    tickets = context.client.bindings.tickets
    tracker = str(tickets.system) if tickets is not None else ""
    target = courier_preflight(ticket_id, tracker, context.client)
    if isinstance(target, TrackerIoReason):
        return _settle(_refused(target, len(parts)), turns=0)
    turns = 0
    if recheck:
        verify = _run_turn(
            context,
            target,
            schema=RESULT_SCHEMA,
            prompt=render_verify_prompt(
                ticket_id,
                markers,
                server=target.server,
                tools=target.tools,
                app=target.narrowing is not ToolNarrowing.PER_TOOL,
            ),
            allowlist=turn_tools(TurnKind.VERIFY, target.tools),
            write_capable=target.narrowing is not ToolNarrowing.PER_TOOL,
            parts_total=len(parts),
        )
        turns += verify.launched
        if verify.run is None:
            return _settle(
                verify.refusal or _refused(TrackerIoReason.PLAN_REFUSED),
                turns=turns,
                layers=verify.layers_unreadable,
            )
        found, already = _judge_verify(verify.run, ticket_id, emitted)
        if found is not None:
            return _settle(found, turns=turns, already_present=already)
    if target.narrowing is ToolNarrowing.PER_TOOL:
        return _post_once(context, target, ticket_id, emitted, markers, turns=turns)
    return _post_with_delta(
        context, target, ticket_id, parts, emitted, markers, turns=turns
    )
