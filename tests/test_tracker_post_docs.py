"""Doc guard: agent-authored tracker posts go through ``cw post-comment`` (#2810).

Pure-markdown assertions over the auto-dev stage docs, in the ``read_text()`` +
literal-substring/window convention of ``test_agent_comment_provenance.py``:
``_cmd``/``_appendix`` come from ``tests.conftest`` and ``_after``/``_nearby``
from ``test_auto_dev_preflight_resolutions``.

Four guarantees:

- **No raw post.** No ``gh issue comment`` with ``--body``, ``--body-file``,
  ``-F`` or ``-b`` (even hard-wrapped) and no ``create_comment`` in
  ``.claude/commands/auto-dev*.md`` outside the routed form (the
  ``cw post-comment`` invocation itself, or ``create_comment`` beside
  ``--emit-only``) or an explicit :data:`ALLOW_LIST` entry with a recorded
  reason, judged per match by :func:`_raw_post_offenders` (which has
  positive-control tests). A stale allow-list entry (its anchor gone from the
  doc) fails too.
- **Routed sites name the seam** and the flags that matter at each.
- **One seam section** in ``auto-dev.md`` names every exit code, every
  ``status`` and ``reason`` (pinned against the enums, so docs and code cannot
  drift), the eight result keys, the foreground rule, no automatic retry after
  exit 3, no raw fallback on any other exit, the redactor limits and the known
  unrouted in-package posters.
- **The script is gone** from every command doc and the version table.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.cli.post_comment import EMITTED_STATUS, exit_code_for
from cw.tracker_post import PostReason, PostStatus
from tests.conftest import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

if TYPE_CHECKING:
    from collections.abc import Sequence

COMMANDS = Path(__file__).parent.parent / ".claude" / "commands"
SEAM_ANCHOR = "## Tracker post seam (#2810)"
SEAM_COMMAND = "cw post-comment"
SEAM_REFERENCE = "*Tracker post seam (#2810)* in `.claude/commands/auto-dev.md`"
RESULT_KEYS = (
    "status",
    "reason",
    "ticket",
    "repo_visibility",
    "redacted",
    "parts_total",
    "posted",
    "out_files",
)
# A raw GitHub post: `gh issue comment` then, within 200 characters and before a
# code-span end, `;`, `|` or a sentence end, a body flag (`--body`,
# `--body-file`, `-F`, `-b`). Matched over whitespace-collapsed text so a
# hard-wrapped or backslash-continued command is still one match.
RAW_GH_POST = re.compile(
    r"gh issue comment\b(?:(?!\.\s)[^`;|\x00]){0,200}?\s"
    r"(?:--body(?:-file)?|-F|-b)(?=[\s=\"'`]|$)"
)
RAW_CREATE_COMMENT = re.compile(r"\bcreate_comment\b")
# A `create_comment` mention is routed only when, within CREATE_COMMENT_WINDOW
# characters either side, the text points at the emit-only part files and has no
# fallback wording (a sentence offering `create_comment` as the alternative to
# the seam is the unredacted Linear post this guard exists to catch).
CREATE_COMMENT_WINDOW = 200
EMIT_ONLY_EVIDENCE = re.compile(r"--emit-only|out_files|part files?", re.IGNORECASE)
FALLBACK_WORDING = re.compile(
    r"fall(?:s|ing)? back|fallback|instead|otherwise|\braw\b", re.IGNORECASE
)
# One `cw post-comment ...` invocation: the command then its argument tokens,
# stopping at a comma, a pipe, a backtick, the end of the text, or a `gh` /
# `create_comment` token (so "cw post-comment or gh issue comment ..." still
# leaves the raw part visible).
SEAM_CALL = re.compile(
    r"cw post-comment(?:\s+(?!gh\b|create_comment)"
    r"(?:\[[^\]]*\]|\"[^\"]*\"|[\w./#<>=:*-]+))*"
)
PLACEHOLDER = "\x00"
BACKTICKED_TOKEN = re.compile(r"`([a-z]+(?:_[a-z]+)*)`")

# {doc: [(anchor substring, reason)]}. A raw-post line must contain one of its
# doc's anchors; every anchor must still be in its doc.
ALLOW_LIST: dict[str, list[tuple[str, str]]] = {
    "auto-dev-intake.md": [
        (
            "| Post a comment | Linear create-comment | `gh issue comment <n> --body",
            "generic tracker-operation mapping table, not a post site",
        ),
    ],
    "auto-dev.md": [
        (
            "write op itself (an allow-listed raw post: `gh issue comment`, Linear "
            "`create_comment`)",
            "the Producers MUST mark rule names the raw ops it governs",
        ),
        (
            "`gh issue comment` or `create_comment` fallback is allowed",
            "the seam's no-raw-fallback rule names the ops it forbids",
        ),
    ],
    "auto-dev-plan.md": [
        (
            "**THEN** post the same plan as a comment",
            "plan-of-record post: ticket-derived text whose signoff markers are "
            "parsed downstream",
        ),
    ],
    "auto-dev-finalize.md": [
        (
            "**Post to Linear:** Comment on the issue with PR link",
            "PR-link comment: a PR URL and a fixed sentence",
        ),
    ],
    "auto-dev-plan-appendix.md": [
        (
            "This comment is an allow-listed raw post",
            "consolidated park comment: redaction can corrupt the approval-token "
            "line and the Recommendation: grammar",
        ),
    ],
    "auto-dev-review.md": [
        (
            "This post is allow-listed: do NOT route it through `cw post-comment`",
            "voided-findings comment: its machine-parsed JSON block does not "
            "survive redaction (tests/test_tracker_post.py pins this)",
        ),
    ],
}


def _flat(text: str) -> str:
    """Collapse whitespace so a hard-wrapped phrase matches as one line."""
    return " ".join(text.split())


def _flat_block(block: str) -> str:
    """One paragraph on one line: continuations joined, whitespace collapsed."""
    return _flat(re.sub(r"\\[ \t]*\n", " ", block))


def _raw_post_offenders(text: str, anchors: Sequence[str] = ()) -> list[str]:
    """Raw tracker posts in *text* that are neither routed nor allow-listed.

    Works per paragraph (blank-line separated) over whitespace-collapsed text,
    so a command wrapped across lines is one match. An *anchors* substring
    (an :data:`ALLOW_LIST` entry) exempts only its own span. A routed
    exemption is per match: the ``cw post-comment ...`` invocation is removed
    first, and a raw ``gh issue comment ... --body[-file] | -F | -b`` that
    remains is an offender; a ``create_comment`` mention is exempt only when
    :func:`_create_comment_is_routed` finds emit-only evidence and no fallback
    wording within a small window around it.
    """
    offenders: list[str] = []
    line = 1
    for block in re.split(r"(\n[ \t]*\n)", text):
        start_line = line
        line += block.count("\n")
        if not block.strip():
            continue
        flat = _flat_block(block)
        for anchor in anchors:
            flat = flat.replace(_flat_block(anchor), PLACEHOLDER)
        remaining = SEAM_CALL.sub(PLACEHOLDER, flat)
        found = [m.start() for m in RAW_GH_POST.finditer(remaining)]
        offenders.extend(
            f"line {start_line}: {remaining[pos : pos + 120]}" for pos in sorted(found)
        )
        # Judged on `flat`, not `remaining`: the seam call (and its
        # `--emit-only`) is the local evidence and must stay visible.
        offenders.extend(
            f"line {start_line}: {flat[m.start() : m.start() + 120]}"
            for m in RAW_CREATE_COMMENT.finditer(flat)
            if not _create_comment_is_routed(flat, m.start(), m.end())
        )
    return offenders


def _create_comment_is_routed(flat: str, start: int, end: int) -> bool:
    """True when a ``create_comment`` mention is local to emit-only wording."""
    window = flat[max(0, start - CREATE_COMMENT_WINDOW) : end + CREATE_COMMENT_WINDOW]
    return bool(EMIT_ONLY_EVIDENCE.search(window)) and not FALLBACK_WORDING.search(
        window
    )


def _command_docs() -> list[Path]:
    docs = sorted(COMMANDS.glob("auto-dev*.md"))
    assert docs, "no auto-dev command docs found"
    return docs


def _seam_section() -> str:
    content = _cmd("auto-dev.md")
    start = content.index(SEAM_ANCHOR)
    end = content.index("\n## ", start + len(SEAM_ANCHOR))
    return content[start:end]


def _seam_block(start_anchor: str, end_anchor: str) -> str:
    section = _seam_section()
    start = section.index(start_anchor)
    return section[start : section.index(end_anchor, start)]


# ---------------------------------------------------------------------------
# No raw post outside the routed form or the allow-list
# ---------------------------------------------------------------------------


def test_no_raw_tracker_post_outside_the_allow_list() -> None:
    offenders: list[str] = []
    for path in _command_docs():
        anchors = [anchor for anchor, _reason in ALLOW_LIST.get(path.name, [])]
        found = _raw_post_offenders(path.read_text(encoding="utf-8"), anchors)
        offenders.extend(f"{path.name}:{item}" for item in found)
    assert offenders == [], "raw tracker post outside the seam:\n" + "\n".join(
        offenders
    )


@pytest.mark.parametrize(
    "fragment",
    [
        "gh issue comment 5 --body-file .cw/x.md",
        "gh issue comment 5 --body 'text'",
        "gh issue comment 5 -F .cw/x.md",
        "gh issue comment 5 -b text",
        "gh issue comment 5 \\\n  --body-file .cw/x.md",
        "run `gh issue comment 5\nthen --body-file .cw/x.md` here",
        "Run cw post-comment, or fall back to gh issue comment 5 --body-file x.",
        "cw post-comment 5 --body-file x || gh issue comment 5 --body-file x",
        "cw post-comment 5 --body-file x or gh issue comment 5 -F x",
        "Post with create_comment directly.",
        "cw post-comment 5 --body-file x\n\nthen gh issue comment 5 -b y",
        "Run `cw post-comment 5 --emit-only --out-dir d`, or fall back to "
        "`create_comment` with the raw text.",
        "Pass `--emit-only`, otherwise call `create_comment` directly.",
    ],
)
def test_raw_post_guard_reports_a_synthetic_offender(fragment: str) -> None:
    """Positive controls: the scan can fail, wrapped, short-flag and mixed."""
    assert _raw_post_offenders(fragment), fragment


@pytest.mark.parametrize(
    "fragment",
    [
        "cw post-comment 5 --body-file x --parts split --consume",
        "`cw post-comment <ticket> --body-file x`, per the seam",
        '```bash\ncw post-comment <ticket> --body-file <path> [--header "<l>"] \\\n'
        "  [--fence | --no-fence] [--emit-only --out-dir <dir>]\n```",
        "Use `--emit-only` then pass each path to `create_comment`.",
        "Pass each `out_files` path's content verbatim to `create_comment`.",
        "Never run `gh issue comment` directly.",
        "gh issue comment is the underlying tool.",
    ],
)
def test_raw_post_guard_accepts_a_routed_or_prose_fragment(fragment: str) -> None:
    assert _raw_post_offenders(fragment) == [], fragment


def test_raw_post_guard_exemption_is_local_to_the_create_comment_mention() -> None:
    """`--emit-only` far from a `create_comment` mention does not exempt it."""
    far = "Run `cw post-comment 5 --emit-only --out-dir d`." + " Filler." * 60
    assert _raw_post_offenders(f"{far} Post with create_comment directly.")
    near = "Pass each `out_files` path's content verbatim to `create_comment`."
    assert _raw_post_offenders(near) == []


def test_raw_post_guard_honours_an_allow_list_anchor_for_its_span_only() -> None:
    anchor = "| Post a comment | `gh issue comment <n> --body"
    allowed = f"{anchor}-file <f>` |"
    assert _raw_post_offenders(allowed, [anchor]) == []
    assert _raw_post_offenders(allowed) != []
    second = f"{allowed}\ngh issue comment 9 -F x"
    assert len(_raw_post_offenders(second, [anchor])) == 1


def test_raw_post_guard_reports_the_paragraph_line() -> None:
    offenders = _raw_post_offenders("ok\n\nfine\n\ngh issue comment 5 -b x")
    assert offenders == ["line 5: gh issue comment 5 -b x"]


def test_every_allow_list_entry_is_still_in_its_doc_with_a_reason() -> None:
    for name, entries in ALLOW_LIST.items():
        content = (COMMANDS / name).read_text(encoding="utf-8")
        for anchor, reason in entries:
            assert anchor in content, f"stale allow-list entry in {name}: {anchor}"
            assert reason.strip(), f"allow-list entry without a reason: {anchor}"


def test_seam_section_lists_every_allow_listed_site() -> None:
    allow = _flat(_seam_block("**Allow-listed raw posts.**", "**Known unrouted"))
    for site in (
        "plan-of-record post",
        "PR-link comment",
        "consolidated park comment",
        "voided-findings comment",
        "`auto-dev-intake.md`",
    ):
        assert site in allow, site
    assert "tests/test_tracker_post_docs.py" in allow


# ---------------------------------------------------------------------------
# Routed sites name the seam and their flags
# ---------------------------------------------------------------------------


def test_finalize_findings_post_uses_its_flags() -> None:
    section = _appendix("finalize")
    window = _after(section, "**Post and read back.**", span=2000)
    assert SEAM_REFERENCE in window
    for flag in ("--fence", "--parts split", "--consume", "--emit-only"):
        assert flag in window, flag


def test_review_blocking_findings_post_uses_its_flags() -> None:
    window = _after(
        _appendix("review"), "**Blocking-findings comment rule (#1815).**", 1600
    )
    assert SEAM_REFERENCE in window
    for flag in ("--no-fence", "--parts split", "--consume", "--emit-only"):
        assert flag in window, flag


def test_operator_actionable_post_uses_its_flags() -> None:
    window = _after(
        _appendix("review"), "**Posting through the seam (#2810).**", span=900
    )
    assert SEAM_COMMAND in window
    assert SEAM_REFERENCE in window
    for flag in ("--no-fence", "--parts split", "--consume", "--emit-only"):
        assert flag in window, flag


def test_plan_blocking_findings_post_uses_its_flags() -> None:
    window = _after(
        _cmd("auto-dev-plan.md"), "**Blocking-findings comment rule (#1815).**", 1600
    )
    assert SEAM_REFERENCE in window
    assert '--header "## Blocking Review Findings"' in window
    for flag in ("--no-fence", "--parts split", "--consume", "--emit-only"):
        assert flag in window, flag


def test_scope_tier_stale_post_uses_the_seam_with_its_flags() -> None:
    window = _after(
        _cmd("auto-dev-plan.md"),
        '`blocker.reason: "scope_tier_stale"`',
        span=2200,
    )
    assert SEAM_COMMAND in window
    assert SEAM_REFERENCE in window
    assert '--header "## Blocking Review Findings"' in window
    assert "### Step 1g.0 Tier Re-verification — MUST_FIX" in window
    for flag in ("--no-fence", "--parts split", "--consume", "--emit-only"):
        assert flag in window, flag


def test_both_history_comment_posts_use_parts_single_without_a_header() -> None:
    content = _cmd("auto-dev-review.md")
    sites = [
        _after(
            content,
            "- `review_not_converging` (`decision: stop_not_converging`)",
            span=900,
        ),
        _after(content, "- **Clean-exit audit trail.**", span=1100),
    ]
    for window in sites:
        assert (
            "cw post-comment <ticket> --body-file .cw/convergence-history.md "
            "--parts single --consume"
        ) in window
        assert "no `--header`" in window
        assert "--emit-only" in window
        assert "`out_files`" in window


def test_review_core_summaries_name_the_seam() -> None:
    content = _cmd("auto-dev-review.md")
    for anchor in (
        "**Blocking-findings comment rule (#1815, third trigger #1817).**",
        "**Operator-actionable findings comment rule (#1817).**",
    ):
        assert SEAM_REFERENCE in _after(content, anchor, span=1600), anchor


def test_intake_routes_findings_text_through_the_seam() -> None:
    window = _nearby(_flat(_cmd("auto-dev-intake.md")), SEAM_REFERENCE, span=300)
    assert SEAM_COMMAND in window
    assert "Tool output or findings text" in window


# ---------------------------------------------------------------------------
# The one seam section
# ---------------------------------------------------------------------------


def test_seam_section_sits_beside_the_comment_provenance_rule() -> None:
    content = _cmd("auto-dev.md")
    seam = content.index(SEAM_ANCHOR)
    rule = content.index("## Comment provenance rule (#2097)")
    assert seam < rule
    assert "\n## " not in content[seam + len(SEAM_ANCHOR) : rule].replace(
        "\n## Comment provenance rule", ""
    )


def test_seam_section_names_the_invocation_and_the_eight_result_keys() -> None:
    section = _seam_section()
    assert "cw post-comment <ticket> --body-file <path>" in section
    result = _seam_block("**Result.**", "**Exit codes and reasons (closed set).**")
    for key in RESULT_KEYS:
        assert f"`{key}`" in result, key
    assert "eight keys that are always present" in _flat(result)


def test_seam_section_lists_exactly_the_enum_values() -> None:
    """The exit table names every status and reason and nothing else."""
    block = _seam_block(
        "**Exit codes and reasons (closed set).**", "**Exit-code to friction"
    )
    tokens = set(BACKTICKED_TOKEN.findall(block))
    reasons = {reason.value for reason in PostReason}
    assert tokens == reasons | {PostStatus.POSTED.value, EMITTED_STATUS}
    for status in (PostStatus.REFUSED, PostStatus.PARTIAL):
        assert f"`status: {status.value}`" in block


def test_seam_exit_codes_match_the_cli() -> None:
    block = _flat(
        _seam_block(
            "**Exit codes and reasons (closed set).**", "**Exit-code to friction"
        )
    )
    assert f"**Exit {exit_code_for(PostStatus.POSTED.value)}**, `reason: null`" in block
    assert exit_code_for(EMITTED_STATUS) == exit_code_for(PostStatus.POSTED.value)
    for status in (PostStatus.REFUSED, PostStatus.PARTIAL):
        code = exit_code_for(status.value)
        assert f"**Exit {code}**, `status: {status.value}`" in block, status
    assert "**Exit 2**: a usage error" in block


def test_seam_section_maps_exit_codes_to_friction() -> None:
    mapping = _flat(_seam_block("**Exit-code to friction mapping.**", "**No automatic"))
    assert "`<prefix> post skipped: post_comment_refused`" in mapping
    assert "`<prefix> post failed: <site>`" in mapping
    assert "Do not run `cw signal-park`" in mapping
    assert "do not run `cw signal-park`" in mapping
    assert "the operator must review and delete those comments" in mapping
    assert "an edit does not remove a comment's edit history" in mapping


def test_seam_exit_zero_emitted_is_not_posted_until_create_comment_ran() -> None:
    """On Linear `emitted` only wrote files: no "once posted" step may run yet."""
    mapping = _flat(_seam_block("**Exit-code to friction mapping.**", "**No automatic"))
    for phrase in (
        "For `emitted` nothing is posted yet",
        "runs only AFTER every `out_files` part has been passed to "
        "`create_comment` successfully",
        "`cw signal-park` Park marker, the `posted` friction line",
        "treat it like a refused post at that site (no park stamp)",
    ):
        assert phrase in mapping, phrase


def test_seam_section_forbids_automatic_retry_after_exit_3() -> None:
    """Advisory soundness note 2: parts are public and there is no idempotency key."""
    section = _flat(_seam_section())
    assert (
        "A `partial` (exit 3) or outcome-unknown result is never retried "
        "automatically: its parts may already be public and there is no "
        "idempotency key" in section
    )


def test_seam_section_forbids_a_raw_fallback_on_any_other_exit() -> None:
    """Advisory soundness note 2: 127 or exit 1 with no JSON is never a raw-post cue."""
    section = _flat(_seam_section())
    assert (
        "Any exit other than 0, 1 or 3 — including 127 (command not found, an "
        "older `cw`) and exit 1 with no parseable JSON on stdout" in section
    )
    assert (
        "means nothing is known to be posted, and no raw `gh issue comment` or "
        "`create_comment` fallback is allowed" in section
    )


def test_seam_section_covers_a_kill_mid_post_and_the_bash_timeout() -> None:
    """A killed process can leave earlier parts public with no JSON."""
    section = _flat(_seam_section())
    assert "A kill after part k was posted leaves earlier parts public" in section
    assert "asking the operator to check the ticket for stray comments" in section
    assert "`timeout` set explicitly high (600000 ms)" in section


def test_seam_section_exit_3_with_an_empty_posted_list_has_a_recovery_path() -> None:
    """With no URL to delete by, the operator is told how to find the comment."""
    mapping = _flat(_seam_block("**Exit-code to friction mapping.**", "**No automatic"))
    assert "When `posted` is empty on exit 3" in mapping
    assert "record the ticket, the `reason` and the time" in mapping
    assert "scan the ticket's newest comments for the header" in mapping


def test_seam_section_requires_the_foreground() -> None:
    section = _flat(_seam_section())
    assert "Run it in the foreground, never `run_in_background`" in section
    assert "*Worker Execution Discipline*" in section


def test_seam_section_states_the_redactor_limits() -> None:
    limits = _flat(_seam_block("**Redactor limits.**", "**Allow-listed raw posts.**"))
    assert "path shapes" in limits
    assert "secret shapes" in limits
    assert "does NOT detect quoted code, fixtures or customer data" in limits
    assert "visibility refusal" in limits
    assert "read-back" in limits
    assert "an edit does not remove GitHub's edit history" in limits


def test_seam_section_states_the_linear_residual() -> None:
    linear = _flat(_seam_block("**Linear.**", "**Redactor limits.**"))
    assert "--emit-only --out-dir <dir>" in linear
    assert "`tracker_not_github`" in linear
    assert "**Residual (unenforced):**" in linear
    assert "no read-back" in linear


def test_seam_section_names_the_known_unrouted_in_package_posters() -> None:
    """Advisory soundness note 3: classified, not silently left open."""
    block = _flat(_seam_block("**Known unrouted in-package posters.**", "\n---"))
    assert "codex_review_persist._post_review_comment" in block
    assert "cli/dev_queue/resolve.py" in block
    assert "known unrouted path" in block


def test_seam_marker_is_appended_by_the_command() -> None:
    section = _flat(_seam_section())
    assert (
        "`cw post-comment` appends the `<!-- cw-agent-authored -->` marker itself"
        in section
    )


# ---------------------------------------------------------------------------
# The script copy and its fence machinery are gone
# ---------------------------------------------------------------------------


def test_no_command_doc_references_the_deleted_script() -> None:
    for path in sorted(COMMANDS.glob("*.md")):
        assert "redact_post_text.py" not in path.read_text(encoding="utf-8"), path.name


def test_version_table_has_no_redactor_row() -> None:
    rows = [
        line
        for line in _cmd("auto-dev-impl.md").splitlines()
        if line.startswith("| `redact_post_text")
    ]
    assert rows == []


def test_finalize_residual_risk_no_longer_defers_to_this_ticket() -> None:
    section = _appendix("finalize")
    assert "**Residual risk (G4).**" not in section
    assert "Redaction here is prompt-driven" not in section
    assert "tracked as the follow-up #2810" not in section
