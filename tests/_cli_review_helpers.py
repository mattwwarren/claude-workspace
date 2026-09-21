"""Shared test helpers for the ``cw.cli.review`` per-submodule test suite.

Payload builders and git helpers used by both of the split
``test_cli_review_*.py`` files. This module has no ``test_`` prefix, so pytest
does not collect it (same convention as ``tests/conftest.py``); it is imported
explicitly by the test modules that use each helper.

It also hosts, as of #2210, the ``cw review settle`` payload builders
(:func:`_settle_entry`, :func:`_settle_payload`,
:func:`_extract_settle_payloads`) and the reworded-claim wording pair
(:data:`CLAIM_ROW1_RECORDED` / :data:`CLAIM_ROW1_CANDIDATE`) the codex-side
tests share with the CLI-side ones. Those two constants are one source of
truth on purpose: five tests across four modules assert that this exact pair
clears the claim matcher's thresholds, and a hand-typed copy in each would let
one drift silently past the matcher it is supposed to pin.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING, Any

from tests.conftest import _clean_git_env, commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# A single-file unified diff: hunk starts at new line 1, one context line
# (advances to 2), two added lines at 2 and 3. Mirrors the fixture shape in
# tests/test_codex_review.py's _MULTI_FILE_DIFF (#1236 precedent).
_CONSOLIDATE_DIFF = """diff --git a/src/cw/foo.py b/src/cw/foo.py
index 111..222 100644
--- a/src/cw/foo.py
+++ b/src/cw/foo.py
@@ -1,2 +1,3 @@
 unchanged = 0
+def broken():
+    pass
"""


def _consolidate_payload(**overrides: object) -> dict[str, Any]:
    """Minimal-but-valid ``cw review consolidate`` request envelope (#1241)."""
    payload: dict[str, Any] = {
        "documents": [],
        "diff": _CONSOLIDATE_DIFF,
        "reviewed_sha": "abc1234",
        "failed_reviewers": [],
    }
    payload.update(overrides)
    return payload


def _branch_repo(
    make_git_repo: Callable[..., Path], name: str
) -> tuple[Path, str, str]:
    """A repo with a `feature` branch one commit ahead of `main`.

    Returns ``(repo, reviewed_sha, real_diff_text)`` where *real_diff_text* is
    the verbatim ``git diff --no-color main...<reviewed_sha>`` output.
    """
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", "feature")
    commit_tracked_file(repo, "src/thing.py", "x = 1\ny = 2\n")
    reviewed_sha = git_in(repo, "rev-parse", "HEAD")
    # Unstripped on purpose: real_diff flows verbatim into the diff parser
    # under test, so the trailing newline must survive (git_in strips it).
    real_diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-color", f"main...{reviewed_sha}"],
        capture_output=True,
        check=True,
        text=True,
        env=_clean_git_env(),
    ).stdout
    return repo, reviewed_sha, real_diff


# #2210: row 1 of the claim-similarity table -- anchored regime (both sides
# carry the `_track_open_findings` symbol), 6 shared tokens out of 7 and 7, so
# Dice is 0.86 and both anchored floors clear. Every reworded-finding test in
# the suite uses this pair, and `test_claim_similarity_table` asserts
# `_claim_similarity` returns a score for it, so a threshold or tokenizer edit
# fails here rather than five tests away.
CLAIM_ROW1_RECORDED = (
    "`_track_open_findings` drops the follow-up task when the branch returns early"
)
CLAIM_ROW1_CANDIDATE = (
    "the early-return branch in `_track_open_findings` drops a follow-up task"
)

# Matches a fenced ```json block, back-referencing the opening fence's length
# so a payload rendered in a widened (4+ backtick) fence still extracts.
# re.DOTALL because the JSON body spans newlines.
_JSON_FENCE_RE = re.compile(r"(?P<fence>`{3,})json\n(?P<body>.*?)\n(?P=fence)", re.DOTALL)


def _settle_entry(**overrides: object) -> dict[str, Any]:
    """One ``cw review settle`` entry, defaulted to the common REJECTED case."""
    entry: dict[str, Any] = {
        "file": "src/cw/foo.py",
        "summary": "Bug here",
        "outcome": "REJECTED",
        "rationale": "",
    }
    entry.update(overrides)
    return entry


def _settle_payload(*entries: dict[str, Any], **overrides: object) -> dict[str, Any]:
    """The ``cw review settle`` request envelope around *entries*."""
    payload: dict[str, Any] = {
        "entries": list(entries) if entries else [_settle_entry()]
    }
    payload.update(overrides)
    return payload


def _extract_settle_payloads(comment: str) -> list[dict[str, Any]]:
    """Every fenced ``json`` payload in a rendered review *comment* (#2210).

    The blocking comment's ``### Settle a finding`` section renders one payload
    per keyable MUST_FIX finding; this reads them back the way an operator's
    copy-paste would, so a test can pipe one through ``cw review settle``
    unedited.
    """
    return [
        json.loads(match.group("body")) for match in _JSON_FENCE_RE.finditer(comment)
    ]
