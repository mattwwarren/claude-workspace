"""Planner orchestration: spawn /orchestrate-plan, collect, validate, persist.

The planner is a one-shot Claude session that consumes a list of pending
TicketTasks and produces a :class:`DispatchPlan` JSON file the dispatcher
can use to override enqueue order.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import uuid4

from cw.config import dev_plan_output_dir
from cw.dev_queue import list_tickets, save_plan
from cw.exceptions import CwError
from cw.models import DispatchPlan
from cw.spawn import spawn_create_impl

if TYPE_CHECKING:
    from pathlib import Path

    from cw.cmux import CmuxAdapter
    from cw.models import ClientConfig, TicketTask


_DEFAULT_TIMEOUT_SECONDS = 300
_POLL_INTERVAL_SECONDS = 1.0


def _format_tickets_prompt(tickets: list[TicketTask], output_path: Path) -> str:
    """Build the prompt body for /orchestrate-plan.

    The prompt instructs the planner skill where to write its JSON output
    and supplies the pending TicketTasks as JSON for context.
    """
    ticket_lines: list[str] = [t.model_dump_json() for t in tickets]
    tickets_block = "\n".join(ticket_lines)
    return (
        f"/orchestrate-plan {output_path}\n\n"
        "Pending tickets (one TicketTask JSON per line):\n"
        f"{tickets_block}\n"
    )


def _wait_for_plan_output(
    output_path: Path,
    timeout_seconds: int,
    *,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
) -> bool:
    """Poll *output_path* until it exists or *timeout_seconds* elapses.

    Returns True if the file appeared in time, False otherwise.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if output_path.exists() and output_path.stat().st_size > 0:
            return True
        time.sleep(poll_interval)
    return output_path.exists() and output_path.stat().st_size > 0


def _validate_and_persist(output_path: Path) -> DispatchPlan | None:
    """Validate the JSON at *output_path* against DispatchPlan and persist.

    Returns the persisted plan on success, or None on validation failure.
    """
    try:
        raw = output_path.read_text()
    except OSError:
        return None
    try:
        plan = DispatchPlan.model_validate_json(raw)
    except ValueError:
        return None
    save_plan(plan)
    return plan


class PlanResult:
    """Outcome of a single planner invocation."""

    def __init__(
        self,
        *,
        plan: DispatchPlan | None,
        session_id: str,
        prompt_path: Path,
        output_path: Path,
        error: str | None = None,
    ) -> None:
        self.plan = plan
        self.session_id = session_id
        self.prompt_path = prompt_path
        self.output_path = output_path
        self.error = error


def run_planner(
    *,
    client: ClientConfig,
    adapter: CmuxAdapter,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
    client_filter: str | None = None,
) -> PlanResult:
    """Spawn the /orchestrate-plan skill and collect its DispatchPlan output.

    Args:
        client: Target client whose cmux workspace hosts the planner session.
        adapter: cmux adapter used to spawn the session (FakeCmuxAdapter in
            tests).
        timeout_seconds: How long to wait for the planner to write its
            output JSON file before giving up.
        poll_interval: Seconds between output-path existence checks.
        client_filter: If set, only include pending tickets for this client
            in the planner prompt.  When None, all pending tickets across
            all clients are included.

    Returns:
        :class:`PlanResult` describing the outcome.  When validation fails
        or the planner times out, ``result.plan`` is None and ``result.error``
        carries a short description.  Callers should leave the dev queue
        untouched in those cases.
    """
    pending = [t for t in list_tickets(client_filter) if t.status.value == "pending"]
    if not pending:
        msg = "No pending tickets to plan."
        raise CwError(msg)

    correlation_id = uuid4().hex[:8]
    output_dir = dev_plan_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"plan-{correlation_id}.json"
    prompt_path = output_dir / f"prompt-{correlation_id}.txt"
    prompt_path.write_text(_format_tickets_prompt(pending, output_path))

    session_id = spawn_create_impl(
        client=client,
        worktree=client.workspace_path,
        prompt_file=prompt_path,
        surface="split",
        label=f"plan-{correlation_id}",
        adapter=adapter,
    )

    appeared = _wait_for_plan_output(
        output_path,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
    )
    if not appeared:
        return PlanResult(
            plan=None,
            session_id=session_id,
            prompt_path=prompt_path,
            output_path=output_path,
            error=f"Timed out after {timeout_seconds}s waiting for plan output.",
        )

    plan = _validate_and_persist(output_path)
    if plan is None:
        return PlanResult(
            plan=None,
            session_id=session_id,
            prompt_path=prompt_path,
            output_path=output_path,
            error="Plan output failed DispatchPlan validation.",
        )

    return PlanResult(
        plan=plan,
        session_id=session_id,
        prompt_path=prompt_path,
        output_path=output_path,
    )
