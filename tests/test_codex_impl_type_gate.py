"""Tests for cw.codex_impl._type_gate: codex IMPL gate 4, end to end (#3531).

Every run drives ``run_codex_impl_stage`` through the run tests' harness
against a ``ScriptedCodexRunner``. ``uv`` is a recording shim from
``tests/_tool_fakes.py`` on ``PATH``: the repo gates (``uv run mypy src/``) and
gate 4's own commands (``uv run mypy --no-pretty ...``, ``uv run ruff check
--force-exclude ...``) are told apart by argv prefix, or by working directory
(``stub_uv_by_cwd``). Mypy output is the real capture under ``tests/fixtures``
with its paths substituted. ``CW_GATE_ROOT`` is a ``tmp_path`` directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.codex_fix_loop.gates import GateFailure, run_cycle_gates
from cw.review_adjudication import (
    DEFERRED_FINDINGS_RELATIVE_PATH,
    read_deferred_findings_md,
)
from cw.stage_verification.impl_type_gate import (
    BASELINE_FRICTION_PREFIX,
    BASELINE_UNCOVERED_PREFIX,
    CONSUMER_UNKNOWN_PREFIX,
    DEFERRED_UNPARSEABLE_FRICTION,
    GATE4_PREFIX,
    LAUNCHER_UNSUPPORTED_PREFIX,
    MYPY_NOT_DECLARED_FRICTION,
    NO_BASELINE_RULE,
    RECORD_FAILED_FRICTION,
    RUFF_NOT_DECLARED_FRICTION,
    SKIPPED_NO_GATE_FRICTION,
    UNTOUCHED_ONLY_PREFIX,
)
from tests._codex_review_helpers import assert_clean_after_refusal
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._tool_fakes import (
    GhReply,
    fake_uv_cli,
    prepend_to_path,
    read_calls,
    stub_uv_by_cwd,
)
from tests.test_codex_impl_run import (
    BRANCH,
    PLAN,
    WIDGET,
    Harness,
    _assert_block,
    harness,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.auto_dev_result import AutoDevResult

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")
__all__ = ["harness"]

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_CAPTURE = (_FIXTURES / "mypy_error_output_real.txt").read_text(encoding="utf-8")
_USAGE = (_FIXTURES / "mypy_usage_error_real.txt").read_text(encoding="utf-8")
_BASELINE_TXT = (_FIXTURES / "mypy_baseline_real.txt").read_text(encoding="utf-8")
OTHER = "src/cw/other.py"
PRIOR = "src/cw/prior.py"
BASELINE_FILE = "mypy-baseline.txt"
CONFIG = ".claude/project-config.yaml"
MYPY_GATE = ("mypy", "uv run mypy src/", 5.0)
RUFF_GATE = ("ruff", "uv run ruff check src/", 5.0)
BASELINE_CMD = f"uv run mypy src/ --baseline-file {BASELINE_FILE}"
BASELINE_GATE = ("mypy", BASELINE_CMD, 5.0)
GATE4_MYPY = "run mypy --no-pretty"
GATE4_RUFF = "run ruff check --force-exclude"
_GATE4_MYPY_ARGV = [
    "run",
    "mypy",
    "--no-pretty",
    "--no-color-output",
    "--hide-absolute-path",
]


def _mypy_output(touched: str = WIDGET, other: str = OTHER) -> str:
    """The real capture with *touched* for ``main.py``, *other* for the helper."""
    return _CAPTURE.replace("pkg/helper.py", other).replace("main.py", touched)


def _baseline_listing(path: str) -> str:
    """The real ``mypy-baseline`` capture with *path* for ``pkg/old.py``."""
    return _BASELINE_TXT.replace("pkg/old.py", path)


def _untouched_output() -> str:
    """The real capture with every diagnostic in an untouched file."""
    return _mypy_output(touched="src/cw/elsewhere.py")


@pytest.fixture
def gate_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "gate-root"
    root.mkdir()
    monkeypatch.setenv("CW_GATE_ROOT", str(root))
    return root


def _uv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, GhReply] | None = None,
) -> Any:
    shim = fake_uv_cli(tmp_path, responses=responses)
    prepend_to_path(monkeypatch, shim.bin_dir)
    return shim


def _uv_by_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    in_gate: GhReply,
    *,
    touch: str | None = None,
) -> Path:
    bin_dir = stub_uv_by_cwd(
        tmp_path, in_gate_worktree=in_gate, touch_in_gate_worktree=touch
    )
    prepend_to_path(monkeypatch, bin_dir)
    return bin_dir


def _seed_main(h: Harness, relpath: str, content: str) -> None:
    """Commit *relpath* and make it ``main``'s and the branch's tip on origin."""
    commit_tracked_file(h.worktree, relpath, content)
    git_in(h.worktree, "push", "origin", "HEAD:main", f"HEAD:{BRANCH}")
    git_in(h.worktree, "fetch", "origin")


def _plan_with(*extra: str) -> str:
    listing = "".join(f"- {path}\n" for path in extra)
    return PLAN.replace(f"- {WIDGET}\n", f"- {WIDGET}\n{listing}", 1)


def _details(result: AutoDevResult) -> str:
    assert result.blocker is not None
    return result.blocker.details


def _assert_gate4_park(result: AutoDevResult, h: Harness, start: str) -> str:
    _assert_block(result, "impl_failed")
    details = _details(result)
    assert details.startswith(GATE4_PREFIX)
    assert result.blocker is not None
    assert result.blocker.retry_eligible is False
    assert_clean_after_refusal(h.worktree, start)
    assert h.commit_count() == 0
    return details


def _assert_no_gate_dirs(root: Path, h: Harness) -> None:
    assert list(root.iterdir()) == []
    assert len(git_in(h.worktree, "worktree", "list").splitlines()) == 1


def _gate4_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "--no-pretty" in c or "--force-exclude" in c]


# -- parks ---------------------------------------------------------------------


def test_a_gate4_mypy_failure_parks_impl_failed_with_the_documented_reason(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,))
    uv = _uv(tmp_path, monkeypatch, {GATE4_MYPY: GhReply(1, _mypy_output())})
    h.turn()
    start, tip = h.head(), h.origin_tip()

    result = h.run()

    details = _assert_gate4_park(result, h, start)
    assert f'"{NO_BASELINE_RULE}"' in details
    assert f"{WIDGET}:5: error: Incompatible types in assignment" in details
    assert f"{WIDGET}:6: error:" in details
    assert "[attr-defined]" in details
    assert f"- {WIDGET}" in details
    assert f"{OTHER}:2" not in details
    assert "exited 1" in details
    assert h.origin_tip() == tip
    _assert_no_gate_dirs(gate_root, h)
    assert uv.calls == [["run", "mypy", "src/"], [*_GATE4_MYPY_ARGV, WIDGET]]


def test_a_gate4_ruff_failure_parks(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(RUFF_GATE, MYPY_GATE))
    ruff_out = f"{WIDGET}:1:1: F401 `os` imported but unused\nFound 1 error.\n"
    uv = _uv(tmp_path, monkeypatch, {GATE4_RUFF: GhReply(1, ruff_out)})
    h.turn()
    start = h.head()

    details = _assert_gate4_park(h.run(), h, start)

    assert "`uv run ruff check --force-exclude src/cw/widget.py` exited 1" in details
    assert "F401 `os` imported but unused" in details
    assert f"- {WIDGET}" in details
    # Fail fast: mypy never ran after the ruff park.
    assert [c for c in uv.calls if "--no-pretty" in c] == []
    _assert_no_gate_dirs(gate_root, h)


def test_a_missing_tool_parks_rather_than_skips(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,))
    missing = GhReply(127, stderr="error: Failed to spawn: `mypy`\n")
    _uv(tmp_path, monkeypatch, {GATE4_MYPY: missing})
    h.turn()
    start = h.head()

    details = _assert_gate4_park(h.run(), h, start)

    assert "exited 127" in details
    assert "Failed to spawn" in details
    _assert_no_gate_dirs(gate_root, h)


@pytest.mark.parametrize(
    ("gates", "seed", "label", "start_of_details"),
    [
        (
            (MYPY_GATE,),
            None,
            "impl-gate4-mypy",
            f"`uv run mypy {' '.join(_GATE4_MYPY_ARGV[2:])} {WIDGET}` could not be run",
        ),
        (
            (RUFF_GATE,),
            None,
            "impl-gate4-ruff",
            f"`uv run ruff check --force-exclude {WIDGET}` could not be run",
        ),
        (
            (MYPY_GATE,),
            (CONFIG, "tests:\n  prepare: make deps\n"),
            "impl-gate4-prepare",
            "tests.prepare could not be run",
        ),
        (
            (BASELINE_GATE,),
            (BASELINE_FILE, "old entry\n"),
            "impl-gate4-mypy-baseline-0",
            f"`{BASELINE_CMD}` could not be run",
        ),
    ],
    ids=["mypy", "ruff", "prepare", "baseline"],
)
def test_an_unavailable_command_parks(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
    gates: tuple[tuple[str, str, float], ...],
    seed: tuple[str, str] | None,
    label: str,
    start_of_details: str,
) -> None:
    h = harness(gates=gates)
    if seed is not None:
        _seed_main(h, *seed)
    _uv(tmp_path, monkeypatch)
    h.turn()
    start = h.head()
    labels: list[str] = []

    def _unavailable(plan: Any, **_kwargs: Any) -> GateFailure:
        labels.append(plan.gates[0].name)
        return GateFailure(
            kind="unavailable",
            gate=plan.gates[0].name,
            command="x",
            exit_code=None,
            seconds=0.0,
            output="",
            why="no usable scratch root",
        )

    with patch("cw.codex_impl._type_gate.run_cycle_gates", _unavailable):
        details = _assert_gate4_park(h.run(), h, start)

    assert labels == [label]
    expected = f"{GATE4_PREFIX}{start_of_details}: no usable scratch root"
    assert details.startswith(expected)
    _assert_no_gate_dirs(gate_root, h)


def test_a_git_failure_while_measuring_parks_gate_setup(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,))
    uv = _uv(tmp_path, monkeypatch)
    h.turn()
    start = h.head()
    failure = subprocess.CalledProcessError(
        128, ["git", "merge-base"], stderr="fatal: no merge base"
    )

    with patch("cw.codex_impl._type_gate.gate_fork_point", side_effect=failure):
        details = _assert_gate4_park(h.run(), h, start)

    assert details == f"{GATE4_PREFIX}gate setup: fatal: no merge base"
    assert _gate4_calls(uv.calls) == []
    _assert_no_gate_dirs(gate_root, h)


def test_an_unusable_gate_root_parks_gate_setup(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_a_dir = tmp_path / "gate-root-file"
    not_a_dir.write_text("x", encoding="utf-8")
    monkeypatch.setenv("CW_GATE_ROOT", str(not_a_dir))
    h = harness(gates=(MYPY_GATE,))
    uv = _uv(tmp_path, monkeypatch)
    h.turn()
    start = h.head()

    details = _assert_gate4_park(h.run(), h, start)

    assert details.startswith(f"{GATE4_PREFIX}gate setup: ")
    assert _gate4_calls(uv.calls) == []


def test_mypy_exit_2_with_only_untouched_errors_parks(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,))
    _uv(tmp_path, monkeypatch, {GATE4_MYPY: GhReply(2, _untouched_output())})
    h.turn()
    start = h.head()

    details = _assert_gate4_park(h.run(), h, start)

    assert "exited 2" in details
    assert NO_BASELINE_RULE in details
    _assert_no_gate_dirs(gate_root, h)


def test_a_touched_error_buried_above_the_gate_output_tail_still_parks(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,))
    capture = _mypy_output().splitlines()
    touched = next(line for line in capture if line.startswith(f"{WIDGET}:5:"))
    untouched = [line for line in capture if line.startswith(f"{OTHER}:")]
    buried = "\n".join([touched, *(untouched * 15)]) + "\n"
    _uv(tmp_path, monkeypatch, {GATE4_MYPY: GhReply(1, buried)})
    h.turn()
    start = h.head()

    details = _assert_gate4_park(h.run(), h, start)

    assert touched in details
    _assert_no_gate_dirs(gate_root, h)


def test_a_mypy_failure_with_no_parseable_error_parks(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,))
    _uv(tmp_path, monkeypatch, {GATE4_MYPY: GhReply(2, _USAGE)})
    h.turn()
    start = h.head()

    details = _assert_gate4_park(h.run(), h, start)

    assert "unrecognized arguments" in details
    assert NO_BASELINE_RULE in details
    _assert_no_gate_dirs(gate_root, h)


# -- passes ----------------------------------------------------------------------


def test_a_green_gate4_runs_in_a_detached_gate_worktree_and_ships(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(RUFF_GATE, MYPY_GATE))
    bin_dir = _uv_by_cwd(tmp_path, monkeypatch, GhReply(0))
    h.turn()
    start = h.head()
    seen: list[tuple[str, Path, str, str]] = []

    def _spy(plan: Any, **kwargs: Any) -> GateFailure | None:
        wt = kwargs["worktree"]
        branch = git_in(wt, "branch", "--show-current")
        seen.append((plan.gates[0].name, wt, branch, git_in(wt, "rev-parse", "HEAD^")))
        return run_cycle_gates(plan, **kwargs)

    with patch("cw.codex_impl._type_gate.run_cycle_gates", _spy):
        result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert [name for name, *_ in seen] == ["impl-gate4-ruff", "impl-gate4-mypy"]
    for _name, wt, branch, parent in seen:
        assert wt.parent == gate_root
        assert wt.name.startswith("cw-gate-wt-impl-")
        assert branch == ""
        assert parent == start
    calls = read_calls(bin_dir, "uv.calls")
    assert ["run", "ruff", "check", "--force-exclude", WIDGET] in calls
    assert [*_GATE4_MYPY_ARGV, WIDGET] in calls
    assert RUFF_NOT_DECLARED_FRICTION not in result.friction_highlights
    _assert_no_gate_dirs(gate_root, h)


def test_a_command_that_dirties_the_gate_worktree_still_passes(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,))
    _uv_by_cwd(tmp_path, monkeypatch, GhReply(0), touch="stray.txt")
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert not (h.worktree / "stray.txt").exists()
    _assert_no_gate_dirs(gate_root, h)


def test_mypy_errors_only_in_untouched_files_do_not_block(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,))
    _uv(tmp_path, monkeypatch, {GATE4_MYPY: GhReply(1, _untouched_output())})
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    token = f"{UNTOUCHED_ONLY_PREFIX}src/cw/elsewhere.py, {OTHER}"
    assert token in result.friction_highlights
    assert RUFF_NOT_DECLARED_FRICTION in result.friction_highlights
    _assert_no_gate_dirs(gate_root, h)


def test_a_resumed_branch_type_checks_the_earlier_commits_files(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,))
    commit_tracked_file(h.worktree, PRIOR, "P = 1\n")
    uv = _uv(tmp_path, monkeypatch)
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert [*_GATE4_MYPY_ARGV, PRIOR, WIDGET] in uv.calls


# -- skips ------------------------------------------------------------------------


def test_no_declared_tool_skips_with_friction_and_never_launches_uv(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness()
    uv = _uv(tmp_path, monkeypatch)
    h.turn()

    with patch("cw.codex_impl._type_gate.snapshot_commit") as snapshot:
        result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert SKIPPED_NO_GATE_FRICTION in result.friction_highlights
    assert uv.calls == []
    snapshot.assert_not_called()
    _assert_no_gate_dirs(gate_root, h)


def test_a_turn_with_no_python_files_runs_no_ruff_or_mypy(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    notes = "docs/notes.md"
    h = harness(gates=(RUFF_GATE, MYPY_GATE), plan=PLAN.replace(WIDGET, notes))
    uv = _uv(tmp_path, monkeypatch)
    h.turn({notes: "notes\n"})

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert _gate4_calls(uv.calls) == []
    assert SKIPPED_NO_GATE_FRICTION not in result.friction_highlights
    _assert_no_gate_dirs(gate_root, h)


@pytest.mark.parametrize(
    ("gates", "tokens"),
    [
        (
            (("ruff", "true || ruff check src/", 5.0),),
            [f"{LAUNCHER_UNSUPPORTED_PREFIX}ruff", MYPY_NOT_DECLARED_FRICTION],
        ),
        (
            (("mypy", "true || mypy src/", 5.0),),
            [RUFF_NOT_DECLARED_FRICTION, f"{LAUNCHER_UNSUPPORTED_PREFIX}mypy"],
        ),
        (
            (("mypy", f"true || mypy src/ --baseline-file {BASELINE_FILE}", 5.0),),
            [RUFF_NOT_DECLARED_FRICTION, f"{LAUNCHER_UNSUPPORTED_PREFIX}mypy-baseline"],
        ),
    ],
    ids=["ruff", "mypy", "baseline-consumer"],
)
def test_a_declared_tool_without_uv_run_is_skipped_with_friction(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
    gates: tuple[tuple[str, str, float], ...],
    tokens: list[str],
) -> None:
    h = harness(gates=gates)
    if any(BASELINE_FILE in command for _name, command, _s in gates):
        _seed_main(h, BASELINE_FILE, "old entry\n")
    uv = _uv(tmp_path, monkeypatch)
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    for token in tokens:
        assert token in result.friction_highlights
    assert uv.calls == []
    _assert_no_gate_dirs(gate_root, h)


@pytest.mark.parametrize(
    "command",
    ["true || make mypy ruff", "true || pre-commit run ruff --all-files"],
    ids=["make", "pre-commit"],
)
def test_a_wrapper_declared_gate_skips_with_friction(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
    command: str,
) -> None:
    h = harness(gates=(("lint", command, 5.0),))
    uv = _uv(tmp_path, monkeypatch)
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert SKIPPED_NO_GATE_FRICTION in result.friction_highlights
    assert uv.calls == []


def test_gate4_repeats_the_declaring_gates_uv_options(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(
        gates=(
            ("ruff", "uv run --extra dev ruff check src/", 5.0),
            ("mypy", "uv run --extra dev mypy src/", 5.0),
        )
    )
    uv = _uv(tmp_path, monkeypatch)
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    options = ["run", "--extra", "dev"]
    assert [*options, "ruff", "check", "--force-exclude", WIDGET] in uv.calls
    assert [*options, *_GATE4_MYPY_ARGV[1:], WIDGET] in uv.calls


@pytest.mark.parametrize(
    "command",
    [
        "true || cd sub && uv run mypy .",
        "uv run --directory backend mypy .",
    ],
    ids=["cd", "directory"],
)
def test_a_gate_cw_cannot_reproduce_is_skipped_not_run_at_the_root(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
    command: str,
) -> None:
    h = harness(gates=(("mypy", command, 5.0),))
    uv = _uv(tmp_path, monkeypatch)
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert f"{LAUNCHER_UNSUPPORTED_PREFIX}mypy" in result.friction_highlights
    assert _gate4_calls(uv.calls) == []
    _assert_no_gate_dirs(gate_root, h)


def test_an_unreproducible_baseline_consumer_runs_verbatim_without_a_record(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    command = f"uv run --directory . mypy src/ --baseline-file {BASELINE_FILE}"
    h = harness(gates=(("mypy", command, 5.0),))
    _seed_main(h, BASELINE_FILE, "old entry\n")
    uv = _uv(tmp_path, monkeypatch)
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    verbatim = ["run", "--directory", ".", "mypy", "src/", "--baseline-file"]
    assert uv.calls.count([*verbatim, BASELINE_FILE]) == 2  # the gate, then gate 4
    assert [c for c in uv.calls if "--no-pretty" in c] == []
    assert f"{LAUNCHER_UNSUPPORTED_PREFIX}mypy" in result.friction_highlights


# -- the baseline ------------------------------------------------------------------


def test_a_baseline_command_failing_in_the_gate_worktree_parks_with_its_rule(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(BASELINE_GATE,))
    _seed_main(h, BASELINE_FILE, "old entry\n")
    fork = h.head()
    _uv_by_cwd(tmp_path, monkeypatch, GhReply(1, _mypy_output()))
    h.turn()

    details = _assert_gate4_park(h.run(), h, fork)

    assert f"`{BASELINE_CMD}` exited 1" in details
    assert (
        f"the blocking set is what `{BASELINE_CMD}` reports in touched files, and "
        f"the baseline file may not gain entries since {fork[:12]}."
    ) in details
    assert f"- {WIDGET}" in details
    assert NO_BASELINE_RULE not in details
    _assert_no_gate_dirs(gate_root, h)


def test_a_grown_baseline_parks(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(BASELINE_GATE,), plan=_plan_with(BASELINE_FILE))
    _seed_main(h, BASELINE_FILE, "old entry\n")
    fork = h.head()
    uv = _uv(tmp_path, monkeypatch)
    h.turn({WIDGET: "WIDGET = 1\n", BASELINE_FILE: "old entry\nnew entry\n"})

    details = _assert_gate4_park(h.run(), h, fork)

    assert f"the mypy baseline file {BASELINE_FILE} gained entries" in details
    assert "a suppression that needs explicit approval" in details
    assert "- new entry" in details
    assert [c for c in uv.calls if BASELINE_FILE in c] == [
        ["run", "mypy", "src/", "--baseline-file", BASELINE_FILE]
    ]
    _assert_no_gate_dirs(gate_root, h)


def test_a_grown_baseline_with_no_visible_consumer_still_parks(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,), plan=_plan_with(BASELINE_FILE))
    _seed_main(h, BASELINE_FILE, "old entry\n")
    fork = h.head()
    _uv(tmp_path, monkeypatch)
    h.turn({WIDGET: "WIDGET = 1\n", BASELINE_FILE: "old entry\nnew entry\n"})

    details = _assert_gate4_park(h.run(), h, fork)

    assert "gained entries" in details
    _assert_no_gate_dirs(gate_root, h)


def test_a_baseline_with_no_visible_consumer_skips_mypy_but_runs_ruff(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(RUFF_GATE, MYPY_GATE))
    _seed_main(h, BASELINE_FILE, "old entry\n")
    uv = _uv(tmp_path, monkeypatch)
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert f"{CONSUMER_UNKNOWN_PREFIX}{BASELINE_FILE}" in result.friction_highlights
    assert ["run", "ruff", "check", "--force-exclude", WIDGET] in uv.calls
    assert [c for c in uv.calls if "--no-pretty" in c] == []


def test_baselined_errors_are_recorded_and_do_not_block(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(BASELINE_GATE,))
    _seed_main(h, BASELINE_FILE, _baseline_listing(WIDGET))
    uv = _uv(tmp_path, monkeypatch, {GATE4_MYPY: GhReply(1, _mypy_output())})
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert (
        f"{BASELINE_FRICTION_PREFIX}file={BASELINE_FILE} cmd={BASELINE_CMD}"
        in result.friction_highlights
    )
    entries = read_deferred_findings_md(h.worktree / DEFERRED_FINDINGS_RELATIVE_PATH)
    assert [(e.file, e.summary) for e in entries] == [
        (WIDGET, f"pre-existing baselined mypy errors in {WIDGET}")
    ]
    assert entries[0].rationale.endswith(
        f"{WIDGET}:5 [assignment], {WIDGET}:6 [attr-defined]"
    )
    baseline_runs = [c for c in uv.calls if BASELINE_FILE in c]
    assert len(baseline_runs) == 2  # the repo gate, then gate 4's run
    assert [*_GATE4_MYPY_ARGV, WIDGET] in uv.calls
    _assert_no_gate_dirs(gate_root, h)


def test_a_record_run_with_nothing_parseable_adds_friction(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(BASELINE_GATE,))
    _seed_main(h, BASELINE_FILE, "old entry\n")
    _uv(tmp_path, monkeypatch, {GATE4_MYPY: GhReply(2, _USAGE)})
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert RECORD_FAILED_FRICTION in result.friction_highlights
    assert not (h.worktree / DEFERRED_FINDINGS_RELATIVE_PATH).exists()


def test_a_malformed_deferred_artifact_is_left_untouched_with_friction(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(BASELINE_GATE,))
    _seed_main(h, BASELINE_FILE, _baseline_listing(WIDGET))
    artifact = h.worktree / DEFERRED_FINDINGS_RELATIVE_PATH
    artifact.write_text("hand-edited, not the artifact\n", encoding="utf-8")
    _uv(tmp_path, monkeypatch, {GATE4_MYPY: GhReply(1, _mypy_output())})
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert DEFERRED_UNPARSEABLE_FRICTION in result.friction_highlights
    assert artifact.read_text(encoding="utf-8") == "hand-edited, not the artifact\n"


def test_a_file_absent_from_the_baseline_is_not_recorded_as_old_debt(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(BASELINE_GATE,))
    # A longer path that ends in the touched one is not an entry for it.
    _seed_main(h, BASELINE_FILE, _baseline_listing(f"vendor/{WIDGET}"))
    _uv(tmp_path, monkeypatch, {GATE4_MYPY: GhReply(1, _mypy_output())})
    h.turn()

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert f"{BASELINE_UNCOVERED_PREFIX}{WIDGET}" in result.friction_highlights
    assert not (h.worktree / DEFERRED_FINDINGS_RELATIVE_PATH).exists()


def test_an_unreadable_baseline_at_record_time_is_friction_not_a_park(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(BASELINE_GATE,))
    _seed_main(h, BASELINE_FILE, _baseline_listing(WIDGET))
    _uv(tmp_path, monkeypatch, {GATE4_MYPY: GhReply(1, _mypy_output())})
    h.turn()
    failure = subprocess.CalledProcessError(128, ["git", "show"])

    with patch("cw.codex_impl._type_gate.baselined_paths", side_effect=failure):
        result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert RECORD_FAILED_FRICTION in result.friction_highlights
    assert not (h.worktree / DEFERRED_FINDINGS_RELATIVE_PATH).exists()


# -- tests.prepare ------------------------------------------------------------------


def _prepare_config(command: str) -> str:
    return f"tests:\n  prepare: >-\n    {command}\n"


def test_tests_prepare_from_the_default_branch_runs_first_in_the_gate_worktree(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    log = tmp_path / "prepare.log"
    h = harness(gates=(RUFF_GATE, MYPY_GATE))
    _seed_main(
        h, CONFIG, _prepare_config(f'printf \'%s %s\\n\' "$PWD" "$TMPDIR" >> {log}')
    )
    _uv(tmp_path, monkeypatch)
    h.turn()
    order: list[tuple[str, Path]] = []

    def _spy(plan: Any, **kwargs: Any) -> GateFailure | None:
        order.append((plan.gates[0].name, kwargs["worktree"]))
        return run_cycle_gates(plan, **kwargs)

    with patch("cw.codex_impl._type_gate.run_cycle_gates", _spy):
        result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert [name for name, _ in order] == [
        "impl-gate4-prepare",
        "impl-gate4-ruff",
        "impl-gate4-mypy",
    ]
    pwd, scratch = log.read_text(encoding="utf-8").split()
    assert Path(pwd) == order[0][1]
    assert Path(pwd).name.startswith("cw-gate-wt-impl-")
    assert Path(scratch).name.startswith("cw-gate-")
    assert not Path(scratch).is_relative_to(h.worktree)
    assert not Path(scratch).is_relative_to(Path(pwd))


def test_a_prepare_added_by_the_turn_is_never_run(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    log = tmp_path / "prepare.log"
    h = harness(gates=(MYPY_GATE,), plan=_plan_with(CONFIG))
    _uv(tmp_path, monkeypatch)
    h.turn({WIDGET: "WIDGET = 1\n", CONFIG: _prepare_config(f"touch {log}")})

    result = h.run()

    assert result.status == "stage_complete", _details(result)
    assert not log.exists()


def test_a_failing_tests_prepare_parks_and_runs_no_check(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(RUFF_GATE, MYPY_GATE))
    _seed_main(h, CONFIG, _prepare_config("echo prep-broke; exit 3"))
    fork = h.head()
    uv = _uv(tmp_path, monkeypatch)
    h.turn()

    details = _assert_gate4_park(h.run(), h, fork)

    assert "tests.prepare `echo prep-broke; exit 3` exited 3 before the type check" in (
        details
    )
    assert "prep-broke" in details
    assert _gate4_calls(uv.calls) == []
    _assert_no_gate_dirs(gate_root, h)


def test_an_unreadable_tests_prepare_parks(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
) -> None:
    h = harness(gates=(MYPY_GATE,))
    _seed_main(h, CONFIG, "tests:\n  prepare: [a, b]\n")
    fork = h.head()
    uv = _uv(tmp_path, monkeypatch)
    h.turn()

    details = _assert_gate4_park(h.run(), h, fork)

    assert details.startswith(f"{GATE4_PREFIX}tests.prepare could not be read: ")
    assert "single shell command string" in details
    assert _gate4_calls(uv.calls) == []
    _assert_no_gate_dirs(gate_root, h)
