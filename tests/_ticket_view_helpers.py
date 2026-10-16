"""Shared setup for the ``cw dev-queue show`` / ticket-view / refusal tests
(#2691, #2692).

Holds only what ``test_dev_queue_next_commands.py``, ``test_ticket_view.py``,
``test_cli_dev_queue_show.py``, ``test_dev_queue_refusal.py``,
``test_cli_dev_queue_refusals.py``, ``test_cli_dev_queue_settle.py``,
``test_dev_queue.py`` and ``test_cli.py`` share and ``tests/conftest.py``
lacks. No ``test_`` prefix, so pytest does not collect it (same convention as
``tests/_reconcile_helpers.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from click.testing import CliRunner, Result

from cw.cli import main
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.config import concurrency_override_file
from cw.dev_queue import (
    AWAITING_OPERATOR_DISPOSITION,
    BRANCH_STALENESS_GATE_DISPOSITION,
    FINALIZE_GATE_HELD_DISPOSITION,
    REVIEW_STALENESS_GATE_DISPOSITION,
    save_dev_queue,
)
from cw.models import (
    DEFAULT_LANE,
    ConcurrencyOverrides,
    DevQueueStore,
    LaneConcurrencyOverride,
    MustFixOverride,
    QueueItemStatus,
    ReapReason,
    Stage,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _make_daemon_session, _make_ticket_task, _seed_sessions

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import pytest

    from cw.models import Session, TicketTask

ACME = "acme"
ACME_TICKET = "ACM-1"
PLAN_PENDING: dict[str, object] = {"status": "plan_pending_approval"}
REVIEW_PENDING: dict[str, object] = {"status": "review_pending_approval"}
_PREFIX = f"cw dev-queue {{verb}} {ACME_TICKET} -c {ACME}"


def seed_queue(tasks: Sequence[TicketTask], sessions: Sequence[Session] = ()) -> None:
    """Persist *tasks* as the whole dev queue and append *sessions* to state."""
    save_dev_queue(DevQueueStore(tasks=list(tasks)))
    if sessions:
        _seed_sessions(*sessions)


def bound_session_for(task: TicketTask, **session_overrides: object) -> Session:
    """A daemon ``Session`` whose ``id`` is *task*'s ``session_id``."""
    kwargs: dict[str, object] = {
        "id": task.session_id,
        "client": task.client,
        "name": f"{task.client}/auto-dev/{task.ticket_id}",
    }
    kwargs.update(session_overrides)
    return _make_daemon_session(**kwargs)


def write_lane_client(
    client: str,
    workspace: Path,
    *,
    lanes: Sequence[str] = (DEFAULT_LANE,),
    attempt_ceiling: int | bool | None = None,
    paused: bool = False,
    signoff: str | None = None,
    finalize_gate: str | None = None,
) -> None:
    """Write a one-client ``clients.yaml`` whose lanes carry the given settings.

    ``attempt_ceiling``, ``paused``, ``signoff`` and ``finalize_gate`` apply to
    every lane in *lanes*; ``attempt_ceiling=False`` is the lane's explicit
    "no ceiling".
    """
    lane_entries: list[dict[str, object]] = []
    for name in lanes:
        entry: dict[str, object] = {"name": name}
        if attempt_ceiling is not None:
            entry["attempt_ceiling"] = attempt_ceiling
        if paused:
            entry["paused"] = True
        if signoff is not None:
            entry["signoff"] = signoff
        if finalize_gate is not None:
            entry["finalize_gate"] = finalize_gate
        lane_entries.append(entry)
    write_clients_yaml(
        ClientSpec(client, workspace, lanes=lane_entries), ensure_workspaces=True
    )


def patch_roster(
    monkeypatch: pytest.MonkeyPatch, live_ids: set[str] | None
) -> FakeNativeDaemonClient:
    """Point the ``show`` and CLI ``requeue`` roster reads at a fake.

    The fake holds *live_ids*; ``None`` makes the roster unreadable (the
    fail-closed read returns None). Patches ``cw.ticket_view`` (the row view)
    and ``cw.cli.dev_queue.crud`` (``dev_queue_requeue``'s daemon client).
    """
    fake = FakeNativeDaemonClient()
    if live_ids is None:
        fake.roster_unreadable = True
    else:
        fake._live.update(live_ids)
    monkeypatch.setattr("cw.ticket_view.get_native_daemon_client", lambda: fake)
    monkeypatch.setattr("cw.cli.dev_queue.crud.get_native_daemon_client", lambda: fake)
    return fake


def install_acme_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lanes: Sequence[str] = (DEFAULT_LANE, "fast"),
) -> FakeNativeDaemonClient:
    """The ``acme`` client with *lanes* and an empty, readable roster."""
    write_lane_client(ACME, tmp_path / "ws", lanes=lanes)
    return patch_roster(monkeypatch, set())


def write_lane_override(
    client: str, lane: str, *, paused: bool | None, consecutive_spawn_errors: int
) -> None:
    """Write ``concurrency_overrides.json`` with one lane override."""
    overrides = ConcurrencyOverrides(
        lanes={
            f"{client}/{lane}": LaneConcurrencyOverride(
                paused=paused, consecutive_spawn_errors=consecutive_spawn_errors
            )
        }
    )
    path = concurrency_override_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(overrides.model_dump_json())


def make_task(**overrides: object) -> TicketTask:
    """An ``acme`` ``ACM-1`` row with *overrides* applied."""
    kwargs: dict[str, object] = {"ticket_id": ACME_TICKET, "client": ACME}
    kwargs.update(overrides)
    return _make_ticket_task(**kwargs)


def make_blocked(**overrides: object) -> TicketTask:
    """A BLOCKED_ON_USER ``ACM-1`` row bound to session ``sess-1``."""
    kwargs: dict[str, object] = {
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "session_id": "sess-1",
    }
    kwargs.update(overrides)
    return make_task(**kwargs)


def make_override() -> MustFixOverride:
    """A recorded MUST_FIX override with fixed field values."""
    return MustFixOverride(
        actor="alice",
        reason="false positive",
        reviewed_sha="a" * 40,
        finding_ids=[],
        recorded_at=datetime(2026, 10, 15, tzinfo=UTC),
    )


def cmd(verb: str, extra: str = "") -> str:
    """``cw dev-queue <verb> ACM-1 -c acme [extra]``."""
    base = _PREFIX.format(verb=verb)
    return f"{base} {extra}" if extra else base


def invoke_dev_queue(*args: str) -> Result:
    """Run ``cw dev-queue <args>`` through Click's ``CliRunner``."""
    return CliRunner().invoke(main, ["dev-queue", *args])


def stdout_lines(result: Result) -> list[str]:
    """*result*'s stdout, split into lines."""
    return result.stdout.splitlines()


@dataclass(frozen=True)
class Case:
    """One row state, the session bound to it, and the expected commands."""

    task: TicketTask
    session_last_result: dict[str, object] | None = None
    with_session: bool = True
    reap_reason: ReapReason | None = None
    expected: list[str] = field(default_factory=list)

    def session(self) -> Session | None:
        if not self.with_session or self.task.session_id is None:
            return None
        return bound_session_for(
            self.task,
            last_result=self.session_last_result,
            reap_reason=self.reap_reason,
        )


_OVERRIDE_CMD = cmd("approve", '--override-must-fix --reason "<why>"')
_SHIP_CMD = cmd("requeue", "--stage finalize")

CASES: dict[str, Case] = {
    "rule1_signoff": Case(
        make_task(status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, stage=Stage.REVIEW),
        with_session=False,
        expected=[cmd("approve"), cmd("requeue")],
    ),
    "rule2_pending": Case(
        make_task(status=QueueItemStatus.PENDING),
        expected=[cmd("wait"), cmd("cancel")],
    ),
    "rule3_running": Case(
        make_task(status=QueueItemStatus.RUNNING, session_id="sess-1"),
        expected=[cmd("wait"), f"cw queue peek -c {ACME}"],
    ),
    "rule4_completed": Case(
        make_task(status=QueueItemStatus.COMPLETED),
        expected=[cmd("requeue", "--from-completed")],
    ),
    "rule4_failed": Case(
        make_task(status=QueueItemStatus.FAILED),
        expected=[cmd("requeue", "--from-failed")],
    ),
    "rule4_cancelled": Case(
        make_task(status=QueueItemStatus.CANCELLED),
        expected=[cmd("requeue", "--from-cancelled")],
    ),
    "rule5_scope_drift_no_session": Case(
        make_blocked(stage=Stage.IMPL, blocked_reason="plan_scope_drift"),
        with_session=False,
        expected=[cmd("approve", "--scope-drift <paths>"), cmd("requeue")],
    ),
    "rule6a_must_fix_override_recorded": Case(
        make_blocked(
            stage=Stage.REVIEW,
            blocked_reason=CODEX_MUST_FIX_FINDINGS,
            must_fix_override=make_override(),
        ),
        with_session=False,
        expected=[_SHIP_CMD, _OVERRIDE_CMD],
    ),
    "rule6_must_fix_no_session": Case(
        make_blocked(stage=Stage.REVIEW, blocked_reason=CODEX_MUST_FIX_FINDINGS),
        with_session=False,
        expected=[_OVERRIDE_CMD, _SHIP_CMD],
    ),
    "rule7_salvage_parked": Case(
        make_blocked(stage=Stage.IMPL),
        reap_reason=ReapReason.SALVAGE_PARKED,
        expected=[cmd("unblock"), cmd("requeue")],
    ),
    "rule8_awaiting_operator": Case(
        make_blocked(stage=Stage.IMPL, disposition=AWAITING_OPERATOR_DISPOSITION),
        with_session=False,
        expected=[cmd("requeue"), f"cw dev-queue drain --held -c {ACME}"],
    ),
    "rule8_review_artifacts_stale": Case(
        make_blocked(stage=Stage.REVIEW, disposition=REVIEW_STALENESS_GATE_DISPOSITION),
        session_last_result=REVIEW_PENDING,
        expected=[cmd("requeue"), f"cw dev-queue drain --held -c {ACME}"],
    ),
    "rule9_plan_gate": Case(
        make_blocked(stage=Stage.PLAN),
        session_last_result=PLAN_PENDING,
        expected=[cmd("approve"), cmd("requeue", "--stage plan")],
    ),
    "rule9_review_gate": Case(
        make_blocked(stage=Stage.REVIEW),
        session_last_result=REVIEW_PENDING,
        expected=[cmd("approve"), cmd("requeue", "--stage review")],
    ),
    "rule9_approval_gate_disposition": Case(
        make_blocked(stage=Stage.PLAN, disposition="approval_gate"),
        expected=[cmd("approve"), cmd("requeue", "--stage plan")],
    ),
    "rule9_finalize_gate_held": Case(
        make_blocked(stage=Stage.REVIEW, disposition=FINALIZE_GATE_HELD_DISPOSITION),
        expected=[cmd("approve"), cmd("requeue", "--stage review")],
    ),
    "rule9_harden_gate_has_no_stage_flag": Case(
        # HARDEN is not a `requeue --stage` choice; requeue's own default
        # (the current stage) is the same move.
        make_blocked(stage=Stage.HARDEN, disposition="approval_gate"),
        expected=[cmd("approve"), cmd("requeue")],
    ),
    "rule10_plain_blocked_no_session": Case(
        make_blocked(stage=Stage.PLAN),
        with_session=False,
        expected=[cmd("requeue")],
    ),
    "rule11_branch_behind_main": Case(
        make_blocked(stage=Stage.REVIEW, disposition=BRANCH_STALENESS_GATE_DISPOSITION),
        session_last_result=REVIEW_PENDING,
        expected=[cmd("requeue")],
    ),
    "rule11_unknown_disposition": Case(
        make_blocked(stage=Stage.IMPL, disposition="some_future_park"),
        expected=[cmd("requeue")],
    ),
    "order_rule6_before_rule9": Case(
        make_blocked(stage=Stage.REVIEW, blocked_reason=CODEX_MUST_FIX_FINDINGS),
        session_last_result=REVIEW_PENDING,
        expected=[_OVERRIDE_CMD, _SHIP_CMD],
    ),
    "order_rule7_before_rule9": Case(
        make_blocked(stage=Stage.PLAN, disposition="approval_gate"),
        reap_reason=ReapReason.SALVAGE_PARKED,
        expected=[cmd("unblock"), cmd("requeue")],
    ),
}
