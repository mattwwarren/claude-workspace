"""Guard tests: plan ticket-quote pre-pass, Check 5, citation-only cycle (#1502).

Pure-markdown assertions over the auto-dev pipeline instruction files, in the
``_cmd`` / ``_appendix`` / ``_after`` convention of
``test_plan_format_only_findings.py``. ``.claude/scripts/check_ticket_quotes.py``
is the deterministic fact source: the orchestrator runs it before each Plan
Reviewer spawn, merges every ``unmatched`` entry into the reviewer's findings as
MUST_FIX with the reserved category ``Citation``, and the reviewer's Check 5
renders the verdict without ever re-verifying a quote. A Citation-only
MUST_FIX gets its own independent, one-shot revision cycle modeled on the
Format-Only cycle.
"""

from pathlib import Path

from tests._shared.workflow_text import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after

ROOT = Path(__file__).parent.parent
AGENTS = ROOT / ".claude" / "agents"

SECTION_HEADING = "## Step 1f.2a: ticket-quote pre-pass (#1502)"
EL_TAG = "<!-- el: plan.ticket-quote-pre-pass -->"
STUB_ANCHOR = "**Ticket-quote pre-pass (#1502).**"
INPUT_ANCHOR = "**Mechanical ticket-quote verdicts:**"
CITATION_BULLET_ANCHOR = (
    "- **MUST_FIX where every persisting finding's category is exactly `Citation`"
)
CITATION_FALLTHROUGH_ANCHOR = (
    "- **MUST_FIX persists after the citation-only cycle (still all `Citation`)**"
)
CITATION_REVISION_ANCHOR = "**Citation-only revision (#1502, #1523).**"
FORMAT_ONLY_REVISION_ANCHOR = "**Format-only revision (defense-in-depth).**"
CHECKPOINT_ANCHOR = "**Headless only — checkpoint the revised draft (#1778).**"
RANGE_BEGIN = "<!-- el-begin: plan.headless-only-checkpoint-revised-draft -->"
CHECK5_REJECT = (
    "**Reject (MUST_FIX) if** the supplied `Mechanical ticket-quote verdicts` "
    "list a non-empty `unmatched` array: emit one MUST_FIX finding per entry, "
    "category `Citation`, naming the exact `quote` string and its plan `line`."
)


def _agent(name: str) -> str:
    return (AGENTS / name).read_text()


def _section() -> str:
    content = _appendix("plan")
    return content[content.index(SECTION_HEADING) :]


# ---------------------------------------------------------------------------
# Appendix section
# ---------------------------------------------------------------------------


def test_appendix_section_exists_with_el_tag_after_a_rule() -> None:
    raw = _appendix("plan", raw=True)
    assert f"---\n\n{EL_TAG}\n{SECTION_HEADING}" in raw
    assert SECTION_HEADING in _appendix("plan")


def test_core_stub_names_script_loop_version_and_friction_strings() -> None:
    content = _cmd("auto-dev-plan.md")
    window = _after(content, STUB_ANCHOR, span=700)
    assert "Before each Plan Reviewer spawn" in window
    assert "every re-spawn after Step 1f.4" in window
    assert "`Step 1f.2a`" in window
    assert "auto-dev-plan-appendix.md" in window


def test_appendix_names_script_candidate_loop_and_minimum_version() -> None:
    section = _section()
    assert "check_ticket_quotes.py" in section
    assert "$SESSION_WT/.claude/scripts/check_ticket_quotes.py" in section
    assert "$HOME/.claude/scripts/check_ticket_quotes.py" in section
    assert "candidate loop" in section
    assert "strict marker parsing" in section
    assert "minimum `cw-script-version` 1" in section
    assert "advisory" in section


def test_appendix_names_the_friction_strings() -> None:
    section = _section()
    for text in (
        "check_ticket_quotes: script absent, skipped",
        "check_ticket_quotes: script stale, skipped",
        "check_ticket_quotes: exit 2 (usage or parse error), skipped",
        "ticket-quote pre-pass: <N> unmatched quote(s)",
    ):
        assert text in section


def test_appendix_runs_before_each_reviewer_spawn_including_respawns() -> None:
    section = _section()
    assert "before each Plan Reviewer spawn" in section
    assert "re-spawns after Step 1f.4" in section
    assert "advisory-mode" in section
    assert "consolidated park" in section


def test_appendix_corpus_is_live_fetch_not_cached_context() -> None:
    section = _section()
    assert "live fetch" in section
    assert "never `.cw/context.json`" in section
    assert "$SESSION_WT/.cw/ticket-quote-corpus.json" in section
    assert "$SESSION_WT/.cw/ticket-quote-resolutions.txt" in section
    assert "gh issue view" in section
    assert "--json title,body,comments" in section


def test_appendix_scratch_plan_is_the_working_plan_not_the_draft() -> None:
    section = _section()
    assert "$SESSION_WT/.cw/ticket-quote-plan.md" in section
    assert "working plan" in section
    assert "Working-plan rule" in section
    assert "NOT the on-disk `.cw/plan-draft.md`" in section


def test_appendix_excludes_agent_authored_comments() -> None:
    section = _section()
    assert "agent-authored" in section
    assert "excluded" in section
    assert "<!-- cw-agent-authored -->" in section
    assert "lstrip()" in section
    assert "startswith" in section
    assert "substring" in section


def test_appendix_states_linear_residual_gap() -> None:
    section = _section()
    assert "Residual gap" in section
    assert "linear" in section
    assert "Write tool" in section
    assert "LLM transcription" in section
    assert "mechanical redirect" in section


def test_appendix_documents_exit_codes_and_grammar() -> None:
    section = _section()
    for text in (
        "exit 0",
        "exit 1",
        "exit 2",
        "`Citation`",
        "labeled field",
        "attribution sentence",
        "identifier form",
        "blockquote",
        "`--resolutions`",
    ):
        assert text in section


# ---------------------------------------------------------------------------
# Step 1f.2 inputs, Step 1f.3 gating, Step 1f.4 revision
# ---------------------------------------------------------------------------


def test_plan_reviewer_station_bullet_counts_five_checks() -> None:
    content = _cmd("auto-dev-plan.md")
    assert (
        "5 checks in `agents/plan-reviewer.md`: Contract Specificity, File "
        "Enumeration, Test Helper Inventory, Observability Call Inventory, "
        "Ticket-quote verification" in content
    )
    assert "4 checks in `agents/plan-reviewer.md`" not in content


def test_reviewer_prompt_carries_mechanical_verdicts_input() -> None:
    content = _cmd("auto-dev-plan.md")
    window = _after(content, INPUT_ANCHOR, span=500)
    assert "`unmatched` list" in window
    assert "Step 1f.2a" in window
    assert "`none` (script ran, exit 0)" in window
    assert "`unavailable` (script absent, stale or exit 2)" in window
    assert "never re-verifies it" in window


def test_orchestrator_merges_unmatched_entries_as_citation_must_fix() -> None:
    section = _section()
    assert "MUST_FIX" in section
    assert "category `Citation`" in section
    assert "exact quote string" in section
    assert "whether or not the reviewer repeated it" in section


def test_step1f3_has_citation_only_gating_bullet() -> None:
    content = _cmd("auto-dev-plan.md")
    window = _after(content, CITATION_BULLET_ANCHOR, span=700)
    assert "citation cycle not yet used (any scope, any mode)" in window
    assert "**citation-only mode**" in window
    assert "does not require or consume the standard revision-cycle budget" in window
    assert "re-run the Step 1f.2a pre-pass first" in window


def test_step1f3_citation_fallthrough_bullet() -> None:
    content = _cmd("auto-dev-plan.md")
    window = _after(content, CITATION_FALLTHROUGH_ANCHOR, span=500)
    assert 'falls through to the standard "persists after 1 revision cycle"' in window
    assert "plan_unreviewable" in window
    assert "one-shot, not a second standard cycle" in window


def test_citation_bullets_sit_after_format_only_fallthrough() -> None:
    content = _cmd("auto-dev-plan.md")
    format_fallthrough = content.index(
        "- **MUST_FIX persists after the format-only cycle (still all `Format-Only`)**"
    )
    gating = content.index(CITATION_BULLET_ANCHOR)
    fallthrough = content.index(CITATION_FALLTHROUGH_ANCHOR)
    soundness = content.index("*Plan Soundness Reviewer verdicts:*")
    assert format_fallthrough < gating < fallthrough < soundness


def test_mixed_category_goes_through_standard_cycle() -> None:
    content = _cmd("auto-dev-plan.md")
    window = _after(content, CITATION_BULLET_ANCHOR, span=1600)
    assert "mixed-category or soundness-co-occurring MUST_FIX" in window
    assert "standard cycle" in window
    assert "`Citation` findings riding along" in window


def test_step1f4_citation_only_revision_paragraph() -> None:
    content = _cmd("auto-dev-plan.md")
    window = _after(content, CITATION_REVISION_ANCHOR, span=2400)
    assert "independent axis" in window
    assert "does **not** decrement" in window
    assert "capped at **1 attempt**" in window
    assert 'fall through to the standard "persists after 1 revision cycle"' in window
    assert "verbatim findings from every station that returned MUST_FIX" in window
    assert (
        "replace it with a verbatim quote that exists in the supplied ticket" in window
    )
    assert "re-read the cited source" in window
    assert "leaving the touch-point's claim unchanged" in window


def test_citation_paragraph_between_format_only_and_checkpoint() -> None:
    content = _cmd("auto-dev-plan.md")
    format_only = content.index(FORMAT_ONLY_REVISION_ANCHOR)
    citation = content.index(CITATION_REVISION_ANCHOR)
    checkpoint = content.index(CHECKPOINT_ANCHOR)
    assert format_only < citation < checkpoint
    raw = _cmd("auto-dev-plan.md", raw=True)
    assert raw.index(CITATION_REVISION_ANCHOR) < raw.index(RANGE_BEGIN)


def test_checkpoint_clause_names_citation_only_cycle() -> None:
    content = _cmd("auto-dev-plan.md")
    window = _after(content, CHECKPOINT_ANCHOR, span=400)
    assert (
        "from the standard MUST_FIX revision cycle, the format-only revision "
        "cycle, or the citation-only revision cycle —" in window
    )
    assert "standard MUST_FIX revision cycle" in window
    assert "format-only revision cycle" in window
    assert "draft-rewrite rule" in _after(content, CHECKPOINT_ANCHOR, span=900)


# ---------------------------------------------------------------------------
# Plan Reviewer spec
# ---------------------------------------------------------------------------


def test_reviewer_spec_has_check_5_and_reserved_category() -> None:
    content = _agent("plan-reviewer.md")
    assert "## The Five Checks" in content
    assert "## The Four Checks" not in content
    assert "### Check 5 — Ticket-quote verification" in content
    assert CHECK5_REJECT in content
    assert "**Reserved category:** `Citation`" in content
    assert "Mechanical ticket-quote verdicts" in content


def test_reviewer_spec_check_5_never_reverifies_and_cannot_waive() -> None:
    content = _agent("plan-reviewer.md")
    window = _after(content, "Check 5 — Ticket-quote verification", span=2500)
    assert "never re-verifies" in window
    assert "cannot waive" in window
    assert "N/A" in window
    assert "`none`" in window
    assert "`unavailable`" in window


def test_reviewer_spec_every_check_count_is_five() -> None:
    content = _agent("plan-reviewer.md")
    for text in (
        "does not extend to Checks 2-5",
        "gap in one of the 5 checks above",
        "plan passes all 5 checks",
        "The 5 checks are deliberately narrow",
        "doesn't map cleanly to one of the 5 checks",
    ):
        assert text in content
    for stale in ("Checks 2-4", "one of the 4 checks", "all 4 checks", "The 4 checks"):
        assert stale not in content


def test_reviewer_spec_output_template_has_check_5_line() -> None:
    content = _agent("plan-reviewer.md")
    idx = content.index(
        "### Check 5 — Ticket-quote verification\n<PASS | N/A | finding>"
    )
    assert idx > content.index("## Output Template")


def test_reviewer_citation_category_covers_mislocated_check_1_citations() -> None:
    content = _agent("plan-reviewer.md")
    window = _after(content, "**Reserved category:** `Citation`", span=1500)
    assert "Check 5" in window
    assert "mislocated" in window
    assert "re-read" in window
    assert "stays an ordinary MUST_FIX" in window


# ---------------------------------------------------------------------------
# Regression guards for text this ticket must leave alone
# ---------------------------------------------------------------------------


def test_no_v3_marker_text_and_marker_versions_unchanged() -> None:
    content = _cmd("auto-dev-plan.md")
    assert "v3" not in content
    assert "`plan-spec` `v2`, `plan-soundness` `v1`" in content


def test_format_only_bullets_and_paragraph_unchanged() -> None:
    content = _cmd("auto-dev-plan.md")
    assert (
        "- **MUST_FIX where every persisting finding's category is exactly "
        "`Format-Only`, format-only cycle not yet used (any scope, any mode)**"
    ) in content
    assert FORMAT_ONLY_REVISION_ANCHOR in content
    assert "This format-only cycle is itself capped at **1 attempt**" in content


def test_plan_stations_keep_sonnet_pins() -> None:
    content = _cmd("auto-dev-plan.md")
    assert '`subagent_type: "Plan Reviewer", model: "sonnet"`' in content
    assert '`subagent_type: "Plan Soundness Reviewer", model: "sonnet"`' in content


def test_soundness_reviewer_spec_is_untouched_by_check_5() -> None:
    content = _agent("plan-soundness-reviewer.md")
    assert "Check 5" not in content
    assert "Citation" not in content
