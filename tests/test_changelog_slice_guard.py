"""No test slices ``CHANGELOG.md`` at a release boundary (#3157).

A pin that finds its entry by slicing ``## [Unreleased]`` (or a version-bounded
window such as ``"\\n## [1.82.0]"``) goes vacuous or breaks at the next release
cut, because the entry moves into a version section. The shared readers in
``tests/_shared/workflow_text.py`` (``changelog_text`` / ``changelog_entry``)
search the whole file instead. This guard stops a new slicer from appearing.

Detection is literal, per string constant: every ``str`` ``ast.Constant`` under
``tests/`` that matches the release-boundary anchor is flagged. Literal rather
than call-shape matching, because ``HEADER = "## [1.104.0]"; text.index(HEADER)``
evades a call-shape scan. Docstrings are skipped, so prose about the convention
does not trip it. Comments never reach the AST.

Files whose synthetic changelog bytes are the subject of the test (release-cut
fixtures, the freeze gate, release-format writers) are allowlisted with a
reason, and an allowlist entry that stops matching fails the guard, so the list
cannot rot into a blanket exemption.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests._shared.workflow_text import _REPO_ROOT

# A release-boundary anchor: an ``[Unreleased]`` heading, a version heading
# (``## [1.82.0]``), or the bare next-heading boundary ``"\n## ["``.
_ANCHOR = re.compile(r"## \[(?:Unreleased|\d|$)")

_ALLOWLIST: dict[str, str] = {
    "tests/_changelog_cut_helpers.py": "synthetic release-cut changelog fixtures",
    "tests/test_changelog_rebuild.py": "changelog rebuild format tests",
    "tests/test_changelog_sync.py": "slices a synthetic post-cut changelog",
    "tests/test_check_changelog_frozen.py": "the freeze gate's own subject",
    "tests/test_mechanical_repair_real_git.py": "synthetic repair changelog bytes",
    "tests/test_repair_remap.py": "synthetic repair changelog bytes",
    "tests/test_merge_probe.py": "synthetic changelog bytes for merge probe",
    "tests/test_review_convergence_merge_tolerance.py": "synthetic merge bytes",
    "tests/test_reconcile_changelog_repair.py": "changelog-repair recipe tests",
    "tests/test_release_tag_workflow.py": "quotes release-tag.yml's warning text",
    "tests/test_changelog_slice_guard.py": "self-test snippets for this guard",
    "tests/test_changelog_text.py": "release-cut simulation builder",
}


def _anchor_lines(source: str) -> list[int]:
    """Line numbers of every release-boundary string constant in *source*."""
    tree = ast.parse(source)
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _ANCHOR.search(node.value) is not None
        }
    )


def _offenders(root: Path, allowlist: Mapping[str, str]) -> list[str]:
    """``relpath:line`` for every flagged constant under *root*, minus the allowlist."""
    found: list[str] = []
    for path in sorted((root / "tests").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in allowlist:
            continue
        source = path.read_text(encoding="utf-8")
        found.extend(f"{rel}:{line}" for line in _anchor_lines(source))
    return found


def _stale_entries(root: Path, allowlist: Mapping[str, str]) -> list[str]:
    """Allowlist entries whose file is gone or no longer carries an anchor."""
    stale: list[str] = []
    for rel in allowlist:
        path = root / rel
        if not path.is_file():
            stale.append(f"{rel}: missing")
        elif not _anchor_lines(path.read_text(encoding="utf-8")):
            stale.append(f"{rel}: no release-boundary anchor left")
    return stale


def _failure_message(offenders: list[str]) -> str:
    listed = "\n".join(f"  {entry}" for entry in offenders)
    return (
        "test slices CHANGELOG.md at a release boundary:\n"
        f"{listed}\n"
        "Use changelog_text() / changelog_entry(needle) from "
        "tests/_shared/workflow_text.py and search the whole file: the entry "
        "moves into a version section at release. A synthetic-changelog "
        "release-format test belongs in the allowlist with a reason."
    )


def _write(root: Path, rel: str, code: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")


def test_no_test_slices_the_changelog_at_a_release_boundary() -> None:
    offenders = _offenders(_REPO_ROOT, _ALLOWLIST)
    assert not offenders, _failure_message(offenders)


def test_allowlist_entries_are_live() -> None:
    """A stale entry fails here; also proves the scan is not vacuous."""
    assert _stale_entries(_REPO_ROOT, _ALLOWLIST) == []


_FLAGGED = [
    'text[text.index("## [Unreleased]") :]',
    'c[: c.index("\\n## [")]',
    'c.index("## [1.82.0]")',
    'H = "## [1.104.0]"\nassert H in text',
    'v = "9.9.9"\nf"## [{v}]"',
    'c.split("## [Unreleased]")',
]


@pytest.mark.parametrize("code", _FLAGGED)
def test_collector_flags_each_slicer_form(tmp_path: Path, code: str) -> None:
    _write(tmp_path, "tests/test_probe.py", f"x = 1\n{code}\n")
    assert _offenders(tmp_path, {}) != []


_IGNORED = [
    '"""Mentions ## [Unreleased] in a docstring."""\n',
    "# the ## [Unreleased] heading is discussed here\n",
    'x = "### Added"\n',
    'x = "[Unreleased] entry"\n',
    'x = "`## [X.Y.Z]`"\n',
    "x = changelog_text()\n",
]


@pytest.mark.parametrize("code", _IGNORED)
def test_collector_ignores_prose_and_docstrings(tmp_path: Path, code: str) -> None:
    _write(tmp_path, "tests/test_probe.py", code)
    assert _offenders(tmp_path, {}) == []


def test_allowlisted_file_is_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_synth.py", 'x = "## [Unreleased]"\n')
    assert _offenders(tmp_path, {}) != []
    assert _offenders(tmp_path, {"tests/test_synth.py": "synthetic fixture"}) == []


def test_stale_entry_is_reported_for_a_missing_file(tmp_path: Path) -> None:
    stale = _stale_entries(tmp_path, {"tests/test_gone.py": "removed"})
    assert stale == ["tests/test_gone.py: missing"]


def test_stale_entry_is_reported_for_a_file_without_an_anchor(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_clean.py", "x = 1\n")
    stale = _stale_entries(tmp_path, {"tests/test_clean.py": "once had a slicer"})
    assert stale == ["tests/test_clean.py: no release-boundary anchor left"]


def test_failure_message_points_at_the_helper() -> None:
    message = _failure_message(["tests/test_x.py:3"])
    assert "changelog_text" in message
    assert "changelog_entry" in message
    assert "tests/test_x.py:3" in message
