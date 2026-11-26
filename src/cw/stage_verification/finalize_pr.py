"""PR detection, the live-identity bind, push, create and mergeability (#3062).

Built on the shared ``cw.gh`` reads and writes; no ``gh pr create`` or fetch
helper lives anywhere else in :mod:`cw.codex_finalize`.

* :func:`detect_branch_pr` is the Pre-Stage Detector Guard. The branch is the
  row's resolved feature branch (``resolve_feature_branch``), never
  ``git branch --show-current``.
* :func:`bind_live_pr` follows
  :func:`cw.stage_verification.impl_feedback.verify_review_feedback`:
  every lookup is live (``gh repo view``, then ``gh pr view`` by URL) and it
  fails closed. Later reads address the PR by the bound URL.
* :func:`probe_default` is a thin call into the C2 default-branch
  fetch-and-probe (:func:`cw.stage_verification.impl_feedback.probe_default_branch` with
  ``repair=True``, which never blocks). A conflict blocks
  ``merge_conflict_post_push``; cw never merges before the push. After it,
  at most one merge is made, by the CHANGELOG-union repair or the create-path
  semantic auto-resolve (``finalize_conflict_resolve``, #3542).
* :func:`check_mergeability` reads the post-push mergeability once; callers
  append their own note to its park. :func:`local_only_merge` makes a failed
  push of a post-push merge truthful.
* :func:`apply_body_section` replaces (or appends) one cw-written ``## `` region
  of a PR body in place; :func:`strip_cw_regions` removes every such region and
  the sign-off block from a copy of a body, so the UI-evidence media check never
  reads cw's own text (#3543). Both match a heading as a whole line and end a
  region only at a heading line or a sign-off marker line.
  :func:`apply_override_section` first makes the model-written override one
  region under the exact heading.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from cw.auto_dev_result.schema._vocab import EXTERNAL_STATE_BLOCKER_REASON
from cw.gh import (
    create_pr,
    fetch_branch_prs,
    fetch_pr_view,
    fetch_repo_name_with_owner,
    github_pr_url,
    parse_pr_url,
)
from cw.review_adjudication import REVIEW_ADJUDICATION_HEADING
from cw.review_signoff import END_MARKER, START_MARKER
from cw.stage_verification.finalize_gates import check_branch
from cw.stage_verification.finalize_result import (
    IMPL_NOT_PUSHED,
    MERGE_CONFLICT_POST_PUSH,
    OPERATOR_UNAVAILABLE,
    PR_ALREADY_TERMINAL,
    FinalizeBlock,
    classify_unavailability,
)
from cw.stage_verification.impl_feedback import PROBE_UNAVAILABLE, probe_default_branch
from cw.stage_verification.posted_text import redact_and_cap, redacted_tail
from cw.stage_verification.push import push_and_verify_head

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = [
    "NOTE_LINE_CAP",
    "OVERRIDE_HEADING",
    "PROBE_UNAVAILABLE",
    "BoundPr",
    "DetectedPr",
    "apply_body_section",
    "apply_override_section",
    "bind_live_pr",
    "check_mergeability",
    "create_pr_for",
    "detect_branch_pr",
    "heading_matches",
    "local_only_merge",
    "probe_default",
    "push_ahead",
    "strip_cw_regions",
]

PR_OPEN: Final = "OPEN"
PR_MERGED: Final = "MERGED"
PR_CLOSED: Final = "CLOSED"
BEHIND_FRICTION: Final = "behind_default_no_action"

_GH_TIMEOUT: Final = 30
_BIND_FIELDS: Final = "number,url,headRefName,headRefOid"
_MERGE_FIELDS: Final = "mergeable,mergeStateStatus"
_BLOCKING_MERGE: Final = frozenset({"CONFLICTING", "DIRTY"})
_MERGEABLE: Final = "MERGEABLE"
_TAIL: Final = 500
#: A park note is one line (this module's local-only-merge note and the semantic
#: auto-resolve's); the default per-line cap would cut it.
NOTE_LINE_CAP: Final = 600
OVERRIDE_HEADING: Final = "## Operator override"
#: A region ends at the next heading line or at a line that starts a sign-off
#: marker; a marker quoted mid-line (in finding text) ends nothing.
# Why: unheaded text under cw's last ``## `` section is deliberately part of
# that region (plan 2.2/2.8, documented in docs/dispatch-runbook.md).
_NEXT_REGION: Final = re.compile(r"\n(?:## |[ \t]*<!-- cw-review-signoff:)")
#: A model's own override heading line, however it is cased or suffixed.
_OVERRIDE_TITLE: Final = re.compile(r"## operator override\b", re.IGNORECASE)


@dataclass(frozen=True)
class DetectedPr:
    """The branch's PR the Detector Guard found."""

    number: int
    url: str
    state: str


@dataclass(frozen=True)
class BoundPr:
    """A PR bound to the live repo: its number and canonical URL."""

    number: int
    url: str


def detect_branch_pr(worktree: Path, branch: str) -> DetectedPr | FinalizeBlock | None:
    """The branch's PR (OPEN first, then MERGED), ``None``, or a block.

    CLOSED (with no OPEN or MERGED sibling) is ``pr_already_terminal``; a
    failed lookup is ``operator_unavailable`` (retryable).
    """
    prs, _available = fetch_branch_prs(branch, cwd=worktree)
    if prs is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh pr list --head {branch} failed; cw cannot tell whether the "
            "branch already has a PR, so nothing was created or reused",
            retry_eligible=True,
        )
    for state in (PR_OPEN, PR_MERGED):
        for pr in prs:
            if pr.state == state:
                return DetectedPr(pr.number, pr.url, pr.state)
    closed = [pr for pr in prs if pr.state == PR_CLOSED]
    if closed:
        return FinalizeBlock(
            PR_ALREADY_TERMINAL,
            f"PR #{closed[0].number} ({closed[0].url}) for branch {branch!r} is "
            "CLOSED without merging: a human decision the pipeline does not work "
            "around. Reopen it or close the ticket; cw creates no duplicate.",
        )
    return None


def _identity_block(why: str) -> FinalizeBlock:
    return FinalizeBlock(
        EXTERNAL_STATE_BLOCKER_REASON,
        f"the PR does not bind to the live repo: {why}; nothing was armed",
    )


def bind_live_pr(
    worktree: Path,
    *,
    pr_number: int,
    pr_url: str,
    branch: str,
    head_sha: str,
    require_head_match: bool,
) -> BoundPr | FinalizeBlock:
    """Bind *pr_url* to the live repo and PR, or fail closed (see module doc)."""
    live_repo = fetch_repo_name_with_owner(timeout=_GH_TIMEOUT, cwd=worktree)
    if live_repo is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            "gh repo view could not read the live repository",
            retry_eligible=True,
        )
    parsed = parse_pr_url(pr_url)
    if parsed != (live_repo, pr_number):
        return _identity_block(
            f"{pr_url!r} is not PR #{pr_number} of the live repo {live_repo}"
        )
    canonical = github_pr_url(live_repo, pr_number)
    view = fetch_pr_view(
        canonical, timeout=_GH_TIMEOUT, fields=_BIND_FIELDS, cwd=worktree
    )
    if view is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh pr view {canonical} could not read the live PR",
            retry_eligible=True,
        )
    if view.get("url") != canonical or view.get("headRefName") != branch:
        return _identity_block(
            f"the live PR reports url {view.get('url')!r} and head "
            f"{view.get('headRefName')!r}, expected {canonical!r} on {branch!r}"
        )
    if require_head_match and view.get("headRefOid") != head_sha:
        return FinalizeBlock(
            EXTERNAL_STATE_BLOCKER_REASON,
            f"PR #{pr_number}'s head {view.get('headRefOid')!r} is not the local "
            f"HEAD {head_sha}; the branch moved, so nothing was armed",
            retry_eligible=True,
        )
    return BoundPr(pr_number, canonical)


def probe_default(
    worktree: Path, *, base_branch: str, pr_exists: bool
) -> tuple[FinalizeBlock | None, tuple[str, ...]]:
    """Would merging ``origin/<default>`` conflict? ``(block, friction)``.

    Runs before the ahead-push, so a conflicting branch is never pushed. An
    unavailable fetch or probe is friction and the stage continues.
    """
    probe = probe_default_branch(worktree, base_branch=base_branch, repair=True)
    if not probe.needs_merge:
        return None, probe.friction
    files = redact_and_cap(", ".join(probe.conflicted_files))
    where = "the open PR" if pr_exists else "this branch (no PR exists yet)"
    return FinalizeBlock(
        MERGE_CONFLICT_POST_PUSH,
        f"merging origin/{base_branch} into {where} conflicts in {files}; "
        "cw does not resolve a conflict found before the push or on an open-PR "
        "reuse run. Merge or rebase the branch, push, then requeue finalize.",
        retry_eligible=pr_exists,
    ), probe.friction


def push_ahead(worktree: Path, *, branch: str, head_sha: str) -> FinalizeBlock | None:
    """Push HEAD to ``origin/<branch>`` and verify it, re-asserting the branch."""
    wrong = check_branch(worktree, branch)
    if wrong is not None:
        return wrong
    try:
        push_and_verify_head(worktree, head_sha)
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = str(getattr(exc, "stderr", "") or exc)
        tail = redacted_tail(stderr)
        reason = classify_unavailability(stderr)
        if reason is not None:
            return FinalizeBlock(
                reason,
                f"git push origin {branch} failed with an unavailability "
                f"signature: {tail}",
                retry_eligible=True,
            )
        return FinalizeBlock(
            IMPL_NOT_PUSHED, f"git push origin {branch} failed: {tail}"
        )
    return None


def create_pr_for(
    worktree: Path, *, base: str, branch: str, title: str, body_file: Path
) -> tuple[int, str] | FinalizeBlock:
    """``gh pr create`` (never draft): ``(number, printed url)`` or a block."""
    done = create_pr(
        base=base, head=branch, title=title, body_file=body_file, cwd=worktree
    )
    if done is None or done.returncode != 0:
        stderr = "" if done is None else done.stderr
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            "gh pr create failed: " + redact_and_cap(stderr.strip()[-_TAIL:]),
            retry_eligible=True,
        )
    url = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    parsed = parse_pr_url(url)
    if parsed is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh pr create printed no PR URL: {redact_and_cap(done.stdout[-_TAIL:])}",
            retry_eligible=True,
        )
    return parsed[1], url


def local_only_merge(
    block: FinalizeBlock, *, merge: str, branch: str, head_sha: str
) -> FinalizeBlock:
    """A failed push of a post-push merge, made truthful: the merge is local only.

    *merge* names it (``CHANGELOG union merge``, ``semantic auto-resolve merge``
    or ``origin/<base> merge``). A requeue reuses the open PR, and that path
    binds the PR head to local HEAD without pushing, so it can never clear this
    block: it is not retry-eligible until the operator pushes the merge by
    hand. No reset: the merge commit stays in the worktree.
    """
    note = redact_and_cap(
        f"the {merge} commit {head_sha} exists only in the local"
        f' worktree; push it with a plain "git push origin'
        f' HEAD:refs/heads/{branch}" and requeue, because a reused PR must'
        " match local HEAD",
        max_line_chars=NOTE_LINE_CAP,
    )
    return replace(block, details=f"{block.details}; {note}", retry_eligible=False)


def check_mergeability(
    worktree: Path, pr_url: str, *, unverified_token: str | None = None
) -> tuple[FinalizeBlock | None, list[str]]:
    """Post-push mergeability: CONFLICTING/DIRTY block; BEHIND is friction.

    *unverified_token* is appended to the friction when the view was read, is
    not blocking and ``mergeable`` is not ``MERGEABLE`` (the caller says what
    the unverified read means). A block or an unreadable view never carries it.
    """
    view = fetch_pr_view(
        pr_url, timeout=_GH_TIMEOUT, fields=_MERGE_FIELDS, cwd=worktree
    )
    if view is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh pr view {pr_url} could not read mergeability",
            retry_eligible=True,
        ), []
    mergeable = str(view.get("mergeable") or "")
    state = str(view.get("mergeStateStatus") or "")
    if mergeable in _BLOCKING_MERGE or state in _BLOCKING_MERGE:
        return FinalizeBlock(
            MERGE_CONFLICT_POST_PUSH,
            f"{pr_url} is not mergeable after the push (mergeable={mergeable}, "
            f"mergeStateStatus={state})",
            retry_eligible=True,
        ), []
    friction = [BEHIND_FRICTION] if state == "BEHIND" else []
    if unverified_token is not None and mergeable != _MERGEABLE:
        friction.append(unverified_token)
    return None, friction


def heading_matches(text: str, heading: str) -> Iterator[re.Match[str]]:
    """Every whole-line occurrence of *heading* in *text*.

    A substring would let an inline mention of a heading in model-written prose
    start a region, so only a line that is the heading (plus trailing
    whitespace, a CRLF included) matches.
    """
    return re.finditer(rf"^{re.escape(heading)}[ \t\r]*$", text, re.MULTILINE)


def _region_end(text: str, after_heading: int) -> int:
    """Where the region whose heading line ends at *after_heading* ends."""
    found = _NEXT_REGION.search(text, after_heading)
    return found.start() if found else len(text)


def apply_body_section(body: str, section: str, *, heading: str) -> str:
    """Replace (or append) the *heading* region of *body* with *section*.

    The region runs from its heading line to the next heading line, a
    ``<!-- cw-review-signoff:`` marker line, or the end of the body. A *section*
    without the heading gets it prefixed. A non-blank tail is joined behind a
    blank line and a region at the end keeps its closing newline, so
    re-applying the same section is byte-stable.
    """
    block = section.strip()
    if not block.startswith(heading):
        block = f"{heading}\n\n{block}"
    match = next(heading_matches(body, heading), None)
    if match is None:
        return f"{body.rstrip()}\n\n{block}\n"
    tail = body[_region_end(body, match.end()) :]
    joined = body[: match.start()] + block
    return joined + ("\n\n" + tail.lstrip() if tail.strip() else "\n")


def _normalized_override(section: str) -> str:
    """*section* as one region: the exact heading line and no later ``## `` line.

    The text is model-written (or the override JSON). A suffixed or differently
    cased heading does not match the region and a later ``## `` line ends it
    early; either way :func:`strip_cw_regions` would leave cw's own override
    text in the media check, which could then arm auto-merge on it.
    """
    lines = section.strip().splitlines()
    if lines and _OVERRIDE_TITLE.match(lines[0]):
        del lines[0]
    rest = "\n".join(f"#{line}" if line.startswith("## ") else line for line in lines)
    return f"{OVERRIDE_HEADING}\n\n" + rest.strip("\n")


def apply_override_section(body: str, section: str) -> str:
    """:func:`apply_body_section` for the operator override, normalised first."""
    return apply_body_section(
        body, _normalized_override(section), heading=OVERRIDE_HEADING
    )


def _heading_spans(text: str, heading: str) -> list[tuple[int, int]]:
    return [
        (m.start(), _region_end(text, m.end())) for m in heading_matches(text, heading)
    ]


def _signoff_spans(text: str) -> list[tuple[int, int]]:
    """Each sign-off block, with an unterminated start running to the end."""
    # Why not reuse ``review_signoff._remove_sections``: it keeps the text after
    # an unterminated start marker, which would fail open for the media check.
    spans: list[tuple[int, int]] = []
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    index = 0
    while index < len(lines):
        marker = lines[index].strip()
        if marker == START_MARKER:
            end = next(
                (
                    n
                    for n in range(index + 1, len(lines))
                    if lines[n].strip() == END_MARKER
                ),
                None,
            )
            stop = len(lines) - 1 if end is None else end
            spans.append((offsets[index], offsets[stop + 1]))
            index = stop + 1
            continue
        if marker == END_MARKER:
            spans.append((offsets[index], offsets[index + 1]))
        index += 1
    return spans


def strip_cw_regions(body: str) -> str:
    """*body* without the regions cw itself writes into a PR body.

    Removes every ``## Review adjudication`` region, every ``## Operator
    override`` region and every sign-off block (a start marker with no end runs
    to the end of the body; a lone end marker line is dropped alone). The result
    feeds the UI-evidence media check only and is never written back to the PR.
    All spans are collected on one LF-normalised text and deleted together, so
    removing one region cannot shift another.
    """
    text = body.replace("\r\n", "\n")
    spans = sorted(
        _heading_spans(text, REVIEW_ADJUDICATION_HEADING)
        + _heading_spans(text, OVERRIDE_HEADING)
        + _signoff_spans(text)
    )
    kept: list[str] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            kept.append(text[cursor:start])
        cursor = max(cursor, end)
    kept.append(text[cursor:])
    return "".join(kept)
