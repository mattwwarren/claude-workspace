"""Guard for the pre-cut changelog test set (#3886).

The ``release_cut`` marker selects the changelog-machinery test files a release
cut runs as a precaution (``uv run --extra mcp pytest tests/ -n auto -m
release_cut``). ``RELEASE_CUT_FILES`` below is the explicit registry. The marker
and the registry must agree in both directions, so a new changelog test file
cannot silently miss the cut set, and a retired one cannot linger.

The scans are AST-based on attribute nodes, never raw text, so docstrings,
comments and string literals (including this guard's own snippets) cannot trip
them. Mirrors the registered-and-applied check in ``tests/test_ci_workflow.py``
and the AST scan in ``tests/test_changelog_slice_guard.py``.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from tests._shared.workflow_text import _REPO_ROOT

pytestmark = pytest.mark.release_cut

MARKER = "release_cut"
RUNBOOK_COMMAND = "uv run --extra mcp pytest tests/ -n auto -m release_cut"
REGISTRY_PATH = "tests/test_release_cut_set.py"

RELEASE_CUT_FILES: tuple[str, ...] = (
    "tests/test_changelog_advisory_workflow.py",
    "tests/test_changelog_gate_prose_sync.py",
    "tests/test_changelog_gate_workflow.py",
    "tests/test_changelog_rebuild.py",
    "tests/test_changelog_repair_ledger_remap.py",
    "tests/test_changelog_repair_real_git.py",
    "tests/test_changelog_slice_guard.py",
    "tests/test_changelog_sync.py",
    "tests/test_changelog_text.py",
    "tests/test_check_changelog_frozen.py",
    "tests/test_classify_merge_conflict.py",
    "tests/test_reconcile_changelog_repair.py",
    "tests/test_release_cut_set.py",
    "tests/test_release_tag_workflow.py",
    "tests/test_repair_fence.py",
    "tests/test_repair_proof.py",
    "tests/test_stage_verification_finalize_changelog_repair.py",
)


def _mark_name(node: ast.AST) -> str | None:
    """``X`` for a ``pytest.mark.X`` attribute node (bare or called), else None."""
    if isinstance(node, ast.Call):
        node = node.func
    if not isinstance(node, ast.Attribute):
        return None
    mark = node.value
    if not (isinstance(mark, ast.Attribute) and mark.attr == "mark"):
        return None
    root = mark.value
    if not (isinstance(root, ast.Name) and root.id == "pytest"):
        return None
    return node.attr


def _marks_used(source: str) -> set[str]:
    """Every ``pytest.mark.X`` name in *source*, at any level and in any form."""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        name = _mark_name(node)
        if name is not None:
            found.add(name)
    return found


def _module_pytestmark_names(source: str) -> set[str]:
    """Mark names a module-level ``pytestmark`` assigns (single, list or tuple)."""
    names: set[str] = set()
    for stmt in ast.parse(source).body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            continue
        values = (
            stmt.value.elts
            if isinstance(stmt.value, ast.List | ast.Tuple)
            else [stmt.value]
        )
        for value in values:
            name = _mark_name(value)
            if name is not None:
                names.add(name)
    return names


def _test_files() -> list[Path]:
    return sorted((_REPO_ROOT / "tests").glob("test_*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def _marked_files() -> set[str]:
    return {
        _rel(path)
        for path in _test_files()
        if MARKER in _module_pytestmark_names(path.read_text(encoding="utf-8"))
    }


def test_every_registry_entry_exists() -> None:
    missing = [p for p in RELEASE_CUT_FILES if not (_REPO_ROOT / p).is_file()]
    assert not missing, (
        f"registry names a file that does not exist: {missing}; "
        "remove or rename it in RELEASE_CUT_FILES"
    )


def test_marker_is_registered() -> None:
    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith(f"{MARKER}:") for m in markers), (
        f"pyproject.toml [tool.pytest.ini_options].markers has no '{MARKER}:' entry; "
        "an unregistered marker only warns, so register it"
    )


def test_marked_files_equal_the_registry() -> None:
    marked = _marked_files()
    registry = set(RELEASE_CUT_FILES)
    unmarked = sorted(registry - marked)
    unlisted = sorted(marked - registry)
    assert not unmarked, (
        f"registry file lacks 'pytestmark = pytest.mark.release_cut': {unmarked}"
    )
    assert not unlisted, (
        "release_cut-marked file missing from RELEASE_CUT_FILES (add it or unmark): "
        f"{unlisted}"
    )


def test_every_changelog_named_test_file_is_in_the_set() -> None:
    changelog_files = {_rel(path) for path in _test_files() if "changelog" in path.name}
    missing = sorted(changelog_files - set(RELEASE_CUT_FILES))
    assert not missing, (
        f"changelog-named test files missing from RELEASE_CUT_FILES: {missing}; "
        "a changelog-machinery file joins the cut set (marker plus registry entry)"
    )


def test_no_member_is_integration_marked() -> None:
    offenders = [
        p
        for p in RELEASE_CUT_FILES
        if "integration" in _marks_used((_REPO_ROOT / p).read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"integration-marked member(s) {offenders}: '-m release_cut' overrides the "
        "addopts '-m not integration' default, so these would run in the cut set; "
        "split the integration tests out of the member file"
    )


def test_marker_use_is_confined_to_registered_files() -> None:
    stray = [
        _rel(path)
        for path in _test_files()
        if MARKER in _marks_used(path.read_text(encoding="utf-8"))
        and _rel(path) not in RELEASE_CUT_FILES
    ]
    assert not stray, (
        f"pytest.mark.release_cut used outside the registry: {stray}; "
        "add the file to RELEASE_CUT_FILES or drop the mark"
    )


def test_runbook_names_the_set() -> None:
    runbook = (_REPO_ROOT / "docs" / "release-playbook.md").read_text(encoding="utf-8")
    assert RUNBOOK_COMMAND in runbook, (
        f"docs/release-playbook.md must name the cut command: {RUNBOOK_COMMAND!r}"
    )
    assert REGISTRY_PATH in runbook, (
        f"docs/release-playbook.md must name the registry path: {REGISTRY_PATH!r}"
    )


# --- scanner self-tests: prove the scans are not vacuous ---------------------

_SINGLE = "import pytest\npytestmark = pytest.mark.release_cut\n"
_LIST = (
    "import pytest\n"
    "pytestmark = [pytest.mark.usefixtures('x'), pytest.mark.release_cut]\n"
)
_FUNC_INTEGRATION = (
    "import pytest\n@pytest.mark.integration\ndef test_a() -> None:\n    pass\n"
)
_CLASS_INTEGRATION = (
    "import pytest\n@pytest.mark.integration()\nclass TestA:\n    pass\n"
)
_DOCSTRING_ONLY = (
    "def test_a() -> None:\n"
    '    """pytest.mark.release_cut and pytest.mark.integration."""\n'
)
_COMMENT_ONLY = "# pytest.mark.integration\ndef test_a() -> None:\n    pass\n"
_STRING_ONLY = "NOTE = 'pytest.mark.release_cut'\n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_SINGLE, {MARKER}),
        (_LIST, {MARKER}),
    ],
)
def test_scanner_finds_module_pytestmark_forms(source: str, expected: set[str]) -> None:
    assert _module_pytestmark_names(source) >= expected
    assert _marks_used(source) >= expected


@pytest.mark.parametrize(
    "source",
    [_FUNC_INTEGRATION, _CLASS_INTEGRATION],
    ids=["function-level", "class-level-called"],
)
def test_scanner_finds_integration_at_function_and_class_level(source: str) -> None:
    assert "integration" in _marks_used(source)


@pytest.mark.parametrize("source", [_DOCSTRING_ONLY, _COMMENT_ONLY, _STRING_ONLY])
def test_scanner_misses_docstrings_comments_and_strings(source: str) -> None:
    used = _marks_used(source)
    assert "integration" not in used
    assert MARKER not in used


def test_scanner_reads_marked_files_from_tmp_path(tmp_path: Path) -> None:
    member = tmp_path / "test_member.py"
    member.write_text(_LIST, encoding="utf-8")
    assert MARKER in _module_pytestmark_names(member.read_text(encoding="utf-8"))
