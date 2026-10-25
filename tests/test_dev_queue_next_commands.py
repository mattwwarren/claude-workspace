"""``cw.dev_queue.next_commands``: the state -> legal-next-command table (#2691).

Three layers, each answering a different question:

- the rule table: which commands each row state yields, in order;
- token legality: every emitted command names a real ``cw`` command and real
  flags on it (the live Click tree, as ``test_cli_surface_conventions`` walks);
- state legality: the verb itself would accept the row as it stands, checked
  by running each verb's own gate against the same saved state.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click
import pytest

import cw.dev_queue.next_commands as next_commands_mod
import cw.executor
from cw.cli import main
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.config import get_client, load_state, save_state
from cw.dev_queue import (
    BRANCH_STALENESS_GATE_DISPOSITION,
    DRAIN_DISPOSITIONS,
    FINALIZE_GATE_HELD_DISPOSITION,
    REVIEW_STALENESS_GATE_DISPOSITION,
    approve_scope_drift_ticket,
    approve_ticket,
    precheck_requeue,
)
from cw.dev_queue.must_fix_override import _require_codex_must_fix_park
from cw.dev_queue.next_commands import (
    HUMAN_GATE_LABEL,
    HUMAN_GATE_PURPOSE_LABEL,
    LANE_PAUSED_SKIP_CODE,
    LANE_RESUME_PURPOSE,
    compact_command,
    dev_queue_command,
    human_gate_attention_command,
    human_gate_settled_notice,
    is_human_gate_command,
    is_human_kept_gate,
    next_commands,
    refusal_suffix,
    row_carries_human_opt_in,
)
from cw.exceptions import ApproveGateError, CwError, RequeueStageError
from cw.executor import pipeline_position, resolve_pipeline_stages
from cw.executor._resolve import pipeline_position as _resolve_pipeline_position
from cw.models import (
    DEFAULT_LANE,
    CwState,
    QueueItemStatus,
    ReapReason,
    Stage,
)
from cw.models.enums import PipelinePosition, stage_position
from cw.models.ticket_view import NextCommand
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile._shared import resolve_session_for_task
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    CASES,
    PLAN_PENDING,
    REVIEW_PENDING,
    Case,
    bound_session_for,
    cmd,
    make_blocked,
    make_task,
    seed_queue,
    write_lane_client,
)
from tests.conftest import _make_ticket_task, plan_body, stub_fetch_plan

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import Session, TicketTask


_T = ACME_TICKET
_C = ACME
_SCOPE_DRIFT_HEAD = "0123456789abcdef0123456789abcdef01234567"
LANE_CIRCUIT_PAUSED = "lane_circuit_paused"


def _commands(case: Case) -> list[str]:
    return [c.command for c in next_commands(case.task, session=case.session())]


class TestRuleTable:
    @pytest.mark.parametrize("case_id", sorted(CASES))
    def test_rule(self, case_id: str) -> None:
        case = CASES[case_id]
        assert _commands(case) == case.expected

    def test_every_command_has_a_purpose(self) -> None:
        for case in CASES.values():
            for command in next_commands(case.task, session=case.session()):
                assert command.purpose

    def test_drain_purpose_warns_it_releases_every_held_ticket(self) -> None:
        cmds = next_commands(CASES["rule8_awaiting_operator"].task, session=None)
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
        task = make_blocked(stage=Stage.PLAN, session_id="acme/auto-dev/ACM-1")
        session = bound_session_for(task, id="real-id", last_result=PLAN_PENDING)
        seed_queue([task], [session])
        resolved = resolve_session_for_task(task, load_state())
        assert resolved is None
        assert [c.command for c in next_commands(task, session=resolved)] == [
            cmd("requeue")
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
    @pytest.mark.parametrize("case_id", sorted(CASES))
    def test_commands_resolve_against_click_tree(self, case_id: str) -> None:
        for command in _commands(CASES[case_id]):
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
    "rule0_signoff",
    "rule0_finalize_gate_held",
    "rule2_pending",
    "rule4_completed",
    "rule4_failed",
    "rule4_cancelled",
    "rule5_scope_drift_no_session",
    "rule6_must_fix_no_session",
    "rule6a_must_fix_override_recorded",
    "rule6_scope_hint_large_ship_dropped",
    "rule6a_scope_hint_large_ship_dropped",
    "rule6b_attempt_cap_blocked",
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


def _is_e1(task: TicketTask, parsed: _Parsed, stages: list[Stage]) -> bool:
    """The walk's single named exception E1 (#2765, deferred bug).

    Rules 6/6a list ``requeue --stage finalize`` for a MUST_FIX row even when
    FINALIZE is not in the lane pipeline, and ``requeue`` refuses it.
    """
    return (
        parsed.verb == "requeue"
        and parsed.flags.get("--stage") == Stage.FINALIZE.value
        and task.blocked_reason == CODEX_MUST_FIX_FINDINGS
        and Stage.FINALIZE not in stages
    )


def _assert_commands_legal(
    task: TicketTask,
    session: Session | None,
    parsed: list[_Parsed],
    *,
    stages: list[Stage],
) -> None:
    """Per-command soundness: each emitted command passes its verb's own gate.

    Read-only checks only; mutating ``approve`` calls are the caller's job.
    """
    for p in parsed:
        if p.verb == "requeue" and _is_e1(task, p, stages):
            # E1 (#2765): pinned as refused so landing the fix forces removal.
            with pytest.raises(RequeueStageError):
                _requeue_is_legal(p)
        elif p.verb == "requeue":
            _requeue_is_legal(p)
        elif p.verb == "unblock":
            assert session is not None
            assert session.reap_reason == ReapReason.SALVAGE_PARKED
            assert task.status == QueueItemStatus.BLOCKED_ON_USER
        elif p.verb == "drain":
            assert task.disposition in DRAIN_DISPOSITIONS
            assert task.status == QueueItemStatus.BLOCKED_ON_USER
        elif p.verb == "approve" and "--override-must-fix" in p.flags:
            _require_codex_must_fix_park(task)


class TestStateLegality:
    """The verb's own gate accepts what was emitted and refuses omitted approve."""

    @pytest.mark.parametrize("case_id", _STATE_CASES)
    def test_emitted_commands_pass_their_verbs_gate(
        self, case_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = CASES[case_id]
        write_lane_client(_C, tmp_path / "ws")
        session = case.session()
        seed_queue([case.task], [session] if session is not None else [])
        monkeypatch.setattr(
            "cw.dev_queue.scope_drift.branch_head_sha_on_origin",
            lambda *_a, **_kw: (_SCOPE_DRIFT_HEAD, True),
        )
        task = case.task
        resolved = resolve_session_for_task(task, load_state())
        parsed = [_parse(c.command) for c in next_commands(task, session=resolved)]
        verbs = [p.verb for p in parsed]
        stages = resolve_pipeline_stages(task, get_client(_C))
        _assert_commands_legal(task, resolved, parsed, stages=stages)

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
        for case in CASES.values():
            assert next_commands(case.task, session=case.session())
        assert not [n for n in vars(next_commands_mod) if n.startswith("load_")]


_HUMAN_APPROVE_CASES = (
    "rule0_signoff",
    "rule0_finalize_gate_held",
    "rule0_scope_hint_large_approval_gate",
    "rule0_scope_hint_large_plan_pending",
)
# Rule 0a (#2760): approve refuses these at session-not-found, so the
# operator's labelled requeue is listed instead.
_HUMAN_REQUEUE_CASES = (
    "rule0_finalize_held_no_session",
    "rule0_scope_hint_large_no_session",
)
_SHIP_DROPPED_CASES = (
    "rule6a_scope_hint_large_ship_dropped",
    "rule6_scope_hint_large_ship_dropped",
    "rule6_hold_finalize_ship_dropped",
    "rule6a_signoff_operator_ship_dropped",
)
_PLAIN_SHIP_CASES = ("rule6_must_fix_no_session", "rule6a_must_fix_override_recorded")
_SHIP_CAVEAT = (
    "if the ticket or lane has a finalize hold, operator signoff or"
    " scope_hint: large, this step is the human's."
)
_GUIDED = {
    "ambiguities_pending_resolution": "The worker parked on open ambiguities.",
    "premises_pending_verification": "The worker parked on unverified premises.",
    "review_health_gate": "Review health is degraded.",
    "review_artifacts_stale": "The review artifacts predate the branch head.",
    "branch_behind_main": "The branch is behind the default branch.",
    "merge_gate_blocked": "A prior pipeline PR for this ticket is still open.",
    "stale_dispatch": "An earlier dispatch left an open, unmerged PR",
    "awaiting_operator": "The park was an operator-unavailability hold.",
}


_HELD = FINALIZE_GATE_HELD_DISPOSITION
_LABEL = HUMAN_GATE_PURPOSE_LABEL
_NO_GATE_SESSION = (
    "approve cannot take this row because no bound session is at the approval gate"
)
# #2760 exact strings (plan P1-P6, P3T, S1-S6), pinned verbatim.
_P1 = "re-run the finalize stage; approve refuses at the terminal stage"
_P2 = (
    "operator-only human gate; not for an agent to run: release the finalize"
    " approval gate and ship; approve refuses at the terminal stage"
)
_P2_HOLD = (
    "operator-only human gate; not for an agent to run: release the finalize-hold"
    " gate and ship; approve refuses at the terminal stage"
)
_P3 = (
    "operator-only human gate; not for an agent to run: re-run the review stage;"
    " approve cannot take this row because no bound session is at the approval"
    " gate"
)
_P3T = (
    "operator-only human gate; not for an agent to run: release the finalize"
    " approval gate and ship; approve cannot take this row because no bound"
    " session is at the approval gate"
)
_P4 = (
    "stage finalize is not in this row's lane pipeline, so approve and requeue"
    " --stage refuse it until the operator adds it to pipeline.stages or removes"
    " and re-adds the ticket; inspect the row"
)
_P5 = "re-run the current stage"
_P6 = "operator-only human gate; not for an agent to run: re-run the current stage"
_HUMAN_TAIL = (
    " This is a human-kept gate the operator opted into: an agent must not"
    " approve, release or requeue it. Notify the human operator."
)
_S1 = (
    " This row is blocked_on_user at stage finalize (disposition"
    " 'review_pending_approval'). Run: `cw dev-queue requeue ACM-1 -c acme"
    " --stage finalize` (re-run the finalize stage; approve refuses at the"
    " terminal stage)."
)
_S2 = (
    " This row is blocked_on_user at stage finalize (disposition"
    f" 'review_pending_approval').{_HUMAN_TAIL} The operator's release command"
    " is `cw dev-queue requeue ACM-1 -c acme --stage finalize` (operator-only;"
    " not for an agent to run)."
)
_S3 = (
    " This row is blocked_on_user at stage review (disposition"
    f" 'review_pending_approval').{_HUMAN_TAIL} The operator's re-run command"
    " is `cw dev-queue requeue ACM-1 -c acme --stage review` (operator-only;"
    " not for an agent to run)."
)
_S4 = (
    " This row is blocked_on_user at stage review (disposition"
    f" 'finalize_gate_held').{_HUMAN_TAIL} The operator's re-run command is"
    " `cw dev-queue requeue ACM-1 -c acme --stage review` (operator-only; not"
    " for an agent to run)."
)
_S5 = (
    " This row is blocked_on_user at stage finalize (disposition"
    f" 'finalize_gate_held').{_HUMAN_TAIL} The operator's release command is"
    " `cw dev-queue requeue ACM-1 -c acme --stage finalize` (operator-only; not"
    " for an agent to run)."
)
_S6 = (
    " This row is blocked_on_user at stage finalize (disposition"
    " 'review_pending_approval'). Run: `cw dev-queue show ACM-1 -c acme` (stage"
    " finalize is not in this row's lane pipeline, so approve and requeue"
    " --stage refuse it until the operator adds it to pipeline.stages or"
    " removes and re-adds the ticket; inspect the row). Alternatively: `cw"
    " dev-queue requeue ACM-1 -c acme` (re-run the current stage)."
)
_STAGE_COMPLETE: dict[str, object] = {"status": "stage_complete"}


def _verb(command: str) -> str:
    return shlex.split(command)[2]


def _large_final(**overrides: object) -> TicketTask:
    """The ticket's case: large scope, finalize, ``review_pending_approval``."""
    kwargs: dict[str, object] = {
        "stage": Stage.FINALIZE,
        "scope_hint": "large",
        "disposition": "review_pending_approval",
    }
    kwargs.update(overrides)
    return make_blocked(**kwargs)


def _gate_session(task: TicketTask) -> Session:
    """A bound session whose last result is at the review approval gate."""
    return bound_session_for(task, last_result=REVIEW_PENDING)


def _first(
    task: TicketTask, session: Session | None, position: PipelinePosition
) -> NextCommand:
    return next_commands(task, session=session, pipeline_position=position)[0]


def _spans(suffix: str) -> list[str]:
    """Every backticked span of *suffix*."""
    return suffix.split("`")[1::2]


class TestHumanKeptGate:
    """R1: a human-kept gate lists only the labelled operator command.

    ``approve`` when a bound session is at the gate, else ``requeue --stage
    <stage>`` (#2760).
    """

    @pytest.mark.parametrize("case_id", _HUMAN_APPROVE_CASES)
    def test_only_the_labelled_approve(self, case_id: str) -> None:
        case = CASES[case_id]
        cmds = next_commands(case.task, session=case.session())
        assert not [c for c in cmds if "requeue" in c.command]
        assert [_verb(c.command) for c in cmds] == ["approve"]
        assert cmds[0].purpose.startswith(HUMAN_GATE_PURPOSE_LABEL)
        assert cmds[0].operator_only is True
        suffix = refusal_suffix(case.task, session=case.session())
        assert "operator-only" in suffix
        assert "Run:" not in suffix
        assert "cw dev-queue requeue" not in suffix
        assert is_human_kept_gate(case.task)

    @pytest.mark.parametrize("case_id", _HUMAN_REQUEUE_CASES)
    def test_sessionless_rows_list_only_the_labelled_requeue(
        self, case_id: str
    ) -> None:
        case = CASES[case_id]
        cmds = next_commands(case.task, session=case.session())
        assert [c.command for c in cmds] == [cmd("requeue", "--stage review")]
        assert cmds[0].purpose == _P3
        assert cmds[0].operator_only is True
        assert not [c for c in cmds if _verb(c.command) == "approve"]
        assert is_human_kept_gate(case.task)

    @pytest.mark.parametrize(
        "case_id",
        sorted(set(CASES) - set(_HUMAN_APPROVE_CASES) - set(_HUMAN_REQUEUE_CASES)),
    )
    def test_no_other_case_is_human_kept(self, case_id: str) -> None:
        assert not is_human_kept_gate(CASES[case_id].task)

    def test_exact_purposes(self) -> None:
        def purpose(case_id: str) -> str:
            case = CASES[case_id]
            return next_commands(case.task, session=case.session())[0].purpose

        assert purpose("rule0_signoff") == (
            "operator-only human gate; not for an agent to run: clear the"
            " operator-signoff gate so the ticket ships"
        )
        assert purpose("rule0_finalize_gate_held") == (
            "operator-only human gate; not for an agent to run: release the"
            " operator-set finalize hold so the ticket finalizes"
        )
        assert purpose("rule0_scope_hint_large_approval_gate") == (
            "operator-only human gate; not for an agent to run: release the"
            " review approval gate the operator set with scope_hint: large"
        )
        assert f"{HUMAN_GATE_LABEL}: " == HUMAN_GATE_PURPOSE_LABEL
        # #2760 purposes, pinned verbatim.
        assert purpose("rule0_finalize_held_no_session") == _P3
        terminal = PipelinePosition.TERMINAL
        large_final = _large_final()
        assert _first(large_final, _gate_session(large_final), terminal).purpose == _P2
        held = make_blocked(stage=Stage.FINALIZE, disposition=_HELD)
        assert _first(held, _gate_session(held), terminal).purpose == _P2_HOLD
        assert _first(large_final, None, terminal).purpose == _P3T
        gate_row = make_blocked(stage=Stage.FINALIZE)
        assert _first(gate_row, _gate_session(gate_row), terminal).purpose == _P1
        off = PipelinePosition.OFF_PIPELINE
        off_cmds = next_commands(gate_row, session=None, pipeline_position=off)
        assert [c.purpose for c in off_cmds] == [_P4, _P5]
        signoff = make_task(
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, stage=Stage.FINALIZE
        )
        signoff_cmds = next_commands(signoff, session=None, pipeline_position=off)
        assert [c.purpose for c in signoff_cmds] == [_P4, _P6]

    def test_human_kept_refusal_suffix_text(self) -> None:
        case = CASES["rule0_finalize_held_no_session"]
        assert refusal_suffix(case.task, session=None) == _S4

    def test_name_valued_human_gate_row_lists_the_labelled_requeue(
        self, tmp_path: Path
    ) -> None:
        # R9 decision: a legacy session-name row resolves to no session here
        # and is treated as sessionless. approve would find the session by
        # name, but show cannot see it, so it is not listed (documented
        # residual; completeness is not claimed).
        task = make_blocked(
            stage=Stage.REVIEW,
            disposition=FINALIZE_GATE_HELD_DISPOSITION,
            session_id="acme/auto-dev/ACM-1",
        )
        seed_queue([task], [bound_session_for(task, id="real-id")])
        resolved = resolve_session_for_task(task, load_state())
        assert resolved is None
        cmds = next_commands(task, session=resolved)
        assert [c.command for c in cmds] == [cmd("requeue", "--stage review")]
        assert cmds[0].purpose == _P3

    def test_sessionless_human_gate_lists_a_requeue_approve_refuses(
        self, tmp_path: Path
    ) -> None:
        write_lane_client(_C, tmp_path / "ws")
        case = CASES["rule0_finalize_held_no_session"]
        seed_queue([case.task])
        with pytest.raises(ApproveGateError, match="session not found"):
            approve_ticket(_T, _C)
        assert _commands(case) == [cmd("requeue", "--stage review")]
        precheck_requeue(_T, _C, "review", native_daemon=FakeNativeDaemonClient())


class TestMustFixShipStepOptIn:
    """R5: rules 6/6a drop the finalize jump for an opted-in row."""

    @pytest.mark.parametrize("case_id", _SHIP_DROPPED_CASES)
    def test_ship_entry_dropped(self, case_id: str) -> None:
        case = CASES[case_id]
        for opt_in in (False, True):
            cmds = next_commands(case.task, session=None, operator_opt_in=opt_in)
            assert not [c for c in cmds if "--stage finalize" in c.command]
            assert not [c for c in cmds if _verb(c.command) == "requeue"]
            assert [c.command for c in cmds] == case.expected

    @pytest.mark.parametrize("case_id", _PLAIN_SHIP_CASES)
    def test_operator_opt_in_seam(self, case_id: str) -> None:
        task = CASES[case_id].task
        held = next_commands(task, session=None, operator_opt_in=True)
        assert [c.command for c in held] == [
            cmd("approve", '--override-must-fix --reason "<why>"')
        ]
        assert "the human's step" in held[0].purpose
        default = next_commands(task, session=None)
        ship = [c for c in default if "--stage finalize" in c.command]
        assert len(ship) == 1
        assert ship[0].purpose.endswith(_SHIP_CAVEAT)

    @pytest.mark.parametrize(
        "case_id",
        sorted(
            case_id
            for case_id, case in CASES.items()
            if case.task.blocked_reason != CODEX_MUST_FIX_FINDINGS
            or row_carries_human_opt_in(case.task)
        ),
    )
    def test_opt_in_changes_nothing_else(self, case_id: str) -> None:
        case = CASES[case_id]
        session = case.session()
        assert next_commands(case.task, session=session, operator_opt_in=True) == (
            next_commands(case.task, session=session)
        )

    def test_held_purposes(self) -> None:
        overridden = next_commands(
            CASES["rule6a_scope_hint_large_ship_dropped"].task, session=None
        )
        assert overridden[0].purpose == (
            "re-record the MUST_FIX override if the verdict changed; it ships"
            " nothing, and shipping this ticket past review is the human's step"
            " because the ticket or its lane carries a finalize hold, operator"
            " signoff or scope_hint: large"
        )
        parked = next_commands(
            CASES["rule6_scope_hint_large_ship_dropped"].task, session=None
        )
        assert parked[0].purpose == (
            "record an operator override of the MUST_FIX park; it ships nothing,"
            " and shipping this ticket past review is the human's step because"
            " the ticket or its lane carries a finalize hold, operator signoff or"
            " scope_hint: large"
        )

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"scope_hint": "large"}, True),
            ({"hold_finalize": "manual"}, True),
            ({"signoff": "operator"}, True),
            ({"scope_hint": "small"}, False),
            ({"scope_hint": "medium"}, False),
            ({}, False),
        ],
    )
    def test_row_carries_human_opt_in(
        self, overrides: dict[str, object], expected: bool
    ) -> None:
        assert row_carries_human_opt_in(make_blocked(**overrides)) is expected


class TestRefusalSuffix:
    @pytest.mark.parametrize("case_id", sorted(CASES))
    def test_names_the_first_command(self, case_id: str) -> None:
        case = CASES[case_id]
        suffix = refusal_suffix(case.task, session=case.session())
        assert f"`{case.expected[0]}`" in suffix
        assert suffix.startswith(
            f" This row is {case.task.status.value} at stage {case.task.stage.value}"
        )

    @pytest.mark.parametrize("disposition", sorted(_GUIDED))
    def test_park_guidance(self, disposition: str) -> None:
        task = make_blocked(stage=Stage.REVIEW, disposition=disposition)
        suffix = refusal_suffix(task, session=None)
        assert f"(disposition '{disposition}'). {_GUIDED[disposition]}" in suffix

    @pytest.mark.parametrize(
        "disposition",
        ["ambiguities_pending_resolution", "premises_pending_verification"],
    )
    def test_plan_park_guidance_points_at_resolve(self, disposition: str) -> None:
        """#2702: a plan park names the row-recording verb."""
        task = make_blocked(stage=Stage.PLAN, disposition=disposition)
        suffix = refusal_suffix(task, session=None)
        assert "`cw dev-queue resolve --file <decisions.md>`" in suffix

    def test_exact_format_with_alternative(self) -> None:
        task = make_blocked(stage=Stage.REVIEW, disposition="review_health_gate")
        assert refusal_suffix(task, session=None) == (
            " This row is blocked_on_user at stage review (disposition"
            " 'review_health_gate'). Review health is degraded. This clears by"
            " re-running review, not by approving. Run: `cw dev-queue requeue"
            " ACM-1 -c acme` (release this ticket only). Alternatively: `cw"
            " dev-queue drain --held -c acme` (releases ALL held tickets of"
            " client acme, not just this one; add --dry-run to preview)."
        )

    def test_no_disposition_clause_when_none(self) -> None:
        suffix = refusal_suffix(_pending_task(), session=None)
        assert suffix.startswith(" This row is pending at stage plan. Run: ")

    def test_skip_drops_by_verb_token(self) -> None:
        case = CASES["rule9_review_gate"]
        suffix = refusal_suffix(case.task, session=case.session(), skip="approve")
        assert cmd("approve") not in suffix
        assert f"Run: `{cmd('requeue', '--stage review')}`" in suffix

    def test_skip_drops_by_flag_token(self) -> None:
        case = CASES["rule6_must_fix_no_session"]
        suffix = refusal_suffix(case.task, session=None, skip="--override-must-fix")
        assert "--override-must-fix" not in suffix
        assert f"Run: `{cmd('requeue', '--stage finalize')}`" in suffix

    @pytest.mark.parametrize(
        "case_id", ["rule10_plain_blocked_no_session", "rule0_signoff"]
    )
    def test_skipping_everything_falls_back_to_show(self, case_id: str) -> None:
        case = CASES[case_id]
        skip = _verb(case.expected[0])
        suffix = refusal_suffix(case.task, session=case.session(), skip=skip)
        assert suffix.endswith(
            f"Run: `{cmd('show')}` (inspect the row and its legal next commands)."
        )
        assert "operator-only" not in suffix

    def test_ticket_id_equal_to_skip_token_is_not_matched(self) -> None:
        task = _make_ticket_task(
            ticket_id="requeue",
            client=_C,
            status=QueueItemStatus.BLOCKED_ON_USER,
            stage=Stage.PLAN,
        )
        suffix = refusal_suffix(task, session=None, skip="requeue")
        assert "cw dev-queue show requeue -c acme" in suffix
        suffix = refusal_suffix(task, session=None, skip="approve")
        assert "cw dev-queue requeue requeue -c acme" in suffix


def _pending_task() -> TicketTask:
    return _make_ticket_task(ticket_id=_T, client=_C, status=QueueItemStatus.PENDING)


class TestCompactCommand:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            (cmd("approve"), "approve"),
            (cmd("requeue", "--stage finalize"), "requeue --stage finalize"),
            (cmd("approve", "--scope-drift <paths>"), "approve --scope-drift <paths>"),
            (
                cmd("approve", '--override-must-fix --reason "<why>"'),
                'approve --override-must-fix --reason "<why>"',
            ),
            (f"cw dev-queue drain --held -c {_C}", "drain --held -c acme"),
        ],
    )
    def test_strips_prefix_and_row_tokens(self, command: str, expected: str) -> None:
        assert compact_command(_pending_task(), command) == expected

    def test_shell_quoted_ids_round_trip(self) -> None:
        task = _make_ticket_task(ticket_id="ACM-1.x", client="my client")
        command = dev_queue_command("requeue", task, "--from-failed")
        assert compact_command(task, command) == "requeue --from-failed"


class TestSettledNotice:
    def test_worked_example(self) -> None:
        task = make_blocked(stage=Stage.REVIEW, disposition="approval_gate")
        assert human_gate_settled_notice(task) == (
            "Cannot approve ticket 'ACM-1': its recorded sentinel was just routed"
            " and the row now sits at a human-kept gate: it is blocked_on_user at"
            " stage review (disposition 'approval_gate'). Nothing was released."
            " operator-only human gate; not for an agent to run: the human"
            " operator reviews it with `cw dev-queue show ACM-1 -c acme` and"
            " releases it with a second `cw dev-queue approve ACM-1 -c acme`."
        )

    def test_signoff_and_no_disposition(self) -> None:
        task = _pending_task().model_copy(
            update={"status": QueueItemStatus.AWAITING_OPERATOR_SIGNOFF}
        )
        notice = human_gate_settled_notice(task)
        assert "it is awaiting_operator_signoff at stage plan. Nothing" in notice
        assert "(disposition" not in notice
        assert f"Nothing was released. {HUMAN_GATE_PURPOSE_LABEL}" in notice
        assert "Run:" not in notice
        assert "cw dev-queue requeue" not in notice


_IMPORT_FIRST = (
    "cw.dev_queue.next_commands",
    "cw.dev_queue.gates",
    "cw.dev_queue.refusal",
    "cw.dev_queue.requeue",
    "cw.dev_queue.approval",
    "cw.dev_queue.drain",
    "cw.dev_queue.scope_drift",
    "cw.dev_queue.dispositions",
)


def _import_in_fresh_interpreter(module: str) -> None:
    subprocess.run(
        [sys.executable, "-I", "-c", f"import {module}"],
        check=True,
        env=os.environ.copy(),
    )


class TestImportOrder:
    """R2: each module imports cleanly as the first ``cw`` import."""

    @pytest.mark.parametrize("module", _IMPORT_FIRST)
    def test_imports_first_without_a_cycle(self, module: str) -> None:
        _import_in_fresh_interpreter(module)


_RESUME = f"cw lane resume {ACME} default"
_PENDING_CMDS = [cmd("wait"), cmd("cancel")]


class TestSkipReasonRemedy:
    """``skip_reason`` / ``lane_resume_command`` (#2693): PENDING lane pauses only."""

    @pytest.mark.parametrize("case_id", sorted(CASES))
    def test_no_skip_reason_is_unchanged(self, case_id: str) -> None:
        case = CASES[case_id]
        cmds = next_commands(
            case.task,
            session=case.session(),
            skip_reason=None,
            lane_resume_command=_RESUME,
        )
        assert [c.command for c in cmds] == case.expected

    @pytest.mark.parametrize("code", [LANE_CIRCUIT_PAUSED, LANE_PAUSED_SKIP_CODE])
    def test_pending_lane_pause_leads_with_resume(self, code: str) -> None:
        task = _make_ticket_task(ticket_id=_T, client=_C)
        cmds = next_commands(
            task, session=None, skip_reason=code, lane_resume_command=_RESUME
        )
        assert [c.command for c in cmds] == [_RESUME, *_PENDING_CMDS]
        assert cmds[0].purpose == LANE_RESUME_PURPOSE

    @pytest.mark.parametrize(
        "code",
        [
            "attempt_cap_blocked",
            "ssh_key_gate",
            "disk_pressure_gate",
            "tracker_mcp_gate_blocked",
            "stale_pr_blocked",
            "lane_cap_blocked",
        ],
    )
    def test_other_codes_gain_no_remedy(self, code: str) -> None:
        task = _make_ticket_task(ticket_id=_T, client=_C)
        cmds = next_commands(
            task, session=None, skip_reason=code, lane_resume_command=_RESUME
        )
        assert [c.command for c in cmds] == _PENDING_CMDS

    def test_lane_pause_without_resume_command_is_unchanged(self) -> None:
        task = _make_ticket_task(ticket_id=_T, client=_C)
        cmds = next_commands(task, session=None, skip_reason=LANE_CIRCUIT_PAUSED)
        assert [c.command for c in cmds] == _PENDING_CMDS

    def test_non_pending_rows_ignore_skip_reason(self) -> None:
        case = CASES["rule3_running"]
        cmds = next_commands(
            case.task,
            session=case.session(),
            skip_reason=LANE_CIRCUIT_PAUSED,
            lane_resume_command=_RESUME,
        )
        assert [c.command for c in cmds] == case.expected


# --- #2760: pipeline position, terminal and off-gate rows, the soundness walk


_DEFAULT_STAGES = [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
_NO_FINALIZE = ("plan", "impl", "review")
_TERMINAL = PipelinePosition.TERMINAL
_INTERIOR = PipelinePosition.INTERIOR
_OFF = PipelinePosition.OFF_PIPELINE


def _p4(stage: Stage) -> str:
    return _P4.replace("stage finalize", f"stage {stage.value}", 1)


def _position_for(task: TicketTask) -> PipelinePosition:
    return pipeline_position(task, get_client(_C))


class TestPipelinePosition:
    @pytest.mark.parametrize(
        ("stage", "stages", "expected"),
        [
            (Stage.FINALIZE, _DEFAULT_STAGES, _TERMINAL),
            (Stage.REVIEW, _DEFAULT_STAGES, _INTERIOR),
            (Stage.PLAN, _DEFAULT_STAGES, _INTERIOR),
            (Stage.HARDEN, _DEFAULT_STAGES, _OFF),
            (Stage.REVIEW, [], _OFF),
        ],
    )
    def test_stage_position(
        self, stage: Stage, stages: list[Stage], expected: PipelinePosition
    ) -> None:
        assert stage_position(stage, stages) is expected

    def test_default_pipeline(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws")
        assert _position_for(make_blocked(stage=Stage.FINALIZE)) is _TERMINAL
        assert _position_for(make_blocked(stage=Stage.REVIEW)) is _INTERIOR
        assert _position_for(make_blocked(stage=Stage.HARDEN)) is _OFF

    def test_lane_stages_override_wins(self, tmp_path: Path) -> None:
        write_lane_client(
            _C,
            tmp_path / "ws",
            lane_stages={DEFAULT_LANE: _NO_FINALIZE},
            client_stages=("plan", "impl", "review", "finalize"),
        )
        assert _position_for(make_blocked(stage=Stage.REVIEW)) is _TERMINAL
        assert _position_for(make_blocked(stage=Stage.FINALIZE)) is _OFF

    def test_executor_only_lane_inherits_client_stages(self, tmp_path: Path) -> None:
        from cw.models import CLAUDE_NATIVE_BACKEND
        from tests._clients_yaml import ClientSpec, write_clients_yaml

        lane = {
            "name": DEFAULT_LANE,
            "pipeline": {"executors": {"review": {"backend": CLAUDE_NATIVE_BACKEND}}},
        }
        write_clients_yaml(
            ClientSpec(
                _C,
                tmp_path / "ws",
                lanes=[lane],
                extra={"pipeline": {"stages": list(_NO_FINALIZE)}},
            ),
            ensure_workspaces=True,
        )
        assert _position_for(make_blocked(stage=Stage.REVIEW)) is _TERMINAL

    def test_no_client_is_interior(self) -> None:
        assert pipeline_position(make_blocked(stage=Stage.FINALIZE), None) is (
            _INTERIOR
        )

    def test_exported_from_executor(self) -> None:
        assert "pipeline_position" in cw.executor.__all__
        assert pipeline_position is _resolve_pipeline_position


class TestTerminalStageGate:
    """Rule 9 and rules 0a/0b at the last stage of the lane pipeline."""

    @pytest.mark.parametrize("disposition", [None, "review_pending_approval"])
    def test_rule9_lists_only_the_requeue(self, disposition: str | None) -> None:
        task = make_blocked(stage=Stage.FINALIZE, disposition=disposition)
        cmds = next_commands(
            task, session=_gate_session(task), pipeline_position=_TERMINAL
        )
        assert [c.command for c in cmds] == [cmd("requeue", "--stage finalize")]
        assert cmds[0].purpose == _P1
        assert cmds[0].operator_only is False
        assert not [c for c in cmds if _verb(c.command) == "approve"]

    def test_human_terminal_at_the_gate_releases_and_ships(self) -> None:
        task = _large_final()
        cmds = next_commands(
            task, session=_gate_session(task), pipeline_position=_TERMINAL
        )
        assert [c.command for c in cmds] == [cmd("requeue", "--stage finalize")]
        assert cmds[0].purpose == _P2
        assert cmds[0].operator_only is True
        assert "re-run" not in cmds[0].purpose
        assert is_human_kept_gate(task)

    def test_finalize_hold_terminal(self) -> None:
        task = make_blocked(stage=Stage.FINALIZE, disposition=_HELD)
        assert _first(task, _gate_session(task), _TERMINAL).purpose == _P2_HOLD

    @pytest.mark.parametrize("last_result", [None, _STAGE_COMPLETE])
    def test_human_terminal_off_the_gate(
        self, last_result: dict[str, object] | None
    ) -> None:
        task = _large_final()
        session = bound_session_for(task, last_result=last_result)
        for bound in (session, None):
            cmds = next_commands(task, session=bound, pipeline_position=_TERMINAL)
            assert [c.command for c in cmds] == [cmd("requeue", "--stage finalize")]
            assert cmds[0].purpose == _P3T
            assert cmds[0].operator_only is True
        review = _large_final(stage=Stage.REVIEW)
        interior = next_commands(
            review, session=bound_session_for(review, last_result=last_result)
        )
        assert [c.purpose for c in interior] == [_P3]

    def test_signoff_at_the_terminal_stage_stays_approvable(self) -> None:
        task = make_task(
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, stage=Stage.FINALIZE
        )
        cmds = next_commands(task, session=None, pipeline_position=_TERMINAL)
        assert [c.command for c in cmds] == [cmd("approve")]

    @pytest.mark.parametrize(
        "case_id",
        sorted(c for c in CASES if c.startswith(("rule5", "rule6", "rule7", "rule8"))),
    )
    def test_rules_5_to_8_unchanged(self, case_id: str) -> None:
        case = CASES[case_id]
        session = case.session()
        assert next_commands(
            case.task, session=session, pipeline_position=_TERMINAL
        ) == next_commands(case.task, session=session)

    def test_harden_terminal_has_no_stage_flag(self) -> None:
        task = make_blocked(stage=Stage.HARDEN, disposition="approval_gate")
        cmds = next_commands(
            task, session=bound_session_for(task), pipeline_position=_TERMINAL
        )
        assert [c.command for c in cmds] == [cmd("requeue")]

    def test_default_position_is_byte_identical(self) -> None:
        task = make_blocked(stage=Stage.FINALIZE)
        cmds = next_commands(task, session=_gate_session(task))
        assert [c.command for c in cmds] == [
            cmd("approve"),
            cmd("requeue", "--stage finalize"),
        ]


class TestHumanGateOffGate:
    """Rule 0a (R5 B, R9): a human-kept row approve cannot take lists a requeue."""

    @pytest.mark.parametrize("last_result", [None, _STAGE_COMPLETE])
    def test_session_off_the_gate(self, last_result: dict[str, object] | None) -> None:
        task = _large_final(stage=Stage.REVIEW)
        cmds = next_commands(
            task, session=bound_session_for(task, last_result=last_result)
        )
        assert [c.command for c in cmds] == [cmd("requeue", "--stage review")]
        assert cmds[0].purpose == _P3
        plan = make_blocked(
            stage=Stage.PLAN, scope_hint="large", disposition="plan_pending_approval"
        )
        plan_cmds = next_commands(
            plan, session=bound_session_for(plan, last_result=last_result)
        )
        assert [c.command for c in plan_cmds] == [cmd("requeue", "--stage plan")]
        assert "re-run the plan stage" in plan_cmds[0].purpose

    @pytest.mark.parametrize(
        "overrides",
        [
            {"scope_hint": "large", "disposition": "review_pending_approval"},
            {"scope_hint": "large", "disposition": "approval_gate"},
            {"disposition": _HELD},
        ],
    )
    def test_no_session(self, overrides: dict[str, object]) -> None:
        task = make_blocked(stage=Stage.REVIEW, **overrides)
        cmds = next_commands(task, session=None)
        assert [c.command for c in cmds] == [cmd("requeue", "--stage review")]
        assert cmds[0].purpose == _P3

    def test_session_at_the_gate_keeps_approve(self) -> None:
        task = _large_final(stage=Stage.REVIEW)
        cmds = next_commands(task, session=_gate_session(task))
        assert [c.command for c in cmds] == [cmd("approve")]
        assert cmds[0].purpose == (
            f"{_LABEL}release the review approval gate the operator set with"
            " scope_hint: large"
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"scope_hint": "large", "disposition": "approval_gate"},
            {"disposition": _HELD},
        ],
    )
    def test_disposition_satisfies_the_gate(self, overrides: dict[str, object]) -> None:
        task = make_blocked(stage=Stage.REVIEW, **overrides)
        cmds = next_commands(task, session=bound_session_for(task, last_result=None))
        assert [c.command for c in cmds] == [cmd("approve")]

    def test_signoff_is_always_approvable(self) -> None:
        task = make_task(
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            stage=Stage.REVIEW,
            session_id="sess-1",
        )
        for session in (bound_session_for(task, last_result=None), None):
            cmds = next_commands(task, session=session)
            assert [c.command for c in cmds] == [cmd("approve")]

    def test_real_guards(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws")
        task = _large_final(stage=Stage.REVIEW)
        session = bound_session_for(task, last_result=_STAGE_COMPLETE)
        seed_queue([task], [session])
        with pytest.raises(ApproveGateError, match="not at an approval gate") as exc:
            approve_ticket(_T, _C)
        assert str(exc.value).endswith(_S3)
        precheck_requeue(_T, _C, "review", native_daemon=FakeNativeDaemonClient())


_OFF_PLAIN_ROWS = {
    "rule9_harden": make_blocked(stage=Stage.HARDEN, disposition="approval_gate"),
    "rule9_finalize": make_blocked(stage=Stage.FINALIZE),
    "rule6_must_fix": CASES["rule6_must_fix_no_session"].task,
    "rule7_salvage": CASES["rule7_salvage_parked"].task,
    "rule10_sessionless": CASES["rule10_plain_blocked_no_session"].task,
    "rule11_generic": CASES["rule11_unknown_disposition"].task,
}
_OFF_HUMAN_CASES = (
    "rule0_signoff",
    "rule0_finalize_held_no_session",
    "rule0_scope_hint_large_no_session",
    "rule0_finalize_gate_held",
)


class TestOffPipelineGate:
    """R12: a row whose stage is not in its lane pipeline lists show + requeue."""

    @pytest.mark.parametrize("row_id", sorted(_OFF_PLAIN_ROWS))
    def test_plain_rows(self, row_id: str) -> None:
        task = _OFF_PLAIN_ROWS[row_id]
        session = bound_session_for(
            task, last_result=REVIEW_PENDING, reap_reason=ReapReason.SALVAGE_PARKED
        )
        cmds = next_commands(task, session=session, pipeline_position=_OFF)
        assert [c.command for c in cmds] == [cmd("show"), cmd("requeue")]
        assert [c.purpose for c in cmds] == [_p4(task.stage), _P5]
        assert [c.operator_only for c in cmds] == [False, False]

    @pytest.mark.parametrize("case_id", _OFF_HUMAN_CASES)
    def test_human_rows(self, case_id: str) -> None:
        case = CASES[case_id]
        cmds = next_commands(case.task, session=case.session(), pipeline_position=_OFF)
        assert [c.command for c in cmds] == [cmd("show"), cmd("requeue")]
        assert [c.purpose for c in cmds] == [_p4(case.task.stage), _P6]
        assert [c.operator_only for c in cmds] == [False, True]

    @pytest.mark.parametrize(
        "case_id",
        ["rule2_pending", "rule3_running", "rule4_completed", "rule4_failed"],
    )
    def test_other_statuses_unchanged(self, case_id: str) -> None:
        case = CASES[case_id]
        cmds = next_commands(case.task, session=case.session(), pipeline_position=_OFF)
        assert [c.command for c in cmds] == case.expected

    def test_real_guards_on_a_default_pipeline_harden_row(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws")
        task = _OFF_PLAIN_ROWS["rule9_harden"]
        seed_queue([task], [bound_session_for(task)])
        assert _position_for(task) is _OFF
        with pytest.raises(ApproveGateError, match="not in pipeline") as exc:
            approve_ticket(_T, _C)
        assert str(exc.value).endswith(" A plain `requeue` re-runs the current stage.")
        daemon = FakeNativeDaemonClient()
        for stage in ("review", "harden"):
            with pytest.raises(RequeueStageError):
                precheck_requeue(_T, _C, stage, native_daemon=daemon)
        precheck_requeue(_T, _C, native_daemon=daemon)


class TestConfigResolvedPosition:
    """The position comes from the resolved lane pipeline, not ``stage == FINALIZE``."""

    @pytest.mark.parametrize("lane_override", [False, True])
    def test_terminal_finalize(self, tmp_path: Path, lane_override: bool) -> None:
        stages = ("plan", "impl", "review", "finalize")
        if lane_override:
            write_lane_client(
                _C,
                tmp_path / "ws",
                lane_stages={DEFAULT_LANE: stages},
                client_stages=("plan", "impl"),
            )
        else:
            write_lane_client(_C, tmp_path / "ws")
        task = make_blocked(stage=Stage.FINALIZE)
        session = _gate_session(task)
        seed_queue([task], [session])
        cmds = next_commands(
            task, session=session, pipeline_position=_position_for(task)
        )
        assert cmds[0].command == cmd("requeue", "--stage finalize")
        precheck_requeue(_T, _C, "finalize", native_daemon=FakeNativeDaemonClient())
        with pytest.raises(ApproveGateError, match="terminal stage"):
            approve_ticket(_T, _C)

    def test_lane_a_review_is_terminal(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws", lane_stages={DEFAULT_LANE: _NO_FINALIZE})
        task = make_blocked(stage=Stage.REVIEW)
        session = _gate_session(task)
        seed_queue([task], [session])
        cmds = next_commands(
            task, session=session, pipeline_position=_position_for(task)
        )
        assert [c.command for c in cmds] == [cmd("requeue", "--stage review")]
        with pytest.raises(ApproveGateError, match="terminal stage"):
            approve_ticket(_T, _C)

    def test_lane_b_review_is_interior(self, tmp_path: Path) -> None:
        write_lane_client(
            _C,
            tmp_path / "ws",
            lane_stages={DEFAULT_LANE: ("plan", "impl", "review", "finalize")},
            client_stages=("plan", "impl"),
        )
        task = make_blocked(stage=Stage.REVIEW)
        session = _gate_session(task)
        seed_queue([task], [session])
        position = _position_for(task)
        assert position is _INTERIOR
        cmds = next_commands(task, session=session, pipeline_position=position)
        assert cmds[0].command == cmd("approve")
        approve_ticket(_T, _C)

    def test_lane_c_finalize_is_off_pipeline(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws", lane_stages={DEFAULT_LANE: _NO_FINALIZE})
        task = make_blocked(stage=Stage.FINALIZE)
        session = _gate_session(task)
        seed_queue([task], [session])
        cmds = next_commands(
            task, session=session, pipeline_position=_position_for(task)
        )
        assert [c.command for c in cmds] == [cmd("show"), cmd("requeue")]
        with pytest.raises(ApproveGateError, match="not in pipeline"):
            approve_ticket(_T, _C)
        daemon = FakeNativeDaemonClient()
        with pytest.raises(RequeueStageError):
            precheck_requeue(_T, _C, "finalize", native_daemon=daemon)
        precheck_requeue(_T, _C, native_daemon=daemon)


_POSITIONS = (_INTERIOR, _TERMINAL, _OFF)
_RELEASE_VERBS = ("approve", "requeue")


class TestOperatorOnlyFlag:
    """R11: ``operator_only`` mirrors the purpose label one-to-one."""

    @pytest.mark.parametrize("case_id", sorted(CASES))
    def test_flag_iff_label(self, case_id: str) -> None:
        case = CASES[case_id]
        human = is_human_kept_gate(case.task)
        for position in _POSITIONS:
            cmds = next_commands(
                case.task, session=case.session(), pipeline_position=position
            )
            for c in cmds:
                assert c.operator_only == c.purpose.startswith(_LABEL)
                if human and _verb(c.command) in _RELEASE_VERBS:
                    assert is_human_gate_command(case.task, c)
                if human and position is not _OFF:
                    assert c.operator_only

    def test_model_default(self) -> None:
        assert NextCommand(command="x", purpose="y").operator_only is False
        props = NextCommand.model_json_schema()["properties"]
        assert props["operator_only"]["default"] is False


class TestIsHumanGateCommand:
    """R15: the row state decides; the flag is only an extra trigger."""

    _HUMAN = CASES["rule0_finalize_gate_held"].task
    _PLAIN = CASES["rule9_review_gate"].task

    @pytest.mark.parametrize(
        ("verb", "expected"), [("approve", True), ("requeue", True), ("show", False)]
    )
    def test_bare_command_on_a_human_row(self, verb: str, expected: bool) -> None:
        bare = NextCommand(command=cmd(verb), purpose="x")
        assert is_human_gate_command(self._HUMAN, bare) is expected

    def test_bare_approve_on_a_plain_row_is_not(self) -> None:
        bare = NextCommand(command=cmd("approve"), purpose="x")
        assert is_human_gate_command(self._PLAIN, bare) is False

    def test_flag_is_an_extra_trigger(self) -> None:
        flagged = NextCommand(command=cmd("show"), purpose="x", operator_only=True)
        assert is_human_gate_command(self._PLAIN, flagged) is True


class TestPositionRefusalSuffix:
    """``refusal_suffix`` with a caller-resolved position (S1-S6)."""

    def test_s1_rule9_terminal(self) -> None:
        task = make_blocked(stage=Stage.FINALIZE, disposition="review_pending_approval")
        suffix = refusal_suffix(
            task,
            session=_gate_session(task),
            skip="approve",
            pipeline_position=_TERMINAL,
        )
        assert suffix == _S1
        assert all(_verb(span) != "approve" for span in _spans(suffix))
        assert "Alternatively:" not in suffix
        bare = make_blocked(stage=Stage.FINALIZE)
        assert refusal_suffix(
            bare,
            session=_gate_session(bare),
            skip="approve",
            pipeline_position=_TERMINAL,
        ) == _S1.replace(" (disposition 'review_pending_approval')", "", 1)

    def test_s2_human_terminal(self) -> None:
        task = _large_final()
        suffix = refusal_suffix(
            task,
            session=_gate_session(task),
            skip="approve",
            pipeline_position=_TERMINAL,
        )
        assert suffix == _S2

    def test_s3_off_gate_interior(self) -> None:
        task = _large_final(stage=Stage.REVIEW)
        session = bound_session_for(task, last_result=_STAGE_COMPLETE)
        assert refusal_suffix(task, session=session) == _S3

    def test_s4_sessionless_interior(self) -> None:
        task = make_blocked(stage=Stage.REVIEW, disposition=_HELD)
        assert refusal_suffix(task, session=None) == _S4

    def test_s5_sessionless_terminal(self) -> None:
        task = make_blocked(stage=Stage.FINALIZE, disposition=_HELD)
        suffix = refusal_suffix(task, session=None, pipeline_position=_TERMINAL)
        assert suffix == _S5

    def test_s6_off_pipeline_plain(self) -> None:
        task = make_blocked(stage=Stage.FINALIZE, disposition="review_pending_approval")
        suffix = refusal_suffix(
            task, session=_gate_session(task), pipeline_position=_OFF
        )
        assert suffix == _S6

    def test_off_pipeline_human(self) -> None:
        task = make_blocked(stage=Stage.FINALIZE, disposition=_HELD)
        suffix = refusal_suffix(task, session=None, pipeline_position=_OFF)
        assert f"Run: `{cmd('show')}`" in suffix
        assert f"Alternatively: `{cmd('requeue')}` ({_LABEL}" in suffix

    @pytest.mark.parametrize("case_id", sorted(CASES))
    def test_default_position_is_identical(self, case_id: str) -> None:
        case = CASES[case_id]
        session = case.session()
        assert refusal_suffix(case.task, session=session) == refusal_suffix(
            case.task, session=session, pipeline_position=_INTERIOR
        )


class TestHumanKeptTerminal:
    """R14: a human-kept terminal requeue releases and ships; off-gate re-runs."""

    def _terminal_rows(self) -> list[tuple[TicketTask, Session | None]]:
        large = _large_final()
        held = make_blocked(stage=Stage.FINALIZE, disposition=_HELD)
        return [
            (large, _gate_session(large)),
            (held, _gate_session(held)),
            (large, None),
            (large, bound_session_for(large, last_result=_STAGE_COMPLETE)),
        ]

    def test_terminal_rows(self) -> None:
        for task, session in self._terminal_rows():
            cmds = next_commands(task, session=session, pipeline_position=_TERMINAL)
            assert [c.command for c in cmds] == [cmd("requeue", "--stage finalize")]
            assert cmds[0].purpose.startswith(_LABEL)
            suffix = refusal_suffix(task, session=session, pipeline_position=_TERMINAL)
            assert "Run:" not in suffix
            assert "operator-only" in suffix
            assert "release command" in suffix
            assert "re-run command" not in suffix

    def test_interior_off_gate_rows(self) -> None:
        review = _large_final(stage=Stage.REVIEW)
        rows: list[tuple[TicketTask, Session | None]] = [
            (review, bound_session_for(review, last_result=_STAGE_COMPLETE)),
            *((CASES[c].task, None) for c in _HUMAN_REQUEUE_CASES),
        ]
        for task, session in rows:
            suffix = refusal_suffix(task, session=session)
            assert "re-run command" in suffix
            assert "release command" not in suffix

    def test_attention_command(self) -> None:
        assert human_gate_attention_command(make_blocked()) == (
            "HUMAN GATE: cw dev-queue show ACM-1 -c acme"
        )


# --- the exhaustive soundness walk (first acceptance bullet) -----------------

_WALK_DISPOSITIONS: tuple[str | None, ...] = (
    None,
    "approval_gate",
    FINALIZE_GATE_HELD_DISPOSITION,
    "review_pending_approval",
    "plan_pending_approval",
    "branch_behind_main",
    "review_artifacts_stale",
    "review_health_gate",
    "attempt_cap_blocked",
    "awaiting_operator",
    "some_future_park",
)
# (bound session present?, its last_result)
_WALK_SESSIONS: tuple[tuple[bool, dict[str, object] | None], ...] = (
    (False, None),
    (True, None),
    (True, PLAN_PENDING),
    (True, REVIEW_PENDING),
    (True, _STAGE_COMPLETE),
)
_WALK_PIPELINES: dict[str, tuple[str, ...] | None] = {
    "default": None,
    "lane_no_finalize": _NO_FINALIZE,
}
# blocked_reason axis: None everywhere, plus the stage's own blocker reason.
_WALK_REASONS: dict[Stage, tuple[str | None, ...]] = {
    Stage.REVIEW: (None, CODEX_MUST_FIX_FINDINGS),
    Stage.IMPL: (None, "plan_scope_drift"),
}
_WALK_OTHER_STATUSES = (
    QueueItemStatus.PENDING,
    QueueItemStatus.RUNNING,
    QueueItemStatus.COMPLETED,
    QueueItemStatus.FAILED,
    QueueItemStatus.CANCELLED,
    QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
)


def _walk_blocked_rows(stage: Stage) -> list[tuple[TicketTask, Session | None]]:
    rows: list[tuple[TicketTask, Session | None]] = []
    for reason in _WALK_REASONS.get(stage, (None,)):
        for disposition in _WALK_DISPOSITIONS:
            for scope_hint in (None, "large"):
                task = make_blocked(
                    stage=stage,
                    disposition=disposition,
                    scope_hint=scope_hint,
                    blocked_reason=reason,
                )
                for present, last_result in _WALK_SESSIONS:
                    session = (
                        bound_session_for(task, last_result=last_result)
                        if present
                        else None
                    )
                    rows.append((task, session))
    return rows


def _walk_other_rows() -> list[tuple[TicketTask, Session | None]]:
    rows: list[tuple[TicketTask, Session | None]] = []
    for status in _WALK_OTHER_STATUSES:
        for stage in Stage:
            task = make_task(status=status, stage=stage, session_id="sess-1")
            rows.append((task, None))
            rows.append((task, bound_session_for(task)))
    return rows


def _assert_labels(
    task: TicketTask, cmds: list[NextCommand], position: PipelinePosition
) -> None:
    """The human-label invariant (R2/R11/R14) over one walk row."""
    human = is_human_kept_gate(task)
    for c in cmds:
        assert c.operator_only == c.purpose.startswith(_LABEL)
        is_release = _verb(c.command) in _RELEASE_VERBS
        if human and is_release:
            assert is_human_gate_command(task, c)
        if (
            human
            and is_release
            and position is _TERMINAL
            and _verb(c.command) == "requeue"
        ):
            assert "release the" in c.purpose
            assert "and ship" in c.purpose
    if human and _verb(cmds[0].command) in _RELEASE_VERBS:
        assert cmds[0].purpose.startswith(_LABEL)
        assert cmds[0].operator_only is True


def _assert_complete(task: TicketTask, cmds: list[NextCommand], refusal: str) -> None:
    """Completeness where ``approve`` refuses: what replaces it is listed."""
    verbs = [_verb(c.command) for c in cmds]
    # Rules 6/6a outrank rule 9 and are unchanged at the terminal stage: they
    # never list a plain approve, a human opt-in drops their ship step by
    # design (R5), and that ship step is E1 (#2765) when FINALIZE is absent.
    if "terminal stage" in refusal and task.blocked_reason != CODEX_MUST_FIX_FINDINGS:
        assert "requeue" in verbs
    if "not in pipeline" in refusal:
        assert [c.command for c in cmds] == [cmd("show"), cmd("requeue")]
        assert cmds[1].operator_only is is_human_kept_gate(task)
    if is_human_kept_gate(task) and (
        "not at an approval gate" in refusal or "session not found" in refusal
    ):
        assert "approve" not in verbs


def _assert_mutating(
    task: TicketTask, cmds: list[NextCommand], parsed: list[_Parsed]
) -> None:
    """The mutating approve checks, last: each one changes the saved row."""
    plain_approve = any(
        p.verb == "approve"
        and "--scope-drift" not in p.flags
        and "--override-must-fix" not in p.flags
        for p in parsed
    )
    if plain_approve:
        approve_ticket(_T, _C)
        return
    if any(p.verb == "approve" and "--scope-drift" in p.flags for p in parsed):
        approve_scope_drift_ticket(_T, _C, ["a.py"])
        return
    try:
        approve_ticket(_T, _C)
    except CwError as exc:
        _assert_complete(task, cmds, str(exc))


def _walk_row(task: TicketTask, session: Session | None) -> None:
    save_state(CwState())
    seed_queue([task], [session] if session is not None else [])
    client = get_client(_C)
    stages = resolve_pipeline_stages(task, client)
    position = pipeline_position(task, client)
    cmds = next_commands(task, session=session, pipeline_position=position)
    parsed = [_parse(c.command) for c in cmds]
    _assert_labels(task, cmds, position)
    _assert_commands_legal(task, session, parsed, stages=stages)
    _assert_mutating(task, cmds, parsed)


def _label(task: TicketTask, session: Session | None) -> str:
    last = session.last_result if session is not None else "absent"
    return (
        f"{task.status.value}/{task.stage.value}/{task.disposition}/"
        f"{task.scope_hint}/{task.blocked_reason}/session={last}"
    )


def _walk(rows: list[tuple[TicketTask, Session | None]]) -> list[str]:
    failures: list[str] = []
    for task, session in rows:
        try:
            _walk_row(task, session)
        except (AssertionError, CwError) as exc:
            failures.append(f"{_label(task, session)}: {exc!r}")
    return failures


class TestEveryStagePairWalk:
    """Soundness: every command ``next_commands`` lists, its verb accepts.

    Exactly one named exception, E1 (#2765), pinned as refused in
    ``_assert_commands_legal``. The converse is not asserted: rule 6 rows
    with a gate session legitimately omit an approve that would succeed.
    """

    @pytest.fixture(autouse=True)
    def _hermetic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        monkeypatch.setattr(
            "cw.dev_queue.scope_drift.branch_head_sha_on_origin",
            lambda *_a, **_kw: (_SCOPE_DRIFT_HEAD, True),
        )

    @staticmethod
    def _write_pipeline(tmp_path: Path, pipeline: str) -> None:
        lane_stages = _WALK_PIPELINES[pipeline]
        write_lane_client(
            _C,
            tmp_path / "ws",
            lane_stages=None if lane_stages is None else {DEFAULT_LANE: lane_stages},
        )

    @pytest.mark.parametrize("stage", list(Stage))
    @pytest.mark.parametrize("pipeline", sorted(_WALK_PIPELINES))
    def test_blocked_rows(self, tmp_path: Path, pipeline: str, stage: Stage) -> None:
        self._write_pipeline(tmp_path, pipeline)
        failures = _walk(_walk_blocked_rows(stage))
        assert not failures, "\n".join(failures)

    @pytest.mark.parametrize("pipeline", sorted(_WALK_PIPELINES))
    def test_other_statuses(self, tmp_path: Path, pipeline: str) -> None:
        self._write_pipeline(tmp_path, pipeline)
        failures = _walk(_walk_other_rows())
        assert not failures, "\n".join(failures)
