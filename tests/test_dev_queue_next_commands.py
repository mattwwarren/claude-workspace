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
from cw.cli import main
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.config import load_state
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
    human_gate_settled_notice,
    is_human_kept_gate,
    next_commands,
    refusal_suffix,
    row_carries_human_opt_in,
)
from cw.exceptions import ApproveGateError
from cw.models import (
    QueueItemStatus,
    ReapReason,
    Stage,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile._shared import resolve_session_for_task
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    CASES,
    PLAN_PENDING,
    Case,
    bound_session_for,
    cmd,
    make_blocked,
    seed_queue,
    write_lane_client,
)
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask


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
        for case in CASES.values():
            assert next_commands(case.task, session=case.session())
        assert not [n for n in vars(next_commands_mod) if n.startswith("load_")]


_HUMAN_GATE_CASES = (
    "rule0_signoff",
    "rule0_finalize_gate_held",
    "rule0_finalize_held_no_session",
    "rule0_scope_hint_large_approval_gate",
    "rule0_scope_hint_large_plan_pending",
    "rule0_scope_hint_large_no_session",
)
_SESSIONLESS_HUMAN_GATE_CASES = (
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
_SESSIONLESS_CLAUSE = (
    "; the bound session is no longer recorded, so approve will refuse until the"
    " operator resolves that"
)
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


def _verb(command: str) -> str:
    return shlex.split(command)[2]


class TestHumanKeptGate:
    """R1: a human-kept gate lists only the labelled operator ``approve``."""

    @pytest.mark.parametrize("case_id", _HUMAN_GATE_CASES)
    def test_only_the_labelled_approve(self, case_id: str) -> None:
        case = CASES[case_id]
        cmds = next_commands(case.task, session=case.session())
        assert not [c for c in cmds if "requeue" in c.command]
        assert [_verb(c.command) for c in cmds] == ["approve"]
        assert cmds[0].purpose.startswith(HUMAN_GATE_PURPOSE_LABEL)
        suffix = refusal_suffix(case.task, session=case.session())
        assert "operator-only" in suffix
        assert "Run:" not in suffix
        assert "cw dev-queue requeue" not in suffix
        assert is_human_kept_gate(case.task)

    @pytest.mark.parametrize("case_id", sorted(set(CASES) - set(_HUMAN_GATE_CASES)))
    def test_no_other_case_is_human_kept(self, case_id: str) -> None:
        assert not is_human_kept_gate(CASES[case_id].task)

    @pytest.mark.parametrize("case_id", _HUMAN_GATE_CASES)
    def test_sessionless_clause_only_without_a_session(self, case_id: str) -> None:
        case = CASES[case_id]
        purpose = next_commands(case.task, session=case.session())[0].purpose
        if case_id in _SESSIONLESS_HUMAN_GATE_CASES:
            assert purpose.endswith(_SESSIONLESS_CLAUSE)
        else:
            assert _SESSIONLESS_CLAUSE not in purpose

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

    def test_human_kept_refusal_suffix_text(self) -> None:
        case = CASES["rule0_finalize_held_no_session"]
        assert refusal_suffix(case.task, session=None) == (
            " This row is blocked_on_user at stage review (disposition"
            " 'finalize_gate_held'). This is a human-kept gate the operator opted"
            " into: an agent must not approve, release or requeue it. Notify the"
            " human operator. The operator's release command is"
            f" `{cmd('approve')}` (operator-only; not for an agent to run). The"
            " bound session is no longer recorded, so approve cannot bind this"
            " release; the human operator must resolve it."
        )

    def test_name_valued_human_gate_row_still_lists_only_the_labelled_approve(
        self, tmp_path: Path
    ) -> None:
        # Documented residual (Adopted Assumption 19): approve would find the
        # session by name, but the listed command is still only approve.
        task = make_blocked(
            stage=Stage.REVIEW,
            disposition=FINALIZE_GATE_HELD_DISPOSITION,
            session_id="acme/auto-dev/ACM-1",
        )
        seed_queue([task], [bound_session_for(task, id="real-id")])
        resolved = resolve_session_for_task(task, load_state())
        assert resolved is None
        cmds = next_commands(task, session=resolved)
        assert [c.command for c in cmds] == [cmd("approve")]
        assert cmds[0].purpose.endswith(_SESSIONLESS_CLAUSE)

    def test_sessionless_human_gate_is_refused_by_approve(self, tmp_path: Path) -> None:
        # Documented divergence (R6, #2718): the labelled approve is listed
        # although the verb refuses a sessionless row at session-not-found.
        write_lane_client(_C, tmp_path / "ws")
        case = CASES["rule0_finalize_held_no_session"]
        seed_queue([case.task])
        with pytest.raises(ApproveGateError, match="session not found"):
            approve_ticket(_T, _C)
        assert _commands(case) == [cmd("approve")]


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
