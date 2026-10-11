"""Tests for the ``worker-tmpdir-scope`` doctor check (#2672).

The ``run_doctor()`` registration case lives in ``tests/test_doctor.py``: it
needs that file's module-private ``_stub_claude_version_ok`` (the same split as
``tests/test_doctor_user_level_hooks.py``).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.doctor.worker_tmpdir import _check_worker_tmpdir_scope
from cw.models import HOOK_CONTEXT_RELATIVE_PATH

if TYPE_CHECKING:
    from cw.doctor._shared import CheckResult
    from cw.native_daemon import FakeNativeDaemonClient

_COUNTS_LABEL = "configured via settings.local.json (effective value not observable"

# A real ``/proc/self/stat`` line captured on Linux (``cat``, pid 726439),
# with pid, comm and starttime (field 22) swapped for placeholders. Every other
# field is kept as captured so the field positions are the observed ones.
_CAPTURED_STAT = (
    "{pid} ({comm}) R 726423 726439 726423 0 -1 4194304 119 0 0 0 0 0 0 0 25 5 1 0 "
    "{starttime} 6209536 513 18446744073709551615 93885203886080 93885203909153 "
    "140734923420992 0 0 0 0 0 0 0 0 0 17 20 0 0 0 0 0 93885203921488 "
    "93885203923064 93886076616704 140734923430221 140734923430241 "
    "140734923430241 140734923436011 0\n"
)

_START = "77143177"


@pytest.fixture(autouse=True)
def daemon(
    monkeypatch: pytest.MonkeyPatch, mock_native_daemon: FakeNativeDaemonClient
) -> FakeNativeDaemonClient:
    """Install the fake daemon at the doctor module's binding."""
    monkeypatch.setattr(
        "cw.doctor.worker_tmpdir.get_native_daemon_client",
        lambda: mock_native_daemon,
    )
    return mock_native_daemon


def _stat_line(pid: int, comm: str, starttime: str) -> str:
    return _CAPTURED_STAT.format(pid=pid, comm=comm, starttime=starttime)


def _seed_proc(
    proc_root: Path, pid: int, *, environ: bytes | None, stat: str | None
) -> None:
    """Write ``<proc_root>/<pid>/environ`` and ``/stat`` (None = absent)."""
    pid_dir = proc_root / str(pid)
    pid_dir.mkdir(parents=True, exist_ok=True)
    if environ is not None:
        (pid_dir / "environ").write_bytes(environ)
    if stat is not None:
        (pid_dir / "stat").write_text(stat, encoding="utf-8")


def _make_worker_dir(
    tmp_path: Path,
    name: str,
    *,
    settings_env: dict[str, object] | None = None,
    cw_managed: bool = True,
) -> Path:
    """A worker cwd, cw-managed via ``cw-context.json`` unless told otherwise."""
    cwd = tmp_path / name
    cwd.mkdir(parents=True)
    context = cwd / HOOK_CONTEXT_RELATIVE_PATH
    context.parent.mkdir(parents=True, exist_ok=True)
    if cw_managed:
        context.write_text("{}", encoding="utf-8")
    if settings_env is not None:
        (context.parent / "settings.local.json").write_text(
            json.dumps({"env": settings_env}), encoding="utf-8"
        )
    return cwd


def _environ(**entries: str) -> bytes:
    return b"".join(f"{k}={v}".encode() + b"\0" for k, v in entries.items())


def _seed_proc_worker(
    daemon: FakeNativeDaemonClient,
    proc_root: Path,
    cwd: Path,
    pid: int,
    environ: bytes,
) -> str:
    _seed_proc(proc_root, pid, environ=environ, stat=_stat_line(pid, "claude", _START))
    return daemon.seed_live_worker(cwd, pid=pid, proc_start=_START)


def _counts(configured: int, verified: int, unverifiable: int) -> str:
    return (
        f"{configured} configured via settings.local.json (effective value not "
        f"observable via /proc), {verified} verified via /proc, "
        f"{unverifiable} unverifiable"
    )


def _check(proc_root: Path) -> CheckResult:
    result = _check_worker_tmpdir_scope(proc_root=proc_root)
    assert result.name == "worker-tmpdir-scope"
    assert result.ok is True
    return result


class TestNoWorkersAndRoster:
    def test_empty_roster(self, tmp_path: Path) -> None:
        result = _check(tmp_path / "proc")
        assert result.warn is False
        assert result.detail == "no live cw workers"

    def test_only_non_cw_workers(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        daemon.seed_live_worker(_make_worker_dir(tmp_path, "plain", cw_managed=False))
        result = _check(tmp_path / "proc")
        assert result.warn is False
        assert result.detail == "no live cw workers"

    def test_roster_unreadable(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        daemon.roster_unreadable = True
        result = _check(tmp_path / "proc")
        assert result.warn is True
        assert (
            result.detail
            == "could not read daemon roster; worker TMPDIR scope not checked"
        )


class TestSettingsEnv:
    def test_settings_tmpdir_under_cwd_is_configured_not_verified(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        cwd = tmp_path / "wt-a"
        _make_worker_dir(
            tmp_path, "wt-a", settings_env={"TMPDIR": str(cwd / ".cw" / "tmp")}
        )
        daemon.seed_live_worker(cwd, pid=1, proc_start=_START)
        absent_proc = tmp_path / "no-such-proc"

        result = _check(absent_proc)

        assert result.warn is False
        assert result.detail == (
            "1 live cw worker(s), none outside their own worktree TMPDIR scope: "
            + _counts(1, 0, 0)
        )
        assert _COUNTS_LABEL in result.detail
        assert "0 verified via /proc" in result.detail
        assert not absent_proc.exists()

    def test_settings_tmpdir_in_sibling_worktree_is_flagged(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        sibling = str(tmp_path / "wt-b" / ".cw" / "tmp")
        cwd = _make_worker_dir(tmp_path, "wt-a", settings_env={"TMPDIR": sibling})
        short_id = daemon.seed_live_worker(cwd)

        result = _check(tmp_path / "proc")

        assert result.warn is True
        assert f"{short_id} (cwd {cwd}): TMPDIR {sibling}" in result.detail
        assert result.detail.startswith(
            "1 of 1 live cw worker(s) have a TMPDIR outside their own worktree: "
        )

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(b"{not json", id="invalid-json"),
            pytest.param(b'["env"]', id="not-an-object"),
            pytest.param(b'{"env": ["TMPDIR"]}', id="env-not-a-dict"),
            pytest.param(b'{"env": {"TMPDIR": 7}}', id="tmpdir-not-a-str"),
            pytest.param(b"\xff\xfe\x00 not utf-8", id="invalid-utf8"),
        ],
    )
    def test_malformed_settings_fall_through_to_proc(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient, raw: bytes
    ) -> None:
        cwd = _make_worker_dir(tmp_path, "wt-a")
        (cwd / ".claude" / "settings.local.json").write_bytes(raw)
        proc_root = tmp_path / "proc"
        sibling = str(tmp_path / "wt-b" / ".cw" / "tmp")
        _seed_proc_worker(daemon, proc_root, cwd, 101, _environ(TMPDIR=sibling))

        result = _check(proc_root)

        assert result.warn is True
        assert f"TMPDIR {sibling}" in result.detail


class TestProcEnviron:
    def test_sibling_tmpdir_in_environ_is_flagged(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        """The ticket's observed state: every worker carries a sibling's TMPDIR."""
        cwd = _make_worker_dir(tmp_path, "wt-a")
        proc_root = tmp_path / "proc"
        sibling = str(tmp_path / "wt-b" / ".cw" / "tmp")
        short_id = _seed_proc_worker(
            daemon, proc_root, cwd, 101, _environ(PATH="/usr/bin", TMPDIR=sibling)
        )

        result = _check(proc_root)

        assert result.warn is True
        assert f"{short_id} (cwd {cwd}): TMPDIR {sibling}" in result.detail
        assert _counts(0, 0, 0) in result.detail

    def test_own_tmpdir_in_environ_is_verified(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        cwd = _make_worker_dir(tmp_path, "wt-a")
        proc_root = tmp_path / "proc"
        _seed_proc_worker(
            daemon, proc_root, cwd, 101, _environ(TMPDIR=str(cwd / ".cw" / "tmp"))
        )

        result = _check(proc_root)

        assert result.warn is False
        assert _counts(0, 1, 0) in result.detail

    def test_tmpdir_absent_from_environ_is_flagged_as_unset(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        cwd = _make_worker_dir(tmp_path, "wt-a")
        proc_root = tmp_path / "proc"
        short_id = _seed_proc_worker(
            daemon, proc_root, cwd, 101, _environ(PATH="/usr/bin")
        )

        result = _check(proc_root)

        assert result.warn is True
        assert f"{short_id} (cwd {cwd}): TMPDIR unset (host default)" in result.detail

    def test_symlinked_cwd_compares_by_realpath(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        real = _make_worker_dir(tmp_path, "wt-real")
        link = tmp_path / "wt-link"
        link.symlink_to(real, target_is_directory=True)
        proc_root = tmp_path / "proc"
        _seed_proc_worker(
            daemon, proc_root, link, 101, _environ(TMPDIR=str(real / ".cw" / "tmp"))
        )

        result = _check(proc_root)

        assert result.warn is False
        assert _counts(0, 1, 0) in result.detail


class TestUnverifiable:
    def _assert_unverifiable(self, result: CheckResult) -> None:
        assert result.warn is False
        assert result.detail == (
            "1 live cw worker(s), none outside their own worktree TMPDIR scope: "
            + _counts(0, 0, 1)
        )

    def test_environ_missing(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        cwd = _make_worker_dir(tmp_path, "wt-a")
        proc_root = tmp_path / "proc"
        _seed_proc(proc_root, 101, environ=None, stat=_stat_line(101, "claude", _START))
        daemon.seed_live_worker(cwd, pid=101, proc_start=_START)
        self._assert_unverifiable(_check(proc_root))

    def test_environ_unreadable(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        """An ``OSError`` other than not-found (a directory where the file was)."""
        cwd = _make_worker_dir(tmp_path, "wt-a")
        proc_root = tmp_path / "proc"
        _seed_proc(proc_root, 101, environ=None, stat=_stat_line(101, "claude", _START))
        (proc_root / "101" / "environ").mkdir()
        daemon.seed_live_worker(cwd, pid=101, proc_start=_START)
        self._assert_unverifiable(_check(proc_root))

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
    def test_environ_permission_denied(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        cwd = _make_worker_dir(tmp_path, "wt-a")
        proc_root = tmp_path / "proc"
        _seed_proc_worker(daemon, proc_root, cwd, 101, _environ(TMPDIR="/elsewhere"))
        environ = proc_root / "101" / "environ"
        environ.chmod(0)
        try:
            self._assert_unverifiable(_check(proc_root))
        finally:
            environ.chmod(0o600)

    def test_proc_root_absent(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        """The macOS shape: there is no ``/proc`` at all."""
        cwd = _make_worker_dir(tmp_path, "wt-a")
        daemon.seed_live_worker(cwd, pid=101, proc_start=_START)
        self._assert_unverifiable(_check(tmp_path / "no-such-proc"))

    def test_stat_missing(self, tmp_path: Path, daemon: FakeNativeDaemonClient) -> None:
        cwd = _make_worker_dir(tmp_path, "wt-a")
        proc_root = tmp_path / "proc"
        _seed_proc(proc_root, 101, environ=_environ(TMPDIR="/elsewhere"), stat=None)
        daemon.seed_live_worker(cwd, pid=101, proc_start=_START)
        self._assert_unverifiable(_check(proc_root))

    @pytest.mark.parametrize(
        "stat",
        [
            pytest.param(
                _stat_line(101, "evil) R 1 2 3 (x", "99999999"),
                id="pid-reused-comm-with-paren-and-spaces",
            ),
            pytest.param("101 (claude R 1 2 3\n", id="no-closing-paren"),
            pytest.param("101 (claude) R 1 2 3\n", id="too-few-fields"),
        ],
    )
    def test_proc_start_mismatch_or_garbled_stat(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient, stat: str
    ) -> None:
        cwd = _make_worker_dir(tmp_path, "wt-a")
        proc_root = tmp_path / "proc"
        _seed_proc(proc_root, 101, environ=_environ(TMPDIR="/elsewhere"), stat=stat)
        daemon.seed_live_worker(cwd, pid=101, proc_start=_START)
        self._assert_unverifiable(_check(proc_root))

    def test_comm_with_paren_and_spaces_still_matches_after_last_paren(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        """Field 22 is counted after the LAST ``)``, not the first."""
        cwd = _make_worker_dir(tmp_path, "wt-a")
        proc_root = tmp_path / "proc"
        _seed_proc(
            proc_root,
            101,
            environ=_environ(TMPDIR=str(cwd / ".cw" / "tmp")),
            stat=_stat_line(101, "evil) R 1 2 3 (x", _START),
        )
        daemon.seed_live_worker(cwd, pid=101, proc_start=_START)

        result = _check(proc_root)

        assert _counts(0, 1, 0) in result.detail

    @pytest.mark.parametrize(
        ("pid", "proc_start"),
        [pytest.param(None, _START, id="pid-none"), pytest.param(101, None, id="ps")],
    )
    def test_missing_pid_or_proc_start(
        self,
        tmp_path: Path,
        daemon: FakeNativeDaemonClient,
        pid: int | None,
        proc_start: str | None,
    ) -> None:
        cwd = _make_worker_dir(tmp_path, "wt-a")
        proc_root = tmp_path / "proc"
        _seed_proc(
            proc_root,
            101,
            environ=_environ(TMPDIR="/elsewhere"),
            stat=_stat_line(101, "claude", _START),
        )
        daemon.seed_live_worker(cwd, pid=pid, proc_start=proc_start)
        self._assert_unverifiable(_check(proc_root))


class TestRendering:
    def _seed_offenders(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient, count: int
    ) -> list[str]:
        sibling = str(tmp_path / "elsewhere" / ".cw" / "tmp")
        return [
            daemon.seed_live_worker(
                _make_worker_dir(tmp_path, f"wt-{i}", settings_env={"TMPDIR": sibling})
            )
            for i in range(count)
        ]

    def test_seven_offenders_list_five_and_overflow(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        ids = self._seed_offenders(tmp_path, daemon, 7)

        result = _check(tmp_path / "proc")

        assert result.warn is True
        assert result.detail.startswith("7 of 7 live cw worker(s) have a TMPDIR")
        for short_id in sorted(ids)[:5]:
            assert f"{short_id} (cwd " in result.detail
        for short_id in sorted(ids)[5:]:
            assert f"{short_id} (cwd " not in result.detail
        assert "; ... and 2 more. " in result.detail

    def test_exactly_five_offenders_have_no_overflow(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        self._seed_offenders(tmp_path, daemon, 5)

        result = _check(tmp_path / "proc")

        assert result.detail.startswith("5 of 5 live cw worker(s)")
        assert "more" not in result.detail

    def test_offender_detail_carries_counts_and_remediation(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        self._seed_offenders(tmp_path, daemon, 1)

        result = _check(tmp_path / "proc")

        assert result.detail.endswith(
            f". {_counts(0, 0, 0)}. Fix: re-spawn the session through cw so spawn "
            "rewrites <cwd>/.claude/settings.local.json before the worker starts; "
            "a worker only picks up its own <cwd>/.cw/tmp from an env block "
            "written before session start."
        )
        assert "upgrade-workers" not in result.detail

    def test_counts_line_mixes_configured_verified_and_unverifiable(
        self, tmp_path: Path, daemon: FakeNativeDaemonClient
    ) -> None:
        proc_root = tmp_path / "proc"
        configured = tmp_path / "wt-conf"
        _make_worker_dir(
            tmp_path,
            "wt-conf",
            settings_env={"TMPDIR": str(configured / ".cw" / "tmp")},
        )
        daemon.seed_live_worker(configured)
        verified = _make_worker_dir(tmp_path, "wt-ver")
        _seed_proc_worker(
            daemon, proc_root, verified, 101, _environ(TMPDIR=str(verified / "x"))
        )
        daemon.seed_live_worker(_make_worker_dir(tmp_path, "wt-unv"))
        daemon.seed_live_worker(_make_worker_dir(tmp_path, "plain", cw_managed=False))

        result = _check(proc_root)

        assert result.warn is False
        assert result.detail == (
            "3 live cw worker(s), none outside their own worktree TMPDIR scope: "
            "1 configured via settings.local.json (effective value not observable "
            "via /proc), 1 verified via /proc, 1 unverifiable"
        )


def test_environ_contents_never_leak_into_detail_or_logs(
    tmp_path: Path,
    daemon: FakeNativeDaemonClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the TMPDIR value leaves the environ read; nothing is logged."""
    cwd = _make_worker_dir(tmp_path, "wt-a")
    proc_root = tmp_path / "proc"
    sibling = str(tmp_path / "wt-b" / ".cw" / "tmp")
    raw = (
        b"SECRET_TOKEN=hunter2\0"
        + b"BLOB=\xff\xfe\x80garbage\0"
        + f"TMPDIR={sibling}".encode()
        + b"\0"
    )
    _seed_proc_worker(daemon, proc_root, cwd, 101, raw)

    with caplog.at_level(logging.DEBUG):
        result = _check(proc_root)

    assert result.warn is True
    assert sibling in result.detail
    for leaked in ("SECRET_TOKEN", "hunter2", "BLOB", "garbage", "�"):
        assert leaked not in result.detail
    assert caplog.records == []
