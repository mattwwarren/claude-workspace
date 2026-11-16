"""Doc pins for the PyYAML-missing ``ci_evidence`` hold (#3673).

Every surface that restates when ``awaiting_ci_evidence`` fires must name the
PyYAML clause, and the operator-facing runbook must name the exit. Text is
whitespace-normalised so a wrapped line does not hide a phrase.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLAUSE = "the base branch declares `ci_evidence` but PyYAML is unavailable"


def _norm(relative: str) -> str:
    return " ".join((ROOT / relative).read_text(encoding="utf-8").split())


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/commands/auto-dev-finalize-appendix.md",
        ".claude/commands/auto-dev.md",
        "docs/headless-contract.md",
        "docs/dispatch-runbook.md",
        "config/CONFIG_REFERENCE.md",
        ".claude/skills/cw-orchestration/SKILL.md",
        ".claude/commands/auto-dev-finalize.md",
    ],
)
def test_every_restated_trigger_names_the_pyyaml_clause(relative: str) -> None:
    assert CLAUSE in _norm(relative)


def test_runbook_park_paragraph_names_the_exit() -> None:
    text = _norm("docs/dispatch-runbook.md")
    assert "pyyaml-unavailable" in text
    assert "install PyYAML" in text
    assert "a requeue alone re-parks until then" in text


def test_finalize_park_bullet_keeps_the_no_arm_rule() -> None:
    text = _norm(".claude/commands/auto-dev-finalize.md")
    assert "never run the gate locally and never arm" in text
    assert "`hold_error`" in text


def test_appendix_park_sourced_hold_error_strings_are_verbatim() -> None:
    text = _norm(".claude/commands/auto-dev-finalize-appendix.md")
    assert (
        "ci_evidence_hold_error: PR #<n> (<url>) at head <sha>: ci-reuse-plan could "
        "not read quality_gates.ci_evidence (<cause>). cw did not arm auto-merge and "
        "did not run any gate locally."
    ) in text
    assert (
        "quality_gates.ci_evidence could not be read without PyYAML; cw did not arm "
        "auto-merge and a requeue re-parks until PyYAML is installed or the config "
        "changes"
    ) in text


def test_changelog_fixed_entry_under_unreleased() -> None:
    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("## [Unreleased]")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## ["))
    section = "\n".join(lines[start:end])
    assert "### Fixed" in section
    entry = next(
        line for line in lines[start:end] if "no longer silently ignores" in line
    )
    for token in (
        "#3673",
        "pyyaml-unavailable",
        "ci_evidence_hold_error",
        "PyYAML",
        "requeue",
        "an unrelated key named `ci_evidence`",
    ):
        assert token in entry
