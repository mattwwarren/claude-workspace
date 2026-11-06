"""``cw dev-queue status``: paused lanes, effective config, held tickets (#2696, #2516).

1:1 test module of ``cw.cli.dev_queue.status`` for the #2696 and #2516
additions. The older status tests stay in ``tests/test_cli.py`` and
``tests/test_dev_queue.py``. Ticks are installed through the
``client_tick_snapshots`` seam via ``tests.conftest._patch_ticks`` (both
seeding styles exist in the older tests; that seam is the one chosen here
because several clients need distinct ticks). The #2516 tests seed real
per-ticket ``dispatch.tick`` events with ``tests._dispatch_tick_helpers``;
``_patch_ticks`` replaces only the snapshot function, so the two coexist.

Pitfall: ``TicketTask.created_at`` defaults to the real clock, and the streak
ignores events older than the row, so held rows are built with an explicit
``created_at`` before ``_NOW``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import yaml
from click.testing import CliRunner, Result
from freezegun import freeze_time

import cw.config
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
from tests._commit_hook_helpers import install_templated_hook, write_commit_hook_config
from tests._dispatch_tick_helpers import (
    client_tick_event,
    seed_dispatch_events,
    skip_tick_events,
)
from tests.conftest import _patch_ticks

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ClientConfig

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
                "pending_skips": [],
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


_HELD_HEADER = (
    "Pending tickets held by dispatch"
    " (latest per-ticket skip; xN = consecutive per-ticket tick events):"
)
_TICK_HEADER = "Last dispatch tick per client:"
_ROW_AT = _T0 - timedelta(hours=3)
_OCCUPIED = "worktree_occupied"
_STEP = timedelta(seconds=30)


def _held_row(ticket_id: str, client: str = "acme", **overrides: object) -> None:
    kwargs: dict[str, object] = {
        "ticket_id": ticket_id,
        "client": client,
        "created_at": _ROW_AT,
    }
    kwargs.update(overrides)
    add_ticket(TicketTask.model_validate(kwargs))


def _seed_skips(
    ticket_id: str,
    count: int,
    *,
    client: str = "acme",
    last_at: datetime = _NOW - timedelta(seconds=12),
    reason: str = _OCCUPIED,
) -> None:
    start = last_at - (count - 1) * _STEP
    seed_dispatch_events(
        skip_tick_events(client, ticket_id, reason, count=count, start=start)
    )


class TestPendingSkips:
    def test_mixed_stream_keeps_the_client_tick_and_shows_the_held_row(self) -> None:
        _held_row("49")
        seed_dispatch_events([client_tick_event("acme", _NOW - timedelta(seconds=40))])
        _seed_skips("49", 103)

        output = _status().output

        (tick_line,) = [
            ln for ln in output.splitlines() if ln.startswith("  acme: claimed=")
        ]
        assert "pending=1" in tick_line
        assert tick_line.endswith("skip=no_pending")
        assert "skip=worktree_occupied" not in output
        assert _block(output, _HELD_HEADER) == [
            _HELD_HEADER,
            "  acme: 49: worktree_occupied x103 (last 12s ago)",
        ]

    def test_held_block_follows_the_tick_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _held_row("49")
        _patch_ticks(monkeypatch, {"acme": {"pending": 1, "tick_at": _NOW}})
        _seed_skips("49", 3)

        lines = _status().output.splitlines()

        assert lines.index(_TICK_HEADER) < lines.index(_HELD_HEADER)

    def test_no_held_rows_prints_no_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _held_row("49")
        _patch_ticks(monkeypatch, {"acme": {"tick_at": _NOW}})

        assert _HELD_HEADER not in _status().output

    def test_stale_last_event_is_marked(self) -> None:
        _held_row("49")
        _seed_skips("49", 4, last_at=_NOW - timedelta(seconds=200))

        block = _block(_status().output, _HELD_HEADER)

        assert block[1:] == ["  acme: 49: worktree_occupied x4 (last 3m ago) [stale]"]

    def test_rows_sort_by_count_then_ticket(self) -> None:
        for ticket in ("49", "51", "50"):
            _held_row(ticket)
        _seed_skips("49", 5)
        _seed_skips("51", 10)
        _seed_skips("50", 10)

        block = _block(_status().output, _HELD_HEADER)

        assert [ln.split(":")[1].strip() for ln in block[1:]] == ["50", "51", "49"]

    def test_client_filter(self) -> None:
        _held_row("49")
        _held_row("B-1", client="beta")
        _seed_skips("49", 3)
        _seed_skips("B-1", 2, client="beta")

        block = _block(_status("--client", "beta").output, _HELD_HEADER)

        assert block[1:] == ["  beta: B-1: worktree_occupied x2 (last 12s ago)"]

    def test_non_pending_row_is_not_shown(self) -> None:
        _held_row("49", status=QueueItemStatus.RUNNING)
        _seed_skips("49", 3)

        assert _HELD_HEADER not in _status().output

    def test_block_prints_without_any_client_tick(self) -> None:
        _held_row("49")
        _seed_skips("49", 3)

        output = _status().output

        assert _TICK_HEADER not in output
        assert _block(output, _HELD_HEADER)[1:] == [
            "  acme: 49: worktree_occupied x3 (last 12s ago)"
        ]


class TestPendingSkipsJson:
    def test_item_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _held_row("49")
        _patch_ticks(monkeypatch, {"acme": {"tick_at": _NOW}})
        seed_dispatch_events(
            skip_tick_events("acme", "49", _OCCUPIED, count=3, start=_T0)
        )

        assert _json()["acme"]["pending_skips"] == [
            {
                "ticket_id": "49",
                "skip_reason": _OCCUPIED,
                "consecutive": 3,
                "first_at": "2026-10-18T12:00:00Z",
                "last_at": "2026-10-18T12:01:00Z",
                "stale": True,
            }
        ]

    def test_tick_client_with_nothing_held_has_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _held_row("49")
        _patch_ticks(monkeypatch, {"acme": {"tick_at": _NOW}})

        assert _json()["acme"]["pending_skips"] == []

    def test_held_client_without_tick_or_pause_gets_an_entry(self) -> None:
        _held_row("49")
        _seed_skips("49", 3)

        entry = _json()["acme"]

        assert entry["skip_reason"] is None
        assert entry["freshness_detail"] is None
        assert entry["blocked_branch"] is None
        assert entry["paused_lanes"] == []
        assert isinstance(entry["effective"], dict)
        pending_skips = entry["pending_skips"]
        assert isinstance(pending_skips, list)
        assert [item["ticket_id"] for item in pending_skips] == ["49"]
        assert "acme" not in _json("--client", "beta")


class TestStatusReadsClientWideTicks:
    def test_per_ticket_event_does_not_replace_the_client_tick(self) -> None:
        _held_row("49")
        seed_dispatch_events(
            [client_tick_event("acme", _NOW - timedelta(seconds=40), pending=2, cap=3)]
        )
        _seed_skips("49", 1, last_at=_NOW - timedelta(seconds=5))

        output = _status().output
        data = _json()

        (tick_line,) = [
            ln for ln in output.splitlines() if ln.startswith("  acme: claimed=")
        ]
        assert "pending=2" in tick_line
        assert "running=0/3" in tick_line
        assert data["acme"]["skip_reason"] == "no_pending"


def _count_load_clients(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every ``load_clients`` call a status run makes (#2936).

    Wraps the real loader at both bindings a call can resolve through:
    ``cw.config`` (``get_client`` and ``load_effective_clients`` look it up
    there) and ``cw.cli.dev_queue.status`` (status imports it by name).
    """
    real = cw.config.load_clients
    calls: list[int] = []

    def counting() -> dict[str, ClientConfig]:
        calls.append(1)
        return real()

    monkeypatch.setattr("cw.config.load_clients", counting)
    monkeypatch.setattr("cw.cli.dev_queue.status.load_clients", counting)
    return calls


class TestClientsLoadedOnce:
    """``dev-queue status`` parses ``clients.yaml`` once per invocation (#2936).

    Each case is one status run over several clients; before #2936 every client
    in the table or tick section re-parsed the file through ``get_client``.
    Deliberately not counted: the freshness-gate subline
    (``cli/_base.py:_resolve_default_branch_and_workspace``) still does its own
    ``get_client`` for a gated client, a residual second parse.
    """

    def test_text_mode_with_ticks_and_paused_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        add_ticket(TicketTask(ticket_id="BET-1", client="beta"))
        _overrides({"acme/default": _breaker()})
        _patch_ticks(
            monkeypatch, {"acme": {"tick_at": _NOW}, "beta": {"tick_at": _NOW}}
        )
        calls = _count_load_clients(monkeypatch)

        out = _status().output

        assert "Last dispatch tick per client:" in out
        assert "  acme/default: paused since" in out
        assert len(calls) == 1

    def test_json_mode_with_tick_and_paused_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _overrides({"beta/default": _breaker()})
        _patch_ticks(monkeypatch, {"acme": {"tick_at": _NOW}})
        calls = _count_load_clients(monkeypatch)

        data = _json()

        assert sorted(data) == ["acme", "beta"]
        assert len(calls) == 1

    def test_empty_queue_with_paused_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _overrides({"acme/default": _breaker()})
        calls = _count_load_clients(monkeypatch)

        out = _status().output

        assert out.startswith("Dev queue is empty.")
        assert len(calls) == 1

    def test_client_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        add_ticket(TicketTask(ticket_id="BET-1", client="beta"))
        _patch_ticks(monkeypatch, {"beta": {"tick_at": _NOW}})
        calls = _count_load_clients(monkeypatch)

        out = _status("--client", "beta").output

        assert "  beta: effective:" in out
        assert len(calls) == 1

    def test_invalid_clients_yaml_still_loads_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        add_ticket(TicketTask(ticket_id="BET-1", client="beta"))
        _write_bad_client_name()
        calls = _count_load_clients(monkeypatch)

        _status()

        assert len(calls) == 1


def _write_bad_client_name() -> None:
    """A ``clients.yaml`` that fails validation with a ``CwError``."""
    clients_file().write_text(
        "clients:\n  'bad;name':\n    workspace_path: /tmp/does-not-matter\n"
    )


def _lane_caps_line(output: str, client: str) -> str:
    (line,) = [
        ln for ln in output.splitlines() if ln.startswith(f"  {client}: effective:")
    ]
    return line


def _lane_caps_json(entry: dict[str, object]) -> object:
    effective = entry["effective"]
    assert isinstance(effective, dict)
    return effective["lane_caps"]


class TestStatusOutputUnchangedByLoadOnce:
    """Characterization: lane caps and load-failure tolerance are unchanged (#2936)."""

    def test_laneless_client_with_paused_default_lane_keeps_ceiling_cap(
        self, tmp_path: Path
    ) -> None:
        """A lane-less client's cap stays the ceiling under a ``default`` pause.

        ``load_effective_clients`` rebuilds ``lanes`` from ``effective_lanes``
        (a synthetic ``default`` at max_parallel 1); lane caps must keep
        reading the declared clients (#1630, #2936).
        """
        write_clients_yaml(
            ClientSpec("plain", tmp_path / "plain"), ensure_workspaces=True
        )
        add_ticket(TicketTask(ticket_id="PLN-1", client="plain"))
        _overrides({"plain/default": _breaker()})

        data = _json()
        out = _status().output

        assert _lane_caps_json(data["plain"]) == {"default": 3}
        assert "lane_caps=default:3" in _lane_caps_line(out, "plain")

    def test_client_missing_from_clients_yaml_gets_ceiling_cap(self) -> None:
        add_ticket(TicketTask(ticket_id="GHO-1", client="ghost"))

        out = _status().output

        assert "lane_caps=default:3" in _lane_caps_line(out, "ghost")

    def test_cw_error_in_clients_yaml_degrades_to_ceiling_keeps_override_pauses(
        self,
    ) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _overrides({"acme/default": _breaker()})
        _write_bad_client_name()

        out = _status().output

        assert "lane_caps=default:3" in _lane_caps_line(out, "acme")
        # The override-held pause is still named: the declared clients are
        # empty, but ``paused_lane_views`` reads every override too.
        assert _block(out, "Paused lanes:") == _ACME_BLOCK

    def test_yaml_syntax_error_still_propagates(self) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        clients_file().write_text("clients: [\n  unterminated\n")

        result = CliRunner().invoke(main, ["dev-queue", "status"])

        assert isinstance(result.exception, yaml.YAMLError)


_MISSING_HOOK_LINE = "  \u26a0 acme: no executable pre-commit hook at "


def _acme_repo(make_git_repo: Callable[..., Path]) -> Path:
    """A real ``acme`` repo declaring a commit-stage hook, with no hook installed."""
    repo = make_git_repo("acme")
    write_commit_hook_config(repo)
    return repo


class TestCommitHookBlock:
    """The text-mode ``Commit hooks:`` block (#2973)."""

    def test_empty_queue_with_missing_hook_prints_the_block(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _acme_repo(make_git_repo)

        lines = _status().output.splitlines()

        assert lines[0] == "Dev queue is empty."
        assert lines[1:3] == ["", "Commit hooks:"]
        assert lines[3].startswith(_MISSING_HOOK_LINE)
        assert str(repo / ".git" / "hooks" / "pre-commit") in lines[3]
        assert "pre-commit install --hook-type pre-commit" in lines[3]
        assert len(lines) == 4

    def test_queued_ticket_prints_the_block_after_paused_lanes(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        _acme_repo(make_git_repo)
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        _overrides({"acme/default": _breaker()})

        out = _status().output

        assert out.index("Paused lanes:") < out.index("Commit hooks:")
        assert _block(out, "Commit hooks:")[1].startswith(_MISSING_HOOK_LINE)

    def test_healthy_client_prints_no_block(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _acme_repo(make_git_repo)
        install_templated_hook(repo, sys.executable)

        assert _status().output == "Dev queue is empty.\n"

    def test_no_config_prints_no_block_and_spawns_no_git(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_git_repo("acme")
        calls: list[object] = []

        def _record(*args: object, **_kwargs: object) -> object:
            calls.append(args)
            raise OSError

        monkeypatch.setattr("cw.doctor.commit_hook.git_hooks_dir", _record)
        monkeypatch.setattr("cw.doctor.commit_hook.run_git", _record)

        out = _status().output

        assert "Commit hooks:" not in out
        assert calls == []

    def test_client_filter_shows_only_that_client(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        _acme_repo(make_git_repo)
        beta = make_git_repo("beta")
        write_commit_hook_config(beta)

        out = _status("-c", "acme").output

        assert "  \u26a0 acme:" in out
        assert "beta" not in out

    def test_both_clients_listed_without_filter(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        _acme_repo(make_git_repo)
        write_commit_hook_config(make_git_repo("beta"))

        out = _status().output

        assert "  \u26a0 acme:" in out
        assert "  \u26a0 beta:" in out

    def test_json_is_unchanged(self, make_git_repo: Callable[..., Path]) -> None:
        add_ticket(TicketTask(ticket_id="ACM-1", client="acme"))
        repo = make_git_repo("acme")
        before = _json()
        write_commit_hook_config(repo)

        after = _json()

        assert after == before
        assert all("commit_hook" not in v for v in after.values())

    def test_invalid_clients_yaml_prints_no_block(self) -> None:
        _write_bad_client_name()

        assert "Commit hooks:" not in _status().output


class TestCommitHookProbeBounds:
    """Status never lets the advisory probe hang or crash the command (#2973)."""

    def test_spent_budget_skips_every_probe(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # ``_status`` runs under freeze_time, which freezes time.monotonic, so
        # the real 5s budget cannot elapse here and is not slept through: a
        # budget of 0.0 is already spent at the first check.
        _acme_repo(make_git_repo)
        calls: list[Path] = []
        monkeypatch.setattr("cw.cli.dev_queue.status._COMMIT_HOOK_PROBE_BUDGET", 0.0)
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.commit_hook_state",
            lambda repo, **_kw: calls.append(repo),
        )

        with caplog.at_level(logging.WARNING, logger="cw.cli.dev_queue.status"):
            out = _status().output

        assert calls == []
        assert "Commit hooks:" not in out
        assert "commit-hook status probe skipped after" in caplog.text

    def test_probe_uses_the_short_timeout(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.cli.dev_queue import status as status_mod

        _acme_repo(make_git_repo)
        timeouts: list[float | None] = []
        real = status_mod.commit_hook_state

        def _spy(repo: Path, *, timeout: float = 10.0) -> object:
            timeouts.append(timeout)
            return real(repo, timeout=timeout)

        monkeypatch.setattr(status_mod, "commit_hook_state", _spy)

        _status()

        assert timeouts
        assert set(timeouts) == {status_mod._COMMIT_HOOK_PROBE_TIMEOUT}

    def test_a_raising_probe_is_logged_and_the_next_client_is_still_probed(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from cw.cli.dev_queue import status as status_mod

        _acme_repo(make_git_repo)
        write_commit_hook_config(make_git_repo("beta"))
        real = status_mod.commit_hook_state
        probed: list[str] = []

        def _flaky(repo: Path, *, timeout: float = 10.0) -> object:
            probed.append(repo.name)
            if repo.name == "acme":
                msg = "boom"
                raise RuntimeError(msg)
            return real(repo, timeout=timeout)

        monkeypatch.setattr(status_mod, "commit_hook_state", _flaky)

        with caplog.at_level(logging.ERROR, logger="cw.cli.dev_queue.status"):
            out = _status().output

        assert probed == ["acme", "beta"]
        assert "  \u26a0 beta:" in out
        assert "  \u26a0 acme:" not in out
        assert "commit-hook status probe failed for client acme" in caplog.text

    def test_transient_state_prints_no_block(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _acme_repo(make_git_repo)

        def _timeout(*_args: object, **_kwargs: object) -> Path:
            raise subprocess.TimeoutExpired(["git"], 2.0)

        monkeypatch.setattr("cw.doctor.commit_hook.git_hooks_dir", _timeout)

        assert "Commit hooks:" not in _status().output


def test_non_repository_commit_hook_probe_emits_no_git_stderr(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A real git failure is transient, including its otherwise inherited stderr."""
    _write_clients(tmp_path, "acme")
    write_commit_hook_config(tmp_path / "acme")

    result = _status()

    assert "Commit hooks:" not in result.output
    assert "fatal:" not in capfd.readouterr().err
