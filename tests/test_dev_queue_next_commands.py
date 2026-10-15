"""``cw.dev_queue.next_commands``: the state -> legal-next-command table (#2691).

Three layers, each answering a different question:

- the rule table: which commands each row state yields, in order;
- token legality: every emitted command names a real ``cw`` command and real
  flags on it (the live Click tree, as ``test_cli_surface_conventions`` walks);
- state legality: the verb itself would accept the row as it stands, checked
  by running each verb's own gate against the same saved state.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import click
import pytest

import cw.dev_queue.next_commands as next_commands_mod
from cw.cli import main
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.config import load_state
from cw.dev_queue import (
    AWAITING_OPERATOR_DISPOSITION,
    BRANCH_STALENESS_GATE_DISPOSITION,
    DRAIN_DISPOSITIONS,
    FINALIZE_GATE_HELD_DISPOSITION,
    REVIEW_STALENESS_GATE_DISPOSITION,
    approve_scope_drift_ticket,
    approve_ticket,
    precheck_requeue,
)
from cw.dev_queue.must_fix_override import _require_codex_must_fix_park
from cw.dev_queue.next_commands import next_commands
from cw.exceptions import ApproveGateError
from cw.models import (
    MustFixOverride,
    QueueItemStatus,
    ReapReason,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile._shared import resolve_session_for_task
from tests._ticket_view_helpers import bound_session_for, seed_queue, write_lane_client
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import Session

_T = "ACM-1"
_C = "acme"
_PREFIX = f"cw dev-queue {{verb}} {_T} -c {_C}"
_SCOPE_DRIFT_HEAD = "0123456789abcdef0123456789abcdef01234567"
_PLAN_PENDING = {"status": "plan_pending_approval"}
_REVIEW_PENDING = {"status": "review_pending_approval"}


def _cmd(verb: str, extra: str = "") -> str:
    base = _PREFIX.format(verb=verb)
    return f"{base} {extra}" if extra else base


def _task(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {"ticket_id": _T, "client": _C}
    kwargs.update(overrides)
    return _make_ticket_task(**kwargs)


def _blocked(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "session_id": "sess-1",
    }
    kwargs.update(overrides)
    return _task(**kwargs)


def _override() -> MustFixOverride:
    return MustFixOverride(
        actor="alice",
        reason="false positive",
        reviewed_sha="a" * 40,
        finding_ids=[],
        recorded_at=datetime(2026, 10, 15, tzinfo=UTC),
    )


@dataclass(frozen=True)
class _Case:
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


_CASES: dict[str, _Case] = {
    "rule1_signoff": _Case(
        _task(status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, stage=Stage.REVIEW),
        with_session=False,
        expected=[_cmd("approve"), _cmd("requeue")],
    ),
    "rule2_pending": _Case(
        _task(status=QueueItemStatus.PENDING),
        expected=[_cmd("wait"), _cmd("cancel")],
    ),
    "rule3_running": _Case(
        _task(status=QueueItemStatus.RUNNING, session_id="sess-1"),
        expected=[_cmd("wait"), f"cw queue peek -c {_C}"],
    ),
    "rule4_completed": _Case(
        _task(status=QueueItemStatus.COMPLETED),
        expected=[_cmd("requeue", "--from-completed")],
    ),
    "rule4_failed": _Case(
        _task(status=QueueItemStatus.FAILED),
        expected=[_cmd("requeue", "--from-failed")],
    ),
    "rule4_cancelled": _Case(
        _task(status=QueueItemStatus.CANCELLED),
        expected=[_cmd("requeue", "--from-cancelled")],
    ),
    "rule5_scope_drift_no_session": _Case(
        _blocked(stage=Stage.IMPL, blocked_reason="plan_scope_drift"),
        with_session=False,
        expected=[_cmd("approve", "--scope-drift <paths>"), _cmd("requeue")],
    ),
    "rule6a_must_fix_override_recorded": _Case(
        _blocked(
            stage=Stage.REVIEW,
            blocked_reason=CODEX_MUST_FIX_FINDINGS,
            must_fix_override=_override(),
        ),
        with_session=False,
        expected=[
            _cmd("requeue", "--stage finalize"),
            _cmd("approve", '--override-must-fix --reason "<why>"'),
        ],
    ),
    "rule6_must_fix_no_session": _Case(
        _blocked(stage=Stage.REVIEW, blocked_reason=CODEX_MUST_FIX_FINDINGS),
        with_session=False,
        expected=[
            _cmd("approve", '--override-must-fix --reason "<why>"'),
            _cmd("requeue", "--stage finalize"),
        ],
    ),
    "rule7_salvage_parked": _Case(
        _blocked(stage=Stage.IMPL),
        reap_reason=ReapReason.SALVAGE_PARKED,
        expected=[_cmd("unblock"), _cmd("requeue")],
    ),
    "rule8_awaiting_operator": _Case(
        _blocked(stage=Stage.IMPL, disposition=AWAITING_OPERATOR_DISPOSITION),
        with_session=False,
        expected=[_cmd("requeue"), f"cw dev-queue drain --held -c {_C}"],
    ),
    "rule8_review_artifacts_stale": _Case(
        _blocked(stage=Stage.REVIEW, disposition=REVIEW_STALENESS_GATE_DISPOSITION),
        session_last_result=_REVIEW_PENDING,
        expected=[_cmd("requeue"), f"cw dev-queue drain --held -c {_C}"],
    ),
    "rule9_plan_gate": _Case(
        _blocked(stage=Stage.PLAN),
        session_last_result=_PLAN_PENDING,
        expected=[_cmd("approve"), _cmd("requeue", "--stage plan")],
    ),
    "rule9_review_gate": _Case(
        _blocked(stage=Stage.REVIEW),
        session_last_result=_REVIEW_PENDING,
        expected=[_cmd("approve"), _cmd("requeue", "--stage review")],
    ),
    "rule9_approval_gate_disposition": _Case(
        _blocked(stage=Stage.PLAN, disposition="approval_gate"),
        expected=[_cmd("approve"), _cmd("requeue", "--stage plan")],
    ),
    "rule9_finalize_gate_held": _Case(
        _blocked(stage=Stage.REVIEW, disposition=FINALIZE_GATE_HELD_DISPOSITION),
        expected=[_cmd("approve"), _cmd("requeue", "--stage review")],
    ),
    "rule9_harden_gate_has_no_stage_flag": _Case(
        # HARDEN is not a `requeue --stage` choice; requeue's own default
        # (the current stage) is the same move.
        _blocked(stage=Stage.HARDEN, disposition="approval_gate"),
        expected=[_cmd("approve"), _cmd("requeue")],
    ),
    "rule10_plain_blocked_no_session": _Case(
        _blocked(stage=Stage.PLAN),
        with_session=False,
        expected=[_cmd("requeue")],
    ),
    "rule11_branch_behind_main": _Case(
        _blocked(stage=Stage.REVIEW, disposition=BRANCH_STALENESS_GATE_DISPOSITION),
        session_last_result=_REVIEW_PENDING,
        expected=[_cmd("requeue")],
    ),
    "rule11_unknown_disposition": _Case(
        _blocked(stage=Stage.IMPL, disposition="some_future_park"),
        expected=[_cmd("requeue")],
    ),
    "order_rule6_before_rule9": _Case(
        _blocked(stage=Stage.REVIEW, blocked_reason=CODEX_MUST_FIX_FINDINGS),
        session_last_result=_REVIEW_PENDING,
        expected=[
            _cmd("approve", '--override-must-fix --reason "<why>"'),
            _cmd("requeue", "--stage finalize"),
        ],
    ),
    "order_rule7_before_rule9": _Case(
        _blocked(stage=Stage.PLAN, disposition="approval_gate"),
        reap_reason=ReapReason.SALVAGE_PARKED,
        expected=[_cmd("unblock"), _cmd("requeue")],
    ),
}


def _commands(case: _Case) -> list[str]:
    return [c.command for c in next_commands(case.task, session=case.session())]


class TestRuleTable:
    @pytest.mark.parametrize("case_id", sorted(_CASES))
    def test_rule(self, case_id: str) -> None:
        case = _CASES[case_id]
        assert _commands(case) == case.expected

    def test_every_command_has_a_purpose(self) -> None:
        for case in _CASES.values():
            for cmd in next_commands(case.task, session=case.session()):
                assert cmd.purpose

    def test_drain_purpose_warns_it_releases_every_held_ticket(self) -> None:
        cmds = next_commands(_CASES["rule8_awaiting_operator"].task, session=None)
        drain = cmds[1]
        assert drain.purpose == (
            f"releases ALL held tickets of client {_C}, not just this one;"
            " add --dry-run to preview"
        )

    def test_drain_set_excludes_branch_behind_main(self) -> None:
        # The #1823 pin rule 11 relies on: a stale branch is never drained.
        assert BRANCH_STALENESS_GATE_DISPOSITION not in DRAIN_DISPOSITIONS
        assert REVIEW_STALENESS_GATE_DISPOSITION in DRAIN_DISPOSITIONS

    def test_name_valued_session_id_resolves_to_no_session(
        self, tmp_path: Path
    ) -> None:
        # Documented divergence from approve's find_by_name_or_id: a legacy
        # row whose session_id holds a session *name* falls to rule 10.
        task = _blocked(stage=Stage.PLAN, session_id="acme/auto-dev/ACM-1")
        session = bound_session_for(task, id="real-id", last_result=_PLAN_PENDING)
        seed_queue([task], [session])
        resolved = resolve_session_for_task(task, load_state())
        assert resolved is None
        assert [c.command for c in next_commands(task, session=resolved)] == [
            _cmd("requeue")
        ]

    def test_ticket_and_client_are_shell_quoted(self) -> None:
        task = _make_ticket_task(ticket_id="ACM-1.x", client="my client")
        cmds = next_commands(task, session=None)
        assert cmds[0].command == "cw dev-queue wait ACM-1.x -c 'my client'"
        assert shlex.split(cmds[0].command)[-1] == "my client"


def _resolve_command(tokens: list[str]) -> click.Command:
    """Walk *tokens* (after ``cw``) down the live Click tree to a leaf."""
    ctx = click.Context(main)
    cmd: click.Command = main
    for token in tokens:
        if not isinstance(cmd, click.Group):
            break
        sub = cmd.get_command(ctx, token)
        assert sub is not None, f"unknown subcommand {token!r}"
        cmd = sub
    assert not isinstance(cmd, click.Group)
    return cmd


class TestTokenLegality:
    @pytest.mark.parametrize("case_id", sorted(_CASES))
    def test_commands_resolve_against_click_tree(self, case_id: str) -> None:
        for command in _commands(_CASES[case_id]):
            tokens = shlex.split(command)
            assert tokens[0] == "cw"
            leaf = _resolve_command(tokens[1:])
            opts = {
                opt
                for param in leaf.params
                if isinstance(param, click.Option)
                for opt in (*param.opts, *param.secondary_opts)
            }
            flags = [t for t in tokens if t.startswith("-")]
            assert set(flags) <= opts, f"{command}: unknown flags"


@dataclass(frozen=True)
class _Parsed:
    verb: str
    flags: dict[str, str | None]


def _parse(command: str) -> _Parsed:
    tokens = shlex.split(command)
    verb = tokens[2]
    flags: dict[str, str | None] = {}
    rest = tokens[3:]
    for index, arg in enumerate(rest):
        if not arg.startswith("-") or arg == "-c":
            continue
        following = rest[index + 1] if index + 1 < len(rest) else None
        value = (
            following
            if following is not None and not following.startswith("-")
            else None
        )
        flags[arg] = value
    return _Parsed(verb, flags)


_STATE_CASES = (
    "rule1_signoff",
    "rule2_pending",
    "rule4_completed",
    "rule4_failed",
    "rule4_cancelled",
    "rule5_scope_drift_no_session",
    "rule6_must_fix_no_session",
    "rule6a_must_fix_override_recorded",
    "rule7_salvage_parked",
    "rule8_awaiting_operator",
    "rule9_review_gate",
    "rule10_plain_blocked_no_session",
    "rule11_branch_behind_main",
)


def _requeue_is_legal(parsed: _Parsed) -> None:
    precheck_requeue(
        _T,
        _C,
        parsed.flags.get("--stage"),
        from_cancelled="--from-cancelled" in parsed.flags,
        from_failed="--from-failed" in parsed.flags,
        from_completed="--from-completed" in parsed.flags,
        native_daemon=FakeNativeDaemonClient(),
    )


class TestStateLegality:
    """The verb's own gate accepts what was emitted and refuses omitted approve."""

    @pytest.mark.parametrize("case_id", _STATE_CASES)
    def test_emitted_commands_pass_their_verbs_gate(
        self, case_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _CASES[case_id]
        write_lane_client(_C, tmp_path / "ws")
        session = case.session()
        seed_queue([case.task], [session] if session is not None else [])
        monkeypatch.setattr(
            "cw.dev_queue.approval.branch_head_sha_on_origin",
            lambda *_a, **_kw: (_SCOPE_DRIFT_HEAD, True),
        )
        task = case.task
        resolved = resolve_session_for_task(task, load_state())
        parsed = [_parse(c.command) for c in next_commands(task, session=resolved)]
        verbs = [p.verb for p in parsed]

        for p in parsed:
            if p.verb == "requeue":
                _requeue_is_legal(p)
            elif p.verb == "unblock":
                assert resolved is not None
                assert resolved.reap_reason == ReapReason.SALVAGE_PARKED
                assert task.status == QueueItemStatus.BLOCKED_ON_USER
            elif p.verb == "drain":
                assert task.disposition in DRAIN_DISPOSITIONS
                assert task.status == QueueItemStatus.BLOCKED_ON_USER
            elif p.verb == "approve" and "--override-must-fix" in p.flags:
                _require_codex_must_fix_park(task)

        plain_approve = any(
            p.verb == "approve"
            and "--scope-drift" not in p.flags
            and "--override-must-fix" not in p.flags
            for p in parsed
        )
        if not plain_approve:
            with pytest.raises(ApproveGateError):
                approve_ticket(_T, _C)
        # Mutating checks last: each one changes the saved row.
        if any(p.verb == "approve" and "--scope-drift" in p.flags for p in parsed):
            approve_scope_drift_ticket(_T, _C, ["a.py"])
        elif plain_approve:
            approve_ticket(_T, _C)
        assert verbs


class TestPurity:
    def test_makes_no_io(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_a: object, **_kw: object) -> object:
            msg = "next_commands must not read state"
            raise AssertionError(msg)

        for target in (
            "cw.dev_queue.storage.load_dev_queue",
            "cw.config.load_state",
            "cw.config.load_clients",
            "cw.events.read_events",
        ):
            monkeypatch.setattr(target, _boom)
        for case in _CASES.values():
            assert next_commands(case.task, session=case.session())
        assert not [n for n in vars(next_commands_mod) if n.startswith("load_")]
