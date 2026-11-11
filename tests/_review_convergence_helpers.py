"""Shared builders for the converge-to-clean tests (#2787).

One home for the factories ``test_review_convergence.py``,
``test_cli_review_converge.py``, ``test_cli_review_confirm.py`` and
``test_queue_peek_convergence.py`` all need, in the style of
``tests/_cli_review_helpers.py``, so no test file grows its own copy.

Every ledger here is built THROUGH the real engine (``record_round``), never a
hand-typed dict, and every ``ReviewVerdict`` through the real
``consolidate_verdict`` path, so a change to either fails the helper loudly
instead of silently weakening a test.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from cw._git import git_output, run_git
from cw.codex_clean_confirm import CONFIRM_DEFER_RATIONALE
from cw.codex_review import CODEX_REVIEW_UNPARSEABLE, _prepare_review_pass
from cw.codex_runner import FakeCodexRunner
from cw.models import ClientConfig
from cw.models.client import ReviewConfig
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    ConvergenceLedger,
    RoundFinding,
    RoundInput,
    SettledEntry,
    UnreadFindings,
    decide_verdict,
    finding_identity,
    from_verdict,
    new_ledger,
    override_confirm_round,
    record_round,
    resolve_confirm_backend,
    save_ledger,
    save_round_verdict,
)
from cw.review_engine.constants import ENGINE_ROLE
from cw.review_findings import (
    ReviewerRunFailure,
    ReviewVerdict,
    consolidate_verdict,
)
from tests._codex_review_helpers import _finding_payload
from tests._shared.git_repos import commit_tracked_file, git_in, push_commit_to_origin
from tests._shared.review_builders import _make_diff, _make_finding, _make_reviewer_doc

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import pytest

    from cw.models.review_attestation import AttestationDeferral, AttestationFinding
    from cw.models.tasks import TicketTask
    from cw.review_adjudication import AdjudicationOutcome
    from cw.review_convergence import (
        ConfirmBackend,
        GatingSeverity,
        RecordableRoundKind,
        RoundKind,
        RoundReport,
    )
    from cw.review_findings import Disposition, Finding, Severity

DEFAULT_SHA = "sha-0001"
NOW = "2026-10-27T12:00:00+00:00"
LOOP_BACKEND = "claude-native"
ROSTER = ["Code Quality Reviewer", "Test Reviewer"]
TICKET_ID = "T-1"
FORK_POINT = "fork0001"
_GATING_DISCARD = frozenset({"MUST_FIX", "SHOULD_FIX"})
_FAILED_REASON = "unparseable_response"
_DISCARD_ROLE = "Performance Reviewer"


def round_finding(
    severity: GatingSeverity = "MUST_FIX",
    summary: str = "Bug here",
    *,
    file: str = "src/cw/foo.py",
    outcome: AdjudicationOutcome = "fix",
    rationale: str = "",
) -> RoundFinding:
    """A :class:`RoundFinding`; ``rationale`` defaults to blank (fine for ``fix``)."""
    return RoundFinding(
        severity=severity,
        file=file,
        summary=summary,
        outcome=outcome,
        rationale=rationale,
    )


def operator_finding(summary: str = "Operator must do X") -> RoundFinding:
    """A MUST_FIX ``operator_action`` finding, which has no diff anchor (``N/A``)."""
    return round_finding(
        "MUST_FIX",
        summary,
        file="N/A",
        outcome="operator_action",
        rationale="needs an operator",
    )


def round_payload(
    kind: RoundKind,
    backend: str,
    sha: str,
    *findings: RoundFinding,
    roster: Sequence[str] = tuple(ROSTER),
) -> dict[str, object]:
    """A ``record-round`` findings-form payload (the ``RoundInput`` envelope)."""
    return {
        "kind": kind,
        "backend": backend,
        "reviewed_sha": sha,
        "roster": list(roster),
        "findings": [f.model_dump(mode="json") for f in findings],
    }


def fresh_ledger(
    *,
    confirm: ConfirmBackend | None = "none",
    max_cycles: int = 8,
    loop_backend: str = LOOP_BACKEND,
    ticket_id: str = TICKET_ID,
    fork_point: str = FORK_POINT,
    checkpoint_3a_sha: str = "sha-0000",
    diff_base: str | None = None,
) -> ConvergenceLedger:
    """An empty ledger whose confirm backend is resolved as the CLI would.

    *diff_base* is the pinned review diff base (#2800); ``None`` is the shape a
    ledger written before that change has, and what ``converge init`` creates.
    """
    ledger = new_ledger(
        ticket_id=ticket_id,
        fork_point=fork_point,
        checkpoint_3a_sha=checkpoint_3a_sha,
        max_cycles=max_cycles,
        loop_backend=loop_backend,
        confirm=resolve_confirm_backend(confirm, loop_backend),
    )
    if diff_base is None:
        return ledger
    return ledger.model_copy(update={"diff_base": diff_base})


def record(
    ledger: ConvergenceLedger,
    *findings: RoundFinding,
    sha: str,
    kind: RecordableRoundKind = "loop",
    backend: str | None = None,
    unread: UnreadFindings | None = None,
    engine: bool = False,
) -> tuple[ConvergenceLedger, RoundReport]:
    """Record one round through the real engine, defaulting the backend by kind.

    *engine* (#3042) is forwarded to ``record_round`` as the round's engine flag.
    """
    chosen = backend or (ledger.confirm_backend if kind == "confirm" else LOOP_BACKEND)
    return record_round(
        ledger,
        kind=kind,
        backend=chosen,
        reviewed_sha=sha,
        findings=list(findings),
        roster=ROSTER,
        now=NOW,
        unread=unread,
        engine=engine,
    )


def ledger_with_rounds(
    *rounds: Sequence[RoundFinding],
    shas: Sequence[str] | None = None,
    confirm: ConfirmBackend | None = "none",
    max_cycles: int = 8,
    ticket_id: str = TICKET_ID,
    fork_point: str = FORK_POINT,
    diff_base: str | None = None,
) -> ConvergenceLedger:
    """A ledger after one ``loop`` round per argument, via the real engine.

    Round ``i`` is recorded at ``shas[i]`` (default ``sha-000<i+1>``). Every round
    but the last must return ``continue`` (the ledger would otherwise have
    stopped or converged mid-build), which the helper asserts, so a fixture
    mistake is loud. The last round's decision is whatever the engine says.
    *diff_base* is pinned on the ledger before the first round (#2800).
    """
    ledger = fresh_ledger(
        confirm=confirm,
        max_cycles=max_cycles,
        ticket_id=ticket_id,
        fork_point=fork_point,
        diff_base=diff_base,
    )
    chosen = (
        list(shas)
        if shas is not None
        else [f"sha-000{i + 1}" for i in range(len(rounds))]
    )
    for index, findings in enumerate(rounds):
        ledger, report = record(ledger, *findings, sha=chosen[index])
        if index < len(rounds) - 1:
            assert report.decision == "continue", (
                f"fixture round {index + 1} returned {report.decision}, not continue"
            )
    return ledger


def _padding_round(index: int) -> list[RoundFinding]:
    """Findings for padding round *index*: unique identities, alternating counts.

    Counts 2,1,2,1,... never form a non-decreasing window of three, so padding
    cannot trip ``no_progress`` on its own.
    """
    count = 2 if index % 2 == 0 else 1
    return [round_finding("SHOULD_FIX", f"padding {index}.{n}") for n in range(count)]


def _pad_cycles(ledger: ConvergenceLedger, cycles: int) -> ConvergenceLedger:
    for index in range(cycles):
        ledger, report = record(ledger, *_padding_round(index), sha=f"pad-{index}")
        assert report.decision == "continue"
    return ledger


def stopped_ledger(
    reason: str,
    *,
    sha: str,
    stop_identities: Sequence[str] = (),
    fix_cycles_used: int | None = None,
    max_cycles: int = 8,
) -> ConvergenceLedger:
    """A ledger stopped for *reason*, built by driving ``record_round`` to the stop.

    *reason* is ``"max_cycles"``, ``"operator_action"``, ``"oscillation"`` or
    ``"no_progress"``; the stopping round is recorded at *sha*. *stop_identities*
    are finding summaries: the recurring findings of an oscillation, or the
    ``N/A`` operator-action findings of an ``operator_action`` stop.
    *fix_cycles_used* pads that many ``continue`` rounds first (for
    ``max_cycles`` it defaults to, and must equal, *max_cycles*).
    """
    ledger = fresh_ledger(max_cycles=max_cycles)
    if reason == "max_cycles":
        cycles = max_cycles if fix_cycles_used is None else fix_cycles_used
        assert cycles == max_cycles, (
            "a max_cycles stop needs fix_cycles_used == max_cycles"
        )
        ledger = _pad_cycles(ledger, cycles)
        ledger, _ = record(ledger, round_finding(summary="one more"), sha=sha)
    else:
        ledger = _pad_cycles(ledger, fix_cycles_used or 0)
        ledger = _drive_to_stop(ledger, reason, sha, tuple(stop_identities))
    assert ledger.status == "stopped", f"expected stopped, got {ledger.status}"
    assert ledger.stop_reason == reason
    return ledger


def _drive_to_stop(
    ledger: ConvergenceLedger, reason: str, sha: str, summaries: tuple[str, ...]
) -> ConvergenceLedger:
    if reason == "operator_action":
        findings = [operator_finding(s) for s in summaries or ("Operator must do X",)]
        ledger, _ = record(ledger, *findings, sha=sha)
    elif reason == "oscillation":
        findings = [
            round_finding(summary=s) for s in summaries or ("Recurring defect",)
        ]
        ledger, _ = record(ledger, *findings, sha=f"{sha}-prior")
        ledger, _ = record(ledger, *findings, sha=sha)
    elif reason == "no_progress":
        for index in range(3):
            ledger, _ = record(
                ledger,
                *[round_finding(summary=f"stalled {index}.{n}") for n in range(3)],
                sha=sha if index == 2 else f"{sha}-stall-{index}",
            )
    else:
        msg = f"unknown stop reason {reason!r}"
        raise AssertionError(msg)
    return ledger


def stopped_confirm_ledger(
    reason: str,
    findings: Sequence[RoundFinding],
    *,
    sha: str,
    ticket_id: str = TICKET_ID,
    loop_backend: str = "codex",
    max_cycles: int = 8,
) -> ConvergenceLedger:
    """A ledger whose last round is a dirty Claude ``confirm`` stopped for *reason*.

    Git-free and I/O-free (#2834): driven through ``record_round`` only. A
    clean loop round at *sha* asks for the confirm, and the confirm round
    records *findings* at *sha* and stops. *reason* is ``"max_cycles"`` (the
    fix budget is padded to *max_cycles* first), ``"oscillation"`` (*findings*
    are dispatched for a fix at ``<sha>-prior`` first, so the confirm re-raises
    them) or ``"operator_action"`` (*findings* carry that outcome).
    ``no_progress`` is not offered: a confirm round always follows a clean loop
    round, so the stall detector cannot fire on it.
    """
    ledger = fresh_ledger(
        confirm="claude",
        loop_backend=loop_backend,
        ticket_id=ticket_id,
        max_cycles=max_cycles,
    )
    if reason == "max_cycles":
        ledger = _pad_cycles(ledger, max_cycles)
    elif reason == "oscillation":
        ledger, report = record(ledger, *findings, sha=f"{sha}-prior")
        assert report.decision == "continue", report.decision
    elif reason != "operator_action":
        msg = f"unsupported confirm stop reason {reason!r}"
        raise AssertionError(msg)
    ledger, report = record(ledger, sha=sha, backend=loop_backend)
    assert report.decision == "confirm_required", report.decision
    ledger, report = record(ledger, *findings, sha=sha, kind="confirm")
    assert ledger.status == "stopped", f"expected stopped, got {ledger.status}"
    assert ledger.stop_reason == reason, ledger.stop_reason
    assert ledger.rounds[-1].kind == "confirm"
    return ledger


def confirmed_ledger(
    *,
    sha: str = DEFAULT_SHA,
    confirm: ConfirmBackend = "codex",
    ticket_id: str = TICKET_ID,
    loop_backend: str = LOOP_BACKEND,
) -> ConvergenceLedger:
    """A ledger converged at *sha* through a clean loop round and a clean confirm.

    *loop_backend* is the ledger's loop backend (and the loop round's), so a
    test can show a rule holds for every backend.
    """
    ledger = fresh_ledger(
        confirm=confirm, ticket_id=ticket_id, loop_backend=loop_backend
    )
    ledger, report = record(ledger, sha=sha, backend=loop_backend)
    if report.decision == "confirm_required":
        ledger, report = record(ledger, sha=sha, kind="confirm")
    assert report.decision == "clean"
    assert ledger.status == "converged"
    return ledger


def write_ledger(worktree: Path, ledger: ConvergenceLedger) -> Path:
    """Persist *ledger* at the worktree's default ledger path; returns that path."""
    path = worktree / LEDGER_RELATIVE_PATH
    save_ledger(path, ledger)
    return path


#: The file ``branch_that_merged_main`` lands on ``origin/main`` only, so a diff
#: that names it was taken from a base before the merge of the default branch.
MAIN_ONLY_FILE = "main_only.py"
_AFTER_MERGE_FILE = "after_merge.py"


class MergedMainBranch(NamedTuple):
    """The shas of a feature branch that merged ``origin/main`` mid-review (#2800).

    ``fork_point`` is the merge-base before the merge (the Checkpoint-2 value
    the ledger is keyed by), ``merge_base`` the merge-base after it (the diff
    base the loop and the confirm must share), ``head`` the branch tip.
    """

    fork_point: str
    head: str
    merge_base: str
    main_only_file: str


def live_merge_base(repo: Path, branch: str = "main") -> str:
    """``git merge-base origin/<branch> HEAD``: the live diff base a pin must equal."""
    return git_in(repo, "merge-base", f"origin/{branch}", "HEAD")


def dirty_confirm_ledger(
    worktree: Path,
    verdict: ReviewVerdict,
    *,
    ticket_id: str = TICKET_ID,
    loop_sha: str | None = None,
    confirm_sha: str | None = None,
    max_cycles: int = 8,
    fork_point: str | None = None,
) -> ConvergenceLedger:
    """A codex-lane ledger whose last round is a dirty Claude confirm (#2807).

    The state the codex gate leaves after a Claude MUST_FIX: a clean codex
    ``loop`` round (awaiting confirm), then a ``confirm`` round recorded from
    *verdict* through the same ``from_verdict`` seam the gate uses, the ledger
    written at the worktree's default path and *verdict* persisted as that
    round's verdict file. Both shas default to the worktree's HEAD; the fork
    point defaults to the live merge-base, as the gate keys it. Git is read
    only for a value not given, so a caller passing *loop_sha*, *confirm_sha*
    and *fork_point* needs no repository (#2834). *verdict* must carry at least
    one MUST_FIX (the helper asserts the round continued).
    """
    head = (
        git_in(worktree, "rev-parse", "HEAD")
        if loop_sha is None or confirm_sha is None
        else ""
    )
    ledger = fresh_ledger(
        confirm="claude",
        loop_backend="codex",
        ticket_id=ticket_id,
        fork_point=fork_point if fork_point is not None else live_merge_base(worktree),
        max_cycles=max_cycles,
    )
    ledger, report = record(ledger, sha=loop_sha or head, backend="codex")
    assert report.decision == "confirm_required", report.decision
    decided = decide_verdict(verdict, CONFIRM_DEFER_RATIONALE)
    findings, unread = from_verdict(decided)
    ledger, report = record(
        ledger, *findings, sha=confirm_sha or head, kind="confirm", unread=unread
    )
    assert report.decision == "continue", report.decision
    path = write_ledger(worktree, ledger)
    save_round_verdict(path, report.round, verdict)
    return ledger


def overridden_confirm_ledger(
    worktree: Path,
    verdict: ReviewVerdict,
    *,
    ticket_id: str = TICKET_ID,
    sha: str | None = None,
    fork_point: str | None = None,
    actor: str = "octocat",
    reason: str = "accepted risk",
    now: str = NOW,
) -> ConvergenceLedger:
    """A :func:`dirty_confirm_ledger` converged by an operator override (#2834).

    Both rounds are recorded at *sha* (the worktree's HEAD when omitted), the
    override covers every MUST_FIX of *verdict* through the real
    ``override_confirm_round``, and the converged ledger is written back over
    the dirty one at the worktree's default path.
    """
    ledger = dirty_confirm_ledger(
        worktree,
        verdict,
        ticket_id=ticket_id,
        loop_sha=sha,
        confirm_sha=sha,
        fork_point=fork_point,
    )
    identities = [
        finding_identity("MUST_FIX", f.file, f.summary, fingerprint=f.fingerprint)
        for f in verdict.must_fix
    ]
    overridden, _ = override_confirm_round(
        ledger,
        identities,
        reviewed_sha=ledger.rounds[-1].reviewed_sha,
        round_identities=identities,
        actor=actor,
        reason=reason,
        now=now,
    )
    write_ledger(worktree, overridden)
    return overridden


def merge_origin_commit(repo: Path, scratch: Path, branch: str, filename: str) -> None:
    """Advance ``origin/<branch>`` out of band and merge it into HEAD.

    The advance is the shared ``push_commit_to_origin`` from the side clone at
    *scratch*; the origin is read from *repo*'s remote, so a caller that
    discarded the bare path does not need it. *repo* fetches and merges the
    new tip, leaving the merge commit on its current branch.
    """
    origin = Path(git_in(repo, "remote", "get-url", "origin"))
    push_commit_to_origin(origin, branch, scratch, filename)
    git_in(repo, "fetch", "origin")
    git_in(repo, "merge", "--no-edit", f"origin/{branch}")


def merge_origin_over_branch_file(
    repo: Path, scratch: Path, branch: str, filename: str
) -> None:
    """Advance ``origin/<branch>`` on *filename* and merge it, keeping main's side.

    *filename* is a file the feature branch also added, so the plain merge of
    ``merge_origin_commit`` would stop on an add/add conflict. ``-X theirs``
    resolves it to the default branch's content, leaving a merge commit that
    changes a file of the branch's own diff: the shape the tolerated-merge
    predicate must refuse.
    """
    origin = Path(git_in(repo, "remote", "get-url", "origin"))
    push_commit_to_origin(origin, branch, scratch, filename)
    git_in(repo, "fetch", "origin")
    git_in(repo, "merge", "--no-edit", "-X", "theirs", f"origin/{branch}")


def branch_that_merged_main(
    repo: Path, scratch: Path, *, branch: str = "feature"
) -> MergedMainBranch:
    """Advance ``origin/main``, merge it into *branch*, commit once more, push.

    *repo* comes from ``make_git_repo_with_origin`` (a feature branch pushed to
    a bare origin whose ``main`` is still the fork point). The advance and the
    merge are ``merge_origin_commit``. The branch is pushed after the merge, as
    the impl agent does, so ``origin/<branch>`` is its tip.
    """
    fork_point = live_merge_base(repo)
    git_in(repo, "checkout", branch)
    merge_origin_commit(repo, scratch, "main", MAIN_ONLY_FILE)
    commit_tracked_file(repo, _AFTER_MERGE_FILE)
    git_in(repo, "push", "origin", branch)
    return MergedMainBranch(
        fork_point=fork_point,
        head=git_in(repo, "rev-parse", "HEAD"),
        merge_base=live_merge_base(repo),
        main_only_file=MAIN_ONLY_FILE,
    )


#: The file :func:`branch_with_overlap_merge` changes on both sides.
OVERLAP_FILE = "shared.py"
_OVERLAP_LINES = 20


class OverlapMerge(NamedTuple):
    """A feature branch whose merge of ``origin/main`` overlaps its own file.

    ``clean`` is the branch tip before the merge (the reviewed sha), ``head``
    the merge, ``main_tip`` the ``origin/main`` it merged.
    """

    clean: str
    head: str
    main_tip: str


def branch_with_overlap_merge(
    repo: Path, scratch: Path, *, branch: str = "feature"
) -> OverlapMerge:
    """Build a textually clean merge of ``origin/main`` over a branch file.

    *repo* comes from ``make_git_repo_with_origin``. ``main`` gains
    :data:`OVERLAP_FILE` and the branch merges it; the branch edits its first
    line (the clean sha), ``origin/main`` edits its last line out of band, and
    the branch merges that cleanly and pushes. The merge's landed change is in
    a file of the branch's own diff and ``git merge-tree`` reproduces it
    exactly, so the tip rule tolerates it (#2979: overlap does not matter).
    """
    lines = [f"line {index}\n" for index in range(_OVERLAP_LINES)]
    git_in(repo, "checkout", "main")
    commit_tracked_file(repo, OVERLAP_FILE, "".join(lines))
    git_in(repo, "push", "origin", "main")
    git_in(repo, "checkout", branch)
    git_in(repo, "merge", "--no-edit", "main")
    commit_tracked_file(repo, OVERLAP_FILE, "".join(["branch edit\n", *lines[1:]]))
    git_in(repo, "push", "origin", branch)
    clean = git_in(repo, "rev-parse", "HEAD")
    origin = Path(git_in(repo, "remote", "get-url", "origin"))
    push_commit_to_origin(
        origin, "main", scratch, OVERLAP_FILE, "".join([*lines[:-1], "main edit\n"])
    )
    git_in(repo, "fetch", "origin")
    git_in(repo, "merge", "--no-edit", "origin/main")
    git_in(repo, "push", "origin", branch)
    return OverlapMerge(
        clean=clean,
        head=git_in(repo, "rev-parse", "HEAD"),
        main_tip=git_in(repo, "rev-parse", "origin/main"),
    )


def side_branch_commit(
    repo: Path, *, base: str = "main", back_to: str = "feature"
) -> str:
    """A commit on a branch off *base* that HEAD (on *back_to*) never reaches."""
    git_in(repo, "checkout", "-b", "side", base)
    commit_tracked_file(repo, "side.py")
    sha = git_in(repo, "rev-parse", "HEAD")
    git_in(repo, "checkout", back_to)
    return sha


def rewind_origin_main(repo: Path, sha: str) -> None:
    """Point the local ``origin/main`` at *sha*, as an unfetched push leaves it."""
    git_in(repo, "update-ref", "refs/remotes/origin/main", sha)


def publish_develop(repo: Path) -> None:
    """Publish ``origin/main``'s commit as ``origin/develop`` and fetch it.

    *repo* comes from ``make_git_repo_with_origin``. The fetch makes
    ``origin/develop`` resolvable locally, as a caller reading it directly
    (rather than through ``converge diff-base``, which fetches itself) needs.
    """
    git_in(repo, "push", "origin", "refs/remotes/origin/main:refs/heads/develop")
    git_in(repo, "fetch", "origin")


def _verdict_finding(index: int, severity: Severity, summary: str) -> Finding:
    return _make_finding(
        severity=severity,
        summary=summary,
        line_start=10 + index,
        line_end=10 + index,
        evidence=f"def broken_{index}():",
    )


def consolidated_verdict(
    *,
    accepted: Sequence[tuple[Severity, str]] = (),
    rejected_must_fix: bool = False,
    discarded: dict[str, int] | None = None,
    failed_role: str | None = None,
    failed_reason: str = _FAILED_REASON,
    sha: str = DEFAULT_SHA,
) -> ReviewVerdict:
    """A ``ReviewVerdict`` built through the REAL ``consolidate_verdict`` path.

    *accepted* is ``(severity, summary)`` pairs, each anchored at its own line so
    dedup never merges two. ``rejected_must_fix=True`` adds a document whose
    MUST_FIX finding cites a file absent from the diff, which consolidation
    rejects as ``unknown_file`` (the #1714 shape). ``discarded={"SHOULD_FIX":
    1}`` adds a ``ReviewerRunFailure`` carrying that discard tally (the #2029
    shape). ``failed_role="<role>"`` adds a failure with no tally, the
    Claude-native ``unparseable_response`` of ``auto-dev-review.md`` Step 3a
    (*failed_reason* picks another, e.g. ``truncated_response``).

    The helper asserts the effect on the matching verdict field, so a change to
    consolidation fails here instead of silently weakening a test: a rejected
    MUST_FIX is non-empty; a discard tally holding MUST_FIX or SHOULD_FIX
    populates ``run_failures_with_should_fix_discards`` while NIT/DEBT-only
    leaves it empty; and every failure leaves a ``failed`` ``agents_run`` record.
    """
    findings = [
        _verdict_finding(i, severity, summary)
        for i, (severity, summary) in enumerate(accepted)
    ]
    lines = [f"def broken_{i}():" for i in range(len(findings))] or ["def broken():"]
    diff = _make_diff(
        *lines, files={"src/cw/foo.py": [10 + i for i in range(len(lines))]}
    )
    documents = [_make_reviewer_doc(*findings)]
    if rejected_must_fix:
        documents.append(
            _make_reviewer_doc(
                _make_finding(
                    severity="MUST_FIX",
                    file="src/cw/never_in_the_diff.py",
                    line_start=None,
                    line_end=None,
                    summary="dropped before adjudication",
                ),
                reviewer_role="Security Reviewer",
            )
        )
    failures: list[ReviewerRunFailure] = []
    if discarded is not None:
        failures.append(
            ReviewerRunFailure(
                role=_DISCARD_ROLE,
                reason=CODEX_REVIEW_UNPARSEABLE,
                discarded_finding_count=sum(discarded.values()),
                discarded_finding_severities=dict(discarded),
            )
        )
    if failed_role is not None:
        failures.append(ReviewerRunFailure(role=failed_role, reason=failed_reason))
    verdict = consolidate_verdict(documents, diff, sha, failed_reviewers=failures)
    _assert_verdict_shape(
        verdict, rejected_must_fix, discarded, [f.role for f in failures]
    )
    return verdict


def _assert_verdict_shape(
    verdict: ReviewVerdict,
    rejected_must_fix: bool,
    discarded: dict[str, int] | None,
    failed_roles: list[str],
) -> None:
    assert bool(verdict.rejected_must_fix) == rejected_must_fix
    gating = discarded is not None and any(
        severity in _GATING_DISCARD and count > 0
        for severity, count in discarded.items()
    )
    assert bool(verdict.run_failures_with_should_fix_discards) == gating
    for role in failed_roles:
        assert any(
            r.reviewer_role == role and r.status == "failed" for r in verdict.agents_run
        ), f"agents_run holds no failed record for {role}"
    _, unread = from_verdict(verdict)
    assert set(unread.failed_roles) == set(failed_roles)


def stamp_dispositions(
    verdict: ReviewVerdict, stamps: Mapping[str, tuple[Disposition, str]]
) -> ReviewVerdict:
    """Stamp ``{summary: (disposition, detail)}`` onto ``verdict.accepted``.

    The adjudication step's effect, applied with ``model_copy`` the way
    ``apply_adjudication`` does, for tests that need reject/defer/operator
    outcomes on a real consolidated verdict. Keyed by summary because
    consolidation reorders ``accepted``; every key must match a finding.
    """
    unknown = set(stamps) - {af.finding.summary for af in verdict.accepted}
    assert not unknown, f"no accepted finding with summary {sorted(unknown)}"
    accepted = [
        af.model_copy(
            update={
                "disposition": stamps[af.finding.summary][0],
                "disposition_detail": stamps[af.finding.summary][1],
            }
        )
        if af.finding.summary in stamps
        else af
        for af in verdict.accepted
    ]
    return verdict.model_copy(update={"accepted": accepted})


def confirm_verdict_payload(
    verdict: ReviewVerdict,
    *,
    result_blocker_reason: str | None = None,
    backend: str = "codex",
) -> dict[str, object]:
    """The exit-0 stdout of ``cw review confirm``: a verdict, never a clean signal."""
    return {
        "available": True,
        "backend": backend,
        "reviewed_sha": verdict.reviewed_sha,
        "result_blocker_reason": result_blocker_reason,
        "verdict": json.loads(verdict.model_dump_json()),
    }


def structurally_invalid_doc_json(severity: str) -> str:
    """A reviewer document with no ``reviewer_role`` key, claiming one finding.

    The whole-document structural failure ``run_codex_roles`` turns into a
    ``ReviewerRunFailure`` whose tally is ``{severity: 1}`` (#2029), built from
    the repo's own ``_finding_payload`` the way
    ``tests/test_codex_review_roles.py`` builds it.
    """
    return json.dumps(
        {
            "status": "ok",
            "detail": "reviewed",
            "findings": [_finding_payload(severity=severity)],
        }
    )


def codex_audit_fixture(name: str) -> str:
    """The text of the captured ``tests/fixtures/codex_audit_events/<name>``."""
    return (Path(__file__).parent / "fixtures" / "codex_audit_events" / name).read_text(
        encoding="utf-8"
    )


def warm_codex_capability(repo: Path, task: TicketTask) -> None:
    """Run one throwaway ``_prepare_review_pass`` to warm the #1709 capability cache.

    The first ``_prepare_review_pass`` in a process misses the cache and
    ``_probe_filesystem_capability`` then spends a slot of the runner it is
    given, so a ``_SequencedRunner`` would hand its first queued result to the
    probe instead of a reviewer role. Call this before the first ``confirm``
    invocation (precedent: ``tests/test_codex_review_core.py``).
    """
    _prepare_review_pass(
        task,
        repo,
        "main",
        config_branch="main",
        runner=FakeCodexRunner(),
        session_id="warm-capability",
    )


# ---------------------------------------------------------------------------
# The default-branch merge tolerance (#2815, #2861, #2979)
# ---------------------------------------------------------------------------

#: Every module that reads git through its own ``run_git``/``git_output``
#: binding on the tolerated-merge path: ``converge_tip`` (the fetch and the
#: current branch), the shared predicate module and ``cw.merge_probe`` (the one
#: ``git merge-tree`` spawner, #2979).
GIT_CONSUMERS = (
    "cw.cli.review.converge_tip",
    "cw.review_convergence._merge_tolerance",
    "cw.merge_probe",
)
#: The modules :class:`ScriptedMergeGit` patches: the predicate and the
#: ``merge-tree`` spawner it delegates to.
_TIP_RULE_MODULES = ("cw.review_convergence._merge_tolerance", "cw.merge_probe")
#: Symbolic shas the scripted tolerated merge defaults to.
PARENT2_SHA = "main-parent-sha"
#: The tree a scripted ``git merge-tree --write-tree`` writes, and the merge's
#: own tree by default (a merge git reproduces exactly). A full-length hex
#: sha, because ``cw.merge_probe`` refuses any other first field.
MERGED_TREE_SHA = "a" * 40
_SCRIPTED_FAILURE = "fatal: scripted"

GitAnswer = str | int | BaseException
GitScript = dict[tuple[str, ...], GitAnswer]


class ScriptedMergeGit:
    """A default-deny git fake for the tip rule (``_merge_tolerance``, ``merge_probe``).

    Kept only for the paths real git cannot reach on demand: unreadable git,
    a ``ValueError``, an old git and a timeout. Each script key is an argv; a
    call answers from the longest key that is a prefix of its argv (the
    ``_git_answering`` precedent in ``tests/test_review_convergence_diff_base.py``).
    A ``str`` answers rc 0 with that stdout. An ``int`` is that exit code with a
    ``fatal: scripted`` stderr. An exception instance is raised. An unscripted
    argv raises ``AssertionError``, so a test using it never reaches real git,
    and every call is recorded in ``calls`` (``calls == []`` means git was
    never asked).
    """

    def __init__(self, script: Mapping[tuple[str, ...], GitAnswer]) -> None:
        self.script: GitScript = dict(script)
        self.calls: list[list[str]] = []

    def _answer(self, argv: Sequence[str]) -> str | int:
        call = list(argv)
        self.calls.append(call)
        keys = [key for key in self.script if tuple(call[: len(key)]) == key]
        if not keys:
            msg = f"unscripted git call: {call}"
            raise AssertionError(msg)
        answer = self.script[max(keys, key=len)]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def run_git(
        self, argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        answer = self._answer(argv)
        if isinstance(answer, str):
            return subprocess.CompletedProcess(list(argv), 0, answer, "")
        return subprocess.CompletedProcess(list(argv), answer, "", _SCRIPTED_FAILURE)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> ScriptedMergeGit:
        """Patch ``run_git`` on the tip-rule modules; returns ``self``."""
        for module in _TIP_RULE_MODULES:
            monkeypatch.setattr(f"{module}.run_git", self.run_git)
        return self


def merge_tree_key(merge: str) -> tuple[str, ...]:
    """The tip rule's ``merge-tree`` argv for *merge*'s two parents (#2979)."""
    return (
        "merge-tree",
        "--write-tree",
        "--no-messages",
        "--name-only",
        "-z",
        f"{merge}^1",
        f"{merge}^2",
    )


def tolerated_merge_script(
    clean: str,
    head: str,
    default_sha: str,
    branch: str = "main",
    *,
    merge: str | None = None,
    parent2: str = PARENT2_SHA,
) -> GitScript:
    """The git answers for "one textually clean default-branch merge after *clean*".

    The argvs are the ones ``default_merge_rejection`` issues, in order: the
    default branch's tracking ref, the clean-is-ancestor check, the
    first-parent merge listing (*merge*, default *head*, with parents *clean*
    and *parent2*), the own-commit listing, the merged parent's ancestry, then
    ``merge-tree`` of the merge's parents and the merge's own tree (equal).
    """
    merged = merge or head
    span = f"{clean}..{head}"
    merges = ("rev-list", "--reverse", "--first-parent", "--parents", "--merges")
    return {
        ("rev-parse", f"refs/remotes/origin/{branch}^{{commit}}"): f"{default_sha}\n",
        ("merge-base", "--is-ancestor", clean, head): "",
        (*merges, span): f"{merged} {clean} {parent2}\n",
        ("rev-list", "--reverse", "--no-merges", span, f"^{default_sha}"): "",
        ("merge-base", "--is-ancestor", parent2, default_sha): "",
        merge_tree_key(merged): f"{MERGED_TREE_SHA}\0",
        ("rev-parse", f"{merged}^{{tree}}"): f"{MERGED_TREE_SHA}\n",
    }


def non_merge_commit_script(clean: str, head: str, default_sha: str) -> GitScript:
    """:func:`tolerated_merge_script` plus a branch commit the default lacks."""
    script = tolerated_merge_script(clean, head, default_sha)
    span = f"{clean}..{head}"
    own = ("rev-list", "--reverse", "--no-merges", span, f"^{default_sha}")
    script[own] = "own-commit\n"
    return script


def branch_with_conflict_resolution_merge(
    repo: Path, scratch: Path, *, branch: str = "feature"
) -> OverlapMerge:
    """A merge of ``origin/main`` that carries a hand-resolved conflict (#2979).

    *repo* comes from ``make_git_repo_with_origin``. ``main`` gains
    :data:`OVERLAP_FILE`, the branch merges it and edits its first line (the
    clean sha), ``origin/main`` edits the SAME line out of band, and the
    branch's merge stops on the conflict, is resolved by hand, committed and
    pushed. ``git merge-tree`` reports that merge as a conflict, so the tip
    rule must refuse it (``not_textually_clean``).
    """
    lines = [f"line {index}\n" for index in range(_OVERLAP_LINES)]
    git_in(repo, "checkout", "main")
    commit_tracked_file(repo, OVERLAP_FILE, "".join(lines))
    git_in(repo, "push", "origin", "main")
    git_in(repo, "checkout", branch)
    git_in(repo, "merge", "--no-edit", "main")
    commit_tracked_file(repo, OVERLAP_FILE, "".join(["branch edit\n", *lines[1:]]))
    git_in(repo, "push", "origin", branch)
    clean = git_in(repo, "rev-parse", "HEAD")
    origin = Path(git_in(repo, "remote", "get-url", "origin"))
    push_commit_to_origin(
        origin, "main", scratch, OVERLAP_FILE, "".join(["main edit\n", *lines[1:]])
    )
    git_in(repo, "fetch", "origin")
    stopped = run_git(
        ["merge", "--no-edit", "origin/main"], cwd=repo, capture_output=True
    )
    assert stopped.returncode != 0, "the fixture merge must conflict"
    resolved = "".join(["branch and main edit\n", *lines[1:]])
    (repo / OVERLAP_FILE).write_text(resolved, encoding="utf-8")
    git_in(repo, "add", OVERLAP_FILE)
    git_in(repo, "commit", "--no-edit")
    git_in(repo, "push", "origin", branch)
    return OverlapMerge(
        clean=clean,
        head=git_in(repo, "rev-parse", "HEAD"),
        main_tip=git_in(repo, "rev-parse", "origin/main"),
    )


def patch_run_git(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    """Set ``run_git`` to *fake* on every module in :data:`GIT_CONSUMERS`."""
    for module in GIT_CONSUMERS:
        monkeypatch.setattr(f"{module}.run_git", fake)


def fail_when(
    monkeypatch: pytest.MonkeyPatch,
    prefix: list[str],
    how: str,
    nth: int | None = None,
) -> list[list[str]]:
    """Fail the tip-check git calls whose argv starts with *prefix*.

    Patches ``run_git`` (and ``git_output`` where the module binds it) on
    every module in :data:`GIT_CONSUMERS` with the same closures, so *nth*
    counts matches across both. *how*: ``oserror`` raises ``OSError``;
    ``rc128`` returns rc 128 from ``run_git`` and raises
    ``CalledProcessError`` from ``git_output``; ``unicode`` raises
    ``UnicodeDecodeError``. Every other call is real. *nth* (0-based) fails
    only that matching call and lets the others through; ``None`` fails every
    match. Returns the argvs that were failed, so a test can assert the
    injected failure was reached.
    """
    failed: list[list[str]] = []
    seen = 0

    def _hit(argv: list[str]) -> bool:
        nonlocal seen
        if list(argv[: len(prefix)]) != prefix:
            return False
        index, seen = seen, seen + 1
        if nth is not None and index != nth:
            return False
        failed.append(list(argv))
        return True

    def _fail(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if how == "oserror":
            vanished = "git vanished"
            raise OSError(vanished)
        if how == "unicode":
            codec, reason = "utf-8", "invalid start byte"
            raise UnicodeDecodeError(codec, b"\xff", 0, 1, reason)
        return subprocess.CompletedProcess(argv, 128, "", "fatal: injected")

    def _run(argv: list[str], **kwargs: Any) -> Any:
        if _hit(argv):
            return _fail(list(argv))
        return run_git(argv, **kwargs)

    def _output(argv: list[str], **kwargs: Any) -> str:
        if _hit(argv):
            result = _fail(list(argv))
            raise subprocess.CalledProcessError(result.returncode, argv)
        return git_output(argv, **kwargs)

    for module in GIT_CONSUMERS:
        monkeypatch.setattr(f"{module}.run_git", _run)
        if hasattr(importlib.import_module(module), "git_output"):
            monkeypatch.setattr(f"{module}.git_output", _output)
    return failed


def make_review_client(
    workspace: Path,
    *,
    convergence: str = "clean",
    default_merge_tolerance: bool = True,
    default_branch: str = "main",
    ignore_paths: Sequence[str] = (),
    name: str = "test-client",
) -> ClientConfig:
    """A client whose review settings drive the tolerated-merge seam (#2861)."""
    return ClientConfig(
        name=name,
        workspace_path=workspace,
        default_branch=default_branch,
        merge_gate_ignore_paths=list(ignore_paths),
        review=ReviewConfig.model_validate(
            {
                "convergence": convergence,
                "default_merge_tolerance": default_merge_tolerance,
            }
        ),
    )


# ---------------------------------------------------------------------------
# The conflict-resolution round (#2980)
# ---------------------------------------------------------------------------

#: The file the resolution fixtures conflict on, and its line count.
RESOLUTION_FILE = "shared.py"
_RESOLUTION_LINES = 40
#: The line both sides edit (the conflict), and the distant lines each side
#: edits on its own: the branch's before the clean sha, main's out of band.
_CONFLICT_LINE = 0
_MAIN_ONLY_LINE = 20
_BRANCH_ONLY_LINE = 30
RESOLUTION_MARKER = "branch and main edit\n"
BRANCH_ONLY_TEXT = "branch distant edit\n"
MAIN_ONLY_TEXT = "main distant edit\n"
#: The second conflict :func:`branch_with_two_resolutions` resolves.
SECOND_RESOLUTION_MARKER = "second resolution\n"
#: The ledger shapes every resolution refusal is checked on: a claude-native
#: loop with a codex confirm, and a codex loop with a claude confirm.
RESOLUTION_LEDGER_SHAPES: tuple[tuple[str, ConfirmBackend], ...] = (
    ("claude-native", "codex"),
    ("codex", "claude"),
)


class ResolutionFixture(NamedTuple):
    """A feature branch whose merge of ``origin/main`` carries a resolution.

    ``clean`` is the reviewed sha, ``head`` the branch tip, ``merge`` the
    resolution merge and ``main_tip`` the ``origin/main`` it merged.
    """

    clean: str
    head: str
    merge: str
    main_tip: str


def resolution_ledger(
    *,
    loop_backend: str = LOOP_BACKEND,
    confirm: ConfirmBackend = "codex",
    confirm_not_independent: bool = False,
    clean: str = DEFAULT_SHA,
    ticket_id: str = TICKET_ID,
) -> ConvergenceLedger:
    """A ledger converged at *clean* through the real engine (#2980).

    A clean loop round on *loop_backend*, then a clean confirm round when the
    engine asks for one. ``confirm_not_independent`` resolves an explicit
    ``claude`` confirm for a ``claude-native`` loop through
    ``resolve_confirm_backend``, so the engine stamps the flag itself.
    """
    chosen: ConfirmBackend = "claude" if confirm_not_independent else confirm
    ledger = fresh_ledger(
        confirm=chosen, loop_backend=loop_backend, ticket_id=ticket_id
    )
    assert ledger.confirm_not_independent is confirm_not_independent
    ledger, report = record(ledger, sha=clean, backend=loop_backend)
    if report.decision == "confirm_required":
        ledger, report = record(ledger, sha=clean, kind="confirm")
    assert report.decision == "clean", report.decision
    return ledger


def _text(edits: Mapping[int, str] | None = None) -> str:
    """:data:`RESOLUTION_FILE`'s 40 lines with *edits* (line index to text)."""
    lines = [f"line {index}\n" for index in range(_RESOLUTION_LINES)]
    for index, text in (edits or {}).items():
        lines[index] = text
    return "".join(lines)


def _seed_resolution_branch(repo: Path, branch: str) -> str:
    """Give main :data:`RESOLUTION_FILE`, edit it on *branch*; returns the clean sha.

    The branch edits a distant line in one commit and the conflict line in
    the next (the clean sha), then pushes.
    """
    git_in(repo, "checkout", "main")
    commit_tracked_file(repo, RESOLUTION_FILE, _text())
    git_in(repo, "push", "origin", "main")
    git_in(repo, "checkout", branch)
    git_in(repo, "merge", "--no-edit", "main")
    commit_tracked_file(
        repo, RESOLUTION_FILE, _text({_BRANCH_ONLY_LINE: BRANCH_ONLY_TEXT})
    )
    commit_tracked_file(
        repo,
        RESOLUTION_FILE,
        _text(
            {
                _CONFLICT_LINE: "branch edit\n",
                _BRANCH_ONLY_LINE: BRANCH_ONLY_TEXT,
            }
        ),
    )
    git_in(repo, "push", "origin", branch)
    return git_in(repo, "rev-parse", "HEAD")


def _push_main(repo: Path, scratch: Path, filename: str, content: str) -> None:
    origin = Path(git_in(repo, "remote", "get-url", "origin"))
    push_commit_to_origin(origin, "main", scratch, filename, content)
    git_in(repo, "fetch", "origin")


def _conflicting_main_merge(repo: Path) -> None:
    stopped = run_git(
        ["merge", "--no-edit", "origin/main"], cwd=repo, capture_output=True
    )
    assert stopped.returncode != 0, "the fixture merge must conflict"


def _commit_resolution(repo: Path, content: str | None) -> None:
    """Commit the stopped merge resolved to *content*, or with its markers left in."""
    if content is not None:
        (repo / RESOLUTION_FILE).write_text(content, encoding="utf-8")
    git_in(repo, "add", RESOLUTION_FILE)
    git_in(repo, "commit", "--no-edit")


def _fixture(repo: Path, clean: str, merge: str, branch: str) -> ResolutionFixture:
    git_in(repo, "push", "origin", branch)
    return ResolutionFixture(
        clean=clean,
        head=git_in(repo, "rev-parse", "HEAD"),
        merge=merge,
        main_tip=git_in(repo, "rev-parse", "origin/main"),
    )


def _resolved_text(conflict: str = RESOLUTION_MARKER) -> str:
    return _text(
        {
            _CONFLICT_LINE: conflict,
            _MAIN_ONLY_LINE: MAIN_ONLY_TEXT,
            _BRANCH_ONLY_LINE: BRANCH_ONLY_TEXT,
        }
    )


def branch_with_resolution_and_unrelated_hunks(
    repo: Path,
    scratch: Path,
    *,
    branch: str = "feature",
    resolved: str | None = RESOLUTION_MARKER,
    clean_merge_first: bool = False,
) -> ResolutionFixture:
    """A resolved conflict next to hunks each side made alone (#2980).

    *repo* comes from ``make_git_repo_with_origin``. :data:`RESOLUTION_FILE`
    has 40 lines. The branch edits a distant line (before the clean sha) and
    the first line (the clean sha). ``origin/main`` edits the first line
    differently (the conflict) and another distant line (merged cleanly by
    git). The merge is resolved by hand to *resolved* on the first line;
    ``None`` commits the conflicted file with its markers left in.
    *clean_merge_first* first merges a textually clean ``origin/main``
    change to another file, so the span holds two merges.
    """
    clean = _seed_resolution_branch(repo, branch)
    if clean_merge_first:
        _push_main(repo, scratch, "main_clean.py", "clean = 1\n")
        git_in(repo, "merge", "--no-edit", "origin/main")
    main_text = _text({_CONFLICT_LINE: "main edit\n", _MAIN_ONLY_LINE: MAIN_ONLY_TEXT})
    _push_main(repo, scratch, RESOLUTION_FILE, main_text)
    _conflicting_main_merge(repo)
    _commit_resolution(repo, None if resolved is None else _resolved_text(resolved))
    merge = git_in(repo, "rev-parse", "HEAD")
    return _fixture(repo, clean, merge, branch)


def branch_with_two_resolutions(
    repo: Path, scratch: Path, *, branch: str = "feature"
) -> ResolutionFixture:
    """:func:`branch_with_resolution_and_unrelated_hunks`, then a second conflict."""
    first = branch_with_resolution_and_unrelated_hunks(repo, scratch, branch=branch)
    main_text = _text(
        {
            _CONFLICT_LINE: "main edit again\n",
            _MAIN_ONLY_LINE: MAIN_ONLY_TEXT,
        }
    )
    _push_main(repo, scratch, RESOLUTION_FILE, main_text)
    _conflicting_main_merge(repo)
    _commit_resolution(repo, _resolved_text(SECOND_RESOLUTION_MARKER))
    return _fixture(repo, first.clean, git_in(repo, "rev-parse", "HEAD"), branch)


def branch_with_hand_edited_clean_merge(
    repo: Path, scratch: Path, *, branch: str = "feature"
) -> ResolutionFixture:
    """A textually clean merge of ``origin/main`` committed with a hand edit."""
    clean = _seed_resolution_branch(repo, branch)
    _push_main(repo, scratch, "main_clean.py", "clean = 1\n")
    git_in(repo, "merge", "--no-commit", "--no-ff", "origin/main")
    edited = _text(
        {
            _CONFLICT_LINE: "branch edit\n",
            _MAIN_ONLY_LINE: "hand edit\n",
            _BRANCH_ONLY_LINE: BRANCH_ONLY_TEXT,
        }
    )
    (repo / RESOLUTION_FILE).write_text(edited, encoding="utf-8")
    git_in(repo, "add", RESOLUTION_FILE)
    git_in(repo, "commit", "--no-edit")
    return _fixture(repo, clean, git_in(repo, "rev-parse", "HEAD"), branch)


#: The tree the scripted resolution merge commits: not the tree the scripted
#: ``merge-tree`` writes (:data:`MERGED_TREE_SHA`), so it is a resolution.
RESOLVED_TREE_SHA = "b" * 40
_SCRIPTED_BRANCH_BASE = "branch-base-sha"
_SCRIPTED_RESOLUTION_DIFF = (
    f"diff --git a/{RESOLUTION_FILE} b/{RESOLUTION_FILE}\n"
    f"--- a/{RESOLUTION_FILE}\n"
    f"+++ b/{RESOLUTION_FILE}\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


def resolution_diff_keys(
    conflicted: str = MERGED_TREE_SHA, resolved: str = RESOLVED_TREE_SHA
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The name-only and unified tree-diff argvs of the resolution round."""
    return (
        ("diff", "--name-only", "-z", "--no-renames", conflicted, resolved),
        ("diff", "--no-color", "--no-renames", conflicted, resolved),
    )


def resolution_merge_script(clean: str, head: str, default_sha: str) -> GitScript:
    """:func:`tolerated_merge_script` whose merge commits a hand-resolved tree.

    ``merge-tree`` writes :data:`MERGED_TREE_SHA` (exit 0) while the merge's
    own tree is :data:`RESOLVED_TREE_SHA`, then the resolution round's tree
    diffs and branch endpoints answer: a one-file resolution of
    :data:`RESOLUTION_FILE` with no conflict marker.
    """
    script = tolerated_merge_script(clean, head, default_sha)
    script["rev-parse", f"{head}^{{tree}}"] = f"{RESOLVED_TREE_SHA}\n"
    names, unified = resolution_diff_keys()
    script[names] = f"{RESOLUTION_FILE}\0"
    script[unified] = _SCRIPTED_RESOLUTION_DIFF
    script["merge-base", f"{head}^1", f"{head}^2"] = f"{_SCRIPTED_BRANCH_BASE}\n"
    script["rev-parse", f"{head}^1"] = f"{clean}\n"
    return script


def octopus_resolution_script(clean: str, head: str, default_sha: str) -> GitScript:
    """:func:`resolution_merge_script` whose merge has three parents."""
    script = resolution_merge_script(clean, head, default_sha)
    merges = ("rev-list", "--reverse", "--first-parent", "--parents", "--merges")
    script[(*merges, f"{clean}..{head}")] = f"{head} {clean} {PARENT2_SHA} third\n"
    return script


def non_default_parent_resolution_script(
    clean: str, head: str, default_sha: str
) -> GitScript:
    """:func:`resolution_merge_script` whose merged parent is off the default branch."""
    script = resolution_merge_script(clean, head, default_sha)
    script["merge-base", "--is-ancestor", PARENT2_SHA, default_sha] = 1
    return script


# ---------------------------------------------------------------------------
# #3042 (RFC 0017 I3): the confirm round after a repo review engine pass
# ---------------------------------------------------------------------------

#: A literal ledger written before #3042: no round carries an ``engine`` key.
_OLD_LEDGER: dict[str, object] = {
    "schema_version": 1,
    "ticket_id": TICKET_ID,
    "fork_point": FORK_POINT,
    "max_cycles": 8,
    "loop_backend": LOOP_BACKEND,
    "confirm_backend": "codex",
    "checkpoint_3a_sha": "sha-0000",
    "rounds": [
        {
            "round": 1,
            "kind": "loop",
            "backend": LOOP_BACKEND,
            "reviewed_sha": DEFAULT_SHA,
            "new_accepted": 0,
            "new_fix": 0,
            "new_operator_action": 0,
            "new_settled": 0,
            "resurfaced_settled": 0,
            "clean": True,
            "roster": [ENGINE_ROLE],
            "recorded_at": NOW,
        }
    ],
    "awaiting_confirm_sha": DEFAULT_SHA,
    "status": "awaiting_confirm",
}


def old_ledger_json() -> str:
    """A literal pre-#3042 ledger JSON (an awaiting-confirm one, no ``engine``)."""
    return json.dumps(_OLD_LEDGER)


def engine_pass_ledger(
    ledger: ConvergenceLedger | None = None,
    *,
    confirm: ConfirmBackend = "codex",
    sha: str = DEFAULT_SHA,
    ticket_id: str = TICKET_ID,
    role: str = ENGINE_ROLE,
    engine: bool = True,
) -> ConvergenceLedger:
    """An ``awaiting_confirm`` ledger whose last round is a clean engine pass.

    The clean ``loop`` round is recorded at *sha* through the real
    ``record_round(..., engine=True)`` onto *ledger* (a fresh claude-native
    ledger with a *confirm* backend when omitted), with *role* as its roster.
    ``engine=False`` records the same round without the flag.
    """
    base = ledger or fresh_ledger(confirm=confirm, ticket_id=ticket_id)
    updated, report = record_round(
        base,
        kind="loop",
        backend=LOOP_BACKEND,
        reviewed_sha=sha,
        findings=[],
        roster=[role],
        now=NOW,
        engine=engine,
    )
    assert report.decision == "confirm_required", report.decision
    return updated


EngineEntry = str | tuple[str, str]


def _entry(entry: EngineEntry) -> tuple[str, str | None]:
    return (entry, None) if isinstance(entry, str) else entry


def engine_confirm_verdict(
    *,
    must_fix: Sequence[EngineEntry] = (),
    should_fix: Sequence[EngineEntry] = (),
    sha: str = DEFAULT_SHA,
    file: str | None = None,
) -> ReviewVerdict:
    """A confirm backend's verdict through :func:`consolidated_verdict`.

    Each entry is a summary or a ``(summary, rule_id)`` pair, so a
    ``coverage.*``/``acceptance.*`` finding can be built. *file* rewrites every
    finding's file (``consolidated_verdict`` anchors them in ``src/cw/foo.py``,
    which review-debt filing refuses as a fixture path).
    """
    pairs = [("MUST_FIX", *_entry(e)) for e in must_fix] + [
        ("SHOULD_FIX", *_entry(e)) for e in should_fix
    ]
    verdict = consolidated_verdict(
        accepted=[(severity, summary) for severity, summary, _ in pairs], sha=sha
    )
    rules = {summary: rule for _, summary, rule in pairs}

    def _rewrite(finding: Finding) -> Finding:
        update: dict[str, object] = {}
        if rules.get(finding.summary) is not None:
            update["rule_id"] = rules[finding.summary]
        if file is not None:
            update["file"] = file
        return finding.model_copy(update=update) if update else finding

    return verdict.model_copy(
        update={
            "accepted": [
                af.model_copy(update={"finding": _rewrite(af.finding)})
                for af in verdict.accepted
            ],
            "must_fix": [_rewrite(f) for f in verdict.must_fix],
        }
    )


def engine_confirm_payload(
    verdict: ReviewVerdict, *, backend: str = "codex"
) -> dict[str, object]:
    """The ``record-round --kind confirm`` verdict payload for *verdict*."""
    return {
        "kind": "confirm",
        "backend": backend,
        "reviewed_sha": verdict.reviewed_sha,
        "roster": ["codex"],
        "verdict": json.loads(verdict.model_dump_json()),
    }


def parked_confirm_ledger(
    worktree: Path,
    verdict: ReviewVerdict,
    *,
    ticket_id: str = TICKET_ID,
    fork_point: str = FORK_POINT,
) -> ConvergenceLedger:
    """An engine-pass ledger parked by a confirm round over *verdict* (#3042).

    The mirror of :func:`dirty_confirm_ledger`: a clean engine ``loop`` round at
    ``verdict.reviewed_sha``, then the confirm round decided exactly as
    ``record-round`` decides it (``prepare_engine_confirm``) and recorded
    through the engine, which must park. The ledger is written at the
    worktree's default path and the decided verdict saved as that round's
    verdict file.
    """
    from cw.cli.review.converge_confirm import prepare_engine_confirm

    sha = verdict.reviewed_sha
    ledger = engine_pass_ledger(
        fresh_ledger(confirm="codex", ticket_id=ticket_id, fork_point=fork_point),
        sha=sha,
    )
    payload = prepare_engine_confirm(
        RoundInput.model_validate(engine_confirm_payload(verdict)), ledger
    )
    assert payload.verdict is not None
    findings, unread = from_verdict(payload.verdict)
    ledger, report = record(ledger, *findings, sha=sha, kind="confirm", unread=unread)
    assert report.decision == "stop_confirm_must_fix", report.decision
    path = write_ledger(worktree, ledger)
    save_round_verdict(path, report.round, payload.verdict)
    return ledger


def settled_deferrals(ledger: ConvergenceLedger) -> list[SettledEntry]:
    """Every settled ``defer`` entry of *ledger*, in order."""
    return [entry for entry in ledger.settled if entry.outcome == "defer"]


def attestation_digest(
    ledger: ConvergenceLedger,
) -> tuple[list[AttestationFinding], list[AttestationDeferral]]:
    """The attestation's ``(deferrals, findings)`` digest of *ledger*."""
    from cw.dev_queue.review_attestation import _digest_from_ledger

    return _digest_from_ledger(ledger)
