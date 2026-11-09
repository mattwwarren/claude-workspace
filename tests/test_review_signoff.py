"""``cw.review_signoff``: the PR-body sign-off section (#3043, RFC 0017 II2).

Pure unit tests. The attestation comes from ``attestation_for``; artifact
entries are plain ``Adjudication`` values. Maps to ``src/cw/review_signoff.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import cw.review_signoff as signoff_mod
from cw.models.review_attestation import (
    AttestationAcceptance,
    AttestationOverride,
)
from cw.review_adjudication import Adjudication
from cw.review_signoff import (
    END_MARKER,
    START_MARKER,
    DebtView,
    SignoffResult,
    SkipReason,
    build_debt_view,
    compose_body,
    friction_line,
    render_section,
    sanitize,
)
from cw.tracker_post import PartPlan
from cw.tracker_post import plan_parts as real_plan_parts
from tests._review_attestation_helpers import attestation_for

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models.review_attestation import ReviewAttestation

HEAD = "a" * 40
OTHER = "b" * 40
HOME = "/home/alice"
WORKTREES: tuple[str, ...] = ("/wt/anchor",)
NO_DEBT = build_debt_view([], 0)
HEADING = "## Review sign-off"


def _entry(**overrides: object) -> Adjudication:
    base: dict[str, object] = {
        "severity": "SHOULD_FIX",
        "file": "src/cw/foo.py",
        "summary": "helper swallows the error",
        "outcome": "defer",
        "rationale": "out of scope",
    }
    base.update(overrides)
    return Adjudication.model_validate(base)


def _compose(
    body: str,
    gate: ReviewAttestation | SkipReason,
    *,
    debt: DebtView = NO_DEBT,
    pr_head: str = HEAD,
) -> SignoffResult:
    return compose_body(
        body,
        gate=gate,
        pr_head=pr_head,
        debt=debt,
        home=HOME,
        worktrees=WORKTREES,
    )


# -- friction grammar --------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "skipped_head_mismatch",
        "stripped_head_mismatch",
        "skipped_invalid:untolerated_commit",
        "stripped_invalid:verdict_not_pass",
        "skipped_truncated",
        "stripped_truncated",
        "repaired_markers",
        "truncated",
        "debt_count_mismatch",
        "artifact_unreadable",
        "skipped_cli_failed",
        "skipped_pr_head_unreadable",
    ],
)
def test_friction_line_accepts_documented_tokens(token: str) -> None:
    assert friction_line(token) == f"friction: review_signoff: {token}"


@pytest.mark.parametrize(
    "token", ["bogus", "skipped_", "invalid:missing", "Stripped_x"]
)
def test_friction_line_rejects_unknown_tokens(token: str) -> None:
    with pytest.raises(ValueError, match="unknown review_signoff friction token"):
        friction_line(token)


# -- sanitize ---------------------------------------------------------------


def test_sanitize_collapses_whitespace_and_newlines() -> None:
    assert sanitize("one\n\ntwo\t three") == "one two three"


def test_sanitize_removes_marker_syntax_and_heading() -> None:
    assert sanitize("x <!-- hidden --> y") == "x hidden y"
    assert "<!--" not in sanitize("<!--<!-- boom -->")
    assert sanitize("## Heading injected") == "Heading injected"


def test_sanitize_caps_at_200_chars() -> None:
    assert len(sanitize("a" * 500)) == signoff_mod.MAX_VALUE_CHARS


# -- render shape ------------------------------------------------------------


def test_engineless_render_has_every_field_in_order() -> None:
    att = attestation_for(HEAD)
    lines = render_section(att, pr_head=HEAD, debt=NO_DEBT).split("\n")
    assert lines[0] == START_MARKER
    assert lines[1] == HEADING
    assert lines[2] == ""
    assert lines[3] == "- engine: none (generic cw review)"
    assert lines[4].startswith("- reviewer backend: ")
    assert lines[5] == f"- attested head: {HEAD}"
    assert lines[6] == "- verdict: PASS"
    assert lines[7] == "- outstanding at head: must_fix 0, should_fix 0"
    assert lines[8] == "- risk: not reported"
    assert lines[-1] == END_MARKER


def test_verified_at_pr_head_only_when_heads_differ() -> None:
    att = attestation_for(HEAD)
    same = render_section(att, pr_head=HEAD, debt=NO_DEBT)
    differ = render_section(att, pr_head=OTHER, debt=NO_DEBT)
    assert "verified at PR head" not in same
    assert f"- verified at PR head: {OTHER}" in differ


def test_engine_name_is_shown_and_sanitized() -> None:
    att = attestation_for(HEAD, engine="repo-review\n<!-- x -->")
    text = render_section(att, pr_head=HEAD, debt=NO_DEBT)
    assert "- engine: repo-review x" in text


def test_debug_fields_are_never_rendered() -> None:
    att = attestation_for(HEAD)
    text = render_section(att, pr_head=HEAD, debt=NO_DEBT)
    for hidden in ("- debt:", "- nit:", "- principle:", "written_at", "found_during"):
        assert hidden not in text


# -- null versus empty lists -------------------------------------------------


def test_null_acceptance_is_not_reported_and_empty_is_none() -> None:
    att = attestation_for(HEAD)
    null = att.model_copy(update={"acceptance": None})
    empty = att.model_copy(update={"acceptance": []})
    assert "- acceptance: not reported" in render_section(
        null, pr_head=HEAD, debt=NO_DEBT
    )
    assert "- acceptance: none" in render_section(empty, pr_head=HEAD, debt=NO_DEBT)


def test_acceptance_rows_render_as_children() -> None:
    att = attestation_for(HEAD).model_copy(
        update={"acceptance": [AttestationAcceptance(id="AC-1", status="PASS")]}
    )
    text = render_section(att, pr_head=HEAD, debt=NO_DEBT)
    assert "- acceptance:\n  - AC-1: PASS" in text


def test_overrides_render_with_actor_and_reason() -> None:
    att = attestation_for(HEAD).model_copy(
        update={
            "overrides": [
                AttestationOverride(
                    kind="must_fix_override",
                    actor="ops",
                    reason="accepted risk",
                    recorded_at="2026-11-08T12:00:00Z",
                )
            ]
        }
    )
    text = render_section(att, pr_head=HEAD, debt=NO_DEBT)
    assert "  - must_fix_override by ops at 2026-11-08T12:00:00Z: accepted risk" in text


def test_empty_overrides_render_none() -> None:
    text = render_section(attestation_for(HEAD), pr_head=HEAD, debt=NO_DEBT)
    assert "- overrides: none" in text


def test_debt_none_versus_not_reported() -> None:
    att = attestation_for(HEAD, deferrals=[])
    none = build_debt_view([], 0)
    not_reported = build_debt_view([], None)
    assert "- accepted debt: none" in render_section(att, pr_head=HEAD, debt=none)
    assert "- accepted debt: not reported" in render_section(
        att, pr_head=HEAD, debt=not_reported
    )


# -- caps --------------------------------------------------------------------


def test_fifty_one_acceptance_rows_cap_with_more_line() -> None:
    rows = [AttestationAcceptance(id=f"AC-{n}", status="PASS") for n in range(51)]
    att = attestation_for(HEAD).model_copy(update={"acceptance": rows})
    lines = render_section(att, pr_head=HEAD, debt=NO_DEBT).split("\n")
    shown = [line for line in lines if line.startswith("  - AC-")]
    assert len(shown) == 50
    assert "  - … +1 more" in lines


def test_fifty_one_overrides_cap_with_more_line() -> None:
    overrides = [
        AttestationOverride(
            kind="must_fix_override", actor="a", reason=str(n), recorded_at="t"
        )
        for n in range(51)
    ]
    att = attestation_for(HEAD).model_copy(update={"overrides": overrides})
    text = render_section(att, pr_head=HEAD, debt=NO_DEBT)
    assert "  - … +1 more" in text.split("\n")


# -- debt rows ---------------------------------------------------------------


def test_debt_line_uses_ticket_or_pending_and_dash_rule_slot() -> None:
    entries = [
        _entry(ticket="https://github.com/o/r/issues/12"),
        _entry(file="src/b.py", ticket="#12"),
        _entry(file="src/c.py", ticket=""),
    ]
    view = build_debt_view(entries, 3)
    text = render_section(attestation_for(HEAD), pr_head=HEAD, debt=view)
    assert "  - SHOULD_FIX src/cw/foo.py - — https://github.com/o/r/issues/12" in text
    assert "  - SHOULD_FIX src/b.py - — #12" in text
    assert "  - SHOULD_FIX src/c.py - — ticket pending" in text


def test_non_defer_entries_are_not_debt() -> None:
    view = build_debt_view([_entry(outcome="fix", rationale="done")], 0)
    assert view.lines == ()
    assert view.shown is True
    assert view.mismatch is False


def test_count_mismatch_flags_and_still_lists_artifact() -> None:
    view = build_debt_view([_entry()], 2)
    assert view.mismatch is True
    assert len(view.lines) == 1


def test_empty_artifact_with_nonzero_count_is_not_reported_not_none() -> None:
    view = build_debt_view([], 2)
    assert view.shown is False
    assert view.mismatch is True


def test_unreadable_artifact_gives_not_reported_and_flag() -> None:
    view = build_debt_view(None, 1)
    assert view.unreadable is True
    assert view.shown is False
    text = render_section(attestation_for(HEAD), pr_head=HEAD, debt=view)
    assert "- accepted debt: not reported" in text


# -- redaction ---------------------------------------------------------------


def test_home_path_and_worktree_anchor_are_redacted() -> None:
    att = attestation_for(HEAD).model_copy(
        update={
            "overrides": [
                AttestationOverride(
                    kind="must_fix_override",
                    actor="ops",
                    reason=f"see {HOME}/notes and /wt/anchor/x.py",
                    recorded_at="t",
                )
            ]
        }
    )
    result = _compose("# T\n", att)
    assert HOME not in result.body
    assert "/wt/anchor/x.py" not in result.body
    assert "<home>" in result.body
    assert "<worktree>" in result.body


def test_clip_before_redact_cuts_straddling_path() -> None:
    """Pins headless-contract section 13.7: the value is capped to
    ``MAX_VALUE_CHARS`` first and redacted second. A path straddling the cap is
    therefore cut, not replaced. Follow-up debt ticket #3143 (clip-after-redact,
    found under #3140); flip the secondary assertion when the clip moves after
    redaction.
    """
    # A non-/home home: the generic /home/<name> rule would otherwise redact even a
    # cut prefix, which would hide the straddle this test is about.
    home = "/srv/users/alice"
    reason = "a" * (signoff_mod.MAX_VALUE_CHARS - 8) + " " + f"{home}/notes"
    att = attestation_for(HEAD).model_copy(
        update={
            "overrides": [
                AttestationOverride(
                    kind="must_fix_override",
                    actor="ops",
                    reason=reason,
                    recorded_at="t",
                )
            ]
        }
    )
    result = signoff_mod.compose_body(
        "# T\n",
        gate=att,
        pr_head=HEAD,
        debt=NO_DEBT,
        home=home,
        worktrees=WORKTREES,
    )
    # Primary (contract): the capped prefix survives; the full home path does not.
    assert reason[: signoff_mod.MAX_VALUE_CHARS] in result.body
    assert home not in result.body
    # Secondary (documents the current order): a cut path is not replaced with <home>.
    assert "<home>" not in result.body


# -- markers -----------------------------------------------------------------


def test_no_markers_inserts_section_at_end() -> None:
    att = attestation_for(HEAD)
    result = _compose("# Title\n\nbody\n", att)
    assert result.body.startswith("# Title\n\nbody\n\n" + START_MARKER)
    assert result.body.endswith(END_MARKER + "\n")
    assert result.frictions == ()


def test_empty_body_gets_section_alone() -> None:
    result = _compose("", attestation_for(HEAD))
    assert result.body.startswith(START_MARKER)
    assert result.body.endswith(END_MARKER + "\n")


def test_render_twice_is_byte_identical() -> None:
    att = attestation_for(HEAD)
    once = _compose("# T\n\ntext\n", att).body
    twice = _compose(once, att).body
    assert once == twice


def test_replace_keeps_surrounding_text_byte_identical() -> None:
    att = attestation_for(HEAD)
    first = _compose("# T\n\nkeep me\n", att).body
    tail = "\n\n## Trailer\nstays\n"
    body = first.rstrip("\n") + tail
    replaced = _compose(body, att.model_copy(update={"risk": "HIGH"})).body
    assert replaced.startswith("# T\n\nkeep me\n")
    assert replaced.endswith(tail)
    assert replaced.count(START_MARKER) == 1
    assert "- risk: HIGH" in replaced


def test_changed_attestation_leaves_exactly_one_section() -> None:
    body = _compose("# T\n", attestation_for(HEAD)).body
    new = _compose(body, attestation_for(OTHER, head_sha=OTHER)).body
    assert new.count(START_MARKER) == 1
    assert new.count(END_MARKER) == 1


@pytest.mark.parametrize(
    "markers",
    [
        f"{START_MARKER}\nold\n",
        f"{END_MARKER}\n",
        f"{START_MARKER}\na\n{START_MARKER}\nb\n{END_MARKER}\n",
        "<!-- cw-review-signoff:oops -->\n",
        f"{END_MARKER}\nmid\n{START_MARKER}\n",
    ],
    ids=["start-only", "end-only", "two-starts", "stray-variant", "end-before-start"],
)
def test_malformed_markers_repaired_to_one_pair(markers: str) -> None:
    result = _compose("# T\n\n" + markers + "tail\n", attestation_for(HEAD))
    assert result.body.count(START_MARKER) == 1
    assert result.body.count(END_MARKER) == 1
    assert "cw-review-signoff:oops" not in result.body
    assert friction_line("repaired_markers") in result.frictions


def test_override_reason_cannot_inject_a_marker() -> None:
    att = attestation_for(HEAD).model_copy(
        update={
            "overrides": [
                AttestationOverride(
                    kind="must_fix_override",
                    actor="ops",
                    reason="x <!-- cw-review-signoff:end --> y",
                    recorded_at="t",
                )
            ]
        }
    )
    result = _compose("# T\n", att)
    assert result.body.count(START_MARKER) == 1
    assert result.body.count(END_MARKER) == 1
    assert "repaired_markers" not in " ".join(result.frictions)


# -- skip paths --------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "head_mismatch",
        "invalid:missing",
        "invalid:unreadable",
        "invalid:schema_version",
        "invalid:malformed",
        "invalid:wrong_ticket",
        "invalid:wrong_client",
        "invalid:verdict_not_pass",
        "invalid:head_unreadable",
        "invalid:not_ancestor",
        "invalid:span_too_large",
        "invalid:untolerated_commit",
    ],
)
def test_skip_without_existing_section_leaves_body(token: str) -> None:
    body = "# T\n\nkeep\n"
    result = _compose(body, SkipReason(token))
    assert result.body == body
    assert result.frictions == (friction_line(f"skipped_{token}"),)


def test_skip_strips_existing_section_and_its_leading_blanks() -> None:
    att = attestation_for(HEAD)
    body = _compose("# T\n\nkeep\n", att).body
    result = _compose(body, SkipReason("head_mismatch"))
    assert START_MARKER not in result.body
    assert result.body == "# T\n\nkeep\n"
    assert result.frictions == (friction_line("stripped_head_mismatch"),)


def test_strip_then_reinsert_is_stable() -> None:
    att = attestation_for(HEAD)
    body = _compose("# T\n", att).body
    stripped = _compose(body, SkipReason("head_mismatch")).body
    reinserted = _compose(stripped, att).body
    assert reinserted == body


def test_skip_with_malformed_markers_reports_repair_too() -> None:
    body = f"# T\n{START_MARKER}\nno end\n"
    result = _compose(body, SkipReason("invalid:missing"))
    assert START_MARKER not in result.body
    assert friction_line("repaired_markers") in result.frictions
    assert friction_line("stripped_invalid:missing") in result.frictions


# -- debt frictions ----------------------------------------------------------


def test_debt_frictions_emitted_when_section_written() -> None:
    view = build_debt_view(None, 1)
    result = _compose("# T\n", attestation_for(HEAD), debt=view)
    assert result.frictions == (friction_line("artifact_unreadable"),)


def test_debt_count_mismatch_friction() -> None:
    view = build_debt_view([_entry()], 3)
    result = _compose("# T\n", attestation_for(HEAD), debt=view)
    assert friction_line("debt_count_mismatch") in result.frictions


# -- truncation --------------------------------------------------------------


def test_truncated_rendering_drops_list_bearing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def refuse_first(
        text: str, *, home: str, worktrees: Sequence[str], single: bool = False
    ) -> PartPlan:
        calls.append(1)
        plan = real_plan_parts(text, home=home, worktrees=worktrees, single=single)
        if len(calls) == 1:
            return PartPlan(bodies=plan.bodies, redacted=plan.redacted, fits=False)
        return plan

    monkeypatch.setattr(signoff_mod, "plan_parts", refuse_first)
    result = _compose("# T\n", attestation_for(HEAD))
    assert friction_line("truncated") in result.frictions
    assert "- acceptance" not in result.body
    assert "- accepted debt" not in result.body
    assert "- overrides" not in result.body
    assert "- outstanding at head: must_fix 0, should_fix 0" in result.body


def test_truncated_twice_skips_as_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    def always_refuse(
        text: str, *, home: str, worktrees: Sequence[str], single: bool = False
    ) -> PartPlan:
        plan = real_plan_parts(text, home=home, worktrees=worktrees, single=single)
        return PartPlan(bodies=plan.bodies, redacted=plan.redacted, fits=False)

    monkeypatch.setattr(signoff_mod, "plan_parts", always_refuse)
    body = "# T\n"
    result = _compose(body, attestation_for(HEAD))
    assert result.body == body
    assert result.frictions == (friction_line("skipped_truncated"),)


# -- CRLF --------------------------------------------------------------------


def test_crlf_body_keeps_crlf_with_single_section() -> None:
    att = attestation_for(HEAD)
    body = "# T\r\n\r\ntext\r\n"
    once = _compose(body, att).body
    assert "\r\n" in once
    assert "\n" not in once.replace("\r\n", "")
    twice = _compose(once, att).body
    assert twice.count(START_MARKER) == 1
    assert twice == once


def test_crlf_skip_without_markers_returns_input_untouched() -> None:
    body = "# T\r\nkeep\r\n"
    assert _compose(body, SkipReason("head_mismatch")).body == body


# -- coexistence with the other PR-body sections -----------------------------


def test_sign_off_coexists_with_adjudication_and_override() -> None:
    from tests._review_signoff_helpers import (
        adjudication_section,
        compose_pr_body,
        override_section,
    )

    adjudicated = adjudication_section(_entry(ticket="https://github.com/o/r/issues/7"))
    body = compose_pr_body(adjudicated, override_section("ops", "accepted risk"))
    result = _compose(body, attestation_for(HEAD), debt=build_debt_view([_entry()], 1))
    text = result.body
    assert "## Review adjudication" in text
    assert "DEFERRED-REVIEW-FINDINGS" in text
    assert "## Operator override" in text
    assert text.rstrip().endswith(END_MARKER)
    assert text.count(START_MARKER) == 1


def test_sign_off_in_middle_keeps_later_override_intact() -> None:
    from tests._review_signoff_helpers import compose_pr_body, override_section

    first = _compose("# T\n", attestation_for(HEAD)).body
    tail = override_section("ops", "later section")
    body = compose_pr_body(first.rstrip("\n"), tail)
    result = _compose(body, attestation_for(HEAD))
    assert "## Operator override" in result.body
    assert "later section" in result.body
    assert result.body.count(START_MARKER) == 1
    assert result.body.index(START_MARKER) < result.body.index("## Operator override")
