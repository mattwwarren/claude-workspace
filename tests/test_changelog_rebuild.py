"""Tests for cw.changelog_rebuild, the pure CHANGELOG [Unreleased] rebuild (#2771).

The rebuild never merges text: it takes main's CHANGELOG verbatim and inserts
only the branch's own [Unreleased] bullets (tip minus merge-base), failing
closed on anything it cannot place unambiguously.

Fixtures under ``tests/fixtures/changelog/`` are unmodified slices of this
repo's own CHANGELOG.md, cut at section boundaries with ``git show``:

- ``changelog_pre_1_77_2_release.md``: ``git show v1.77.2^:CHANGELOG.md``
  (commit 1dd3a366), lines 1-21, preamble through the end of ``[1.77.1]``.
- ``changelog_at_v1_77_2.md``: ``git show v1.77.2:CHANGELOG.md`` (commit
  e3e28556), lines 1-23, the same cut (through the end of ``[1.77.1]``).
- ``changelog_main_unreleased_with_fixed.md``: ``git show
  db1a452d:CHANGELOG.md``, lines 1-18, preamble through the end of
  ``[1.78.1]``; its ``[Unreleased]`` already holds a ``### Fixed`` and so does
  ``[1.78.1]``.
"""

from __future__ import annotations

import importlib.util
import itertools
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from cw import changelog_rebuild
from cw.changelog_rebuild import (
    ChangelogRebuildError,
    ChangelogRepairDisposition,
    OwnItem,
    parse_headings,
    rebuild_changelog,
    section_text,
)
from cw.reconcile.review_recipes.changelog_repair_git import _diff_fingerprint

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "changelog"
_PRE_1_77_2 = _FIXTURES / "changelog_pre_1_77_2_release.md"
_AT_1_77_2 = _FIXTURES / "changelog_at_v1_77_2.md"
_MAIN_WITH_FIXED = _FIXTURES / "changelog_main_unreleased_with_fixed.md"

_PREAMBLE = "# Changelog\n\n"
_RELEASED_1_0_0 = "## [1.0.0] - 2026-01-01\n\n### Added\n\n- Initial (#0).\n"

BASE = (
    _PREAMBLE
    + "## [Unreleased]\n\n### Added\n\n- Alpha feature (#1).\n\n"
    + "### Fixed\n\n- Beta fix (#2).\n\n"
    + _RELEASED_1_0_0
)
BRANCH = (
    _PREAMBLE
    + "## [Unreleased]\n\n### Added\n\n- Alpha feature (#1).\n\n"
    + "### Fixed\n\n- Beta fix (#2).\n\n- Gamma fix (#3).\n\n"
    + _RELEASED_1_0_0
)
_RELEASED_1_1_0 = (
    "## [1.1.0] - 2026-02-01\n\n### Added\n\n- Alpha feature (#1).\n\n"
    "### Fixed\n\n- Beta fix (#2).\n\n"
)
MAIN = _PREAMBLE + "## [Unreleased]\n\n" + _RELEASED_1_1_0 + _RELEASED_1_0_0
EXPECTED = (
    _PREAMBLE
    + "## [Unreleased]\n\n### Fixed\n\n- Gamma fix (#3).\n\n"
    + _RELEASED_1_1_0
    + _RELEASED_1_0_0
)


def _disposition(base: str, branch: str, main: str) -> ChangelogRepairDisposition:
    with pytest.raises(ChangelogRebuildError) as excinfo:
        rebuild_changelog(base=base, branch=branch, main=main)
    return excinfo.value.disposition


# --- 1. the release-moved-Unreleased case -----------------------------------


def test_release_moved_unreleased_inserts_only_the_branch_bullet() -> None:
    result = rebuild_changelog(base=BASE, branch=BRANCH, main=MAIN)

    assert result.text == EXPECTED
    assert [item.text for item in result.inserted] == ["- Gamma fix (#3)."]
    assert result.skipped_present == ()
    unreleased = result.text.split("## [1.1.0]")[0]
    assert "Alpha" not in unreleased
    assert "Beta" not in unreleased
    assert unreleased.count("### Fixed") == 1
    released = result.text.split("## [1.1.0]")[1].split("## [1.0.0]")[0]
    assert "Gamma" not in released


# --- 2. the ticket's second shape -------------------------------------------


def test_appends_inside_existing_unreleased_subsection() -> None:
    main = (
        _PREAMBLE
        + "## [Unreleased]\n\n### Fixed\n\n- Main-only fix (#9).\n\n"
        + _RELEASED_1_1_0
        + _RELEASED_1_0_0
    )

    result = rebuild_changelog(base=BASE, branch=BRANCH, main=main)

    assert result.text == (
        _PREAMBLE
        + "## [Unreleased]\n\n### Fixed\n\n- Main-only fix (#9).\n\n"
        + "- Gamma fix (#3).\n\n"
        + _RELEASED_1_1_0
        + _RELEASED_1_0_0
    )
    assert result.text.split("## [1.1.0]")[0].count("### Fixed") == 1


def test_creates_exactly_one_subsection_when_main_lacks_it() -> None:
    main = (
        _PREAMBLE
        + "## [Unreleased]\n\n### Added\n\n- Main add (#8).\n\n"
        + _RELEASED_1_1_0
        + _RELEASED_1_0_0
    )

    result = rebuild_changelog(base=BASE, branch=BRANCH, main=main)

    unreleased = result.text.split("## [1.1.0]")[0]
    assert unreleased == (
        _PREAMBLE
        + "## [Unreleased]\n\n### Added\n\n- Main add (#8).\n\n"
        + "### Fixed\n\n- Gamma fix (#3).\n\n"
    )


def test_main_fixture_with_unreleased_fixed_appends_into_it() -> None:
    main = _MAIN_WITH_FIXED.read_text(encoding="utf-8")
    base = main
    lines = main.split("\n")
    bullet_at = lines.index("### Fixed") + 2
    branch_lines = [
        *lines[: bullet_at + 1],
        "",
        "- Own fix (#4).",
        *lines[bullet_at + 1 :],
    ]
    branch = "\n".join(branch_lines)

    result = rebuild_changelog(base=base, branch=branch, main=main)

    assert result.text == branch
    released = result.text.split("## [1.78.1]")[1]
    assert released == main.split("## [1.78.1]")[1]


# --- 3. duplicate-bullet idempotence ----------------------------------------


def test_own_bullet_already_in_main_unreleased_is_skipped() -> None:
    main = EXPECTED

    result = rebuild_changelog(base=BASE, branch=BRANCH, main=main)

    assert result.text == main
    assert result.inserted == ()
    assert [item.text for item in result.skipped_present] == ["- Gamma fix (#3)."]


def test_own_bullet_already_released_is_skipped() -> None:
    main = (
        _PREAMBLE
        + "## [Unreleased]\n\n## [1.1.0] - 2026-02-01\n\n### Fixed\n\n"
        + "- Gamma   fix (#3).\n\n"
        + _RELEASED_1_0_0
    )

    result = rebuild_changelog(base=BASE, branch=BRANCH, main=main)

    assert result.text == main
    assert len(result.skipped_present) == 1


def test_rerun_on_result_is_a_no_op() -> None:
    first = rebuild_changelog(base=BASE, branch=BRANCH, main=MAIN)

    second = rebuild_changelog(base=BASE, branch=BRANCH, main=first.text)

    assert second.text == first.text
    assert second.inserted == ()


# --- 4-7. fail-closed checks ------------------------------------------------


def test_released_section_touched_in_tip() -> None:
    branch = BRANCH.replace("- Initial (#0).", "- Initial, edited (#0).")

    assert (
        _disposition(BASE, branch, MAIN)
        == ChangelogRepairDisposition.CHANGELOG_RELEASED_SECTION_TOUCHED
    )


def test_preamble_touched_in_tip() -> None:
    branch = BRANCH.replace("# Changelog", "# Change log")

    assert (
        _disposition(BASE, branch, MAIN)
        == ChangelogRepairDisposition.CHANGELOG_RELEASED_SECTION_TOUCHED
    )


def test_tip_edits_a_base_bullet() -> None:
    branch = BRANCH.replace("- Beta fix (#2).", "- Beta fix, reworded (#2).")

    assert _disposition(BASE, branch, MAIN) == (
        ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS
    )


def test_tip_removes_base_prose() -> None:
    base = BASE.replace("### Added\n\n", "### Added\n\nSome prose.\n\n", 1)

    assert _disposition(base, BRANCH, MAIN) == (
        ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS
    )


def test_tip_adds_a_version_heading() -> None:
    branch = BRANCH.replace(
        "## [1.0.0]", "## [1.0.1] - 2026-01-02\n\n- Sneaky (#5).\n\n## [1.0.0]"
    )

    assert _disposition(BASE, branch, MAIN) == (
        ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS
    )


def test_tip_duplicates_a_heading() -> None:
    branch = BRANCH + "\n## [1.0.0] - 2026-01-01\n"

    assert _disposition(BASE, branch, MAIN) == (
        ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS
    )


def test_own_bullet_outside_any_subsection() -> None:
    branch = BRANCH.replace(
        "## [Unreleased]\n\n", "## [Unreleased]\n\n- Loose (#6).\n\n"
    )

    assert _disposition(BASE, branch, MAIN) == (
        ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS
    )


def test_own_bullet_added_twice() -> None:
    branch = BRANCH.replace(
        "- Gamma fix (#3).\n", "- Gamma fix (#3).\n\n- Gamma fix (#3).\n"
    )

    assert _disposition(BASE, branch, MAIN) == (
        ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS
    )


@pytest.mark.parametrize(
    ("base", "branch", "main"),
    [
        pytest.param(
            BASE.replace("## [Unreleased]", "## [Pending]"),
            BRANCH,
            MAIN,
            id="missing-unreleased",
        ),
        pytest.param(BASE, BRANCH, MAIN + "\n## [Unreleased]\n", id="two-unreleased"),
        pytest.param("", BRANCH, MAIN, id="empty-base"),
        pytest.param(BASE, "  \n", MAIN, id="blank-branch"),
        pytest.param(BASE, BRANCH, "", id="empty-main"),
        pytest.param(
            BASE,
            BRANCH.replace("- Gamma fix (#3).", "Gamma prose line."),
            MAIN,
            id="prose-in-added-area",
        ),
        pytest.param(
            BASE,
            BRANCH.replace("\n", "\r\n", 3),
            MAIN,
            id="mixed-crlf",
        ),
        pytest.param(BASE, BRANCH, MAIN.replace("\n", "\r", 1), id="bare-cr"),
    ],
)
def test_unparseable_inputs(base: str, branch: str, main: str) -> None:
    assert _disposition(base, branch, main) == (
        ChangelogRepairDisposition.CHANGELOG_UNPARSEABLE
    )


def test_error_carries_disposition_and_detail() -> None:
    with pytest.raises(ChangelogRebuildError) as excinfo:
        rebuild_changelog(base="", branch=BRANCH, main=MAIN)

    assert excinfo.value.detail
    assert str(excinfo.value.disposition) in str(excinfo.value)


def test_crlf_main_keeps_its_newline_style() -> None:
    main = MAIN.replace("\n", "\r\n")

    result = rebuild_changelog(base=BASE, branch=BRANCH, main=main)

    assert result.text == EXPECTED.replace("\n", "\r\n")


def test_main_without_trailing_newline_keeps_it() -> None:
    result = rebuild_changelog(base=BASE, branch=BRANCH, main=MAIN.rstrip("\n"))

    assert result.text == EXPECTED.rstrip("\n")


# --- 8. canonical ordering --------------------------------------------------


def test_new_subsections_follow_keep_a_changelog_order() -> None:
    main = (
        _PREAMBLE
        + "## [Unreleased]\n\n### Changed\n\n- Main change (#7).\n\n"
        + _RELEASED_1_0_0
    )
    base = _PREAMBLE + "## [Unreleased]\n\n" + _RELEASED_1_0_0
    branch = (
        _PREAMBLE
        + "## [Unreleased]\n\n### Security\n\n- Sec (#11).\n\n"
        + "### Added\n\n- New thing (#10).\n\n### Weird\n\n- Odd (#12).\n\n"
        + _RELEASED_1_0_0
    )

    result = rebuild_changelog(base=base, branch=branch, main=main)

    assert result.text == (
        _PREAMBLE
        + "## [Unreleased]\n\n### Added\n\n- New thing (#10).\n\n"
        + "### Changed\n\n- Main change (#7).\n\n"
        + "### Security\n\n- Sec (#11).\n\n### Weird\n\n- Odd (#12).\n\n"
        + _RELEASED_1_0_0
    )


def test_unreleased_as_last_section_without_trailing_blank() -> None:
    base = _PREAMBLE + "## [Unreleased]\n"
    branch = _PREAMBLE + "## [Unreleased]\n\n### Added\n\n- Thing (#1).\n"
    main = _PREAMBLE + "## [Unreleased]\n"

    result = rebuild_changelog(base=base, branch=branch, main=main)

    assert result.text == branch


def test_unreleased_directly_followed_by_heading() -> None:
    main = _PREAMBLE + "## [Unreleased]\n" + _RELEASED_1_0_0
    base = _PREAMBLE + "## [Unreleased]\n\n" + _RELEASED_1_0_0
    branch = (
        _PREAMBLE
        + "## [Unreleased]\n\n### Added\n\n- Thing (#1).\n\n"
        + (_RELEASED_1_0_0)
    )

    result = rebuild_changelog(base=base, branch=branch, main=main)

    assert (
        result.text
        == (_PREAMBLE + "## [Unreleased]\n\n### Added\n\n- Thing (#1).\n\n")
        + _RELEASED_1_0_0
    )


# --- 9. multi-line continuation ---------------------------------------------


def test_multi_line_bullet_continuation_is_preserved() -> None:
    own = "- Multi line (#13).\n  continued here.\n\n  and a second paragraph."
    branch = BRANCH.replace("- Gamma fix (#3).", own)

    result = rebuild_changelog(base=BASE, branch=branch, main=MAIN)

    assert own in result.text
    assert [item.text for item in result.inserted] == [own]


# --- 10. empty own set ------------------------------------------------------


def test_empty_own_set_returns_main_unchanged() -> None:
    main = MAIN.replace("\n", "\r\n")

    result = rebuild_changelog(base=BASE, branch=BASE, main=main)

    assert result.text is main
    assert result.inserted == ()


# --- 11. additive property --------------------------------------------------


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(line in it for line in needle)


_MAIN_BLOCKS = {
    "Added": "### Added\n\n- Main A (#20).\n\n",
    "Changed": "### Changed\n\n- Main C (#21).\n\n- Main C2 (#22).\n\n",
    "Fixed": "### Fixed\n\n- Main F (#23).\n\n",
}
_OWN_BLOCKS = {
    "Added": "### Added\n\n- Own A (#30).\n\n",
    "Fixed": "### Fixed\n\n- Own F (#31).\n\n",
    "Removed": "### Removed\n\n- Own R (#32).\n\n",
}


@pytest.mark.parametrize(
    ("main_subs", "own_subs"),
    [
        (main_subs, own_subs)
        for main_subs in itertools.chain.from_iterable(
            itertools.combinations(_MAIN_BLOCKS, n) for n in range(4)
        )
        for own_subs in (("Added",), ("Fixed", "Removed"), tuple(_OWN_BLOCKS))
    ],
)
def test_result_is_additive_over_main(
    main_subs: tuple[str, ...], own_subs: tuple[str, ...]
) -> None:
    main = (
        _PREAMBLE
        + "## [Unreleased]\n\n"
        + "".join(_MAIN_BLOCKS[s] for s in main_subs)
        + _RELEASED_1_0_0
    )
    base = _PREAMBLE + "## [Unreleased]\n\n" + _RELEASED_1_0_0
    branch = (
        _PREAMBLE
        + "## [Unreleased]\n\n"
        + "".join(_OWN_BLOCKS[s] for s in own_subs)
        + _RELEASED_1_0_0
    )

    result = rebuild_changelog(base=base, branch=branch, main=main)

    main_lines = [line for line in main.split("\n") if line.strip()]
    result_lines = [line for line in result.text.split("\n") if line.strip()]
    assert _is_subsequence(main_lines, result_lines)
    assert len(result.inserted) == len(own_subs)
    unreleased = result.text.split("## [1.0.0]")[0]
    headings = [line for line in unreleased.split("\n") if line.startswith("### ")]
    assert len(headings) == len(set(headings))
    assert rebuild_changelog(base=base, branch=branch, main=result.text).text == (
        result.text
    )


# --- 12. parity with the frozen-section script ------------------------------


def _load_frozen_script() -> ModuleType:
    path = _REPO_ROOT / ".claude" / "scripts" / "check_changelog_frozen.py"
    spec = importlib.util.spec_from_file_location("_cl_frozen_parity", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "path",
    [_REPO_ROOT / "CHANGELOG.md", _PRE_1_77_2, _AT_1_77_2, _MAIN_WITH_FIXED],
    ids=lambda p: p.name,
)
def test_parse_parity_with_check_changelog_frozen(path: Path) -> None:
    frozen = _load_frozen_script()
    lines = path.read_text(encoding="utf-8").splitlines()

    ours = parse_headings(lines)
    theirs = frozen.parse_headings(lines)

    assert [(h.version, h.line) for h in ours] == [(h.version, h.line) for h in theirs]
    for heading in ours:
        assert section_text(lines, heading.line) == frozen.section_text(
            lines, heading.line
        )


# --- 13. fixture-driven real shape ------------------------------------------


def test_real_release_shape_from_fixtures() -> None:
    base = _PRE_1_77_2.read_text(encoding="utf-8")
    main = _AT_1_77_2.read_text(encoding="utf-8")
    own = "- **A branch-only fix (#9999).** Lands after the 1.77.2 release."
    lines = base.split("\n")
    last_bullet = max(
        i
        for i, line in enumerate(lines[: lines.index("## [1.77.1] - 2026-10-19")])
        if line.startswith("- ")
    )
    branch = "\n".join([*lines[: last_bullet + 1], own, *lines[last_bullet + 1 :]])

    result = rebuild_changelog(base=base, branch=branch, main=main)

    main_lines = main.split("\n")
    result_lines = result.text.split("\n")
    released_at = main_lines.index("## [1.77.2] - 2026-10-25")
    assert result_lines[-(len(main_lines) - released_at) :] == main_lines[released_at:]
    assert result.text.count(own) == 1
    unreleased = result.text.split("## [1.77.2]")[0]
    assert own in unreleased
    assert unreleased.count("### Fixed") == 1


# --- 14. _diff_fingerprint --------------------------------------------------


_DIFF = (
    "diff --git a/src/x.py b/src/x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/x.py\n"
    "+++ b/src/x.py\n"
    "@@ -10,0 +11,2 @@ def f():\n"
    "+    a = 1\n"
    "+    b = 2\n"
)


def test_fingerprint_ignores_line_number_shifts() -> None:
    shifted = _DIFF.replace("@@ -10,0 +11,2 @@ def f():", "@@ -40,0 +41,2 @@ def g():")

    assert _diff_fingerprint(shifted) == _diff_fingerprint(_DIFF)


def test_fingerprint_ignores_index_lines() -> None:
    reindexed = _DIFF.replace("index 1111111..2222222", "index 3333333..4444444")

    assert _diff_fingerprint(reindexed) == _diff_fingerprint(_DIFF)


def test_fingerprint_detects_an_edited_line() -> None:
    edited = _DIFF.replace("+    b = 2", "+    b = 3")

    assert _diff_fingerprint(edited) != _diff_fingerprint(_DIFF)


def test_fingerprint_of_empty_diff_is_stable() -> None:
    assert _diff_fingerprint("") == _diff_fingerprint("")
    assert _diff_fingerprint("") != _diff_fingerprint(_DIFF)


# --- self-check: each postcondition guard trips on a corrupted insert ----------

_MAIN_WITH_OWN_FIXED = (
    _PREAMBLE
    + "## [Unreleased]\n\n### Fixed\n\n- Main-only fix (#9).\n\n"
    + _RELEASED_1_1_0
    + _RELEASED_1_0_0
)


def _corrupt_insert(
    monkeypatch: pytest.MonkeyPatch, corrupt: Callable[[list[str], OwnItem], None]
) -> None:
    """Make ``_insert_item`` do the real insert, then *corrupt* the body."""
    real = changelog_rebuild._insert_item

    def fake(body: list[str], item: OwnItem) -> None:
        real(body, item)
        corrupt(body, item)

    monkeypatch.setattr(changelog_rebuild, "_insert_item", fake)


def _assert_ambiguous(excinfo: pytest.ExceptionInfo[ChangelogRebuildError]) -> str:
    assert excinfo.value.disposition is ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS
    return excinfo.value.detail


def test_self_check_trips_when_a_released_section_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _corrupt_insert(
        monkeypatch,
        lambda body, _item: body.extend(["## [9.9.9] - 2026-03-01", "", "- Bogus."]),
    )

    with pytest.raises(ChangelogRebuildError) as excinfo:
        rebuild_changelog(base=BASE, branch=BRANCH, main=MAIN)

    assert "preamble or a released section changed" in _assert_ambiguous(excinfo)


def test_self_check_trips_when_the_result_is_not_additive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def drop_main_bullet(body: list[str], _item: OwnItem) -> None:
        body[:] = [line for line in body if "Main-only" not in line]

    _corrupt_insert(monkeypatch, drop_main_bullet)

    with pytest.raises(ChangelogRebuildError) as excinfo:
        rebuild_changelog(base=BASE, branch=BRANCH, main=_MAIN_WITH_OWN_FIXED)

    detail = _assert_ambiguous(excinfo)
    assert "result is not additive over main" in detail
    assert "preamble or a released section changed" not in detail


@pytest.mark.parametrize(
    ("corrupt", "count"),
    [
        pytest.param(
            lambda body, _item: body.__setitem__(
                slice(None), [line for line in body if "Gamma" not in line]
            ),
            0,
            id="dropped",
        ),
        pytest.param(
            lambda body, _item: body.extend(["", "- Gamma fix (#3)."]),
            2,
            id="doubled",
        ),
    ],
)
def test_self_check_trips_when_an_inserted_bullet_is_not_unique(
    monkeypatch: pytest.MonkeyPatch,
    corrupt: Callable[[list[str], OwnItem], None],
    count: int,
) -> None:
    _corrupt_insert(monkeypatch, corrupt)

    with pytest.raises(ChangelogRebuildError) as excinfo:
        rebuild_changelog(base=BASE, branch=BRANCH, main=MAIN)

    detail = _assert_ambiguous(excinfo)
    assert f"inserted bullet occurs {count} times: " in detail
    assert "Gamma fix (#3)." in detail


def test_self_check_trips_on_a_duplicate_subsection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _corrupt_insert(
        monkeypatch,
        lambda body, _item: body.extend(["", "### Fixed", "", "- Other fix (#8)."]),
    )

    with pytest.raises(ChangelogRebuildError) as excinfo:
        rebuild_changelog(base=BASE, branch=BRANCH, main=MAIN)

    assert "duplicate subsection under [Unreleased]" in _assert_ambiguous(excinfo)


def test_self_check_trips_on_a_duplicate_version_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _corrupt_insert(
        monkeypatch,
        lambda body, _item: body.extend(["## [1.0.0] - 2026-01-01", "", "- Again."]),
    )

    with pytest.raises(ChangelogRebuildError) as excinfo:
        rebuild_changelog(base=BASE, branch=BRANCH, main=MAIN)

    assert "duplicate '## [..]' heading" in _assert_ambiguous(excinfo)


def test_a_non_idempotent_rebuild_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = changelog_rebuild._rebuild
    calls: list[str] = []

    def drifting(base: str, branch: str, main: str) -> changelog_rebuild.RebuildResult:
        result = real(base, branch, main)
        if calls:
            return result._replace(text=result.text + "drift\n")
        calls.append(main)
        return result

    monkeypatch.setattr(changelog_rebuild, "_rebuild", drifting)

    with pytest.raises(ChangelogRebuildError) as excinfo:
        rebuild_changelog(base=BASE, branch=BRANCH, main=MAIN)

    assert "rebuild is not idempotent over its own result" in _assert_ambiguous(excinfo)
    assert len(calls) == 1
