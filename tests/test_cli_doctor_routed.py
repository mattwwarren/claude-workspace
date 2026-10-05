"""CLI tests for the class-11 confirmation path of ``cw doctor --reap`` (#2524).

``cw doctor --reap`` never grants an unscoped batch close of routed-result
sessions: an interactive run prompts per session, ``--yes`` closes only the
sessions it names (or the single finding when there is exactly one), and a
``--json`` caller must opt in with ``--yes`` plus ``--routed-session-id``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.doctor import CheckResult, DoctorReport
from cw.doctor._shared import WedgeFinding
from cw.doctor.routed_result_wedge import WEDGE_ROUTED_RESULT_STRANDED

if TYPE_CHECKING:
    from pathlib import Path

_IDS = ("sess-a", "sess-b")


def _finding(session_id: str) -> WedgeFinding:
    return WedgeFinding(
        wedge_class=WEDGE_ROUTED_RESULT_STRANDED,
        session_id=session_id,
        ticket_id=f"T-{session_id}",
        recipe="close it",
        state_file="/state.json",
    )


def _report(session_ids: tuple[str, ...], *, ok: bool = True) -> DoctorReport:
    return DoctorReport(
        version="test",
        checks=[CheckResult("stub", ok=ok, detail="stub")],
        wedge_findings=[_finding(sid) for sid in session_ids],
    )


class _Harness:
    """Stub the doctor run and the class-11 closer; record what was closed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reaped: list[list[str]] = []
        self.run_doctor_calls: list[bool] = []
        self.selector_calls: list[str] = []
        self.selector_ok = True
        self.report = _report(_IDS)
        monkeypatch.setattr("cw.cli.maintenance.run_doctor", self._run_doctor)
        monkeypatch.setattr(
            "cw.cli.maintenance.reap_routed_result_findings", self._reap
        )
        monkeypatch.setattr(
            "cw.cli.maintenance._reap_session_by_selector", self._selector
        )

    def _run_doctor(
        self, *, reap: bool = False, reap_routed_result: bool = True, **_: object
    ) -> DoctorReport:
        self.run_doctor_calls.append(reap)
        return self.report

    def _reap(self, findings: list[WedgeFinding]) -> list[str]:
        ids = [f.session_id or "" for f in findings]
        self.reaped.append(ids)
        return ids

    def _selector(self, session: str, *, bounded: bool = False) -> bool:
        del bounded
        self.selector_calls.append(session)
        return self.selector_ok


@pytest.fixture
def harness(tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    del tmp_config_dir
    return _Harness(monkeypatch)


def test_json_without_yes_asks_for_confirmation_and_closes_nothing(
    harness: _Harness,
) -> None:
    result = CliRunner().invoke(main, ["doctor", "--reap", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["confirmation_required"] is True
    assert payload["routed_result_session_ids"] == list(_IDS)
    assert "--routed-session-id" in payload["confirmation_hint"]
    assert harness.reaped == []


def test_json_confirmation_exit_code_follows_report_health(
    harness: _Harness,
) -> None:
    harness.report = _report(_IDS, ok=False)

    result = CliRunner().invoke(main, ["doctor", "--reap", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output)["confirmation_required"] is True
    assert harness.reaped == []


def test_json_yes_without_ids_across_several_sessions_asks_for_ids(
    harness: _Harness,
) -> None:
    result = CliRunner().invoke(main, ["doctor", "--reap", "--json", "--yes"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["confirmation_required"] is True
    assert (
        "provide --routed-session-id for every session"
        in (payload["confirmation_hint"])
    )
    assert harness.reaped == []


def test_yes_without_ids_across_several_sessions_is_refused(
    harness: _Harness,
) -> None:
    result = CliRunner().invoke(main, ["doctor", "--reap", "--yes"])

    assert result.exit_code != 0
    assert "--yes requires --routed-session-id" in result.output
    assert harness.reaped == []


def test_yes_with_explicit_ids_closes_only_those_sessions(harness: _Harness) -> None:
    result = CliRunner().invoke(
        main, ["doctor", "--reap", "--json", "--yes", "--routed-session-id", "sess-b"]
    )

    assert result.exit_code == 0, result.output
    assert harness.reaped == [["sess-b"]]
    assert "confirmation_required" not in json.loads(result.output)


def test_unknown_explicit_id_selects_nothing(harness: _Harness) -> None:
    result = CliRunner().invoke(
        main, ["doctor", "--reap", "--yes", "--routed-session-id", "ghost"]
    )

    assert result.exit_code == 0, result.output
    assert harness.reaped == []
    assert harness.selector_calls == []


def test_yes_with_a_single_finding_closes_it(harness: _Harness) -> None:
    harness.report = _report(("sess-a",))

    result = CliRunner().invoke(main, ["doctor", "--reap", "--yes"])

    assert result.exit_code == 0, result.output
    assert harness.reaped == [["sess-a"]]
    assert "session=sess-a ticket=T-sess-a" in result.output


def test_interactive_run_prompts_per_session(harness: _Harness) -> None:
    result = CliRunner().invoke(main, ["doctor", "--reap"], input="n\ny\n")

    assert result.exit_code == 0, result.output
    assert "Close routed-result session sess-a?" in result.output
    assert "Close routed-result session sess-b?" in result.output
    assert harness.reaped == [["sess-b"]]


def test_interactive_decline_closes_nothing(harness: _Harness) -> None:
    result = CliRunner().invoke(main, ["doctor", "--reap"], input="n\nn\n")

    assert result.exit_code == 0, result.output
    assert harness.reaped == []


def test_session_scope_with_a_routed_finding_closes_only_that_session(
    harness: _Harness,
) -> None:
    result = CliRunner().invoke(main, ["doctor", "--reap", "sess-a", "--yes"])

    assert result.exit_code == 0, result.output
    assert harness.reaped == [["sess-a"]]
    assert harness.selector_calls == []
    assert harness.run_doctor_calls == [False]


def test_session_scope_without_a_routed_finding_uses_the_general_reaper(
    harness: _Harness,
) -> None:
    harness.report = _report(())

    result = CliRunner().invoke(main, ["doctor", "--reap", "other-session"])

    assert result.exit_code == 0, result.output
    assert harness.selector_calls == ["other-session"]
    assert harness.reaped == []


def test_scoped_close_never_falls_through_to_the_general_reaper(
    harness: _Harness,
) -> None:
    """An explicit --routed-session-id whose finding vanished closes nothing."""
    harness.report = _report(())

    result = CliRunner().invoke(
        main, ["doctor", "--reap", "--yes", "--routed-session-id", "sess-a"]
    )

    assert result.exit_code == 0, result.output
    assert harness.reaped == []
    assert harness.run_doctor_calls == [False]


def test_reap_without_routed_findings_runs_the_general_doctor_reap(
    harness: _Harness,
) -> None:
    harness.report = _report(())

    result = CliRunner().invoke(main, ["doctor", "--reap"])

    assert result.exit_code == 0, result.output
    assert harness.run_doctor_calls == [False, True]
    assert harness.reaped == []


def test_failing_check_exits_nonzero_after_a_confirmed_close(
    harness: _Harness,
) -> None:
    harness.report = _report(("sess-a",), ok=False)

    result = CliRunner().invoke(main, ["doctor", "--reap", "--yes"])

    assert result.exit_code == 1
    assert harness.reaped == [["sess-a"]]


def test_unknown_session_scope_exits_one(harness: _Harness) -> None:
    harness.report = _report(())
    harness.selector_ok = False

    result = CliRunner().invoke(main, ["doctor", "--reap", "ghost"])

    assert result.exit_code == 1
    assert "No session found matching 'ghost'" in result.output
    assert harness.reaped == []


def test_plain_doctor_run_never_reaps(harness: _Harness) -> None:
    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert harness.run_doctor_calls == [False]
    assert harness.reaped == []
    assert harness.selector_calls == []
