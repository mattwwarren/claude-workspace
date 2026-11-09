"""Guard tests: sub-MUST_FIX mechanical rejections are rendered, not discarded (#2008).

Pure-markdown assertions over `/auto-dev-review`'s Checkpoint 3a rules and the
review appendix. ``_cmd``/``_appendix`` come from ``tests.conftest`` and
``_after``/``_nearby`` from ``test_auto_dev_preflight_resolutions``; no private
copy of any helper is defined here.

Background: Checkpoint 3a told the coordinating session to discard every
`.rejected` finding whose severity is not MUST_FIX ("log them, do not present
them to the bucket sort"). A mechanically rejected SHOULD_FIX was therefore
never judged and never shown to a human, while the log read "Review clean".
The prompt now counts and renders them (informational only, never bucketed,
never gating).
"""

from tests.conftest import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

OLD_DISCARD_TEXT = "entirely — log them, do not present them to the bucket sort"
REJECTED_MUST_FIX_ANCHOR = (
    "**A `.rejected` finding with `severity: MUST_FIX` may NOT be silently "
    "discarded** (#1714)."
)
CARVE_OUT_ANCHOR = "**The one carve-out — session-transient tree state (#2087).**"
NEW_BULLET_ANCHOR = (
    "**Sub-MUST_FIX rejections are counted and rendered, not discarded (#2008).**"
)
STRIPPED_ANCHOR = "Log `.stripped_escalations`"
FREEZE_ANCHOR = "**Freeze** `.review.must_fix_initial`"
CLEAN_ANCHOR = "**Small scope + NO_ISSUES → AUTO-ACCEPT.**"
DISPOSITION_ANCHOR = "Log the disposition:"
APPENDIX_SUBSECTION = "### Mechanically rejected below MUST_FIX (informational)"

# The two friction tokens this change introduces.
COUNT_LINE_PREFIX = "mechanical_rejections_below_must_fix:"
DETAIL_LINE_PREFIX = "rejected_below_must_fix:"

# Existing readers key on these prefixes (sources: src/cw/dev_queue/gates.py:56,58
# and src/cw/review_history/sentinel.py:35-40). A new token sharing a prefix
# would mis-trigger stale-tip / approval-park handling or parse as a
# review-history round line.
READER_PREFIXES = (
    "review_tip_check: stale",
    "review_approval_park:",
    "review_round_",
)

SEVERITY_ORDER = ("SHOULD_FIX", "DEBT", "NIT", "PRINCIPLE", "unknown")


def _review_doc() -> str:
    return _cmd("auto-dev-review.md")


def _bullet_text() -> str:
    """The whole new bullet: from its anchor to the next sibling bullet."""
    doc = _review_doc()
    start = doc.index(NEW_BULLET_ANCHOR)
    return doc[start : doc.index(STRIPPED_ANCHOR, start)]


def test_old_discard_instruction_is_gone() -> None:
    assert OLD_DISCARD_TEXT not in _review_doc()
    assert OLD_DISCARD_TEXT not in _appendix("review")


def test_rewritten_bullet_renders_instead_of_discarding() -> None:
    window = _nearby(_review_doc(), REJECTED_MUST_FIX_ANCHOR, span=1300)
    assert "**Do NOT discard a `.rejected` finding**" in window
    assert "whose `severity` is **not** `MUST_FIX`" in window
    assert "never eligible for bucket assignment" in window
    assert (
        "its anchor is unreliable by definition, making it an operator-review "
        "signal, not a fix-loop input"
    ) in window
    assert "since #2099 an unmatched *evidence quote* is not a rejection reason" in (
        window
    )


def test_must_fix_rule_is_unweakened() -> None:
    window = _after(_review_doc(), REJECTED_MUST_FIX_ANCHOR, span=900)
    assert 'EXITS `blocked` with `blocker.reason: "review_blocked"`' in window


def test_new_bullet_is_placed_after_carve_out_and_before_freeze() -> None:
    """Guards the 900-char #1714 window and the 500/900/3000 Freeze windows."""
    doc = _review_doc()
    carve_out = doc.index(CARVE_OUT_ANCHOR)
    new = doc.index(NEW_BULLET_ANCHOR)
    stripped = doc.index(STRIPPED_ANCHOR)
    freeze = doc.index(FREEZE_ANCHOR)
    assert carve_out < new < stripped < freeze


def test_new_bullet_defines_the_set_and_the_count() -> None:
    text = _bullet_text()
    assert "every entry in `.rejected` that is not in `.rejected_must_fix`" in text
    assert "`.rejected_count` minus `len(.rejected_must_fix)`" in text
    assert "FIRST `cw review consolidate` call" in text
    assert "When N is 0, render nothing" in text


def test_new_bullet_names_tokens_cap_and_overflow() -> None:
    text = _bullet_text()
    assert COUNT_LINE_PREFIX in text
    assert DETAIL_LINE_PREFIX in text
    assert "up to 5 detail entries" in text
    assert "rejected_below_must_fix: +<K> more not shown" in text


def test_new_bullet_promises_no_durable_path() -> None:
    assert ".cw/review-findings/" not in _bullet_text()


def test_new_bullet_cites_raw_fields_and_question_mark_fallback() -> None:
    text = _bullet_text()
    assert "`file`/`line_start`/`summary`" in text
    assert "`raw`" in text
    assert "renders as `?`" in text


def test_new_bullet_states_a_total_severity_order() -> None:
    text = _bullet_text()
    for marker in ("Order entries", "List severities in the order"):
        window = text[text.index(marker) :]
        positions = [window.index(name) for name in SEVERITY_ORDER]
        assert positions == sorted(positions), marker
    assert "keeping reviewer order (as in `.rejected`)" in text


def test_new_bullet_breakdown_omits_must_fix() -> None:
    text = _bullet_text()
    assert "omits the `MUST_FIX` key" in text
    assert "sums to N" in text


def test_new_bullet_is_informational_only() -> None:
    text = _bullet_text()
    assert "the round does not exit" in text
    assert "`health.recommendation` is untouched" in text
    assert "`ambiguities`" in text
    assert "`next_actions`" in text


def test_new_bullet_does_not_re_render_on_step_3b_re_review() -> None:
    text = _bullet_text()
    assert "a Step 3b re-review neither re-renders nor overwrites" in text


def test_new_bullet_points_at_the_appendix_for_blocking_rounds() -> None:
    text = _bullet_text()
    assert "blocking-findings comment" in text
    assert "informational" in text


def test_new_friction_tokens_collide_with_no_reader_prefix() -> None:
    for token in (COUNT_LINE_PREFIX, DETAIL_LINE_PREFIX):
        for prefix in READER_PREFIXES:
            assert not token.startswith(prefix), (token, prefix)
            assert not prefix.startswith(token), (token, prefix)


def test_review_clean_line_is_qualified_when_rejections_exist() -> None:
    window = _after(_review_doc(), CLEAN_ANCHOR, span=900)
    assert (
        "Review clean — N finding(s) mechanically rejected, not adjudicated" in window
    )


def test_all_rejected_round_takes_the_no_issues_branch() -> None:
    window = _after(_review_doc(), CLEAN_ANCHOR, span=900)
    assert "empty `.accepted`" in window
    assert "NO_ISSUES" in window
    assert "all-rejected" in window


def test_disposition_line_reports_mechanical_rejections() -> None:
    window = _after(_review_doc(), DISPOSITION_ANCHOR, span=400)
    assert "mechanically rejected below MUST_FIX (not adjudicated)" in window


def test_appendix_names_the_informational_subsection() -> None:
    appendix = _appendix("review")
    window = _after(appendix, APPENDIX_SUBSECTION, span=1200)
    assert "never a blocking finding" in window
    assert "`<details>`" in window
    assert "N is above 5" in window
    assert "+<K> more not shown" in window


def test_appendix_does_not_repeat_the_blocking_header_in_the_subsection() -> None:
    appendix = _appendix("review")
    window = _after(appendix, APPENDIX_SUBSECTION, span=1200)
    assert "## Blocking Review Findings" not in window
