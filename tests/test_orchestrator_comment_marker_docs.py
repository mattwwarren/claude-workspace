"""Orchestration-skill comment-marker docs (#2617).

Orchestrator sessions post tracker comments directly (``gh issue comment``,
``gh pr comment``, ``gh issue close --comment``, Linear ``create_comment``).
Those land under the operator's account, so each one must carry the
``<!-- cw-agent-authored -->`` marker or the Comment provenance rule reads it as
operator-typed. This module pins the one shared rule in ``/cw-orchestration``
and the per-site wording that points at it.
"""

from __future__ import annotations

import re
from pathlib import Path

from cw.gh import AGENT_COMMENT_MARKER
from tests.conftest import _norm, _section_between

SKILLS = Path(__file__).resolve().parent.parent / ".claude" / "skills"
REFERENCE = "*Posting tracker comments* in `/cw-orchestration`"
SHARED_HOME = "cw-orchestration"
RAW_POST = re.compile(
    r"gh issue comment|gh pr comment[^\n]*--body|gh issue close[^\n]*--comment"
    r"|create_comment"
)
# A skill that raw-posts but must not reference the shared rule, with the reason.
# The entry is stale when its file has no raw-post match at all.
ALLOW_LIST = {"harden-ticket": "binding resolutions comment, deliberately unmarked"}


def _skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _raw_post_skills() -> list[str]:
    names = []
    for path in sorted(SKILLS.iterdir()):
        if path.name == SHARED_HOME or not (path / "SKILL.md").is_file():
            continue
        if RAW_POST.search(_skill(path.name)):
            names.append(path.name)
    return names


def _command_semantics() -> str:
    return _section_between(_skill(SHARED_HOME), "## Command semantics", "\n## ")


def test_scanner_positive_and_negative_controls() -> None:
    """The scanner must fire on a real raw post and stay quiet on a plain cw call."""
    assert RAW_POST.search("gh issue comment 12 --body-file x.md")
    assert RAW_POST.search("gh pr comment 9 --body-file x.md")
    assert RAW_POST.search("Linear create_comment(issue)")
    assert not RAW_POST.search("cw post-comment T-1 --body-file x.md -c c")


def test_shared_rule_lives_in_cw_orchestration() -> None:
    bullets = [
        b
        for b in _command_semantics().split("\n- ")
        if "**Posting tracker comments.**" in b
    ]
    assert len(bullets) == 1
    rule = _norm(bullets[0])
    assert AGENT_COMMENT_MARKER in rule
    assert "on its own line after a blank line" in rule
    assert "cw post-comment" in rule
    assert "cw dev-queue resolve" in rule
    assert "tracker MCP" in rule
    assert "create_comment" in rule
    assert "operator-voice text unmarked" in rule
    assert "never relay an operator decision unmarked" in rule
    assert "Comment provenance rule" in rule
    assert "<!-- auto-dev-preflight-resolutions -->" in rule
    assert "cw review settle" in rule


def test_every_skill_with_a_raw_post_references_the_shared_rule() -> None:
    offenders = [
        name
        for name in _raw_post_skills()
        if name not in ALLOW_LIST and REFERENCE not in _skill(name)
    ]
    assert offenders == []


def test_allow_list_entries_are_not_stale() -> None:
    for name, reason in ALLOW_LIST.items():
        assert reason
        assert RAW_POST.search(_skill(name)), f"stale ALLOW_LIST entry: {name}"


def test_sprint_buildout_rationale_names_the_marker() -> None:
    text = _norm(_skill("sprint-buildout"))
    assert "rationale.md" in text
    assert AGENT_COMMENT_MARKER in text
    assert f"End it with a blank line and the line `{AGENT_COMMENT_MARKER}`" in text
    assert "none is needed here" not in text


def test_cw_followup_close_routes_through_post_comment() -> None:
    text = _skill("cw-followup")
    post = text.index(
        "cw post-comment <TICKET> --body-file .cw/close-comment.md -c <CLIENT>"
    )
    close = text.index("gh issue close <TICKET>")
    assert post < close
    assert "&&" in text[post:close]
    assert '--comment "<the drafted close comment>"' not in text


def test_cw_followup_mirrors_are_documented() -> None:
    text = _norm(_skill("cw-followup"))
    assert "already carries the marker" in text
    assert "deliberate exception" in text


def test_adopt_pr_comment_body_ends_with_the_marker() -> None:
    text = _norm(_skill("adopt-pr"))
    assert (
        f"End the body with a blank line and the line `{AGENT_COMMENT_MARKER}`" in text
    )


def test_cw_fanout_comment_is_a_re_read_trigger_not_the_answer() -> None:
    text = _norm(_skill("cw-fanout"))
    assert REFERENCE in text
    assert "re-read trigger" in text
    assert "never the answer" in text


def test_orchestrate_sprint_has_the_relay_anti_pattern() -> None:
    section = _section_between(
        _skill("orchestrate-sprint"), "## Anti-patterns", "\n## "
    )
    assert "Relaying operator text as an unmarked tracker comment" in section


def test_harden_ticket_names_the_one_deliberate_unmarked_post() -> None:
    text = _norm(_skill("harden-ticket"))
    assert "the one deliberately unmarked" in text
    assert "cw dev-queue resolve" in text
    assert REFERENCE in text
