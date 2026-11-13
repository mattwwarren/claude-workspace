"""Tests for :mod:`cw.launch_marker` (#2615): the tri-state probe and verdict.

R9: only positive evidence of absence (``psutil.NoSuchProcess``, or a pid whose
start time moved past the tolerance) proves a dead owner. Everything unreadable
reads as alive.
"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path

import psutil
import pytest

import cw.launch_marker as launch_marker_mod
from cw.dispatch_loop_identity import START_TIME_TOLERANCE_NS
from cw.launch_marker import (
    LaunchOwnerState,
    ProbeState,
    ProcessProbe,
    current_launch_marker,
    in_initial_pid_namespace,
    launch_owner_state,
    observed_launch_owner_state,
    probe_process_start_time_ns,
    read_boot_id,
    read_since_boot_ns,
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


def _patch_pidns(monkeypatch: pytest.MonkeyPatch, *, initial: bool) -> None:
    def _fake(**_kwargs: object) -> bool:
        return initial

    monkeypatch.setattr(launch_marker_mod, "in_initial_pid_namespace", _fake)


class TestInitialPidNamespace:
    """The real function, imported before the conftest autouse patch."""

    @pytest.fixture
    def linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.launch_marker.sys.platform", "linux")

    @staticmethod
    def _link(proc_root: Path, target: str) -> None:
        ns = proc_root / "self" / "ns"
        ns.mkdir(parents=True)
        (ns / "pid").symlink_to(target)

    @pytest.mark.usefixtures("linux")
    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            pytest.param("pid:[4026531836]", True, id="initial"),
            pytest.param("pid:[4026532198]", False, id="container"),
        ],
    )
    def test_link_decides(self, tmp_path: Path, target: str, expected: bool) -> None:
        self._link(tmp_path, target)

        assert in_initial_pid_namespace(proc_root=tmp_path) is expected

    @pytest.mark.usefixtures("linux")
    def test_missing_link_fails_closed(self, tmp_path: Path) -> None:
        assert in_initial_pid_namespace(proc_root=tmp_path) is False

    @pytest.mark.usefixtures("linux")
    def test_unreadable_link_fails_closed(self, tmp_path: Path) -> None:
        """A path that is not a symlink makes ``readlink`` raise (EINVAL)."""
        ns = tmp_path / "self" / "ns"
        ns.mkdir(parents=True)
        (ns / "pid").write_text("pid:[4026531836]", encoding="utf-8")

        assert in_initial_pid_namespace(proc_root=tmp_path) is False

    def test_non_linux_counts_as_initial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.launch_marker.sys.platform", "darwin")

        assert in_initial_pid_namespace(proc_root=tmp_path) is True


class TestCurrentLaunchMarkerNamespaceGate:
    def test_no_marker_outside_the_initial_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_pidns(monkeypatch, initial=False)

        assert current_launch_marker() is None


class TestObservedLaunchOwnerState:
    @pytest.mark.parametrize(
        "probe",
        [
            pytest.param(ProcessProbe(ProbeState.ABSENT, None), id="absent"),
            pytest.param(
                ProcessProbe(
                    ProbeState.PRESENT,
                    _MARKER_START_NS + START_TIME_TOLERANCE_NS + 1,
                ),
                id="recycled",
            ),
        ],
    )
    def test_owner_gone_demoted_outside_the_initial_namespace(
        self, monkeypatch: pytest.MonkeyPatch, probe: ProcessProbe
    ) -> None:
        _patch_probe(monkeypatch, probe)
        _patch_pidns(monkeypatch, initial=False)

        assert observed_launch_owner_state(_marker()) is LaunchOwnerState.IN_FLIGHT

    def test_owner_gone_passes_in_the_initial_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_probe(monkeypatch, ProcessProbe(ProbeState.ABSENT, None))
        _patch_pidns(monkeypatch, initial=True)

        assert observed_launch_owner_state(_marker()) is LaunchOwnerState.OWNER_GONE

    @pytest.mark.parametrize("initial", [True, False], ids=["initial", "foreign"])
    def test_in_flight_and_unknown_are_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, initial: bool
    ) -> None:
        _patch_probe(monkeypatch, ProcessProbe(ProbeState.UNREADABLE, None))
        _patch_pidns(monkeypatch, initial=initial)

        assert observed_launch_owner_state(_marker()) is LaunchOwnerState.IN_FLIGHT
        assert observed_launch_owner_state(None) is LaunchOwnerState.UNKNOWN


# --- #3319: clock-independent identity --------------------------------------

_BOOT_A = "6f1c2a9e-0000-4000-8000-00000000000a"
_BOOT_B = "6f1c2a9e-0000-4000-8000-00000000000b"
_SINCE_BOOT_NS = 123_450_000_000
_TEN_SECONDS_NS = 10 * 1_000_000_000

# A real ``/proc/self/stat`` line captured on Linux, with pid, comm and
# starttime (field 22) swapped for placeholders (same capture as
# ``tests/test_doctor_worker_tmpdir.py``).
_CAPTURED_STAT = (
    "{pid} ({comm}) R 726423 726439 726423 0 -1 4194304 119 0 0 0 0 0 0 0 25 5 1 0 "
    "{starttime} 6209536 513 18446744073709551615 93885203886080 93885203909153 "
    "140734923420992 0 0 0 0 0 0 0 0 0 17 20 0 0 0 0 0 93885203921488 "
    "93885203923064 93886076616704 140734923430221 140734923430241 "
    "140734923430241 140734923436011 0\n"
)


def _write_stat(proc_root: Path, pid: int, text: str) -> None:
    (proc_root / str(pid)).mkdir(parents=True, exist_ok=True)
    (proc_root / str(pid) / "stat").write_text(text, encoding="utf-8")


def _write_boot_id(proc_root: Path, boot_id: str) -> None:
    path = proc_root / "sys" / "kernel" / "random" / "boot_id"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(boot_id + "\n", encoding="ascii")


def _patch_boot_id(monkeypatch: pytest.MonkeyPatch, boot_id: str | None) -> None:
    def _fake(**_kwargs: object) -> str | None:
        return boot_id

    monkeypatch.setattr(launch_marker_mod, "read_boot_id", _fake)


def _new_marker() -> LaunchMarker:
    return LaunchMarker(
        pid=_MARKER_PID,
        start_time_ns=_MARKER_START_NS,
        start_since_boot_ns=_SINCE_BOOT_NS,
        boot_id=_BOOT_A,
    )


def _present(wall_offset_ns: int = 0, since_offset_ns: int | None = 0) -> ProcessProbe:
    since = None if since_offset_ns is None else _SINCE_BOOT_NS + since_offset_ns
    return ProcessProbe(ProbeState.PRESENT, _MARKER_START_NS + wall_offset_ns, since)


class TestReadSinceBootNs:
    @pytest.mark.parametrize(
        "comm", ["python3", "a) b (c", "two words"], ids=["plain", "paren", "space"]
    )
    def test_field_22_is_converted_with_clk_tck(
        self, tmp_path: Path, comm: str
    ) -> None:
        hertz = os.sysconf("SC_CLK_TCK")
        _write_stat(
            tmp_path, 7, _CAPTURED_STAT.format(pid=7, comm=comm, starttime=hertz * 3)
        )

        assert read_since_boot_ns(7, proc_root=tmp_path) == 3 * 1_000_000_000

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("7 (python3) R 1 2 3\n", id="short"),
            pytest.param("7 python3 R " + "0 " * 30, id="no-comm-close"),
            pytest.param(
                _CAPTURED_STAT.format(pid=7, comm="x", starttime="garbled"),
                id="non-int",
            ),
            pytest.param(
                _CAPTURED_STAT.format(pid=7, comm="x", starttime="-5"), id="negative"
            ),
        ],
    )
    def test_short_or_garbled_stat_is_none(self, tmp_path: Path, text: str) -> None:
        _write_stat(tmp_path, 7, text)

        assert read_since_boot_ns(7, proc_root=tmp_path) is None

    def test_missing_stat_is_none(self, tmp_path: Path) -> None:
        assert read_since_boot_ns(7, proc_root=tmp_path) is None

    def test_unusable_clk_tck_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_stat(tmp_path, 7, _CAPTURED_STAT.format(pid=7, comm="x", starttime=5))
        monkeypatch.setattr(launch_marker_mod.os, "sysconf", lambda _name: 0)

        assert read_since_boot_ns(7, proc_root=tmp_path) is None


class TestReadBootId:
    def test_reads_and_strips(self, tmp_path: Path) -> None:
        _write_boot_id(tmp_path, _BOOT_A)

        assert read_boot_id(proc_root=tmp_path) == _BOOT_A

    def test_missing_is_none(self, tmp_path: Path) -> None:
        assert read_boot_id(proc_root=tmp_path) is None

    def test_empty_is_none(self, tmp_path: Path) -> None:
        _write_boot_id(tmp_path, "")

        assert read_boot_id(proc_root=tmp_path) is None

    def test_non_ascii_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "sys" / "kernel" / "random" / "boot_id"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe")

        assert read_boot_id(proc_root=tmp_path) is None


class TestSinceBootVerdict:
    def test_present_probe_without_since_boot_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A two-field probe: no since-boot value, and no wall-clock fallback.
        _patch_probe(
            monkeypatch,
            ProcessProbe(ProbeState.PRESENT, _MARKER_START_NS + _TEN_SECONDS_NS),
        )
        _patch_boot_id(monkeypatch, _BOOT_A)

        assert launch_owner_state(_new_marker()) is LaunchOwnerState.IN_FLIGHT

    @pytest.mark.parametrize(
        "wall_offset_ns", [_TEN_SECONDS_NS, -_TEN_SECONDS_NS], ids=["ahead", "behind"]
    )
    def test_wall_clock_step_is_still_in_flight(
        self, monkeypatch: pytest.MonkeyPatch, wall_offset_ns: int
    ) -> None:
        """The #3319 regression: a step moves the wall-clock start only."""
        _patch_probe(monkeypatch, _present(wall_offset_ns=wall_offset_ns))
        _patch_boot_id(monkeypatch, _BOOT_A)

        assert launch_owner_state(_new_marker()) is LaunchOwnerState.IN_FLIGHT

    def test_since_boot_within_tolerance_is_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_probe(monkeypatch, _present(since_offset_ns=START_TIME_TOLERANCE_NS))
        _patch_boot_id(monkeypatch, _BOOT_A)

        assert launch_owner_state(_new_marker()) is LaunchOwnerState.IN_FLIGHT

    def test_since_boot_drift_same_boot_is_a_recycled_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_probe(monkeypatch, _present(since_offset_ns=START_TIME_TOLERANCE_NS + 1))
        _patch_boot_id(monkeypatch, _BOOT_A)

        assert launch_owner_state(_new_marker()) is LaunchOwnerState.OWNER_GONE

    def test_other_boot_is_owner_gone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_probe(monkeypatch, _present())
        _patch_boot_id(monkeypatch, _BOOT_B)

        assert launch_owner_state(_new_marker()) is LaunchOwnerState.OWNER_GONE

    def test_unreadable_boot_id_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_probe(monkeypatch, _present(since_offset_ns=_TEN_SECONDS_NS))
        _patch_boot_id(monkeypatch, None)

        assert launch_owner_state(_new_marker()) is LaunchOwnerState.IN_FLIGHT

    def test_absent_probe_is_still_owner_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_probe(monkeypatch, ProcessProbe(ProbeState.ABSENT, None))

        assert launch_owner_state(_new_marker()) is LaunchOwnerState.OWNER_GONE

    @pytest.mark.parametrize(
        "marker",
        [
            pytest.param(_marker(), id="neither"),
            pytest.param(
                LaunchMarker(
                    pid=_MARKER_PID,
                    start_time_ns=_MARKER_START_NS,
                    start_since_boot_ns=_SINCE_BOOT_NS,
                ),
                id="no-boot-id",
            ),
            pytest.param(
                LaunchMarker(
                    pid=_MARKER_PID, start_time_ns=_MARKER_START_NS, boot_id=_BOOT_A
                ),
                id="no-since-boot",
            ),
        ],
    )
    def test_legacy_marker_keeps_the_wall_clock_verdict(
        self, monkeypatch: pytest.MonkeyPatch, marker: LaunchMarker
    ) -> None:
        _patch_probe(monkeypatch, _present(wall_offset_ns=_TEN_SECONDS_NS))
        _patch_boot_id(monkeypatch, _BOOT_A)

        assert launch_owner_state(marker) is LaunchOwnerState.OWNER_GONE


class _SteppingProcess:
    """``psutil.Process`` stand-in whose wall-clock start jumps after one read."""

    def __init__(self, first_s: float, step_s: float) -> None:
        self._reads = 0
        self._first_s = first_s
        self._step_s = step_s

    def __call__(self, _pid: int) -> _SteppingProcess:
        return self

    def create_time(self) -> float:
        self._reads += 1
        return self._first_s + (self._step_s if self._reads > 1 else 0.0)


def _fake_proc(monkeypatch: pytest.MonkeyPatch, proc_root: Path) -> None:
    monkeypatch.setattr(
        launch_marker_mod,
        "read_since_boot_ns",
        partial(read_since_boot_ns, proc_root=proc_root),
    )
    monkeypatch.setattr(
        launch_marker_mod, "read_boot_id", partial(read_boot_id, proc_root=proc_root)
    )


class TestCurrentLaunchMarkerSinceBoot:
    def test_sets_both_fields_when_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_pidns(monkeypatch, initial=True)
        hertz = os.sysconf("SC_CLK_TCK")
        pid = os.getpid()
        _write_stat(
            tmp_path, pid, _CAPTURED_STAT.format(pid=pid, comm="x", starttime=hertz)
        )
        _write_boot_id(tmp_path, _BOOT_A)
        _fake_proc(monkeypatch, tmp_path)

        marker = current_launch_marker()

        assert marker is not None
        assert marker.start_since_boot_ns == 1_000_000_000
        assert marker.boot_id == _BOOT_A

    @pytest.mark.parametrize(
        ("since", "boot"),
        [(None, _BOOT_A), (_SINCE_BOOT_NS, None), (None, None)],
        ids=["no-since-boot", "no-boot-id", "neither"],
    )
    def test_sets_neither_when_either_is_unreadable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        since: int | None,
        boot: str | None,
    ) -> None:
        _patch_pidns(monkeypatch, initial=True)
        _patch_probe(monkeypatch, ProcessProbe(ProbeState.PRESENT, 5, since))
        _patch_boot_id(monkeypatch, boot)

        marker = current_launch_marker()

        assert marker is not None
        assert marker.start_since_boot_ns is None
        assert marker.boot_id is None

    def test_real_linux_marker_carries_the_pair(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not Path("/proc/self/stat").is_file():
            pytest.skip("no /proc on this platform")
        _patch_pidns(monkeypatch, initial=True)

        marker = current_launch_marker()

        assert marker is not None
        assert marker.start_since_boot_ns is not None
        assert marker.boot_id == read_boot_id()

    def test_end_to_end_wall_clock_step_stays_in_flight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Marker written, then the wall clock steps +10 s before the verdict;
        the fake ``/proc`` start tick and boot id are unchanged."""
        _patch_pidns(monkeypatch, initial=True)
        pid = os.getpid()
        _write_stat(
            tmp_path, pid, _CAPTURED_STAT.format(pid=pid, comm="x", starttime=4242)
        )
        _write_boot_id(tmp_path, _BOOT_A)
        _fake_proc(monkeypatch, tmp_path)
        monkeypatch.setattr(psutil, "Process", _SteppingProcess(1_700_000_000.0, 10.0))

        marker = current_launch_marker()

        assert marker is not None
        assert marker.boot_id == _BOOT_A
        assert launch_owner_state(marker) is LaunchOwnerState.IN_FLIGHT


class TestProbeSinceBoot:
    def test_live_pid_carries_since_boot(self) -> None:
        if not Path("/proc/self/stat").is_file():
            pytest.skip("no /proc on this platform")

        probe = probe_process_start_time_ns(os.getpid())

        assert probe.since_boot_ns == read_since_boot_ns(os.getpid())
        assert probe.since_boot_ns is not None

    @pytest.mark.parametrize(
        "exc",
        [psutil.NoSuchProcess(_MARKER_PID), psutil.AccessDenied(_MARKER_PID)],
        ids=["absent", "unreadable"],
    )
    def test_absent_or_unreadable_has_no_since_boot(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        _patch_process(monkeypatch, exc)

        assert probe_process_start_time_ns(_MARKER_PID).since_boot_ns is None
