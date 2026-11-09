"""The real-``.cw/`` write guard refuses, records and aborts writes (#3113).

Hermetic: ``tmp_path`` stands in for the repo root. Each test re-arms the
guard on its own fake ``.cw/`` (``arm`` replaces the autouse arm for the rest
of the test) with a private recorder, so a refusal here never reaches the
autouse ``launch_recorder``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path

import pytest

from cw.atomic import atomic_write_text
from tests import _real_cw_guard as guard
from tests._host_probe_guard import LaunchRecorder, finish

_WRITE = os.O_WRONLY | os.O_CREAT


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def cw_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo" / ".cw"
    (root / "tmp").mkdir(parents=True)
    (root / "deferred-findings.md").write_text("real\n", encoding="utf-8")
    return root


@pytest.fixture
def recorder(cw_root: Path) -> LaunchRecorder:
    rec = LaunchRecorder()
    guard.arm(cw_root, (cw_root / "tmp",), rec)
    return rec


class TestTargetPaths:
    @pytest.mark.parametrize(
        "flags",
        [_WRITE, os.O_WRONLY | os.O_TRUNC, os.O_WRONLY | os.O_APPEND, os.O_RDWR],
    )
    def test_a_write_open_names_its_path(self, flags: int) -> None:
        assert guard.target_paths("open", ("p", None, flags)) == ("p",)

    def test_a_read_only_open_is_not_a_write(self) -> None:
        assert guard.target_paths("open", ("p", "r", os.O_RDONLY)) == ()

    def test_a_non_int_flag_is_not_a_write(self) -> None:
        assert guard.target_paths("open", ("p", "r", None)) == ()

    @pytest.mark.parametrize(
        ("event", "args", "expected"),
        [
            ("os.mkdir", ("d", 0o777, -1), ("d",)),
            ("os.rename", ("a", "b", -1, -1), ("a", "b")),
            ("os.remove", ("f", -1), ("f",)),
            ("os.rmdir", ("d", -1), ("d",)),
            ("shutil.rmtree", ("d", None), ("d",)),
            ("os.truncate", ("f", 0), ("f",)),
            ("os.remove", ("f", 3), ()),
            ("os.rename", ("a", "b", 3, -1), ()),
            ("os.listdir", ("d",), ()),
        ],
    )
    def test_the_event_table(
        self, event: str, args: tuple[object, ...], expected: tuple[object, ...]
    ) -> None:
        assert guard.target_paths(event, args) == expected


class TestRefuses:
    def test_a_path_under_the_root_is_refused(self, cw_root: Path) -> None:
        guard.arm(cw_root, (), LaunchRecorder())
        assert guard.refuses(cw_root / "deferred-findings.md")
        assert guard.refuses(cw_root)

    def test_bytes_and_pathlike_paths(
        self, cw_root: Path, recorder: LaunchRecorder
    ) -> None:
        assert guard.normalise(b"/x") == "/x"
        assert guard.normalise(Path("/x")) == "/x"
        assert guard.refuses(os.fsencode(cw_root / "x"))
        assert guard.normalise(object()) is None

    def test_an_int_fd_is_ignored(self, recorder: LaunchRecorder) -> None:
        assert guard.normalise(3) is None
        assert not guard.refuses(3)

    def test_a_relative_path_resolves_against_the_cwd(
        self, cw_root: Path, recorder: LaunchRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(cw_root.parent)
        assert guard.refuses(".cw/deferred-findings.md")
        assert guard.refuses(b".cw/x")

    def test_a_dotdot_traversal_back_into_the_root_is_refused(
        self, cw_root: Path, recorder: LaunchRecorder
    ) -> None:
        assert guard.refuses(cw_root / "tmp" / ".." / "x")

    def test_an_allowed_subtree_and_an_outside_path_pass(
        self, cw_root: Path, tmp_path: Path, recorder: LaunchRecorder
    ) -> None:
        assert not guard.refuses(cw_root / "tmp" / "pytest-of-x" / "f")
        assert not guard.refuses(tmp_path / "elsewhere")
        assert not guard.refuses(str(cw_root) + "-sibling")

    def test_a_vanished_cwd_does_not_judge_a_relative_path(
        self, tmp_path: Path, recorder: LaunchRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gone = tmp_path / "gone"
        gone.mkdir()
        monkeypatch.chdir(gone)
        gone.rmdir()
        try:
            assert guard.normalise("x") is None
        finally:
            monkeypatch.chdir(tmp_path)

    def test_unarmed_refuses_nothing(self, cw_root: Path) -> None:
        guard.disarm()
        assert not guard.is_armed()
        assert not guard.refuses(cw_root / "x")


class TestHook:
    def test_an_armed_refusal_raises_and_records_once(
        self, cw_root: Path, recorder: LaunchRecorder
    ) -> None:
        with pytest.raises(guard.RealCwWriteError) as raised:
            (cw_root / "new.md").write_text("leak\n", encoding="utf-8")
        assert isinstance(raised.value, PermissionError)
        (violation,) = recorder.violations()
        assert violation.kind == "cw_write"
        assert str(cw_root / "new.md") in violation.detail
        assert not (cw_root / "new.md").exists()

    def test_a_refused_write_leaves_the_tree_byte_identical(
        self, cw_root: Path, recorder: LaunchRecorder
    ) -> None:
        before = _tree_hash(cw_root)
        with pytest.raises(guard.RealCwWriteError):
            atomic_write_text(cw_root / "deferred-findings.md", "fixture\n")
        with pytest.raises(guard.RealCwWriteError):
            (cw_root / "deferred-findings.md").write_text("x", encoding="utf-8")
        assert _tree_hash(cw_root) == before

    def test_reads_and_allowed_writes_pass(
        self, cw_root: Path, recorder: LaunchRecorder
    ) -> None:
        assert (cw_root / "deferred-findings.md").read_text(encoding="utf-8")
        (cw_root / "tmp" / "ok").write_text("ok", encoding="utf-8")
        assert recorder.violations() == []

    def test_a_swallowed_refusal_still_fails_finish(
        self,
        cw_root: Path,
        recorder: LaunchRecorder,
        request: pytest.FixtureRequest,
    ) -> None:
        with contextlib.suppress(OSError):
            (cw_root / "x").write_text("leak", encoding="utf-8")
        guard.disarm()
        with pytest.raises(AssertionError, match="cw_write: open "):
            finish(recorder, request.node)

    def test_creating_a_missing_cw_dir_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "fresh" / ".cw"
        root.parent.mkdir()
        rec = LaunchRecorder()
        guard.arm(root, (), rec)
        with pytest.raises(guard.RealCwWriteError):
            root.mkdir()
        assert not root.exists()
        assert [v.kind for v in rec.violations()] == ["cw_write"]

    def test_an_unarmed_hook_is_a_no_op(self, cw_root: Path) -> None:
        guard.disarm()
        (cw_root / "free.md").write_text("ok", encoding="utf-8")
        assert (cw_root / "free.md").exists()


def test_install_registers_the_hook_once(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[object] = []
    monkeypatch.setattr(guard._STATE, "installed", False)
    monkeypatch.setattr("sys.addaudithook", registered.append)
    guard.install_hook()
    guard.install_hook()
    assert registered == [guard._hook]
