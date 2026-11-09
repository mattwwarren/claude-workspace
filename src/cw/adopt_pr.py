"""RFC 0016 adoption intake: read and classify one open PR (GitHub #2943).

``cw dev-queue add <TICKET> --adopt-pr <N>`` reads the PR here, with no lock
held, before ``add_ticket`` takes the dev-queue lock. The module sits above
``cw.dev_queue`` (it imports ``cw.pr_hydrate``, which imports ``cw.dev_queue``),
which is why it is not part of the dev-queue package.

Refusals (:class:`~cw.exceptions.AdoptionRefusedError`, no row written), in
precedence order (R7): gh unreadable; a fork PR (``isCrossRepository``); a PR
that is not OPEN; a ``headRefName`` failing the row's branch charset; then a
malformed ``headRefOid`` or a PR from another repository. The carried-by-a-
live-row check runs later, inside ``add_ticket``'s lock.

Routing (#2944, RFC 0016 B1): a green PR enters at review. A ``CONFLICTING``
PR and/or one with failing CI enters at impl carrying repair feedback that
:func:`cw.pr_feedback.synthesize_pr_feedback` builds from the live PR, naming
every condition that holds; an operator's ``--review-feedback-file`` replaces
the synthesis once it matches the fresh read. A synthesis failure, or a head
that moved between the two reads, refuses. ``mergeable: UNKNOWN`` after the
bounded re-poll still parks (a row IS written, BLOCKED_ON_USER)
``adopt_pr_state_unknown``. PENDING CI is not failing (the shared helper's
behaviour), and a draft PR is green.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from cw.dev_queue import (
    _PRE_DISPATCH_ADOPT_PR_STATE_UNKNOWN_REASON,
    ADOPT_PR_STATE_UNKNOWN_DISPOSITION,
    transition_task_status,
)
from cw.dev_queue.adoption import AdoptedPrSnapshot
from cw.exceptions import AdoptionRefusedError
from cw.gh import PR_ADOPT_FIELDS, fetch_issue_body, fetch_pr_view
from cw.gh_check_vocabulary import (
    CHECK_RUN_CONCLUSIONS,
    CHECK_RUN_STATUSES,
    STATUS_CONTEXT_STATES,
)
from cw.models import QueueItemStatus, Stage, TicketTask
from cw.pr_feedback import FeedbackSynthesisError, synthesize_pr_feedback
from cw.pr_feedback_text import payload_string
from cw.pr_hydrate import _parse_pr_url, repo_slug_mismatch, summarize_status_checks
from cw.pr_identity import read_pr_identity
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import ClientConfig
    from cw.models.review_feedback import ReviewFeedback

#: Total ``gh pr view`` reads while GitHub reports ``mergeable: UNKNOWN``.
ADOPT_UNKNOWN_MAX_POLLS: Final = 3
#: Seconds slept between those reads (injected ``sleep`` in tests).
ADOPT_UNKNOWN_POLL_INTERVAL_SECONDS: Final = 5.0
#: Bound, in seconds, on the ticket-existence ``gh issue view`` read.
_ISSUE_READ_TIMEOUT_SECONDS: Final = 15

_MERGEABLE_UNKNOWN: Final = "UNKNOWN"
_MERGEABLE_CONFLICTING: Final = "CONFLICTING"
_MERGEABLE_VALUES: Final = frozenset(
    {"MERGEABLE", _MERGEABLE_CONFLICTING, _MERGEABLE_UNKNOWN}
)


@dataclass(frozen=True)
class AdoptIntake:
    """One classified adoption read.

    ``disposition`` is the park to stamp (only ``adopt_pr_state_unknown``),
    else ``None``; ``reasons`` names every not-green condition (empty when
    green); ``polls`` is how many ``gh pr view`` reads it took; and
    ``review_feedback`` is the repair feedback the row carries (synthesized,
    or the operator's file), which makes it enter at impl.
    """

    snapshot: AdoptedPrSnapshot
    disposition: str | None
    reasons: tuple[str, ...]
    polls: int
    review_feedback: ReviewFeedback | None = None

    @property
    def repair(self) -> bool:
        """True for a classified (not state-unknown) PR that is not green."""
        return self.disposition is None and bool(self.reasons)

    @property
    def blocked_reason(self) -> str | None:
        if self.disposition == ADOPT_PR_STATE_UNKNOWN_DISPOSITION:
            return _PRE_DISPATCH_ADOPT_PR_STATE_UNKNOWN_REASON
        return None

    @property
    def details(self) -> str:
        return "; ".join(self.reasons)


def _refuse(msg: str) -> AdoptionRefusedError:
    return AdoptionRefusedError(msg)


def _malformed_classification(pr_number: int, detail: str) -> AdoptionRefusedError:
    msg = f"Refusing to adopt PR #{pr_number}: gh returned malformed {detail}."
    return _refuse(msg)


def _check_refusals(
    payload: dict[str, object], pr_number: int, client: ClientConfig
) -> AdoptedPrSnapshot:
    """Raise on every intake refusal; return the snapshot when none applies."""
    if payload.get("isCrossRepository") is not False:
        msg = (
            f"Refusing to adopt PR #{pr_number}: fork PRs are not adoptable (RFC 0016)."
        )
        raise _refuse(msg)
    ident = read_pr_identity(payload)
    if not ident.is_open:
        msg = (
            f"Refusing to adopt PR #{pr_number}: it is"
            f" {ident.state or 'not OPEN'}, not OPEN."
        )
        raise _refuse(msg)
    if ident.head_ref_error is not None:
        msg = (
            f"Refusing to adopt PR #{pr_number}: head branch {ident.head_ref!r}"
            f" fails the row branch charset ({ident.head_ref_error})."
        )
        raise _refuse(msg)
    if not ident.head_sha_valid:
        msg = (
            f"Refusing to adopt PR #{pr_number}: headRefOid {ident.head_sha!r}"
            " is not a full SHA."
        )
        raise _refuse(msg)
    parsed = ident.url_target
    if parsed is None:
        msg = f"Refusing to adopt PR #{pr_number}: unparseable PR url {ident.url!r}."
        raise _refuse(msg)
    if parsed[1] != pr_number:
        msg = (
            f"Refusing to adopt PR #{pr_number}: PR url {ident.url!r} names"
            f" PR #{parsed[1]}, not PR #{pr_number}."
        )
        raise _refuse(msg)
    mismatch = repo_slug_mismatch(parsed[0], _git_dir(client))
    if mismatch is not None:
        msg = (
            f"Refusing to adopt PR #{pr_number}: it belongs to {parsed[0]}, but"
            f" client '{client.name}' is {mismatch}."
        )
        raise _refuse(msg)
    return AdoptedPrSnapshot(
        pr_url=ident.url,
        branch=ident.head_ref,
        head_sha=ident.head_sha,
        is_draft=payload.get("isDraft") is True,
    )


def _validate_classification_payload(
    payload: dict[str, object], pr_number: int
) -> None:
    """Refuse incomplete GitHub data before it can be classified as green."""
    mergeable = payload.get("mergeable")
    if not isinstance(mergeable, str) or mergeable not in _MERGEABLE_VALUES:
        raise _malformed_classification(pr_number, f"mergeable data ({mergeable!r})")

    rollup = payload.get("statusCheckRollup")
    if not isinstance(rollup, list):
        raise _malformed_classification(
            pr_number, "statusCheckRollup data (expected a list)"
        )
    for index, check in enumerate(rollup):
        if not isinstance(check, dict):
            raise _malformed_classification(
                pr_number, f"statusCheckRollup entry {index}"
            )
        typename = check.get("__typename")
        if typename == "CheckRun":
            status = check.get("status")
            if (
                not isinstance(status, str)
                or status not in CHECK_RUN_STATUSES
                or not isinstance(check.get("name"), str)
            ):
                raise _malformed_classification(
                    pr_number, f"CheckRun statusCheckRollup entry {index}"
                )
            conclusion = check.get("conclusion")
            if conclusion is not None and (
                not isinstance(conclusion, str)
                or conclusion not in CHECK_RUN_CONCLUSIONS
            ):
                raise _malformed_classification(
                    pr_number, f"CheckRun conclusion at statusCheckRollup entry {index}"
                )
        elif typename == "StatusContext":
            state = check.get("state")
            if (
                not isinstance(state, str)
                or state not in STATUS_CONTEXT_STATES
                or not isinstance(check.get("context"), str)
            ):
                raise _malformed_classification(
                    pr_number, f"StatusContext statusCheckRollup entry {index}"
                )
        else:
            raise _malformed_classification(
                pr_number, f"statusCheckRollup entry {index} (unknown type)"
            )


def _not_green_reasons(payload: dict[str, object]) -> tuple[str, ...]:
    reasons: list[str] = []
    if payload_string(payload, "mergeable") == _MERGEABLE_CONFLICTING:
        reasons.append("mergeable: CONFLICTING (resolve the merge conflict)")
    rollup = payload.get("statusCheckRollup")
    checks = (
        [c for c in rollup if isinstance(c, dict)] if isinstance(rollup, list) else []
    )
    summary = summarize_status_checks(checks)
    if not summary["ok"]:
        names = ", ".join(str(f["name"]) for f in summary["failing"])
        reasons.append(f"failing CI: {names}")
    return tuple(reasons)


def _read(pr_number: int, client: ClientConfig) -> dict[str, object]:
    payload = fetch_pr_view(
        str(pr_number), fields=PR_ADOPT_FIELDS, cwd=_git_dir(client)
    )
    if payload is None:
        msg = (
            f"Refusing to adopt PR #{pr_number}: gh could not read it"
            " (gh missing, unauthenticated, or the PR does not exist)."
        )
        raise _refuse(msg)
    return dict(payload)


def _live_repo(snapshot: AdoptedPrSnapshot, pr_number: int) -> tuple[str, int]:
    """``(owner/repo, number)`` from the snapshot's url.

    :func:`_check_refusals` already refused an unparseable url; the empty
    slug fallback only keeps this total, and it can never match a feedback
    file or name a PR the synthesizer would accept (both fail closed).
    """
    parsed = _parse_pr_url(snapshot.pr_url)
    return (parsed[0], pr_number) if parsed is not None else ("", pr_number)


def _bind_operator_feedback(
    feedback: ReviewFeedback, snapshot: AdoptedPrSnapshot, pr_number: int
) -> ReviewFeedback:
    """Return *feedback* when it names this PR at its fresh head, else refuse."""
    repo, _number = _live_repo(snapshot, pr_number)
    checks: tuple[tuple[str, object, object], ...] = (
        ("repo", feedback.repo, repo),
        ("pr_number", feedback.pr_number, pr_number),
        ("reviewed_sha", feedback.reviewed_sha, snapshot.head_sha),
    )
    for field, file_value, live_value in checks:
        if file_value != live_value:
            msg = (
                f"Refusing to adopt PR #{pr_number}: --review-feedback-file"
                f" {field} {file_value!r} does not match the PR ({live_value!r})."
            )
            raise _refuse(msg)
    return feedback


def _synthesize(
    client: ClientConfig, snapshot: AdoptedPrSnapshot, pr_number: int
) -> ReviewFeedback:
    """Repair feedback for the non-green PR, bound to the snapshot's head."""
    repo, number = _live_repo(snapshot, pr_number)
    try:
        feedback = synthesize_pr_feedback(repo, number, cwd=_git_dir(client))
    except FeedbackSynthesisError as exc:
        msg = f"Refusing to adopt PR #{pr_number}: cannot build repair feedback: {exc}."
        raise _refuse(msg) from exc
    if feedback.reviewed_sha != snapshot.head_sha:
        msg = (
            f"Refusing to adopt PR #{pr_number}: its head moved from"
            f" {snapshot.head_sha} to {feedback.reviewed_sha} while building"
            " repair feedback; re-run the add."
        )
        raise _refuse(msg)
    return feedback


def _poll(
    client: ClientConfig, pr_number: int, sleep: Callable[[float], None]
) -> tuple[dict[str, object], AdoptedPrSnapshot, int]:
    """Read until GitHub reports a mergeable state or the polls run out."""
    polls = 0
    while True:
        polls += 1
        payload = _read(pr_number, client)
        snapshot = _check_refusals(payload, pr_number, client)
        _validate_classification_payload(payload, pr_number)
        unknown = payload_string(payload, "mergeable") == _MERGEABLE_UNKNOWN
        if not unknown or polls >= ADOPT_UNKNOWN_MAX_POLLS:
            return payload, snapshot, polls
        sleep(ADOPT_UNKNOWN_POLL_INTERVAL_SECONDS)


def _classify_intake(
    client: ClientConfig,
    pr_number: int,
    payload: dict[str, object],
    snapshot: AdoptedPrSnapshot,
    polls: int,
    *,
    synthesize: bool,
    operator_feedback: ReviewFeedback | None,
) -> AdoptIntake:
    bound = (
        _bind_operator_feedback(operator_feedback, snapshot, pr_number)
        if operator_feedback is not None
        else None
    )
    if payload_string(payload, "mergeable") == _MERGEABLE_UNKNOWN:
        reason = f"mergeable: UNKNOWN after {polls} reads"
        return AdoptIntake(
            snapshot, ADOPT_PR_STATE_UNKNOWN_DISPOSITION, (reason,), polls, bound
        )
    reasons = _not_green_reasons(payload)
    if bound is None and reasons and synthesize:
        bound = _synthesize(client, snapshot, pr_number)
    return AdoptIntake(snapshot, None, reasons, polls, bound)


def read_adopted_pr(
    client: ClientConfig,
    pr_number: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    synthesize: bool = True,
    operator_feedback: ReviewFeedback | None = None,
) -> AdoptIntake:
    """Read PR *pr_number* for *client* and classify it (no lock held).

    Raises :class:`AdoptionRefusedError` for a refusal. While GitHub reports
    ``mergeable: UNKNOWN`` it re-reads up to :data:`ADOPT_UNKNOWN_MAX_POLLS`
    times in total, sleeping :data:`ADOPT_UNKNOWN_POLL_INTERVAL_SECONDS`
    between reads; every read re-runs the refusals.

    A non-green PR gets synthesized repair feedback unless *synthesize* is
    False (the requeue re-check reads only). *operator_feedback* (the
    ``--review-feedback-file`` payload) replaces the synthesis, for a green
    or non-green PR, once its repo, PR number and ``reviewed_sha`` match this
    read; it is bound even when the PR stays ``mergeable: UNKNOWN``.
    """
    payload, snapshot, polls = _poll(client, pr_number, sleep)
    return _classify_intake(
        client,
        pr_number,
        payload,
        snapshot,
        polls,
        synthesize=synthesize,
        operator_feedback=operator_feedback,
    )


def verify_ticket_exists(
    client: ClientConfig, ticket_id: str, pr_number: int
) -> str | None:
    """Check the ticket an adopted row names exists (A12); return a note.

    A review-entry row never runs Stage 0, so this read is its only intake.
    A PR adopted as its own ticket is proven by the PR read. A github-issues
    tracker reads the issue and refuses when it is unreadable. Any other
    tracker is not verified here; the returned note says so.
    """
    if ticket_id == str(pr_number):
        return None
    tracker = resolve_tracker(client.workspace_path)
    if tracker != TRACKER_GITHUB_ISSUES:
        return (
            f"note: ticket '{ticket_id}' existence not verified (tracker"
            f" {tracker or 'unknown'} is not {TRACKER_GITHUB_ISSUES})."
        )
    body = fetch_issue_body(
        ticket_id, _ISSUE_READ_TIMEOUT_SECONDS, cwd=_git_dir(client)
    )
    if body is None:
        msg = (
            f"Refusing to adopt PR #{pr_number} as ticket '{ticket_id}': gh could"
            f" not read issue {ticket_id}."
        )
        raise _refuse(msg)
    return None


def build_adopted_task(
    ticket_id: str, client: ClientConfig, intake: AdoptIntake, *, lane: str
) -> TicketTask:
    """The adopted row for *intake*: review entry, or impl entry with feedback.

    ``stage=REVIEW`` for a green PR, or ``stage=IMPL`` carrying
    ``review_feedback`` when the intake has repair feedback (its
    ``reviewed_sha`` equals the pin by construction); ``branch=headRefName``,
    the pin at ``headRefOid``, and ``pr_url`` equal to ``adopted_pr_url``. The
    state-unknown park is applied with :func:`transition_task_status` after
    construction (never a constructor ``status=``), so the row is PENDING or
    BLOCKED_ON_USER, never COMPLETED.
    """
    snapshot = intake.snapshot
    feedback = intake.review_feedback
    try:
        task = TicketTask(
            ticket_id=ticket_id,
            client=client.name,
            lane=lane,
            stage=Stage.REVIEW if feedback is None else Stage.IMPL,
            branch=snapshot.branch,
            adopted_pr_url=snapshot.pr_url,
            adopted_head_sha=snapshot.head_sha,
            pr_url=snapshot.pr_url,
            review_feedback=feedback,
            ever_spawned=False,
        )
    except ValidationError as exc:
        msg = f"Refusing to adopt {snapshot.pr_url} as ticket {ticket_id!r}: {exc}"
        raise _refuse(msg) from exc
    if intake.disposition is not None:
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=intake.disposition,
            blocked_reason=intake.blocked_reason,
            unproductive=False,
        )
    return task
