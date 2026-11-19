"""Post authorized text to a non-GitHub tracker through one codex agent turn (#3057).

RFC 0018 A2 (D-1). cw Python never talks to a non-GitHub tracker API
(ADR-0013); instead it launches one ``codex exec`` turn whose only callable
tools are the tracker's comment create and list tools (an explicit MCP
allowlist, :mod:`cw.tracker_io._argv`). cw owns everything around the turn:

1. the tracker's allowlist and the codex MCP server name
   (``tracker_mcp_gate.servers.codex``);
2. a positive-evidence MCP check (``ABSENT``/``DISABLED`` refuse, ``ENABLED``
   and ``UNKNOWN`` proceed, as S3 does);
3. the parts, planned with the library half of ``cw post-comment
   --emit-only`` (:func:`cw.tracker_post.plan_parts` plus the agent marker),
   so the posted text is byte-identical to the ``part-<i>.md`` files;
4. one launch in a fresh uuid scratch directory removed afterwards, so no
   other turn's ``-o`` file can ever be read;
5. parsing and self-attested verification (:mod:`cw.tracker_io._verify`).

:func:`post_parts_via_turn` returns a :class:`TrackerIoOutcome` for every
operational failure (launch, timeout, unparseable result, verification
mismatch), each with a closed reason. An unexpected exception raised by the
runner propagates to the caller; ``_post_via_tracker_io`` in
``cw.codex_review_persist`` is the guard that catches it. It never retries.
Callers must establish authorization and target binding before invoking it;
the prompt and model-echoed result are not authorization.

The O2b courier (:mod:`cw.tracker_io._drain`) reuses this module's scratch,
preflight and transport checks; it has its own entry point and turn loop.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.backend_mcp_config import (
    McpState,
    read_backend_mcp,
    scan_declared_servers,
)
from cw.codex_review import _codex_scratch_dir
from cw.codex_review._const import _is_spawn_error
from cw.config import state_dir
from cw.gh import agent_marked_body
from cw.models import CODEX_BACKEND
from cw.models.capabilities import ToolNarrowing
from cw.redact_post_text import git_toplevel, redaction_anchors, resolve_home
from cw.tracker_io._argv import (
    TrackerIoConfigError,
    build_tracker_io_argv,
    comment_tools_for,
    get_tool_for,
    narrowing_for,
    validate_server_name,
)
from cw.tracker_io._prompt import (
    RESULT_SCHEMA,
    parse_turn_result,
    render_prompt,
    validate_ticket_id,
)
from cw.tracker_io._verify import (
    PostedComment,
    PublicState,
    TrackerIoReason,
    public_state_for,
    verify_turn_result,
)
from cw.tracker_post import plan_parts

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.models import ClientConfig

_log = logging.getLogger(__name__)

#: Wall-clock budget for one turn.
TURN_TIMEOUT_SECONDS = 300

_SCHEMA_NAME = "schema.json"
_RESULT_NAME = "result.json"


class TrackerIoLayerError(TrackerIoConfigError):
    """A codex config layer exists but cannot be read (O2b, R22).

    Always carries ``MCP_SERVER_UNNAMED``. *paths* lists each unreadable layer
    as ``"<path>: <reason>"``, so the courier's gate record can name the file
    without parsing a message string.
    """

    def __init__(self, paths: tuple[str, ...]) -> None:
        message = "codex config layer(s) exist but cannot be read: " + "; ".join(paths)
        super().__init__(TrackerIoReason.MCP_SERVER_UNNAMED, message)
        self.paths = paths


@dataclass(frozen=True)
class TrackerIoContext:
    """What the turn needs from its caller: the runner, client and worktree."""

    runner: CodexRunner
    client: ClientConfig
    worktree: Path


@dataclass(frozen=True)
class TrackerIoOutcome:
    """What one turn did. *reason* is ``None`` only for a verified post."""

    public_state: PublicState
    reason: TrackerIoReason | None
    parts_total: int
    posted: tuple[PostedComment, ...] = ()
    marker_survived: bool | None = None
    # The exact marked, redacted bodies handed to the turn (#3353). Empty for a
    # refusal, where nothing was emitted; the audit hash is computed from these.
    emitted: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        """True when the agent's report passed every check."""
        return self.reason is None


@dataclass(frozen=True)
class CourierTarget:
    """What the courier needs to address one tracker ticket (O2b, R3, R8).

    *tools* is the (create, list) pair; *get_tool* reads one issue for the
    snapshot. *narrowing* is ``PER_TOOL`` for a declared server and
    ``PER_SERVER`` for a plugin app.
    """

    server: str
    tools: tuple[str, str]
    get_tool: str
    narrowing: ToolNarrowing


def _refused(reason: TrackerIoReason, parts_total: int = 0) -> TrackerIoOutcome:
    return TrackerIoOutcome(
        public_state_for(reason, parts_returned=0), reason, parts_total
    )


def plan_turn_parts(
    text: str, context: TrackerIoContext, *, marked: bool = True
) -> tuple[str, ...]:
    """Redact, wrap and split *text*; each part carries the agent marker.

    *marked* ``False`` returns the parts without the marker (#3060): the codex
    PLAN executor's GitHub path posts each part through ``post_issue_comment``,
    which appends the marker itself, so a marked part would carry it twice.

    Raises ``ValueError`` when the text cannot be planned.
    """
    worktree = context.worktree
    plan = plan_parts(
        text,
        home=resolve_home(os.environ),
        worktrees=redaction_anchors(
            context.client, git_toplevel(worktree), None, (str(worktree),)
        ),
        single=False,
    )
    if not marked:
        return tuple(plan.bodies)
    return tuple(agent_marked_body(body) for body in plan.bodies)


def _resolve(
    ticket_id: str, tracker: str, client: ClientConfig
) -> tuple[str, tuple[str, str]] | TrackerIoReason:
    """The server name and comment tools, or why the turn must not launch."""
    try:
        tools = comment_tools_for(tracker, client.bindings.tickets, CODEX_BACKEND)
        # A missing servers.codex entry is "" here: MCP_SERVER_UNNAMED too.
        server = validate_server_name(client.tracker_mcp_server(CODEX_BACKEND) or "")
        validate_ticket_id(ticket_id)
    except TrackerIoConfigError as exc:
        return exc.reason
    return server, tools


def _check_declared(server: str, client: ClientConfig) -> TrackerIoReason | None:
    """Refuse a declared-but-disabled or undeclared MCP server (legacy check)."""
    reading = read_backend_mcp(CODEX_BACKEND, server, client.workspace_path)
    if reading.state in (McpState.ABSENT, McpState.DISABLED):
        _log.warning(
            "tracker_io_mcp_not_enabled server=%s state=%s: %s",
            server,
            reading.state.value,
            reading.reason,
        )
        return TrackerIoReason.TRACKER_MCP_NOT_ENABLED
    return None


def _preflight(
    ticket_id: str, tracker: str, client: ClientConfig
) -> tuple[str, tuple[str, str]] | TrackerIoReason:
    """The server name and tools, or why the turn must not launch (legacy)."""
    resolved = _resolve(ticket_id, tracker, client)
    if isinstance(resolved, TrackerIoReason):
        return resolved
    server, tools = resolved
    refusal = _check_declared(server, client)
    if refusal is not None:
        return refusal
    return server, tools


def courier_preflight(
    ticket_id: str, tracker: str, client: ClientConfig
) -> CourierTarget | TrackerIoReason:
    """The courier's preflight (O2b, R4).

    Narrowing comes from the user and system layers only (R3). A ``PER_SERVER``
    plugin app skips the declared-server check, so a clean app-only host with no
    config proceeds (R4). A ``PER_TOOL`` server keeps the legacy check.
    """
    resolved = _resolve(ticket_id, tracker, client)
    if isinstance(resolved, TrackerIoReason):
        return resolved
    server, tools = resolved
    try:
        get_tool = get_tool_for(tracker, client.bindings.tickets, CODEX_BACKEND)
    except TrackerIoConfigError as exc:
        return exc.reason
    narrowing = narrowing_for(server, scan_declared_servers(CODEX_BACKEND).names)
    if narrowing is ToolNarrowing.PER_TOOL:
        refusal = _check_declared(server, client)
        if refusal is not None:
            return refusal
    return CourierTarget(server, tools, get_tool, narrowing)


def _scratch_layer_root() -> Path:
    """The parent of every turn scratch directory (O2b, R11)."""
    return state_dir() / "codex-review"


def _scratch_unwritable() -> TrackerIoConfigError:
    return TrackerIoConfigError(
        TrackerIoReason.SCRATCH_UNWRITABLE,
        "tracker-IO scratch directory is unwritable",
    )


@contextmanager
def turn_scratch(schema: object) -> Iterator[Path]:
    """A fresh scratch directory holding *schema*, removed on exit (O2b).

    Raises ``TrackerIoConfigError(SCRATCH_UNWRITABLE)`` when it cannot be made.
    The directory is removed on every exit, including a raise from the body.
    """
    try:
        scratch = _codex_scratch_dir(uuid.uuid4().hex)
    except OSError as exc:
        _log.warning("tracker_io_scratch_unwritable", exc_info=True)
        raise _scratch_unwritable() from exc
    try:
        try:
            (scratch / _SCHEMA_NAME).write_text(json.dumps(schema), encoding="utf-8")
        except OSError as exc:
            _log.warning("tracker_io_scratch_unwritable", exc_info=True)
            raise _scratch_unwritable() from exc
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def other_servers_for(scratch: Path, *, write_capable: bool) -> tuple[str, ...]:
    """The other declared servers to disable for a turn run in *scratch* (O2b).

    Walks the project chain from the scratch cwd (R11) as well as the user and
    system layers. No layer at all returns ``()`` and the turn proceeds
    (nothing to disable). A readable layer returns its names. A layer that
    exists but cannot be read is refused for a *write_capable* turn (R22); a
    read-only turn keeps the readable names (best effort).
    """
    scan = scan_declared_servers(CODEX_BACKEND, root=scratch)
    for reason in scan.unreadable:
        _log.warning("tracker_io_layer_unreadable: %s", reason)
    if scan.unreadable and write_capable:
        raise TrackerIoLayerError(scan.unreadable)
    return scan.names


def transport_refusal(run: CodexRunResult, total: int) -> TrackerIoOutcome | None:
    """The outcome for a turn that never produced a result, else ``None``.

    Shared by the legacy turn and the courier (timeout, spawn error, non-zero
    exit). Every case is post-launch, so the caller decides the disposition.
    """
    if run.timed_out:
        return _refused(TrackerIoReason.TURN_TIMEOUT, total)
    if _is_spawn_error(run):
        return _refused(TrackerIoReason.CODEX_UNAVAILABLE, total)
    if run.returncode != 0:
        _log.warning("tracker_io_turn_exit rc=%s: %s", run.returncode, run.stderr)
        return _refused(TrackerIoReason.TURN_EXIT_NONZERO, total)
    return None


def _classify(
    run: CodexRunResult, ticket_id: str, parts: tuple[str, ...]
) -> TrackerIoOutcome:
    total = len(parts)
    refusal = transport_refusal(run, total)
    if refusal is not None:
        return refusal
    verified = verify_turn_result(
        ticket_id, parts, parse_turn_result(run.output_file_content)
    )
    return TrackerIoOutcome(
        verified.public_state,
        verified.reason,
        total,
        verified.posted,
        verified.marker_survived,
        emitted=parts,
    )


def _launch(
    ticket_id: str,
    parts: tuple[str, ...],
    *,
    server: str,
    tools: tuple[str, str],
    context: TrackerIoContext,
) -> TrackerIoOutcome:
    """Run the legacy turn once in a fresh scratch directory, always removed."""
    prompt = render_prompt(ticket_id, parts, server=server, tools=tools)
    try:
        with turn_scratch(RESULT_SCHEMA) as scratch:
            try:
                argv = build_tracker_io_argv(
                    server=server,
                    tools=tools,
                    schema_path=scratch / _SCHEMA_NAME,
                    output_path=scratch / _RESULT_NAME,
                    other_servers=other_servers_for(scratch, write_capable=True),
                )
            except TrackerIoConfigError as exc:
                _log.warning("tracker_io_config_refused: %s", exc)
                return _refused(exc.reason, len(parts))
            try:
                run = context.runner.run(
                    scratch, argv, TURN_TIMEOUT_SECONDS, stdin=prompt
                )
            except OSError:
                _log.warning("tracker_io_codex_unavailable", exc_info=True)
                return _refused(TrackerIoReason.CODEX_UNAVAILABLE, len(parts))
            return _classify(run, ticket_id, parts)
    except TrackerIoConfigError as exc:
        return _refused(exc.reason, len(parts))


def post_parts_via_turn(
    ticket_id: str,
    text: str,
    *,
    tracker: str,
    context: TrackerIoContext,
    session_id: str,
) -> TrackerIoOutcome:
    """Post *text* to *ticket_id* on *tracker* through one codex agent turn.

    *tracker* is the client's ``tracking.primary.system``. Operational failures
    return an outcome; an unexpected runner exception propagates (the caller
    guards it). Never retries; see the module docstring.
    """
    checked = _preflight(ticket_id, tracker, context.client)
    if isinstance(checked, TrackerIoReason):
        return _refused(checked)
    server, tools = checked
    try:
        parts = plan_turn_parts(text, context) if text.strip() else ()
    except ValueError:
        _log.warning("tracker_io_plan_refused ticket=%s", ticket_id, exc_info=True)
        parts = ()
    if not parts:
        return _refused(TrackerIoReason.PLAN_REFUSED)
    outcome = _launch(ticket_id, parts, server=server, tools=tools, context=context)
    if outcome.verified:
        _log.info(
            "tracker_io_posted ticket=%s session=%s ids=%s",
            ticket_id,
            session_id,
            ",".join(p.comment_id for p in outcome.posted),
        )
    return outcome
