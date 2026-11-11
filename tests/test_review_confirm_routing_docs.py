"""Guard tests: confirm routing over an engine pass agrees with the code (#3042).

Pure-markdown assertions in the style of ``tests/test_review_engine_docs.py``.
The router and the CLI are tested in Python
(``tests/test_review_convergence_confirm_route.py``,
``tests/test_cli_review_converge_confirm.py``); this file pins what the prose
promises: ``review_confirm_must_fix`` is registered and documented in lockstep,
the confirm round after an engine pass skips ``cw review adjudicate`` and obeys
``.decision``, the park's exit, appendix trigger and stop-release row exist,
the runbook carries both operator sequences, the rationale and both known
limitations, and ``docs/events.md`` documents the event's exact key set.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cw.auto_dev_result import KNOWN_BLOCKER_REASONS
from cw.review_convergence import (
    CONFIRM_ENGINE_DEFER_RATIONALE,
    confirm_escalated_event_payload,
)
from tests._review_convergence_helpers import (
    DEFAULT_SHA,
    engine_pass_ledger,
    record,
    round_finding,
)
from tests._shared.workflow_text import _appendix, _cmd
from tests.test_agent_comment_provenance import _blocker_reason_table, _table_reasons

ROOT = Path(__file__).resolve().parent.parent

REVIEW = "auto-dev-review.md"
REASON = "review_confirm_must_fix"
DECISION = "stop_confirm_must_fix"
STOP = "confirm_must_fix"
AFTER_ENGINE_ANCHOR = "**After an engine pass**"
CONFIRM_BULLET_ANCHOR = "- **0** with `.available == true`"
CLEAN_EXITS_ANCHOR = "**Clean-mode exits"
ESCALATED_ANCHOR = "- **Escalated confirm**"
OVERRIDE = "cw dev-queue approve <T> -c <C> --override-must-fix"
SHIP = "cw dev-queue requeue <T> -c <C> --stage finalize"
REGRESS = "cw dev-queue requeue <T> -c <C> --stage impl --regress"
ADJUDICATE = "cw review adjudicate"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _engine_pass_sentence() -> str:
    """The "After an engine pass" sentence group, up to "A clean"."""
    content = _cmd(REVIEW)
    start = content.index(AFTER_ENGINE_ANCHOR)
    return content[start : content.index(" A clean", start)]


def _confirm_bullet() -> str:
    content = _cmd(REVIEW)
    start = content.index(CONFIRM_BULLET_ANCHOR)
    return content[start : content.index("\n- **3**", start)]


def _first(text: str, *needles: str) -> list[int]:
    return [text.index(needle) for needle in needles]


# --- blocker reason lockstep ---------------------------------------------------


def test_the_reason_is_a_known_blocker_reason() -> None:
    assert REASON in KNOWN_BLOCKER_REASONS


def test_the_reason_is_a_row_of_the_auto_dev_blocker_table() -> None:
    assert REASON in _table_reasons(_blocker_reason_table())


def test_the_reason_is_in_the_stage_three_exit_table() -> None:
    row = next(
        line
        for line in _cmd("auto-dev.md").splitlines()
        if line.startswith("| S3 clean-mode `record-round --kind confirm` decides")
    )
    assert f'`blocker.reason: "{REASON}"`' in row
    assert "BLOCKED_ON_USER" in row


def test_the_reason_is_a_headless_contract_row() -> None:
    row = next(
        line
        for line in _read("docs/headless-contract.md").splitlines()
        if line.startswith(f"| `{REASON}` |")
    )
    assert "--override-must-fix" in row
    assert "`retry_eligible: false`" in row


# --- the auto-dev-review.md flow -----------------------------------------------


def test_the_decision_list_names_the_exit() -> None:
    content = _cmd(REVIEW)
    bullet = next(
        line for line in content.splitlines() if line.startswith(f"- `{DECISION}`")
    )
    assert f"`{REASON}` exit" in bullet
    assert content.index(bullet) > content.index("- `stop_confirm_unavailable`:")


def test_the_confirm_bullet_keeps_its_order_and_qualifies_adjudication() -> None:
    bullet = _confirm_bullet()
    voided, suppress, record_round = _first(
        bullet,
        "cw review check-voided",
        "cw review converge suppress",
        "cw review converge record-round --kind confirm",
    )
    assert voided < suppress < record_round
    assert "then adjudication unless the previous round was an engine pass" in bullet


def test_the_engine_pass_sentence_names_only_the_two_pre_steps() -> None:
    sentence = _engine_pass_sentence()
    voided, suppress, record_round = _first(
        sentence,
        "cw review check-voided",
        "cw review converge suppress",
        "cw review converge record-round --kind confirm",
    )
    assert voided < suppress < record_round
    assert "are the only pre-steps" in sentence
    assert "obey its `.decision`" in sentence
    assert "no `--defer-should-fix`" in sentence
    assert ADJUDICATE not in sentence[voided:record_round]


@pytest.mark.parametrize("occurrence", [0])
def test_adjudicate_appears_only_inside_the_skip_clause(occurrence: int) -> None:
    sentence = _engine_pass_sentence()
    hits = [m.start() for m in re.finditer(re.escape(ADJUDICATE), sentence)]
    assert len(hits) == 1
    assert sentence[: hits[occurrence]].endswith("skip `")


def test_the_clean_mode_exit_follows_the_escalated_confirm_bullet() -> None:
    content = _cmd(REVIEW)
    exits = content[content.index(CLEAN_EXITS_ANCHOR) :]
    escalated = exits.index(ESCALATED_ANCHOR)
    bullet_start = exits.index(f"- `{REASON}` (`decision: {DECISION}`")
    assert bullet_start > escalated
    bullet = exits[bullet_start : exits.index("\n", bullet_start)]
    assert ".cw/review-convergence/round-<n>-verdict.json" in bullet
    assert "stop.identities" in bullet
    assert "`retry_eligible: false`" in bullet
    assert "confirm_escalated: confirm_must_fix round=<n>" in bullet
    for command in (OVERRIDE, SHIP, REGRESS):
        assert command in bullet, command


# --- appendix ------------------------------------------------------------------


def test_the_appendix_has_a_fifth_trigger_under_the_one_rule_header() -> None:
    appendix = _appendix("review")
    assert appendix.count("## Blocking-findings comment rule") == 1
    start = appendix.index("## Blocking-findings comment rule")
    section = appendix[start : appendix.index("\n## ", start + 1)]
    assert "**Fifth trigger" in section
    assert f'`blocker.reason: "{REASON}"`' in section


def test_the_appendix_stop_release_table_has_the_row() -> None:
    row = next(
        line
        for line in _appendix("review").splitlines()
        if line.startswith(f"| `{STOP}` |")
    )
    assert f"`{REASON}`" in row
    for command in (SHIP, REGRESS):
        assert command in row, command
    assert "--override-must-fix" in row


# --- runbook, events, changelog ------------------------------------------------


def _runbook_section() -> str:
    runbook = _read("docs/dispatch-runbook.md")
    start = runbook.index(f"**Park: `{REASON}` (RFC 0017, #3042).**")
    return runbook[start : runbook.index("\n**", start + 1)]


def test_the_runbook_names_both_sequences_and_the_rationale() -> None:
    section = " ".join(_runbook_section().split())
    assert OVERRIDE in section
    assert SHIP in section
    assert REGRESS in section
    assert f'"{CONFIRM_ENGINE_DEFER_RATIONALE}"' in section


def test_the_runbook_records_both_known_limitations() -> None:
    section = " ".join(_runbook_section().split())
    assert "files its own debt ticket" in section
    assert "no per-round cap" in section
    assert "listing hits its limit" in section


def test_the_events_doc_key_set_matches_the_payload_builder() -> None:
    events = _read("docs/events.md")
    start = events.index("### `review.confirm_escalated`")
    section = events[start : events.index("\n### ", start + 1)]
    opened = section.index("```json\n") + len("```json\n")
    block = section[opened : section.index("\n```", opened)]
    documented = json.loads(block)
    ledger, report = record(
        engine_pass_ledger(), round_finding("MUST_FIX"), sha=DEFAULT_SHA, kind="confirm"
    )
    assert report.decision == DECISION
    payload = confirm_escalated_event_payload(ledger, ticket_id="T", client=None)
    assert set(documented) == set(payload)
    assert "Emitted after the ledger save" in section
    assert "`correlation_id`" in section
    assert "Deliberately **not** added to `_DEFAULT_OPERATOR_EVENT_TYPES`" in section


def test_the_changelog_has_the_bullet() -> None:
    assert "(#3042, RFC 0017 I3" in _read("CHANGELOG.md")
