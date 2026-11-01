"""Guard tests: the v4 scope tier is a risk classification, not a size.

Pure-markdown assertions over the auto-dev pipeline instruction files,
following the ``read_text()`` + literal-substring convention of
``test_plan_stage_retier.py``. ``_cmd`` is imported from ``tests.conftest``.

Background: under v1 the Small tier was ``≤10 files AND ≤500 lines AND no
forbidden-area touches``. v4 removes the size ceiling: Small means no
forbidden-area touch at any size, Large means a forbidden area was touched,
and size becomes the advisory ``large_diff`` (over 10 files OR over 500
lines) that selects the Opus impl model and the full reviewer set but never
parks a gate. These tests pin that wording in every stage file so a regression
to size-based tiering in one stage cannot ship while the others say v4.
"""

from tests.conftest import _cmd

STAGE_FILES = (
    "auto-dev.md",
    "auto-dev-plan.md",
    "auto-dev-impl.md",
    "auto-dev-review.md",
    "auto-dev-finalize.md",
)
PRE_V4_BOUNDARY = "≤10 files AND ≤500 lines"
PRE_V4_LARGE = ">10 files OR >500 lines"
LARGE_DIFF_DEFINITION = "over 10 files OR over 500 lines"
NEVER_PARKS = "never changes the tier and never parks a gate"


# ---------------------------------------------------------------------------
# 1. The pre-v4 size boundary is gone from every stage file.
# ---------------------------------------------------------------------------


def test_no_stage_file_states_the_pre_v4_size_boundary() -> None:
    """No stage file still classifies the tier by the v1 file/line ceiling."""
    for name in STAGE_FILES:
        content = _cmd(name)
        assert PRE_V4_BOUNDARY not in content, name
        assert PRE_V4_LARGE not in content, name


# ---------------------------------------------------------------------------
# 2. Step 1d.3 classifies by risk alone and defines the size advisory.
# ---------------------------------------------------------------------------


def test_plan_step1d3_classifies_by_forbidden_area_only() -> None:
    """Step 1d.3's Small/Large lines name the forbidden-area touch, not size."""
    content = _cmd("auto-dev-plan.md")
    assert (
        "- **Small:** no forbidden-area touches, at any file or line count" in content
    )
    assert "- **Large:** touches a forbidden area" in content


def test_plan_step1d2b_is_retired() -> None:
    """The CI/CD content gate is retired from the default forbidden set."""
    content = _cmd("auto-dev-plan.md")
    assert "**Step 1d.2b — retired (v4):**" in content
    assert "**Step 1d.2b — content gate (CI/CD only):**" not in content


def test_plan_size_advisory_logs_large_diff_and_never_parks() -> None:
    """Step 1d records large_diff as friction and states it never gates."""
    content = _cmd("auto-dev-plan.md")
    assert 'append `"large_diff"` to `friction_highlights`' in content
    assert NEVER_PARKS in content


def test_plan_step1g0_boundary_matches_step1d3() -> None:
    """Step 1g.0's recomputation names the same risk-only boundary."""
    content = _cmd("auto-dev-plan.md")
    assert "`no forbidden-area touches` = small; otherwise large" in content


def test_plan_forbidden_flag_tokens_are_defined() -> None:
    """`--forbidden` tokens (used by /auto-debt) resolve to named detectors."""
    content = _cmd("auto-dev-plan.md")
    assert (
        "`ci` → `.github/workflows/**` hunks that touch a pipeline-behavior YAML key"
        in content
    )
    assert "`shared-bases` → shared base classes with 3+ consumers" in content


# ---------------------------------------------------------------------------
# 3. auto-dev.md holds the one definition of large_diff and the v4 table row.
# ---------------------------------------------------------------------------


def test_auto_dev_defines_large_diff_once_with_sources() -> None:
    """The size-advisory paragraph is the definition: threshold plus sources."""
    content = _cmd("auto-dev.md")
    assert "**Size is advisory, not a tier input (v4).**" in content
    assert "This paragraph is the one definition of the threshold" in content
    assert LARGE_DIFF_DEFINITION in content
    assert NEVER_PARKS in content


def test_threshold_numbers_live_only_in_the_definition() -> None:
    """No stage file other than auto-dev.md restates the large_diff numbers."""
    for name in (
        "auto-dev-plan.md",
        "auto-dev-impl.md",
        "auto-dev-review.md",
        "auto-dev-review-appendix.md",
        "auto-dev-finalize.md",
        "auto-debt.md",
    ):
        content = _cmd(name)
        assert "10 files" not in content, name
        assert "500 lines" not in content, name
        assert "N > 10" not in content, name


def test_impl_keeps_opus_for_a_forbidden_area_plan() -> None:
    """A Large (forbidden-area) plan selects Opus however small it is."""
    content = _cmd("auto-dev-impl.md")
    assert "A forbidden-area plan keeps\nOpus however small it is." in content


def test_review_sparse_skip_excludes_large_diff() -> None:
    """The sparse re-review skip never applies to a large_diff diff."""
    content = _cmd("auto-dev-review.md")
    assert "(Small scope only, and never for a `large_diff` diff):" in content


def test_auto_dev_axis2_table_is_risk_only() -> None:
    """The Axis 2 tier table names the forbidden-area touch, not a size."""
    content = _cmd("auto-dev.md")
    assert (
        "| **Small** | no forbidden-area touches — at any file or line count |"
        in content
    )
    assert (
        "| **Large** | touches a forbidden area (`migrations/**`, auth/security core) |"
        in content
    )


def test_auto_dev_evolution_table_marks_v4_current() -> None:
    """The Scope Tier Evolution table has v4 current and v1 superseded."""
    content = _cmd("auto-dev.md")
    assert "| v4 (current) | none — size is advisory" in content
    assert "| v1 (superseded) | 10 files, 500 lines |" in content
    # The scope-conformance table below it keeps its own "v1 (current)" row.
    assert "| v1 (current) | 10 files, 500 lines |" not in content


# ---------------------------------------------------------------------------
# 4. The review stage keys the reviewer set on size but nothing else.
# ---------------------------------------------------------------------------


def test_review_reviewer_set_selection_is_scoped_to_the_two_lists() -> None:
    """large_diff picks the reviewer list only; Small/Large scope stay tier."""
    content = _cmd("auto-dev-review.md")
    assert "**Reviewer-set selection (v4).**" in content
    assert '"Small scope" and "Large scope" mean the tier alone.' in content
    assert "it never parks a gate or asks a person" in content
    # The pinned list headings are untouched (tests/test_auto_dev_model_pins.py).
    assert "**Small scope:** Spawn these reviewers" in content
    assert "**Large scope:** Spawn full reviewer set" in content


def test_review_fallback_rederives_by_forbidden_area_only() -> None:
    """The sentinel tier fallback no longer counts files and lines."""
    content = _cmd("auto-dev-review.md")
    assert "**Small** = no forbidden-area touches; **Large** otherwise" in content


def test_review_downgrade_check_has_no_size_clause() -> None:
    """The Step 1.5 downgrade no longer requires a ≤10 files / ≤500 lines diff."""
    content = _cmd("auto-dev-review.md")
    assert "independently disagrees with the Stage-1 call, THEN:" in content
    assert "otherwise ≤10 files / ≤500 lines" not in content


# ---------------------------------------------------------------------------
# 5. Impl and finalize cite the definition rather than a tier.
# ---------------------------------------------------------------------------


def test_impl_resolves_model_from_diff_size() -> None:
    """Stage 2 resolves the model from the size advisory, not the tier."""
    content = _cmd("auto-dev-impl.md")
    assert "### Resolve the impl model from diff size" in content
    assert "### Resolve the impl model from scope tier" not in content
    assert '"Resolve the impl model from diff size" below' in content


def test_finalize_fallback_rederives_by_forbidden_area_only() -> None:
    """Finalize's sentinel tier fallback matches the review stage."""
    content = _cmd("auto-dev-finalize.md")
    assert "**Small** = no forbidden-area touches; **Large** otherwise" in content


# ---------------------------------------------------------------------------
# 6. /auto-debt no longer claims a size threshold.
# ---------------------------------------------------------------------------


def test_auto_debt_scope_limit_is_risk_only() -> None:
    """The debt alias's --scope-limit rejects forbidden-area tickets only."""
    content = _cmd("auto-debt.md")
    assert "reject tickets classified Large — a forbidden-area touch" in content
    assert "reject tickets >10 files or >500 lines" not in content
