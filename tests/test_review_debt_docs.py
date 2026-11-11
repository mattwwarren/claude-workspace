"""Doc pins for review-debt filing at deferral time (GitHub #3041, RFC 0017 D-7).

These guard prose workers and operators act on: Step H3's skip and headless
paragraph, finalize's re-file gate and PR-link steps, the stage skills' MCP
path, the blocker vocabulary, the events registry, the config reference and
the runbook's stated limits.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cw.auto_dev_result.schema._vocab import KNOWN_BLOCKER_REASONS
from tests.conftest import _norm, _section_between

ROOT = Path(__file__).resolve().parents[1]

_REASON = "review_debt_unfiled"
_EVENTS = (
    "review.debt_filed",
    "review.debt_deduped",
    "review.debt_filing_failed",
    "review.debt_emitted",
    "review.debt_verify_failed",
    "review.debt_closed",
    "review.debt_close_failed",
    "review.debt_enqueued",
    "review.debt_enqueue_skipped",
)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _h3() -> str:
    text = _read(".claude/commands/auto-dev.md")
    return _norm(
        _section_between(
            text,
            "### Step H3: Harvest deferred review findings from merged PRs",
            "\n### ",
        )
    )


def test_h3_skips_ticketed_entries() -> None:
    assert "skip any entry that carries a `ticket:` line" in _h3()


def test_h3_headless_paragraph_names_cw_filing() -> None:
    section = _h3()
    assert "cw files review-debt tickets at deferral time" in section
    assert "auto_create_issues: false" in section


def test_finalize_has_the_refile_gate_and_the_pr_link_step() -> None:
    text = _norm(_read(".claude/commands/auto-dev-finalize.md"))
    gate = text.index("**2a.")
    link = text.index("**2b.")
    assert gate < link
    assert "cw review debt file" in text[gate:link]
    assert "cw review debt link-pr --pr <n>" in text[link:]
    for needle in (f'"{_REASON}"', '"stage4_review_debt"', "retry_eligible"):
        assert needle in text[gate:link]


def test_review_skill_documents_the_mcp_emit_and_record_path() -> None:
    text = _norm(_read(".claude/commands/auto-dev-review.md"))
    for needle in (
        "cw review debt emit",
        "cw review debt record --ids <file>",
        _REASON,
        "friction_highlights",
    ):
        assert needle in text


def test_impl_appendix_allows_the_optional_ticket_line() -> None:
    text = _norm(_read(".claude/commands/auto-dev-impl-appendix.md"))
    assert "Each entry, exactly four lines" not in text
    assert "ticket: <issue URL" in text


def test_the_reason_is_in_every_vocabulary_surface() -> None:
    assert _REASON in KNOWN_BLOCKER_REASONS
    assert f"`{_REASON}`" in _read(".claude/commands/auto-dev.md")
    assert _REASON in _read("docs/headless-contract.md")
    assert _REASON in _read("src/cw/dev_queue/dispositions.py")


def test_events_registry_has_all_review_debt_sections() -> None:
    text = _read("docs/events.md")
    for event in _EVENTS:
        assert f"### `{event}`" in text


def test_this_repo_opts_into_auto_filing() -> None:
    config = yaml.safe_load(_read(".claude/project-config.yaml"))
    assert config["tracking"]["primary"]["auto_create_issues"] is True


def test_config_reference_and_setup_document_the_key() -> None:
    assert "tracking.primary.auto_create_issues" in _read("config/CONFIG_REFERENCE.md")
    assert "auto_create_issues: true" in _read(".claude/commands/setup.md")


def test_runbook_states_the_limits() -> None:
    text = _read("docs/dispatch-runbook.md")
    section = _norm(_section_between(text, "## Review debt filing"))
    for needle in (
        "proves the shape of the reported IDs, not that the tickets exist",
        "orphaned ticket",
        "known limitation",
        "cw review debt file",
    ):
        assert needle in section


def _close_section() -> str:
    text = _read("docs/dispatch-runbook.md")
    return _norm(
        _section_between(text, "### Closing debt of PRs that close unmerged", "\n---")
    )


def test_runbook_documents_the_close_sweep() -> None:
    section = _close_section()
    for needle in (
        "600 seconds",
        "review_debt_close_enabled: false",
        "exactly `github-issues`",
        "every linked PR is CLOSED and not merged",
        "The rule is literal",
        "reopen it if the work moved to another PR",
        "holds no lock",
        "at most 10 close attempts",
        "60 second budget",
    ):
        assert needle in section


def test_runbook_states_the_close_sweep_listing_limitation() -> None:
    section = _close_section()
    assert "**Known limitation.**" in section
    assert "1000 or more open `review-debt` issues" in section
    assert "Nothing is paginated" in section


def test_runbook_dedup_counts_only_open_row_tickets() -> None:
    section = _norm(_section_between(_read("docs/dispatch-runbook.md"), "**Dedup.**"))
    assert "(schema v57)" in section
    assert "(schema v55)" not in section
    assert "counts only while it is still OPEN" in section
    assert "closes a debt ticket by hand" in section
    assert "--limit 1000" in section
    assert "A full 1000-item page" in section


def test_config_reference_documents_the_close_switch() -> None:
    text = _read("config/CONFIG_REFERENCE.md")
    assert "\nreview_debt_close_enabled: true\n" in text
    assert "#3044" in text


def test_changelog_announces_the_close_sweep() -> None:
    text = _read("CHANGELOG.md")
    # Why: the whole file, not just [Unreleased]; the entry moves at release.
    unreleased = text
    for needle in (
        "#3044",
        "review.debt_closed",
        "review.debt_close_failed",
        "review_debt_close_enabled: false",
    ):
        assert needle in unreleased


def test_changelog_announces_the_group() -> None:
    text = _read("CHANGELOG.md")
    # Why: the whole file, not just [Unreleased]; the entry moves at release.
    unreleased = text
    assert "cw review debt" in unreleased


def test_audit_warning_token_is_declared_and_emitted_verbatim() -> None:
    # The emitter's literal, the stage prose that tells the worker to copy it,
    # and the S3 row must all name the same token (#3041 codex MUST_FIX).
    from cw.cli.review.debt import (
        _DEBT_FRICTION_PREFIXES,
        _FRICTION_DEBT_AUDIT_WARNING,
    )

    assert _FRICTION_DEBT_AUDIT_WARNING == "friction: debt_audit_warning"
    assert _FRICTION_DEBT_AUDIT_WARNING in _DEBT_FRICTION_PREFIXES
    review = _read(".claude/commands/auto-dev-review.md")
    s3_row = _read(".claude/commands/auto-dev.md")
    assert "debt_audit_warning: <n>" in review
    assert "`debt_audit_warning: <n>`" in s3_row
    assert "debt_audit_warning*" not in s3_row


def test_friction_grammar_validates_the_complete_line() -> None:
    from cw.cli.review.debt import (
        _DEBT_FRICTION_GRAMMAR,
        _emit_friction,
    )

    assert {
        rule.prefix.removeprefix("friction: "): rule.value_pattern
        for rule in _DEBT_FRICTION_GRAMMAR
    } == {
        "debt_filing": (
            "(?:skipped_no_context|skipped_no_tracker|skipped_no_session"
            "|refused_fixture_entry)"
        ),
        "debt_filing_failed": "[1-9][0-9]*",
        "debt_audit_warning": "[1-9][0-9]*",
        "debt_pr_link": "skipped_no_pr",
        "debt_pr_link_failed": "[1-9][0-9]*",
    }

    _emit_friction("friction: debt_filing_failed", "2")
    with pytest.raises(ValueError, match="unsupported debt friction line"):
        _emit_friction("friction: debt_filing_failed", "not-a-count")


def test_config_reference_documents_debt_autoenqueue() -> None:
    text = _read("config/CONFIG_REFERENCE.md")
    assert "## Review Debt Auto-Enqueue (`review.debt_autoenqueue`, #3045)" in text
    assert "review: {debt_autoenqueue: {lane: debt}}" in text
    assert "Not retroactive" in text
    assert "One level deep" in text


def test_runbook_documents_debt_autoenqueue() -> None:
    text = _read("docs/dispatch-runbook.md")
    assert "## Review debt auto-enqueue (#3045)" in text
    assert "dev_queue_review_debt_enqueue" in text
    assert "Forward-only first run" in text


def test_changelog_announces_debt_autoenqueue() -> None:
    text = _read("CHANGELOG.md")
    # Why: the whole file, not just [Unreleased]; the entry moves at release.
    unreleased = text
    for needle in ("#3045", "review.debt_enqueued", "review.debt_enqueue_skipped"):
        assert needle in unreleased


def _finalize_2a() -> str:
    text = _norm(_read(".claude/commands/auto-dev-finalize.md"))
    return text[text.index("**2a.") : text.index("**2b.")]


def test_finalize_2a_states_the_session_gate_and_the_fixture_screen() -> None:
    # #3113: outside a cw session there is no default artifact to read.
    section = _finalize_2a()
    for needle in (
        "outside a cw session",
        "--deferred-findings <path>",
        "refused_fixture_entry",
    ):
        assert needle in section


def test_runbook_states_the_session_gate_and_the_fixture_screen() -> None:
    section = _norm(
        _section_between(_read("docs/dispatch-runbook.md"), "## Review debt filing")
    )
    for needle in (
        "skipped_no_session",
        "refused_fixture_entry",
        "src/cw/foo.py",
        "stays in the artifact",
        "--deferred-findings <path>",
    ):
        assert needle in section


def test_review_skill_names_the_new_friction_tokens() -> None:
    text = _read(".claude/commands/auto-dev-review.md")
    for token in (
        "debt_filing: skipped_no_session",
        "debt_filing: refused_fixture_entry",
    ):
        assert token in text


def test_changelog_announces_the_session_gate() -> None:
    text = _read("CHANGELOG.md")
    # Why: the whole file, not just [Unreleased]; the entry moves at release.
    unreleased = text
    assert "skipped_no_session" in unreleased
    assert "cw review adjudicate" in unreleased


def test_claude_md_documents_the_real_cw_guard() -> None:
    text = _norm(_read("CLAUDE.md"))
    for needle in ("tests/_real_cw_guard.py", "real_cw_write_allowed", ".cw/tmp"):
        assert needle in text
