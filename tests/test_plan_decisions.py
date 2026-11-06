"""``parse_plan_decisions``: the plan's decided sections as row entries (#2984).

The plan stage records ``## Adopted Assumptions``, ``## Non-Goals`` and
``## Decisions`` on the dev-queue row; this parser is what turns each section
into one ``(kind, text)`` item per top-level list item.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.plan_decisions import PlanDecision, parse_plan_decisions

# This ticket's own approved-plan sections, verbatim (`.cw/` is untracked).
_PLAN = Path(__file__).parent / "fixtures" / "plan_2984_decided_sections.md"


def _texts(plan: str, kind: str) -> list[str]:
    return [item.text for item in parse_plan_decisions(plan) if item.kind == kind]


class TestSections:
    def test_each_heading_maps_to_its_kind_in_document_order(self) -> None:
        plan = (
            "# Plan\n\n"
            "## Summary\n\nNot a decision.\n\n"
            "## Adopted Assumptions\n\n- assume A\n- assume B\n\n"
            "## Non-Goals\n\n- do not X\n\n"
            "## Decisions\n\n1. decided D\n2. decided E\n\n"
            "## Ambiguities\n\nNO_AMBIGUITIES\n"
        )
        assert parse_plan_decisions(plan) == [
            PlanDecision("adopted_assumption", "assume A"),
            PlanDecision("adopted_assumption", "assume B"),
            PlanDecision("non_goal", "do not X"),
            PlanDecision("plan_decision", "decided D"),
            PlanDecision("plan_decision", "decided E"),
        ]

    def test_continuation_lines_are_kept_verbatim_and_marker_stripped(
        self,
    ) -> None:
        plan = (
            "## Non-Goals\n\n"
            "- **Do not add a store.** Rationale here,\n"
            "  continued on an indented line.\n"
            "  - a nested bullet stays with its parent\n"
            "* second item\n"
        )
        assert _texts(plan, "non_goal") == [
            "**Do not add a store.** Rationale here,\n"
            "  continued on an indented line.\n"
            "  - a nested bullet stays with its parent",
            "second item",
        ]

    def test_indented_top_level_list_uses_the_minimum_indent(self) -> None:
        plan = "## Decisions\n\n  - first\n    - nested\n  - second\n"
        assert _texts(plan, "plan_decision") == ["first\n    - nested", "second"]

    def test_unbulleted_body_is_one_entry(self) -> None:
        plan = "## Decisions\n\nWe keep the store.\nIt is append-only.\n\n## Next\n"
        assert _texts(plan, "plan_decision") == [
            "We keep the store.\nIt is append-only."
        ]

    @pytest.mark.parametrize(
        "body",
        ["None.", "None", "N/A", "N/A — nothing adopted.", "NO_AMBIGUITIES", "- None."],
    )
    def test_inert_bodies_yield_nothing(self, body: str) -> None:
        assert parse_plan_decisions(f"## Adopted Assumptions\n\n{body}\n") == []

    def test_fenced_illustrations_are_ignored(self) -> None:
        plan = (
            "## Summary\n\n```markdown\n## Non-Goals\n\n- fenced, not real\n```\n\n"
            "## Non-Goals\n\n- real\n~~~\n- also fenced\n~~~\n"
        )
        assert _texts(plan, "non_goal") == ["real"]

    @pytest.mark.parametrize(
        "heading",
        [
            "## non-goals",
            "## Non Goals",
            "## NON-GOALS",
            "## Non-goals:",
            "##  Non-Goals  ",
        ],
    )
    def test_heading_match_is_case_insensitive_and_accepts_spacing(
        self, heading: str
    ) -> None:
        assert _texts(f"{heading}\n\n- x\n", "non_goal") == ["x"]

    def test_deeper_heading_starts_an_item_and_does_not_end_the_section(
        self,
    ) -> None:
        plan = "## Decisions\n\n- one\n\n### Detail\n\n- two\n\n## Other\n\n- no\n"
        assert _texts(plan, "plan_decision") == ["one", "### Detail", "two"]

    def test_headed_paragraphs_are_one_entry_per_heading(self) -> None:
        plan = "## Decisions\n\n### D1\nUse X.\n\n### D2\nKeep Y.\n"
        assert _texts(plan, "plan_decision") == ["### D1\nUse X.", "### D2\nKeep Y."]

    def test_level_three_heading_is_not_a_decided_section(self) -> None:
        assert parse_plan_decisions("### Decisions\n\n- not a section\n") == []

    def test_missing_sections_yield_nothing(self) -> None:
        assert parse_plan_decisions("# Plan\n\n## Summary\n\n- a bullet\n") == []
        assert parse_plan_decisions("") == []

    def test_empty_section_yields_nothing(self) -> None:
        assert parse_plan_decisions("## Non-Goals\n\n## Decisions\n") == []

    def test_preamble_before_the_first_bullet_is_kept_as_an_entry(self) -> None:
        plan = "## Decisions\n\nResolved by the operator:\n\n- keep X\n"
        assert _texts(plan, "plan_decision") == ["Resolved by the operator:", "keep X"]


class TestThisPlan:
    def test_own_non_goals_and_adopted_assumptions(self) -> None:
        items = parse_plan_decisions(_PLAN.read_text(encoding="utf-8"))
        non_goals = [i.text for i in items if i.kind == "non_goal"]
        adopted = [i.text for i in items if i.kind == "adopted_assumption"]
        assert len(non_goals) == 13
        assert non_goals[0].startswith("Do not add a second constraint store")
        assert non_goals[-1] == (
            "Do not mirror `plan_decisions` into `.claude/scripts/`. Only `cw`"
            " consumes it."
        )
        assert len(adopted) == 15
        assert adopted[0].startswith("**`## Decisions` is recorded as its own kind")
        # The nested sub-bullets of the trust-boundary entry stay with it.
        trust = next(text for text in adopted if text.startswith("**Trust boundary"))
        assert "Why not a fingerprint check" in trust
        assert "Why not `purpose`" in trust
        # `## Ambiguities` is NO_AMBIGUITIES and is not a decided section.
        assert all(i.kind != "plan_decision" for i in items)
