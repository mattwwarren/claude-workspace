"""Guard tests: Step 1c.0a fold of operator answers into the plan (#2258).

Pure-markdown assertions over the auto-dev plan instruction files, following
the ``read_text()`` + literal-substring/window convention of
``test_consolidated_park.py``, plus one real-fixture test that drives
``cw.plan_fingerprint`` directly (test 12).

Background: on a resumed round, an operator's answer to a parked ambiguity or
to an advisory plan-review finding did not reliably reach the plan body before
the stations re-reviewed it. Two gaps caused it. The draft was never folded
with the answers before Step 1d/1f read it (the stale-text gap), and the
stations never saw the decision, so a finding the operator had already
answered was re-flagged (the stations-cannot-see-the-decision gap). The fold
(Step 1c.0a) closes the first gap, the *Working-plan rule* and the ``Decided:``
label close the second, and the park comment carries decisions across a failed
fold without a new bookkeeping line.
"""

import hashlib
from pathlib import Path

from cw.plan_fingerprint import compute_plan_draft_fingerprint, leading_bookkeeping_end
from tests._shared.workflow_text import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after
from tests.test_plan_stage_settlement import _step1c0_block, _step1c_headless_section

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENTS_ROOT = _REPO_ROOT / ".claude" / "agents"

FOLD_HEADING = "## Step 1c.0a: fold operator answers into the plan before review"
FOLD_TAG = "<!-- el: plan.fold-operator-answers-before-review -->"
WORKING_PLAN_RULE = "**Working-plan rule (#2176, #2258).**"
DECISIONS_PARAGRAPH = "**Decisions on record.**"
PARK_HEADER = "**Consolidated park (single-exit rule, #1650).**"


def _fold_section() -> str:
    """The Step 1c.0a section, tags stripped, up to the next ``##`` heading."""
    section = _appendix("plan")
    start = section.index(FOLD_HEADING)
    end = section.index("\n## ", start + 1)
    return section[start:end]


def _paragraph_at(text: str, anchor: str) -> str:
    """The paragraph (up to the next blank line) starting at *anchor*."""
    start = text.index(anchor)
    end = text.find("\n\n", start)
    return text[start : end if end != -1 else len(text)]


def _step1g_section() -> str:
    core = _cmd("auto-dev-plan.md")
    start = core.index("**Step 1g.0")
    return core[start : start + 3000]


def _step1f2_line() -> str:
    core = _cmd("auto-dev-plan.md")
    start = core.index("**Each prompt must include:** the working plan")
    return core[start : core.index("\n", start)]


def _park_block() -> str:
    return _after(_appendix("plan"), PARK_HEADER, span=8000)


def _agent_text(name: str) -> str:
    return (_AGENTS_ROOT / name).read_text(encoding="utf-8")


def test_fold_section_sits_between_1c0_and_the_park_and_is_tagged() -> None:
    """Test 1: the fold section exists once, in order, with its own tag."""
    raw = _appendix("plan", raw=True)
    assert raw.count(FOLD_HEADING) == 1
    assert raw.index("## Step 1c.0: round-cap read") < raw.index(FOLD_HEADING)
    assert raw.index(FOLD_HEADING) < raw.index("## Consolidated park")
    assert f"{FOLD_TAG}\n{FOLD_HEADING}" in raw

    core = _cmd("auto-dev-plan.md")
    step1c = core[core.index("### Step 1c: Ambiguity Verification") :]
    assert "Step 1c.0a" in step1c
    assert step1c.index("Step 1c.0a") < step1c.index(
        "1. **Source the ambiguity list.**"
    )
    assert "Run Step 1c.0a before this write" in _step1c0_block()


def test_fold_gating_is_headless_resumed_only() -> None:
    """Test 2: the trigger and its exclusions are stated."""
    section = _fold_section()
    assert "headless resumed rounds only" in section
    assert "not on the Step 1a.0b fast path" in section
    assert "never on a fresh dispatch" in section
    assert "before Step 1c.1" in section


def test_fold_candidates_cite_steps_3_and_3b_and_no_provenance_literals() -> None:
    """Test 3: candidates by citation; no provenance criteria restated."""
    section = _fold_section()
    assert "Step 1c.0 steps 3 and 3b" in section
    for literal in (
        "cw-agent-authored",
        "pipeline fixed header",
        "plan-of-record post",
    ):
        assert literal not in section


def test_fold_bounds_failsoft_and_bookkeeping() -> None:
    """Test 4: one attempt, independent bounds, fail-soft, no grammar change."""
    section = _fold_section()
    assert "one attempt per dispatch" in section
    assert "independent of the 1-cycle budget" in section
    assert "1a.0b's per-source cap" in section
    assert "persists no bookkeeping line" in section
    assert "no new `blocker.reason`" in section
    assert "Fail-soft and bounds" in section
    assert "prior draft is kept byte-identical" in section


def test_fold_never_approves_or_emits_resolution_telemetry() -> None:
    """Test 5: no approval revocation, no resolution_consumed; emission rule note."""
    section = _fold_section()
    assert (
        "calls neither `revoke-plan-approval` nor writes `plan-stage-approval-revoked`"
        in section
    )
    assert "never emits `resolution_consumed`" in section
    core = _cmd("auto-dev-plan.md")
    assert "Step 1c.0a's fold is excluded likewise" in _after(
        core,
        "**`resolution_consumed`/`resolution_evidence` emission rule (#1896).**",
        span=3000,
    )


def test_working_plan_rule_is_defined_once_in_the_headless_section() -> None:
    """Test 6: the Working-plan rule states its parts once, after Draft-rewrite."""
    section = _step1c_headless_section()
    assert section.count(WORKING_PLAN_RULE) == 1
    assert section.index("**Draft-rewrite rule") < section.index(WORKING_PLAN_RULE)
    rule = _paragraph_at(section, WORKING_PLAN_RULE)
    assert "Step 1d, Step 1f and Step 1g" in rule
    assert "Transcription is not deferrable" in rule
    assert "plan-spec-reviewed" in rule
    assert "plan-soundness-reviewed" in rule
    assert "a no-change fold is a no-op" in rule
    assert "Step 1f.4 is excluded from strip-all" in rule


def test_working_plan_required_headings_match_the_step_1b_prompt() -> None:
    """Test 7: the validator's six headings are the ones Step 1b asks for."""
    core = _cmd("auto-dev-plan.md")
    step1b = core[
        core.index("### Step 1b: Generate Plan (Agent)") : core.index(
            "### Step 1c: Ambiguity Verification"
        )
    ]
    rule = _paragraph_at(_step1c_headless_section(), WORKING_PLAN_RULE)
    for heading in (
        "## Patterns Found",
        "## Touch-point Contract",
        "## Files Modified",
        "## Non-Goals",
        "## Ambiguities",
        "## Pre-flight Resolution Conformance",
    ):
        assert heading in rule
        assert heading in step1b
    for accumulator in (
        "## Adopted Assumptions",
        "## Self-Verified Premises",
        "## Deferred Premises",
        "## Settled Plan Items",
    ):
        assert accumulator in rule
    assert "every accumulator section the prior draft carried" in rule


def test_step_1a0b_item_3_cites_the_working_plan_rule() -> None:
    """Test 8: the resolutions revision transcribes now, and is validated by name."""
    raw = _appendix("plan")
    window = _after(
        raw, "transcribes every `R<n>` into the plan body in this same pass", span=500
    )
    assert "never deferred behind open or newly parked ambiguities" in window
    assert "Working-plan rule" in window


def test_decided_label_is_wired_into_the_station_prompt() -> None:
    """Test 9: the 1f.2 prompt carries the decisions and the Decided label."""
    line = _step1f2_line()
    assert "Operator decisions on record" in line
    assert "`Decided:`" in line
    assert "Resolution gate's checks" in line
    assert "unmatched finding decision:" in line
    assert "never lowers a finding's severity or changes its gating" in line
    for identifier in ("`comment:<id>`", "`R<n>`", "`row:<round>`"):
        assert identifier in line
    assert "#### Decided, pending edit" not in line


def test_decided_label_is_defined_in_both_station_agents() -> None:
    """Test 9 (cont.): both station specs take the optional input and the sub-bullet."""
    for name in ("plan-reviewer.md", "plan-soundness-reviewer.md"):
        text = _agent_text(name)
        assert "Operator decisions on record (optional input)" in text
        assert '- Decided: <identifier> — "<verbatim quote>"' in text
        assert "does not change the verdict tier" in text
        assert "must not be labelled `Decided`" in text


def test_consolidated_park_splits_decided_findings_and_keeps_flat_shape() -> None:
    """Test 9 (cont.): park groups, flat-shape clause, unchanged status string."""
    park = _park_block()
    assert "#### Still open" in park
    assert "#### Decided, pending edit" in park
    assert "When none is decided, keep the flat shape" in park
    assert "`advisory findings decided: <d> of <f>`" in park
    assert (
        "consolidated park: <a> ambiguities, <p> premises, "
        "<f> advisory findings, scope <tier>"
    ) in park


def test_grammar_is_not_extended() -> None:
    """Test 10: the closed six-form grammar stands; the fold adds no form or id."""
    core = _cmd("auto-dev-plan.md")
    assert "these six forms are the ENTIRE grammar" in core
    section = _fold_section()
    assert "plan-stage-settled:" not in section
    assert "F<n>" not in section
    assert "plan-stage-" not in _paragraph_at(section, DECISIONS_PARAGRAPH)


def test_step_1g0_and_rule_sites_name_the_scope_tier_exit() -> None:
    """Test 11: the Step 1g.0 exit is an instance of both rules, at the bullet end."""
    step1g = _step1g_section()
    window = _after(
        step1g, "Emit `stage.errored` before the `blocked` sentinel.", span=250
    )
    assert "draft-persistence rule" in window
    assert "draft-rewrite rule" in window
    assert "write the plan's current text to `.cw/plan-draft.md`" in step1g

    headless = _step1c_headless_section()
    persistence = _paragraph_at(headless, "**Draft-persistence rule")
    assert "scope_tier_stale" in persistence[-400:]
    rewrite = _paragraph_at(headless, "**Draft-rewrite rule")
    assert "scope_tier_stale" in rewrite[-400:]
    assert "Step 1c.0a" in rewrite[-400:]
    assert 'blocker.reason: "scope_tier_stale"' not in persistence
    assert 'blocker.reason: "scope_tier_stale"' not in rewrite


def test_real_fixture_bookkeeping_split_and_reprepend_invariant() -> None:
    """Test 12: the fold's split-then-re-prepend matches cw.plan_fingerprint.

    Builds a draft with all seven leading forms in grammar order plus both
    signoff lines in the body. The fold splits the leading block off, rewrites
    only the body and re-prepends the block, so the fingerprint must equal the
    hash of the new body alone.
    """
    attempted = (
        "<!-- plan-stage-resolutions-attempted: source=row:1:"
        + "b" * 64
        + "; outcome=succeeded -->\n"
    )
    revoked = (
        "<!-- plan-stage-approval-revoked: at=2026-11-12T00:00:00Z; fingerprint="
        + "c" * 64
        + " -->\n"
    )
    applied = "<!-- plan-stage-resolutions-applied: source=row:1:" + "b" * 64 + " -->\n"
    leading = (
        "<!-- plan-stage-scan-round: 1 -->\n"
        "<!-- plan-stage-last-evaluated: operator_comment=42|body_sha="
        + "a"
        * 64
        + " -->\n"
        "<!-- plan-stage-settled: A1: ADOPTED -->\n"
        "<!-- plan-stage-settled: R2.P1: CONFIRMED -->\n"
        + attempted
        + revoked
        + applied
    )
    signoffs = (
        "<!-- plan-spec-reviewed: 2026-11-12 v2 -->\n"
        "<!-- plan-soundness-reviewed: 2026-11-12 v1 -->\n"
    )
    body = "\n# Plan\n\nbody text\n"
    draft = leading + signoffs + body

    assert leading_bookkeeping_end(draft) == len(leading)
    assert leading_bookkeeping_end(leading + signoffs) == len(leading)

    for new_body in ("# Plan\n\nedited\n", "x"):
        rebuilt = leading + new_body
        assert leading_bookkeeping_end(rebuilt) == len(leading)
        assert (
            compute_plan_draft_fingerprint(rebuilt)
            == hashlib.sha256(new_body.encode("utf-8")).hexdigest()
        )

    with_signoffs = compute_plan_draft_fingerprint(draft)
    without_signoffs = compute_plan_draft_fingerprint(leading + body)
    assert with_signoffs != without_signoffs


def test_decisions_on_record_paragraph_is_the_single_definition() -> None:
    """Test 13: the Decisions-on-record paragraph carries every rule (a)-(g)."""
    section = _fold_section()
    assert section.count(DECISIONS_PARAGRAPH) == 1
    para = _paragraph_at(section, DECISIONS_PARAGRAPH)
    assert "step 3 and 3b candidates" in para
    assert "`#### Decided, pending edit`" in para
    assert "Resolution gate's quote check" in para
    assert "Comment provenance rule" in para
    assert "fail-closed" in para
    assert "fast path" in para
    assert "read-only" in para
    assert "fresh dispatch" in para
    assert "`none`" in para
    assert "until a fold applies it" in para
    assert "plan-stage-" not in para
    core_line = _step1f2_line()
    assert "Decisions on record" in core_line
    assert "Step 1c.0a" in core_line
    assert "Decided, pending edit" not in core_line


def test_park_step_3_carries_decided_findings_across_rounds() -> None:
    """Test 14: a decided finding stays in its group until a fold applies it."""
    park = _park_block()
    assert "stays in `#### Decided, pending edit`" in park
    assert "until a fold applies it" in park
    assert "adds no bookkeeping line" in park
