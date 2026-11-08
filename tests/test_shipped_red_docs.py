"""Doc pins for the red-shipped-PR recovery (GitHub #2945).

These guard prose that operators and workers act on: the runbook entry, the
events registry, the skill text and the finalize clause the "fix is
re-reviewed" guarantee leans on.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import _norm, _section_between

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _runbook_section() -> str:
    text = _read("docs/dispatch-runbook.md")
    start = text.index("### Red shipped PR")
    end = text.index("\n### ", start + 1)
    return _norm(text[start:end])


def test_runbook_names_the_page_command_and_surfaces() -> None:
    section = _runbook_section()

    for needle in (
        "shipped_pr_checks_failed",
        "cw dev-queue recover-red-pr",
        "cw queue peek",
        "RED_SHIPPED_PR",
        "review_feedback_stale",
        "Decision: regress a completed row to impl",
        "no bounded CI check in finalize",
    ):
        assert needle in section


def test_runbook_states_the_re_arm_obligation() -> None:
    section = _runbook_section()

    assert "finalize re-arms it after the recovered commit passes review" in section
    assert "The PR stays red and disarmed meanwhile" in section
    assert "Do not re-arm by hand (`gh pr merge --auto`)" in section
    assert 'reason == "cli_recover_red_pr"' in section
    assert "`was_armed` and `disarmed`" in section
    assert "there is no event" in section
    assert "outcome=row_write_refused" in section
    assert "A retry is safe" in section
    assert "#2993" in section
    assert "shipped_red_signal_enabled: false" in section


def test_runbook_defines_the_episode_literally() -> None:
    section = _runbook_section()

    assert "One page per episode" in section
    assert (
        "the row `COMPLETED`, the PR head SHA unchanged, and the PR stayed red"
        in section
    )
    assert "when a successful required-checks lookup returns no failing" in section
    assert "A lookup that fails, times out or cannot be parsed is unknown" in section
    assert "does NOT page again" in section
    assert "pages as a NEW episode" in section
    assert "a pointer, not the evidence" in section
    assert "re-reads the live failing checks, pinned to the head SHA" in section
    assert "wait for the whole PR" not in section
    assert "re-pages on a changed set" not in section


def test_runbook_lists_the_known_limits() -> None:
    section = _runbook_section()

    for needle in (
        "cancelled-only",
        "never ran",
        "STARTUP_FAILURE",
        "over-inclusive pre-filter",
        "red to green to red at the same head SHA",
        "not seen until one of those happens",
        "no required checks reported",
    ):
        assert needle in section


def test_runbook_covers_the_auto_fix_ci_recipe_interplay() -> None:
    section = _runbook_section()

    assert "`auto_fix_ci`" in section
    assert "off by default" in section
    assert "requeues the row at the SAME stage" in section
    assert "no disarm, no synthesized feedback, no fix agent" in section
    assert "Do not arm both on one lane" in section
    assert "row is not COMPLETED" in section
    assert "#2989" in section


def test_runbook_never_tells_the_operator_to_hand_edit_queue_state() -> None:
    section = _runbook_section()

    for forbidden in ("sessions.json", "dev_queue.json", "edit the queue"):
        assert forbidden not in section


def test_which_verb_table_and_from_completed_cross_reference_the_section() -> None:
    text = _norm(_read("docs/dispatch-runbook.md"))

    assert "is recovered with `recover-red-pr`, not `--from-completed`" in text
    assert 'own verb: see "Red shipped PR" above' in text


def _completed_section() -> str:
    return _norm(
        _section_between(
            _read("docs/dispatch-runbook.md"),
            "### COMPLETED row recovery (`--from-completed`)",
            "\n### ",
        )
    )


def test_from_completed_section_states_the_conflict_routes() -> None:
    section = _completed_section()
    for condition in (
        "Step 4c.5",
        "does not itself merge the default branch",
        "repair_changelog_conflict",
        "repair_mechanical_conflict",
        "never a rebase or force-push",
        "recover-red-pr",
        "BEHIND alone",
        "ADR-0022",
        "finalize will not reset over local commits",
        "git -C <worktree> status --porcelain",
        "reset --hard origin/<branch>",
    ):
        assert condition in section, condition


def test_red_section_points_at_the_status_decision() -> None:
    section = _runbook_section()
    assert "requeue <T> -c <C> --stage impl --regress" in section
    assert "RUNNING or PENDING" in section
    assert "wait" in section.split("RUNNING or PENDING", 1)[1]
    assert "BEHIND alone" in section
    assert "never approve or finalize past a red or conflicted head" in section.lower()


def test_skill_and_runbook_agree_on_the_never_past_rule() -> None:
    needle = "never approve or finalize past a red or conflicted head"
    for rel in (".claude/skills/cw-orchestration/SKILL.md", "docs/dispatch-runbook.md"):
        assert needle in _norm(_read(rel)).lower(), rel


def test_peek_skill_describes_the_red_row() -> None:
    skill = _norm(_read(".claude/skills/cw-queue-peek/SKILL.md"))

    assert "or RED_SHIPPED_PR" in skill
    assert "A `RED_SHIPPED_PR` row is not a stop candidate" in skill
    assert "never `cw spawn close`" in skill


def test_orchestration_skill_state_map_lists_the_recovery() -> None:
    skill = _norm(_read(".claude/skills/cw-orchestration/SKILL.md"))

    assert (
        "`shipped_pr_checks_failed` is recovered with `cw dev-queue recover-red-pr"
        in skill
    )


def test_finalize_open_pr_reuse_clause_is_still_present() -> None:
    """Acceptance 3 leans on finalize re-arming only behind Step 4.0 and 4.0b."""
    text = _norm(_read(".claude/commands/auto-dev-finalize.md"))

    assert "first run Step 4.0, then Step 4.0b" in text


def test_events_registry_lists_the_reason_and_requeue_reason() -> None:
    text = _norm(_read("docs/events.md"))

    assert '`"shipped_pr_checks_failed"`' in text
    assert "One event per episode" in text
    assert "`<head_sha>@<alerted_at>`" in text
    assert "cli_recover_red_pr" in text


def test_rfc_errata_records_the_decision() -> None:
    text = _norm(_read("docs/rfcs/0016-adopt-an-open-pr.md"))

    assert "A red shipped PR is recovered by regressing the COMPLETED row" in text
    assert "not by a bounded CI check in finalize" in text


def test_config_reference_documents_the_off_switch() -> None:
    text = _read("config/CONFIG_REFERENCE.md")
    assert "shipped_red_signal_enabled: true" in text
    assert "Set false to stop the per-tick `gh` polls" in text


def test_changelog_entry_exists() -> None:
    text = _read("CHANGELOG.md")
    unreleased = text[text.index("## [Unreleased]") :]
    assert "recover-red-pr" in unreleased
    assert "Closes #2905" in unreleased


def test_readme_table_has_the_command_row() -> None:
    lines = _read("README.md").splitlines()

    assert any(line.startswith("| `cw dev-queue recover-red-pr ") for line in lines)
