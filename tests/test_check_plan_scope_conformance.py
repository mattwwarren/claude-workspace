"""Tests for .claude/scripts/check_plan_scope_conformance.py (#1779).

Uses importlib to load the script directly (it lives outside the src/ tree),
mirroring tests/test_prep_pr_state.py's loader pattern. All fixtures are
deterministic string literals — no live plan document is ever read.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Script loader
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "check_plan_scope_conformance.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_plan_scope_conformance", _SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_plan_scope_conformance", mod)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _plan_text(paths: list[str]) -> str:
    """Build a realistic plan document with a ``## Files Modified`` section."""
    bullets = "\n".join(f"- {p} (~40 lines)" for p in paths)
    return (
        "# Implementation Plan: Something (#9999)\n\n"
        "## Patterns Found\n\n"
        "- Proposed: a thing.\n\n"
        "## Files Modified\n\n"
        f"{bullets}\n\n"
        "**Scope tier:** small\n\n"
        "## Ambiguities\n\n"
        "NO_AMBIGUITIES\n"
    )


def _paths(prefix: str, count: int) -> list[str]:
    return [f"src/cw/{prefix}_{i}.py" for i in range(count)]


_DEFAULTS = (_mod.SCOPE_DRIFT_RATIO, _mod.SCOPE_DRIFT_ABS_FLOOR)


def _check(plan_files: list[str], touched_files: list[str]) -> dict[str, object]:
    ratio, abs_floor = _DEFAULTS
    result = _mod.check_scope_conformance(plan_files, touched_files, ratio, abs_floor)
    return dict(result)


# ---------------------------------------------------------------------------
# Threshold arithmetic
# ---------------------------------------------------------------------------


def test_no_drift_within_threshold() -> None:
    """The ticket's negative regression case: plan 14 files, delivered 16."""
    planned = _paths("planned", 14)
    delivered = [*planned, "src/cw/extra_a.py", "src/cw/extra_b.py"]

    verdict = _check(planned, delivered)

    assert verdict["plan_file_count"] == 14
    assert verdict["delivered_file_count"] == 16
    assert verdict["allowed_extra"] == 7
    assert verdict["triggered"] is False


def test_drift_exceeds_threshold_realistic_1711() -> None:
    """The ticket's positive regression case: plan 14 files, delivered 31."""
    planned = _paths("planned", 14)
    delivered = [*planned, *_paths("unplanned", 17)]

    verdict = _check(planned, delivered)

    assert verdict["plan_file_count"] == 14
    assert verdict["delivered_file_count"] == 31
    assert verdict["allowed_extra"] == 7
    assert verdict["triggered"] is True
    assert verdict["extra_files"] == sorted(_paths("unplanned", 17))


def test_missing_planned_files_do_not_count_as_drift() -> None:
    """A planned file absent from the diff must not inflate extra_files."""
    planned = _paths("planned", 10)
    delivered = planned[:3]

    verdict = _check(planned, delivered)

    assert verdict["extra_files"] == []
    assert verdict["triggered"] is False
    assert verdict["plan_file_count"] == 10
    assert verdict["delivered_file_count"] == 3


def test_small_plan_abs_floor_applies() -> None:
    """A 2-file plan with 4 extra files stays under the absolute floor of 5."""
    planned = _paths("planned", 2)
    delivered = [*planned, *_paths("unplanned", 4)]

    verdict = _check(planned, delivered)

    assert verdict["allowed_extra"] == _mod.SCOPE_DRIFT_ABS_FLOOR
    assert verdict["triggered"] is False


def test_extra_files_equal_to_allowed_extra_does_not_trigger() -> None:
    """Boundary: len(extra_files) == allowed_extra must NOT trigger.

    The trigger condition is a strict ``>``; landing exactly on the allowance
    must read as conforming, not drift.
    """
    planned = _paths("planned", 14)
    delivered = [*planned, *_paths("unplanned", 7)]

    verdict = _check(planned, delivered)

    assert verdict["allowed_extra"] == 7
    assert len(verdict["extra_files"]) == 7
    assert verdict["triggered"] is False


def test_extra_files_one_over_allowed_extra_triggers() -> None:
    """Boundary: len(extra_files) == allowed_extra + 1 must trigger.

    Guards against an off-by-one (e.g. ``>`` accidentally becoming ``>=``)
    silently flipping pipeline behavior right at the threshold.
    """
    planned = _paths("planned", 14)
    delivered = [*planned, *_paths("unplanned", 8)]

    verdict = _check(planned, delivered)

    assert verdict["allowed_extra"] == 7
    assert len(verdict["extra_files"]) == 8
    assert verdict["triggered"] is True


def test_blocker_details_enumerates_extra_file_paths() -> None:
    """extra_files is the operator's only authorization surface (R1).

    It must carry the exact delivered-but-unplanned paths, verbatim, in a
    stable sorted order.
    """
    planned = ["src/cw/a.py", "src/cw/b.py"]
    delivered = [
        "src/cw/a.py",
        "src/cw/z_last.py",
        "src/cw/m_middle.py",
        "src/cw/c_first.py",
        "tests/test_z.py",
        "docs/thing.md",
        "src/cw/n.py",
    ]

    verdict = _check(planned, delivered)

    assert verdict["extra_files"] == [
        "docs/thing.md",
        "src/cw/c_first.py",
        "src/cw/m_middle.py",
        "src/cw/n.py",
        "src/cw/z_last.py",
        "tests/test_z.py",
    ]
    assert verdict["triggered"] is True


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------


def test_parses_files_modified_section_from_plan_md() -> None:
    """Bullet lines under ``## Files Modified`` yield exactly their paths."""
    text = _plan_text(
        ["src/cw/one.py", "tests/test_one.py", ".claude/commands/auto-dev.md"]
    )

    assert _mod._parse_files_modified(text) == [
        "src/cw/one.py",
        "tests/test_one.py",
        ".claude/commands/auto-dev.md",
    ]


def test_parses_backticked_and_bolded_bullet_paths() -> None:
    """Paths wrapped in backticks or bold markers are unwrapped."""
    text = (
        "## Files Modified\n\n"
        "- `src/cw/one.py` (~40 lines)\n"
        "- **tests/test_one.py** — new test\n"
        "* docs/thing.md\n\n"
        "## Next\n"
    )

    assert _mod._parse_files_modified(text) == [
        "src/cw/one.py",
        "tests/test_one.py",
        "docs/thing.md",
    ]


def test_ignores_malformed_or_non_bullet_lines_in_files_modified() -> None:
    """Stray prose, tables, and non-path bullets under the heading are ignored."""
    text = (
        "## Files Modified\n\n"
        "This section enumerates every file the plan will touch.\n"
        "| File | Est. lines |\n"
        "|---|---|\n"
        "- src/cw/real.py (~10 lines)\n"
        "- Note that nothing else is touched\n"
        "-\n"
        "- **Scope tier:** large\n\n"
        "## Ambiguities\n"
        "- src/cw/not_in_section.py\n"
    )

    assert _mod._parse_files_modified(text) == ["src/cw/real.py"]


def test_missing_heading_yields_empty_file_list() -> None:
    """A plan with no ``## Files Modified`` heading parses to nothing."""
    assert _mod._parse_files_modified("# Plan\n\n## Patterns Found\n\n- a\n") == []


# ---------------------------------------------------------------------------
# pyproject.toml per-repo override (R2)
# ---------------------------------------------------------------------------


def _write_repo(tmp_path: Path, pyproject: str | None) -> Path:
    """Create ``<tmp_path>/repo/.cw/plan.md`` and return the plan path."""
    repo = tmp_path / "repo"
    (repo / ".cw").mkdir(parents=True)
    plan = repo / ".cw" / "plan.md"
    plan.write_text(_plan_text(_paths("planned", 14)), encoding="utf-8")
    if pyproject is not None:
        (repo / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return plan


def test_pyproject_override_applied(tmp_path: Path) -> None:
    """A narrower [tool.cw.scope_conformance] flips a passing case to triggered."""
    plan = _write_repo(
        tmp_path,
        "[tool.cw.scope_conformance]\nratio = 1.05\nabs_floor = 1\n",
    )

    ratio, abs_floor = _mod._load_scope_thresholds(plan)
    assert ratio == 1.05
    assert abs_floor == 1

    planned = _paths("planned", 14)
    delivered = [*planned, "src/cw/extra_a.py", "src/cw/extra_b.py"]
    verdict = dict(_mod.check_scope_conformance(planned, delivered, ratio, abs_floor))
    assert verdict["allowed_extra"] == 1
    assert verdict["triggered"] is True

    # The same input under the shipped defaults does NOT trigger.
    assert _check(planned, delivered)["triggered"] is False


def test_pyproject_override_fails_safe_on_malformed_toml(tmp_path: Path) -> None:
    """Syntactically invalid TOML falls back to the shipped defaults."""
    plan = _write_repo(tmp_path, "[tool.cw.scope_conformance\nratio = = =\n")

    assert _mod._load_scope_thresholds(plan) == _DEFAULTS


def test_pyproject_override_fails_safe_on_missing_table_or_key(
    tmp_path: Path,
) -> None:
    """A missing table, a missing key, or a wrong-typed value falls back."""
    no_table = _write_repo(tmp_path / "a", '[project]\nname = "x"\n')
    assert _mod._load_scope_thresholds(no_table) == _DEFAULTS

    partial = _write_repo(
        tmp_path / "b", "[tool.cw.scope_conformance]\nabs_floor = 2\n"
    )
    assert _mod._load_scope_thresholds(partial) == (_mod.SCOPE_DRIFT_RATIO, 2)

    wrong_type = _write_repo(
        tmp_path / "c",
        '[tool.cw.scope_conformance]\nratio = "wide"\nabs_floor = true\n',
    )
    assert _mod._load_scope_thresholds(wrong_type) == _DEFAULTS


def test_pyproject_override_fails_safe_on_missing_file(tmp_path: Path) -> None:
    """No pyproject.toml anywhere up the tree falls back to defaults."""
    plan = _write_repo(tmp_path, None)

    assert _mod._load_scope_thresholds(plan) == _DEFAULTS


def test_pyproject_override_found_by_upward_search(tmp_path: Path) -> None:
    """The loader searches upward from --plan's parent for pyproject.toml."""
    plan = _write_repo(tmp_path, "[tool.cw.scope_conformance]\nabs_floor = 3\n")
    nested = plan.parent / "nested" / "deeper"
    nested.mkdir(parents=True)
    nested_plan = nested / "plan.md"
    nested_plan.write_text(plan.read_text(encoding="utf-8"), encoding="utf-8")

    assert _mod._load_scope_thresholds(nested_plan) == (_mod.SCOPE_DRIFT_RATIO, 3)


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def _run_cli(plan: Path, touched: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--plan",
            str(plan),
            "--touched-files",
            str(touched),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_exit_code_and_json_contract(tmp_path: Path) -> None:
    """Exit 0 within threshold, 1 when triggered, 2 on parse error."""
    planned = _paths("planned", 14)
    plan = tmp_path / "plan.md"
    plan.write_text(_plan_text(planned), encoding="utf-8")

    clean = tmp_path / "touched-clean.txt"
    clean.write_text("\n".join([*planned, "src/cw/extra.py"]) + "\n", encoding="utf-8")
    ok = _run_cli(plan, clean)
    assert ok.returncode == 0, ok.stderr
    verdict = json.loads(ok.stdout)
    assert set(verdict) == {
        "triggered",
        "extra_files",
        "allowed_extra",
        "plan_file_count",
        "delivered_file_count",
    }
    assert verdict["triggered"] is False
    assert verdict["extra_files"] == ["src/cw/extra.py"]
    assert verdict["allowed_extra"] == 7
    assert verdict["plan_file_count"] == 14
    assert verdict["delivered_file_count"] == 15

    drifted = tmp_path / "touched-drift.txt"
    drifted.write_text(
        "\n".join([*planned, *_paths("unplanned", 17)]) + "\n", encoding="utf-8"
    )
    tripped = _run_cli(plan, drifted)
    assert tripped.returncode == 1
    assert json.loads(tripped.stdout)["triggered"] is True

    headless_plan = tmp_path / "no-heading.md"
    headless_plan.write_text("# Plan\n\n## Patterns Found\n\n- a\n", encoding="utf-8")
    unparseable = _run_cli(headless_plan, clean)
    assert unparseable.returncode == 2
    assert "Files Modified" in unparseable.stderr


def test_cli_exit_2_on_missing_input_files(tmp_path: Path) -> None:
    """An unreadable --plan or --touched-files is a usage error, not a block."""
    plan = tmp_path / "plan.md"
    plan.write_text(_plan_text(_paths("planned", 3)), encoding="utf-8")
    touched = tmp_path / "touched.txt"
    touched.write_text("src/cw/planned_0.py\n", encoding="utf-8")

    assert _run_cli(tmp_path / "absent.md", touched).returncode == 2
    assert _run_cli(plan, tmp_path / "absent.txt").returncode == 2


def test_cli_ignores_blank_lines_in_touched_files(tmp_path: Path) -> None:
    """Blank/whitespace lines in the touched-files list are not counted."""
    planned = _paths("planned", 3)
    plan = tmp_path / "plan.md"
    plan.write_text(_plan_text(planned), encoding="utf-8")
    touched = tmp_path / "touched.txt"
    touched.write_text("\n".join(planned) + "\n\n   \n", encoding="utf-8")

    result = _run_cli(plan, touched)

    assert result.returncode == 0
    assert json.loads(result.stdout)["delivered_file_count"] == 3
