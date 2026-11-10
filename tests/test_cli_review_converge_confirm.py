"""``record-round`` over a repo review engine pass, end to end (#3042, RFC 0017 I3).

Every case drives the real ``cw review converge record-round`` with
``--ledger <path>`` (the command never touches git). The filing cases run in
a cw session repo built on the shared ``_session_repo`` helper with a fake
``gh``; the rest run from an isolated *tmp_path* so no case writes into the
checkout's ``.cw/``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli.review.converge_confirm import prepare_engine_confirm, round_ran_engine
from cw.models import OrchestratorEventType
from cw.review_convergence import (
    CONFIRM_ENGINE_DEFER_RATIONALE,
    CONFIRM_ENGINE_PARK_RATIONALE,
    ConvergenceLedger,
    RoundInput,
    decide_confirm_verdict,
    load_ledger,
    round_verdict_path,
    verdict_ran_engine,
)
from cw.review_debt_filing import artifact_debt_entries
from cw.review_engine.constants import ENGINE_ROLE
from tests._cli_review_helpers import (
    _isolated_cwd,
    _session_repo,
    _write_session_context,
)
from tests._review_convergence_helpers import (
    DEFAULT_SHA,
    TICKET_ID,
    attestation_digest,
    consolidated_verdict,
    engine_confirm_payload,
    engine_confirm_verdict,
    engine_pass_ledger,
    fresh_ledger,
    record,
    round_finding,
    round_payload,
    settled_deferrals,
    stamp_dispositions,
    write_ledger,
)
from tests._review_debt_helpers import (
    _events_of,
    register_confirm_client,
    stub_run_debt_filing,
)
from tests._review_engine_helpers import engine_loop_verdict
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

    from cw.review_findings import ReviewVerdict
    from tests._review_debt_helpers import DebtFilingCall

_CLIENT = "acme"
_TICKET = "3042"
_REAL_FILE = "src/thing.py"
_FIXTURES = Path(__file__).parent / "fixtures"
_CREATE_STDOUT = (_FIXTURES / "gh_issue_create_stdout.txt").read_text(encoding="utf-8")
_AUTH_STDERR = (_FIXTURES / "gh_issue_create_stderr_auth.txt").read_text(
    encoding="utf-8"
)
_EVENT_KEYS = {"ticket_id", "client", "reason", "round", "reviewed_sha", "identities"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(autouse=True)
def _cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolated_cwd(monkeypatch, tmp_path)


@pytest.fixture
def filings(monkeypatch: pytest.MonkeyPatch) -> list[DebtFilingCall]:
    return stub_run_debt_filing(monkeypatch)


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
    fake = FakeGhCli().install(monkeypatch)
    fake.reply("gh", "label", "create")
    fake.reply("gh", "issue", "list", stdout="[]")
    fake.reply("gh", "issue", "create", stdout=_CREATE_STDOUT)
    return fake


@pytest.fixture
def repo(make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """A cw session repo whose context names the client and the ticket."""
    return _session_repo(make_git_repo, monkeypatch, client=_CLIENT, ticket_id=_TICKET)


def _ledger_file(root: Path) -> Path:
    return root / ".cw" / "review-convergence.json"


def _record(root: Path, payload: object, *extra: str) -> Result:
    return CliRunner().invoke(
        main,
        [
            "review",
            "converge",
            "record-round",
            "-",
            "--ledger",
            str(_ledger_file(root)),
            *extra,
        ],
        input=json.dumps(payload),
    )


def _report(result: Result) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def _stored(root: Path) -> ConvergenceLedger:
    ledger = load_ledger(_ledger_file(root))
    assert ledger is not None
    return ledger


def _engine_ledger(root: Path, *, ticket_id: str = TICKET_ID) -> ConvergenceLedger:
    ledger = engine_pass_ledger(ticket_id=ticket_id)
    write_ledger(root, ledger)
    return ledger


def _persisted(root: Path, round_number: int) -> dict[str, Any]:
    path = round_verdict_path(_ledger_file(root), round_number)
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return body


def _escalations() -> list[dict[str, object]]:
    return _events_of(OrchestratorEventType.REVIEW_CONFIRM_ESCALATED)


def _loop_payload(verdict: ReviewVerdict, roster: list[str]) -> dict[str, object]:
    return {
        "kind": "loop",
        "backend": "claude-native",
        "reviewed_sha": verdict.reviewed_sha,
        "roster": roster,
        "verdict": json.loads(verdict.model_dump_json()),
    }


class TestEngineFlag:
    def _fresh(self, root: Path) -> None:
        write_ledger(root, fresh_ledger(confirm="codex"))

    def test_an_engine_only_loop_verdict_sets_the_flag(self, tmp_path: Path) -> None:
        self._fresh(tmp_path)
        result = _record(
            tmp_path, _loop_payload(engine_loop_verdict(DEFAULT_SHA), ["anything"])
        )
        assert result.exit_code == 0, result.output
        assert _report(result)["decision"] == "confirm_required"
        assert _stored(tmp_path).rounds[-1].engine is True

    def test_a_mixed_agents_run_does_not(self, tmp_path: Path) -> None:
        self._fresh(tmp_path)
        verdict = engine_loop_verdict(DEFAULT_SHA, also=("Test Reviewer",))
        result = _record(tmp_path, _loop_payload(verdict, [ENGINE_ROLE]))
        assert result.exit_code == 0, result.output
        assert _stored(tmp_path).rounds[-1].engine is False

    def test_a_findings_payload_falls_back_to_the_roster(self, tmp_path: Path) -> None:
        self._fresh(tmp_path)
        payload = round_payload(
            "loop", "claude-native", DEFAULT_SHA, roster=[ENGINE_ROLE]
        )
        result = _record(tmp_path, payload)
        assert result.exit_code == 0, result.output
        assert _stored(tmp_path).rounds[-1].engine is True

    def test_a_findings_payload_with_another_roster_does_not(
        self, tmp_path: Path
    ) -> None:
        self._fresh(tmp_path)
        result = _record(tmp_path, round_payload("loop", "claude-native", DEFAULT_SHA))
        assert result.exit_code == 0, result.output
        assert _stored(tmp_path).rounds[-1].engine is False

    def test_a_confirm_payload_never_sets_it(
        self, tmp_path: Path, filings: list[DebtFilingCall]
    ) -> None:
        _engine_ledger(tmp_path)
        payload = engine_confirm_payload(engine_confirm_verdict())
        payload["roster"] = [ENGINE_ROLE]
        result = _record(tmp_path, payload)
        assert result.exit_code == 0, result.output
        assert _stored(tmp_path).rounds[-1].engine is False
        assert filings == []


class TestPark:
    def test_a_must_fix_parks_without_a_fix_cycle(
        self, tmp_path: Path, filings: list[DebtFilingCall]
    ) -> None:
        before = _engine_ledger(tmp_path)
        verdict = engine_confirm_verdict(must_fix=["Real bug"])
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        report = _report(result)
        assert report["decision"] == "stop_confirm_must_fix"
        assert report["reason"] == "confirm_must_fix"
        stored = _stored(tmp_path)
        assert stored.status == "stopped"
        assert stored.fix_cycles_used == before.fix_cycles_used
        assert stored.fixed == []
        assert round_verdict_path(_ledger_file(tmp_path), report["round"]).is_file()
        assert filings == []

    def test_the_event_and_friction_line(self, tmp_path: Path) -> None:
        _engine_ledger(tmp_path)
        _write_session_context(
            tmp_path, headless=True, client=_CLIENT, ticket_id=_TICKET
        )
        verdict = engine_confirm_verdict(must_fix=["Real bug"])
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        round_number = _report(result)["round"]
        assert (
            f"friction: confirm_escalated: confirm_must_fix round={round_number}"
            in result.stderr.splitlines()
        )
        (event,) = _escalations()
        assert set(event) == _EVENT_KEYS
        stored = _stored(tmp_path)
        assert event == {
            "ticket_id": _TICKET,
            "client": _CLIENT,
            "reason": "confirm_must_fix",
            "round": round_number,
            "reviewed_sha": DEFAULT_SHA,
            "identities": [i.audit_key() for i in stored.stop_identities],
        }

    def test_the_event_correlates_with_the_ticket(self, tmp_path: Path) -> None:
        from cw.events import read_events

        _engine_ledger(tmp_path)
        verdict = engine_confirm_verdict(must_fix=["Real bug"])
        result = _record(
            tmp_path,
            engine_confirm_payload(verdict),
            "-c",
            "other",
            "--ticket",
            "T-77",
        )
        assert result.exit_code == 0, result.output
        (event,) = [
            e
            for e in read_events()
            if e.type is OrchestratorEventType.REVIEW_CONFIRM_ESCALATED
        ]
        assert event.correlation_id == "T-77"
        assert event.payload["client"] == "other"

    def test_with_no_identity_the_client_is_null(self, tmp_path: Path) -> None:
        _engine_ledger(tmp_path)
        verdict = engine_confirm_verdict(must_fix=["Real bug"])
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        (event,) = _escalations()
        assert "client" in event
        assert event["client"] is None
        assert event["ticket_id"] == TICKET_ID

    def test_an_event_failure_is_friction_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.cli.review.converge_confirm.record_event", _boom)
        _engine_ledger(tmp_path)
        verdict = engine_confirm_verdict(must_fix=["Real bug"])
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        assert "friction: confirm_escalated_event_failed: OSError" in result.stderr
        assert _stored(tmp_path).status == "stopped"

    @pytest.mark.parametrize("rule", ["coverage.patch", "acceptance.criteria"])
    def test_a_never_deferrable_should_fix_alone_parks(
        self, tmp_path: Path, filings: list[DebtFilingCall], rule: str
    ) -> None:
        _engine_ledger(tmp_path)
        verdict = engine_confirm_verdict(should_fix=[("Untested path", rule)])
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        report = _report(result)
        assert report["decision"] == "stop_confirm_must_fix"
        (identity,) = _stored(tmp_path).stop_identities
        assert identity.severity == "SHOULD_FIX"
        assert filings == []
        (accepted,) = _persisted(tmp_path, report["round"])["accepted"]
        assert accepted["disposition"] == "operator_actionable"
        assert accepted["disposition_detail"] == CONFIRM_ENGINE_PARK_RATIONALE


class TestFindingsFormRefusal:
    def test_a_findings_confirm_after_an_engine_pass_records_nothing(
        self, tmp_path: Path, filings: list[DebtFilingCall]
    ) -> None:
        _engine_ledger(tmp_path)
        before = _ledger_file(tmp_path).read_bytes()
        out = tmp_path / "out-verdict.json"
        payload = round_payload(
            "confirm", "codex", DEFAULT_SHA, round_finding("MUST_FIX")
        )
        result = _record(tmp_path, payload, "--out-verdict", str(out))
        assert result.exit_code == 1
        assert "needs a verdict payload" in result.output
        assert _ledger_file(tmp_path).read_bytes() == before
        assert not round_verdict_path(_ledger_file(tmp_path), 2).exists()
        assert not out.exists()
        assert filings == []
        assert _escalations() == []

    def test_the_same_findings_on_a_non_engine_ledger_record(
        self, tmp_path: Path
    ) -> None:
        write_ledger(tmp_path, engine_pass_ledger(engine=False))
        payload = round_payload(
            "confirm", "codex", DEFAULT_SHA, round_finding("MUST_FIX")
        )
        result = _record(tmp_path, payload)
        assert result.exit_code == 0, result.output
        assert _report(result)["decision"] == "continue"


class TestVerdictDecisions:
    def test_a_dropped_must_fix_is_decided_not_refused(
        self, tmp_path: Path, filings: list[DebtFilingCall]
    ) -> None:
        _engine_ledger(tmp_path)
        verdict = stamp_dispositions(
            engine_confirm_verdict(must_fix=["Real bug"], should_fix=["Style"]),
            {"Real bug": ("dropped", "")},
        )
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        report = _report(result)
        assert report["decision"] == "stop_confirm_must_fix"
        by_summary = {
            a["finding"]["summary"]: a
            for a in _persisted(tmp_path, report["round"])["accepted"]
        }
        assert by_summary["Real bug"]["disposition"] == "fixed"
        assert by_summary["Style"]["disposition"] == "deferred"
        assert (
            by_summary["Style"]["disposition_detail"] == CONFIRM_ENGINE_DEFER_RATIONALE
        )
        (call,) = filings
        assert [a.summary for a in call.applied] == ["Style"]


class TestShouldFixOnly:
    def test_it_converges_and_files_one_built_in_key_ticket(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_confirm_client(repo, monkeypatch, name=_CLIENT, ticket=_TICKET)
        _engine_ledger(repo, ticket_id=_TICKET)
        unsaved = _ledger_file(repo).read_bytes()
        verdict = engine_confirm_verdict(should_fix=["Style nit"], file=_REAL_FILE)
        result = _record(repo, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        assert _report(result)["decision"] == "clean"
        stored = _stored(repo)
        assert stored.status == "converged"
        assert len(settled_deferrals(stored)) == 1
        creates = [a for a in cli.argvs() if a[:3] == ["gh", "issue", "create"]]
        assert len(creates) == 1
        (entry,) = artifact_debt_entries(repo / ".cw" / "deferred-findings.md")
        assert _SHA256.match(entry.key)
        assert entry.ticket
        # The unsaved-ledger re-run (a crash before the commit point) dedupes.
        _ledger_file(repo).write_bytes(unsaved)
        again = _record(repo, engine_confirm_payload(verdict))
        assert again.exit_code == 0, again.output
        creates = [a for a in cli.argvs() if a[:3] == ["gh", "issue", "create"]]
        assert len(creates) == 1

    def test_a_filing_failure_still_exits_zero(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_confirm_client(repo, monkeypatch, name=_CLIENT, ticket=_TICKET)
        cli.reply("gh", "issue", "create", returncode=1, stderr=_AUTH_STDERR)
        _engine_ledger(repo, ticket_id=_TICKET)
        verdict = engine_confirm_verdict(should_fix=["Style nit"], file=_REAL_FILE)
        result = _record(repo, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        assert "friction: debt_filing_failed: 1" in result.stderr
        assert _stored(repo).status == "converged"

    def test_a_session_without_a_client_skips_with_no_context(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _session_repo(make_git_repo, monkeypatch, ticket_id=_TICKET)
        _engine_ledger(root, ticket_id=_TICKET)
        verdict = engine_confirm_verdict(should_fix=["Style nit"], file=_REAL_FILE)
        result = _record(root, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        assert "friction: debt_filing: skipped_no_context" in result.stderr
        assert _stored(root).status == "converged"

    def test_outside_a_session_it_skips_with_no_session(self, tmp_path: Path) -> None:
        _engine_ledger(tmp_path)
        verdict = engine_confirm_verdict(should_fix=["Style nit"], file=_REAL_FILE)
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        assert "friction: debt_filing: skipped_no_session" in result.stderr
        assert _stored(tmp_path).status == "converged"
        assert not (tmp_path / ".cw" / "deferred-findings.md").exists()


class TestMixedAndClean:
    def test_a_mixed_round_files_then_parks(
        self, tmp_path: Path, filings: list[DebtFilingCall]
    ) -> None:
        _engine_ledger(tmp_path)
        verdict = engine_confirm_verdict(must_fix=["Real bug"], should_fix=["Style"])
        result = _record(tmp_path, engine_confirm_payload(verdict), "--ticket", "T-5")
        assert result.exit_code == 0, result.output
        assert _report(result)["decision"] == "stop_confirm_must_fix"
        (call,) = filings
        assert (call.client, call.ticket, call.artifact) == (None, "T-5", None)
        (adjudication,) = call.applied
        assert adjudication.outcome == "defer"
        assert adjudication.severity == "SHOULD_FIX"
        assert adjudication.rationale == CONFIRM_ENGINE_DEFER_RATIONALE
        assert adjudication.line_start is not None
        assert adjudication.evidence
        stored = _stored(tmp_path)
        assert stored.status == "stopped"
        assert len(settled_deferrals(stored)) == 1

    def test_a_clean_confirm_converges_with_an_unchanged_digest(
        self, tmp_path: Path, filings: list[DebtFilingCall]
    ) -> None:
        before = _engine_ledger(tmp_path)
        result = _record(tmp_path, engine_confirm_payload(engine_confirm_verdict()))
        assert result.exit_code == 0, result.output
        assert _report(result)["decision"] == "clean"
        stored = _stored(tmp_path)
        assert stored.status == "converged"
        assert attestation_digest(stored) == attestation_digest(before)
        assert filings == []


class TestNoEngineRegression:
    """R11.4: a non-engine claude-native ledger keeps today's behavior."""

    def _awaiting(self, root: Path) -> None:
        write_ledger(root, engine_pass_ledger(engine=False))

    def test_defer_should_fix_is_clean(self, tmp_path: Path) -> None:
        self._awaiting(tmp_path)
        verdict = consolidated_verdict(accepted=[("SHOULD_FIX", "Style")])
        result = _record(
            tmp_path, engine_confirm_payload(verdict), "--defer-should-fix"
        )
        assert result.exit_code == 0, result.output
        assert _report(result)["decision"] == "clean"

    def test_without_the_flag_a_should_fix_continues(self, tmp_path: Path) -> None:
        self._awaiting(tmp_path)
        verdict = consolidated_verdict(accepted=[("SHOULD_FIX", "Style")])
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        assert _report(result)["decision"] == "continue"

    def test_a_must_fix_continues(self, tmp_path: Path) -> None:
        self._awaiting(tmp_path)
        verdict = consolidated_verdict(accepted=[("MUST_FIX", "Real bug")])
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        assert _report(result)["decision"] == "continue"
        assert _escalations() == []


class TestPreRecordExits:
    def test_the_unread_guard_on_a_would_be_clean_engine_confirm(
        self, tmp_path: Path, filings: list[DebtFilingCall]
    ) -> None:
        _engine_ledger(tmp_path)
        before = _ledger_file(tmp_path).read_bytes()
        verdict = consolidated_verdict(
            accepted=[("SHOULD_FIX", "Style")], rejected_must_fix=True
        )
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 1
        assert "unread_findings" in result.output
        assert _ledger_file(tmp_path).read_bytes() == before
        assert not round_verdict_path(_ledger_file(tmp_path), 2).exists()
        assert filings == []

    def test_a_wrong_sha_confirm(self, tmp_path: Path) -> None:
        _engine_ledger(tmp_path)
        before = _ledger_file(tmp_path).read_bytes()
        verdict = engine_confirm_verdict(must_fix=["Real bug"], sha="sha-other")
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 1
        assert _ledger_file(tmp_path).read_bytes() == before
        assert _escalations() == []

    def test_an_undecided_finding_on_a_non_engine_ledger(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, engine_pass_ledger(engine=False))
        before = _ledger_file(tmp_path).read_bytes()
        verdict = stamp_dispositions(
            consolidated_verdict(accepted=[("MUST_FIX", "Real bug")]),
            {"Real bug": ("dropped", "")},
        )
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 1
        assert "undecided finding" in result.output
        assert _ledger_file(tmp_path).read_bytes() == before


def test_a_recorded_park_needs_no_adjudicate_step(tmp_path: Path) -> None:
    """The engine path and the pure engine agree on the same payload."""
    ledger = _engine_ledger(tmp_path)
    expected, report = record(
        ledger, round_finding("MUST_FIX", "Real bug"), sha=DEFAULT_SHA, kind="confirm"
    )
    assert report.decision == "stop_confirm_must_fix"
    verdict = engine_confirm_verdict(must_fix=["Real bug"])
    result = _record(tmp_path, engine_confirm_payload(verdict))
    assert result.exit_code == 0, result.output
    assert _stored(tmp_path).stop_identities == expected.stop_identities


class TestFilingSources:
    """``file_confirm_deferrals`` joins each deferral to its verdict finding."""

    def test_a_non_gating_finding_is_never_a_source(
        self, tmp_path: Path, filings: list[DebtFilingCall]
    ) -> None:
        _engine_ledger(tmp_path)
        verdict = consolidated_verdict(
            accepted=[("NIT", "Nit"), ("SHOULD_FIX", "Style")]
        )
        result = _record(tmp_path, engine_confirm_payload(verdict))
        assert result.exit_code == 0, result.output
        (call,) = filings
        assert [a.summary for a in call.applied] == ["Style"]

    def test_a_verdictless_payload_files_without_line_or_evidence(
        self, filings: list[DebtFilingCall]
    ) -> None:
        from cw.cli.review.converge_confirm import file_confirm_deferrals
        from cw.review_convergence import RoundInput

        payload = RoundInput.model_validate(
            round_payload(
                "confirm", "codex", DEFAULT_SHA, round_finding("SHOULD_FIX", "Style")
            )
        )
        file_confirm_deferrals(
            engine_pass_ledger(),
            payload,
            payload.findings or [],
            None,
            "T-1",
            now="2026-11-09T00:00:00Z",
        )
        (call,) = filings
        (adjudication,) = call.applied
        assert adjudication.line_start is None
        assert adjudication.evidence == ""
        assert adjudication.round == 2


class TestSharedHelperEquivalence:
    """#3216: the CLI wrappers delegate to the helpers the codex lane shares."""

    @staticmethod
    def _mixed() -> ReviewVerdict:
        return engine_confirm_verdict(
            must_fix=["Real bug"],
            should_fix=["Plain nit", ("Untested", "coverage.patch")],
        )

    def test_prepare_engine_confirm_decides_through_decide_confirm_verdict(
        self,
    ) -> None:
        ledger = engine_pass_ledger()
        verdict = self._mixed()
        payload = RoundInput.model_validate(engine_confirm_payload(verdict))

        prepared = prepare_engine_confirm(payload, ledger)

        assert prepared.verdict == decide_confirm_verdict(
            verdict, ledger, CONFIRM_ENGINE_DEFER_RATIONALE
        )
        assert prepared.verdict is not None
        dispositions = {
            af.finding.summary: af.disposition for af in prepared.verdict.accepted
        }
        assert dispositions == {
            "Real bug": "fixed",
            "Plain nit": "deferred",
            "Untested": "operator_actionable",
        }
        # Only the verdict changes: the envelope fields are carried through.
        assert prepared.model_copy(update={"verdict": None}) == payload.model_copy(
            update={"verdict": None}
        )

    @pytest.mark.parametrize(
        "ledger",
        [engine_pass_ledger(engine=False), fresh_ledger()],
        ids=["non-engine-pass", "no-rounds"],
    )
    def test_a_confirm_without_an_engine_pass_is_returned_unchanged(
        self, ledger: ConvergenceLedger
    ) -> None:
        payload = RoundInput.model_validate(engine_confirm_payload(self._mixed()))

        assert prepare_engine_confirm(payload, ledger) is payload

    def test_a_loop_payload_is_returned_unchanged(self) -> None:
        payload = RoundInput.model_validate(
            {**engine_confirm_payload(self._mixed()), "kind": "loop"}
        )

        assert prepare_engine_confirm(payload, engine_pass_ledger()) is payload

    def test_round_ran_engine_follows_verdict_ran_engine(self) -> None:
        engine = engine_loop_verdict(DEFAULT_SHA)
        mixed = engine_loop_verdict(DEFAULT_SHA, also=("Code Quality Reviewer",))

        def _loop(verdict: ReviewVerdict) -> RoundInput:
            return RoundInput.model_validate(
                {**engine_confirm_payload(verdict), "kind": "loop"}
            )

        assert round_ran_engine(_loop(engine)) is True
        assert round_ran_engine(_loop(mixed)) is False
        assert (
            round_ran_engine(_loop(engine).model_copy(update={"kind": "confirm"}))
            is False
        )
        for verdict in (engine, mixed):
            assert round_ran_engine(_loop(verdict)) is verdict_ran_engine(verdict)
