"""Doc guards for #3096: a clean reviewer answer is the REVIEW_FINDINGS empty frame.

Reviewers that answered a clean review with bare prose or a bare ``NO_ISSUES``
were recorded as unparseable (``reviewer_failed: <role> (unparseable_response)``).
The coordinator prompt, the Output contract and each reviewer spec must now
name the same empty frame, and the one-shot frame-missing re-prompt must be
documented in the Output contract region.

The CLI acceptance of the documented frame is proven in
``tests/test_cli_review_workspace.py``; this file proves the prose and the
specs say the same thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.codex_review._context._agent_spec import _REVIEWER_ROLE_AGENT_FILES
from cw.review_findings import ReviewerFindingsDocument
from tests._shared.workflow_text import _cmd, _norm, _section_between

_AGENTS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "agents"
_CLEAN_SECTION_START = "**Every reviewer prompt must include:**"
_OUTPUT_CONTRACT = "**Output contract.**"
_CHECKPOINT_3A = "### Checkpoint 3a"
_REPROMPT_HEAD = "**Frame-missing re-prompt (#3096).**"
_FRAME_OPEN = "<<<REVIEW_FINDINGS"
_FRAME_CLOSE = "REVIEW_FINDINGS>>>"
_ANCHOR = "## Output under a REVIEW_FINDINGS contract"
_QUALIFIER = "outside a review_findings contract"
_ROLE_FILES = sorted(set(_REVIEWER_ROLE_AGENT_FILES.values()))
# Specs that carry a pre-anchor NO_ISSUES line (each must be qualified).
_PRE_ANCHOR_NO_ISSUES = frozenset(
    {"data-safety-reviewer.md", "product-manager-reviewer.md"}
)


def _documented_empty_frame(role: str) -> str:
    section = _section_between(
        _cmd("auto-dev-review.md"), _CLEAN_SECTION_START, _OUTPUT_CONTRACT
    )
    begin = section.index(_FRAME_OPEN) + len(_FRAME_OPEN)
    end = section.index(_FRAME_CLOSE, begin)
    body = section[begin:end].strip()
    return body.replace("<role>", role).replace(
        "<what you checked>", "reviewed the assigned scope"
    )


def _reprompt_paragraph() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index(_REPROMPT_HEAD)
    end = content.index("\n\n", start)
    return _norm(content[start:end])


def _spec_lines(filename: str) -> tuple[list[str], int]:
    lines = (_AGENTS_DIR / filename).read_text(encoding="utf-8").splitlines()
    anchor_index = next(
        index for index, line in enumerate(lines) if line.strip() == _ANCHOR
    )
    return lines, anchor_index


def _no_issues_violations(filename: str) -> list[str]:
    lines, anchor_index = _spec_lines(filename)
    violations: list[str] = []
    for index, line in enumerate(lines):
        if "NO_ISSUES" not in line:
            continue
        lowered = line.lower()
        if _QUALIFIER in lowered:
            continue
        if index >= anchor_index and "never" in lowered:
            continue
        violations.append(f"{filename}:{index + 1}")
    return violations


@pytest.mark.parametrize("role", list(_REVIEWER_ROLE_AGENT_FILES))
def test_documented_clean_frame_validates_for_every_role(role: str) -> None:
    document = ReviewerFindingsDocument.model_validate_json(
        _documented_empty_frame(role)
    )

    assert document.reviewer_role == role
    assert document.status == "ok"
    assert document.findings == []
    assert document.detail.strip()


def test_clean_answer_block_forbids_prose_and_bare_no_issues() -> None:
    section = _norm(
        _section_between(
            _cmd("auto-dev-review.md"), _CLEAN_SECTION_START, _OUTPUT_CONTRACT
        )
    )

    assert "A prose-only answer and a bare `NO_ISSUES` are invalid." in section


def test_old_no_issues_if_clean_bullet_is_gone() -> None:
    assert "`NO_ISSUES` if clean" not in _cmd("auto-dev-review.md")


def test_reprompt_paragraph_sits_in_output_contract_region() -> None:
    content = _cmd("auto-dev-review.md")
    contract = content.index(_OUTPUT_CONTRACT)
    head = content.index(_REPROMPT_HEAD)
    checkpoint = content.index(_CHECKPOINT_3A)

    assert contract < head < checkpoint


@pytest.mark.parametrize(
    "phrase",
    [
        "record-reprompt",
        "exactly once",
        "exit 3",
        "record-failure",
        "no `<<<REVIEW_FINDINGS` token",
        "never re-prompted",
        "a prose answer is never treated as clean",
        "fresh `Agent` subagent",
        "not `SendMessage`",
        "subagent_type",
        "reviewer_reprompted: <role> (frame_missing)",
        "friction_highlights",
    ],
)
def test_reprompt_paragraph_documents_contract(phrase: str) -> None:
    assert phrase in _reprompt_paragraph()


@pytest.mark.parametrize("filename", _ROLE_FILES)
def test_every_reviewer_spec_carries_shared_contract_section(filename: str) -> None:
    text = (_AGENTS_DIR / filename).read_text(encoding="utf-8")

    assert _ANCHOR in text.splitlines()


@pytest.mark.parametrize("filename", _ROLE_FILES)
def test_no_issues_mentions_are_qualified_or_forbidden(filename: str) -> None:
    assert _no_issues_violations(filename) == []


def test_known_qualified_no_issues_lines_are_present() -> None:
    expected = {"data-safety-reviewer.md": 2, "product-manager-reviewer.md": 1}
    for filename in _ROLE_FILES:
        lines, anchor_index = _spec_lines(filename)
        qualified = [
            line
            for line in lines[:anchor_index]
            if "NO_ISSUES" in line and _QUALIFIER in line.lower()
        ]
        assert len(qualified) == expected.get(filename, 0), filename


@pytest.mark.parametrize(
    "filename",
    [name for name in _ROLE_FILES if name not in _PRE_ANCHOR_NO_ISSUES],
)
def test_other_specs_have_no_pre_anchor_no_issues(filename: str) -> None:
    lines, anchor_index = _spec_lines(filename)

    assert [line for line in lines[:anchor_index] if "NO_ISSUES" in line] == []
