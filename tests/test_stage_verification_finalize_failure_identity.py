"""Failure identity for the FINALIZE base-branch-state classifier (#3542).

Every fixture is a real capture (``tests/fixtures/gate_output/`` from the locked
pytest and ruff on a throwaway project; the mypy ones are the existing real
captures). Variations are built by editing a captured line, never by writing
tool output from scratch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.stage_verification import finalize_failure_identity as fid

_FIXTURES = Path(__file__).parent / "fixtures"
_GATE = _FIXTURES / "gate_output"


def _read(name: str) -> str:
    return (_GATE / name).read_text(encoding="utf-8")


_PYTEST = _read("pytest_failures.txt")
_COLLECT = _read("pytest_collection_error.txt")
_COVERAGE = _read("pytest_coverage_fail.txt")
_COVERAGE_MET = _read("pytest_coverage_met.txt")
_STOP = _read("pytest_stop_first.txt")
_RUFF = _read("ruff_check.txt")
_RUFF_CONCISE = _read("ruff_check_concise.txt")
_RUFF_FORMAT = _read("ruff_format.txt")
_MYPY = (_FIXTURES / "mypy_error_output_real.txt").read_text(encoding="utf-8")
_MYPY_COLUMNS = (_FIXTURES / "mypy_error_output_columns_real.txt").read_text(
    encoding="utf-8"
)
_ROOT_A = "/redacted/repo"


def _match(gate: str, branch: str, base: str) -> bool:
    mine = fid.failure_identity(gate, branch, roots=(_ROOT_A,))
    theirs = fid.failure_identity(gate, base, roots=("/elsewhere/scratch",))
    assert mine is not None
    assert theirs is not None
    return fid.identity_matches(mine, theirs)


# -- recognised gates --------------------------------------------------------


@pytest.mark.parametrize(
    ("gate", "text"),
    [
        ("pytest", _PYTEST),
        ("ruff", _RUFF),
        ("ruff-check", _RUFF_CONCISE),
        ("ruff-format", _RUFF_FORMAT),
        ("mypy", _MYPY),
        ("mypy-strict", _MYPY_COLUMNS),
    ],
)
def test_the_same_failures_match(gate: str, text: str) -> None:
    assert _match(gate, text, text)


def test_pytest_identity_is_the_summary_node_ids_and_messages() -> None:
    ident = fid.failure_identity("pytest", _PYTEST, roots=())
    assert ident is not None
    assert sorted(ident.items) == [
        (
            "ERROR",
            "tests/test_fixture_err.py::test_uses_boom",
            "RuntimeError: fixture setup...",
        ),
        ("FAILED", "tests/test_calc.py::TestGroup::test_inside", "assert 0 == 1"),
        (
            "FAILED",
            "tests/test_calc.py::test_add_type",
            "TypeError: can only concatenate st...",
        ),
        ("FAILED", "tests/test_calc.py::test_add_wrong", "assert 4 == 5"),
    ]


def test_the_same_node_failing_differently_on_base_does_not_match() -> None:
    # test_value: ``assert 1 == 2`` on the branch, ``KeyError`` on the base.
    assert not _match(
        "pytest",
        _read("pytest_message_branch.txt"),
        _read("pytest_message_other_exception.txt"),
    )


def test_the_same_failure_from_two_roots_matches() -> None:
    mine = fid.failure_identity(
        "pytest", _read("pytest_message_root_a.txt"), roots=("/redacted/repo",)
    )
    theirs = fid.failure_identity(
        "pytest",
        _read("pytest_message_root_b.txt"),
        roots=("/redacted/scratch/tree",),
    )
    assert mine is not None
    assert theirs is not None
    assert any("<root>/data/missing.txt" in item[2] for item in mine.items)
    assert fid.identity_matches(mine, theirs)


@pytest.mark.parametrize(
    "capture", ["pytest_message_type_cut.txt", "pytest_message_absent.txt"]
)
def test_a_summary_line_without_a_whole_exception_type_has_no_identity(
    capture: str,
) -> None:
    assert fid.failure_identity("pytest", _read(capture), roots=()) is None


def test_a_long_node_id_has_an_identity_only_on_the_wide_terminal() -> None:
    # The same two tests, captured at 80 and at the gate runs' 200 columns.
    assert (
        fid.failure_identity("pytest", _read("pytest_long_node_80.txt"), roots=())
        is None
    )
    wide = fid.failure_identity("pytest", _read("pytest_long_node_200.txt"), roots=())
    assert wide is not None
    assert [item[2] for item in wide.items] == ["assert 1 == 2", "KeyError: 'missing'"]


def test_a_met_coverage_threshold_keeps_the_identity() -> None:
    assert "Required test coverage of 50% reached" in _COVERAGE_MET
    ident = fid.failure_identity("pytest", _COVERAGE_MET, roots=())
    assert ident is not None
    assert ident.items == (
        ("FAILED", "tests/test_calc.py::test_add_wrong", "assert 4 == 5"),
    )


@pytest.mark.parametrize("marker", ["FAIL Required test coverage", "Coverage failure"])
def test_each_coverage_failure_line_alone_gives_no_identity(marker: str) -> None:
    lines = [line for line in _COVERAGE.splitlines() if marker in line]
    assert len(lines) == 1
    text = f"{_COVERAGE_MET}{lines[0]}\n"
    assert fid.failure_identity("pytest", text, roots=()) is None


def test_a_base_superset_matches() -> None:
    branch = _PYTEST.replace(
        "FAILED tests/test_calc.py::test_add_type - TypeError: can only"
        " concatenate st...\n",
        "",
    ).replace("3 failed, 1 passed, 1 error", "2 failed, 2 passed, 1 error")
    assert _match("pytest", branch, _PYTEST)


def test_an_extra_branch_failure_does_not_match() -> None:
    base = _PYTEST.replace(
        "FAILED tests/test_calc.py::test_add_type - TypeError: can only"
        " concatenate st...\n",
        "",
    ).replace("3 failed, 1 passed, 1 error", "2 failed, 2 passed, 1 error")
    assert not _match("pytest", _PYTEST, base)


def test_an_extra_failure_beyond_the_posted_tail_does_not_match() -> None:
    # The posted output is the last 20 lines: put the differing line before them.
    padding = "".join(f"noise line {n}\n" for n in range(40))
    branch = _RUFF_CONCISE.replace(
        "Found 3 errors.",
        "pkg/other.py:4:1: F401 [*] `re` imported but unused\nFound 4 errors.",
    )
    branch_text = branch.replace("Found 4 errors.", padding + "Found 4 errors.")
    base_text = _RUFF_CONCISE.replace("Found 3 errors.", padding + "Found 3 errors.")
    assert not _match("ruff", branch_text, base_text)


def test_ruff_line_shifts_still_match() -> None:
    shifted = _RUFF.replace("pkg/calc.py:10:5", "pkg/calc.py:14:5")
    assert _match("ruff", _RUFF, shifted)
    assert _match("ruff", _RUFF_CONCISE, _RUFF_CONCISE.replace(":10:5:", ":12:5:"))


def test_mypy_line_shifts_still_match() -> None:
    shifted = _MYPY.replace("main.py:6: error", "main.py:9: error")
    assert _match("mypy", _MYPY, shifted)
    assert _match("mypy", _MYPY_COLUMNS, _MYPY)


def test_a_second_instance_of_the_same_violation_does_not_match() -> None:
    extra = "pkg/calc.py:2:8: F401 [*] `sys` imported but unused\n"
    branch = _RUFF_CONCISE.replace("Found 3 errors.", extra + "Found 4 errors.")
    assert not _match("ruff", branch, _RUFF_CONCISE)
    line = 'main.py:6: error: "int" has no attribute "missing"  [attr-defined]\n'
    mypy_branch = _MYPY.replace(
        "Found 4 errors in 2 files", "Found 5 errors in 2 files"
    ).replace(line, line + line.replace(":6:", ":8:"))
    assert not _match("mypy", mypy_branch, _MYPY)


@pytest.mark.parametrize(
    ("gate", "text"),
    [
        ("pytest", _PYTEST.replace("3 failed", "4 failed")),
        ("pytest", _COVERAGE),
        ("pytest", _COLLECT),
        ("pytest", _STOP),
        ("pytest", ""),
        ("ruff", _RUFF.replace("Found 3 errors.", "Found 2 errors.")),
        ("ruff-format", _RUFF_FORMAT.replace("1 file would", "2 files would")),
        ("ruff", "error: Failed to parse pyproject.toml\n"),
        ("mypy", _MYPY.replace("Found 4 errors", "Found 5 errors")),
        ("mypy", _MYPY + "pkg/x.py: error: errors prevented further checking\n"),
        ("mypy", ""),
        ("python-2", ""),
        ("python-2", "\n  \n"),
    ],
)
def test_no_identity(gate: str, text: str) -> None:
    assert fid.failure_identity(gate, text, roots=()) is None


def test_a_non_python_mypy_diagnostic_mismatches_the_count() -> None:
    text = _MYPY.replace(
        "Found 4 errors in 2 files", "Found 5 errors in 2 files"
    ).replace("main.py:7: note", "setup.cfg:1: error: bad option\nmain.py:7: note")
    assert fid.failure_identity("mypy", text, roots=()) is None


# -- unrecognised gates ------------------------------------------------------


def test_an_unrecognised_gate_matches_only_equal_output() -> None:
    assert _match("python-2", "changelog-frozen-bad\n", "changelog-frozen-bad\n")
    assert not _match("python-2", "changelog-frozen-bad\n", "something-else\n")


def test_paths_ansi_and_scratch_dirs_are_normalised() -> None:
    branch = "\x1b[31mfail\x1b[0m at /redacted/repo/x.py tmp=/tmp/cw-gate-ab12_x\n"
    base = "fail at /elsewhere/scratch/x.py tmp=/var/tmp/cw-gate-zz99\n"
    assert _match("python-2", branch, base)


_UV_LINE = _MYPY.splitlines()[4]


def test_the_duration_rule_on_a_real_uv_line() -> None:
    assert _UV_LINE == "Installed 53 packages in 3.10s"
    assert _match("python-2", _UV_LINE, _UV_LINE.replace("3.10s", "12ms"))
    assert _match("python-2", _UV_LINE, _UV_LINE.replace("3.10s", "1.2s"))
    assert _match("python-2", _UV_LINE, _UV_LINE.replace("3.10s", "1m 05s"))
    assert not _match("python-2", _UV_LINE, _UV_LINE.replace("53", "54"))


def test_durations_do_not_relax_a_recognised_gate() -> None:
    # Recognised gates compare parsed failures; the duration token is unused.
    ident = fid.failure_identity("pytest", _PYTEST, roots=())
    assert ident is not None
    assert all("<duration>" not in part for item in ident.items for part in item)


def test_recognised_and_unrecognised_identities_never_match() -> None:
    mine = fid.failure_identity("python-2", "x\n", roots=())
    theirs = fid.failure_identity("pytest", _PYTEST, roots=())
    assert mine is not None
    assert theirs is not None
    assert not fid.identity_matches(mine, theirs)


@pytest.mark.parametrize(
    ("name", "recognised"),
    [
        ("pytest", True),
        ("pytest-unit", True),
        ("ruff-format", True),
        ("mypy", True),
        ("python-2", False),
        ("uv-lock", False),
        ("pre-commit", False),
    ],
)
def test_is_recognised_gate(name: str, recognised: bool) -> None:
    assert fid.is_recognised_gate(name) is recognised
