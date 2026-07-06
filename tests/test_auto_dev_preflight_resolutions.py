"""Guard tests: pre-flight resolutions as a binding, checked constraint (#828).

Pure-markdown assertions over the auto-dev pipeline instruction files. Mirrors the
``read_text()`` + literal-substring convention of ``test_auto_dev_model_pins.py``.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"
AGENTS = ROOT / ".claude" / "agents"
SKILLS = ROOT / ".claude" / "skills"

REFUSE = "multiple resolution comments detected — re-run /harden-ticket to consolidate"
MARKER = "<!-- auto-dev-preflight-resolutions -->"
BLOCKER_HEADER = "## Multi-Marker Gate Blocked"


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def _agent(name: str) -> str:
    return (AGENTS / name).read_text()


def _skill(name: str) -> str:
    return (SKILLS / name).read_text()


def _step1b_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1b: Generate Plan")
    end = content.index("### Step 1c:")
    return content[start:end]


def _nearby(content: str, anchor: str, span: int = 400) -> str:
    idx = content.index(anchor)
    return content[max(0, idx - span) : idx + len(anchor)]


def test_plan_refuses_multiple_marker_comments() -> None:
    """>1 marker comment refuses with the exact operator message."""
    assert REFUSE in _cmd("auto-dev-plan.md")


def test_plan_refuse_uses_ambiguities_status() -> None:
    """Multi-marker refuse reuses the ambiguities_pending_resolution status."""
    window = _nearby(_cmd("auto-dev-plan.md"), REFUSE)
    assert "ambiguities_pending_resolution" in window


def test_plan_setup_greps_preflight_marker() -> None:
    """Step 1b setup greps the pre-flight resolutions marker."""
    assert MARKER in _cmd("auto-dev-plan.md")


def test_marker_consistent_between_producer_and_consumer() -> None:
    """harden-ticket posts the same marker literal auto-dev-plan greps for.

    A typo in either file breaks pre-flight-resolution detection silently —
    exactly the drift class this ticket exists to close.
    """
    assert MARKER in _skill("harden-ticket/SKILL.md")
    assert MARKER in _cmd("auto-dev-plan.md")


def test_multi_marker_blocker_has_distinct_header() -> None:
    """The >1-marker gate posts its blocker under a distinct, greppable header."""
    assert BLOCKER_HEADER in _step1b_section()


def test_multi_marker_blocker_forbids_literal_marker() -> None:
    """The gate's blocker-comment template must never emit the literal marker (#967)."""
    section = _step1b_section()
    assert "MUST NOT contain the literal pre-flight resolutions marker" in section


def test_gate_tally_excludes_self_authored_blocker() -> None:
    """Defense in depth: the marker tally excludes the pipeline's own blocker header."""
    section = _step1b_section()
    assert "exclude any comment bearing the pipeline's own blocker header" in section
    assert BLOCKER_HEADER in section


def test_harden_directs_superseding_comment() -> None:
    """Re-harden posts a fresh superseding comment, not an append."""
    assert "## Pre-flight Resolutions (operator) — supersedes all prior" in _skill(
        "harden-ticket/SKILL.md"
    )


def test_harden_drops_accretion_guidance() -> None:
    """The 'append to the resolution comment' guidance is gone (newline-normalized)."""
    normalized = " ".join(_skill("harden-ticket/SKILL.md").split())
    assert "append to the resolution comment" not in normalized


def test_step1b_receives_all_comments() -> None:
    """Step 1b's context bullet passes ALL ticket comments, mirroring Step 1c."""
    assert "ALL ticket comments in chronological order" in _step1b_section()


def test_plan_injects_binding_resolutions_section() -> None:
    """Step 1b injects a `## Binding Pre-flight Resolutions` section."""
    assert "## Binding Pre-flight Resolutions" in _cmd("auto-dev-plan.md")


def test_plan_emits_conformance_section() -> None:
    """The producer contract requires a Pre-flight Resolution Conformance section."""
    assert "## Pre-flight Resolution Conformance" in _cmd("auto-dev-plan.md")


def test_conformance_line_format() -> None:
    """The conformance line template is specified verbatim."""
    template = (
        "- R<n>: <short restatement> — "
        "<how the plan honors it> [SATISFIED | NOT APPLICABLE]"
    )
    assert template in _cmd("auto-dev-plan.md")


def test_conformance_placed_before_ambiguities() -> None:
    """Within Step 1b, the conformance bullet precedes the Ambiguities bullet."""
    section = _step1b_section()
    conf = section.index("## Pre-flight Resolution Conformance")
    amb = section.index("## Ambiguities")
    assert conf < amb


def test_reviewer_check1_weaves_conformance() -> None:
    """Plan Reviewer Check 1 (full section) gates conformance as MUST_FIX / MISSING."""
    content = _agent("plan-reviewer.md")
    start = content.index("### Check 1 — Contract Specificity")
    end = content.index("### Check 2 — File Enumeration")
    section = content[start:end]
    assert "## Binding Pre-flight Resolutions" in section
    assert "## Pre-flight Resolution Conformance" in section
    assert "MUST_FIX" in section
    assert "MISSING" in section


def test_plan_spec_stays_v2_no_bump() -> None:
    """The plan-spec marker stays v2 — no version bump for this ticket (R4)."""
    content = _cmd("auto-dev-plan.md")
    assert "plan-spec" in content
    assert "v2" in content
    assert "v3" not in content


def test_conformance_omitted_when_no_binding_resolutions() -> None:
    """The producer bullet states the no-marker omit fallback near the Binding name."""
    anchor = "omit `## Pre-flight Resolution Conformance` entirely"
    preceding = _nearby(_cmd("auto-dev-plan.md"), anchor)
    assert "## Binding Pre-flight Resolutions" in preceding


# ---------------------------------------------------------------------------
# Zero-comment context.json staleness (#952)
# ---------------------------------------------------------------------------


def test_intake_step3_fetch_includes_comments() -> None:
    """Step 3 single-ticket github-issues fetch requests comments."""
    assert "gh issue view <n> --json title,body,state,url,comments" in _cmd(
        "auto-dev-intake.md"
    )


def test_intake_table_row_includes_comments() -> None:
    """The op-mapping 'Fetch ticket body' row (github-issues) also fetches comments."""
    content = _cmd("auto-dev-intake.md")
    row = next(ln for ln in content.splitlines() if "Fetch ticket body" in ln)
    assert "title,body,state,url,comments" in row


def test_plan_live_fetch_every_invocation() -> None:
    """Plan Stage 1 mandates a comments live-fetch on every invocation."""
    assert "live-fetch the ticket comments on every invocation" in _cmd(
        "auto-dev-plan.md"
    )


def test_plan_marker_source_pinned_to_live_fetch() -> None:
    """Step 1b marker grep is pinned to the live fetch, excluding the cache."""
    assert "NEVER the `.cw/context.json` `comments` array" in _cmd("auto-dev-plan.md")


def test_plan_cached_comments_is_snapshot_only() -> None:
    """The cached comments array is documented as a Stage-0 provenance snapshot."""
    assert "Stage-0 provenance snapshot only" in _cmd("auto-dev-plan.md")


def test_intake_warn_names_needs_attention() -> None:
    """The intake WARN names the session.needs_attention event and its reason."""
    window = _nearby(_cmd("auto-dev-intake.md"), "comments_fetch_failed")
    assert "session.needs_attention" in window


def test_intake_warn_documents_empty_undetectable() -> None:
    """Intake documents that a fetch-succeeds-but-empty case is not detectable."""
    assert (
        "NOT detectable from within this stage without a second independent source"
        in _cmd("auto-dev-intake.md")
    )


def test_intake_linear_list_comments_before_0d() -> None:
    """Linear mode mandates list_comments before Step 0d, not model initiative."""
    content = _cmd("auto-dev-intake.md")
    assert "list_comments(<id>)" in content
    assert "mandatory op that MUST run before Step 0d" in content
