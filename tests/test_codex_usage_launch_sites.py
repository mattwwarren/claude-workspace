"""Drift guard: every codex launch site is classified for usage accounting (#3063).

The accumulator rides inside the one ``runner`` handed to every launch site, so
a *new* launch that is not classified here would silently record nothing. This
test scans ``src/cw`` with ``ast`` for the two shapes a launch takes (a string
literal ``"exec"`` in an argv builder, and ``<...runner>.run(...)``) and fails
until each match is listed in :data:`_SITES` as RECORDED (with the stage and the
wiring expression that makes it so) or UNREACHABLE (with the reason). It is the
same style as ``tests/test_ambiguous_session_lookup_guard.py``.

Sites are keyed by ``(file, enclosing function)``, not by line number, so an
unrelated edit does not break the table.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "cw"

RECORDED: Final = "recorded"
UNREACHABLE: Final = "unreachable"

# (file, enclosing function) -> (classification, note)
_SITES: Final[dict[tuple[str, str], tuple[str, str]]] = {
    # PLAN, IMPL and FINALIZE turns: the driver hands each stage a runner
    # recording under its own stage; nothing at these lines changes.
    ("codex_plan/_argv.py", "build_plan_argv"): (RECORDED, "PLAN argv"),
    ("codex_plan/_run.py", "turn"): (RECORDED, "RecordingRunner(PLAN)"),
    ("codex_impl/_argv.py", "build_impl_argv"): (RECORDED, "IMPL argv"),
    ("codex_impl/_run.py", "turn"): (RECORDED, "RecordingRunner(IMPL)"),
    ("codex_finalize/_argv.py", "build_finalize_argv"): (RECORDED, "FINALIZE argv"),
    ("codex_finalize/_run.py", "turn"): (RECORDED, "RecordingRunner(FINALIZE)"),
    # Reviewer roles (and PLAN stations through the same loop): the role loop
    # relabels with the real role name. The first attempt carries --json.
    ("codex_review/_roles.py", "_build_generic_codex_argv"): (RECORDED, "role argv"),
    ("codex_review/_roles.py", "_run_codex_role"): (
        RECORDED,
        "first run records; the degrade-and-retry rerun strips --json and is "
        "skipped by the recorder, and a failed first attempt is silent",
    ),
    # Aggregator: first run recorded, the flag-rejection retry is not.
    ("codex_review/_engine_aggregate.py", "_run_once"): (
        RECORDED,
        "first run records as ENGINE_AGGREGATE; the retry has no --json",
    ),
    # Fix cycle and repair turn share the one runner the driver relabels FIX.
    ("codex_fix_loop/commit.py", "_build_fix_codex_argv"): (RECORDED, "fix argv"),
    ("codex_fix_loop/commit.py", "_run_fix_and_commit"): (RECORDED, "FIX"),
    ("codex_fix_loop/commit.py", "_run_repair_turn"): (RECORDED, "FIX (same runner)"),
    # Tracker-IO: recorded through the late flush.
    ("tracker_io/_argv.py", "build_tracker_io_argv"): (RECORDED, "tracker-IO argv"),
    ("tracker_io/_turn.py", "_launch"): (RECORDED, "TRACKER_IO via flush_late_usage"),
    # Not a turn: the capability probe has no --json and is skipped.
    ("codex_review/_capability.py", "_build_probe_argv"): (
        UNREACHABLE,
        "capability probe argv, not a turn",
    ),
    ("codex_review/_capability.py", "_run_capability_probe"): (
        UNREACHABLE,
        "capability probe run, not a turn; the recorder skips it (no --json)",
    ),
    # Not codex: the claude -p lane records on ReviewerRunMetrics only (R11).
    ("claude_review/_roles.py", "_run_claude_role"): (
        UNREACHABLE,
        "claude -p lane; tokens land on ReviewerRunMetrics, not Session.usage",
    ),
}

# Each RECORDED stage must still be wired: (file, snippet that must appear).
_WIRING: Final[list[tuple[str, str]]] = [
    ("codex_driver.py", "usage.recording_runner(UsageStage.PLAN"),
    ("codex_driver.py", "usage.recording_runner(UsageStage.IMPL"),
    ("codex_driver.py", "UsageStage.FINALIZE"),
    ("codex_driver.py", "UsageStage.REVIEW_ROLE"),
    ("codex_review/_roles.py", "relabeled(runner, role=role)"),
    ("codex_plan/_stations.py", "relabeled(runner, UsageStage.PLAN_STATION)"),
    (
        "codex_review/_engine_round.py",
        "relabeled(inputs.runner, UsageStage.ENGINE_AGGREGATE)",
    ),
    ("codex_fix_loop/_driver.py", "relabeled(runner, UsageStage.FIX)"),
    ("codex_review_persist.py", "relabeled(runner, UsageStage.TRACKER_IO)"),
    ("codex_stage_post.py", "relabeled(runner, UsageStage.TRACKER_IO)"),
]


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return node.name
    return "<module>"


def _runner_name(call: ast.Call) -> str | None:
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "run"):
        return None
    value = func.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _scan() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        relative = path.relative_to(_SRC).as_posix()
        for node in ast.walk(tree):
            is_exec = isinstance(node, ast.Constant) and node.value == "exec"
            name = _runner_name(node) if isinstance(node, ast.Call) else None
            if is_exec or (name is not None and name.endswith("runner")):
                found.add((relative, _enclosing_function(node, parents)))
    return found


def test_every_launch_site_is_classified() -> None:
    unclassified = sorted(_scan() - set(_SITES))

    assert not unclassified, (
        "new codex launch site(s) not classified for usage accounting; add each "
        "to _SITES as RECORDED (and wire a relabeled/recording runner) or "
        f"UNREACHABLE with a reason: {unclassified}"
    )


def test_the_table_has_no_stale_entries() -> None:
    stale = sorted(set(_SITES) - _scan())

    assert not stale, f"classified launch site(s) no longer exist: {stale}"


def test_classifications_are_known_values() -> None:
    assert {kind for kind, _note in _SITES.values()} <= {RECORDED, UNREACHABLE}


@pytest.mark.parametrize(("file", "snippet"), _WIRING)
def test_recorded_stages_are_still_wired(file: str, snippet: str) -> None:
    source = (_SRC / file).read_text(encoding="utf-8")

    assert snippet in source
