"""Guard tests: sentinel review counters actually reach the sentinel (#2098).

Pure-markdown assertions over the auto-dev pipeline instruction files. Mirrors
the ``read_text()`` + literal-substring/window convention of
``test_plan_persistence.py`` / ``test_auto_dev_preflight_resolutions.py``.
``_cmd`` is imported from ``tests.conftest`` (#1787); ``_after``/``_nearby``
are imported from ``test_auto_dev_preflight_resolutions`` rather than
duplicated, since that file already defines and exports them.

Background: two related producer gaps closed together.

Case 1 — ``review.rejected_count``/``review.rejected_count_by_severity``
(#2000) were added to the ``Review`` schema model but never landed in
``auto-dev-review.md``'s Freeze rule or its Stage 3 Completion sentinel
template, so every real producer omitted them and a defaulted ``0`` read as
"nothing rejected" even on a round with real mechanically-rejected findings.
Both prompt copies now name the two fields.

Case 2 — ``resolution_consumed``/``resolution_evidence`` (#1896) are correctly
scoped to Step 1c.0 settlement only; the fix here is a clarifying clause, not
a widening — a Step 1b ``## Binding Pre-flight Resolutions`` merge must not be
read as a settlement, since ``cw.dispatch.productivity`` uses this field as
one of three OR'd anti-gaming productivity signals.
"""

from tests.conftest import _cmd
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

FREEZE_ANCHOR = "**Freeze** `.review.must_fix_initial`"
RESOLUTION_RULE_ANCHOR = (
    "**`resolution_consumed`/`resolution_evidence` emission rule (#1896).**"
)


def _checkpoint_3a_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("### Checkpoint 3a: Adjudicate every finding")
    end = content.index("### Step 3b:")
    return content[start:end]


def _stage3_completion_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("## Stage 3 Completion (headless only)")
    return content[start:]


def test_freeze_rule_names_all_five_review_fields() -> None:
    """The Freeze rule names must_fix_initial/should_fix/agents_run AND the
    two #2000 rejected-count fields — not just the original three (#2098).
    """
    section = _checkpoint_3a_section()
    window = _after(section, FREEZE_ANCHOR, span=500)
    assert ".review.must_fix_initial" in window
    assert ".review.should_fix" in window
    assert ".review.agents_run" in window
    assert ".review.rejected_count" in window
    assert ".review.rejected_count_by_severity" in window


def test_freeze_rule_forbids_hand_computing_or_omitting_rejected_count() -> None:
    """The rejected-count fields must be copied verbatim, never hand-derived
    or silently dropped — a hand-written 0 is a false clean signal (#2098).
    """
    section = _checkpoint_3a_section()
    window = _after(section, FREEZE_ANCHOR, span=900)
    assert "never hand-computed and never omitted" in window
    assert "not-reported" in window


def test_sentinel_template_carries_rejected_count_keys() -> None:
    """Stage 3 Completion's sentinel template carries both new keys as null
    placeholders (#2098), alongside the pre-existing review fields.
    """
    section = _stage3_completion_section()
    assert '"rejected_count": null' in section
    assert '"rejected_count_by_severity": null' in section
    # Still alongside the fields the Freeze rule has always frozen.
    window = _nearby(section, '"rejected_count": null', span=200)
    assert '"agents_run"' in window
    assert '"must_fix_initial"' in window


def test_resolution_rule_scopes_to_step_1c0_settlement_only() -> None:
    """The #1896 emission rule now names the Step 1c.0-only scope and
    excludes a Step 1b pre-flight-resolutions merge from ever emitting
    resolution_consumed (#2098) — clarification, not a widening.
    """
    content = _cmd("auto-dev-plan.md")
    window = _after(content, RESOLUTION_RULE_ANCHOR, span=1500)
    assert "Scoped to Step 1c.0 settlement only" in window
    assert "Binding Pre-flight Resolutions" in window
    assert "NOT a settlement" in window
    assert "never emits these keys" in window


def test_resolution_rule_names_pre_flight_resolution_conformance_as_trace() -> None:
    """A Step 1b merge's trace is documented as the plan's own conformance
    section and friction_highlights — not resolution_consumed (#2098).
    """
    content = _cmd("auto-dev-plan.md")
    window = _after(content, RESOLUTION_RULE_ANCHOR, span=1500)
    assert "Pre-flight Resolution Conformance" in window
    assert "friction_highlights" in window
