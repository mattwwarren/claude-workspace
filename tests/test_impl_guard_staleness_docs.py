"""Doc-structure guards for the impl Pre-Stage Detector Guard staleness/regress
override (#1794) — mirrors tests/test_scope_conformance_gate_docs.py's pairing
of a script-behavior test file with a prose-wiring test file.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cw.auto_dev_result import IMPL_COMMENTS_UNREADABLE_AFTER_REGRESS_BLOCKER_REASON
from cw.gh import AGENT_COMMENT_MARKER
from tests.conftest import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after


def _guard_section() -> str:
    """The detector guard's resume dispositions + staleness/regress check.

    #1879 relocated this block to ``auto-dev-impl-appendix.md``: it applies
    only when ``detect_current_stage()`` reports that the ticket already
    carries branch work, which a fresh dispatch never does. The core doc keeps
    the detector call and the trigger sentence; every assertion below follows
    the content to its new home rather than being dropped.
    """
    content = _appendix("impl")
    start = content.index("## Pre-Stage Detector Guard: resume dispositions")
    end = content.index("\n## ", start)
    return content[start:end]


def test_core_doc_keeps_detector_call_and_appendix_trigger() -> None:
    """Detection stays on the common path; only the resume branch moved."""
    content = _cmd("auto-dev-impl.md")
    assert "run `detect_current_stage()`" in content
    assert "Pre-Stage Detector Guard: resume dispositions" in content
    assert "never re-implement over existing branch work by default" in content


def test_guard_invokes_staleness_script() -> None:
    section = _guard_section()
    assert ".claude/scripts/check_impl_guard_staleness.py" in section
    assert "--head-commit-at" in section
    assert "--comments-file" in section
    assert "--regressed-into-stage" in section


def test_guard_reads_comments_file_from_guard_root() -> None:
    """#2315: the guard's `--comments-file` argument reads the same
    worktree-scoped path the Orientation step wrote, not the old
    `$CW_SESSION`-keyed `/tmp` path — and reuses the fence's already-computed
    `$GUARD_ROOT` rather than deriving a new anchor."""
    section = _guard_section()
    assert '--comments-file "$GUARD_ROOT/.cw/impl-comments.json"' in section


def test_guard_reads_regressed_into_stage_from_queue_metadata() -> None:
    section = _guard_section()
    assert "queue_metadata.regressed_into_stage" in section


def test_guard_past_s2_common_case_still_short_circuits() -> None:
    """Inverse of the bug: no new evidence -> unchanged fast path (AC3)."""
    section = _guard_section()
    assert "advance to that stage's entry point; do not re-implement" in section


def test_guard_stale_branch_resumes_instead_of_advancing() -> None:
    section = _guard_section()
    assert "the trailer's premise" in section
    assert "do NOT advance to the next stage's entry point" in section
    assert "Resume from current branch HEAD; do not reset" in section


def test_guard_stale_branch_requires_fresh_trailer() -> None:
    section = _guard_section()
    assert "must append a fresh `Auto-Dev-Stage: impl-complete` trailer" in section


def test_guard_stale_comments_delivered_as_binding_instructions() -> None:
    """AC4 (R4): pins the exact clause that hands live-fetched comments to the
    Stage 2 agent as binding -- not merely "a fetch was attempted"."""
    section = _guard_section()
    assert "as new, binding instructions to read and act on" in section


def test_guard_known_limitation_cites_1801() -> None:
    """#1801 evaluated and accepted the no-sentinel-death gap this sentence
    describes -- the prose cross-references the ticket that made that call."""
    section = _guard_section()
    assert "Known limitation" in section
    assert "#1801" in section


def test_guard_fails_open_on_script_exit_2() -> None:
    """A malformed input must not block the pipeline — exit 2 degrades to the
    unchanged short-circuit behaviour, with a friction breadcrumb."""
    section = _guard_section()
    assert "impl_guard_staleness_check_failed" in section
    assert "fail open" in section


def test_orientation_live_fetches_comments_not_cache() -> None:
    content = _cmd("auto-dev-impl.md")
    window = _after(content, "**Comments are live, not cached", span=900)
    assert "MUST live-fetch the ticket comments on every invocation" in window
    assert "Stage 0 does NOT re-run between pipeline stages" in window


def test_orientation_writes_impl_comments_cache_under_guard_root() -> None:
    """#2315: the impl-comments cache is worktree-scoped, not `$CW_SESSION`-keyed.

    A fixed `/tmp/impl-comments-$CW_SESSION.json` name collides across two
    workers whose parent shell shares an inherited/machine-wide `$CW_SESSION`.
    Both Orientation-step write sites (the fresh-fetch write and the
    fetch-failure fallback write) must target `$GUARD_ROOT/.cw/impl-comments.json`
    instead, and the paragraph must forbid improvising a fixed path.
    """
    content = _cmd("auto-dev-impl.md")
    window = _after(content, "**Comments are live, not cached", span=3200)
    assert window.count("$GUARD_ROOT/.cw/impl-comments.json") == 2, (
        "expected both the fresh-fetch write and the fetch-failure-fallback "
        "write to target the new worktree-scoped path"
    )
    assert "Never write this cache to a fixed" in window
    assert "not guaranteed unique across" in window


def test_orientation_cites_per_stage_dispatch_mechanism() -> None:
    content = _cmd("auto-dev-impl.md")
    assert "src/cw/executor.py" in content


def test_guard_resolves_repo_local_then_global_script_path() -> None:
    """Repo-local first, then the copy install-skills.sh ships (#2141).

    The repo-local candidate is asserted in its ``$GUARD_ROOT``-anchored
    spelling, not as a bare substring: a cwd-relative probe would satisfy the
    looser assertion while silently missing the repo-local copy whenever the
    cwd is not the worktree root (review round 2).
    """
    section = _guard_section()
    assert '"$GUARD_ROOT/.claude/scripts/check_impl_guard_staleness.py"' in section
    assert '"$HOME/.claude/scripts/check_impl_guard_staleness.py"' in section
    assert "for candidate in .claude/scripts/" not in section


def test_guard_absent_from_both_locations_skips_non_blocking() -> None:
    """Absent from both locations keeps the fail-open short-circuit (#2141).

    Distinct from the pre-existing exit-2 (``impl_guard_staleness_check_failed``)
    branch, which is unchanged: a missing file coincidentally exits 2 too, but
    it is not an unparseable timestamp and must not be labelled as one.
    """
    section = _guard_section()
    assert "check_impl_guard_staleness: script absent, skipped" in section
    assert "impl_guard_staleness_check_failed" in section


def test_guard_greps_cw_script_version_marker_and_headless_blocks_on_stale() -> None:
    """File-staleness is a hard stop, and is NOT the script's own `stale` field.

    The resolver's marker check (is this copy of the script current?) precedes
    and is independent of the script's ``stale: true/false`` JSON verdict (are
    the impl comments newer than HEAD?). The two must not collapse into one
    concept: no HEADLESS BLOCK line may also carry the fail-open verdict.
    """
    section = _guard_section()
    assert "cw-script-version" in section
    assert "HEADLESS BLOCK" in section
    stale_lines = [line for line in section.splitlines() if "HEADLESS BLOCK" in line]
    assert stale_lines
    assert all("stale: false" not in line for line in stale_lines)


def test_guard_rematerializes_context_json() -> None:
    content = _cmd("auto-dev-impl.md")
    window = _after(content, "**Comments are live, not cached", span=1400)
    assert "overwrite `.cw/context.json`" in window
    assert "materialized_by_session" in window


def test_orientation_regressed_comments_fetch_failure_hard_blocks() -> None:
    """#2415: a comments-fetch failure on an IMPL entry reached via
    `_stage_regress` hard-blocks instead of the generic WARN-and-continue --
    a regress exists specifically to act on newer comments, so continuing on
    a stale cached array would defeat it. The non-regress WARN branch must
    survive unchanged alongside the new hard-block branch."""
    content = _cmd("auto-dev-impl.md")
    window = _after(content, "**Comments are live, not cached", span=3200)
    assert (
        f'blocker.reason: "{IMPL_COMMENTS_UNREADABLE_AFTER_REGRESS_BLOCKER_REASON}"'
        in window
    )
    assert "queue_metadata.regressed_into_stage" in window
    assert "impl_comments_fetch_failed" in window
    assert "a stale-but-real array is better evidence than none" in window


def test_no_cw_session_keyed_tmp_path_remains_in_impl_docs() -> None:
    """#2315 AC7: no fixed `$CW_SESSION`-keyed `/tmp` scratch path may remain
    anywhere in either the core doc or its appendix, for any of the three
    re-keyed scratch files (impl-comments cache, touched-files, approved-extra)
    — a machine-wide/inherited `$CW_SESSION` collides two workers' caches
    regardless of which of the three files it names."""
    docs = {
        "auto-dev-impl.md": _cmd("auto-dev-impl.md"),
        "auto-dev-impl-appendix.md": _appendix("impl"),
    }
    for doc, content in docs.items():
        for forbidden in (
            "impl-comments-$CW_SESSION",
            "touched_files-$CW_SESSION",
            "approved-extra-$CW_SESSION",
        ):
            assert forbidden not in content, f"{doc}: still contains {forbidden!r}"


# ---------------------------------------------------------------------------
# #2794: the regress findings carve-out. A FINALIZE `agent_block` whose details
# are review findings posts them as an agent-authored `## Blocking Review
# Findings` comment; the operator-authority filter would drop it, so both
# stale=true bullets pass exactly that one post, as labelled evidence.
# ---------------------------------------------------------------------------

CARVE_OUT_ANCHOR = "**Regress findings carve-out (#2794):**"
CARVE_OUT_END = "since the existing parts are still passed"
EXISTING_AGENT_SENTENCE = (
    "An agent-authored comment postdating `HEAD_COMMIT_AT` alone does not "
    "license a fresh attempt."
)
FINDINGS_HEADER = "## Blocking Review Findings"
FINDINGS_SUBHEADING = "### Finalize ship-it gate findings"
_PART_LABEL = re.compile(r"^\(part (\d+) of (\d+)\)$", re.MULTILINE)

# The redacted observed `gh issue view 2633 --json comments` payload: the shape a
# live-fetched `.cw/impl-comments.json` entry really has (`author` is an object
# holding `login`, the timestamp is `createdAt`). Test entries are built from it
# so a field name cannot drift from the tracker's.
_OBSERVED_COMMENTS = json.loads(
    (Path(__file__).parent / "fixtures" / "gh_issue_comments_2633.json").read_text()
)["comments"]

ME = "operator-login"
FORGER = "someone-else"
HEAD_AT = "2026-10-28T09:00:00Z"


def _carve_out_lines() -> list[str]:
    """The two stale=true bullet lines that carry the carve-out clause.

    Asserts the count, so no per-line test below can pass vacuously.
    """
    lines = [line for line in _guard_section().splitlines() if CARVE_OUT_ANCHOR in line]
    assert len(lines) == 2, f"expected the carve-out in 2 bullets, found {len(lines)}"
    return lines


def _carve_out_clause(line: str) -> str:
    start = line.index(CARVE_OUT_ANCHOR)
    return line[start : line.index(CARVE_OUT_END, start)]


def test_carve_out_sits_in_both_stale_true_bullets() -> None:
    lines = _carve_out_lines()
    assert _guard_section().count(CARVE_OUT_ANCHOR) == 2
    assert lines[0].lstrip().startswith("- **If the verdict's `stale` is `true`:**")
    assert lines[1].startswith(
        "- If `stage` is past S2 **AND the verdict's `stale` is `true`**"
    )
    for line in lines:
        # The operator-authority rule stays; the carve-out follows it.
        assert EXISTING_AGENT_SENTENCE in line
        assert line.index(EXISTING_AGENT_SENTENCE) < line.index(CARVE_OUT_ANCHOR)


def test_carve_out_clause_is_identical_in_both_stale_true_bullets() -> None:
    """The two copies cannot drift: they are one clause stated twice."""
    first, second = (_carve_out_clause(line) for line in _carve_out_lines())
    assert first == second


def test_carve_out_requires_all_four_conditions() -> None:
    for line in _carve_out_lines():
        clause = _carve_out_clause(line)
        for phrase in (
            "`REGRESSED_INTO_STAGE` is non-empty",
            "single newest finalize-findings post",
            f"begins with the exact header line `{FINDINGS_HEADER}`",
            "the first non-blank line after that header is exactly "
            f"`{FINDINGS_SUBHEADING}`",
            "a sub-heading line quoted mid-body does not count",
            f"carries the marker `{AGENT_COMMENT_MARKER}`",
            "its author login",
            "all four",
            "tracker identity finalize posts as",
            "ME=$(gh api user --jq .login)",
            "`viewer`",
            "fail closed",
        ):
            assert phrase in clause, phrase


def test_carve_out_names_the_compared_identity_field_on_each_side() -> None:
    """F6: GitHub compares login to login; Linear has no comparable field."""
    for line in _carve_out_lines():
        clause = _carve_out_clause(line)
        for phrase in (
            "names one field on each side",
            "the entry's author login against `ME=$(gh api user --jq .login)`",
            "no entry field is comparable to any field of the MCP `viewer` identity",
            "Linear delivery therefore fails closed with "
            "`withheld (identity_unresolved)`",
            "the accepted behaviour",
        ):
            assert phrase in clause, phrase
        assert "all five" not in clause


def test_carve_out_names_the_real_entry_field_names() -> None:
    """The entries are the tracker's own payload: `author.login`, `createdAt`.

    A bare "`author`" read as a string compares an object to `ME` and withholds
    on GitHub every time, so the clause must name the real keys, accept the core
    doc's mapped form as the alternative, and forbid the object comparison.
    """
    for line in _carve_out_lines():
        clause = _carve_out_clause(line)
        for phrase in (
            "`author.login`",
            "`createdAt`",
            "`author` is an object",
            "else from `author` when it is a string",
            "else `created_at`",
            "never compare the `author` object itself to `ME`",
        ):
            assert phrase in clause, phrase
        assert (
            "(the field each `.cw/impl-comments.json` entry already carries)"
            not in (clause)
        )
        assert "the entry's `author` (a login)" not in clause
        assert "writes `author` as `<login>`" not in clause


def test_observed_payload_shape_matches_the_names_the_clause_uses() -> None:
    """Ground truth for the field names: the observed tracker payload itself.

    If the tracker's shape ever differs from what the clause names, this fails
    on the fixture, not on a model of it.
    """
    clause = _carve_out_clause(_carve_out_lines()[0])
    for entry in _OBSERVED_COMMENTS:
        assert isinstance(entry["author"], dict)
        assert isinstance(entry["author"]["login"], str)
        assert isinstance(entry["createdAt"], str)
        assert "created_at" not in entry
        assert "`author.login`" in clause
        assert "`createdAt`" in clause


def test_selection_reads_the_observed_payload_shape() -> None:
    """A findings post in the real entry shape is selected for its login."""
    genuine = _findings_comment(ME, "2026-10-28T10:00:00Z")
    assert isinstance(genuine["author"], dict)
    assert "created_at" not in genuine
    # Real-shaped neighbours: the observed comments, which are not findings.
    neighbours = list(_OBSERVED_COMMENTS)

    assert _select_regress_findings([*neighbours, genuine], ME) == [genuine]
    assert _select_regress_findings([*neighbours, genuine], FORGER) == []
    assert _withheld_reason([*neighbours, genuine], FORGER) == "author_mismatch"
    # A string `author` compared to the identity is what the object comparison
    # got wrong: the raw entry's `author` is never equal to `ME`.
    assert genuine["author"] != ME


def test_selection_accepts_the_mapped_form_too() -> None:
    """The core doc's `{author: <login>, created_at}` mapping names the same post."""
    genuine = _findings_comment(ME, "2026-10-28T10:00:00Z")
    mapped = _mapped(genuine)
    assert mapped["author"] == ME
    assert "createdAt" not in mapped

    assert _select_regress_findings([mapped], ME) == [mapped]
    older_mapped = _mapped(_findings_comment(ME, "2026-10-28T09:00:00Z"))
    assert _select_regress_findings([older_mapped, mapped], ME) == [mapped]
    assert _select_regress_findings([mapped], FORGER) == []


def test_carve_out_drops_forgeries_and_unrelated_agent_comments() -> None:
    for line in _carve_out_lines():
        clause = _carve_out_clause(line)
        for phrase in (
            "from any other author is dropped and does not displace the genuine post",
            "even when it is newer",
            "no other agent-authored comment",
            "## Pending Verification Scan",
            "bare marker",
            "stays dropped",
        ):
            assert phrase in clause, phrase


def test_carve_out_passes_every_part_and_names_missing_ones() -> None:
    for line in _carve_out_lines():
        clause = _carve_out_clause(line)
        for phrase in (
            "`(part i of n)`",
            "the newest matching comment for each part index 1..n",
            "labelled with the SAME `n` as the newest matching comment's label",
            "ignored for part selection",
            "counts as missing, never delivered from another label",
            "passed verbatim in part order",
            "all parts",
            "which indices are missing",
            "the evidence is incomplete",
        ):
            assert phrase in clause, phrase


def test_carve_out_has_no_head_recency_gate() -> None:
    """A5: the post's age against the head is not a qualifying condition."""
    for line in _carve_out_lines():
        clause = _carve_out_clause(line)
        assert "created_at` is later than `HEAD_COMMIT_AT`" not in clause
        assert "later than" not in clause
        assert "stale_post" not in clause
        assert "predates the fix commit" not in clause
        assert "between instants" not in clause
        assert "deliberately not a condition" in clause
        assert (
            '"newest" is the latest timestamp (`createdAt`, else `created_at`)'
            in clause
        )
        # The unchanged agent-authored sentence still precedes the carve-out.
        assert EXISTING_AGENT_SENTENCE in line


def test_carve_out_is_labelled_evidence_not_instruction() -> None:
    for line in _carve_out_lines():
        clause = _carve_out_clause(line)
        assert (
            '"Regress findings (agent-authored evidence from the failed finalize '
            'gate)"' in clause
        )
        assert "evidence" in clause
        assert "not a binding operator instruction" in clause
        assert 'marking each passed comment "posted <timestamp>"' in clause
        assert "with its own timestamp" in clause
        assert (
            "tell the Stage 2 agent to verify against the current code whether "
            "each finding still applies" in clause
        )
        assert (
            "a finding already addressed by a later commit is not a reason to "
            "change code" in clause
        )
        assert "does not license a fresh attempt on its own" in clause
        assert "destructive-directive gate" in clause
        assert "binding instructions" not in clause


def test_carve_out_records_its_outcome_in_friction() -> None:
    for line in _carve_out_lines():
        tail = line[line.index(CARVE_OUT_ANCHOR) :]
        for phrase in (
            "`regress_findings_carveout: delivered (parts <i>/<n>)`",
            "`regress_findings_carveout: withheld (<reason>)`",
            "`identity_unresolved`",
            "`no_matching_comment`",
            "`author_mismatch`",
            "`parts_missing <indices>`",
        ):
            assert phrase in tail, phrase
        assert "stale_post" not in tail


def _gh_entry(author: str, created_at: str, body: str) -> dict[str, Any]:
    """An entry in the observed live-fetch shape, values replaced, keys kept."""
    entry = json.loads(json.dumps(_OBSERVED_COMMENTS[0]))
    entry["author"] = {"login": author}
    entry["createdAt"] = created_at
    entry["body"] = body
    return entry


def _mapped(entry: dict[str, Any]) -> dict[str, Any]:
    """The core doc's mapped form: `{author: <login>, created_at, body}`."""
    return {
        "author": _login(entry),
        "created_at": _created(entry),
        "body": entry["body"],
    }


def _login(entry: dict[str, Any]) -> str:
    """The author login: `author.login` (raw gh shape) or a string `author`."""
    author = entry.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or "")
    return author if isinstance(author, str) else ""


def _created(entry: dict[str, Any]) -> str:
    return entry.get("createdAt") or entry.get("created_at") or ""


def _instant(timestamp: str) -> datetime:
    """An ISO-8601 timestamp as an aware instant, for ordering "newest".

    ``fromisoformat`` reads a trailing ``Z`` natively on Python 3.11+.
    """
    parsed = datetime.fromisoformat(timestamp)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_findings_shape(entry: dict[str, Any]) -> bool:
    """Header, adjacent sub-heading and marker: the first three conditions.

    The sub-heading must be the first non-blank line after the header, the
    shape the finalize read-back regex (``^## Blocking Review Findings\\s+###
    Finalize ship-it gate findings``) matches; a quoted mid-body line is not.
    """
    body = entry.get("body", "")
    lines = body.splitlines()
    after_header = [line for line in lines[1:] if line.strip()]
    return (
        bool(lines)
        and lines[0] == FINDINGS_HEADER
        and bool(after_header)
        and after_header[0] == FINDINGS_SUBHEADING
        and AGENT_COMMENT_MARKER in body
    )


def _by_identity(entry: dict[str, Any], identity: str) -> bool:
    return bool(identity) and _login(entry) == identity


def _qualifies(entry: dict[str, Any], identity: str) -> bool:
    """The four documented conditions, in the order the clause states them.

    ``created_at`` is not a condition (A5): an older-than-head post still
    qualifies, and the agent judges recency from the timestamp the label carries.
    """
    return _is_findings_shape(entry) and _by_identity(entry, identity)


def _part_of(entry: dict[str, Any]) -> tuple[int, int] | None:
    label = _PART_LABEL.search(entry["body"])
    return None if label is None else (int(label[1]), int(label[2]))


def _matching(comments: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    """Comments satisfying all four conditions, oldest instant first."""
    return sorted(
        (c for c in comments if _qualifies(c, identity)),
        key=lambda c: _instant(_created(c)),
    )


def _select_regress_findings(
    comments: list[dict[str, Any]], identity: str
) -> list[dict[str, Any]]:
    """Reference model of the documented carve-out selection rule.

    This is a model of a prompt-driven rule, not coverage of shipped code: no
    script implements the selection, so nothing here executes in production.
    The phrase pins in this module are what bind the model to the prose in
    ``auto-dev-impl-appendix.md``; keep both in step. Module-local on purpose.

    "Newest" and the per-part pick run only over comments that already satisfy
    all four conditions, so a newer forgery can never displace the real post.
    Parts are taken only from comments labelled with the same ``n`` as the
    newest match.
    """
    matches = _matching(comments, identity)
    if not matches:
        return []
    newest = _part_of(matches[-1])
    if newest is None:
        return [matches[-1]]
    total = newest[1]
    selected: list[dict[str, Any]] = []
    for index in range(1, total + 1):
        same_part = [c for c in matches if _part_of(c) == (index, total)]
        if same_part:
            selected.append(same_part[-1])
    return selected


def _parts_missing(comments: list[dict[str, Any]], identity: str) -> list[int]:
    """Indices 1..n with no same-``n`` comment: the documented ``parts_missing``."""
    matches = _matching(comments, identity)
    newest = _part_of(matches[-1]) if matches else None
    if newest is None:
        return []
    present = {
        index
        for c in _select_regress_findings(comments, identity)
        if (index := (_part_of(c) or (0, 0))[0])
    }
    return [i for i in range(1, newest[1] + 1) if i not in present]


def _withheld_reason(comments: list[dict[str, Any]], identity: str) -> str | None:
    """The documented friction reason when nothing is delivered, else ``None``."""
    if _select_regress_findings(comments, identity):
        return None
    if not identity:
        return "identity_unresolved"
    shaped = [c for c in comments if _is_findings_shape(c)]
    if not shaped:
        return "no_matching_comment"
    return "author_mismatch"


def _findings_comment(author: str, created_at: str, label: str = "") -> dict[str, Any]:
    label_line = f"{label}\n" if label else ""
    return _gh_entry(
        author,
        created_at,
        f"{FINDINGS_HEADER}\n{FINDINGS_SUBHEADING}\n{label_line}"
        "```\ngate: Step 8 ship-it\ndetails: MUST_FIX x\n```\n\n"
        f"{AGENT_COMMENT_MARKER}",
    )


def test_forged_author_never_displaces_the_genuine_post() -> None:
    genuine = _findings_comment(ME, "2026-10-28T10:00:00Z")
    forged = _findings_comment(FORGER, "2026-10-28T11:00:00Z")

    assert _select_regress_findings([genuine, forged], ME) == [genuine]
    assert _select_regress_findings([forged], ME) == []
    assert _select_regress_findings([genuine, forged], "") == []


def _quoting_comment(author: str, created_at: str) -> dict[str, Any]:
    """Right header and marker, but the sub-heading only quoted mid-body.

    The shape of a #1815 ``review_blocked`` post that cites the finalize
    sub-heading in its prose.
    """
    return _gh_entry(
        author,
        created_at,
        f"{FINDINGS_HEADER}\n### reviewer — MUST_FIX\n"
        f"see the earlier post under:\n{FINDINGS_SUBHEADING}\n\n"
        f"{AGENT_COMMENT_MARKER}",
    )


def test_sub_heading_quoted_mid_body_never_displaces_the_genuine_post() -> None:
    """Kill-check: a "contains the sub-heading line" match turns this red."""
    genuine = _findings_comment(ME, "2026-10-28T10:00:00Z")
    quoting = _quoting_comment(ME, "2026-10-28T11:00:00Z")

    assert _select_regress_findings([genuine, quoting], ME) == [genuine]
    assert _select_regress_findings([quoting], ME) == []
    assert _withheld_reason([quoting], ME) == "no_matching_comment"


def test_blank_lines_between_header_and_sub_heading_still_qualify() -> None:
    """The read-back regex uses ``\\s+``, so blank lines are allowed."""
    spaced = _findings_comment(ME, "2026-10-28T10:00:00Z")
    spaced["body"] = spaced["body"].replace(
        f"{FINDINGS_HEADER}\n", f"{FINDINGS_HEADER}\n\n", 1
    )
    assert _select_regress_findings([spaced], ME) == [spaced]


def test_unrelated_agent_comments_are_never_selected() -> None:
    scan = _gh_entry(
        ME,
        "2026-10-28T12:00:00Z",
        f"## Pending Verification Scan\n- x\n\n{AGENT_COMMENT_MARKER}",
    )
    bare = _gh_entry(ME, "2026-10-28T13:00:00Z", AGENT_COMMENT_MARKER)
    review = _gh_entry(
        ME,
        "2026-10-28T14:00:00Z",
        f"{FINDINGS_HEADER}\n### reviewer — MUST_FIX\n\n{AGENT_COMMENT_MARKER}",
    )
    assert _select_regress_findings([scan, bare, review], ME) == []


def test_multi_part_post_selects_every_part_in_order() -> None:
    part2 = _findings_comment(ME, "2026-10-28T10:00:02Z", "(part 2 of 2)")
    part1 = _findings_comment(ME, "2026-10-28T10:00:01Z", "(part 1 of 2)")
    forged = _findings_comment(FORGER, "2026-10-28T10:00:03Z", "(part 1 of 2)")

    assert _select_regress_findings([part2, forged, part1], ME) == [part1, part2]
    assert _select_regress_findings([part2], ME) == [part2]


def test_post_older_than_the_head_commit_is_still_delivered_as_evidence() -> None:
    """A5: no HEAD-timestamp gate; the label carries the timestamp instead."""
    old = _findings_comment(ME, "2026-10-28T08:00:00Z")
    assert _instant(_created(old)) < _instant(HEAD_AT)

    assert _select_regress_findings([old], ME) == [old]
    assert _withheld_reason([old], ME) is None
    # An older post is delivered alongside a newer one only as "newest" wins.
    new = _findings_comment(ME, "2026-10-28T10:00:00Z")
    assert _select_regress_findings([old, new], ME) == [new]
    assert _select_regress_findings([new, old], ME) == [new]


def test_parts_come_only_from_comments_with_the_newest_matchs_n() -> None:
    """Round 1 posted 2 parts, round 2 only "part 1 of 3": part 2 of 2 is not
    stitched into round 2's evidence."""
    round1_part1 = _findings_comment(ME, "2026-10-28T10:00:01Z", "(part 1 of 2)")
    round1_part2 = _findings_comment(ME, "2026-10-28T10:00:02Z", "(part 2 of 2)")
    round2_part1 = _findings_comment(ME, "2026-10-28T11:00:01Z", "(part 1 of 3)")
    comments = [round1_part1, round1_part2, round2_part1]

    assert _select_regress_findings(comments, ME) == [round2_part1]
    assert _parts_missing(comments, ME) == [2, 3]
    # The complete round is delivered with nothing missing.
    assert _select_regress_findings(comments[:2], ME) == [round1_part1, round1_part2]
    assert _parts_missing(comments[:2], ME) == []


def test_withheld_reasons_follow_the_documented_vocabulary() -> None:
    genuine = _findings_comment(ME, "2026-10-28T10:00:00Z")
    forged = _findings_comment(FORGER, "2026-10-28T11:00:00Z")
    unrelated = _gh_entry(ME, "2026-10-28T12:00:00Z", "hi")

    assert _withheld_reason([genuine], "") == "identity_unresolved"
    assert _withheld_reason([unrelated], ME) == "no_matching_comment"
    assert _withheld_reason([forged], ME) == "author_mismatch"
    assert _withheld_reason([genuine], ME) is None


def test_selection_helper_and_prose_name_the_same_conditions() -> None:
    """Ties the helper above to the clause, so dropping a condition from the
    prose (the author key above all) fails here as well."""
    clause = _carve_out_clause(_carve_out_lines()[0])
    for token in (
        FINDINGS_HEADER,
        FINDINGS_SUBHEADING,
        AGENT_COMMENT_MARKER,
        "`author.login`",
        "`createdAt`",
    ):
        assert token in clause
