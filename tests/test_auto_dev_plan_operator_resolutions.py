"""Prose pins for row-delivered operator resolutions (#2702).

``cw dev-queue resolve`` records the operator's answers on the dev-queue row;
the plan stage reads them from ``queue_metadata.operator_resolutions`` as
binding resolutions. These tests pin the Step 1b read-first paragraph, the
``row:<N>`` source identity, the R2 revise-then-compare ordering in Step
1a.0b, the R3 scan-round reset, the R5 friction line and the authority
sentence in ``auto-dev.md``.
"""

from __future__ import annotations

from tests.conftest import _appendix, _checkpoint1_section, _cmd, _norm
from tests.test_auto_dev_preflight_resolutions import _after

_ROW_FIRST = "**Row-delivered resolutions (#2702) — read first.**"
_ORDERING = "**Row-delivered delta ordering (#2702).**"
_INPUTS_NOT_BODY = "**Operator resolutions are inputs, not plan body (#2702).**"
_NO_ALTERNATE = (
    "**Row-delivered revisions never use the Operator-authority-delta alternate"
    " (#2702).**"
)


def _plan() -> str:
    return _cmd("auto-dev-plan.md")


def _step1b_section() -> str:
    content = _plan()
    return content[
        content.index("### Step 1b: Generate Plan") : content.index("### Step 1c:")
    ]


def _row_first_paragraph() -> str:
    return _norm(_after(_step1b_section(), _ROW_FIRST, span=2800))


def _ordering_paragraph() -> str:
    return _norm(_after(_appendix("plan"), _ORDERING, span=4200))


class TestStep1bRowFirst:
    def test_reads_the_row_from_cw_context(self) -> None:
        paragraph = _row_first_paragraph()
        assert "queue_metadata.operator_resolutions" in paragraph
        assert ".claude/cw-context.json" in paragraph

    def test_paragraph_precedes_the_body_comment_precedence_rule(self) -> None:
        section = _step1b_section()
        assert section.index(_ROW_FIRST) < section.index("**Body/comment precedence")

    def test_same_binding_authority_injected_every_round_in_round_order(self) -> None:
        paragraph = _row_first_paragraph()
        assert (
            "the same binding authority as a `<!-- auto-dev-preflight-resolutions -->`"
            " comment" in paragraph
        )
        assert "## Binding Pre-flight Resolutions" in paragraph
        assert "in `round` order" in paragraph
        assert "on every round" in paragraph

    def test_comment_and_body_path_is_the_fallback_only(self) -> None:
        paragraph = _row_first_paragraph()
        assert "When the list is empty or absent" in paragraph
        assert "as the fallback" in paragraph

    def test_marker_channels_become_untallied_mirrors(self) -> None:
        paragraph = _row_first_paragraph()
        assert "un-tallied mirrors" in paragraph
        assert "skip the tally, the newest-wins branch and its friction entry" in (
            paragraph
        )

    def test_never_copies_resolution_text_into_the_draft(self) -> None:
        assert (
            "Never copy a resolution's text verbatim into `.cw/plan-draft.md`"
            in _row_first_paragraph()
        )

    def test_friction_line_for_an_ignored_human_marker_comment(self) -> None:
        paragraph = _row_first_paragraph()
        assert (
            "`resolutions-comment-ignored-as-mirror: comment <id> carries the"
            " preflight marker but the dev-queue row already holds"
            " operator_resolutions (round <N>)" in paragraph
        )
        assert "lacking `<!-- cw-agent-authored -->`" in paragraph


class TestRowSourceIdentity:
    def test_applied_marker_grammar_lists_row(self) -> None:
        assert (
            "<!-- plan-stage-resolutions-applied:"
            " source=comment:<id>|body:<sha>|row:<N>:<sha>|none -->" in _plan()
        )

    def test_resolution_source_placeholder_lists_row(self) -> None:
        assert (
            "`<resolution-source>` is exactly one of `comment:<id>`, `body:<sha>`"
            " or `row:<N>:<sha>`" in _plan()
        )

    def test_placeholder_definition_is_cw_computed_and_legacy_aware(self) -> None:
        """#2728: the digest is cw's; the worker never recomputes it."""
        content = _plan()
        window = _norm(_after(content, "`<resolution-source>` is exactly one of", 1600))
        assert "queue_metadata.operator_resolutions_source" in window
        assert "the worker never recomputes it" in window
        assert "legacy" in window

    def test_fingerprint_rule_names_resolutions_as_inputs(self) -> None:
        content = _plan()
        start = content.index("### Plan-draft fingerprint rule")
        section = content[start : content.index("### Step 1d:", start)]
        window = _norm(_after(section, _INPUTS_NOT_BODY, span=900))
        assert "`plan-stage-resolutions-applied: source=row:<N>:<sha>`" in window
        assert "in every accepted form (including the legacy bare `row:<N>`)" in window
        assert "stripped" in window

    def test_scan_round_counter_resets_on_a_new_row_identity(self) -> None:
        """R3: one assertion, on the tracker-state-delta reset paragraph."""
        assert (
            "A new `row:<N>:<sha>` resolutions source identity (Step 1a.0b) is also a"
            " tracker-state delta and resets `N` to 0"
            in _norm(
                _after(_plan(), "**Tracker-state-delta reset (#2154).**", span=3200)
            )
        )

    def test_appendix_item_1_includes_row_identity(self) -> None:
        window = _norm(
            _after(_appendix("plan"), "1. **Resolutions-delta detection.**", span=2400)
        )
        assert "`row:<N>:<sha>`" in window
        assert "queue_metadata.operator_resolutions" in window
        assert "queue_metadata.operator_resolutions_source" in window
        assert "legacy `row:<N>`" in window


class TestRowDeliveredOrdering:
    def test_equal_fingerprint_keeps_the_approval(self) -> None:
        paragraph = _ordering_paragraph()
        window = paragraph[paragraph.index("**Equal: the approval stands.**") :][:700]
        assert "no `revoke-plan-approval` call" in window
        assert "no `plan-stage-approval-revoked` line" in window
        assert "`approved_fingerprint_match`" in window

    def test_different_fingerprint_revokes_and_reasks(self) -> None:
        paragraph = _ordering_paragraph()
        start = paragraph.index("**Different: revoke exactly as item 3 does today.**")
        window = paragraph[start:][:900]
        assert "`cw dev-queue revoke-plan-approval <ticket> -c <client>`" in window
        assert "`<!-- plan-stage-approval-revoked:" in window
        assert "quoting both fingerprints" in window

    def test_revise_then_compare_then_revoke(self) -> None:
        paragraph = _ordering_paragraph()
        revise = paragraph.index("**Revise first.**")
        compare = paragraph.index("**Then compare.**")
        revoke = paragraph.index("**Different: revoke exactly as item 3 does today.**")
        assert revise < compare < revoke

    def test_item_3_points_row_identities_away_before_its_label(self) -> None:
        appendix = _appendix("plan")
        pointer = appendix.index(
            "For a `row:<N>:<sha>` source identity, skip this item and follow"
            f" {_ORDERING.removesuffix('.**')}**"
        )
        assert pointer < appendix.index("**On delta: revise.**")
        assert "Skip item 3's up-front `revoke-plan-approval` call" in (
            _ordering_paragraph()
        )

    def test_failed_revision_revokes_and_exits_blocked(self) -> None:
        paragraph = _ordering_paragraph()
        window = paragraph[paragraph.index("**If the revision fails") :][:700]
        assert "revoke" in window
        assert "EXIT `blocked`" in window
        assert '`blocker.reason: "agent_block"`' in window

    def test_same_write_resets_the_scan_round_counter(self) -> None:
        paragraph = _ordering_paragraph()
        assert "`plan-stage-resolutions-applied: source=row:<N>:<sha>`" in paragraph
        assert "`<!-- plan-stage-scan-round: 0 -->`" in paragraph
        assert "In the SAME write" in paragraph

    def test_operator_authority_alternate_never_carries_a_row_revision(self) -> None:
        paragraph = _ordering_paragraph()
        assert "never tolerated as advisory-only drift" in paragraph
        assert "a row resolution is not an operator-authority comment" in paragraph

    def test_row_identity_is_content_bound(self) -> None:
        """#2728: a re-enqueued row's new resolutions are always detected."""
        paragraph = _ordering_paragraph()
        assert "content-bound" in paragraph
        assert "queue_metadata.operator_resolutions_source" in _appendix("plan")
        assert "only meaningful within one row lifetime" not in paragraph
        assert "#2728" not in paragraph

    def test_legacy_bare_identity_against_digest_form_is_a_delta(self) -> None:
        """Fifth Delta comparison bullet: one redundant revision, no more."""
        appendix = _appendix("plan")
        start = appendix.index("2. **Delta comparison.**")
        window = _norm(appendix[start : appendix.index("3. For a ", start)])
        assert "persisted legacy `row:<N>`" in window
        assert "`row:<N>:<sha>`" in window
        assert "one redundant revision" in window

    def test_cap_is_per_full_row_identity(self) -> None:
        assert "the cap of 1 attempt per `row:<N>:<sha>`" in _ordering_paragraph()


class TestCheckpoint1NoAlternate:
    def test_paragraph_sits_after_the_mismatch_sub_case(self) -> None:
        section = _checkpoint1_section()
        assert section.index("**Fingerprint mismatch sub-case (#2102).**") < (
            section.index(_NO_ALTERNATE)
        )

    def test_alternate_does_not_apply_to_a_row_delivery(self) -> None:
        section = _checkpoint1_section()
        window = _norm(_after(section, _NO_ALTERNATE, span=1400))
        assert "only exact equality of `draft_fp` and `plan_approved_fingerprint`" in (
            window
        )
        assert "never tolerated as advisory-only drift" in window
        assert "a row resolution is not an operator-authority comment" in window


class TestStep1cRowEntries:
    def test_pm_reviewer_ticket_block_carries_row_entries(self) -> None:
        assert "## Operator resolutions recorded on the dev-queue row (binding)" in (
            _plan()
        )

    def test_appendix_step_3b_lists_row_candidates(self) -> None:
        window = _norm(
            _after(
                _appendix("plan"),
                "3b. **Row-delivered candidates (#2702).**",
                span=700,
            )
        )
        assert "queue_metadata.operator_resolutions" in window
        assert "exempt from the provenance gate" in window


class TestAuthoritySentence:
    def test_two_things_still_named_and_row_added(self) -> None:
        content = _cmd("auto-dev.md")
        window = _norm(
            _after(content, "Exactly two things carry operator authority", span=1600)
        )
        assert "unmarked comment written by a human" in window
        assert "<!-- auto-dev-preflight-resolutions -->" in window
        assert "deliberately binding" in window
        assert "queue_metadata.operator_resolutions" in window
        assert "<!-- cw-agent-authored -->" in window
        assert "agent analysis for every other purpose" in window
