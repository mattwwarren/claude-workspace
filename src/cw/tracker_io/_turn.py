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
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.backend_mcp_config import (
    McpState,
    read_backend_mcp,
    tracker_io_other_server_names,
)
from cw.codex_review import _codex_scratch_dir
from cw.codex_review._const import _is_spawn_error
from cw.gh import agent_marked_body
from cw.models import CODEX_BACKEND
from cw.redact_post_text import git_toplevel, redaction_anchors, resolve_home
from cw.tracker_io._argv import (
    TrackerIoConfigError,
    build_tracker_io_argv,
    comment_tools_for,
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
    from pathlib import Path

    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.models import ClientConfig

_log = logging.getLogger(__name__)

#: Wall-clock budget for one turn.
TURN_TIMEOUT_SECONDS = 300

_SCHEMA_NAME = "schema.json"
_RESULT_NAME = "result.json"


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


def _preflight(
    ticket_id: str, tracker: str, client: ClientConfig
) -> tuple[str, tuple[str, str]] | TrackerIoReason:
    """The server name and tools, or why the turn must not launch."""
    try:
        tools = comment_tools_for(tracker)
        # A missing servers.codex entry is "" here: MCP_SERVER_UNNAMED too.
        server = validate_server_name(client.tracker_mcp_server(CODEX_BACKEND) or "")
        validate_ticket_id(ticket_id)
    except TrackerIoConfigError as exc:
        return exc.reason
    reading = read_backend_mcp(CODEX_BACKEND, server, client.workspace_path)
    if reading.state in (McpState.ABSENT, McpState.DISABLED):
        _log.warning(
            "tracker_io_mcp_not_enabled server=%s state=%s: %s",
            server,
            reading.state.value,
            reading.reason,
        )
        return TrackerIoReason.TRACKER_MCP_NOT_ENABLED
    return server, tools


def _other_servers() -> tuple[str, ...]:
    names = tracker_io_other_server_names()
    if names is None:
        _log.info("tracker_io_other_servers_unknown: none disabled")
        return ()
    return names


def _classify(
    run: CodexRunResult, ticket_id: str, parts: tuple[str, ...]
) -> TrackerIoOutcome:
    total = len(parts)
    if run.timed_out:
        return _refused(TrackerIoReason.TURN_TIMEOUT, total)
    if _is_spawn_error(run):
        return _refused(TrackerIoReason.CODEX_UNAVAILABLE, total)
    if run.returncode != 0:
        _log.warning("tracker_io_turn_exit rc=%s: %s", run.returncode, run.stderr)
        return _refused(TrackerIoReason.TURN_EXIT_NONZERO, total)
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
    tracker: str,
    context: TrackerIoContext,
) -> TrackerIoOutcome:
    """Run the turn once in a fresh scratch directory, always removed."""
    prompt = render_prompt(ticket_id, parts, server=server, tools=tools)
    try:
        scratch = _codex_scratch_dir(uuid.uuid4().hex)
    except OSError:
        _log.warning("tracker_io_scratch_unwritable", exc_info=True)
        return _refused(TrackerIoReason.SCRATCH_UNWRITABLE, len(parts))
    try:
        schema = scratch / _SCHEMA_NAME
        try:
            schema.write_text(json.dumps(RESULT_SCHEMA), encoding="utf-8")
        except OSError:
            _log.warning("tracker_io_scratch_unwritable", exc_info=True)
            return _refused(TrackerIoReason.SCRATCH_UNWRITABLE, len(parts))
        try:
            argv = build_tracker_io_argv(
                server=server,
                tracker=tracker,
                schema_path=schema,
                output_path=scratch / _RESULT_NAME,
                other_servers=_other_servers(),
            )
        except TrackerIoConfigError as exc:
            _log.warning("tracker_io_config_refused: %s", exc)
            return _refused(exc.reason, len(parts))
        try:
            run = context.runner.run(scratch, argv, TURN_TIMEOUT_SECONDS, stdin=prompt)
        except OSError:
            _log.warning("tracker_io_codex_unavailable", exc_info=True)
            return _refused(TrackerIoReason.CODEX_UNAVAILABLE, len(parts))
        return _classify(run, ticket_id, parts)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


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
    outcome = _launch(
        ticket_id, parts, server=server, tools=tools, tracker=tracker, context=context
    )
    if outcome.verified:
        _log.info(
            "tracker_io_posted ticket=%s session=%s ids=%s",
            ticket_id,
            session_id,
            ",".join(p.comment_id for p in outcome.posted),
        )
    return outcome
