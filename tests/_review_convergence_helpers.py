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

import json
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from cw.codex_review import CODEX_REVIEW_UNPARSEABLE, _prepare_review_pass
from cw.codex_runner import FakeCodexRunner
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    ConvergenceLedger,
    RoundFinding,
    UnreadFindings,
    from_verdict,
    new_ledger,
    record_round,
    resolve_confirm_backend,
    save_ledger,
)
from cw.review_findings import (
    ReviewerRunFailure,
    ReviewVerdict,
    consolidate_verdict,
)
from tests._codex_review_helpers import _finding_payload
from tests.conftest import (
    _make_diff,
    _make_finding,
    _make_reviewer_doc,
    commit_tracked_file,
    git_in,
    push_commit_to_origin,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cw.models.tasks import TicketTask
    from cw.review_adjudication import AdjudicationOutcome
    from cw.review_convergence import (
        ConfirmBackend,
        GatingSeverity,
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
    kind: RoundKind = "loop",
    backend: str | None = None,
    unread: UnreadFindings | None = None,
) -> tuple[ConvergenceLedger, RoundReport]:
    """Record one round through the real engine, defaulting the backend by kind."""
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


def confirmed_ledger(
    *, sha: str = DEFAULT_SHA, confirm: ConfirmBackend = "codex"
) -> ConvergenceLedger:
    """A ledger converged at *sha* through a clean loop round and a clean confirm."""
    ledger = fresh_ledger(confirm=confirm)
    ledger, report = record(ledger, sha=sha)
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


def branch_that_merged_main(
    repo: Path, scratch: Path, *, branch: str = "feature"
) -> MergedMainBranch:
    """Advance ``origin/main``, merge it into *branch*, commit once more, push.

    *repo* comes from ``make_git_repo_with_origin`` (a feature branch pushed to
    a bare origin whose ``main`` is still the fork point). The advance is the
    shared out-of-band push, ``push_commit_to_origin``, from the side clone at
    *scratch*; the origin is read from *repo*'s remote, so a caller that
    discarded the bare path does not need it. The branch is pushed after the
    merge, as the impl agent does, so ``origin/<branch>`` is its tip.
    """
    fork_point = git_in(repo, "merge-base", "origin/main", "HEAD")
    origin = Path(git_in(repo, "remote", "get-url", "origin"))
    push_commit_to_origin(origin, "main", scratch, MAIN_ONLY_FILE)
    git_in(repo, "checkout", branch)
    git_in(repo, "fetch", "origin")
    git_in(repo, "merge", "--no-edit", "origin/main")
    commit_tracked_file(repo, _AFTER_MERGE_FILE)
    git_in(repo, "push", "origin", branch)
    return MergedMainBranch(
        fork_point=fork_point,
        head=git_in(repo, "rev-parse", "HEAD"),
        merge_base=live_merge_base(repo),
        main_only_file=MAIN_ONLY_FILE,
    )


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
        task, repo, "main", runner=FakeCodexRunner(), session_id="warm-capability"
    )
