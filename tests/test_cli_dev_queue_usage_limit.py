"""Tests for ``cw dev-queue usage-limit clear`` (#3547).

The probe seam is patched at the consuming module, so no ``claude`` launches;
the client's ``operator_github_login`` is set, so no ``gh`` launches either.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

import cw.dispatch_state
from cw.cli import main
from cw.dev_queue import list_tickets
from cw.dispatch import dispatch_tick
from cw.dispatch_state import load_usage_limited_until
from cw.dispatch_state_marks import load_usage_limit_clear_marks
from cw.events import read_events
from cw.lane_pause import iso_z
from cw.models import ClientConfig, OrchestratorConfig, OrchestratorEventType
from cw.native_daemon import FakeNativeDaemonClient
from cw.usage_limit_probe import ProbeResult, ProbeVerdict
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._shared.dispatch_fixtures import (
    seed_stamped_row,
    seed_usage_limit_window,
    write_client_with_operator,
)
from tests._shared.lock_helpers import spy_no_locks_held

_CLIENT = "test-client"
_PROBE = "cw.cli.dev_queue.usage_limit.probe_account_usage_limit"
_NOW = datetime.now(UTC).replace(microsecond=0)
_UNTIL = _NOW + timedelta(hours=2)
_STAMP = _NOW + timedelta(hours=1)


@pytest.fixture(autouse=True)
def _client(tmp_path: Path) -> None:
    write_client_with_operator(tmp_path / "ws", name=_CLIENT)


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, ["dev-queue", "usage-limit", *args])


def _clear(*args: str) -> Result:
    return _invoke("clear", "-c", _CLIENT, *args)


def _probe_returning(
    monkeypatch: pytest.MonkeyPatch, verdict: ProbeVerdict, detail: str = "d"
) -> list[str | None]:
    calls: list[str | None] = []

    def _probe(model: str | None) -> ProbeResult:
        calls.append(model)
        return ProbeResult(verdict=verdict, detail=detail)

    monkeypatch.setattr(_PROBE, _probe)
    return calls


def _cleared_events() -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(event_types=[OrchestratorEventType.USAGE_LIMIT_CLEARED])
    ]


def _sidecar_bytes() -> bytes | None:
    path = cw.dispatch_state.DISPATCH_STATE_FILE
    return path.read_bytes() if path.exists() else None


def _stamps() -> dict[str, datetime | None]:
    return {t.ticket_id: t.next_eligible_at for t in list_tickets(_CLIENT)}


def _seed_window_and_rows() -> None:
    seed_usage_limit_window(_CLIENT, _UNTIL)
    seed_stamped_row(_CLIENT, "T-1", next_eligible_at=_STAMP)
    seed_stamped_row(_CLIENT, "T-2", next_eligible_at=_STAMP)


class TestHelp:
    def test_group_help(self) -> None:
        result = _invoke("--help")

        assert result.exit_code == 0
        assert "Inspect and clear per-client usage-limit back-off windows." in (
            result.output
        )

    def test_clear_help(self) -> None:
        result = _invoke("clear", "--help")

        assert result.exit_code == 0
        output = " ".join(result.output.split())
        assert (
            "Lift a client's usage-limit back-off window early, after the account"
            " limit was reset by hand." in output
        )
        assert "Print what would be cleared; change nothing and run no probe." in output
        assert (
            "Skip the account probe and clear even if the account may still be"
            " limited." in output
        )


class TestNothingToClear:
    @pytest.mark.parametrize("extra", [(), ("--dry-run",)])
    def test_exits_zero_without_probe_or_event(
        self, monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...]
    ) -> None:
        calls = _probe_returning(monkeypatch, ProbeVerdict.AVAILABLE)

        result = _clear(*extra)

        assert result.exit_code == 0, result.output
        assert (
            f"nothing to clear for {_CLIENT}: no active usage-limit window and no"
            " waiting rows" in result.output
        )
        assert calls == []
        assert _cleared_events() == []
        assert load_usage_limit_clear_marks() == {}


class TestDryRun:
    def test_prints_the_plan_and_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_window_and_rows()
        calls = _probe_returning(monkeypatch, ProbeVerdict.AVAILABLE)
        before = _sidecar_bytes()

        result = _clear("--dry-run")

        assert result.exit_code == 0, result.output
        out = result.output
        assert (
            f"dry run: would clear usage-limit window for {_CLIENT}"
            f" (until {iso_z(_UNTIL)})" in out
        )
        for ticket in ("T-1", "T-2"):
            assert (
                f"dry run: would release {ticket}: next_eligible_at"
                f" {iso_z(_STAMP)} -> none" in out
            )
        assert (
            "dry run: note: next_eligible_at is shared by usage-limit, spawn-error"
            " and worktree waits; every future stamp on the client's pending rows"
            " is released" in out
        )
        assert "dry run: no changes made" in out
        assert calls == []
        assert _sidecar_bytes() == before
        assert _stamps() == {"T-1": _STAMP, "T-2": _STAMP}
        assert _cleared_events() == []

    def test_rows_without_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_stamped_row(_CLIENT, "T-1", next_eligible_at=_STAMP)
        _probe_returning(monkeypatch, ProbeVerdict.AVAILABLE)

        result = _clear("--dry-run")

        assert result.exit_code == 0, result.output
        assert f"dry run: no active usage-limit window for {_CLIENT}" in result.output


class TestProbeRefusals:
    def test_limited_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_window_and_rows()
        calls = _probe_returning(monkeypatch, ProbeVerdict.LIMITED, "weekly limit")
        before = _sidecar_bytes()

        result = _clear()

        assert result.exit_code == 1
        assert (
            f"refusing to clear {_CLIENT}: the account probe reports the usage"
            " limit is still in effect (weekly limit)" in result.output
        )
        assert "Re-run with --force to clear anyway." in result.output
        assert calls == [None]
        assert _sidecar_bytes() == before
        assert _stamps() == {"T-1": _STAMP, "T-2": _STAMP}
        assert _cleared_events() == []

    def test_error_fails_closed_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_window_and_rows()
        calls = _probe_returning(monkeypatch, ProbeVerdict.ERROR, "timed out")
        before = _sidecar_bytes()

        result = _clear()

        assert result.exit_code == 1
        assert (
            f"refusing to clear {_CLIENT}: the account probe could not determine"
            " the account state (timed out)" in result.output
        )
        assert "Re-run with --force to clear without probing." in result.output
        assert "still in effect" not in result.output
        assert calls == [None]
        assert _sidecar_bytes() == before
        assert _cleared_events() == []


class TestClear:
    def test_available_clears_and_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_window_and_rows()
        _probe_returning(monkeypatch, ProbeVerdict.AVAILABLE)

        result = _clear()

        assert result.exit_code == 0, result.output
        assert (
            f"cleared usage-limit window for {_CLIENT} (was until {iso_z(_UNTIL)})"
            in result.output
        )
        assert "released 2 row(s): T-1, T-2" in result.output
        assert (
            "a running dispatch loop drops the window on its next tick;"
            " no restart needed" in result.output
        )
        assert load_usage_limited_until() == {}
        assert _stamps() == {"T-1": None, "T-2": None}
        events = _cleared_events()
        assert len(events) == 1
        assert events[0]["probe"] == "available"
        assert events[0]["forced"] is False
        assert events[0]["cleared_by"] == "operator-login"

    def test_rows_only_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_stamped_row(_CLIENT, "T-1", next_eligible_at=_STAMP)
        _probe_returning(monkeypatch, ProbeVerdict.AVAILABLE)

        result = _clear()

        assert result.exit_code == 0, result.output
        assert (
            f"no active usage-limit window for {_CLIENT}; clear mark recorded"
            in result.output
        )
        assert "released 1 row(s): T-1" in result.output

    def test_window_only_summary_omits_the_rows_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_usage_limit_window(_CLIENT, _UNTIL)
        _probe_returning(monkeypatch, ProbeVerdict.AVAILABLE)

        result = _clear()

        assert result.exit_code == 0, result.output
        assert "released" not in result.output

    @pytest.mark.parametrize("verdict", [ProbeVerdict.LIMITED, ProbeVerdict.ERROR])
    def test_force_skips_the_probe(
        self, monkeypatch: pytest.MonkeyPatch, verdict: ProbeVerdict
    ) -> None:
        _seed_window_and_rows()
        calls = _probe_returning(monkeypatch, verdict)

        result = _clear("--force")

        assert result.exit_code == 0, result.output
        assert calls == []
        assert load_usage_limited_until() == {}
        event = _cleared_events()[0]
        assert event["forced"] is True
        assert event["probe"] == "skipped"

    def test_actor_and_probe_run_with_no_lock_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_window_and_rows()
        actor = spy_no_locks_held(
            monkeypatch,
            "cw.cli.dev_queue.usage_limit.resolve_actor_or_empty",
            returns="spy-actor",
        )
        probe = spy_no_locks_held(
            monkeypatch,
            _PROBE,
            returns=ProbeResult(verdict=ProbeVerdict.AVAILABLE, detail="ok"),
        )

        result = _clear()

        assert result.exit_code == 0, result.output
        assert actor == [()]
        assert probe == [()]
        assert _cleared_events()[0]["cleared_by"] == "spy-actor"

    def test_event_write_failure_prints_summary_then_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_window_and_rows()
        _probe_returning(monkeypatch, ProbeVerdict.AVAILABLE)

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "events disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.usage_limit_window.record_event", _boom)

        result = _clear()

        assert result.exit_code != 0
        out = result.output
        summary = out.index(f"cleared usage-limit window for {_CLIENT}")
        failure = out.index(f"usage-limit window for {_CLIENT} was cleared (was until")
        assert summary < failure
        assert (
            "but the dispatch.usage_limit_cleared audit event could not be recorded"
            in out
        )
        assert load_usage_limited_until() == {}

    def test_unknown_client_fails_and_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _probe_returning(monkeypatch, ProbeVerdict.AVAILABLE)

        result = _invoke("clear", "-c", "nope", "--force")

        assert result.exit_code != 0
        assert "Unknown client 'nope'" in result.output
        assert calls == []
        assert _sidecar_bytes() is None


def test_acceptance_cleared_row_dispatches(
    sample_client_config: ClientConfig,
    simple_config: OrchestratorConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_clients_yaml(
        ClientSpec.from_config(
            sample_client_config, extra={"operator_github_login": "operator-login"}
        )
    )
    _seed_window_and_rows()
    _probe_returning(monkeypatch, ProbeVerdict.AVAILABLE)
    daemon = FakeNativeDaemonClient()

    assert _clear().exit_code == 0
    assert _stamps() == {"T-1": None, "T-2": None}
    result = dispatch_tick(
        simple_config,
        native_daemon=daemon,
        usage_limited_until=load_usage_limited_until(),
    )

    assert result.spawned == 1
    assert len(daemon.spawn_calls) == 1
