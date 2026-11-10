"""Guard tests: the repo review engine prose agrees with the code (RFC 0017, #3039).

Pure-markdown assertions in the style of ``test_agent_comment_provenance.py``
and ``test_review_convergence_docs.py``. The engine round itself is tested in
Python (``tests/test_review_engine_*.py``); this file pins only what the
prose promises the CLI, so a later edit cannot reorder a step, drop a
fail-closed exit mapping, let the prose drift from ``ENGINE_PREAMBLE``, or
re-introduce a ``reject`` path for an engine finding.

The load-bearing contracts:

- Step 3a.5 is the one separately headed engine branch, and it sequences
  ``converge config``, ``preflight``, ``workspace init``, ``materialize``, an
  inline Read of ``SKILL.md``, ``record``, ``workspace reconcile`` and
  Checkpoint 3a in that order, with no shell-side finding logic;
- the preamble in the prose is byte-equal to ``ENGINE_PREAMBLE``;
- an engine finding is never rejected;
- ``review_engine_modified`` is registered and documented in lockstep;
- the ``operator-settle --outcome defer`` sequence is documented three times.
"""

from __future__ import annotations

import re
from pathlib import Path

from cw.auto_dev_result import KNOWN_BLOCKER_REASONS
from cw.review_engine.constants import (
    ENGINE_PREAMBLE,
    ENGINE_ROLE,
    FRICTION,
    EngineRoundFailure,
)
from tests.conftest import _appendix, _cmd
from tests.test_agent_comment_provenance import _blocker_reason_table, _table_reasons
from tests.test_auto_dev_preflight_resolutions import _after

ROOT = Path(__file__).resolve().parent.parent

REVIEW = "auto-dev-review.md"
REASON = "review_engine_modified"
STEP_HEADING = "### Step 3a.5: Engine round"
CHECKPOINT_HEADING = "### Checkpoint 3a"
ERROR_RULE_ANCHOR = "**The `converge config` error rule (#2787).**"
CLEAN_RE_REVIEW_ANCHOR = "**Clean mode (#2787): the sparse-feedback skip never fires.**"
DEFER_SEQUENCE_DOCS = (
    ".claude/commands/auto-dev-review.md",
    "docs/dispatch-runbook.md",
    ".claude/skills/cw-orchestration/SKILL.md",
)
DEFER_COMMAND = "cw review converge operator-settle"
DEFER_FLAG = "--outcome defer"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _step_section() -> str:
    content = _cmd(REVIEW)
    start = content.index(STEP_HEADING)
    end = content.index(CHECKPOINT_HEADING, start)
    return content[start:end]


def _error_rule_paragraph() -> str:
    content = _cmd(REVIEW)
    start = content.index(ERROR_RULE_ANCHOR)
    return content[start : content.index("\n\n", start)]


def _clean_re_review_paragraph() -> str:
    content = _cmd(REVIEW)
    start = content.index(CLEAN_RE_REVIEW_ANCHOR)
    return content[start : content.index("\n\n", start)]


def _engine_round_paragraph() -> str:
    content = _cmd(REVIEW)
    start = content.index("**Engine round (RFC 0017, #3039).**")
    return content[start : content.index("**Close the reviewer round", start)]


# --- Step 3a.5 ------------------------------------------------------------------


def test_engine_branch_heading_appears_exactly_once() -> None:
    content = _cmd(REVIEW)
    assert content.count(STEP_HEADING) == 1
    assert content.count("### Step 3a.5") == 1
    assert content.index(STEP_HEADING) < content.index(CHECKPOINT_HEADING)


def test_step_calls_each_command_in_order() -> None:
    section = _step_section()
    anchors = (
        "cw review engine preflight",
        "cw review workspace init",
        "cw review engine materialize",
        "Read `<skill_dir>/SKILL.md`",
        "cw review engine record",
        "cw review workspace reconcile",
        "Checkpoint 3a",
        "cw review consolidate",
        "cw review check-voided",
        "cw review converge suppress",
        "cw review adjudicate",
        "cw review converge record-round",
    )
    positions = [section.index(anchor) for anchor in anchors]
    assert positions == sorted(positions), anchors


def test_converge_config_precedes_the_step_in_the_engine_pointer() -> None:
    content = _cmd(REVIEW)
    pointer = _after(content, "**Engine branch (RFC 0017, #3039).**", span=900)
    assert "cw review converge config" in pointer
    assert "Step 3a.5" in pointer
    assert 'skip "Reviewer-set selection" through the "Output contract"' in pointer
    assert content.index("**Clean-mode head (#2787).**") < content.index(
        "**Engine branch (RFC 0017, #3039).**"
    )


def test_step_has_no_shell_side_finding_logic() -> None:
    section = _step_section()
    assert "jq" not in section
    assert "--jq" not in section
    assert "```" not in section


def test_step_maps_exit_codes_to_blockers() -> None:
    section = _step_section()
    preflight = _after(section, "1. **Preflight.**", span=1400)
    assert "Exit 3" in preflight
    assert f'`blocker.reason: "{REASON}"`' in preflight
    assert "`touched`" in preflight
    assert "cw signal-park" in preflight
    assert "appendix" in preflight
    for lead in ("3. **Materialize the skill.**", "5. **Record the round.**"):
        window = _after(section, lead, span=1500)
        assert "Exit 3" in window, lead
        assert '`blocker.reason: "agent_block"`' in window, lead
        assert "`retry_eligible: true`" in window, lead
        assert "Any other non-zero exit is `agent_block`" in window, lead
    assert "`retry_eligible: false`" in section


def test_step_records_the_engine_role_as_the_whole_roster() -> None:
    section = _step_section()
    assert f'--role "{ENGINE_ROLE}"' in section
    assert f'["{ENGINE_ROLE}"]' in section


def test_step_forbids_the_skill_tool_and_reads_skill_md_inline() -> None:
    section = _step_section()
    window = _after(section, "4. **Run the engine inline.**", span=1600)
    assert "Do not call the Skill tool for the engine" in window
    assert "Read `<skill_dir>/SKILL.md`" in window
    assert "`skill_dir` field" in window
    assert "`args_line`" in window
    assert "do not rebuild or edit it" in window


def test_step_quotes_the_engine_preamble_verbatim() -> None:
    assert ENGINE_PREAMBLE in _step_section()
    assert ENGINE_PREAMBLE in _cmd(REVIEW)


def test_args_line_format_is_documented_in_the_contract_doc() -> None:
    contract = _read("docs/headless-contract.md")
    assert "cw-engine: schema_version=<n> head_sha=<40-hex HEAD>" in contract
    assert "summary_path=<absolute path> base_dir=<absolute skill_dir>" in contract


# --- never reject an engine finding ---------------------------------------------


def test_prose_forbids_rejecting_an_engine_finding() -> None:
    paragraph = _engine_round_paragraph()
    assert "Never `reject` an engine finding" in paragraph
    assert 'outcome: "operator_action"' in paragraph
    assert "believe is a false positive" in paragraph
    assert "`non_deferrable: true`" in paragraph
    assert "fingerprint" in paragraph


def test_prose_corrects_engine_route_refusals_and_never_coerces() -> None:
    paragraph = _engine_round_paragraph()
    assert "`engine_route: <code>: <file>: <fingerprint>`" in paragraph
    assert "exits 1" in paragraph
    assert "Never coerce the row" in paragraph


def test_no_engine_prose_names_a_reject_outcome_for_an_engine_finding() -> None:
    texts = [
        _step_section(),
        _engine_round_paragraph(),
        _read("docs/dispatch-runbook.md").split(
            "**Deferring an engine MUST_FIX (RFC 0017, #3039).**", 1
        )[1][:1800],
    ]
    for text in texts:
        for match in re.finditer(r"reject", text):
            context = text[max(0, match.start() - 40) : match.end() + 60].lower()
            assert any(
                negation in context
                for negation in ("never", "refused", "forbidden", "not ")
            ), context
        assert 'outcome: "reject"' not in text


# --- the converge config error rule and the clean-mode re-review ----------------


def test_error_rule_carries_the_engine_clause() -> None:
    paragraph = _error_rule_paragraph()
    assert "**Engine clause (RFC 0017, #3039).**" in paragraph
    assert "non-null `engine`" in paragraph
    assert "`engine_error`" in paragraph
    assert "never fall back to the built-in reviewer roster" in paragraph
    assert "`engine_unresolvable: <engine_error>`" in paragraph
    assert "Step 3a.5" in paragraph


def test_error_rule_is_still_stated_once() -> None:
    assert _cmd(REVIEW).count(ERROR_RULE_ANCHOR) == 1


def test_clean_re_review_points_back_to_the_engine_step() -> None:
    paragraph = _clean_re_review_paragraph()
    assert "Step 3a.5" in paragraph
    assert f'`["{ENGINE_ROLE}"]` only' in paragraph


# --- the operator-settle defer sequence -----------------------------------------


def test_defer_sequence_is_documented_in_three_places() -> None:
    for rel in DEFER_SEQUENCE_DOCS:
        text = _read(rel)
        window = _after(text, "Deferring an engine MUST_FIX", span=3200)
        assert DEFER_COMMAND in window, rel
        assert DEFER_FLAG in window, rel
        assert "`fingerprint`" in window, rel
        assert "DEFERRED" in window, rel
        assert "engine::<fingerprint>" in window, rel
        assert "cw dev-queue requeue <T> -c <C>" in window, rel
        assert "cw review converge status" in window, rel
        assert "`coverage.*`" in window, rel


def test_defer_sequence_has_five_steps_in_the_runbook() -> None:
    text = _read("docs/dispatch-runbook.md")
    start = text.index("**Deferring an engine MUST_FIX (RFC 0017, #3039).**")
    section = text[start : text.index("A `coverage.*` or `acceptance.*`", start)]
    steps = re.findall(r"^(\d)\. ", section, flags=re.MULTILINE)
    assert steps == ["1", "2", "3", "4", "5"]


def test_defer_sequence_has_five_numbered_steps_in_the_command_file() -> None:
    window = _after(_cmd(REVIEW), "An engine MUST_FIX the operator accepts", span=1700)
    assert re.findall(r"\((\d)\) ", window) == ["1", "2", "3", "4", "5"]


# --- blocker reason lockstep ----------------------------------------------------


def test_review_engine_modified_is_a_known_blocker_reason() -> None:
    assert REASON in KNOWN_BLOCKER_REASONS


def test_review_engine_modified_is_a_row_of_the_auto_dev_blocker_table() -> None:
    assert REASON in _table_reasons(_blocker_reason_table())


def test_review_engine_modified_is_in_the_stage_three_exit_table() -> None:
    content = _cmd("auto-dev.md")
    row = next(
        line
        for line in content.splitlines()
        if line.startswith("| S3 `cw review engine preflight` exits 3")
    )
    assert f'`blocker.reason: "{REASON}"`' in row
    assert "BLOCKED_ON_USER" in row


def test_review_engine_modified_is_in_the_headless_contract_row() -> None:
    contract = _read("docs/headless-contract.md")
    row = next(
        line for line in contract.splitlines() if line.startswith(f"| `{REASON}` |")
    )
    assert "cw review engine allow" in row
    assert "`retry_eligible: false`" in row


def test_appendix_names_the_park_as_a_fourth_blocking_findings_trigger() -> None:
    appendix = _appendix("review")
    start = appendix.index("## Blocking-findings comment rule")
    end = appendix.index("\n## ", start + 1)
    section = appendix[start:end]
    assert "**Fourth trigger (RFC 0017, #3039)" in section
    assert f'`blocker.reason: "{REASON}"`' in section
    assert "same fixed header" in section
    assert "`touched`" in section
    assert appendix.count("## Blocking-findings comment rule") == 1


# --- contract, events and config docs -------------------------------------------


def test_headless_contract_documents_the_engine_skill_contract() -> None:
    contract = _read("docs/headless-contract.md")
    start = contract.index("### 12.10 Engine-Skill Contract")
    section = contract[start:]
    assert "ENGINE_PREAMBLE" in section
    assert ENGINE_PREAMBLE in " ".join(section.replace('"', "").split())
    assert ".cw/review-engine/<round-id>/summary.json" in section
    assert "never the Skill tool" in section
    for command in ("preflight", "materialize", "record", "allow"):
        assert f"| `{command} " in section, command
    assert "Exit 3" in section or "| Exit 3 |" in section
    assert "files outside the skill directory" in " ".join(section.split()).lower() or (
        "outside" in section and "`guarded_paths`" in section
    )
    assert "### 12.14 Known Limitation: Unguarded Files" in section
    assert "Files the engine reads or runs that sit outside" in " ".join(
        section.split()
    )


def test_headless_contract_lists_every_round_failure_with_its_friction() -> None:
    contract = _read("docs/headless-contract.md")
    start = contract.index("### 12.12 Engine Round Failures")
    section = contract[start : contract.index("### 12.13", start)]
    for failure in EngineRoundFailure:
        assert f"| `{failure}` |" in section, failure
        assert FRICTION[failure].split(":")[0] in section, failure


def test_headless_contract_no_longer_says_s1_only() -> None:
    contract = _read("docs/headless-contract.md")
    assert "S1 only" not in contract
    assert "`engine_non_deferrable`" in contract
    assert "`engine_coverage_incomplete`" in contract


def test_events_doc_documents_review_engine_allowed() -> None:
    events = _read("docs/events.md")
    start = events.index("### `review.engine_allowed`")
    section = events[start : events.index("\n### ", start + 1)]
    for key in ("ticket_id", "client", "head_sha", "reason", "actor", "recorded_at"):
        assert f'"{key}"' in section, key
    assert "cw review engine allow" in section
    assert "Event first, then save" in section
    assert "Deliberately **not** added to `_DEFAULT_OPERATOR_EVENT_TYPES`" in section


def test_events_doc_documents_the_deferred_outcome_and_engine_key() -> None:
    events = _read("docs/events.md")
    start = events.index("### `review.finding_settled`")
    section = events[start : events.index("\n### ", start + 1)]
    assert '`outcome` is `"DEFERRED"`' in section
    assert "`key` is `engine::<fingerprint>`" in section
    assert "--outcome defer" in section


def test_config_reference_documents_the_engine_context_and_doctor_warning() -> None:
    config = _read("config/CONFIG_REFERENCE.md")
    start = config.index("### Review Engine")
    section = config[start : config.index("\n## ", start + 1)]
    assert "`review.engine`" in section
    assert "`{skill, guarded_paths}`" in section
    assert "(schema 19)" in section
    assert "`review-engine/<client>/<lane>`" in section
    assert "default branch" in section
    assert "not guarded" in section
    assert "stale context below schema 19" in section
    assert "`review_engine_modified`" in section


def test_changelog_carries_the_engine_bullet_with_operator_note() -> None:
    # Why: search the whole file, not just [Unreleased]. The bullet moves to a
    # version section at release, and a section-bounded slice fails every cut.
    changelog = _read("CHANGELOG.md")
    bullet = next(
        line for line in changelog.splitlines() if "(RFC 0017, #3039)" in line
    )
    assert bullet.startswith("- **")
    assert "**Operator note:**" in bullet
    for needle in (
        REASON,
        "cw review engine allow",
        "--outcome defer",
        "dev-queue schema is now 55",
        "schema is now 19",
        "review-engine/<client>/<lane>",
    ):
        assert needle in bullet, needle
