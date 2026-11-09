"""Doc pins for the PR-body sign-off section (#3043, RFC 0017 II2).

Prose and contract pins in the ``test_review_debt_docs.py`` style: the finalize
Step 4d item 2 wiring, the ``headless-contract.md`` section 13.7 vocabulary and
the ``CONFIG_REFERENCE.md`` key. The cross-check at the end ties every token in
the library's friction grammar to the contract.
"""

from __future__ import annotations

import re
from pathlib import Path

from cw.review_signoff import _TOKEN_RE, END_MARKER, START_MARKER

ROOT = Path(__file__).resolve().parents[1]
FINALIZE = ROOT / ".claude" / "commands" / "auto-dev-finalize.md"
CONTRACT = ROOT / "docs" / "headless-contract.md"
CONFIG = ROOT / "config" / "CONFIG_REFERENCE.md"
_SIGNOFF_CMD = "cw review signoff render"
_PREFIX = "friction: review_signoff: "


def _step_4d_item2() -> str:
    text = FINALIZE.read_text(encoding="utf-8")
    start = text.index("2. **Append review adjudication to the PR body")
    end = text.index("**2b. Link the debt tickets to the PR (#3041):**")
    return text[start:end]


def _section_13_7() -> str:
    text = CONTRACT.read_text(encoding="utf-8")
    start = text.index("### 13.7 PR-body sign-off section")
    return text[start:]


def test_signoff_command_appears_once_in_item_2_between_2a_and_2b() -> None:
    text = FINALIZE.read_text(encoding="utf-8")
    assert text.count(_SIGNOFF_CMD) == 1
    item = _step_4d_item2()
    assert _SIGNOFF_CMD in item
    assert text.index("**2a. Review-debt re-file gate") < text.index(_SIGNOFF_CMD)
    assert text.index(_SIGNOFF_CMD) < text.index("**2b. Link the debt tickets")


def test_item_2_names_the_flags_branches_and_output_path() -> None:
    item = _step_4d_item2()
    for needle in (
        "--pr-head",
        "headRefOid",
        "--current-body-file",
        ".cw/pr-body.signoff.md",
        "**rc 0:**",
        "**rc 3:**",
        "**Any other rc",
        "--body-file <body-file>",
    ):
        assert needle in item, needle


def test_unmodified_body_is_saved_and_used_when_nothing_composed() -> None:
    item = _step_4d_item2()
    assert (
        "gh pr view <pr-number> --json body --jq .body > .cw/pr-body.current.md" in item
    )
    assert "`.cw/pr-body.current.md` (the unmodified body saved above)" in item


def test_override_boundary_sentence_is_pinned() -> None:
    item = _step_4d_item2()
    assert (
        "up to the next `\\n## ` heading, a `<!-- cw-review-signoff:` marker line, "
        "or EOF"
    ) in item


def test_both_prose_friction_lines_use_the_one_form() -> None:
    item = _step_4d_item2()
    assert f"{_PREFIX}skipped_cli_failed" in item
    assert f"{_PREFIX}skipped_pr_head_unreadable" in item


def test_pointer_kept_and_unconditional_rewrite_removed() -> None:
    item = _step_4d_item2()
    pointer = "via the **Write tool** — see CLAUDE.md's **Agent File Operations** rule"
    assert pointer in item
    stale = "Re-write via `gh pr edit <pr-number> --body-file .cw/pr-body.md`"
    assert stale not in FINALIZE.read_text(encoding="utf-8")


def test_contract_has_section_13_7_with_markers_and_heading() -> None:
    section = _section_13_7()
    assert START_MARKER in section
    assert END_MARKER in section
    assert "## Review sign-off" in section


def test_contract_states_validity_exit_codes_and_truncation() -> None:
    section = _section_13_7()
    assert "pr_section" in section
    assert "`valid` at local HEAD" in section
    assert "`--pr-head` equals HEAD" in section
    for code in ("- Exit 0", "- Exit 3", "- Exit 2", "- Exit 1"):
        assert code in section, code
    assert "truncated" in section
    assert "not reported" in section


def test_every_grammar_token_is_documented_in_contract() -> None:
    section = _section_13_7()
    concrete = [
        "head_mismatch",
        "invalid:untolerated_commit",
        "truncated",
        "repaired_markers",
        "debt_count_mismatch",
        "artifact_unreadable",
        "skipped_cli_failed",
        "skipped_pr_head_unreadable",
    ]
    for token in concrete:
        assert token in section, token
    for token in (
        "skipped_truncated",
        "stripped_truncated",
        "stripped_head_mismatch",
        "stripped_invalid:",
        "skipped_invalid:",
    ):
        assert token in section, token


def test_grammar_has_no_undocumented_shape() -> None:
    # Every literal alternative the library accepts must be spelled in 13.7.
    section = _section_13_7()
    samples = [
        "skipped_head_mismatch",
        "stripped_head_mismatch",
        "skipped_invalid:untolerated_commit",
        "skipped_truncated",
        "stripped_truncated",
        "repaired_markers",
        "truncated",
        "debt_count_mismatch",
        "artifact_unreadable",
        "skipped_cli_failed",
        "skipped_pr_head_unreadable",
    ]
    for token in samples:
        assert _TOKEN_RE.fullmatch(token), token
        assert token in section, token
    # The verifier reason is open-ended in the grammar; the contract documents
    # the <reason> form for both skip and strip.
    assert "stripped_invalid:<reason>" in section


def test_config_reference_documents_pr_section_in_both_places() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    assert "It has one\nkey:" not in text
    assert "It has two\nkeys:" in text
    assert "pr_section: bool = false" in text
    row = next(line for line in text.splitlines() if line.startswith("| `review` |"))
    assert "pr_section" in row


def test_changelog_carries_the_entry() -> None:
    # Why: the whole file, not just [Unreleased]; the entry moves at release.
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text
    assert "### Added" in unreleased
    assert re.search(r"#3043", unreleased)
