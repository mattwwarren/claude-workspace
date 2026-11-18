"""Doc lint: stage docs speak in ``tickets`` role terms (RFC 0019 R3, #3704).

The stage docs under ``.claude/commands`` name tracker operations by role, as
the rendered ``Bindings`` block names them, instead of by a concrete tracker
tool. This lint keeps them from drifting back:

- No ``mcp__plugin_linear*`` tool name and no bare ``get_issue``,
  ``list_comments``, ``list_issues``, ``save_issue``, ``save_comment`` or
  ``create_comment`` outside a fenced code block, except on an allow-listed
  line (:data:`ROLE_ALLOW`), each with a recorded reason.
- No operation table (a contiguous markdown table with a backticked
  ``linear`` cell and a ``gh issue`` cell).
- The Tracker Resolution pointer names the Bindings block and its delimiters.

Positive controls prove each rule fires; negative controls prove fences and
prose do not. The needles for the operation names come from ``_OP_PHRASES``,
so renderer drift breaks this file.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from cw.bindings_block import _OP_PHRASES, BINDINGS_BEGIN, BINDINGS_END
from cw.contract_manifest import DOC_SCOPES
from cw.markdown_fence import classify_lines
from cw.models.bindings import TicketOp
from tests._shared.workflow_text import _cmd

if TYPE_CHECKING:
    from collections.abc import Sequence

DOCS = tuple(DOC_SCOPES)
CONTEXT_CHARS = 120
POINTER_TAG = "<!-- el: intake.tracker-resolution -->"
POINTER_HEADING = "## Tracker Resolution (read first — before Stage 0)"
# Tracker tool names and bare operation names. Matched only outside fences.
FORBIDDEN = re.compile(
    r"mcp__plugin_linear\w*"
    r"|\b(?:get_issue|list_comments|list_issues|save_issue|save_comment|create_comment)\b"
)
TABLE_LINEAR_CELL = re.compile(r"`linear`")
TABLE_GH_CELL = re.compile(r"gh issue\b")

# {doc: [(single-line anchor substring, reason)]}. A forbidden-token line must
# contain one of its doc's anchors; every anchor must still be in its doc.
ROLE_ALLOW: dict[str, list[tuple[str, str]]] = {
    "auto-dev.md": [
        (
            "passed to `create_comment` successfully",
            "tracker post seam (#2810), owned by O3: emit-only Linear part post",
        ),
        (
            "part's `create_comment` fails",
            "tracker post seam (#2810), owned by O3: emit-only Linear part post",
        ),
        (
            "or `create_comment` fallback is allowed",
            "tracker post seam (#2810): the no-raw-fallback rule names the op it "
            "forbids",
        ),
        (
            "`create_comment`, oldest first",
            "tracker post seam (#2810), owned by O3: emit-only Linear part post",
        ),
        (
            "Linear `create_comment`)",
            "tracker post seam (#2810): the Producers MUST mark rule names the raw op",
        ),
    ],
    "auto-dev-finalize-appendix.md": [
        (
            "create_comment",
            "tracker post seam (#2810): the emit-only Linear part-file post",
        ),
    ],
    "auto-dev-review.md": [
        (
            "create_comment",
            "tracker post seam (#2810): the emit-only Linear part-file post and "
            "the convergence-history comment",
        ),
    ],
    "auto-dev-impl-appendix.md": [
        (
            "Linear: `list_comments` returns no GitHub-style",
            "D-D3 fail-closed rule (identity_unresolved), Epic IV (R6): not rewritten",
        ),
    ],
}


def _offenders(text: str, anchors: Sequence[str] = ()) -> list[str]:
    """Forbidden tracker tokens and operation tables outside fences.

    Returns ``line N: <context>`` strings. A line is exempt only when it
    contains one of *anchors*; another offending line still fires.
    """
    lines = text.splitlines()
    kinds = classify_lines(lines)
    found: list[str] = []
    for index, (line, kind) in enumerate(zip(lines, kinds, strict=True)):
        if kind == "fence":
            continue
        if FORBIDDEN.search(line) and not any(a in line for a in anchors):
            found.append(f"line {index + 1}: {line[:CONTEXT_CHARS]}")
    found.extend(_table_offenders(lines, kinds))
    return found


def _table_offenders(lines: Sequence[str], kinds: Sequence[str]) -> list[str]:
    """Operation tables: a contiguous table block with a linear and a gh cell."""
    found: list[str] = []
    start: int | None = None
    for index in range(len(lines) + 1):
        in_table = index < len(lines) and kinds[index] == "table"
        if in_table and start is None:
            start = index
        elif not in_table and start is not None:
            block = lines[start:index]
            has_linear = any(TABLE_LINEAR_CELL.search(row) for row in block)
            has_gh = any(TABLE_GH_CELL.search(row) for row in block)
            if has_linear and has_gh:
                found.append(f"line {start + 1}: operation table ({len(block)} rows)")
            start = None
    return found


def _role_allow(doc: str) -> list[str]:
    return [anchor for anchor, _reason in ROLE_ALLOW.get(doc, [])]


def _pointer_section(content: str) -> str:
    start = content.index(POINTER_TAG)
    end = content.index("\n---", start)
    return content[start:end]


# ---- positive controls: each rule fires -------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Call get_issue(<id>) to fetch the body.",
        "Read the `list_comments(<id>)` result.",
        "Use list_issues with the filters.",
        "Save via save_issue.",
        "Reply with save_comment.",
        "Post with create_comment.",
        "Use mcp__plugin_linear_linear__save_issue directly.",
    ],
)
def test_forbidden_token_fires(line: str) -> None:
    assert _offenders(line), line


def test_forbidden_token_fires_in_table_row() -> None:
    text = "| Fetch | `list_comments(<id>)` | x |\n"
    assert any("list_comments" in o for o in _offenders(text))


def test_operation_table_fires() -> None:
    text = (
        "| Operation | `linear` | `github-issues` |\n"
        "|---|---|---|\n"
        "| Fetch | get_issue(<id>) | `gh issue view <n>` |\n"
    )
    assert any("operation table" in o for o in _offenders(text))


def test_second_offender_on_anchored_line_still_fires() -> None:
    anchor = "Linear `create_comment`)"
    text = f"see ({anchor}\n\nlater get_issue(<id>) again\n"
    found = _offenders(text, anchors=[anchor])
    assert found == ["line 3: later get_issue(<id>) again"]


# ---- negative controls: fences and prose do not fire -----------------------


@pytest.mark.parametrize(
    "text",
    [
        "```\nget_issue(<id>)\nmcp__plugin_linear_linear__save_issue\n```\n",
        "bounded linear backoff between retries\n",
        "the plain word linear in prose\n",
        "Linear create-comment wording in prose\n",
        f"{_OP_PHRASES[TicketOp.GET]} with the labeled gh command\n",
        f"{_OP_PHRASES[TicketOp.LIST_COMMENTS]} before Step 0d\n",
    ],
)
def test_negative_control_does_not_fire(text: str) -> None:
    assert _offenders(text) == [], text


def test_gh_issue_in_prose_is_not_a_table() -> None:
    text = "Run `gh issue comment <n> --body-file` by hand.\n"
    assert _offenders(text) == []


# ---- real docs --------------------------------------------------------------


@pytest.mark.parametrize("doc", DOCS)
def test_no_stage_doc_names_a_tracker_tool_or_operation_table(doc: str) -> None:
    content = _cmd(doc, raw=True)
    assert _offenders(content, anchors=_role_allow(doc)) == [], doc


def test_allow_list_entries_are_live_and_reasoned() -> None:
    for doc, entries in ROLE_ALLOW.items():
        assert doc in DOC_SCOPES, doc
        content = _cmd(doc, raw=True)
        for anchor, reason in entries:
            assert anchor in content, f"stale allow-list anchor in {doc}: {anchor!r}"
            assert reason.strip(), f"blank reason for {anchor!r} in {doc}"


def test_pointer_names_bindings_block_and_role_rules() -> None:
    section = _pointer_section(_cmd("auto-dev-intake.md", raw=True))
    assert POINTER_TAG in section
    assert POINTER_HEADING in section
    assert BINDINGS_BEGIN in section
    assert BINDINGS_END in section
    assert "bindings_block" in section
    assert ".claude/cw-context.json" in section
    assert "the `tickets` role" in section
    assert _OP_PHRASES[TicketOp.GET] in section
    assert _OP_PHRASES[TicketOp.LIST_COMMENTS] in section
    assert "A bare integer argument such as `403`" in section
    assert "Never authorize or call a tracker MCP tool" in section
