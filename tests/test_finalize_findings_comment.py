"""Guard tests: finalize ``agent_block`` findings reach the regressed IMPL run (#2794).

Pure-markdown assertions over the auto-dev pipeline instruction files, in the
``read_text()`` + literal-substring/window convention of
``test_blocking_findings_comment.py``: ``_cmd``/``_appendix`` come from
``tests.conftest`` and ``_after`` from ``test_auto_dev_preflight_resolutions``.

Background: when ``/prep-pr``'s ship-it gate blocks a FINALIZE run on review
findings, the sentinel collapses to ``agent_block`` and Sub-rule 5a regresses
the ticket to IMPL, but the only channel carrying the findings there was
``prior_attempts_summary``, capped at 500 characters. Option A (operator
decision): finalize's main session posts the findings, redacted and in parts,
as an agent-authored ``## Blocking Review Findings`` comment under the
sub-heading ``### Finalize ship-it gate findings``, then stamps
``cw signal-park``; the IMPL appendix's regress findings carve-out passes that
one post to the Stage 2 agent as labelled evidence.

Since #2810 the post goes through ``cw post-comment``, which redacts, wraps,
splits, posts and reads back deterministically; its behaviour is unit-tested in
``tests/test_tracker_post.py`` and ``tests/test_cli_post_comment.py``. This file
pins the stage prose: the flags, the exit-code handling, and the layers the
over-500-character acceptance case is composed from (the documented body shape,
the unchanged ``prior_attempts_summary`` cap, the redactor's no-truncation
behavior, and the IMPL carve-out's verbatim-delivery clause).
"""

from pathlib import Path

import pytest

from cw import redact_post_text
from cw.auto_dev_result.schema import FINALIZE_REGRESS_BLOCKER_REASONS
from cw.gh import AGENT_COMMENT_MARKER
from cw.spawn import _PRIOR_ATTEMPT_DETAILS_MAX_LEN
from tests._shared.workflow_text import _appendix, _bash_fences, _cmd
from tests.test_agent_comment_provenance import PIPELINE_HEADERS
from tests.test_auto_dev_finalize_automerge_verification import _ORDER_OF_EVALUATION
from tests.test_auto_dev_preflight_resolutions import _after
from tests.test_bodyfile_write_tool_conformance import (
    POINTER,
    RESTATED_ARGUMENT_PHRASES,
)
from tests.test_impl_guard_staleness_docs import (
    CARVE_OUT_ANCHOR,
    FINDINGS_HEADER,
    FINDINGS_SUBHEADING,
    _carve_out_clause,
    _carve_out_lines,
)
from tests.test_sentinel_emission_discipline import (
    FINALIZE_FINDINGS_SECTION,
    FINALIZE_PARK_MARKER,
    _finalize_findings_section,
    _paragraph_containing,
)

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"

RULE_REFERENCE = "blocking-findings comment rule"
CORE_ANCHOR = "**Finalize review-findings comment (#2794):**"
ORDER_CLAUSE = (
    "; within step (4), a review-findings block also posts the findings comment"
)
PART_LABEL = "(part i of n)"
POST_STEP = "**Post and read back.**"
REDACTOR_LIMITS = "**Redactor limits.**"
SEAM_REFERENCE = "*Tracker post seam (#2810)* in `.claude/commands/auto-dev.md`"
FAILED_FRICTION = "blocking findings post failed: finalize_agent_block"
SKIPPED_FRICTION = "blocking findings post skipped: post_comment_refused"
POSTED_FRICTION = "blocking findings posted: finalize_agent_block"
REVIEW_PARK_IGNORE = (
    "If it is denied, exits non-zero, is an unknown command (an older `cw`), or "
    "says `park marker NOT recorded`, ignore that and emit the exit sentinel "
    "unchanged."
)

ROW_FINALIZE_FINDINGS_ANCHOR = (
    "| S4c `/prep-pr` BLOCK whose details are review findings"
)
MEANING_ROW_AGENT_BLOCK_ANCHOR = (
    "| `agent_block` | Any other agent returned friction level BLOCK that the "
    "pipeline could not auto-resolve"
)

ACCEPTANCE_DETAILS_CHARS = 60_500
GITHUB_PART_LIMIT = "60,000"


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def _core_paragraph() -> str:
    return _paragraph_containing(_cmd("auto-dev-finalize.md"), CORE_ANCHOR)


def _section_paragraph(anchor: str) -> str:
    return _paragraph_containing(_finalize_findings_section(), anchor)


def _post_step() -> str:
    """The Post and read back step: prose, command fence and exit bullets."""
    section = _finalize_findings_section()
    return section[section.index(POST_STEP) : section.index(REDACTOR_LIMITS)]


def _post_fence() -> str:
    fences = [f for f in _bash_fences(_post_step()) if "cw post-comment" in f]
    assert len(fences) == 1
    return fences[0]


def _exit_bullet(prefix: str) -> str:
    return next(line for line in _post_step().splitlines() if line.startswith(prefix))


# ---------------------------------------------------------------------------
# Core doc: the Step 4c paragraph and the order of evaluation
# ---------------------------------------------------------------------------


def test_core_paragraph_follows_the_base_branch_state_classifier() -> None:
    content = _cmd("auto-dev-finalize.md")
    classifier = content.index("**Base-branch-state classifier (#2320):**")
    paragraph = content.index(CORE_ANCHOR)
    assert classifier < paragraph < content.index("**Unavailability classifier")


def test_core_paragraph_names_trigger_order_and_rule() -> None:
    paragraph = _core_paragraph()
    for phrase in (
        "`gate:` text begins `Step 8 ship-it` or equals `Step 7 max review cycles`",
        "the literal token `MUST_FIX`",
        "only after the Unavailability classifier, the denied-arm check (#2625) "
        "and the Base-branch-state classifier (#2320) have not claimed the block",
        "finalize's main session",
        RULE_REFERENCE,
        FINDINGS_HEADER,
        FINDINGS_SUBHEADING,
        "posts nothing, exactly as today",
        "`external_state_block`",
        "merge conflict",
        "Park marker clause",
        FINALIZE_FINDINGS_SECTION.removeprefix("## "),
    ):
        assert phrase in paragraph, phrase


def test_prep_pr_still_produces_the_gates_the_trigger_names() -> None:
    """The trigger matches `/prep-pr`'s `gate:` text; a rename there would turn
    the whole feature into a silent no-op, so pin both producers."""
    prep_pr = _cmd("prep-pr.md")
    assert 'gate: "Step 7 max review cycles"' in prep_pr
    assert 'gate: "Step 8 ship-it' in prep_pr
    trigger = _core_paragraph()
    assert "Step 7 max review cycles" in trigger
    assert "Step 8 ship-it" in trigger


def test_core_paragraph_does_not_name_the_park_stamp_command() -> None:
    assert "cw signal-park" not in _core_paragraph()


def test_order_of_evaluation_pin_holds_and_findings_clause_follows_it() -> None:
    """G2: the #2625 order string stays byte-identical; the clause is appended."""
    content = _cmd("auto-dev-finalize.md")
    assert _ORDER_OF_EVALUATION in content
    assert ORDER_CLAUSE not in _ORDER_OF_EVALUATION
    assert content.index(ORDER_CLAUSE) == content.index(_ORDER_OF_EVALUATION) + len(
        _ORDER_OF_EVALUATION
    )


# ---------------------------------------------------------------------------
# Appendix section: trigger, body shape, provenance, sentinel
# ---------------------------------------------------------------------------


def test_section_sits_before_the_unavailability_classifier() -> None:
    appendix = _appendix("finalize")
    assert appendix.index(FINALIZE_FINDINGS_SECTION) < appendix.index(
        "## Unavailability classifier: signature table"
    )


def test_section_names_header_subheading_and_marker() -> None:
    section = _finalize_findings_section()
    assert f"`{FINDINGS_HEADER}`" in section
    assert f"`{FINDINGS_SUBHEADING}`" in section
    assert section.index(f"`{FINDINGS_HEADER}`") < section.index(
        f"`{FINDINGS_SUBHEADING}`"
    )
    assert AGENT_COMMENT_MARKER in section
    assert RULE_REFERENCE in section


def test_section_states_the_trigger_and_its_accepted_limit() -> None:
    paragraph = _section_paragraph("**Trigger.**")
    assert "`Step 8 ship-it`" in paragraph
    assert "`Step 7 max review cycles`" in paragraph
    assert "`MUST_FIX`" in paragraph
    assert "**Accepted limit:**" in paragraph
    assert "takes today's path" in paragraph


def test_body_is_gate_plus_verbatim_details_untruncated() -> None:
    paragraph = _section_paragraph("**Body shape.**")
    assert "`gate:` line plus its `details:` text, posted verbatim, not truncated" in (
        paragraph
    )
    assert "`_PRIOR_ATTEMPT_DETAILS_MAX_LEN`" in paragraph
    assert "500-character" in paragraph
    assert "does not apply to this path" in paragraph


def test_section_keeps_agent_block_and_details_unchanged() -> None:
    paragraph = _section_paragraph("**Sentinel.**")
    assert '`blocker.reason` stays `"agent_block"`' in paragraph
    assert "`blocker.details` is unchanged" in paragraph
    assert POSTED_FRICTION in paragraph
    assert "friction_highlights" in paragraph


def test_failed_post_is_never_stamped() -> None:
    assert FAILED_FRICTION in _exit_bullet("- **Exit 3**")
    stamp = _exit_bullet("- `cw signal-park`")
    assert "only after exit 0" in stamp
    assert "do not run it" in stamp


# ---------------------------------------------------------------------------
# The post goes through cw post-comment (#2810)
# ---------------------------------------------------------------------------


def test_post_step_routes_through_the_seam_with_its_flags() -> None:
    step = _post_step()
    assert SEAM_REFERENCE in step
    fence = _post_fence()
    assert "--body-file .cw/finalize-findings.txt" in fence
    assert f'--header "{FINDINGS_HEADER}"' in fence
    assert f'--subheading "{FINDINGS_SUBHEADING}"' in fence
    for flag in ("--fence", "--parts split", "--consume"):
        assert flag in fence, flag
    assert "in the foreground" in step


def test_the_old_shell_machinery_is_gone() -> None:
    section = _finalize_findings_section()
    for stale in (
        "redact_post_text.py",
        "REDACT_EXIT",
        "READBACK_MISMATCH",
        "cmp -s",
        "trap 'rm -f",
        "**Step 0 — clear leftovers.**",
        "**Read-back (G4).**",
        "**Residual risk (G4).**",
        "cw-script-version",
        "redactor_unavailable",
    ):
        assert stale not in section, stale


def test_redaction_runs_before_measuring_and_splitting() -> None:
    step = _post_step()
    assert "The command redacts first" in step
    assert (
        "the redaction output, not the raw text, is what gets measured, split "
        "and posted" in step
    )
    assert (
        "the header, sub-heading, label line, fence and marker are added after "
        "redaction" in step
    )
    assert step.index("redacts first") < step.index(PART_LABEL)


def test_refused_post_fails_closed_with_no_park() -> None:
    bullet = _exit_bullet("- **Exit 1**")
    for phrase in (
        "any exit other than 0 or 3",
        "exit 1 with no parseable JSON",
        SKIPPED_FRICTION,
        "otherwise unchanged",
        "Never fall back to a raw `gh issue comment`",
    ):
        assert phrase in bullet, phrase


def test_exit_handling_points_at_the_seam_mapping_not_a_copy() -> None:
    """The finalize bullets state only finalize-specific values (H1b/H1c)."""
    step = _post_step()
    assert (
        "per the *Exit-code to friction mapping* in the *Tracker post seam (#2810)* "
        "of `.claude/commands/auto-dev.md`" in step
    )
    bullet = _exit_bullet("- **Exit 3**")
    assert "never retry the post" in bullet
    assert "The seam's exit-3 recording applies" in bullet
    assert "empty-`posted` case" in bullet
    # The seam owns the recording sentence; a second copy here is what drifted.
    assert "recording the comment URL of EACH already-posted part" not in step
    assert "Deletion needs a human" not in step
    assert "public and read back" not in step


def test_park_stamp_runs_only_after_exit_zero_and_every_linear_part_posted() -> None:
    bullet = _exit_bullet("- `cw signal-park`")
    assert "only after exit 0" in bullet
    assert "`emitted` on Linear" in bullet
    assert "every `out_files` part was posted via `create_comment`" in bullet


def test_raw_file_never_outlives_the_post() -> None:
    step = _post_step()
    assert "`--consume` deletes the raw file once it has been read, on every path" in (
        step
    )
    assert "never outlives the post" in step


def test_linear_uses_emit_only() -> None:
    step = _post_step()
    assert "--emit-only --out-dir .cw/finalize-findings-parts" in step
    assert "`create_comment`" in step


# ---------------------------------------------------------------------------
# Body file convention and the free-text ticket
# ---------------------------------------------------------------------------


def test_section_uses_the_write_tool_pointer_and_body_file() -> None:
    section = _finalize_findings_section()
    assert POINTER in section
    assert "--body-file" in section
    for phrase in RESTATED_ARGUMENT_PHRASES:
        assert phrase not in section, phrase


def test_free_text_ticket_posts_nothing() -> None:
    paragraph = _section_paragraph("**Tracker and ticket.**")
    assert "A free-text ticket (no tracker) posts nothing" in paragraph
    assert '"**Post to Linear:**"' in paragraph


# ---------------------------------------------------------------------------
# Parts, never truncated (R4)
# ---------------------------------------------------------------------------


def test_long_findings_split_into_parts_never_truncated() -> None:
    step = _post_step()
    assert "--parts split" in step
    assert GITHUB_PART_LIMIT in step
    assert "at most 59,000" in step
    assert "on line boundaries" in step
    assert PART_LABEL in step
    assert "header, sub-heading, label line, fence and marker included" in step
    assert "never truncated" in step


def test_section_never_references_the_truncating_helper() -> None:
    section = _finalize_findings_section()
    assert "redact_and_cap" not in section
    assert "[truncated" not in section


# ---------------------------------------------------------------------------
# Redactor limits, park marker and park flag (G5)
# ---------------------------------------------------------------------------


def test_park_marker_follows_the_post_and_requires_exit_zero() -> None:
    section = _finalize_findings_section()
    assert section.rindex(FINALIZE_PARK_MARKER) > section.index(POST_STEP)
    paragraph = _section_paragraph(FINALIZE_PARK_MARKER)
    assert "once `cw post-comment` exits 0" in paragraph
    assert (
        "on Linear, every `out_files` part was posted via `create_comment`" in paragraph
    )
    assert "read back successfully" not in paragraph
    assert "cw signal-park" in paragraph
    assert REVIEW_PARK_IGNORE in paragraph
    assert "After any other exit (the exit mapping above), do not run it." in paragraph
    assert FAILED_FRICTION not in paragraph


def test_park_marker_mirrors_the_review_appendix_ignore_wording() -> None:
    assert REVIEW_PARK_IGNORE in _appendix("review")


def test_redactor_limits_replace_the_residual_risk_paragraph() -> None:
    paragraph = _section_paragraph(REDACTOR_LIMITS)
    assert "path shapes and secret shapes only" in paragraph
    assert "quoted code, fixtures or customer data" in paragraph
    assert "a header name followed by a space and its value" in paragraph
    assert "(`x-api-key abc123`;" in paragraph
    assert "the colon and equals forms are detected" in paragraph
    assert "visibility refusal" in paragraph
    assert "read-back" in paragraph
    assert SEAM_REFERENCE in paragraph
    section = _finalize_findings_section()
    assert "prompt-driven" not in section
    assert "follow-up #2810" not in section


def test_park_flag_note_is_stated() -> None:
    paragraph = _section_paragraph("**Park-flag note (G5).**")
    assert "parks the row `BLOCKED_ON_USER`" in paragraph
    assert "does NOT 5a-regress" in paragraph
    assert "intended semantics" in paragraph
    assert "ships dark by default" in paragraph


# ---------------------------------------------------------------------------
# Gate-collapse and meaning rows
# ---------------------------------------------------------------------------


def test_auto_dev_rows_mention_blocking_findings_comment() -> None:
    content = _cmd("auto-dev.md")
    row = _after(content, ROW_FINALIZE_FINDINGS_ANCHOR, span=500)
    assert "blocking findings" in row.lower()
    assert '`blocker.reason: "agent_block"`' in row
    meaning = _after(content, MEANING_ROW_AGENT_BLOCK_ANCHOR, span=350)
    assert "blocking findings" in meaning.lower()
    # The generic catch-all row stays.
    assert "| Any other agent BLOCK (Plan / prep-pr / etc.) |" in content


def test_headless_contract_rows_mention_blocking_findings_comment() -> None:
    content = _doc("headless-contract.md")
    row = _after(content, ROW_FINALIZE_FINDINGS_ANCHOR, span=500)
    assert "blocking findings" in row.lower()
    meaning = _after(content, MEANING_ROW_AGENT_BLOCK_ANCHOR, span=350)
    assert "blocking findings" in meaning.lower()
    assert "| Any other agent BLOCK (Plan / prep-pr / etc.) |" in content


LINEAR_QUALIFIER = "on Linear the carve-out withholds"
GITHUB_QUALIFIER = "on `github-issues`"
NOT_PASSED = "not passed to the regressed IMPL run"


def _row(content: str) -> str:
    return next(
        line for line in content.splitlines() if ROW_FINALIZE_FINDINGS_ANCHOR in line
    )


@pytest.mark.parametrize(
    "content", [_cmd("auto-dev.md"), _doc("headless-contract.md")], ids=["cmd", "doc"]
)
def test_contract_row_qualifies_delivery_by_tracker(content: str) -> None:
    """Linear always withholds (G3), so no row may say the run "receives" it flat."""
    row = _row(content)
    assert GITHUB_QUALIFIER in row
    assert LINEAR_QUALIFIER in row
    assert "`identity_unresolved`" in row
    assert NOT_PASSED in row
    assert "stay in the tracker comment" in row


def test_finalize_sentinel_paragraph_qualifies_delivery_by_tracker() -> None:
    paragraph = _section_paragraph("**Sentinel.**")
    assert "on `github-issues` the regressed run receives this comment" in paragraph
    assert "On Linear the carve-out withholds with `identity_unresolved`" in paragraph
    assert "not passed to the regressed IMPL run" in paragraph


def test_finalize_core_paragraph_qualifies_delivery_by_tracker() -> None:
    assert (
        "where, on `github-issues`, the regressed run receives the comment as "
        "evidence (on Linear the carve-out withholds it with `identity_unresolved`)"
    ) in _core_paragraph()


# ---------------------------------------------------------------------------
# Regression pins: nothing on the routing side moves
# ---------------------------------------------------------------------------


def test_header_reused_not_added_to_the_provenance_enumeration() -> None:
    assert FINDINGS_HEADER in PIPELINE_HEADERS
    assert len(PIPELINE_HEADERS) == 4
    rule = _cmd("auto-dev.md")
    rule_section = rule[
        rule.index("## Comment provenance rule (#2097)") : rule.index(
            "## Tool-Use Denial Exit"
        )
    ]
    assert FINDINGS_SUBHEADING not in rule_section


def test_finalize_regress_reasons_unchanged() -> None:
    assert sorted(FINALIZE_REGRESS_BLOCKER_REASONS) == ["agent_block"]
    assert "external_state_block" not in FINALIZE_REGRESS_BLOCKER_REASONS


# ---------------------------------------------------------------------------
# Acceptance: an over-500-character multi-finding block, composed from layers
# ---------------------------------------------------------------------------


def _acceptance_details() -> str:
    findings = [
        f"MUST_FIX {n}: src/cw/module_{n}.py:{n * 10} handler drops the error"
        for n in range(1, 13)
    ]
    details = "\n".join(findings)
    assert len(details) > _PRIOR_ATTEMPT_DETAILS_MAX_LEN
    return details


def test_acceptance_body_shape_posts_long_details_verbatim() -> None:
    details = _acceptance_details()
    assert len(details) > _PRIOR_ATTEMPT_DETAILS_MAX_LEN
    paragraph = _section_paragraph("**Body shape.**")
    assert "posted verbatim, not truncated" in paragraph


def test_acceptance_prior_attempts_cap_is_unchanged() -> None:
    assert _PRIOR_ATTEMPT_DETAILS_MAX_LEN == 500


def test_acceptance_redactor_keeps_every_character() -> None:
    worktree = "/home/alice/.cw/wt/abcd1234/dev-2794"
    details = f"gate: Step 8 ship-it\n{worktree}/src/cw/x.py\n" + _acceptance_details()
    padded = (
        details
        + "\n"
        + "MUST_FIX trailing finding text. " * (ACCEPTANCE_DETAILS_CHARS // 32)
    )
    assert len(padded) > ACCEPTANCE_DETAILS_CHARS

    result = redact_post_text.redact_post_text(padded, home="/home/alice", worktrees=[])

    assert len(result) == len(padded) - len(worktree) + len("<worktree>")
    assert result.endswith(padded[-1000:])


def test_acceptance_impl_carve_out_delivers_every_part_verbatim() -> None:
    lines = _carve_out_lines()
    assert len(lines) == 2
    for line in lines:
        assert CARVE_OUT_ANCHOR in line
        clause = _carve_out_clause(line)
        assert "passed verbatim in part order" in clause
        assert "all parts" in clause
