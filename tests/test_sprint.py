"""Tests for cw.sprint — RFC parse/validate, plan build, plan apply."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from cw import sprint
from cw.exceptions import RfcContractError, SprintApplyError
from cw.sprint import (
    AppliedBuildout,
    BuildoutConfig,
    BuildoutPlan,
    GhSurface,
    apply_plan,
    build_plan,
    load_buildout_config,
    load_rfc_text,
    parse_rfc,
)
from tests.conftest import _write_project_config_yaml

FIXTURES = Path(__file__).parent / "fixtures"

MINIMAL_RFC = """\
# RFC 0011 — Availability- & Counterparty-Aware Holding

## Summary

Body.

## Design

### Epic I — Availability-aware holding (inward)

Hold work when the environment cannot carry it.

## Phasing

| Wave | Track A |
|------|---------|
| 0 | S1 |

## Resolved decisions

- **D-S1 — Counterparty derivation.** Derive at pr_hydrate, no stored field.
- **D-A1 — Park-class shape.** New blocker.reason value.

## Tickets

### S1 — counterparty axis + self-identity

- **Epic:** none
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** none
- **Context:** The shared seam both epics build on.
- **Scope:** D-S1
- **Acceptance:**
  - Counterparty resolves to self when no PR exists.

### A1 — park class (keystone)

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1
- **Context:** The keystone park class.
- **Scope:** D-A1
- **Acceptance:**
  - A held task routes to BLOCKED_ON_USER.
  - No new QueueItemStatus is introduced.

## References

- `src/cw/pr_hydrate.py:257` — where counterparty derivation lands

## Issues

Issues: _(filled by `/sprint-buildout`)_
"""


def test_parse_rfc_extracts_number_and_title() -> None:
    doc = parse_rfc(MINIMAL_RFC)
    assert doc.number == "0011"
    assert doc.title == "Availability- & Counterparty-Aware Holding"


def test_parse_rfc_extracts_epics() -> None:
    doc = parse_rfc(MINIMAL_RFC)
    assert [e.key for e in doc.epics] == ["I"]
    assert doc.epics[0].name == "Availability-aware holding (inward)"
    assert "Hold work when the environment" in doc.epics[0].intent


def test_parse_rfc_extracts_tickets_in_document_order() -> None:
    doc = parse_rfc(MINIMAL_RFC)
    assert [t.code for t in doc.tickets] == ["S1", "A1"]

    s1, a1 = doc.tickets
    assert s1.name == "counterparty axis + self-identity"
    assert s1.epic is None
    assert s1.wave == 0
    assert s1.sprint == 0
    assert s1.depends_on == []
    assert s1.context == "The shared seam both epics build on."
    assert s1.scope == ["D-S1"]
    assert s1.acceptance == ["Counterparty resolves to self when no PR exists."]

    assert a1.name == "park class (keystone)"
    assert a1.epic == "I"
    assert a1.depends_on == ["S1"]
    assert a1.context == "The keystone park class."
    assert len(a1.acceptance) == 2


def test_parse_rfc_expands_scope_citations_from_resolved_decisions() -> None:
    doc = parse_rfc(MINIMAL_RFC)
    assert doc.decisions["D-S1"].startswith("**D-S1 — Counterparty derivation.**")


def test_parse_rfc_extracts_references() -> None:
    doc = parse_rfc(MINIMAL_RFC)
    assert doc.references == ["src/cw/pr_hydrate.py:257"]


@pytest.mark.parametrize(
    "heading",
    ["## Tickets", "## Design", "## Resolved decisions", "## References"],
)
def test_parse_rfc_refuses_rfc_missing_a_required_section(heading: str) -> None:
    mangled = MINIMAL_RFC.replace(heading, "## Something Else")
    with pytest.raises(RfcContractError, match=f"missing section: {heading}"):
        parse_rfc(mangled)


def test_parse_rfc_refuses_ticket_missing_a_required_field() -> None:
    mangled = MINIMAL_RFC.replace(
        "- **Acceptance:**\n  - Counterparty resolves to self when no PR exists.\n", ""
    )
    with pytest.raises(RfcContractError, match="ticket S1: missing field: Acceptance"):
        parse_rfc(mangled)


def test_parse_rfc_refuses_scope_citing_an_undefined_decision() -> None:
    mangled = MINIMAL_RFC.replace("- **Scope:** D-A1", "- **Scope:** D-NOPE")
    with pytest.raises(
        RfcContractError, match="ticket A1: Scope cites undefined decision: D-NOPE"
    ):
        parse_rfc(mangled)


def test_parse_rfc_refuses_depends_on_an_unknown_ticket() -> None:
    mangled = MINIMAL_RFC.replace("- **Depends on:** S1", "- **Depends on:** ZZ9")
    with pytest.raises(
        RfcContractError, match="ticket A1: Depends on unknown ticket: ZZ9"
    ):
        parse_rfc(mangled)


def test_parse_rfc_refuses_ticket_naming_an_undefined_epic() -> None:
    mangled = MINIMAL_RFC.replace("- **Epic:** I\n", "- **Epic:** IX\n")
    with pytest.raises(RfcContractError, match="ticket A1: unknown epic: IX"):
        parse_rfc(mangled)


def test_parse_rfc_refuses_a_hard_wrapped_field_continuation_line() -> None:
    """A field value split across two physical lines must be a loud refusal,
    not a silent drop — ``_FIELD_RE`` only ever captures the first line."""
    mangled = MINIMAL_RFC.replace(
        "- **Context:** The shared seam both epics build on.",
        "- **Context:** The shared seam both epics\nbuild on.",
    )
    with pytest.raises(
        RfcContractError,
        match=r"ticket S1: unparseable line \(fields must be single-line\): build on\.",
    ):
        parse_rfc(mangled)


def test_load_rfc_text_prefers_origin_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, b"origin/main content", b"")

    monkeypatch.setattr(sprint._sp, "run", fake_run)
    assert load_rfc_text("docs/rfcs/0011-x.md", tmp_path) == "origin/main content"


def test_load_rfc_text_falls_back_to_the_working_tree_on_a_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent from origin/main (e.g. a brand-new, unmerged RFC) -> read from disk."""
    rfc = tmp_path / "docs" / "rfcs" / "0011-x.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text("working tree content", encoding="utf-8")

    def fake_run(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            cmd, 1, b"", b"fatal: path not in origin/main"
        )

    monkeypatch.setattr(sprint._sp, "run", fake_run)
    assert load_rfc_text("docs/rfcs/0011-x.md", tmp_path) == "working tree content"


def test_load_rfc_text_falls_back_to_the_working_tree_on_a_git_show_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """git binary missing/hung (OSError/TimeoutExpired) -> same fallback as a miss."""
    rfc = tmp_path / "docs" / "rfcs" / "0011-x.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text("working tree content", encoding="utf-8")

    def fake_run(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        msg = "git not found"
        raise OSError(msg)

    monkeypatch.setattr(sprint._sp, "run", fake_run)
    assert load_rfc_text("docs/rfcs/0011-x.md", tmp_path) == "working tree content"


def test_parse_rfc_refuses_a_mangled_title_line() -> None:
    """The `# RFC NNNN — <title>` line is the sole source of ``doc.number``/
    ``doc.title``; if it doesn't match, refuse with the title-parse message."""
    mangled = MINIMAL_RFC.replace(
        "# RFC 0011 — Availability- & Counterparty-Aware Holding",
        "# Not An RFC Title",
    )
    with pytest.raises(
        RfcContractError, match=r"missing section: # RFC NNNN — <title>"
    ):
        parse_rfc(mangled)


def test_parse_rfc_refuses_a_non_integer_wave_field() -> None:
    """``_parse_int_field``'s except ValueError branch must name the offending
    ticket, key, and raw value verbatim."""
    mangled = MINIMAL_RFC.replace("- **Wave:** 0", "- **Wave:** abc")
    with pytest.raises(
        RfcContractError, match=r"ticket S1: Wave must be an integer, got: 'abc'"
    ):
        parse_rfc(mangled)


def test_parse_rfc_refuses_acceptance_header_with_zero_bullets() -> None:
    """Distinct from the missing-field case: the ``- **Acceptance:**`` header
    line is present, but its bullet lines are stripped — this must trip
    ``_acceptance_bullets``'s raise, not ``_ticket_fields``'s."""
    mangled = MINIMAL_RFC.replace(
        "- **Acceptance:**\n  - Counterparty resolves to self when no PR exists.\n",
        "- **Acceptance:**\n",
    )
    with pytest.raises(RfcContractError, match="ticket S1: missing field: Acceptance"):
        parse_rfc(mangled)


CONFIG_YAML = """\
sprint_buildout:
  milestone:
    title_pattern: "v{version} — {rfc_title}"
  epic:
    title_pattern: "epic: {name}"
    labels: []
    children_marker: "<!-- children -->"
  ticket:
    title_pattern: "RFC {rfc_num} {code} — {name}"
    labels: [feature]
    footer_pattern: "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint})"
    footer_epic_clause: ", Epic #{epic}"
"""


def test_load_buildout_config_reads_the_sprint_buildout_block(tmp_path: Path) -> None:
    _write_project_config_yaml(tmp_path, CONFIG_YAML)
    cfg = load_buildout_config(tmp_path)
    assert cfg.ticket_labels == ["feature"]
    assert cfg.epic_labels == []
    assert cfg.children_marker == "<!-- children -->"
    assert cfg.notion is None


def test_load_buildout_config_refuses_when_the_block_is_absent(tmp_path: Path) -> None:
    _write_project_config_yaml(
        tmp_path, "tracking:\n  primary:\n    system: github-issues\n"
    )
    with pytest.raises(RfcContractError, match="missing sprint_buildout block"):
        load_buildout_config(tmp_path)


def _config() -> BuildoutConfig:
    """Build from the nested YAML shape — the model itself is flat, so this must
    go through from_block(), not model_validate()."""
    return BuildoutConfig.from_block(
        {
            "milestone": {"title_pattern": "v{version} — {rfc_title}"},
            "epic": {
                "title_pattern": "epic: {name}",
                "labels": [],
                "children_marker": "<!-- children -->",
            },
            "ticket": {
                "title_pattern": "RFC {rfc_num} {code} — {name}",
                "labels": ["feature"],
                "footer_pattern": "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint})",
                "footer_epic_clause": ", Epic #{epic}",
            },
        }
    )


def test_build_plan_renders_the_milestone_title() -> None:
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    assert (
        plan.milestone_title == "v1.20.0 — Availability- & Counterparty-Aware Holding"
    )


def test_build_plan_renders_epic_titles_and_embeds_the_children_marker() -> None:
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    assert [e.title for e in plan.epics] == [
        "epic: Availability-aware holding (inward)"
    ]
    # The marker is templated in from the start so the checklist backfill is a
    # marker replacement, not a fragile string-surgery pass.
    assert "<!-- children -->" in plan.epics[0].body


def test_build_plan_renders_ticket_titles_and_bodies() -> None:
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    titles = [t.title for t in plan.tickets]
    assert titles == [
        "RFC 0011 S1 — counterparty axis + self-identity",
        "RFC 0011 A1 — park class (keystone)",
    ]

    a1 = plan.tickets[1]
    assert a1.labels == ["feature"]
    assert "## Context" in a1.body
    assert "The keystone park class." in a1.body
    # Scope cites D-A1, so the decision's full text is transcribed into the body.
    assert "Park-class shape" in a1.body
    assert "## Acceptance" in a1.body
    assert "- [ ] A held task routes to BLOCKED_ON_USER." in a1.body
    assert "## Dependencies" in a1.body
    assert "S1" in a1.body


def test_build_plan_omits_the_epic_clause_for_an_epic_less_ticket() -> None:
    """S1 has no epic. Its footer must not carry an `Epic #—` placeholder —
    the clause is omitted entirely, not rendered with an em-dash default."""
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    s1, a1 = plan.tickets

    assert "Epic #" not in s1.body
    assert "Part of RFC 0011 Wave 0 (Sprint 0)" in s1.body
    assert "Epic #I" in a1.body


def test_build_plan_maps_sprints_to_ticket_codes() -> None:
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    assert plan.sprint_map == {0: ["S1"], 1: ["A1"]}


def test_build_plan_maps_epics_to_child_ticket_codes() -> None:
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    assert plan.epic_children == {"I": ["A1"]}


def test_load_buildout_config_refuses_when_a_required_key_is_missing_from_a_section(
    tmp_path: Path,
) -> None:
    """`epic:` is present, but `children_marker` is missing — this must raise the
    same guided RfcContractError as a missing section, not a bare KeyError."""
    _write_project_config_yaml(
        tmp_path,
        """\
sprint_buildout:
  milestone:
    title_pattern: "v{version} — {rfc_title}"
  epic:
    title_pattern: "epic: {name}"
  ticket:
    title_pattern: "RFC {rfc_num} {code} — {name}"
    footer_pattern: "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint})"
    footer_epic_clause: ", Epic #{epic}"
""",
    )
    with pytest.raises(
        RfcContractError,
        match=r"sprint_buildout\.epic: missing required key: children_marker",
    ):
        load_buildout_config(tmp_path)


def test_load_buildout_config_refuses_a_malformed_notion_value(tmp_path: Path) -> None:
    """A present-but-non-mapping `notion:` is a config typo, not an opt-out —
    it must refuse, not silently degrade to "Notion phase skipped"."""
    _write_project_config_yaml(
        tmp_path,
        CONFIG_YAML + "  notion: true\n",
    )
    with pytest.raises(RfcContractError, match=r"sprint_buildout\.notion: malformed"):
        load_buildout_config(tmp_path)


def test_load_buildout_config_parses_a_present_notion_block(tmp_path: Path) -> None:
    _write_project_config_yaml(
        tmp_path,
        CONFIG_YAML
        + """\
  notion:
    data_source: "collection://abc"
    project_page: "def"
    sprint_page_properties:
      Type: Sprint
""",
    )
    cfg = load_buildout_config(tmp_path)
    assert cfg.notion is not None
    assert cfg.notion.data_source == "collection://abc"
    assert cfg.notion.project_page == "def"
    assert cfg.notion.sprint_page_properties == {"Type": "Sprint"}


def test_rfc_0011_fixture_reproduces_the_real_buildout() -> None:
    """The acceptance bar: a conforming RFC 0011 yields the block that was filed."""
    doc = parse_rfc((FIXTURES / "rfc-0011-tickets.md").read_text(encoding="utf-8"))
    plan = build_plan(doc, _config(), version="1.20.0")

    assert plan.milestone_title == (
        "v1.20.0 — Availability- & Counterparty-Aware Holding"
    )
    assert [e.code for e in plan.epics] == ["I", "II"]
    assert len(plan.tickets) == 13

    assert plan.sprint_map[0] == ["S1", "S2"]
    assert sorted(plan.sprint_map[1]) == ["A1", "A2", "A5", "B1", "B2"]
    assert sorted(plan.sprint_map[2]) == ["A3", "A4", "A6", "B3", "B4", "B5"]

    assert sorted(plan.epic_children["I"]) == ["A1", "A2", "A3", "A4", "A5", "A6"]
    assert sorted(plan.epic_children["II"]) == ["B1", "B2", "B3", "B4", "B5"]

    # The dependency the RFC's Phasing table calls out: S2 rides S1.
    s2 = next(t for t in doc.tickets if t.code == "S2")
    assert s2.depends_on == ["S1"]


# --- apply_plan --------------------------------------------------------

TWO_EPIC_RFC = """\
# RFC 0012 — Two-Epic Fixture

## Summary

Body.

## Design

### Epic I — First epic

Intent one.

### Epic II — Second epic

Intent two.

## Phasing

| Wave | Track A |
|------|---------|
| 0 | B1 |

## Resolved decisions

- **D-B1 — Some decision.** Some text.

## Tickets

### B1 — ticket under epic two

- **Epic:** II
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** none
- **Context:** Some context for B1.
- **Scope:** D-B1
- **Acceptance:**
  - Some acceptance bullet.

## References

- `src/cw/example.py:1` — example reference

## Issues

Issues: _(filled by `/sprint-buildout`)_
"""


def _plan() -> BuildoutPlan:
    """Thin wrapper reusing this file's existing MINIMAL_RFC + _config()
    fixtures, so apply_plan tests aren't re-deriving a plan by hand."""
    return build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")


class FakeGh:
    """Test double for GhSurface. Records call order (title-qualified for
    create_issue, number-qualified for the milestone-scoped lookups) and
    every issue body created/updated, so tests can assert on both call
    sequencing and content without a real `gh` binary.

    ``existing`` seeds titles already filed under the milestone (idempotent
    re-entry); ``milestone_exists`` is deliberately decoupled from it so a
    milestone that already exists but has zero issues filed yet (R6) can be
    modeled distinctly from "milestone + all issues already exist".
    """

    def __init__(
        self,
        *,
        existing: dict[str, int] | None = None,
        milestone_exists: bool = False,
    ) -> None:
        self.existing: dict[str, int] = dict(existing or {})
        self.milestone_exists = milestone_exists
        self.milestone_number = 1
        self.calls: list[str] = []
        self.bodies: dict[int, str] = {}
        self.fail_find_milestone = False
        self.fail_create_milestone = False
        self.fail_milestone_issue_titles = False
        self.fail_create_issue: set[str] = set()
        self._next_number = 100

    def find_milestone(self, title: str) -> tuple[int | None, bool]:
        self.calls.append(f"find_milestone:{title}")
        if self.fail_find_milestone:
            return None, False
        if self.milestone_exists:
            return self.milestone_number, True
        return None, True

    def create_milestone(self, title: str) -> int | None:
        self.calls.append(f"create_milestone:{title}")
        if self.fail_create_milestone:
            return None
        return self.milestone_number

    def milestone_issue_titles(
        self, milestone: int
    ) -> tuple[dict[str, int] | None, bool]:
        self.calls.append(f"milestone_issue_titles:{milestone}")
        if self.fail_milestone_issue_titles:
            return None, False
        return dict(self.existing), True

    def create_issue(
        self, title: str, body: str, *, labels: list[str], milestone: int
    ) -> int | None:
        self.calls.append(f"create_issue:{title}")
        if title in self.fail_create_issue:
            return None
        number = self._next_number
        self._next_number += 1
        self.existing[title] = number
        self.bodies[number] = body
        return number

    def update_issue_body(self, number: int, body: str) -> bool:
        self.calls.append(f"update_issue_body:{number}")
        self.bodies[number] = body
        return True


def test_apply_plan_creates_milestone_then_epics_then_tickets_then_backfills() -> None:
    plan = _plan()
    gh = FakeGh()
    result = apply_plan(plan, client=gh)

    epic_number = result.epic_numbers["I"]
    assert gh.calls == [
        f"find_milestone:{plan.milestone_title}",
        f"create_milestone:{plan.milestone_title}",
        f"milestone_issue_titles:{result.milestone_number}",
        f"create_issue:{plan.epics[0].title}",
        f"create_issue:{plan.tickets[0].title}",
        f"create_issue:{plan.tickets[1].title}",
        f"update_issue_body:{epic_number}",
    ]


def test_apply_plan_backfills_the_children_checklist_into_the_marker() -> None:
    plan = _plan()
    gh = FakeGh()
    result = apply_plan(plan, client=gh)

    epic_number = result.epic_numbers["I"]
    body = gh.bodies[epic_number]
    assert "<!-- children -->" in body
    assert "- [ ] A1" in body


def test_apply_plan_rewrites_the_ticket_footer_with_the_real_epic_number() -> None:
    plan = _plan()
    gh = FakeGh()
    result = apply_plan(plan, client=gh)

    a1_number = result.ticket_numbers["A1"]
    epic_number = result.epic_numbers["I"]
    body = gh.bodies[a1_number]
    assert f"Epic #{epic_number}" in body
    assert "Epic #I" not in body


def test_apply_plan_is_idempotent_and_skips_issues_that_already_exist() -> None:
    plan = _plan()
    existing = {
        plan.epics[0].title: 501,
        plan.tickets[0].title: 502,
        plan.tickets[1].title: 503,
    }
    gh = FakeGh(existing=existing, milestone_exists=True)

    result = apply_plan(plan, client=gh)

    assert result.epic_numbers["I"] == 501
    assert result.ticket_numbers["S1"] == 502
    assert result.ticket_numbers["A1"] == 503
    assert result.created == []
    assert set(result.skipped) == {
        plan.epics[0].title,
        plan.tickets[0].title,
        plan.tickets[1].title,
    }
    assert not any(call.startswith("create_issue") for call in gh.calls)
    assert not any(call.startswith("create_milestone") for call in gh.calls)


def test_apply_plan_raises_when_milestone_creation_fails() -> None:
    plan = _plan()
    gh = FakeGh()
    gh.fail_create_milestone = True

    with pytest.raises(SprintApplyError, match="failed to create milestone"):
        apply_plan(plan, client=gh)


def test_apply_plan_raises_when_the_milestone_lookup_itself_fails() -> None:
    plan = _plan()
    gh = FakeGh()
    gh.fail_find_milestone = True

    with pytest.raises(SprintApplyError, match="failed to look up milestone"):
        apply_plan(plan, client=gh)


def test_apply_plan_raises_when_the_milestone_issue_lookup_fails() -> None:
    plan = _plan()
    gh = FakeGh(milestone_exists=True)
    gh.fail_milestone_issue_titles = True

    with pytest.raises(SprintApplyError, match="failed to list issues"):
        apply_plan(plan, client=gh)


def test_apply_plan_reuses_a_milestone_that_exists_with_zero_issues_yet() -> None:
    plan = _plan()
    gh = FakeGh(milestone_exists=True)

    result = apply_plan(plan, client=gh)

    assert result.milestone_number == gh.milestone_number
    assert not any(call.startswith("create_milestone") for call in gh.calls)
    assert len(result.created) == 3


def test_sprint_apply_error_carries_the_partial_applied_state() -> None:
    plan = _plan()
    gh = FakeGh()
    gh.fail_create_issue = {plan.tickets[1].title}

    with pytest.raises(SprintApplyError) as exc_info:
        apply_plan(plan, client=gh)

    applied = exc_info.value.applied
    assert isinstance(applied, AppliedBuildout)
    assert plan.epics[0].title in applied.created
    assert plan.tickets[0].title in applied.created
    assert plan.tickets[1].title not in applied.created
    assert "I" in applied.epic_numbers


def test_apply_plan_raises_when_epic_creation_fails() -> None:
    plan = _plan()
    gh = FakeGh()
    gh.fail_create_issue = {plan.epics[0].title}

    with pytest.raises(SprintApplyError, match="failed to create issue"):
        apply_plan(plan, client=gh)


def test_apply_plan_is_idempotent_and_skips_an_epic_that_already_exists() -> None:
    plan = _plan()
    gh = FakeGh(existing={plan.epics[0].title: 777}, milestone_exists=True)

    result = apply_plan(plan, client=gh)

    assert result.epic_numbers["I"] == 777
    assert plan.epics[0].title in result.skipped
    assert not any(
        call == f"create_issue:{plan.epics[0].title}" for call in gh.calls
    )
    assert "S1" in result.ticket_numbers
    assert "A1" in result.ticket_numbers


def test_apply_plan_uses_ghclient_by_default_and_forwards_calls_to_cw_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    calls: list[str] = []

    def fake_find_milestone(title: str) -> tuple[int | None, bool]:
        calls.append("find_milestone")
        return None, True

    def fake_create_milestone(title: str) -> int | None:
        calls.append("create_milestone")
        return 1

    def fake_milestone_issue_titles(
        milestone: int,
    ) -> tuple[dict[str, int] | None, bool]:
        calls.append("milestone_issue_titles")
        return {}, True

    def fake_create_issue(
        title: str, body: str, *, labels: list[str], milestone: int
    ) -> int | None:
        calls.append("create_issue")
        return 42

    def fake_update_issue_body(number: int, body: str) -> bool:
        calls.append("update_issue_body")
        return True

    monkeypatch.setattr(sprint.gh, "find_milestone", fake_find_milestone)
    monkeypatch.setattr(sprint.gh, "create_milestone", fake_create_milestone)
    monkeypatch.setattr(
        sprint.gh, "milestone_issue_titles", fake_milestone_issue_titles
    )
    monkeypatch.setattr(sprint.gh, "create_issue", fake_create_issue)
    monkeypatch.setattr(sprint.gh, "update_issue_body", fake_update_issue_body)

    result = apply_plan(plan)

    assert result.milestone_number == 1
    assert calls == [
        "find_milestone",
        "create_milestone",
        "milestone_issue_titles",
        "create_issue",
        "create_issue",
        "create_issue",
        "update_issue_body",
    ]


def test_fake_gh_satisfies_the_ghsurface_protocol() -> None:
    assert isinstance(FakeGh(), GhSurface)


def test_backfill_children_handles_an_epic_with_zero_children() -> None:
    plan = build_plan(parse_rfc(TWO_EPIC_RFC), _config(), version="1.20.0")
    gh = FakeGh()
    applied = AppliedBuildout(milestone_number=1, epic_numbers={"I": 501, "II": 502})

    sprint._backfill_children(plan, applied, gh)

    body = gh.bodies[501]
    assert "<!-- children -->" in body
    assert "(no children)" in body


def test_apply_plan_resolves_epic_ii_refs_without_corruption_from_epic_i() -> None:
    plan = build_plan(parse_rfc(TWO_EPIC_RFC), _config(), version="1.20.0")
    gh = FakeGh()

    result = apply_plan(plan, client=gh)

    epic_ii_number = result.epic_numbers["II"]
    b1_number = result.ticket_numbers["B1"]
    body = gh.bodies[b1_number]
    assert re.search(rf"Epic #{epic_ii_number}(?!\w)", body)
    assert "Epic #II" not in body


def test_apply_plan_leaves_the_epic_less_ticket_footer_epic_free() -> None:
    plan = _plan()
    gh = FakeGh()

    result = apply_plan(plan, client=gh)

    s1_number = result.ticket_numbers["S1"]
    assert "Epic #" not in gh.bodies[s1_number]
