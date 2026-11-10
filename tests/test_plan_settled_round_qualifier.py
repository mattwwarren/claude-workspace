"""Guard tests: round-qualified settled-marker ids and the fingerprint settle (#2626).

Pure-markdown assertions over ``auto-dev-plan.md`` and its appendix, in the
per-feature style of ``test_plan_comment_approval_token.py``; helpers and
anchors are imported from ``test_plan_stage_settlement`` rather than re-derived.

Background: ``<n>`` restarts at 1 in every park comment, so a bare ``A1`` settled
by round 1 made round 2's unrelated item 1 read as already settled. The
settled-marker id now carries the park's ordinal (``R<k>.A<n>``, written only for
``k >= 2``). Separately, the ``plan-stage-last-evaluated`` fingerprint was
persisted only on the three EXIT outcomes, so an AUTO-CONTINUE round left it
stale; the *Fingerprint settle* persists it there without ever incrementing the
round counter.
"""

import hashlib
import re

from cw.plan_fingerprint import compute_plan_draft_fingerprint
from tests.conftest import _appendix, _cmd
from tests.test_plan_stage_settlement import (
    CAP_CHECK_ANCHOR,
    SETTLED_MARKER,
    STEP4C_ANCHOR,
    _cap_check_block,
    _seam,
    _step1c0_block,
    _step1c_section,
)

GRAMMAR_ANCHOR = f"**Settlement marker grammar (`{SETTLED_MARKER}`, #1683).**"
ROUND_QUALIFIED_ANCHOR = "**Round-qualified item ids (#2626).**"
SETTLE_ANCHOR = "> **Fingerprint settle (#2626).**"
ORDINAL_ANCHOR = "**Park ordinal and item ids (#2626).**"

_BODY = "# Plan\n\nreconciled draft body\n"
_BODY_SHA = hashlib.sha256(_BODY.encode("utf-8")).hexdigest()
_ROUND = "<!-- plan-stage-scan-round: 1 -->\n"


def _paragraph(text: str, anchor: str) -> str:
    """The blank-line-delimited paragraph starting at *anchor*."""
    start = text.index(anchor)
    end = text.find("\n\n", start)
    return text[start : end if end != -1 else len(text)]


def _core_paragraph(anchor: str) -> str:
    return _paragraph(_cmd("auto-dev-plan.md"), anchor)


def _settle() -> str:
    """The Fingerprint settle blockquote line, from the seam."""
    seam = _seam()
    start = seam.index(SETTLE_ANCHOR)
    return seam[start : seam.index("\n", start)]


def _step_item(block: str, marker: str) -> str:
    """The numbered Step 1c.0 item (one logical line) starting with *marker*."""
    start = block.index(marker)
    end = block.find("\n   ", start + 1)
    return block[start : end if end != -1 else len(block)]


# ---------------------------------------------------------------------------
# Defect 1: round-qualified item ids.
# ---------------------------------------------------------------------------


def test_doc_grammar_lines_are_stripped_by_the_python_fingerprint() -> None:
    """Every grammar form the core doc prints is bookkeeping to the Python rule."""
    section = _step1c_section()
    start = section.index(GRAMMAR_ANCHOR)
    fence_open = section.index("```\n", start) + len("```\n")
    fence_close = section.index("\n```", fence_open)
    forms = section[fence_open:fence_close].splitlines()
    assert len(forms) == 6

    for form in forms:
        line = (
            form.replace("<n>", "1")
            .replace("<k>", "2")
            .replace("<x>", "b")
            .split(" | ")[0]
        )
        marker = f"<!-- {SETTLED_MARKER}: {line} -->\n"
        assert compute_plan_draft_fingerprint(_ROUND + marker + _BODY) == _BODY_SHA, (
            f"doc grammar form {form!r} is not stripped by cw.plan_fingerprint"
        )


def test_round_one_is_bare_and_r1_is_never_written() -> None:
    paragraph = _core_paragraph(ROUND_QUALIFIED_ANCHOR)
    assert "The bare spelling (`A1`, `P2`) is the round-1 spelling" in paragraph
    assert "the only spelling ever written for round 1" in paragraph
    assert "the `R<k>.` spelling is written only for `<k>` ≥ 2" in paragraph
    assert "`R1.` is never written (it is non-conforming)" in paragraph


def test_two_rounds_each_parking_item_1_get_distinct_ids() -> None:
    assert ORDINAL_ANCHOR in _step1c0_block()
    paragraph = _paragraph(_step1c0_block(), ORDINAL_ANCHOR)
    assert (
        "item id `A<n>` (ambiguity) or `P<n>` (premise) when `k` is 1, and "
        "`R<k>.A<n>` or `R<k>.P<n>` when `k` ≥ 2"
    ) in paragraph
    assert "by exact string comparison (`A1` never matches `R2.A1`)" in paragraph
    assert "Ids compare as exact strings: `A1` and `R2.A1` are different items" in (
        _core_paragraph(ROUND_QUALIFIED_ANCHOR)
    )


def test_item_id_is_derived_from_comments_not_the_round_counter() -> None:
    paragraph = _paragraph(_appendix("plan"), ORDINAL_ANCHOR)
    assert "derived from the live-fetched comments every round" in paragraph
    assert "never from the round counter `N`" in paragraph
    assert "no new persisted state exists" in paragraph
    core = _core_paragraph(ROUND_QUALIFIED_ANCHOR)
    assert "`<k>` is not the round counter `N`" in core


def test_settled_means_full_id_already_present_and_never_duplicated() -> None:
    appendix = _appendix("plan")
    paragraph = _paragraph(appendix, ORDINAL_ANCHOR)
    assert "An item is **settled** exactly when" in paragraph
    assert "a settled item is not a still-open item" in paragraph
    assert "step 4 never re-evaluates it" in paragraph
    step5 = _step_item(_step1c0_block(), "5. For each settled")
    assert "skip the write when that exact id is already present" in step5


def test_legacy_bare_ids_parse_as_round_one() -> None:
    assert "A bare id written by an earlier version reads as round 1" in (
        _paragraph(_appendix("plan"), ORDINAL_ANCHOR)
    )
    assert "Every bare id persisted by an earlier version keeps parsing and means " in (
        _core_paragraph(ROUND_QUALIFIED_ANCHOR)
    )


def test_step5_marker_and_settled_entry_carry_the_full_id_friction_line_unchanged() -> (
    None
):
    step5 = _step_item(_step1c0_block(), "5. For each settled")
    assert "item id per step 2's *Park ordinal and item ids*" in step5
    assert "led by the item's full id (`A1`, `R2.A1`)" in step5
    assert "`plan-stage item settled: <item id> → <marker value> — round <N>`" in step5


def test_plain_comment_answers_still_accepted() -> None:
    assert "No marker, no required format — plain natural language." in (
        _step1c0_block()
    )


# ---------------------------------------------------------------------------
# Defect 2: the fingerprint settle on outcomes the cap check does not reach.
# ---------------------------------------------------------------------------


def test_fingerprint_settle_follows_cap_check_inside_the_seam() -> None:
    core = _cmd("auto-dev-plan.md")
    assert core.count(SETTLE_ANCHOR) == 1
    settle = core.index(SETTLE_ANCHOR)
    assert core.index(CAP_CHECK_ANCHOR) < settle < core.index(STEP4C_ANCHOR)
    assert SETTLE_ANCHOR in _cap_check_block()
    assert SETTLE_ANCHOR in _seam()


def test_fingerprint_settle_scope() -> None:
    settle = _settle()
    assert "AUTO-CONTINUE (`parked` empty AND `unverified` empty)" in settle
    assert "the stub check's `deferred_stub_unresolved` hard-EXIT" in settle
    assert "run this before that park's draft write" in settle


def test_fingerprint_settle_never_increments_or_caps() -> None:
    settle = _settle()
    assert "`N` is never incremented here" in settle
    assert "the `N >= 2` test never runs" in settle
    assert "only the three EXIT outcomes increment and cap" in settle


def test_fingerprint_settle_resets_n_only_on_delta_in_the_same_write() -> None:
    settle = _settle()
    assert "set `N` to 0 on a difference and leave `N` as it is otherwise" in settle
    assert "persist the fresh fingerprint to the second line in that same write" in (
        settle
    )
    assert "a delta is never consumed without its reset" in settle


def test_fingerprint_settle_gated_on_round_counter_line() -> None:
    settle = _settle()
    assert (
        "when `.cw/plan-draft.md` exists and its first line is the round-counter line"
        in (settle)
    )
    assert "A draft with no round-counter line has no persisted fingerprint" in settle
    assert "leave it byte-identical" in settle


def test_unchanged_autocontinue_round_does_not_reset_the_counter() -> None:
    assert (
        "An AUTO-CONTINUE round whose tracker state is unchanged therefore neither "
        "resets nor increments `N`, so the parks on either side of it count toward "
        "the same cap of 2."
    ) in _settle()


def test_fingerprint_settle_keeps_body_sha_nonzero_exception() -> None:
    assert "The `cw body-sha` non-zero exception above applies unchanged" in _settle()


def test_step4c_autocontinue_bullet_cites_the_settle() -> None:
    core = _cmd("auto-dev-plan.md")
    start = core.index(
        "`parked` empty AND `unverified` empty → AUTO-CONTINUE to Step 1d."
    )
    bullet = core[start : core.index("\n", start)]
    assert "The Pre-branch seam's *Fingerprint settle* has already run" in bullet


def test_round_counter_paragraph_still_says_autocontinue_never_increments() -> None:
    paragraph = _core_paragraph("**Round counter (`plan-stage-scan-round`, #1683).**")
    assert "AUTO-CONTINUE never does, since nothing is parked." in paragraph
    assert "which resets `N` on a genuine delta but never increments it" in paragraph


def test_step1c0_step0_names_both_persistence_points() -> None:
    step0 = _step_item(_step1c0_block(), "0. **Fingerprint read (#2154).**")
    assert "persisted by the Pre-branch seam, not here" in step0
    assert "cap check's own tracker-state-delta reset step on the three EXIT" in step0
    assert "*Fingerprint settle* on AUTO-CONTINUE and the stub check's hard-EXIT" in (
        step0
    )


def test_settle_block_has_no_forbidden_cap_block_tokens() -> None:
    """The settle lives inside the cap-check window other tests scan."""
    settle = _settle()
    for token in (
        "plan_approved_at",
        "newest-comment-id",
        "newest-comment-timestamp",
        "resolution_consumed",
        "resolution_evidence",
        "reused from",
    ):
        assert token not in settle
    assert re.search(r"redact", settle, re.IGNORECASE) is None
