"""Guard tests: finalize ``agent_block`` findings reach the regressed IMPL run (#2794).

Pure-markdown assertions over the auto-dev pipeline instruction files, in the
``read_text()`` + literal-substring/window convention of
``test_blocking_findings_comment.py``: ``_cmd``/``_appendix`` come from
``tests.conftest`` and ``_after`` from ``test_auto_dev_preflight_resolutions``.

Background: when ``/prep-pr``'s ship-it gate blocks a FINALIZE run on review
findings, the sentinel collapses to ``agent_block`` and Sub-rule 5a regresses
the ticket to IMPL, but the only channel carrying the findings there was
``prior_attempts_summary``, capped at 500 characters. Option A (operator
decision): finalize's main session posts the findings, redacted and in parts,
as an agent-authored ``## Blocking Review Findings`` comment under the
sub-heading ``### Finalize ship-it gate findings``, then stamps
``cw signal-park``; the IMPL appendix's regress findings carve-out passes that
one post to the Stage 2 agent as labelled evidence.

The post itself is prompt-driven, so the over-500-character acceptance case is
composed from layers: the documented body shape, the unchanged
``prior_attempts_summary`` cap, the redactor's no-truncation behavior, and the
IMPL carve-out's verbatim-delivery clause.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cw.auto_dev_result.schema import FINALIZE_REGRESS_BLOCKER_REASONS
from cw.gh import AGENT_COMMENT_MARKER
from cw.spawn import _PRIOR_ATTEMPT_DETAILS_MAX_LEN
from tests.conftest import (
    _appendix,
    _bash_fences,
    _cmd,
    load_guard_script_module,
    substitute_fence_placeholders,
)
from tests.test_agent_comment_provenance import PIPELINE_HEADERS
from tests.test_auto_dev_finalize_automerge_verification import _ORDER_OF_EVALUATION
from tests.test_auto_dev_preflight_resolutions import _after
from tests.test_bodyfile_write_tool_conformance import (
    POINTER,
    RESTATED_ARGUMENT_PHRASES,
)
from tests.test_impl_guard_staleness_docs import (
    CARVE_OUT_ANCHOR,
    FINDINGS_HEADER,
    FINDINGS_SUBHEADING,
    _carve_out_clause,
    _carve_out_lines,
)
from tests.test_sentinel_emission_discipline import (
    FINALIZE_FINDINGS_SECTION,
    FINALIZE_PARK_MARKER,
    _finalize_findings_section,
    _paragraph_containing,
)

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
REDACTOR = ROOT / ".claude" / "scripts" / "redact_post_text.py"

RULE_REFERENCE = "blocking-findings comment rule"
CORE_ANCHOR = "**Finalize review-findings comment (#2794):**"
ORDER_CLAUSE = (
    "; within step (4), a review-findings block also posts the findings comment"
)
PART_LABEL = "(part i of n)"
READ_BACK = "**Read-back (G4).**"
FAILED_FRICTION = "blocking findings post failed: finalize_agent_block"
SKIPPED_FRICTION = "blocking findings post skipped: redactor_unavailable"
POSTED_FRICTION = "blocking findings posted: finalize_agent_block"
REVIEW_PARK_IGNORE = (
    "If it is denied, exits non-zero, is an unknown command (an older `cw`), or "
    "says `park marker NOT recorded`, ignore that and emit the exit sentinel "
    "unchanged."
)

ROW_FINALIZE_FINDINGS_ANCHOR = (
    "| S4c `/prep-pr` BLOCK whose details are review findings"
)
MEANING_ROW_AGENT_BLOCK_ANCHOR = (
    "| `agent_block` | Any other agent returned friction level BLOCK that the "
    "pipeline could not auto-resolve"
)

ACCEPTANCE_DETAILS_CHARS = 60_500
GITHUB_PART_LIMIT = "60,000"


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def _core_paragraph() -> str:
    return _paragraph_containing(_cmd("auto-dev-finalize.md"), CORE_ANCHOR)


def _section_paragraph(anchor: str) -> str:
    return _paragraph_containing(_finalize_findings_section(), anchor)


# ---------------------------------------------------------------------------
# Core doc: the Step 4c paragraph and the order of evaluation
# ---------------------------------------------------------------------------


def test_core_paragraph_follows_the_base_branch_state_classifier() -> None:
    content = _cmd("auto-dev-finalize.md")
    classifier = content.index("**Base-branch-state classifier (#2320):**")
    paragraph = content.index(CORE_ANCHOR)
    assert classifier < paragraph < content.index("**Unavailability classifier")


def test_core_paragraph_names_trigger_order_and_rule() -> None:
    paragraph = _core_paragraph()
    for phrase in (
        "`gate:` text begins `Step 8 ship-it` or equals `Step 7 max review cycles`",
        "the literal token `MUST_FIX`",
        "only after the Unavailability classifier, the denied-arm check (#2625) "
        "and the Base-branch-state classifier (#2320) have not claimed the block",
        "finalize's main session",
        RULE_REFERENCE,
        FINDINGS_HEADER,
        FINDINGS_SUBHEADING,
        "posts nothing, exactly as today",
        "`external_state_block`",
        "merge conflict",
        "Park marker clause",
        FINALIZE_FINDINGS_SECTION.removeprefix("## "),
    ):
        assert phrase in paragraph, phrase


def test_prep_pr_still_produces_the_gates_the_trigger_names() -> None:
    """The trigger matches `/prep-pr`'s `gate:` text; a rename there would turn
    the whole feature into a silent no-op, so pin both producers."""
    prep_pr = _cmd("prep-pr.md")
    assert 'gate: "Step 7 max review cycles"' in prep_pr
    assert 'gate: "Step 8 ship-it' in prep_pr
    trigger = _core_paragraph()
    assert "Step 7 max review cycles" in trigger
    assert "Step 8 ship-it" in trigger


def test_core_paragraph_does_not_name_the_park_stamp_command() -> None:
    assert "cw signal-park" not in _core_paragraph()


def test_order_of_evaluation_pin_holds_and_findings_clause_follows_it() -> None:
    """G2: the #2625 order string stays byte-identical; the clause is appended."""
    content = _cmd("auto-dev-finalize.md")
    assert _ORDER_OF_EVALUATION in content
    assert ORDER_CLAUSE not in _ORDER_OF_EVALUATION
    assert content.index(ORDER_CLAUSE) == content.index(_ORDER_OF_EVALUATION) + len(
        _ORDER_OF_EVALUATION
    )


# ---------------------------------------------------------------------------
# Appendix section: trigger, body shape, provenance, sentinel
# ---------------------------------------------------------------------------


def test_section_sits_before_the_unavailability_classifier() -> None:
    appendix = _appendix("finalize")
    assert appendix.index(FINALIZE_FINDINGS_SECTION) < appendix.index(
        "## Unavailability classifier: signature table"
    )


def test_section_names_header_subheading_and_marker() -> None:
    section = _finalize_findings_section()
    assert f"`{FINDINGS_HEADER}`" in section
    assert f"`{FINDINGS_SUBHEADING}`" in section
    assert section.index(f"`{FINDINGS_HEADER}`") < section.index(
        f"`{FINDINGS_SUBHEADING}`"
    )
    assert AGENT_COMMENT_MARKER in section
    assert RULE_REFERENCE in section


def test_section_states_the_trigger_and_its_accepted_limit() -> None:
    paragraph = _section_paragraph("**Trigger.**")
    assert "`Step 8 ship-it`" in paragraph
    assert "`Step 7 max review cycles`" in paragraph
    assert "`MUST_FIX`" in paragraph
    assert "**Accepted limit:**" in paragraph
    assert "takes today's path" in paragraph


def test_body_is_gate_plus_verbatim_details_untruncated() -> None:
    paragraph = _section_paragraph("**Body shape.**")
    assert "`gate:` line plus its `details:` text, posted verbatim, not truncated" in (
        paragraph
    )
    assert "`_PRIOR_ATTEMPT_DETAILS_MAX_LEN`" in paragraph
    assert "500-character" in paragraph
    assert "does not apply to this path" in paragraph


def test_section_keeps_agent_block_and_details_unchanged() -> None:
    paragraph = _section_paragraph("**Sentinel.**")
    assert '`blocker.reason` stays `"agent_block"`' in paragraph
    assert "`blocker.details` is unchanged" in paragraph
    assert POSTED_FRICTION in paragraph
    assert "friction_highlights" in paragraph


def test_failed_post_is_never_stamped() -> None:
    paragraph = _section_paragraph("**Step 4 — post.**")
    assert FAILED_FRICTION in paragraph
    assert "do not run `cw signal-park`" in paragraph


# ---------------------------------------------------------------------------
# Redaction: ordering (R5) and fail closed (A6)
# ---------------------------------------------------------------------------


def test_redaction_runs_before_measuring_and_splitting() -> None:
    section = _finalize_findings_section()
    assert section.index("redact_post_text.py") < section.index(PART_LABEL)
    assert section.index("**Step 2 — redact.**") < section.index(
        "**Step 3 — measure and split.**"
    )
    assert (
        "The redaction output, not the raw text, is what gets measured, split "
        "and posted" in section
    )
    assert (
        "the header, sub-heading, label line, fence and marker are added after "
        "redaction" in section
    )


def test_redactor_absent_stale_or_failing_fails_closed() -> None:
    paragraph = _section_paragraph("**Fail closed (A6).**")
    for phrase in (
        "absent from both locations",
        "below the minimum `cw-script-version`",
        "non-zero `REDACT_EXIT`",
        "post nothing",
        "do not run `cw signal-park`",
        SKIPPED_FRICTION,
        "otherwise unchanged",
    ):
        assert phrase in paragraph, phrase


def test_headless_block_bullet_reconciles_with_fail_closed_outcome() -> None:
    """G1: the HEADLESS-BLOCK bullet names the condition, not an EXIT."""
    bullet = next(
        line
        for line in _finalize_findings_section().splitlines()
        if "HEADLESS BLOCK" in line and "redact_post_text.py" in line
    )
    assert "need >= 1" in bullet
    assert "NOT an `EXIT blocked`" in bullet
    assert "fails closed and continues" in bullet
    assert "`redactor_unavailable`" in bullet


SCRATCH_FILES = (
    "finalize-findings.txt",
    "finalize-findings.redacted.txt",
    "finalize-findings-part-*.md",
    "finalize-findings-readback-*.md",
)


def _pre_clean_fence() -> str:
    fences = [
        fence
        for fence in _bash_fences(_finalize_findings_section())
        if "rm -f" in fence and "redact_post_text.py" not in fence
    ]
    assert len(fences) == 1
    return fences[0]


def test_leftover_scratch_files_are_cleared_before_the_raw_text_is_composed() -> None:
    """Finalize re-runs in the same worktree on a regress attempt: attempt 1's
    part and readback files must not be posted or compared by attempt 2."""
    section = _finalize_findings_section()
    assert section.index("**Step 0 — clear leftovers.**") < section.index(
        "**Step 1 — compose the raw text.**"
    )
    paragraph = _section_paragraph("**Step 0 — clear leftovers.**")
    assert "on a regress attempt" in paragraph
    fence = _pre_clean_fence()
    assert fence.count("GUARD_ROOT=$(git rev-parse --show-toplevel") == 1
    for name in SCRATCH_FILES:
        assert f"/.cw/{name}" in fence, name


def test_pre_clean_fence_removes_only_the_four_scratch_families(
    tmp_path: Path,
) -> None:
    cw_dir = tmp_path / "repo" / ".cw"
    cw_dir.mkdir(parents=True)
    planted = [
        "finalize-findings.txt",
        "finalize-findings.redacted.txt",
        "finalize-findings-part-1.md",
        "finalize-findings-part-2.md",
        "finalize-findings-readback-1.md",
    ]
    kept = ["impl-comments.json", "plan.md"]
    for name in (*planted, *kept):
        (cw_dir / name).write_text("x", encoding="utf-8")
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)}

    result = subprocess.run(
        ["bash", "-c", _pre_clean_fence()],
        cwd=tmp_path / "repo",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in cw_dir.iterdir()) == sorted(kept)


def test_raw_findings_file_is_removed_on_every_path_out_of_the_redactor_fence() -> None:
    """The raw text holds what the redactor strips; the part and readback files
    may stay for inspection, the raw file may not. The trap is executed in
    tests/test_scope_conformance_gate_docs.py's redactor-fence cases."""
    section = _finalize_findings_section()
    assert "trap 'rm -f \"$GUARD_ROOT/.cw/finalize-findings.txt\"' EXIT" in section
    bullet = next(
        line
        for line in section.splitlines()
        if line.startswith("- **Fence exit 0 with `REDACT_EXIT=0`**")
    )
    assert "removed the raw `finalize-findings.txt`" in bullet
    assert "every fail-closed path" in bullet
    assert "the raw file never does" in bullet


def test_redactor_fence_passes_both_worktree_anchors() -> None:
    section = _finalize_findings_section()
    assert '--worktree "$GUARD_ROOT" --worktree "$CTX_WORKTREE"' in section
    assert '"$GUARD_ROOT/.claude/scripts/redact_post_text.py"' in section
    assert '"$HOME/.claude/scripts/redact_post_text.py"' in section


def test_version_table_row_names_the_finalize_appendix_site() -> None:
    row = next(
        line
        for line in _cmd("auto-dev-impl.md").splitlines()
        if line.startswith("| `redact_post_text.py`")
    )
    assert "| 1 |" in row
    assert "`auto-dev-finalize-appendix.md`" in row


# ---------------------------------------------------------------------------
# Body file convention and the free-text ticket
# ---------------------------------------------------------------------------


def test_section_uses_the_write_tool_pointer_and_body_file() -> None:
    section = _finalize_findings_section()
    assert POINTER in section
    assert "--body-file" in section
    for phrase in RESTATED_ARGUMENT_PHRASES:
        assert phrase not in section, phrase


def test_free_text_ticket_posts_nothing() -> None:
    paragraph = _section_paragraph("**Tracker and ticket.**")
    assert "A free-text ticket (no tracker) posts nothing" in paragraph
    assert '"**Post to Linear:**"' in paragraph


# ---------------------------------------------------------------------------
# Parts, never truncated (R4)
# ---------------------------------------------------------------------------


def test_long_findings_split_into_parts_never_truncated() -> None:
    paragraph = _section_paragraph("**Step 3 — measure and split.**")
    assert GITHUB_PART_LIMIT in paragraph
    assert "Reserve 1,000 characters for that wrapper" in paragraph
    assert "at most 59,000" in paragraph
    assert "on line boundaries" in paragraph
    assert PART_LABEL in paragraph
    assert "header, sub-heading, label line, fence and marker included" in paragraph
    assert "never truncated" in paragraph
    assert "Truncation of the finding text is forbidden on this path" in paragraph


def test_section_never_references_the_truncating_helper() -> None:
    section = _finalize_findings_section()
    assert "redact_and_cap" not in section
    assert "[truncated" not in section


# ---------------------------------------------------------------------------
# Read-back (G4), residual risk (G4), park marker and park flag (G5)
# ---------------------------------------------------------------------------


def test_read_back_compares_bytes_and_fails_closed() -> None:
    paragraph = _section_paragraph(READ_BACK)
    assert "finalize-findings-part-<i>.md" in paragraph
    assert "byte" in paragraph
    assert "`cmp`" in paragraph
    assert "fail closed" in paragraph
    assert FAILED_FRICTION in paragraph
    assert "naming the parts already posted" in paragraph
    assert "CRLF becomes LF" in paragraph


def test_read_back_names_the_non_empty_guard() -> None:
    """Two empty sides must never compare equal (round 2): the prose says so."""
    paragraph = _section_paragraph(READ_BACK)
    assert "Before the `cmp`" in paragraph
    assert "non-empty after normalization" in paragraph
    assert "`READBACK_MISSING`" in paragraph
    assert "two empty sides never compare equal" in paragraph
    fence = _read_back_fence()
    assert '[ -s "$POSTED" ] && [ -s "$READBACK" ]' in fence
    assert fence.index("READBACK_MISSING") < fence.index("cmp -s")


# The read-back fence is the control that decides whether a public post is
# stamped as verified, so it is run, not just text-asserted: `gh` is a stub on
# PATH that honours `--jq` by running the real `jq` over a planted
# `{"comments": [...]}` document (gh's `--json comments` shape), so the fence's
# own comment-selection filter executes; the fence's bash does the rest.
_READBACK_TICKET = "42"
_READBACK_LABEL = "(part 1 of 2)"
_READBACK_PLACEHOLDERS = {
    "i": "1",
    "ticket": _READBACK_TICKET,
    "label": _READBACK_LABEL,
}
requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="jq is not installed; the read-back selection filter needs the real binary",
)
_READBACK_OK = "READBACK_OK"
_READBACK_MISMATCH = "READBACK_MISMATCH"
_READBACK_MISSING = "READBACK_MISSING"
_READBACK_FETCH_FAILED = "READBACK_FETCH_FAILED"
_GH_STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$GH_STUB_ARGV"
[ -n "$GH_STUB_FAIL" ] && exit 1
filter=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--jq" ]; then filter="$2"; shift; fi
  shift
done
exec jq -r "$filter" "$GH_STUB_COMMENTS"
"""


def _read_back_fence() -> str:
    fences = [
        fence
        for fence in _bash_fences(_finalize_findings_section())
        if "READBACK=" in fence
    ]
    assert len(fences) == 1
    return substitute_fence_placeholders(fences[0], _READBACK_PLACEHOLDERS)


def _run_read_back_fence(
    tmp_path: Path,
    *,
    posted: bytes | None,
    served: bytes | list[bytes],
    gh_fails: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the read-back fence with *posted* on disk and *served* from `gh`.

    *served* is one comment body or the ticket's comment list, oldest first;
    the stub writes the bodies as ``{"comments": [...]}`` and the fence's own
    ``--jq`` filter picks among them. The stub records its argv to
    ``tmp_path / "gh-argv.txt"`` (one argument per line).
    """
    cw_dir = tmp_path / "repo" / ".cw"
    cw_dir.mkdir(parents=True)
    if posted is not None:
        (cw_dir / "finalize-findings-part-1.md").write_bytes(posted)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    bodies = [served] if isinstance(served, bytes) else served
    comments = tmp_path / "comments.json"
    comments.write_text(
        json.dumps({"comments": [{"body": body.decode("utf-8")} for body in bodies]}),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_STUB_COMMENTS": str(comments),
        "GH_STUB_ARGV": str(tmp_path / "gh-argv.txt"),
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }
    if gh_fails:
        env["GH_STUB_FAIL"] = "1"
    return subprocess.run(
        ["bash", "-c", _read_back_fence()],
        cwd=tmp_path / "repo",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


COMMENT = (
    b"## Blocking Review Findings\n\n### Finalize ship-it gate findings\n"
    b"(part 1 of 2)\nbody\n"
)
PART_TWO = COMMENT.replace(b"(part 1 of 2)", b"(part 2 of 2)")


@requires_jq
def test_read_back_fence_matching_body_is_ok(tmp_path: Path) -> None:
    result = _run_read_back_fence(tmp_path, posted=COMMENT, served=COMMENT)
    assert result.returncode == 0, result.stderr
    assert _READBACK_OK in result.stdout


@requires_jq
@pytest.mark.parametrize(
    "served",
    [
        COMMENT.replace(b"\n", b"\r\n"),
        COMMENT + b"   \n\n",
        COMMENT.rstrip(b"\n"),
    ],
    ids=["crlf", "trailing-whitespace", "no-final-newline"],
)
def test_read_back_fence_normalizes_crlf_and_trailing_whitespace(
    tmp_path: Path, served: bytes
) -> None:
    result = _run_read_back_fence(tmp_path, posted=COMMENT, served=served)
    assert result.returncode == 0, result.stderr
    assert _READBACK_OK in result.stdout


@requires_jq
@pytest.mark.parametrize(
    "served",
    [
        COMMENT.replace(b"body", b"b0dy"),
        COMMENT.replace(b"body", b"extra\nbody"),
        COMMENT + b"extra\n",
    ],
    ids=["changed-byte", "inserted-line", "appended-text"],
)
def test_read_back_fence_differing_body_is_a_mismatch(
    tmp_path: Path, served: bytes
) -> None:
    result = _run_read_back_fence(tmp_path, posted=COMMENT, served=served)
    assert result.returncode == 1
    assert _READBACK_MISMATCH in result.stdout
    assert _READBACK_OK not in result.stdout


def test_read_back_fence_failing_gh_is_fetch_failed(tmp_path: Path) -> None:
    result = _run_read_back_fence(
        tmp_path, posted=COMMENT, served=COMMENT, gh_fails=True
    )
    assert result.returncode == 1
    assert _READBACK_FETCH_FAILED in result.stdout
    assert _READBACK_OK not in result.stdout


@requires_jq
@pytest.mark.parametrize(
    ("posted", "served"),
    [
        (COMMENT, b""),
        (COMMENT, b"\n"),
        (COMMENT, b"  \r\n\n"),
        (None, b""),
        (None, b"\n"),
        (None, COMMENT),
        (b"", b""),
        (b"", b"\n"),
        (b"  \n", b"\n"),
        (b"", COMMENT),
    ],
    ids=[
        "readback-empty",
        "readback-bare-newline",
        "readback-blank-crlf",
        "part-missing-readback-empty",
        "part-missing-readback-bare-newline",
        "part-missing-readback-present",
        "both-empty",
        "part-empty-readback-newline",
        "both-blank",
        "part-empty-readback-present",
    ],
)
def test_read_back_fence_empty_side_is_never_ok(
    tmp_path: Path, posted: bytes | None, served: bytes
) -> None:
    """A comment that never landed, or a part never written, is not a match."""
    result = _run_read_back_fence(tmp_path, posted=posted, served=served)
    assert result.returncode != 0
    assert _READBACK_OK not in result.stdout
    assert _READBACK_MISSING in result.stdout


# The cases below execute the fence's own `--jq` selection filter (header and
# sub-heading regex, `contains(<label>)` part match, `last`, the `// ""`
# fallback). Kill-checks: changing `last` to `first`, or dropping the
# `select(.body | contains(...))`, or dropping the header+sub-heading `test(...)`
# select, each turns at least one test below red.
OLD_COMMENT = COMMENT.replace(b"body", b"older body")
REVIEW_COMMENT = (
    b"## Blocking Review Findings\n\n### reviewer \xe2\x80\x94 MUST_FIX\n"
    b"(part 1 of 2)\nbody\n"
)
CHATTER = b"quoting (part 1 of 2) in unrelated chatter\n"


@requires_jq
def test_read_back_selects_only_the_matching_part_comment(tmp_path: Path) -> None:
    """Part 2 is the newest comment, but the fence reads back part 1."""
    result = _run_read_back_fence(
        tmp_path, posted=COMMENT, served=[CHATTER, COMMENT, PART_TWO]
    )
    assert result.returncode == 0, result.stderr
    assert _READBACK_OK in result.stdout


@requires_jq
@pytest.mark.parametrize(
    "decoy",
    [CHATTER, REVIEW_COMMENT, b" " + COMMENT],
    ids=["label-in-chatter", "wrong-sub-heading", "leading-space-before-header"],
)
def test_read_back_ignores_a_comment_that_only_carries_the_label(
    tmp_path: Path, decoy: bytes
) -> None:
    """A newer comment with the label but not header+sub-heading never wins."""
    result = _run_read_back_fence(tmp_path, posted=COMMENT, served=[COMMENT, decoy])
    assert result.returncode == 0, result.stderr
    assert _READBACK_OK in result.stdout


@requires_jq
@pytest.mark.parametrize(
    ("served", "posted", "ok"),
    [
        ([OLD_COMMENT, COMMENT], COMMENT, True),
        ([COMMENT, OLD_COMMENT], OLD_COMMENT, True),
        ([OLD_COMMENT, COMMENT], OLD_COMMENT, False),
        ([COMMENT, OLD_COMMENT], COMMENT, False),
    ],
    ids=["newest-last", "newest-is-old-body", "stale-posted", "newer-posted-loses"],
)
def test_read_back_compares_against_the_newest_matching_comment(
    tmp_path: Path, served: list[bytes], posted: bytes, ok: bool
) -> None:
    """Two matching comments: the last (newest) one is the read-back."""
    result = _run_read_back_fence(tmp_path, posted=posted, served=served)
    if ok:
        assert result.returncode == 0, result.stderr
        assert _READBACK_OK in result.stdout
    else:
        assert result.returncode == 1
        assert _READBACK_MISMATCH in result.stdout
        assert _READBACK_OK not in result.stdout


@requires_jq
@pytest.mark.parametrize(
    "served",
    [[], [CHATTER], [REVIEW_COMMENT, PART_TWO], [b"## Blocking Review Findings\n"]],
    ids=["no-comments", "chatter-only", "other-label-and-sub-heading", "header-only"],
)
def test_read_back_with_no_matching_comment_is_missing_never_ok(
    tmp_path: Path, served: list[bytes]
) -> None:
    result = _run_read_back_fence(tmp_path, posted=COMMENT, served=served)
    assert result.returncode != 0
    assert _READBACK_MISSING in result.stdout
    assert _READBACK_OK not in result.stdout


@requires_jq
def test_read_back_fetches_the_ticket_comments_as_json(tmp_path: Path) -> None:
    """Pins the exact `gh` invocation the fence makes for ticket 42."""
    _run_read_back_fence(tmp_path, posted=COMMENT, served=COMMENT)
    argv = (tmp_path / "gh-argv.txt").read_text(encoding="utf-8").splitlines()
    assert argv[:5] == ["issue", "view", _READBACK_TICKET, "--json", "comments"]
    assert argv[5] == "--jq"
    assert len(argv) == 7
    assert f'contains("{_READBACK_LABEL}")' in argv[6]


def test_read_back_mismatch_records_urls_and_asks_for_human_deletion() -> None:
    """F4: the read-back is post-hoc detection, so the entry carries the remedy."""
    paragraph = _section_paragraph(READ_BACK)
    assert "comment URL (or id) of EACH already-posted part" in paragraph
    assert "the operator must review and delete those comments" in paragraph
    assert "Deletion needs a human" in paragraph
    assert "does not remove its edit history" in paragraph


def test_read_back_states_only_what_it_proves() -> None:
    """F3: no claim that a bypassed redactor cannot pass; points at G4 risk."""
    paragraph = _section_paragraph(READ_BACK)
    assert "cannot pass by matching itself" not in paragraph
    assert "equals the part file the session wrote" in paragraph
    assert "guards post-time divergence only" in paragraph
    assert "**Residual risk (G4)**" in paragraph


def test_park_marker_follows_read_back_and_requires_it() -> None:
    section = _finalize_findings_section()
    assert section.rindex(FINALIZE_PARK_MARKER) > section.index(READ_BACK)
    paragraph = _section_paragraph(FINALIZE_PARK_MARKER)
    assert "read back successfully" in paragraph
    assert "cw signal-park" in paragraph
    assert REVIEW_PARK_IGNORE in paragraph


def test_park_marker_mirrors_the_review_appendix_ignore_wording() -> None:
    assert REVIEW_PARK_IGNORE in _appendix("review")


def test_residual_risk_and_follow_up_are_stated() -> None:
    paragraph = _section_paragraph("**Residual risk (G4).**")
    assert "prompt-driven" in paragraph
    assert "nothing deterministic enforces that it ran on the exact bytes posted" in (
        paragraph
    )
    assert "narrows but does not eliminate" in paragraph
    assert "follow-up" in paragraph
    assert "#2810" in paragraph


def test_park_flag_note_is_stated() -> None:
    paragraph = _section_paragraph("**Park-flag note (G5).**")
    assert "parks the row `BLOCKED_ON_USER`" in paragraph
    assert "does NOT 5a-regress" in paragraph
    assert "intended semantics" in paragraph
    assert "ships dark by default" in paragraph


# ---------------------------------------------------------------------------
# Gate-collapse and meaning rows
# ---------------------------------------------------------------------------


def test_auto_dev_rows_mention_blocking_findings_comment() -> None:
    content = _cmd("auto-dev.md")
    row = _after(content, ROW_FINALIZE_FINDINGS_ANCHOR, span=500)
    assert "blocking findings" in row.lower()
    assert '`blocker.reason: "agent_block"`' in row
    meaning = _after(content, MEANING_ROW_AGENT_BLOCK_ANCHOR, span=350)
    assert "blocking findings" in meaning.lower()
    # The generic catch-all row stays.
    assert "| Any other agent BLOCK (Plan / prep-pr / etc.) |" in content


def test_headless_contract_rows_mention_blocking_findings_comment() -> None:
    content = _doc("headless-contract.md")
    row = _after(content, ROW_FINALIZE_FINDINGS_ANCHOR, span=500)
    assert "blocking findings" in row.lower()
    meaning = _after(content, MEANING_ROW_AGENT_BLOCK_ANCHOR, span=350)
    assert "blocking findings" in meaning.lower()
    assert "| Any other agent BLOCK (Plan / prep-pr / etc.) |" in content


LINEAR_QUALIFIER = "on Linear the carve-out withholds"
GITHUB_QUALIFIER = "on `github-issues`"
NOT_PASSED = "not passed to the regressed IMPL run"


def _row(content: str) -> str:
    return next(
        line for line in content.splitlines() if ROW_FINALIZE_FINDINGS_ANCHOR in line
    )


@pytest.mark.parametrize(
    "content", [_cmd("auto-dev.md"), _doc("headless-contract.md")], ids=["cmd", "doc"]
)
def test_contract_row_qualifies_delivery_by_tracker(content: str) -> None:
    """Linear always withholds (G3), so no row may say the run "receives" it flat."""
    row = _row(content)
    assert GITHUB_QUALIFIER in row
    assert LINEAR_QUALIFIER in row
    assert "`identity_unresolved`" in row
    assert NOT_PASSED in row
    assert "stay in the tracker comment" in row


def test_finalize_sentinel_paragraph_qualifies_delivery_by_tracker() -> None:
    paragraph = _section_paragraph("**Sentinel.**")
    assert "on `github-issues` the regressed run receives this comment" in paragraph
    assert "On Linear the carve-out withholds with `identity_unresolved`" in paragraph
    assert "not passed to the regressed IMPL run" in paragraph


def test_finalize_core_paragraph_qualifies_delivery_by_tracker() -> None:
    assert (
        "where, on `github-issues`, the regressed run receives the comment as "
        "evidence (on Linear the carve-out withholds it with `identity_unresolved`)"
    ) in _core_paragraph()


# ---------------------------------------------------------------------------
# Regression pins: nothing on the routing side moves
# ---------------------------------------------------------------------------


def test_header_reused_not_added_to_the_provenance_enumeration() -> None:
    assert FINDINGS_HEADER in PIPELINE_HEADERS
    assert len(PIPELINE_HEADERS) == 4
    rule = _cmd("auto-dev.md")
    rule_section = rule[
        rule.index("## Comment provenance rule (#2097)") : rule.index(
            "## Tool-Use Denial Exit"
        )
    ]
    assert FINDINGS_SUBHEADING not in rule_section


def test_finalize_regress_reasons_unchanged() -> None:
    assert sorted(FINALIZE_REGRESS_BLOCKER_REASONS) == ["agent_block"]
    assert "external_state_block" not in FINALIZE_REGRESS_BLOCKER_REASONS


# ---------------------------------------------------------------------------
# Acceptance: an over-500-character multi-finding block, composed from layers
# ---------------------------------------------------------------------------


def _acceptance_details() -> str:
    findings = [
        f"MUST_FIX {n}: src/cw/module_{n}.py:{n * 10} handler drops the error"
        for n in range(1, 13)
    ]
    details = "\n".join(findings)
    assert len(details) > _PRIOR_ATTEMPT_DETAILS_MAX_LEN
    return details


def test_acceptance_body_shape_posts_long_details_verbatim() -> None:
    details = _acceptance_details()
    assert len(details) > _PRIOR_ATTEMPT_DETAILS_MAX_LEN
    paragraph = _section_paragraph("**Body shape.**")
    assert "posted verbatim, not truncated" in paragraph


def test_acceptance_prior_attempts_cap_is_unchanged() -> None:
    assert _PRIOR_ATTEMPT_DETAILS_MAX_LEN == 500


def test_acceptance_redactor_keeps_every_character() -> None:
    redactor = load_guard_script_module(REDACTOR, "redact_post_text")
    worktree = "/home/alice/.cw/wt/abcd1234/dev-2794"
    details = f"gate: Step 8 ship-it\n{worktree}/src/cw/x.py\n" + _acceptance_details()
    padded = (
        details
        + "\n"
        + "MUST_FIX trailing finding text. " * (ACCEPTANCE_DETAILS_CHARS // 32)
    )
    assert len(padded) > ACCEPTANCE_DETAILS_CHARS

    result = redactor.redact_post_text(padded, home="/home/alice", worktrees=[])

    assert len(result) == len(padded) - len(worktree) + len("<worktree>")
    assert result.endswith(padded[-1000:])


def test_acceptance_impl_carve_out_delivers_every_part_verbatim() -> None:
    lines = _carve_out_lines()
    assert len(lines) == 2
    for line in lines:
        assert CARVE_OUT_ANCHOR in line
        clause = _carve_out_clause(line)
        assert "passed verbatim in part order" in clause
        assert "all parts" in clause
