"""Pins for the /harden-ticket sweep upgrades (#1506).

Consolidates #1505, #1507, #2085 and #1693: four mandatory mechanical checks
(M1-M4), four sweep questions (Q1-Q4), six review-round plan-quality classes, a
resolution self-critique pass (S1-S3) before ``Proceed.``, an optional redacted
probe step (2b) with a ``Probed facts`` template, and a case-fixture reference.

All phrase assertions run on whitespace-normalized text so line-wrapping never
matters. The #1655 positioning contract stays in
``test_harden_ticket_positioning.py``; this file is the distinct sweep-wording
concern.
"""

from __future__ import annotations

import re
import subprocess
import sys

import pytest

from tests._shared.workflow_text import _REPO_ROOT, _norm, _section_between, _skill

_SKILL = "harden-ticket/SKILL.md"
_CASES = "harden-ticket/references/sweep-checklist-cases.md"
_MULTI_TASK = "harden-ticket/references/multi-task-plan-hardening.md"

_MAX_SKILL_LINES = 400

# Repo-specific names that must never leak into the generic six-class
# paragraph. Each entry was grepped in the 12 historical cases plus #1693
# (the case reference may name repo internals; the paragraph may not).
_CLIENT_SPECIFIC_TOKENS = (
    "ReviewVerdict",
    "AcceptedFinding",
    "CodexExecutor",
    "codex_review.py",  # #1392
    "WorktreeGcReport",  # #1389
    "DispatchSkipReason",
    "HostCapacitySnapshot",
    "DevQueueStore",  # #1444
    "EventBus",
    "cw_queue_events_server",  # #1303
    "LastResultSource",
    "EmitValidationError",
    "AutoDevResult",  # #1457
    "HookContextConflictError",
    "ConciergeCandidate",
    "_compute_dead_on_arrival",  # #1674
    "TicketTask",
    "QueueItemStatus",  # #1444, #1638, #1674
)
# The #1506 body marks its #1693 citations of this name as stale.
_STALE_TYPE_NAME = "SchemaVersion"

_HISTORICAL_TICKETS = {
    "#1392",
    "#1389",
    "#1462",
    "#1444",
    "#1303",
    "#1457",
    "#1674",
    "#1689",
    "#1643",
    "#1638",
    "#1618",
    "#1859",
}

_SLF001_SNIPPET = (
    "from cw.event_bus import EventBus\n\n"
    '_bus = EventBus(events_file="e", cursors_file="c", log_label="x")\n'
    "_lock = _bus._lock\n"
)


def _skill_text() -> str:
    return _skill(_SKILL)


def _prompt() -> str:
    """The sweep prompt fence body, whitespace-normalized."""
    return _norm(
        _section_between(
            _skill_text(),
            "Read-only pre-flight under-specification",
            "End with a one-line verdict.",
        )
    )


def _triage() -> str:
    return _norm(_section_between(_skill_text(), "### 2. Triage", "### 2b. Probe"))


def _probe() -> str:
    return _norm(_section_between(_skill_text(), "### 2b. Probe", "### 3. Compose"))


def _compose() -> str:
    return _norm(_section_between(_skill_text(), "### 3. Compose", "### 4. Hand off"))


def _untagged_fences(content: str) -> list[str]:
    """Bodies of fenced blocks that carry no language tag."""
    fences: list[str] = []
    lines = content.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() == "```":
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index].strip() != "```":
                body.append(lines[index])
                index += 1
            fences.append("\n".join(body))
        index += 1
    return fences


def test_sweep_prompt_carries_four_mechanical_checks() -> None:
    prompt = _prompt()
    for phrase in (
        "wc -l",
        "grep -rn",
        "tests/",
        "grep every file or symbol path named in the ticket",
        "file:line",
    ):
        assert phrase in prompt, phrase


@pytest.mark.parametrize(
    ("label", "needles"),
    [
        ("M1", ("#1392", "#1470", "#1307", "projected", "module-size ceiling")),
        ("M2", ("#1389",)),
        ("M3", ("#1462",)),
        ("M4", ("#1444", "Theorizing Before Grepping the Mechanism")),
    ],
)
def test_each_mechanical_check_has_label_and_motivating_ticket(
    label: str, needles: tuple[str, ...]
) -> None:
    prompt = _prompt()
    start = prompt.index(f"**{label}")
    window = prompt[start : start + 900]
    for needle in needles:
        assert needle in window, (label, needle)


def test_triage_resolves_mechanical_findings() -> None:
    triage = _triage()
    assert "Mechanical results are triaged, not asked" in triage
    for label in ("M1", "M2", "M3", "M4", "Q1", "Q2"):
        assert label in triage, label
    assert "split decision" in triage
    assert "resolved now" in triage
    assert "a finding, not a premise" in triage


_SIX_CLASS_PHRASES = (
    "shared-vs-owner placement",
    "sensitive values carried by except/output paths",
    "three-state lookups",
    "fail open",
    "base-signature changes without the override/caller sweep",
    "full-overwrite write endpoints",
    "runtime-only configuration companions",
    "publish sequencing",
    "idempotency / lock-ordering",
)


def _six_class_window() -> str:
    return _norm(
        _section_between(
            _skill_text(),
            "Anticipate the plan-quality classes",
            "Confirm the file set",
        )
    )


@pytest.mark.parametrize("phrase", _SIX_CLASS_PHRASES)
def test_plan_quality_paragraph_names_six_review_round_classes(phrase: str) -> None:
    assert phrase in _six_class_window()


def test_six_class_paragraph_stays_generic() -> None:
    """No repo-specific token in the paragraph; no stale name in any skill file."""
    window = _six_class_window()
    for token in (*_CLIENT_SPECIFIC_TOKENS, _STALE_TYPE_NAME):
        assert token not in window, token
    for name in (_SKILL, _MULTI_TASK, _CASES):
        assert _STALE_TYPE_NAME not in _skill(name), (name, _STALE_TYPE_NAME)


def test_two_questions_numeric_and_causal() -> None:
    prompt = _prompt()
    for phrase in (
        "Numeric claims",
        "compare them explicitly",
        "every observed value",
        "Causal claims",
        "what other mechanism would produce the same observation",
        "I didn't check",
        "an observation, not a mechanism",
    ):
        assert phrase in prompt, phrase


def test_mutation_vacuity_and_verdict_pull() -> None:
    prompt = _prompt()
    for phrase in (
        "still exist after the fix",
        "vacuous",
        "unexpected green",
        "never recorded as a pass",
    ):
        assert phrase in prompt, phrase


def test_self_created_verification_evidence() -> None:
    prompt = _prompt()
    for phrase in (
        "run URL",
        "gh run view",
        "not `skipped`",
        "outside the specified mechanism",
    ):
        assert phrase in prompt, phrase


def test_settled_marker_settles_asks_not_claims() -> None:
    text = _norm(_skill_text())
    start = text.index("plan-stage-settled")
    window = text[start - 300 : start + 700]
    assert "#1683" in window
    assert "by identity only" in window
    assert "never verifies an asserted claim" in window
    assert "Q1/Q2" in window
    assert "No redaction" in window


def test_self_critique_is_numbered_step_between_compose_and_post() -> None:
    compose = _compose()
    for label in ("**S1", "**S2", "**S3"):
        assert label in compose, label
    s1 = compose[compose.index("**S1") : compose.index("**S2")]
    s2 = compose[compose.index("**S2") : compose.index("**S3")]
    s3 = compose[compose.index("**S3") :]
    for phrase in ("ruff check", "mypy", "scratch reconstruction", "SLF001", "#1303"):
        assert phrase in s1, phrase
    for phrase in (
        "ARCHITECTURE.md",
        "ADR",
        "#1457",
        "one-door",
        "§8",
        "a defect in the resolution, not a question for the operator",
    ):
        assert phrase in s2, phrase
    assert "what NEW blocker do they create downstream" in s3
    assert compose.index("Self-critique pass") < compose.index("Post it to the issue")
    assert "before `Proceed.`" in compose
    assert "consolidated list" in compose


def test_probe_step_trigger_scope_guard_and_examples() -> None:
    probe = _probe()
    for phrase in (
        "runtime behavior of an external binary or service",
        "read-only",
        "scratch",
        "seconds-to-minutes",
        "only facts the resolutions depend on",
        "not a pre-implementation",
        "say so",
        "pre-authorize",
        "first-run discovery",
        "exactly those named facts",
        "#1238",
        "R11",
        "R12",
        "R13",
        "malformed-schema",
        "snap",
        "exit-code",
    ):
        assert phrase in probe, phrase


def test_probe_step_redacts_before_posting() -> None:
    probe = _probe()
    for phrase in (
        "never raw bodies or headers",
        "error class or shape",
        "redact credentials and identifiers",
        "Scope-Check Anything Leaving This Machine",
    ):
        assert phrase in probe, phrase
    template = _template_fence()
    assert "error class or shape" in template
    assert "output excerpt" not in template


def _template_fence() -> str:
    for fence in _untagged_fences(_skill_text()):
        if "### Probed facts" in fence:
            return fence
    pytest.fail("no untagged fence containing '### Probed facts'")


def test_probed_facts_template_sits_before_proceed_marker() -> None:
    template = _template_fence()
    assert template.index("Probed facts") < template.index("Proceed.")
    non_empty = [line for line in template.splitlines() if line.strip()]
    assert non_empty[-1].strip() == "<!-- auto-dev-preflight-resolutions -->"
    assert "Proceed." in non_empty[-2]
    assert "optional" in template


def _case_rows() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in _skill(_CASES).splitlines():
        match = re.match(r"\|\s*(#\d+)\s*\|([^|]*)\|([^|]*)\|", line)
        if match:
            rows.append(
                (match.group(1), match.group(2).strip(), match.group(3).strip())
            )
    return rows


def test_case_fixture_every_historical_case_is_caught() -> None:
    rows = _case_rows()
    assert {ticket for ticket, _, _ in rows} == _HISTORICAL_TICKETS
    labels = set(re.findall(r"\*\*([MQS]\d)\b", _skill_text()))
    assert {"M1", "M2", "M3", "M4", "Q1", "Q2", "Q3", "Q4", "S1", "S2", "S3"} <= labels
    for ticket, check, caught in rows:
        for check_id in re.split(r"\s*\+\s*", check):
            assert check_id.strip() in labels, (ticket, check_id)
        assert caught == "yes", (ticket, caught)
    assert "references/sweep-checklist-cases.md" in _skill_text()
    cases = _skill(_CASES)
    assert "concierge/_common.py" in cases
    assert "_find_session_for_ticket" in cases
    assert "concierge.py:174-193" not in cases


def _ruff_stdin(filename: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--config",
            "pyproject.toml",
            "--stdin-filename",
            filename,
            "-",
        ],
        input=_SLF001_SNIPPET,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_self_critique_s1_reproduces_slf001_for_the_1303_snippet() -> None:
    in_src = _ruff_stdin("src/cw/_scratch_1303.py")
    assert in_src.returncode != 0, in_src.stdout
    assert "SLF001" in in_src.stdout
    in_tests = _ruff_stdin("tests/test_scratch_1303.py")
    assert in_tests.returncode == 0, in_tests.stdout
    assert "SLF001" not in in_tests.stdout


def test_skill_stays_short_enough_for_context_pressure() -> None:
    lines = _skill_text().count("\n") + 1
    assert lines <= _MAX_SKILL_LINES, (
        f"SKILL.md is {lines} lines (cap {_MAX_SKILL_LINES}); move detail into "
        "references/ rather than raising the cap"
    )
