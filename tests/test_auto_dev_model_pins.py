"""Guard tests: every /auto-dev subagent spawn must carry an explicit model: pin."""

from pathlib import Path

COMMANDS = Path(__file__).parent.parent / ".claude" / "commands"


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def test_plan_step1b_plan_agent_pins_opus() -> None:
    """Step 1b Plan agent spawn must pin model: "opus"."""
    assert 'subagent_type: "Plan", model: "opus"' in _cmd("auto-dev-plan.md")


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


def test_impl_spawn_heading_announces_opus() -> None:
    """Impl spawn-shape heading must announce model: "opus" for code generation."""
    assert 'model: "opus"' in _cmd("auto-dev-impl.md")


def test_review_small_scope_pins_sonnet() -> None:
    """Small-scope reviewer heading must carry model: "sonnet" annotation."""
    content = _cmd("auto-dev-review.md")
    assert '**Small scope:** Spawn these reviewers (all `model: "sonnet"`' in content


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
