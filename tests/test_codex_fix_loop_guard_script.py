"""Tests for :mod:`cw.codex_fix_loop.guard_script` (#3062)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.codex_fix_loop.guard_script import find_guard_script, script_marker_ok

_NAME = "check_must_fix_override.py"


def _script(root: Path, body: str, name: str = _NAME) -> Path:
    path = root / ".claude" / "scripts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestFindGuardScript:
    def test_repo_local_wins_over_home(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        local = _script(worktree, "#!/usr/bin/env python3\n")
        _script(Path.home(), "#!/usr/bin/env python3\n")
        assert find_guard_script(worktree, _NAME) == local

    def test_falls_back_to_home(self, tmp_path: Path) -> None:
        home = _script(Path.home(), "#!/usr/bin/env python3\n")
        assert find_guard_script(tmp_path / "wt", _NAME) == home

    def test_absent_is_none(self, tmp_path: Path) -> None:
        assert find_guard_script(tmp_path / "wt", "no_such_script.py") is None


class TestScriptMarkerOk:
    @pytest.mark.parametrize(
        ("body", "minimum", "expected"),
        [
            ("#!/usr/bin/env python3\n# cw-script-version: 1\n", 1, True),
            ("#!/usr/bin/env python3\n# cw-script-version: 3\n", 2, True),
            ("#!/usr/bin/env python3\n# cw-script-version: 1\n", 2, False),
            ("#!/usr/bin/env python3\nprint('x')\n", 1, False),
            ("#!/usr/bin/env python3\n# cw-script-version: abc\n", 1, False),
            ("#!/usr/bin/env python3\n# cw-script-version: 1234567\n", 1, False),
            ("1\n2\n3\n4\n5\n# cw-script-version: 9\n", 1, False),
            ("x = 1  # cw-script-version: 9\n", 1, False),
        ],
    )
    def test_marker_rule(
        self, tmp_path: Path, body: str, minimum: int, expected: bool
    ) -> None:
        assert script_marker_ok(_script(tmp_path, body), minimum) is expected

    def test_unreadable_is_stale(self, tmp_path: Path) -> None:
        assert script_marker_ok(tmp_path / "missing.py", 1) is False

    def test_undecodable_is_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "bin.py"
        path.write_bytes(b"\xff\xfe\x00")
        assert script_marker_ok(path, 1) is False
