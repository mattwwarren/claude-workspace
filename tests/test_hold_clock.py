"""Self-tests for ``tests/_hold_clock.py`` (#3585)."""

from __future__ import annotations

import shlex
import subprocess
import time
from typing import TYPE_CHECKING

from tests._hold_clock import Hold, HoldClock, wait_for_path

if TYPE_CHECKING:
    from pathlib import Path


def test_clock_does_not_advance_without_a_marker(tmp_path: Path) -> None:
    clock = HoldClock(Hold(tmp_path))

    assert clock.monotonic() == 0.0
    assert clock.monotonic() == 0.0
    assert clock.total_held_reads == 0


def test_clock_advances_by_step_per_read_while_marker_exists(tmp_path: Path) -> None:
    hold = Hold(tmp_path)
    clock = HoldClock(hold, step=0.5, polls=10)
    hold.marker.touch()

    assert clock.monotonic() == 0.5
    assert clock.monotonic() == 1.0
    assert clock.held_reads == 2
    assert clock.total_held_reads == 2


def test_release_appears_exactly_at_read_polls(tmp_path: Path) -> None:
    hold = Hold(tmp_path)
    clock = HoldClock(hold, polls=3)
    hold.marker.touch()

    clock.monotonic()
    clock.monotonic()
    assert not hold.release.exists()

    clock.monotonic()
    assert hold.release.exists()


def test_held_reads_reset_when_marker_is_gone_but_total_is_monotone(
    tmp_path: Path,
) -> None:
    hold = Hold(tmp_path)
    clock = HoldClock(hold, polls=10)
    hold.marker.touch()
    clock.monotonic()
    clock.monotonic()

    hold.marker.unlink()
    clock.monotonic()

    assert clock.held_reads == 0
    assert clock.total_held_reads == 2
    assert clock.now == 2.0


def test_shell_quotes_paths_and_bounds_the_loop(tmp_path: Path) -> None:
    hold = Hold(tmp_path / "with space")

    cmd = hold.shell("echo ran")

    assert shlex.quote(str(hold.marker)) in cmd
    assert shlex.quote(str(hold.release)) in cmd
    assert "-lt 3000" in cmd
    assert cmd.endswith("; echo ran")


def test_wait_for_path_true_for_existing_and_false_after_deadline(
    tmp_path: Path,
) -> None:
    present = tmp_path / "present"
    present.touch()

    assert wait_for_path(present) is True
    assert wait_for_path(tmp_path / "absent", deadline_s=0.05) is False


def test_real_child_is_released_by_the_clock_and_runs_its_tail(
    tmp_path: Path,
) -> None:
    hold = Hold(tmp_path)
    out = tmp_path / "ran"
    clock = HoldClock(hold, polls=3)
    cmd = hold.shell(f"echo ran > {out}")

    proc = subprocess.Popen(["sh", "-c", cmd])
    try:
        assert wait_for_path(hold.marker)
        deadline = time.monotonic() + 30.0
        while not hold.release.exists() and time.monotonic() < deadline:
            clock.monotonic()
            time.sleep(0.02)
        assert proc.wait(timeout=30.0) == 0
    finally:
        hold.release.touch()
        proc.wait(timeout=30.0)

    assert out.read_text(encoding="utf-8") == "ran\n"
    assert not hold.marker.exists()
