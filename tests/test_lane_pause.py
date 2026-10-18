"""The shared lane-pause view, predicates and renderer (#2696).

1:1 test module of ``cw.lane_pause``: the display contract ``cw dev-queue
status``, ``cw lane ls`` and ``cw dev-queue show`` all render through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cw.lane_pause import (
    WORKTREE_BREAKER_MULTIPLIER,
    LanePauseSource,
    LanePauseTrigger,
    LanePauseView,
    breaker_tripped,
    build_lane_pause_view,
    iso_z,
    paused_lane_views,
    render_lane_pause_lines,
    utc_now_seconds,
    worktree_breaker_threshold,
)
from freezegun import freeze_time
from pydantic import ValidationError

from cw.models import (
    ClientConfig,
    ConcurrencyOverrides,
    LaneConcurrencyOverride,
    LaneConfig,
)

_T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
_NOW = _T0 + timedelta(minutes=5)


def _breaker(**updates: object) -> LaneConcurrencyOverride:
    base = LaneConcurrencyOverride(
        paused=True,
        paused_at=_T0,
        pause_source="circuit_breaker",
        pause_trigger="spawn_errors",
        consecutive_spawn_errors=3,
        recent_spawn_errors=["boom"],
        circuit_trip_streak=1,
        circuit_auto_resume_at=_T0 + timedelta(minutes=15),
    )
    return base.model_copy(update=updates)


def _view(
    override: LaneConcurrencyOverride | None, *, auto_resume_enabled: bool = True
) -> LanePauseView:
    return build_lane_pause_view(
        "acme", "default", override, auto_resume_enabled=auto_resume_enabled
    )


def _client(name: str, *lanes: LaneConfig) -> ClientConfig:
    return ClientConfig(name=name, workspace_path=f"/ws/{name}", lanes=list(lanes))


class TestThresholds:
    @pytest.mark.parametrize(("threshold", "expected"), [(1, 3), (2, 6), (3, 9)])
    def test_worktree_threshold_is_three_times_the_breaker(
        self, threshold: int, expected: int
    ) -> None:
        assert WORKTREE_BREAKER_MULTIPLIER == 3
        assert worktree_breaker_threshold(threshold) == expected

    @pytest.mark.parametrize(
        ("spawn", "worktree", "tripped"),
        [(0, 0, False), (2, 5, False), (3, 0, True), (0, 9, True), (2, 8, False)],
    )
    def test_breaker_tripped(self, spawn: int, worktree: int, tripped: bool) -> None:
        override = LaneConcurrencyOverride(
            consecutive_spawn_errors=spawn, consecutive_worktree_errors=worktree
        )
        assert breaker_tripped(override, threshold=3) is tripped


class TestTimestamps:
    def test_utc_now_seconds_drops_microseconds(self) -> None:
        with freeze_time(datetime(2026, 10, 18, 13, 19, 36, 961607, tzinfo=UTC)):
            now = utc_now_seconds()
        assert now == datetime(2026, 10, 18, 13, 19, 36, tzinfo=UTC)
        assert now.microsecond == 0

    def test_iso_z(self) -> None:
        assert iso_z(_T0) == "2026-10-18T12:00:00Z"
        assert iso_z(_T0.replace(microsecond=5)) == "2026-10-18T12:00:00Z"


class TestBuildLanePauseView:
    @pytest.mark.parametrize(
        ("trigger", "expected"),
        [
            ("spawn_errors", LanePauseTrigger.SPAWN_ERRORS),
            ("worktree_errors", LanePauseTrigger.WORKTREE_ERRORS),
        ],
    )
    def test_circuit_breaker(self, trigger: str, expected: LanePauseTrigger) -> None:
        view = _view(_breaker(pause_trigger=trigger))

        assert view.source == LanePauseSource.CIRCUIT_BREAKER
        assert view.trigger == expected
        assert view.since == _T0
        assert view.trip_streak == 1
        assert view.recent_errors == ["boom"]
        assert view.auto_resume_at == _T0 + timedelta(minutes=15)
        assert view.auto_resume_enabled is True
        assert view.resume_command == "cw lane resume acme default"

    def test_operator(self) -> None:
        view = _view(
            LaneConcurrencyOverride(paused=True, paused_at=_T0, pause_source="operator")
        )

        assert view.source == LanePauseSource.OPERATOR
        assert view.trigger is None
        assert view.since == _T0
        assert view.trip_streak == 0
        assert view.auto_resume_at is None

    def test_legacy(self) -> None:
        view = _view(LaneConcurrencyOverride(paused=True, consecutive_spawn_errors=5))

        assert view.source == LanePauseSource.LEGACY
        assert view.trigger is None
        assert view.since is None
        assert view.trip_streak == 0
        assert view.recent_errors == []
        assert view.auto_resume_at is None

    def test_unknown_persisted_source_reads_as_legacy(self) -> None:
        view = _view(LaneConcurrencyOverride(paused=True, pause_source="martian"))

        assert view.source == LanePauseSource.LEGACY

    def test_unknown_persisted_trigger_is_dropped(self) -> None:
        view = _view(_breaker(pause_trigger="martian"))

        assert view.trigger is None

    @pytest.mark.parametrize(
        "override", [None, LaneConcurrencyOverride(consecutive_spawn_errors=3)]
    )
    def test_declared(self, override: LaneConcurrencyOverride | None) -> None:
        view = _view(override)

        assert view.source == LanePauseSource.DECLARED
        assert view.trigger is None
        assert view.since is None
        assert view.trip_streak == 0
        assert view.recent_errors == []
        assert view.auto_resume_at is None

    def test_disabled_auto_resume_forces_no_schedule(self) -> None:
        view = _view(_breaker(), auto_resume_enabled=False)

        assert view.auto_resume_at is None
        assert view.auto_resume_enabled is False

    def test_json_shape(self) -> None:
        assert _view(_breaker()).model_dump(mode="json") == {
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

    def test_extra_fields_forbidden(self) -> None:
        payload = _view(_breaker()).model_dump()
        payload["surprise"] = 1
        with pytest.raises(ValidationError):
            LanePauseView.model_validate(payload)


class TestPausedLaneViews:
    def test_union_sorted_with_override_only_lane(self) -> None:
        clients = {
            "zeta": _client("zeta", LaneConfig(name="default", paused=True)),
            "acme": _client(
                "acme", LaneConfig(name="slow", paused=True), LaneConfig(name="fast")
            ),
        }
        overrides = ConcurrencyOverrides(
            lanes={
                "acme/slow": LaneConcurrencyOverride(paused=True),
                "ghost/default": LaneConcurrencyOverride(paused=True),
                "acme/fast": LaneConcurrencyOverride(paused=None),
            }
        )

        views = paused_lane_views(clients, overrides, auto_resume_enabled=True)

        assert [(v.client, v.lane, v.source) for v in views] == [
            ("acme", "slow", LanePauseSource.LEGACY),
            ("ghost", "default", LanePauseSource.LEGACY),
            ("zeta", "default", LanePauseSource.DECLARED),
        ]

    def test_override_unpause_suppresses_a_declared_pause(self) -> None:
        # Effective clients already carry the override's paused=False.
        clients = {"acme": _client("acme", LaneConfig(name="default", paused=False))}
        overrides = ConcurrencyOverrides(
            lanes={"acme/default": LaneConcurrencyOverride(paused=False)}
        )

        assert paused_lane_views(clients, overrides, auto_resume_enabled=True) == []

    def test_none_paused(self) -> None:
        clients = {"acme": _client("acme")}
        assert (
            paused_lane_views(clients, ConcurrencyOverrides(), auto_resume_enabled=True)
            == []
        )


def _render(view: LanePauseView) -> list[str]:
    return render_lane_pause_lines(view, now=_NOW)


class TestRenderer:
    def test_circuit_breaker_full_block(self) -> None:
        assert _render(_view(_breaker())) == [
            "paused since 2026-10-18T12:00:00Z (5m ago) by circuit_breaker"
            " (spawn errors, trip #1)",
            "  last spawn errors (oldest first):",
            "    - boom",
            "  auto-resume: 2026-10-18T12:15:00Z (in 10m)",
            "  resume: cw lane resume acme default",
        ]

    def test_worktree_trigger_wording(self) -> None:
        lines = _render(_view(_breaker(pause_trigger="worktree_errors")))

        assert lines[0].endswith("(worktree setup errors, trip #1)")

    def test_due_auto_resume(self) -> None:
        view = _view(_breaker(circuit_auto_resume_at=_T0))

        assert (
            "  auto-resume: 2026-10-18T12:00:00Z (due; resumes on the next"
            " dispatch tick)"
        ) in _render(view)

    def test_auto_resume_off(self) -> None:
        lines = _render(_view(_breaker(), auto_resume_enabled=False))

        assert "  auto-resume: off (lane_circuit_auto_resume_minutes=0)" in lines

    def test_no_schedule_while_enabled(self) -> None:
        lines = _render(_view(_breaker(circuit_auto_resume_at=None)))

        assert (
            "  auto-resume: no schedule (tripped before auto-resume was enabled)"
            in lines
        )

    def test_circuit_breaker_without_a_stamp(self) -> None:
        lines = _render(_view(_breaker(paused_at=None)))

        assert lines[0] == (
            "paused since unknown by circuit_breaker (spawn errors, trip #1)"
        )

    def test_operator(self) -> None:
        view = _view(
            LaneConcurrencyOverride(paused=True, paused_at=_T0, pause_source="operator")
        )

        assert _render(view) == [
            "paused since 2026-10-18T12:00:00Z (5m ago) by operator",
            "  auto-resume: no (operator-held)",
            "  resume: cw lane resume acme default",
        ]

    def test_legacy(self) -> None:
        view = _view(LaneConcurrencyOverride(paused=True))

        assert _render(view) == [
            "paused: legacy, since unknown (no pause metadata recorded)",
            "  auto-resume: no (legacy pause is operator-held)",
            "  resume: cw lane resume acme default",
        ]

    def test_declared(self) -> None:
        assert _render(_view(None)) == [
            "paused: declared in clients.yaml",
            "  auto-resume: no (declared in clients.yaml)",
            "  resume: cw lane resume acme default (overrides clients.yaml),"
            " or remove paused: true from clients.yaml",
        ]

    def test_errors_listed_oldest_first(self) -> None:
        lines = _render(_view(_breaker(recent_spawn_errors=["first", "second"])))

        assert lines[1:4] == [
            "  last spawn errors (oldest first):",
            "    - first",
            "    - second",
        ]
