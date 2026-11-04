"""CLI tests for ``cw doctor --reap --ticket <T> -c <C>`` (#2716).

A scoped reap calls ``run_doctor(reap=True, scope=ReapScope(C, T))`` exactly
once and never the unscoped battery, the positional selector reap or a
routed-result close. Every misuse is a Click usage error (exit 2) that runs
nothing.

``tests/test_cli_doctor_routed.py``'s ``_Harness`` discards ``scope`` and
records only ``reap``, so this module carries its own small recording stub
(``_ScopedHarness``) rather than widening that one.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import click
import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli.maintenance import doctor
from cw.doctor import CheckResult, DoctorReport, ReapScope

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

_TICKET_HELP = (
    "Scope --reap to this ticket under --client: closes, reverts and cancels"
    " nothing outside that (client, ticket). Requires --reap and --client."
)
_CLIENT_HELP = "Client that owns --ticket; required with --ticket."
_NEEDS_CLIENT = (
    "--ticket requires --client: ticket numbers repeat across clients,"
    " so name the client too."
)
_NEEDS_REAP = "--ticket requires --reap: it only scopes a reap."
_NO_SELECTOR = (
    "--ticket cannot be combined with a SESSION argument or --routed-session-id:"
    " a scoped reap does not close routed-result sessions."
)
_NEEDS_TICKET = "--client requires --ticket: it only scopes a reap to one ticket."


class _ScopedHarness:
    """Record every doctor entry point the CLI could reach."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_doctor_calls: list[dict[str, object]] = []
        self.selector_calls: list[str] = []
        self.routed_calls: int = 0
        self.named_calls: int = 0
        self.report = DoctorReport(
            version="test", checks=[CheckResult("stub", ok=True, detail="stub")]
        )
        monkeypatch.setattr("cw.cli.maintenance.run_doctor", self._run_doctor)
        monkeypatch.setattr(
            "cw.cli.maintenance._reap_session_by_selector", self._selector
        )
        monkeypatch.setattr(
            "cw.cli.maintenance.reap_routed_result_findings", self._routed
        )
        monkeypatch.setattr(
            "cw.cli.maintenance.close_named_routed_result_sessions", self._named
        )

    def _run_doctor(self, **kwargs: object) -> DoctorReport:
        self.run_doctor_calls.append(kwargs)
        return self.report

    def _selector(self, session: str, **_kwargs: object) -> bool:
        self.selector_calls.append(session)
        return True

    def _routed(self, *_args: object, **_kwargs: object) -> list[str]:
        self.routed_calls += 1
        return []

    def _named(self, *_args: object, **_kwargs: object) -> list[str]:
        self.named_calls += 1
        return []

    def assert_nothing_ran(self) -> None:
        assert self.run_doctor_calls == []
        assert self.selector_calls == []
        assert self.routed_calls == 0
        assert self.named_calls == 0


@pytest.fixture
def harness(tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> _ScopedHarness:
    del tmp_config_dir
    return _ScopedHarness(monkeypatch)


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, ["doctor", *args])


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--ticket", "T-1", "--reap"), _NEEDS_CLIENT),
        (("--ticket", "T-1", "-c", "acme"), _NEEDS_REAP),
        (("--reap", "--ticket", "T-1", "-c", "acme", "sess-1"), _NO_SELECTOR),
        (
            ("--reap", "--ticket", "T-1", "-c", "acme", "--routed-session-id", "x"),
            _NO_SELECTOR,
        ),
        (("--reap", "-c", "acme"), _NEEDS_TICKET),
    ],
    ids=["no-client", "no-reap", "session", "routed-session-id", "no-ticket"],
)
def test_usage_errors_exit_2_and_run_nothing(
    harness: _ScopedHarness, args: tuple[str, ...], message: str
) -> None:
    result = _invoke(*args)

    assert result.exit_code == click.UsageError.exit_code
    assert message in result.output
    harness.assert_nothing_ran()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("-c", "acme", "sess-1"), _NEEDS_TICKET),
        (("--ticket", "T-1", "sess-1"), _NEEDS_REAP),
        (("--reap", "--ticket", "T-1", "sess-1"), _NEEDS_CLIENT),
    ],
    ids=["client-first", "reap-second", "client-before-selector"],
)
def test_usage_error_check_order(
    harness: _ScopedHarness, args: tuple[str, ...], message: str
) -> None:
    result = _invoke(*args)

    assert result.exit_code == click.UsageError.exit_code
    assert message in result.output
    harness.assert_nothing_ran()


@pytest.mark.parametrize("client_flag", ["-c", "--client"])
def test_scoped_reap_calls_run_doctor_once_with_the_scope(
    harness: _ScopedHarness, client_flag: str
) -> None:
    result = _invoke("--reap", "--ticket", "T-1", client_flag, "acme")

    assert result.exit_code == 0, result.output
    assert harness.run_doctor_calls == [
        {"reap": True, "scope": ReapScope("acme", "T-1")}
    ]
    assert harness.selector_calls == []
    assert harness.routed_calls == 0
    assert harness.named_calls == 0


def test_scoped_json_output_parses(harness: _ScopedHarness) -> None:
    result = _invoke("--reap", "--ticket", "T-1", "-c", "acme", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [c["name"] for c in payload["checks"]] == ["stub"]


@pytest.mark.parametrize("as_json", [True, False])
def test_scoped_failing_report_exits_1(harness: _ScopedHarness, as_json: bool) -> None:
    harness.report = DoctorReport(
        version="test", checks=[CheckResult("wedge-reap", ok=False, detail="t/o")]
    )
    extra = ("--json",) if as_json else ()

    result = _invoke("--reap", "--ticket", "T-1", "-c", "acme", *extra)

    assert result.exit_code == 1


def test_scoped_yes_has_no_effect(harness: _ScopedHarness) -> None:
    result = _invoke("--reap", "--ticket", "T-1", "-c", "acme", "--yes")

    assert result.exit_code == 0, result.output
    assert harness.run_doctor_calls == [
        {"reap": True, "scope": ReapScope("acme", "T-1")}
    ]


def test_scoped_session_skipped_warning_still_exits_0(
    harness: _ScopedHarness,
) -> None:
    harness.report = DoctorReport(
        version="test",
        checks=[
            CheckResult(
                "scoped-reap/session-skipped",
                ok=True,
                warn=True,
                detail="skipped: s (unreadable record)",
            )
        ],
    )

    result = _invoke("--reap", "--ticket", "T-1", "-c", "acme", "--json")

    assert result.exit_code == 0, result.output


def test_unscoped_reap_still_runs_the_battery_then_the_reap(
    harness: _ScopedHarness,
) -> None:
    result = _invoke("--reap")

    assert result.exit_code == 0, result.output
    assert harness.run_doctor_calls == [
        {"reap": False},
        {"reap": True, "reap_routed_result": False},
    ]


def test_unscoped_positional_selector_is_unchanged(harness: _ScopedHarness) -> None:
    result = _invoke("--reap", "sess-1")

    assert result.exit_code == 0, result.output
    assert harness.selector_calls == ["sess-1"]


def test_unscoped_routed_session_id_is_unchanged(harness: _ScopedHarness) -> None:
    result = _invoke("--reap", "--routed-session-id", "sess-1")

    assert harness.named_calls == 1
    assert result.exit_code == 1


def test_option_help_strings() -> None:
    params = {p.name: p for p in doctor.params}

    assert getattr(params["ticket"], "help", None) == _TICKET_HELP
    assert getattr(params["client"], "help", None) == _CLIENT_HELP
    assert doctor.help is not None
    assert "--ticket" in doctor.help
