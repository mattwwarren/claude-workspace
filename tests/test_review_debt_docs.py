"""Doc pins for review-debt filing at deferral time (GitHub #3041, RFC 0017 D-7).

These guard prose workers and operators act on: Step H3's skip and headless
paragraph, finalize's re-file gate and PR-link steps, the stage skills' MCP
path, the blocker vocabulary, the events registry, the config reference and
the runbook's stated limits.
"""

from __future__ import annotations

from pathlib import Path

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


def test_events_registry_has_all_five_sections() -> None:
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


def test_changelog_announces_the_group() -> None:
    text = _read("CHANGELOG.md")
    start = text.index("## [Unreleased]")
    unreleased = text[start : text.index("\n## [", start + 1)]
    assert "cw review debt" in unreleased
