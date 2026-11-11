"""Guard tests: the Claude confirm round's spend is uncapped by decision (#2812).

Pure-markdown assertions in the style of ``test_review_convergence_docs.py``,
plus one code-level pin. The decision is recorded in ADR-0021, the dispatch
runbook, ``config/CONFIG_REFERENCE.md`` and the CHANGELOG. The code pin makes
"documented, no knob" falsifiable: if a spend or budget setting or a
``--max-budget-usd`` argv ever appears, the decision has been reopened and the
ADR must be superseded rather than silently contradicted.
"""

from __future__ import annotations

from pathlib import Path

from cw.claude_review import build_claude_review_argv
from cw.models.client import ReviewConfig
from tests._shared.workflow_text import _norm
from tests.test_auto_dev_preflight_resolutions import _after

ROOT = Path(__file__).resolve().parent.parent

ADR_NAME = "0021-claude-confirm-spend-is-uncapped-by-decision.md"
ADR_PATH = f"docs/adr/{ADR_NAME}"
RUNBOOK = "docs/dispatch-runbook.md"
CONFIG_REFERENCE = "config/CONFIG_REFERENCE.md"
CHANGELOG = "CHANGELOG.md"

CODEX_LANE_HEADING = "#### Codex-lane clean mode (#2791)"
HISTORY_HEADING = "### Reading a review's history (cw review history, #2790)"
SPEND_BULLET = "- **Confirm spend is uncapped (#2812).**"
CONFIG_ANCHOR = "**Uncapped confirm spend (#2812):**"
CHANGELOG_ANCHOR = (
    "**The Claude confirm round's spend stays uncapped by decision (#2812).**"
)
REVIEW_ROW = "| `review` | ReviewConfig |"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_adr_0021_records_the_uncapped_decision() -> None:
    path = ROOT / ADR_PATH
    assert path.is_file()
    raw = path.read_text(encoding="utf-8")
    assert "**Status:** Accepted" in raw
    assert "**Driven by:** #2812" in raw
    text = _norm(raw)
    for needle in (
        "no per-run or per-ticket spend ceiling",
        "`cw review confirm --backend claude`",
        "`max_cycles`",
        "wall-clock budget",
        "`effective_max_cycles + 1`",
        "`review.confirm_backend: none`",
        "`review.convergence: blocking`",
        "self-reported",
        "not recorded",
        "real cost data",
    ):
        assert needle in text, needle


def test_adr_0021_is_indexed() -> None:
    index = _read("docs/adr/README.md")
    link = f"[0021]({ADR_NAME})"
    assert link in index
    row = next(line for line in index.splitlines() if link in line)
    assert row.rstrip().endswith("| Accepted |")


def test_config_reference_states_the_uncapped_decision() -> None:
    content = _read(CONFIG_REFERENCE)
    row_start = content.index(REVIEW_ROW)
    row = content[row_start : content.index("\n", row_start)]
    window = _after(row, CONFIG_ANCHOR, span=1200)
    for needle in (
        "no spend ceiling",
        "`max_cycles`",
        "`confirm_backend: none`",
        "`convergence: blocking`",
        "not recorded",
    ):
        assert needle in window, needle
    assert "confirm_max_spend_usd" not in content


def test_runbook_states_the_uncapped_decision_in_the_codex_lane_section() -> None:
    runbook = _read(RUNBOOK)
    start = runbook.index(CODEX_LANE_HEADING)
    end = runbook.find("\n#### ", start + len(CODEX_LANE_HEADING))
    section = runbook[start:] if end == -1 else runbook[start:end]
    assert SPEND_BULLET in section
    bullet = _norm(_after(section, SPEND_BULLET, span=1500))
    for needle in (
        "ADR-0021",
        "`review.confirm_backend: none`",
        "`review.convergence: blocking`",
        "`cw review confirm --backend claude`",
        "`total_cost_usd`",
        "not recorded",
    ):
        assert needle in bullet, needle


def test_runbook_history_section_says_cost_is_not_recorded() -> None:
    window = _norm(_after(_read(RUNBOOK), HISTORY_HEADING, span=6000))
    assert "cost is not recorded" in window


def test_changelog_entry_sits_under_unreleased() -> None:
    changelog = _read(CHANGELOG)
    unreleased = changelog[: changelog.index("\n## [1.82.0]")]
    assert CHANGELOG_ANCHOR in unreleased
    entry = _norm(_after(unreleased, CHANGELOG_ANCHOR, span=1500))
    for needle in (
        "ADR-0021",
        "no new config key",
        "`review.confirm_backend: none`",
    ):
        assert needle in entry, needle


def test_no_spend_cap_exists_in_code() -> None:
    capped = [f for f in ReviewConfig.model_fields if "spend" in f or "budget" in f]
    assert not capped
    for model in (None, "m"):
        argv = build_claude_review_argv(model=model, schema="{}")
        assert "--max-budget-usd" not in argv
