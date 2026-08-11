"""Guard tests: every /auto-dev subagent spawn must carry an explicit model: pin."""

from pathlib import Path

from cw.models import CONTEXT_JSON_RELATIVE_PATH, HOOK_CONTEXT_RELATIVE_PATH

COMMANDS = Path(__file__).parent.parent / ".claude" / "commands"


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def test_plan_step1b_plan_agent_pins_sonnet() -> None:
    """Step 1b Plan agent spawns Sonnet (review targets the weaker model, not Opus)."""
    content = _cmd("auto-dev-plan.md")
    assert 'subagent_type: "Plan", model: "sonnet"' in content
    assert 'subagent_type: "Plan", model: "opus"' not in content


def test_plan_step1c_pm_reviewer_pins_sonnet() -> None:
    """Step 1c PM Reviewer ambiguity-scan sentence must pin model: "sonnet"."""
    content = _cmd("auto-dev-plan.md")
    assert 'ambiguity scan** mode (`model: "sonnet"`)' in content


def test_plan_step1f2_plan_reviewer_pins_sonnet() -> None:
    """Step 1f.2 Plan Reviewer spawn must pin model: "sonnet"."""
    assert 'subagent_type: "Plan Reviewer", model: "sonnet"' in _cmd("auto-dev-plan.md")


def test_plan_step1f2_plan_soundness_reviewer_pins_sonnet() -> None:
    """Step 1f.2 Plan Soundness Reviewer spawn must pin model: "sonnet"."""
    content = _cmd("auto-dev-plan.md")
    assert 'subagent_type: "Plan Soundness Reviewer", model: "sonnet"' in content


def test_plan_step1f4_revision_agent_pins_sonnet() -> None:
    """Step 1f.4 plan-revision agent must pin sonnet (not opus like Step 1b)."""
    content = _cmd("auto-dev-plan.md")
    assert 'Re-spawn the **Plan** agent (`model: "sonnet"`)' in content


def test_review_orientation_states_comments_are_live_not_cached() -> None:
    """#1730: Stage 3 must re-fetch comments, mirroring auto-dev-impl.md's own
    "live, not cached" convention — a cached array can predate the send-back."""
    content = _cmd("auto-dev-review.md")
    assert "Comments are live, not cached (#1730)." in content


def test_review_business_context_bullet_marks_comments_binding() -> None:
    """#1730: the Business Context comments bullet must flag operator comments
    as a binding adjudication input, not passive background."""
    content = _cmd("auto-dev-review.md")
    assert "binding adjudication input" in content


def test_review_checkpoint3a_references_pending_operator_comment_marker() -> None:
    """#1730: Checkpoint 3a must name the queue_metadata marker that elevates
    the live-fetched comments, and must point at the file it is actually
    written to. A bare substring check on the marker name alone previously
    passed unchanged whether the doc named the wrong file (.cw/context.json,
    which never carries queue_metadata) or the right one -- the same
    reader/writer path-drift bug already caught, and given a mutation-proof
    test, on the codex-backend code path (test_codex_review_context.py's
    test_marker_not_read_from_stage0_ticket_context). This test pins both
    occurrences (the Orientation banner and Checkpoint 3a's (4c)) to the
    shared HOOK_CONTEXT_RELATIVE_PATH constant instead."""
    content = _cmd("auto-dev-review.md")
    hook_path = HOOK_CONTEXT_RELATIVE_PATH.as_posix()
    marker = "`queue_metadata.pending_operator_comment`"
    correct = f"`{hook_path}`'s {marker}"
    wrong = f"`{CONTEXT_JSON_RELATIVE_PATH.as_posix()}`'s {marker}"
    assert content.count(correct) == 2
    assert wrong not in content


def test_impl_spawn_heading_announces_scope_based_model() -> None:
    """Impl heading announces scope-resolved $IMPL_MODEL."""
    content = _cmd("auto-dev-impl.md")
    assert "all variants pass `model: $IMPL_MODEL`" in content


def test_impl_model_maps_scope_tier_to_model() -> None:
    """Impl model must map large->opus, small->sonnet, resolved from the scope tier."""
    content = _cmd("auto-dev-impl.md")
    assert '`large` → `"opus"`' in content
    assert '`small` → `"sonnet"`' in content


def test_impl_variants_pin_resolved_model() -> None:
    """Every impl spawn variant passes the resolved $IMPL_MODEL inline."""
    content = _cmd("auto-dev-impl.md")
    assert '`isolation: "worktree"`, `model: $IMPL_MODEL`' in content
    # dispatch-worktree variant omits isolation but still pins the resolved model
    assert "with `model: $IMPL_MODEL`" in content


def test_review_small_scope_pins_sonnet() -> None:
    """Small-scope heading and individual entries must carry model: "sonnet"."""
    content = _cmd("auto-dev-review.md")
    assert '**Small scope:** Spawn these reviewers (all `model: "sonnet"`' in content
    assert 'subagent_type: "Code Quality Reviewer", model: "sonnet"' in content


def test_review_large_scope_pins_sonnet() -> None:
    """Large-scope reviewer heading must carry model: "sonnet" annotation."""
    content = _cmd("auto-dev-review.md")
    assert '(per `/review` command patterns) (all `model: "sonnet"`' in content


def test_review_fix_agent_pins_sonnet() -> None:
    """Fix agent in review stage must pin model: "sonnet"."""
    content = _cmd("auto-dev-review.md")
    pin = '`isolation: "worktree"`, `model: "sonnet"`, and `run_in_background: true`'
    assert pin in content


def test_finalize_prior_pr_ci_fix_pins_sonnet() -> None:
    """Prior-PR CI-fix agent must pin model: "sonnet"."""
    content = _cmd("auto-dev-finalize.md")
    assert 'spawn agent (`model: "sonnet"`) in that branch' in content


def test_finalize_prep_pr_agent_pins_sonnet() -> None:
    """Step 4c /prep-pr agent must pin model: "sonnet"."""
    content = _cmd("auto-dev-finalize.md")
    assert 'Spawn a **general-purpose** agent (`model: "sonnet"`) scoped' in content


def test_finalize_ui_capture_agent_pins_haiku() -> None:
    """UI-evidence capture agent must pin model: "haiku" (cheap screenshot work)."""
    content = _cmd("auto-dev-finalize.md")
    pin = '`isolation: "worktree"`, `model: "haiku"`, `run_in_background: true`'
    assert pin in content


def test_finalize_ci_fix_agent_pins_sonnet() -> None:
    """CI-fix agent in finalize stage must pin model: "sonnet"."""
    content = _cmd("auto-dev-finalize.md")
    needle = 'Spawn agent (`model: "sonnet"`) in the worktree to investigate CI failure'
    assert needle in content


def test_finalize_feedback_address_agent_pins_sonnet() -> None:
    """Feedback-address agent must pin model: "sonnet"."""
    content = _cmd("auto-dev-finalize.md")
    needle = 'Spawn agent (`model: "sonnet"`) in the worktree. Agent reads all review'
    assert needle in content
