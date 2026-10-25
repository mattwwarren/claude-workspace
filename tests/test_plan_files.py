"""Tests for cw.plan_files — plan ``## Files Modified`` parsing (#1905).

Mirrors test_check_plan_scope_conformance.py's parsing-test shape: both
exercise the same ``## Files Modified`` contract, one via the standalone
.claude/scripts mirror, the other via the in-package src/cw.plan_files copy
that local_runner/executor consume. The shared fixture builder ``_plan_text``
lives in tests/conftest.py so the two suites cannot drift apart.
"""

from __future__ import annotations

from cw.plan_files import (
    SMALL_TIER_MAX_FILES,
    SMALL_TIER_MAX_LINES,
    _looks_like_path,
    parse_plan_files_modified,
    parse_plan_scope_tier,
    resolve_review_scope_tier,
)
from tests.conftest import _plan_text


def test_parses_bullet_paths_from_files_modified_section() -> None:
    """Bullets under the heading yield their path token, in document order."""
    plan = "## Files Modified\n- a/b.py (~10 lines)\n- c.py\n"
    assert parse_plan_files_modified(plan) == ["a/b.py", "c.py"]


def test_parses_table_rows_under_files_modified_heading() -> None:
    """Markdown-table rows yield their first cell's path token (#1779)."""
    plan = (
        "## Files Modified\n\n"
        "| File | Change |\n"
        "|---|---|\n"
        "| src/cw/thing.py | edit |\n"
        "| tests/test_thing.py | new |\n"
    )
    assert parse_plan_files_modified(plan) == [
        "src/cw/thing.py",
        "tests/test_thing.py",
    ]


def test_missing_heading_returns_empty_list() -> None:
    """No ``## Files`` heading → [] (not an error) — the fallback contract."""
    assert parse_plan_files_modified("# Plan\n\njust do the thing\n") == []


def test_ignores_prose_bullets_without_path_markers() -> None:
    """A prose bullet with no path marker is not counted as a file."""
    plan = "## Files Modified\n- Note that nothing else is touched\n- a/b.py\n"
    assert parse_plan_files_modified(plan) == ["a/b.py"]


def test_strips_backticks_and_bold_from_bullet_paths() -> None:
    """Backtick and bold decoration is stripped off the path token."""
    assert parse_plan_files_modified("## Files Modified\n- `a/b.py`\n") == ["a/b.py"]
    assert parse_plan_files_modified("## Files Modified\n- **a/b.py**\n") == ["a/b.py"]


def test_heading_prefix_match_ignores_exact_wording() -> None:
    """Heading is matched by prefix, so the real #1784 variant still parses."""
    plan = "## Files touched, with estimated line deltas\n- src/cw/x.py (~5)\n"
    assert parse_plan_files_modified(plan) == ["src/cw/x.py"]


def test_stops_at_next_heading() -> None:
    """Bullets under a later section are outside the files-modified body."""
    plan = "## Files Modified\n- a/b.py\n\n## Testing\n- tests/other.py\n"
    assert parse_plan_files_modified(plan) == ["a/b.py"]


def test_deduplicates_paths_preserving_first_occurrence_order() -> None:
    """Repeated paths collapse to the first occurrence, order preserved."""
    plan = "## Files Modified\n- b.py\n- a/b.py\n- b.py\n"
    assert parse_plan_files_modified(plan) == ["b.py", "a/b.py"]


def test_bare_dot_and_slash_bullets_are_not_paths() -> None:
    """A bullet carrying only ``.``/``..``/``/`` is decoration, not a file."""
    plan = "## Files Modified\n- .\n- ..\n- /\n- a/b.py\n"
    assert parse_plan_files_modified(plan) == ["a/b.py"]


def test_empty_bullet_body_is_skipped() -> None:
    """A bullet whose body is empty after decoration-stripping yields nothing."""
    plan = "## Files Modified\n- *\n- \n- a/b.py\n"
    assert parse_plan_files_modified(plan) == ["a/b.py"]


def test_ignores_files_modified_section_illustrated_inside_a_code_fence() -> None:
    """A fenced illustration ahead of the real section is not the manifest.

    This is the false positive that blocked #1905's first impl pass: the plan
    doc fenced an example ``## Files Modified`` block, and the fence-unaware
    matcher parsed the example's fake path as the whole manifest. The mirrored
    gate script now has this same fence-awareness — see
    tests/test_check_plan_scope_conformance.py's
    ``test_files_modified_heading_inside_fenced_example_is_ignored`` (#1917).
    """
    plan = (
        "# Plan\n\n"
        "## Patterns Found\n\n"
        "```\n"
        "## Files Modified\n"
        "- src/fake_illustration.py\n"
        "```\n\n"
        "## Files Modified\n"
        "- src/real_target.py\n"
    )
    assert parse_plan_files_modified(plan) == ["src/real_target.py"]


def test_skips_fenced_bullets_inside_the_files_modified_body() -> None:
    """A fence opened and closed mid-section hides only its own bullets."""
    plan = (
        "## Files Modified\n"
        "- a/before.py\n"
        "```python\n"
        "- a/fenced.py\n"
        "```\n"
        "- a/after.py\n"
    )
    assert parse_plan_files_modified(plan) == ["a/before.py", "a/after.py"]


def test_tilde_fence_is_also_skipped() -> None:
    """A ``~~~``-fenced example is ignored, same as a backtick fence (#1917).

    Mirrors test_check_plan_scope_conformance.py's sibling test.
    """
    plan = (
        "# Plan\n\n"
        "~~~\n"
        "## Files Modified\n"
        "- src/fake_illustration.py\n"
        "~~~\n\n"
        "## Files Modified\n"
        "- src/real_target.py\n"
    )
    assert parse_plan_files_modified(plan) == ["src/real_target.py"]


def test_indented_fence_marker_still_toggles() -> None:
    """A fence marker indented under a list item still opens/closes the fence.

    Mirrors test_check_plan_scope_conformance.py's sibling test.
    """
    plan = (
        "## Files Modified\n"
        "- a/before.py\n"
        "- example:\n"
        "  ```\n"
        "  ## Files Modified\n"
        "  - a/fake.py\n"
        "  ```\n"
        "- a/after.py\n"
    )
    assert parse_plan_files_modified(plan) == ["a/before.py", "a/after.py"]


def test_looks_like_path_rejects_tokens_containing_whitespace() -> None:
    """Defensive guard inherited from the mirrored script: a token with inner
    whitespace is never a path, even though the bullet/table split above cannot
    produce one today."""
    assert _looks_like_path("a b.py") is False


def test_parses_shared_conftest_plan_fixture() -> None:
    """The shared ``_plan_text`` builder round-trips through the parser.

    Pins that src/cw.plan_files agrees with the .claude/scripts mirror on the
    exact fixture shape test_check_plan_scope_conformance.py asserts against.
    """
    paths = ["src/cw/one.py", "tests/test_one.py"]
    assert parse_plan_files_modified(_plan_text(paths)) == paths


# --- #2767: the plan's `**Scope tier:**` marker and the review tier resolver ---


def test_parse_scope_tier_reads_canonical_marker() -> None:
    """The canonical Stage 1g line yields its tier."""
    assert parse_plan_scope_tier("# Plan\n\n**Scope tier:** large\n") == "large"
    assert parse_plan_scope_tier("**Scope tier:** small\n") == "small"


def test_parse_scope_tier_tolerates_measurement_suffix() -> None:
    """The ``(N files, ~M lines, forbidden_touched=...)`` suffix is ignored."""
    plan = "**Scope tier:** large (31 files, ~2179 lines, forbidden_touched=false)\n"
    assert parse_plan_scope_tier(plan) == "large"


def test_parse_scope_tier_tolerates_downgrade_note() -> None:
    """A Stage 3 downgrade note after the value does not hide the value."""
    plan = "**Scope tier:** small *(downgraded at Stage 3 from large)*\n"
    assert parse_plan_scope_tier(plan) == "small"


def test_parse_scope_tier_is_case_insensitive_and_normalizes() -> None:
    """The marker parser defines its own case rule: it returns lowercase."""
    assert parse_plan_scope_tier("**scope TIER:** Large\n") == "large"


def test_parse_scope_tier_ignores_fenced_illustration() -> None:
    """A marker inside a code fence is an illustration, not the plan's tier."""
    plan = "```\n**Scope tier:** large\n```\n\n**Scope tier:** small\n"
    assert parse_plan_scope_tier(plan) == "small"
    assert parse_plan_scope_tier("~~~\n**Scope tier:** large\n~~~\n") is None


def test_parse_scope_tier_missing_marker_returns_none() -> None:
    """No marker line -> None, and looser phrasings are not parsed."""
    assert parse_plan_scope_tier("# Plan\n\njust do the thing\n") is None
    assert parse_plan_scope_tier("tier: small\n**Scope:** Small\n") is None


def test_parse_scope_tier_garbage_value_returns_none() -> None:
    """A marker with a value other than small/large is not a tier."""
    assert parse_plan_scope_tier("**Scope tier:** medium\n") is None
    assert parse_plan_scope_tier("**Scope tier:** smallish\n") is None


def test_parse_scope_tier_first_match_wins() -> None:
    """The first marker in document order wins over a later one."""
    plan = "**Scope tier:** large\n\nlater:\n\n**Scope tier:** small\n"
    assert parse_plan_scope_tier(plan) == "large"


_LARGE_PLAN = "# Plan\n\n**Scope tier:** large\n"
_SMALL_PLAN = "# Plan\n\n**Scope tier:** small\n"


def test_resolve_marker_beats_conflicting_hint_and_measurement() -> None:
    """Source 1 (the plan marker) wins over the hint and the diff stat."""
    assert resolve_review_scope_tier(_LARGE_PLAN, "small", (1, 1)) == "large"
    assert resolve_review_scope_tier(_SMALL_PLAN, "large", (99, 9999)) == "small"


def test_resolve_hint_used_when_no_marker() -> None:
    """Source 2 (``scope_hint``) applies when the plan carries no marker."""
    assert resolve_review_scope_tier("# Plan\n", "large", (1, 1)) == "large"
    assert resolve_review_scope_tier(None, "small", (99, 9999)) == "small"


def test_resolve_ignores_unrecognized_hint() -> None:
    """A hint outside {small, large} is not a source; the diff decides."""
    assert resolve_review_scope_tier(None, "medium", (1, 1)) == "small"
    assert resolve_review_scope_tier(None, "Large", None) is None


def test_resolve_measured_diff_boundaries() -> None:
    """Source 3: small iff files <= 10 AND lines <= 500, else large."""
    assert SMALL_TIER_MAX_FILES == 10
    assert SMALL_TIER_MAX_LINES == 500
    assert resolve_review_scope_tier(None, None, (10, 500)) == "small"
    assert resolve_review_scope_tier(None, None, (11, 500)) == "large"
    assert resolve_review_scope_tier(None, None, (10, 501)) == "large"


def test_resolve_no_source_returns_none() -> None:
    """All three sources absent -> None (the caller blocks, never small)."""
    assert resolve_review_scope_tier(None, None, None) is None
    assert resolve_review_scope_tier("# Plan, no marker\n", None, None) is None


def test_resolve_none_plan_text_skips_marker_source() -> None:
    """``plan_text=None`` (no ``.cw/plan.md``) falls through to the hint."""
    assert resolve_review_scope_tier(None, "large", None) == "large"
