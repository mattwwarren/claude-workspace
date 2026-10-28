"""Shared test helpers for the ``cw.cli.review`` per-submodule test suite.

Payload builders and git helpers used by all three of the split
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

As of #2319 it also hosts the raw ``ReviewVerdict`` / ``AcceptedFinding``
payload builders (:func:`_verdict_payload`, :func:`_accepted_payload`) and the
:data:`_TICKET` id, shared by ``test_cli_review_commands.py`` and the
``check-voided`` tests in ``test_cli_review_voided.py``.

As of #2701 it also hosts the ``cw review settle`` operator-machine setup
(:data:`_OPERATOR`, :data:`_SETTLE_REASON`, :func:`_write_session_context`,
:func:`_enter_operator_machine`) and :func:`_seed_review_row`, moved out of
``test_cli_review_commands.py`` so ``test_cli_review_settle_store.py`` shares
them rather than copying them.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING, Any

from cw.dev_queue import add_ticket
from cw.models import HOOK_CONTEXT_RELATIVE_PATH, QueueItemStatus, Stage, TicketTask
from tests.conftest import (
    _clean_git_env,
    _finding_kwargs,
    _seed_sessions,
    commit_tracked_file,
    git_in,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    import pytest

    from cw.models import Session
    from cw.review_finding_dispositions import FindingDisposition


#: The gh login every settle test resolves as the settling operator.
_OPERATOR = "mattwwarren"
#: The ``--reason`` every settle test passes unless it is testing the reason.
_SETTLE_REASON = "operator rejected: intentional tradeoff, see ADR-0012"


def _write_session_context(root: Path, *, headless: bool) -> None:
    """Stamp *root* with the ``.claude/cw-context.json`` ``cw`` would write.

    Shared by every settle case: "operator's machine" is expressed as a
    context reporting ``headless: false`` rather than as the absence of one,
    because the suite itself may run inside a headless worker whose context
    the upward search would otherwise find. Joins
    :data:`~cw.models.HOOK_CONTEXT_RELATIVE_PATH`, the same constant the
    writer and the guard use, so this fixture cannot drift onto another path.
    """
    path = root / HOOK_CONTEXT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 8, "session_id": "abc", "headless": headless}),
        encoding="utf-8",
    )


def _enter_operator_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, login: str = _OPERATOR
) -> None:
    """Run a settle test from a non-worker cw session worktree (#2701).

    A plain function rather than a fixture: a fixture imported from a
    non-conftest module trips F401/F811, so each consumer keeps a three-line
    autouse fixture that calls this. The repo checkout the suite runs in may
    carry a real ``headless: true`` context (under dispatch), so a nearer
    ``headless: false`` one is written into *tmp_path*; ``TMPDIR`` is cleared
    for the same reason, because a dispatch worker points it into its own
    headless worktree, which the guard reads as a worker.
    """
    _write_session_context(tmp_path, headless=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: login)


def _seed_review_row(
    ticket_id: str,
    client: str = "acme",
    *,
    ledger: dict[str, FindingDisposition] | None = None,
    status: QueueItemStatus = QueueItemStatus.PENDING,
    worktree_path: Path | None = None,
    session_id: str | None = None,
    sessions: Sequence[Session] = (),
) -> TicketTask:
    """Enqueue a REVIEW-stage row for *ticket_id* carrying *ledger* (#2701).

    Idempotent: ``add_ticket`` declines a second live row for the same
    ``(client, ticket_id)``, so a test may seed before every invoke. Returns
    the task it asked to enqueue. *worktree_path* (#2787) is where the row's
    worktree lives, for ``converge operator-settle`` tests that reach a ledger
    through the row. *session_id* binds the row to a session and *sessions*
    are appended to state (#2790); unlike the row, sessions are appended on
    every call, so pass them once.
    """
    task = TicketTask(
        ticket_id=ticket_id,
        client=client,
        stage=Stage.REVIEW,
        status=status,
        finding_dispositions=ledger or {},
        worktree_path=worktree_path,
        session_id=session_id,
    )
    add_ticket(task)
    if sessions:
        _seed_sessions(*sessions)
    return task


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


#: The ``ticket_id`` every payload command's test envelope correlates to.
_TICKET = "T-1814"


def _verdict_payload(*accepted: dict[str, Any], **overrides: object) -> dict[str, Any]:
    """A raw ``ReviewVerdict`` dict for the #1805 adjudicate/verify-fixes CLI."""
    must_fix = [
        af["finding"]
        for af in accepted
        if af["finding"]["severity"] == "MUST_FIX"
        and af.get("disposition", "fixed") != "deferred"
    ]
    payload: dict[str, Any] = {
        "blocking": bool(must_fix),
        "must_fix": must_fix,
        "reviewed_sha": "abc1234",
        "accepted": list(accepted),
        "review": {
            "must_fix_initial": len(must_fix),
            "should_fix": 0,
            "fix_cycles_used": 0,
            "deferred": 0,
            "agents_run": 1,
        },
    }
    payload.update(overrides)
    return payload


def _accepted_payload(**overrides: object) -> dict[str, Any]:
    """A raw ``AcceptedFinding`` dict wrapping ``_finding_kwargs``."""
    finding_overrides = {
        k: v
        for k, v in overrides.items()
        if k not in {"disposition", "disposition_detail", "reviewers"}
    }
    payload: dict[str, Any] = {
        "finding": _finding_kwargs(**finding_overrides),
        "reviewers": overrides.get("reviewers", ["Code Quality Reviewer"]),
    }
    for key in ("disposition", "disposition_detail"):
        if key in overrides:
            payload[key] = overrides[key]
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
_JSON_FENCE_RE = re.compile(
    r"(?P<fence>`{3,})json\n(?P<body>.*?)\n(?P=fence)", re.DOTALL
)


def _settle_entry(**overrides: object) -> dict[str, Any]:
    """One ``cw review settle`` entry, defaulted to the common REJECTED case.

    ``reviewed_sha`` is part of the rendered payload as of #2210 — the record
    has to be able to answer "against what code was this silenced", and the CLI
    refuses an entry it cannot resolve one for.
    """
    entry: dict[str, Any] = {
        "file": "src/cw/foo.py",
        "summary": "Bug here",
        "outcome": "REJECTED",
        "rationale": "",
        "reviewed_sha": "abc1234",
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
