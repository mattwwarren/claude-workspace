"""The outbox drain (#3706, RFC 0019 O2; MCP courier O2b, ADR-0023).

``run_drain`` posts a client's ``pending`` and ``unverified`` outbox entries to
its ``bindings.tickets`` tracker. A ``cli: gh`` binding posts through
:func:`cw.gh.post_issue_comment` (via :func:`cw.tracker_post.post_parts` for
agent-authored entries) and :func:`cw.gh.fetch_issue_comments`; it dedups per
part on a ``<!-- cw-outbox:<marker>:<i>/<n> -->`` line found, as a whole line,
in one comment listing taken at the start of each entry. A binding with a codex
MCP realization posts through the courier (:mod:`cw.tracker_io._drain`), one
agent turn per entry, verified by read-back or a before/after snapshot delta.

A sibling of the :mod:`cw.outbox` package, not inside it: the package imports
no subprocess and no tracker code (pinned by ``tests/test_outbox.py``). This
module imports only the courier entry point (``cw.tracker_io._drain``), the
codex runner and the gate filer (``cw.outbox_needs_you``); it never imports
``cw.codex_review*``.

The run holds only the unguarded per-client drain lease across ``gh`` calls;
store calls take the leaf ``outbox`` lock for each call only. The claimant is
``current_launch_marker()``, read inside ``claim``: outside the initial pid
namespace it is ``None`` and such a claim is never auto-recovered.

Decision table (plan P3): only binding-level evidence (no usable binding, or an
unreadable comment listing) sets the per-role unreachable marker and breaks
that role for the rest of the run (later entries of it are handled without
``gh``). Every other failure is about one entry: a pending entry with nothing
public is held (released ``transient_failure`` and added to the held list,
retried only by ``cw outbox drain --force``), and anything that may be public
settles ``unverified``. Nothing here uses a timer (ADR-0014).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from cw.codex_runner import RealCodexRunner
from cw.events import record_event
from cw.exceptions import CwError
from cw.gh import FETCH_COMMENTS_TIMEOUT, fetch_issue_comments, post_issue_comment
from cw.models import CODEX_BACKEND, OrchestratorEventType
from cw.outbox import (
    DrainLeaseHeldError,
    OutboxEntry,
    OutboxError,
    OutboxReason,
    OutboxRecordError,
    OutboxRole,
    OutboxState,
    UnreachableMarker,
    append_held,
    claim,
    clear_all_markers,
    clear_held,
    clear_marker,
    drain_lease,
    list_entries,
    marked_roles,
    read_held,
    read_marker,
    read_parts,
    release,
    settle,
    write_marker,
)
from cw.outbox_needs_you import (
    UnreachableCause,
    build_gate_payload,
    file_gate_record,
)
from cw.tracker_io._dedup import CourierPart
from cw.tracker_io._drain import (
    CourierContext,
    CourierDisposition,
    courier_cause,
    courier_drain,
    courier_mismatch,
)
from cw.tracker_post import (
    PostedPart,
    PostReason,
    PostResult,
    PostStatus,
    find_comment_body,
    parse_comment_url,
    post_parts,
    readback_matches,
)
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.codex_runner import CodexRunner
    from cw.models import ClientConfig

logger = logging.getLogger(__name__)

DRAIN_ENVELOPE_SCHEMA_VERSION: Literal[1] = 1

_NON_TERMINAL = frozenset(
    {OutboxState.PENDING, OutboxState.CLAIMED, OutboxState.UNVERIFIED}
)


class DrainReport(BaseModel):
    """One client's ``cw outbox drain`` outcome (plan P12)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client: str
    drained: int
    delivered: int
    unverified: int
    skipped: int
    released: int
    held: int
    lease_held: bool
    unreachable_roles: list[str]


class DrainEnvelope(BaseModel):
    """The ``cw outbox drain --json`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = DRAIN_ENVELOPE_SCHEMA_VERSION
    clients: list[DrainReport]
    unconfigured: list[str]


@dataclass(frozen=True)
class GhTarget:
    """Where a client's tickets posts go: ``gh`` run in *cwd*."""

    cwd: Path


@dataclass(frozen=True)
class McpTarget:
    """A tickets binding with a codex MCP realization: the courier posts (O2b)."""

    server: str


def select_target(cfg: ClientConfig) -> GhTarget | McpTarget | None:
    """The target of *cfg*'s tickets binding, or ``None`` if unusable.

    A ``cli`` realization is ``gh`` (the binding model admits only ``gh`` there).
    Otherwise a codex MCP realization is the courier's target. ``None`` when the
    binding is absent or has neither (no codex realization, O2b G1).
    """
    tickets = cfg.bindings.tickets
    if tickets is None:
        return None
    if tickets.cli is not None:
        return GhTarget(cwd=_git_dir(cfg))
    server = tickets.mcp_server(CODEX_BACKEND)
    if server is None:
        return None
    return McpTarget(server=server)


def part_marker_line(marker: str, index: int, total: int) -> str:
    """The per-part dedup line ``<!-- cw-outbox:<marker>:<i>/<n> -->`` (1-based)."""
    return f"<!-- cw-outbox:{marker}:{index}/{total} -->"


@dataclass
class _Run:
    """One client's drain run: counters, the seen set and the broken roles."""

    client: str
    cfg: ClientConfig
    target: GhTarget | McpTarget | None
    seen: set[str]
    runner: CodexRunner
    broken: dict[OutboxRole, UnreachableMarker] = field(default_factory=dict)
    delivered_roles: set[OutboxRole] = field(default_factory=set)
    delivered: int = 0
    unverified: int = 0
    released: int = 0
    held: int = 0
    claims: int = 0
    accounted: set[str] = field(default_factory=set)
    storage_error: OutboxError | None = None
    marker_write_failures: set[OutboxRole] = field(default_factory=set)


def _accounted(run: _Run, entry: OutboxEntry) -> bool:
    """Mark a committed entry transition, returning whether it was new."""
    if entry.id in run.accounted:
        return False
    run.accounted.add(entry.id)
    return True


def _emit(
    event_type: OrchestratorEventType,
    entry: OutboxEntry,
    reason: OutboxReason,
    *,
    posted: Sequence[PostedPart] = (),
    held: bool = False,
    turns: int = 0,
) -> None:
    """Record one outbox event, best-effort: a failure is one WARNING.

    *turns* is the courier turn count. The key is present only when a courier
    turn ran (R20): never on a ``gh`` drain, a skipped event or a pre-launch
    release.
    """
    payload: dict[str, object] = {
        "client": entry.client,
        "ticket": entry.ticket,
        "role": entry.role.value,
        "marker": entry.marker,
        "producer": entry.producer,
        "entry_id": entry.id,
        "reason": reason.value,
        "posted": [{"index": p.index, "url": p.url} for p in posted],
        "held": held,
    }
    if turns > 0:
        payload["turns"] = turns
    try:
        record_event(event_type, payload, correlation_id=entry.ticket)
    except (OSError, CwError) as exc:
        logger.warning(
            "outbox: could not record %s for entry %s (%s)",
            event_type.value,
            entry.id,
            type(exc).__name__,
        )


def _token(entry: OutboxEntry) -> str:
    # A claimed entry always carries its token (the model enforces it).
    return entry.claim_token or ""


def _deliver(
    run: _Run,
    entry: OutboxEntry,
    reason: OutboxReason,
    posted: Sequence[PostedPart] = (),
    *,
    turns: int = 0,
) -> None:
    settle(
        run.client,
        entry.id,
        claim_token=_token(entry),
        state=OutboxState.DELIVERED,
        reason=reason,
    )
    if _accounted(run, entry):
        run.delivered += 1
        run.delivered_roles.add(entry.role)
        _emit(
            OrchestratorEventType.OUTBOX_ENTRY_DELIVERED,
            entry,
            reason,
            posted=posted,
            turns=turns,
        )


def _resettle_unverified(
    run: _Run,
    entry: OutboxEntry,
    reason: OutboxReason = OutboxReason.READBACK_UNVERIFIED,
) -> None:
    """Settle *entry* ``unverified`` and count it; announcing is the caller's."""
    settle(
        run.client,
        entry.id,
        claim_token=_token(entry),
        state=OutboxState.UNVERIFIED,
        reason=reason,
    )
    if _accounted(run, entry):
        run.unverified += 1


def _to_unverified(
    run: _Run,
    entry: OutboxEntry,
    reason: OutboxReason = OutboxReason.READBACK_UNVERIFIED,
    *,
    posted: Sequence[PostedPart] = (),
    turns: int = 0,
) -> None:
    _resettle_unverified(run, entry, reason)
    _emit(
        OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED,
        entry,
        reason,
        posted=posted,
        turns=turns,
    )


def _hold(run: _Run, entry: OutboxEntry, *, turns: int = 0) -> None:
    """Release a per-item failure ``transient_failure`` and hold it until --force."""
    release(
        run.client,
        entry.id,
        claim_token=_token(entry),
        reason=OutboxReason.TRANSIENT_FAILURE,
    )
    try:
        append_held(run.client, entry.id)
    except OutboxError as exc:
        # The record transition committed, but the durable retry suppression
        # did not.  Reconciliation accounts for the committed release while
        # the retained error makes the run fail visibly.
        run.storage_error = run.storage_error or exc
        raise
    counted = _accounted(run, entry)
    if counted:
        run.released += 1
        run.held += 1
        _emit(
            OrchestratorEventType.OUTBOX_ENTRY_PENDING,
            entry,
            OutboxReason.TRANSIENT_FAILURE,
            held=True,
            turns=turns,
        )


def _per_item_failure(run: _Run, entry: OutboxEntry, *, turns: int = 0) -> None:
    """Rows G2/P2/P4: hold a pending entry; an unverified one stays unverified."""
    if entry.claimed_from is OutboxState.PENDING:
        _hold(run, entry, turns=turns)
    else:
        _to_unverified(run, entry, turns=turns)


def _persist_marker(
    run: _Run,
    entry: OutboxEntry,
    marker: UnreachableMarker,
    *,
    persist: bool,
) -> None:
    """Persist a marker, retaining the error if its sidecar cannot be written."""
    if not (persist or entry.role in run.marker_write_failures):
        return
    try:
        write_marker(run.client, marker)
    except OutboxError as exc:
        # The entry transition is already committed.  Retain the storage
        # error and let the run fail visibly; the entry is not a per-item
        # failure and must not be added to the held list.
        run.storage_error = run.storage_error or exc
        run.marker_write_failures.add(entry.role)
        logger.warning(
            "outbox: could not persist unreachable marker for %s (%s)",
            entry.role.value,
            type(exc).__name__,
        )
        return
    run.marker_write_failures.discard(entry.role)


def _announce(
    run: _Run,
    entry: OutboxEntry,
    marker: UnreachableMarker,
    *,
    persist: bool = False,
    turns: int = 0,
) -> None:
    """Return *entry* to a legal state with no ``gh`` call, under *marker*.

    Pending releases with the marker's reason; unverified re-settles. Crash
    consistency (plan P5, item 31): the entry's state change is persisted
    first, then the marker record (always when *persist*, else only when this
    entry joins the episode), then the event, once per entry per episode
    (``announced``).
    """
    first = entry.id not in marker.announced
    if first:
        marker = marker.model_copy(update={"announced": [*marker.announced, entry.id]})
    is_pending = entry.claimed_from is OutboxState.PENDING
    if is_pending:
        release(run.client, entry.id, claim_token=_token(entry), reason=marker.reason)
    else:
        _resettle_unverified(run, entry)
        counted = entry.id in run.accounted
    run.broken[entry.role] = marker
    _persist_marker(run, entry, marker, persist=persist or first)
    if is_pending:
        counted = _accounted(run, entry)
        if counted:
            run.released += 1
    if not first or not counted:
        return
    if is_pending:
        _emit(
            OrchestratorEventType.OUTBOX_ENTRY_PENDING,
            entry,
            marker.reason,
            held=False,
            turns=turns,
        )
    else:
        _emit(
            OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED,
            entry,
            OutboxReason.READBACK_UNVERIFIED,
            turns=turns,
        )


def _binding_fault(
    run: _Run,
    entry: OutboxEntry,
    reason: OutboxReason,
    *,
    turns: int = 0,
    cause: UnreachableCause | None = None,
    detail: str = "",
) -> None:
    """Rows G1/L1: set the role's marker (keeping its episode) and break the role.

    A *cause* files the episode's gate record once (O2b, R25). The gh listing
    fault passes none and never files.
    """
    existing = read_marker(run.client, entry.role)
    announced = [] if existing is None else existing.announced
    gate_filed = existing is not None and existing.gate_filed
    marker = UnreachableMarker(
        role=entry.role, reason=reason, announced=announced, gate_filed=gate_filed
    )
    _announce(run, entry, marker, persist=True, turns=turns)
    if cause is not None:
        _file_gate_once(run, entry, cause, detail)


def _system_name(run: _Run, role: OutboxRole) -> str | None:
    """The tickets system name for the gate text; ``None`` for other roles."""
    tickets = run.cfg.bindings.tickets
    if role is not OutboxRole.TICKETS or tickets is None or tickets.system is None:
        return None
    return str(tickets.system)


def _file_gate_once(
    run: _Run, entry: OutboxEntry, cause: UnreachableCause, detail: str
) -> None:
    """File the episode's gate record unless the marker's bit says it is filed.

    The bit is set only when the filer returns ``True``, so a missing or failing
    ``needs_you.py`` is retried on the next tick (R13). A marker that could not be
    persisted does not file: the retry happens when the marker write succeeds.
    """
    marker = run.broken.get(entry.role)
    if marker is None or marker.gate_filed or entry.role in run.marker_write_failures:
        return
    payload = build_gate_payload(
        run.client,
        entry.role.value,
        cause,
        system=_system_name(run, entry.role),
        detail=detail,
    )
    if not file_gate_record(payload):
        return
    marker = marker.model_copy(update={"gate_filed": True})
    run.broken[entry.role] = marker
    _persist_marker(run, entry, marker, persist=True)


def _post_operator_part(ticket: str, body: str, *, cwd: Path) -> PostResult:
    """Post one operator-authored part (no agent marker) and read it back.

    ``post_parts`` always appends the agent marker, so this is the small second
    path: one post, one listing, no retry loop.
    """
    proc = post_issue_comment(ticket, body, cwd=cwd, operator_authored=True)

    def _stop(
        status: PostStatus, reason: PostReason, *posted: PostedPart
    ) -> PostResult:
        return PostResult(status, reason, posted, 1)

    if proc is None:
        return _stop(PostStatus.PARTIAL, PostReason.POST_OUTCOME_UNKNOWN)
    if proc.returncode != 0:
        return _stop(PostStatus.REFUSED, PostReason.POST_FAILED)
    url = parse_comment_url(proc.stdout)
    if url is None:
        return _stop(PostStatus.PARTIAL, PostReason.URL_MISSING)
    part = PostedPart(index=1, url=url)
    comments = fetch_issue_comments(ticket, FETCH_COMMENTS_TIMEOUT, cwd=cwd)
    fetched = find_comment_body(comments or [], url)
    if fetched is None:
        return _stop(PostStatus.PARTIAL, PostReason.READBACK_MISSING, part)
    if not readback_matches(body, fetched):
        return _stop(PostStatus.PARTIAL, PostReason.READBACK_MISMATCH, part)
    return PostResult(PostStatus.POSTED, None, (part,), 1)


def _post_part(entry: OutboxEntry, body: str, *, cwd: Path) -> PostResult:
    if entry.operator_authored:
        return _post_operator_part(entry.ticket, body, cwd=cwd)
    return post_parts(entry.ticket, [body], cwd=cwd)


def _listing_lines(comments: Sequence[dict[str, object]]) -> frozenset[str]:
    lines: set[str] = set()
    for comment in comments:
        body = comment.get("body")
        if isinstance(body, str):
            lines.update(line.strip() for line in body.splitlines())
    return frozenset(lines)


def _settle_fault(
    run: _Run,
    entry: OutboxEntry,
    result: PostResult,
    *,
    public: bool,
    posted: Sequence[PostedPart],
) -> None:
    """Rows P2 to P7: the first part that did not post decides the outcome."""
    if result.status is PostStatus.REFUSED and not public:
        _per_item_failure(run, entry)
        return
    reason = (
        OutboxReason.READBACK_MISMATCH
        if result.reason is PostReason.READBACK_MISMATCH
        else OutboxReason.READBACK_UNVERIFIED
    )
    _to_unverified(run, entry, reason, posted=posted)


def _post_missing(
    run: _Run,
    entry: OutboxEntry,
    parts: Sequence[str],
    present: frozenset[int],
    cwd: Path,
) -> None:
    """Post every part whose marker line is absent, in order (rows P1 to P7)."""
    total = len(parts)
    posted: list[PostedPart] = []
    for index, part in enumerate(parts, start=1):
        if index in present:
            continue
        body = f"{part}\n\n{part_marker_line(entry.marker, index, total)}"
        result = _post_part(entry, body, cwd=cwd)
        posted.extend(PostedPart(index=index, url=p.url) for p in result.posted)
        if result.status is not PostStatus.POSTED:
            public = bool(present) or bool(posted)
            _settle_fault(run, entry, result, public=public, posted=posted)
            return
    _deliver(run, entry, OutboxReason.POSTED, posted)


def _drain_entry(run: _Run, entry: OutboxEntry) -> None:
    """Apply the P3 decision table to one claimed entry."""
    target = run.target
    if entry.role is not OutboxRole.TICKETS or target is None:
        _binding_fault(
            run,
            entry,
            OutboxReason.BINDING_UNREACHABLE,
            cause=UnreachableCause.NO_REALIZATION,
        )
        return
    try:
        parts = read_parts(entry)
    except OutboxRecordError:
        _per_item_failure(run, entry)
        return
    if isinstance(target, McpTarget):
        _drain_entry_mcp(run, entry, parts)
        return
    _drain_entry_gh(run, entry, parts, target)


def _courier_parts(entry: OutboxEntry, parts: Sequence[str]) -> list[CourierPart]:
    """Each part with its marker line, as the courier posts and reads it back."""
    total = len(parts)
    courier: list[CourierPart] = []
    for index, part in enumerate(parts, start=1):
        line = part_marker_line(entry.marker, index, total)
        courier.append(CourierPart(body=f"{part}\n\n{line}", marker_line=line))
    return courier


def _drain_entry_mcp(run: _Run, entry: OutboxEntry, parts: Sequence[str]) -> None:
    """Post one entry through the MCP courier and map its verdict (O2b)."""
    cfg = run.cfg
    result = courier_drain(
        entry.ticket,
        _courier_parts(entry, parts),
        operator_authored=entry.operator_authored,
        recheck=entry.claimed_from is OutboxState.UNVERIFIED,
        context=CourierContext(runner=run.runner, client=cfg),
    )
    turns = result.turns
    posted = [PostedPart(index=p.index, url=p.url) for p in result.posted]
    if result.disposition is CourierDisposition.DELIVERED:
        reason = (
            OutboxReason.MARKER_ALREADY_PRESENT
            if result.already_present
            else OutboxReason.POSTED
        )
        _deliver(run, entry, reason, posted, turns=turns)
    elif result.disposition is CourierDisposition.UNVERIFIED:
        reason = (
            OutboxReason.READBACK_MISMATCH
            if courier_mismatch(result)
            else OutboxReason.READBACK_UNVERIFIED
        )
        _to_unverified(run, entry, reason, posted=posted, turns=turns)
    elif result.disposition is CourierDisposition.UNREACHABLE:
        _binding_fault(
            run,
            entry,
            OutboxReason.BINDING_UNREACHABLE,
            turns=turns,
            cause=courier_cause(result),
            detail="; ".join(result.layers_unreadable),
        )
    else:
        _per_item_failure(run, entry, turns=turns)


def _drain_entry_gh(
    run: _Run, entry: OutboxEntry, parts: Sequence[str], target: GhTarget
) -> None:
    """Post one entry through ``gh`` (the O2 path, unchanged)."""
    comments = fetch_issue_comments(
        entry.ticket, FETCH_COMMENTS_TIMEOUT, cwd=target.cwd
    )
    if comments is None:
        _binding_fault(run, entry, OutboxReason.TRANSIENT_FAILURE)
        return
    lines = _listing_lines(comments)
    total = len(parts)
    present = frozenset(
        index
        for index in range(1, total + 1)
        if part_marker_line(entry.marker, index, total) in lines
    )
    if len(present) == total:
        _deliver(run, entry, OutboxReason.MARKER_ALREADY_PRESENT)
        return
    _post_missing(run, entry, parts, present, target.cwd)


def _entry_for_id(client: str, entry_id: str) -> OutboxEntry | None:
    """Read the current record after a recoverable per-entry store failure."""
    try:
        return next(
            (entry for entry in list_entries(client) if entry.id == entry_id), None
        )
    except OutboxError as exc:
        logger.warning(
            "outbox: could not re-read entry %s after store failure (%s)",
            entry_id,
            type(exc).__name__,
        )
        return None


def _reconcile_store_failure(
    run: _Run,
    entry_id: str,
    exc: OutboxError,
    *,
    unchanged_state: OutboxState | None = None,
) -> None:
    """Keep a committed per-entry write and isolate the failed entry.

    Atomic record writes can commit before reporting an ``OutboxRecordError``.
    Re-reading the record lets the drain account for that commit without
    attempting the entry again in this run.  An unchanged claimed record is
    intentionally left for the next run's recovery path.
    """
    logger.warning(
        "outbox: store operation failed for entry %s; continuing (%s)",
        entry_id,
        type(exc).__name__,
    )
    current = _entry_for_id(run.client, entry_id)
    if current is None or current.state is OutboxState.CLAIMED:
        # The write either did not commit or left the entry claimed.  Do not
        # suppress the error: the entry is unresolved and needs the next run
        # (or recovery) to make progress safely.
        run.storage_error = run.storage_error or exc
        return
    if current.state is unchanged_state:
        run.storage_error = run.storage_error or exc
        return
    if current.id in run.accounted:
        return
    if current.state is OutboxState.FAILED:
        return
    if current.state is OutboxState.DELIVERED:
        run.accounted.add(current.id)
        run.delivered += 1
        run.delivered_roles.add(current.role)
        _emit(
            OrchestratorEventType.OUTBOX_ENTRY_DELIVERED,
            current,
            current.reason or OutboxReason.POSTED,
        )
    elif current.state is OutboxState.UNVERIFIED:
        run.accounted.add(current.id)
        run.unverified += 1
        _emit(
            OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED,
            current,
            current.reason or OutboxReason.READBACK_UNVERIFIED,
        )
    elif current.state is OutboxState.PENDING:
        run.accounted.add(current.id)
        run.released += 1
        held = current.id in read_held(run.client)
        if held:
            run.held += 1
        _emit(
            OrchestratorEventType.OUTBOX_ENTRY_PENDING,
            current,
            current.reason or OutboxReason.TRANSIENT_FAILURE,
            held=held,
        )


def _next_candidate_id(client: str, state: OutboxState, seen: set[str]) -> str | None:
    """Return the oldest unhandled candidate for claim-error isolation."""
    try:
        candidates = [
            entry
            for entry in list_entries(client)
            if entry.state is state and entry.id not in seen
        ]
    except OutboxError as exc:
        logger.warning(
            "outbox: could not identify claim candidate for %s (%s)",
            client,
            type(exc).__name__,
        )
        return None
    if not candidates:
        return None
    return min(candidates, key=lambda entry: (entry.created_at, entry.id)).id


def _drain_state(run: _Run, state: OutboxState, cap: int) -> None:
    """Claim *state* entries one at a time until none is left or *cap* is hit."""
    while run.claims < cap:
        candidate_id = _next_candidate_id(run.client, state, run.seen)
        try:
            claimed = claim(
                run.client, state=state, limit=1, exclude_ids=frozenset(run.seen)
            )
        except OutboxError as exc:
            # A claim write may have committed before reporting its error.
            if candidate_id is None:
                run.storage_error = run.storage_error or exc
                return
            run.seen.add(candidate_id)
            current = _entry_for_id(run.client, candidate_id)
            if current is not None and current.state is OutboxState.CLAIMED:
                run.claims += 1
            _reconcile_store_failure(run, candidate_id, exc, unchanged_state=state)
            continue
        if not claimed:
            return
        entry = claimed[0]
        run.seen.add(entry.id)
        run.claims += 1
        try:
            marker = run.broken.get(entry.role)
            if marker is not None:
                _announce(run, entry, marker)
            else:
                _drain_entry(run, entry)
        except OutboxError as exc:
            _reconcile_store_failure(run, entry.id, exc)


def _non_terminal_ids(client: str) -> frozenset[str]:
    return frozenset(e.id for e in list_entries(client) if e.state in _NON_TERMINAL)


def _emit_skipped(client: str, before: frozenset[str]) -> int:
    """Emit ``entry_skipped`` for every entry the store settled unreadable."""
    skipped = 0
    for entry in list_entries(client):
        if (
            entry.id in before
            and entry.state is OutboxState.FAILED
            and entry.reason is OutboxReason.BODY_UNREADABLE
        ):
            skipped += 1
            _emit(OrchestratorEventType.OUTBOX_ENTRY_SKIPPED, entry, entry.reason)
    return skipped


def _unreachable(client: str) -> list[str]:
    return sorted(role.value for role in marked_roles(client))


def _run_leased(
    client: str,
    cfg: ClientConfig,
    *,
    cap: int,
    force: bool,
    runner: CodexRunner,
) -> DrainReport:
    if force:
        clear_all_markers(client)
        clear_held(client)
    run = _Run(
        client=client,
        cfg=cfg,
        target=select_target(cfg),
        seen=set(read_held(client)),
        runner=runner,
    )
    before = _non_terminal_ids(client)
    _drain_state(run, OutboxState.UNVERIFIED, cap)
    _drain_state(run, OutboxState.PENDING, cap)
    skipped = _emit_skipped(client, before)
    for role in run.delivered_roles - set(run.broken):
        clear_marker(client, role)
    drained = run.delivered + run.unverified + skipped
    report = DrainReport(
        client=client,
        drained=drained,
        delivered=run.delivered,
        unverified=run.unverified,
        skipped=skipped,
        released=run.released,
        held=run.held,
        lease_held=False,
        unreachable_roles=_unreachable(client),
    )
    if run.storage_error is not None:
        raise run.storage_error
    return report


def run_drain(
    client: str,
    cfg: ClientConfig,
    *,
    limit: int | None = None,
    force: bool = False,
    runner: CodexRunner | None = None,
) -> DrainReport:
    """Drain *client*'s outbox once, holding its drain lease (plan P3, P12).

    *limit* overrides ``cfg.outbox.max_entries_per_run``. *force* first clears
    every unreachable marker and the held list, so those entries are retried.
    *runner* drives the MCP courier (default: the real codex runner; tests pass a
    scripted one). When another drain holds the lease nothing is touched and the
    report has ``lease_held=True``. An unexpected store error propagates; the
    entry it left claimed is recovered by the next run's ``claim``.
    """
    cap = cfg.outbox.max_entries_per_run if limit is None else limit
    codex = RealCodexRunner() if runner is None else runner
    try:
        with drain_lease(client):
            return _run_leased(client, cfg, cap=cap, force=force, runner=codex)
    except DrainLeaseHeldError:
        return DrainReport(
            client=client,
            drained=0,
            delivered=0,
            unverified=0,
            skipped=0,
            released=0,
            held=0,
            lease_held=True,
            unreachable_roles=_unreachable(client),
        )
