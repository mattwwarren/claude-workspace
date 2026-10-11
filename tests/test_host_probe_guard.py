"""Tests for ``tests/_host_probe_guard.py`` -- host-state probe isolation (#2664).

The helpers are exercised directly here with a throwaway denylist, so they
are proven independent of ``tests.conftest._OPTIONAL_BINARY_DENYLIST``. The
autouse wiring (``_hide_optional_binaries`` and ``_isolate_process_table``)
is pinned separately in ``tests/test_conftest.py``.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from tests._host_probe_guard import (
    blocked_binary,
    empty_process_table,
    install_binary_launch_guard,
    scoped_process_iter,
)
from tests.conftest import _write_bin_stub

_DENY = frozenset({"alpha", "beta"})
_NO_FORCED: frozenset[str] = frozenset()
_SCRIPT = "#!/bin/sh\nexit 0\n"


class TestBlockedBinary:
    """``blocked_binary`` decides which ``Popen`` args the guard refuses."""

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (["alpha"], "alpha"),
            (("beta", "--version"), "beta"),
            (["alpha", "x"], "alpha"),
        ],
    )
    def test_bare_denylisted_name_is_blocked(self, args: object, expected: str) -> None:
        assert blocked_binary(args, _DENY, _NO_FORCED) == expected

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(["/usr/bin/alpha"], id="absolute-path"),
            pytest.param(["./alpha"], id="relative-path"),
            pytest.param(["git", "status"], id="not-denylisted"),
            pytest.param([], id="empty-list"),
            pytest.param((), id="empty-tuple"),
            pytest.param("alpha --version", id="shell-string"),
            pytest.param(b"alpha", id="bytes"),
            pytest.param([Path("alpha")], id="pathlike-first-element"),
            pytest.param([b"alpha"], id="bytes-first-element"),
            pytest.param(None, id="not-a-sequence"),
        ],
    )
    def test_everything_else_passes_through(self, args: object) -> None:
        assert blocked_binary(args, _DENY, _NO_FORCED) is None

    def test_forced_present_name_passes_through(self) -> None:
        assert blocked_binary(["alpha"], _DENY, frozenset({"alpha"})) is None
        assert blocked_binary(["beta"], _DENY, frozenset({"alpha"})) == "beta"


class TestInstallBinaryLaunchGuard:
    """``install_binary_launch_guard`` wraps ``Popen.__init__`` and raises ENOENT."""

    @staticmethod
    def _put_on_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> Path:
        bin_dir = _write_bin_stub(tmp_path, name, _SCRIPT)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        return bin_dir

    def test_guard_turns_a_present_binary_into_filenotfound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._put_on_path(tmp_path, monkeypatch, "alpha")
        # Control: without the guard the stub on PATH really runs.
        assert subprocess.run(["alpha"], check=False).returncode == 0

        install_binary_launch_guard(monkeypatch, _DENY, _NO_FORCED)

        with pytest.raises(FileNotFoundError) as excinfo:
            subprocess.run(["alpha"], check=False)
        assert excinfo.value.errno == errno.ENOENT
        assert excinfo.value.filename == "alpha"

    def test_guard_covers_the_args_keyword_and_check_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._put_on_path(tmp_path, monkeypatch, "beta")
        install_binary_launch_guard(monkeypatch, _DENY, _NO_FORCED)

        with pytest.raises(FileNotFoundError):
            subprocess.Popen(args=["beta"])
        with pytest.raises(FileNotFoundError):
            subprocess.check_output(("beta", "--version"))

    def test_guard_leaves_other_launches_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = self._put_on_path(tmp_path, monkeypatch, "alpha")
        install_binary_launch_guard(monkeypatch, _DENY, _NO_FORCED)

        by_path = subprocess.run([str(bin_dir / "alpha")], check=False)
        interpreter = subprocess.run([sys.executable, "-c", "pass"], check=False)

        assert by_path.returncode == 0
        assert interpreter.returncode == 0

    def test_forced_present_name_still_launches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._put_on_path(tmp_path, monkeypatch, "alpha")
        install_binary_launch_guard(monkeypatch, _DENY, frozenset({"alpha"}))

        assert subprocess.run(["alpha"], check=False).returncode == 0


class TestEmptyProcessTable:
    """``empty_process_table`` is a drop-in for ``psutil.process_iter``."""

    def test_yields_nothing(self) -> None:
        assert list(empty_process_table()) == []

    def test_accepts_attrs_and_ad_value(self) -> None:
        assert list(empty_process_table(["name", "cwd"])) == []
        assert list(empty_process_table(["name", "cwd"], "denied")) == []


class TestScopedProcessIter:
    """``scoped_process_iter`` keeps real kernel entries for owned pids only.

    The autouse ``_isolate_process_table`` fixture has already replaced
    ``psutil.process_iter`` with the empty table, so every positive assertion
    here also proves the helper reads through the real iterator captured at
    import time rather than whatever ``psutil.process_iter`` currently is.
    """

    def test_yields_exactly_the_owned_pid_with_real_attributes(self) -> None:
        scoped = scoped_process_iter({os.getpid()})

        found = list(scoped(["name", "cwd"]))

        assert [process.pid for process in found] == [os.getpid()]
        assert found[0].info["name"] == psutil.Process().name()

    def test_a_pid_absent_from_the_table_is_simply_missing(self) -> None:
        absent = max(psutil.pids()) + 1

        assert list(scoped_process_iter({absent})(["name"])) == []

    def test_an_empty_scope_yields_nothing(self) -> None:
        assert list(scoped_process_iter(set())(["name"])) == []
