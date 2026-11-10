"""Tests for :mod:`cw.launch_marker` (#2615): the tri-state probe and verdict.

R9: only positive evidence of absence (``psutil.NoSuchProcess``, or a pid whose
start time moved past the tolerance) proves a dead owner. Everything unreadable
reads as alive.
"""

from __future__ import annotations

import os

import psutil
import pytest

import cw.launch_marker as launch_marker_mod
from cw.dispatch_loop_identity import START_TIME_TOLERANCE_NS
from cw.launch_marker import (
    LaunchOwnerState,
    ProbeState,
    ProcessProbe,
    current_launch_marker,
    launch_owner_state,
    probe_process_start_time_ns,
)
from cw.local_runner import read_process_start_time_ns
from cw.models import LaunchMarker

# Above every default pid_max (Linux caps it at 2**22), so nothing runs there.
_NONEXISTENT_PID = 2_000_000_000
_MARKER_START_NS = 1_700_000_000_000_000_000
_MARKER_PID = 4242


class _RaisingProcess:
    """Stand-in for ``psutil.Process`` whose ``create_time`` raises *exc*."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self, _pid: int) -> _RaisingProcess:
        return self

    def create_time(self) -> float:
        raise self._exc


def _patch_process(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    # cw.launch_marker resolves ``psutil.Process`` at call time.
    monkeypatch.setattr(psutil, "Process", _RaisingProcess(exc))


def _patch_probe(monkeypatch: pytest.MonkeyPatch, probe: ProcessProbe) -> None:
    monkeypatch.setattr(
        launch_marker_mod, "probe_process_start_time_ns", lambda _pid: probe
    )


def _marker() -> LaunchMarker:
    return LaunchMarker(pid=_MARKER_PID, start_time_ns=_MARKER_START_NS)


class TestProbeProcessStartTimeNs:
    def test_live_pid_is_present_and_matches_the_existing_reader(self) -> None:
        probe = probe_process_start_time_ns(os.getpid())

        existing = read_process_start_time_ns(os.getpid())
        assert probe.state is ProbeState.PRESENT
        assert probe.start_time_ns is not None
        assert existing is not None
        assert abs(probe.start_time_ns - existing) <= START_TIME_TOLERANCE_NS

    def test_real_nonexistent_pid_is_absent(self) -> None:
        probe = probe_process_start_time_ns(_NONEXISTENT_PID)

        assert probe == ProcessProbe(ProbeState.ABSENT, None)

    def test_no_such_process_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_process(monkeypatch, psutil.NoSuchProcess(_MARKER_PID))

        probe = probe_process_start_time_ns(_MARKER_PID)

        assert probe == ProcessProbe(ProbeState.ABSENT, None)

    def test_zombie_is_unreadable_not_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The handler order is load-bearing: a zombie IS a NoSuchProcess.
        assert issubclass(psutil.ZombieProcess, psutil.NoSuchProcess)
        _patch_process(monkeypatch, psutil.ZombieProcess(_MARKER_PID))

        probe = probe_process_start_time_ns(_MARKER_PID)

        assert probe == ProcessProbe(ProbeState.UNREADABLE, None)

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(psutil.AccessDenied(_MARKER_PID), id="access-denied"),
            pytest.param(psutil.TimeoutExpired(1.0, _MARKER_PID), id="timeout"),
        ],
    )
    def test_other_psutil_errors_are_unreadable(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        _patch_process(monkeypatch, exc)

        probe = probe_process_start_time_ns(_MARKER_PID)

        assert probe == ProcessProbe(ProbeState.UNREADABLE, None)

    def test_real_negative_pid_is_unreadable(self) -> None:
        probe = probe_process_start_time_ns(-1)

        assert probe == ProcessProbe(ProbeState.UNREADABLE, None)


class TestLaunchOwnerState:
    def test_no_marker_is_unknown(self) -> None:
        assert launch_owner_state(None) is LaunchOwnerState.UNKNOWN

    def test_absent_probe_is_owner_gone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_probe(monkeypatch, ProcessProbe(ProbeState.ABSENT, None))

        assert launch_owner_state(_marker()) is LaunchOwnerState.OWNER_GONE

    @pytest.mark.parametrize(
        "offset_ns",
        [
            pytest.param(0, id="equal"),
            pytest.param(1_000_000_000, id="one-second-wobble"),
            pytest.param(-1_000_000_000, id="one-second-wobble-earlier"),
            pytest.param(START_TIME_TOLERANCE_NS, id="exactly-the-tolerance"),
        ],
    )
    def test_present_within_tolerance_is_in_flight(
        self, monkeypatch: pytest.MonkeyPatch, offset_ns: int
    ) -> None:
        _patch_probe(
            monkeypatch,
            ProcessProbe(ProbeState.PRESENT, _MARKER_START_NS + offset_ns),
        )

        assert launch_owner_state(_marker()) is LaunchOwnerState.IN_FLIGHT

    def test_present_past_tolerance_is_a_recycled_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_probe(
            monkeypatch,
            ProcessProbe(
                ProbeState.PRESENT, _MARKER_START_NS + START_TIME_TOLERANCE_NS + 1
            ),
        )

        assert launch_owner_state(_marker()) is LaunchOwnerState.OWNER_GONE

    def test_unreadable_probe_is_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_probe(monkeypatch, ProcessProbe(ProbeState.UNREADABLE, None))

        assert launch_owner_state(_marker()) is LaunchOwnerState.IN_FLIGHT

    def test_end_to_end_this_process_is_in_flight(self) -> None:
        assert launch_owner_state(current_launch_marker()) is LaunchOwnerState.IN_FLIGHT

    def test_end_to_end_nonexistent_pid_is_owner_gone(self) -> None:
        marker = LaunchMarker(pid=_NONEXISTENT_PID, start_time_ns=_MARKER_START_NS)

        assert launch_owner_state(marker) is LaunchOwnerState.OWNER_GONE


class TestCurrentLaunchMarker:
    def test_marker_names_this_process(self) -> None:
        marker = current_launch_marker()

        assert marker is not None
        assert marker.pid == os.getpid()
        probe = probe_process_start_time_ns(os.getpid())
        assert marker.start_time_ns == probe.start_time_ns

    @pytest.mark.parametrize(
        "probe",
        [
            pytest.param(ProcessProbe(ProbeState.UNREADABLE, None), id="unreadable"),
            pytest.param(ProcessProbe(ProbeState.ABSENT, None), id="absent"),
        ],
    )
    def test_no_readable_start_time_gives_no_marker(
        self, monkeypatch: pytest.MonkeyPatch, probe: ProcessProbe
    ) -> None:
        _patch_probe(monkeypatch, probe)

        assert current_launch_marker() is None
