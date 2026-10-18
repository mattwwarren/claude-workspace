"""``cw dev-queue status``: paused-lane cause, resume hint, effective config (#2696).

1:1 test module of ``cw.cli.dev_queue.status`` for the #2696 additions. The
older status tests stay in ``tests/test_cli.py`` and ``tests/test_dev_queue.py``.
Ticks are installed through the ``latest_tick_summary_by_client`` seam via
``tests.conftest._patch_ticks`` (both seeding styles exist in the older tests;
that seam is the one chosen here because several clients need distinct ticks).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner, Result
from freezegun import freeze_time

from cw.cli import main
from cw.config import (
    _save_concurrency_overrides,
    clients_file,
    orchestrator_config_file,
)
from cw.dev_queue import add_ticket
from cw.models import (
    ClientConcurrencyOverride,
    ConcurrencyOverrides,
    LaneConcurrencyOverride,
    LaneConfig,
    QueueItemStatus,
    TicketTask,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _patch_ticks

if TYPE_CHECKING:
    from pathlib import Path

_T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
_NOW = _T0 + timedelta(minutes=5)

_ACME_BLOCK = [
    "Paused lanes:",
    "  acme/default: paused since 2026-10-18T12:00:00Z (5m ago) by circuit_breaker"
    " (spawn errors, trip #1)",
    "    last spawn errors (oldest first):",
    "      - boom",
    "    auto-resume: 2026-10-18T12:15:00Z (in 10m)",
    "    resume: cw lane resume acme default",
]


def _breaker() -> LaneConcurrencyOverride:
    return LaneConcurrencyOverride(
        paused=True,
        paused_at=_T0,
        pause_source="circuit_breaker",
        pause_trigger="spawn_errors",
        consecutive_spawn_errors=3,
        recent_spawn_errors=["boom"],
        circuit_trip_streak=1,
        circuit_auto_resume_at=_T0 + timedelta(minutes=15),
    )


def _write_orchestrator(body: str = "default_ceiling: 3\n") -> None:
    path = orchestrator_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _write_clients(tmp_path: Path, *names: str) -> None:
    write_clients_yaml(
        *(
            ClientSpec(
                name,
                tmp_path / name,
                lanes=[
                    LaneConfig(name="default", max_parallel=1),
                    LaneConfig(name="fast", max_parallel=2),
                ],
            )
            for name in names
        ),
        ensure_workspaces=True,
    )


def _overrides(
    lanes: dict[str, LaneConcurrencyOverride],
    clients: dict[str, ClientConcurrencyOverride] | None = None,
) -> None:
    _save_concurrency_overrides(
        ConcurrencyOverrides(lanes=lanes, clients=clients or {})
    )


def _status(*args: str) -> Result:
    with freeze_time(_NOW):
        result = CliRunner().invoke(main, ["dev-queue", "status", *args])
    assert result.exit_code == 0, result.output
    return result


def _block(output: str, header: str) -> list[str]:
    """The lines of the section starting at *header*, up to the next blank line."""
    lines = output.splitlines()
    start = lines.index(header)
    end = next(
        (i for i in range(start, len(lines)) if not lines[i].strip()), len(lines)
    )
    return lines[start:end]


@pytest.fixture(autouse=True)
def _config(tmp_path: Path) -> None:
    _write_orchestrator()
    _write_clients(tmp_path, "acme", "beta")


class TestPausedLanesBlock:
    def test_paused_lane_with_pending_shows_cause_and_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _overrides({"acme/default": _breaker()})
        _patch_ticks(
            monkeypatch,
            {"acme": {"skip_reason": "lane_circuit_paused", "tick_at": _NOW}},
        )

        out = _status().output

        assert _block(out, "Paused lanes:") == _ACME_BLOCK
        # The block sits between the table and the tick section.
        assert out.index("Paused lanes:") < out.index("Last dispatch tick per client:")

    def test_paused_lane_with_no_tasks_while_another_lane_has_tasks(self) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme", lane="fast"))
        _overrides({"acme/default": _breaker()})

        out = _status().output

        assert _block(out, "Paused lanes:") == _ACME_BLOCK

    def test_empty_queue_still_shows_paused_lanes(self) -> None:
        _overrides({"acme/default": _breaker()})

        out = _status().output

        lines = out.splitlines()
        assert lines[0] == "Dev queue is empty."
        assert lines[1:] == ["", *_ACME_BLOCK]

    def test_empty_queue_with_nothing_paused(self) -> None:
        assert _status().output == "Dev queue is empty.\n"

    def test_client_filter(self) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        add_ticket(TicketTask(ticket_id="BET-1", client="beta"))
        _overrides(
            {
                "acme/default": _breaker(),
                "beta/fast": LaneConcurrencyOverride(
                    paused=True, paused_at=_T0, pause_source="operator"
                ),
            }
        )

        everything = _status().output
        only_beta = _status("--client", "beta").output

        assert "  acme/default: paused since" in everything
        assert "  beta/fast: paused since" in everything
        assert _block(only_beta, "Paused lanes:") == [
            "Paused lanes:",
            "  beta/fast: paused since 2026-10-18T12:00:00Z (5m ago) by operator",
            "    auto-resume: no (operator-held)",
            "    resume: cw lane resume beta fast",
        ]

    def test_paused_default_lane_without_pending_shows_its_lane_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_ticket(
            TicketTask(
                ticket_id="ACM-1", client="acme", status=QueueItemStatus.COMPLETED
            )
        )
        _overrides({"acme/default": _breaker()})
        _patch_ticks(monkeypatch, {"acme": {"tick_at": _NOW}})

        out = _status().output

        assert "    lane default: pending=0 running=0 blocked=0 signoff=0 [PAUSED]" in (
            out.splitlines()
        )

    def test_disabled_auto_resume_reads_off(self) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _overrides({"acme/default": _breaker()})
        _write_orchestrator("default_ceiling: 3\nlane_circuit_auto_resume_minutes: 0\n")

        block = _block(_status().output, "Paused lanes:")

        assert "    auto-resume: off (lane_circuit_auto_resume_minutes=0)" in block

    def test_invalid_clients_yaml_keeps_override_entries(self) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _overrides({"acme/default": _breaker()})
        clients_file().write_text("- not a mapping\n")

        out = _status().output

        assert _block(out, "Paused lanes:") == _ACME_BLOCK


_EFFECTIVE = (
    "Effective config (declared + runtime overrides, as loaded by this command):"
)


class TestEffectiveConfig:
    def test_override_ceiling_and_caps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _overrides({}, {"acme": ClientConcurrencyOverride(ceiling=2)})
        _patch_ticks(monkeypatch, {"acme": {"cap": 2, "tick_at": _NOW}})

        out = _status().output

        assert _block(out, _EFFECTIVE) == [
            _EFFECTIVE,
            "  acme: effective: ceiling=2 (override) lane_caps=default:1,fast:2"
            " breaker_threshold=3 worktree_breaker_threshold=9"
            " auto_resume_minutes=15 auto_resume_max_minutes=240",
        ]

    def test_tickless_client_with_tasks_gets_a_line(self) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _write_orchestrator(
            "default_ceiling: 3\nper_client_ceiling: {acme: 4}\n"
            "lane_circuit_auto_resume_minutes: 0\n"
        )

        out = _status().output

        assert _block(out, _EFFECTIVE) == [
            _EFFECTIVE,
            "  acme: effective: ceiling=4 (declared) lane_caps=default:1,fast:2"
            " breaker_threshold=3 worktree_breaker_threshold=9"
            " auto_resume_minutes=0 (disabled) auto_resume_max_minutes=240",
        ]
        assert "Last dispatch tick per client:" not in out

    def test_cap_mismatch_is_annotated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _patch_ticks(monkeypatch, {"acme": {"cap": 5, "tick_at": _NOW}})

        line = _block(_status().output, _EFFECTIVE)[1]

        assert line.endswith(
            " [CONFIG? last tick used cap=5, configured ceiling=3;"
            " reload failed/pending or serve --max-parallel]"
        )
        assert "ceiling=3 (default)" in line

    def test_matching_cap_is_not_annotated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _patch_ticks(monkeypatch, {"acme": {"cap": 3, "tick_at": _NOW}})

        assert "[CONFIG?" not in _status().output

    def test_unconfigured_client_falls_back_to_the_ceiling(self) -> None:
        add_ticket(TicketTask(ticket_id="GHO-1", client="ghost"))

        line = _block(_status().output, _EFFECTIVE)[1]

        assert line.startswith("  ghost: effective: ceiling=3 (default)")
        assert " lane_caps=default:3 " in line


class TestTickSection:
    def test_client_without_a_tick_gets_no_tick_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        add_ticket(TicketTask(ticket_id="BET-1", client="beta"))
        _patch_ticks(monkeypatch, {"beta": {"tick_at": _NOW}})

        lines = _status().output.splitlines()

        assert any(ln.startswith("  beta: claimed=") for ln in lines)
        assert not any(ln.startswith("  acme: claimed=") for ln in lines)


class TestStaleTickAnnotation:
    def test_resumed_since_this_tick(self, monkeypatch: pytest.MonkeyPatch) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _patch_ticks(
            monkeypatch,
            {"acme": {"skip_reason": "lane_circuit_paused", "tick_at": _NOW}},
        )

        lines = _status().output.splitlines()

        (tick_line,) = [ln for ln in lines if ln.startswith("  acme: claimed=")]
        assert tick_line.endswith("skip=lane_circuit_paused [resumed since this tick]")

    def test_not_annotated_while_still_paused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _overrides({"acme/default": _breaker()})
        _patch_ticks(
            monkeypatch,
            {"acme": {"skip_reason": "lane_circuit_paused", "tick_at": _NOW}},
        )

        assert "[resumed since this tick]" not in _status().output


def _json(*args: str) -> dict[str, dict[str, object]]:
    data: dict[str, dict[str, object]] = json.loads(_status("--json", *args).output)
    return data


class TestJson:
    def test_paused_lanes_and_effective_shapes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _overrides(
            {"acme/default": _breaker()},
            {"acme": ClientConcurrencyOverride(ceiling=3)},
        )
        _patch_ticks(
            monkeypatch,
            {"acme": {"skip_reason": "lane_circuit_paused", "cap": 2, "tick_at": _NOW}},
        )

        data = _json()

        assert data == {
            "acme": {
                "skip_reason": "lane_circuit_paused",
                "freshness_detail": None,
                "blocked_branch": None,
                "needs_attn": 0,
                "paused_lanes": [
                    {
                        "client": "acme",
                        "lane": "default",
                        "source": "circuit_breaker",
                        "trigger": "spawn_errors",
                        "since": "2026-10-18T12:00:00Z",
                        "trip_streak": 1,
                        "recent_errors": ["boom"],
                        "auto_resume_at": "2026-10-18T12:15:00Z",
                        "auto_resume_enabled": True,
                        "resume_command": "cw lane resume acme default",
                    }
                ],
                "effective": {
                    "ceiling": 3,
                    "ceiling_source": "override",
                    "lane_caps": {"default": 1, "fast": 2},
                    "circuit_breaker_threshold": 3,
                    "worktree_breaker_threshold": 9,
                    "auto_resume_minutes": 15,
                    "auto_resume_max_minutes": 240,
                    "last_tick_cap": 2,
                },
            }
        }

    def test_tickless_paused_client_appears_with_null_tick_fields(self) -> None:
        _overrides({"beta/fast": LaneConcurrencyOverride(paused=True)})

        data = _json()

        assert list(data) == ["beta"]
        entry = data["beta"]
        assert entry["skip_reason"] is None
        assert entry["freshness_detail"] is None
        assert entry["blocked_branch"] is None
        assert entry["needs_attn"] == 0
        paused = entry["paused_lanes"]
        assert isinstance(paused, list)
        assert [p["source"] for p in paused] == ["legacy"]
        effective = entry["effective"]
        assert isinstance(effective, dict)
        assert effective["last_tick_cap"] is None

    def test_client_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _overrides({"beta/fast": LaneConcurrencyOverride(paused=True)})
        _patch_ticks(monkeypatch, {"acme": {"tick_at": _NOW}})

        assert list(_json()) == ["acme", "beta"]
        assert list(_json("--client", "acme")) == ["acme"]
        assert list(_json("--client", "beta")) == ["beta"]

    def test_empty_when_nothing_applies(self) -> None:
        assert _json() == {}

    def test_tick_client_without_pauses_has_empty_paused_lanes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_ticks(monkeypatch, {"ghost": {"tick_at": _NOW}})

        entry = _json()["ghost"]

        assert entry["paused_lanes"] == []
        effective = entry["effective"]
        assert isinstance(effective, dict)
        assert effective["lane_caps"] == {"default": 3}
        assert effective["ceiling_source"] == "default"
        assert effective["last_tick_cap"] == 3
