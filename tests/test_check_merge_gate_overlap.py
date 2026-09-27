"""Tests for .claude/scripts/check_merge_gate_overlap.py (#2431).

The script is the pure set-logic half of Step 4a's merge gate: it intersects
two pre-computed file lists, drops ``--ignore-path`` entries, and reports
whether the surviving overlap must escalate to a ``git merge-tree`` probe. It
never shells out to git and never reads client config, so every case here is
a deterministic file-list fixture under ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import GUARD_MARKER_CURRENT, load_guard_script_module

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "check_merge_gate_overlap.py"

_mod = load_guard_script_module(_SCRIPT, "check_merge_gate_overlap")


def _write_list(tmp_path: Path, name: str, paths: list[str]) -> Path:
    listing = tmp_path / name
    listing.write_text("".join(f"{p}\n" for p in paths), encoding="utf-8")
    return listing


def _run(
    tmp_path: Path,
    branch: list[str],
    pr: list[str],
    ignore: list[str] | None = None,
    *,
    as_json: bool = True,
) -> list[str]:
    argv = [
        "filter",
        "--branch-files",
        str(_write_list(tmp_path, "branch.txt", branch)),
        "--pr-files",
        str(_write_list(tmp_path, "pr.txt", pr)),
    ]
    for path in ignore or []:
        argv += ["--ignore-path", path]
    if as_json:
        argv.append("--json")
    return argv


def test_script_carries_current_version_marker() -> None:
    header = "\n".join(_SCRIPT.read_text(encoding="utf-8").splitlines()[:5])
    assert GUARD_MARKER_CURRENT.strip() in header


def test_disjoint_lists_are_non_blocking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _mod.main(_run(tmp_path, ["src/a.py"], ["src/b.py"]))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload == {
        "blocking": False,
        "overlap": [],
        "ignored": [],
        "overlap_after_ignore": [],
    }


def test_overlap_without_ignore_paths_is_blocking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _mod.main(_run(tmp_path, ["pyproject.toml", "src/a.py"], ["pyproject.toml"]))
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["blocking"] is True
    assert payload["overlap"] == ["pyproject.toml"]
    assert payload["ignored"] == []
    assert payload["overlap_after_ignore"] == ["pyproject.toml"]


def test_overlap_fully_covered_by_ignore_paths_is_non_blocking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _mod.main(
        _run(
            tmp_path,
            ["mypy-baseline.txt", "uv.lock", "src/a.py"],
            ["mypy-baseline.txt", "uv.lock", "src/b.py"],
            ["mypy-baseline.txt", "uv.lock"],
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["blocking"] is False
    assert payload["overlap"] == ["mypy-baseline.txt", "uv.lock"]
    assert payload["ignored"] == ["mypy-baseline.txt", "uv.lock"]
    assert payload["overlap_after_ignore"] == []


def test_overlap_partially_covered_keeps_only_the_remainder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _mod.main(
        _run(
            tmp_path,
            ["mypy-baseline.txt", "pyproject.toml"],
            ["mypy-baseline.txt", "pyproject.toml"],
            ["mypy-baseline.txt"],
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["blocking"] is True
    assert payload["ignored"] == ["mypy-baseline.txt"]
    assert payload["overlap_after_ignore"] == ["pyproject.toml"]


def test_ignore_paths_are_exact_match_not_glob_or_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory or glob ignore entry excuses nothing (exact string match)."""
    code = _mod.main(
        _run(
            tmp_path,
            ["generated/out.py"],
            ["generated/out.py"],
            ["generated", "generated/*", "out.py"],
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["overlap_after_ignore"] == ["generated/out.py"]


def test_omitted_ignore_flag_equals_empty_ignore_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _run(tmp_path, ["a.py"], ["a.py"])
    assert "--ignore-path" not in argv
    code = _mod.main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ignored"] == []


def test_blank_lines_and_whitespace_are_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _mod.main(_run(tmp_path, ["", "  a.py  ", ""], ["a.py", ""]))
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["overlap"] == ["a.py"]


@pytest.mark.parametrize("which", ["branch", "pr"])
def test_missing_input_file_is_usage_error(
    tmp_path: Path, which: str, capsys: pytest.CaptureFixture[str]
) -> None:
    present = str(_write_list(tmp_path, "present.txt", ["a.py"]))
    missing = str(tmp_path / "does-not-exist.txt")
    branch, pr = (missing, present) if which == "branch" else (present, missing)
    code = _mod.main(["filter", "--branch-files", branch, "--pr-files", pr, "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "check_merge_gate_overlap:" in captured.err


@pytest.mark.parametrize("which", ["branch", "pr"])
def test_empty_input_list_is_usage_error(
    tmp_path: Path, which: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty list means the caller's collection step failed — fail closed.

    Pre-#2431, a failed ``gh pr diff`` produced an empty list, an empty
    intersection, and a silent pass. The caller treats exit 2 as a tooling
    failure and blocks.
    """
    branch = ["a.py"] if which == "pr" else []
    pr = ["a.py"] if which == "branch" else []
    code = _mod.main(_run(tmp_path, branch, pr))
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "named no files" in captured.err


def test_missing_required_flag_is_usage_error(tmp_path: Path) -> None:
    present = str(_write_list(tmp_path, "present.txt", ["a.py"]))
    with pytest.raises(SystemExit) as excinfo:
        _mod.main(["filter", "--branch-files", present])
    assert excinfo.value.code == 2


def test_summary_line_without_json_blocking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _mod.main(_run(tmp_path, ["a.py", "b.py"], ["a.py"], as_json=False))
    out = capsys.readouterr().out
    assert code == 1
    assert out.startswith("check_merge_gate_overlap: blocking")
    assert "a.py" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_summary_line_without_json_non_blocking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _mod.main(
        _run(tmp_path, ["uv.lock"], ["uv.lock"], ["uv.lock"], as_json=False)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("check_merge_gate_overlap: non-blocking")
    assert "uv.lock" in out


def test_script_runs_as_standalone_executable(tmp_path: Path) -> None:
    """The fence invokes the file directly, not the imported module."""
    argv = _run(tmp_path, ["a.py"], ["a.py"], ["a.py"])
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), *argv],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ignored"] == ["a.py"]
