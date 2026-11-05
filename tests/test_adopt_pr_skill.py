"""Prose-sync pins for the ``/adopt-pr`` skill (RFC 0016 R12, #2943)."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = (_ROOT / ".claude" / "skills" / "adopt-pr" / "SKILL.md").read_text(
    encoding="utf-8"
)
_GUIDE = (_ROOT / "src" / "cw" / "data" / "GUIDE.md").read_text(encoding="utf-8")
_POINTER = "via the **Write tool** — see CLAUDE.md's **Agent File Operations** rule"


def test_frontmatter_names_the_skill() -> None:
    assert _SKILL.startswith("---\nname: adopt-pr\ndescription: ")


def test_the_four_rungs_appear_in_order() -> None:
    rungs = [
        "1. **Explicit ticket.**",
        "2. **`closingIssuesReferences`.**",
        "3. **Agent parse.**",
        "4. **The knock.**",
    ]
    positions = [_SKILL.index(rung) for rung in rungs]
    assert positions == sorted(positions)


def test_echo_comes_before_the_add() -> None:
    echo = _SKILL.index("## Step 3: Echo before adding")
    add = _SKILL.index("```bash\ncw dev-queue add <TICKET> --adopt-pr <N> -c <client>")
    assert echo < add


def test_knock_offers_ticket_pr_as_ticket_or_stop() -> None:
    assert "**a ticket**" in _SKILL
    assert "**PR-as-ticket**" in _SKILL
    assert "**stop**" in _SKILL


def test_fork_prs_are_refused() -> None:
    assert "fork PRs are not adoptable" in _SKILL


def test_cross_tracker_id_is_a_label_only() -> None:
    assert "never becomes the row's" in _SKILL


def test_gh_bodies_use_the_write_tool_and_body_file() -> None:
    assert _POINTER in _SKILL
    assert "--body-file" in _SKILL


def test_guide_lists_the_skill() -> None:
    assert "- `adopt-pr` — adopt an open human-authored PR (RFC 0016)" in _GUIDE
