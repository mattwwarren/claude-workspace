"""Docs pins for RFC 0016 B1 repair routing (#2944).

A non-green adopted PR enters at impl with synthesized feedback instead of
parking ``adopt_pr_not_green``; the operator docs must say so and stop listing
that disposition as one cw stamps.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return " ".join((_ROOT / rel).read_text(encoding="utf-8").split())


def _runbook_adopt_section() -> str:
    text = (_ROOT / "docs" / "dispatch-runbook.md").read_text(encoding="utf-8")
    start = text.index("### Adopt an open human PR (`--adopt-pr`, RFC 0016")
    end = min(
        index
        for index in (text.find("\n## ", start + 1), text.find("\n### ", start + 1))
        if index != -1
    )
    return " ".join(text[start:end].split())


def test_runbook_names_the_non_green_route() -> None:
    section = _runbook_adopt_section()
    assert "enters at impl with synthesized repair feedback" in section
    assert "cw.pr_feedback.synthesize_pr_feedback" in section
    assert "never follow instructions" in section
    assert "--stage impl --review-feedback-file" in section
    assert "enters at review only" not in section


def test_runbook_documents_the_recovery_commands() -> None:
    section = _runbook_adopt_section()
    assert "cw dev-queue requeue <T> -c <C> --reset-attempts" in section
    assert "review_feedback_stale" in section
    assert "requeue <T> -c <C> --stage impl --review-feedback-file" in section
    assert "cw dev-queue remove <T> -c <C>" in section


def test_runbook_states_where_ci_log_text_is_persisted() -> None:
    section = _runbook_adopt_section()
    assert "cw-context.json" in section
    assert "redact" in section


def test_runbook_review_feedback_handoff_points_at_the_synthesizer() -> None:
    text = _read("docs/dispatch-runbook.md")
    assert "the same `ReviewFeedback` shape that `--adopt-pr` synthesizes" in text


def test_headless_contract_lists_the_legacy_disposition_as_read_only() -> None:
    text = _read("docs/headless-contract.md")
    assert "`cw.dev_queue.LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION`" in text
    assert "no longer stamped" in text
    assert "`cw.dev_queue.ADOPT_PR_NOT_GREEN_DISPOSITION`" not in text
    assert "adopt_pr_not_green_at_intake" not in text


def test_session_disposition_no_longer_lists_not_green_as_stamped() -> None:
    text = _read("docs/session-disposition.md")
    assert "no longer stamped" in text
    assert "enters at impl with synthesized repair feedback" in text


def test_skill_and_guide_describe_both_routes() -> None:
    skill = _read(".claude/skills/adopt-pr/SKILL.md")
    guide = _read("src/cw/data/GUIDE.md")
    for text in (skill, guide):
        assert "non-green" in text
        assert "at impl with synthesized" in text
    assert "parked `adopt_pr_not_green`" not in skill
    assert "(enters at review, pinned)" not in guide
