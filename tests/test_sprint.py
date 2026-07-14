"""Tests for cw.sprint — RFC parse/validate, plan build, plan apply."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cw import sprint
from cw.exceptions import RfcContractError
from cw.sprint import (
    BuildoutConfig,
    build_plan,
    load_buildout_config,
    load_rfc_text,
    parse_rfc,
)
from tests.conftest import _write_project_config_yaml

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
