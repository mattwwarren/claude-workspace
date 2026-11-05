"""RFC 0016 adoption intake: read and classify one open PR (GitHub #2943).

``cw dev-queue add <TICKET> --adopt-pr <N>`` reads the PR here, with no lock
held, before ``add_ticket`` takes the dev-queue lock. The module sits above
``cw.dev_queue`` (it imports ``cw.pr_hydrate``, which imports ``cw.dev_queue``),
which is why it is not part of the dev-queue package.

Refusals (:class:`~cw.exceptions.AdoptionRefusedError`, no row written), in
precedence order (R7): gh unreadable; a fork PR (``isCrossRepository``); a PR
that is not OPEN; a ``headRefName`` failing the row's branch charset; then a
malformed ``headRefOid`` or a PR from another repository. The carried-by-a-
live-row check runs later, inside ``add_ticket``'s lock. Parks (a row IS
written, BLOCKED_ON_USER): ``mergeable: UNKNOWN`` after the bounded re-poll
parks ``adopt_pr_state_unknown``; then ``CONFLICTING`` and/or failing CI parks
``adopt_pr_not_green`` naming every condition that holds. PENDING CI is not
failing (the shared helper's behaviour), and a draft PR is green.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from cw._git import FULL_COMMIT_ID_RE
from cw.dev_queue import (
    _PRE_DISPATCH_ADOPT_PR_NOT_GREEN_REASON,
    _PRE_DISPATCH_ADOPT_PR_STATE_UNKNOWN_REASON,
    ADOPT_PR_NOT_GREEN_DISPOSITION,
    ADOPT_PR_STATE_UNKNOWN_DISPOSITION,
    transition_task_status,
)
from cw.dev_queue.adoption import AdoptedPrSnapshot
from cw.exceptions import AdoptionRefusedError
from cw.gh import GH_PR_STATE_OPEN, PR_ADOPT_FIELDS, fetch_issue_body, fetch_pr_view
from cw.models import QueueItemStatus, Stage, TicketTask
from cw.pr_hydrate import _parse_pr_url, repo_slug_mismatch, summarize_status_checks
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import ClientConfig

#: Total ``gh pr view`` reads while GitHub reports ``mergeable: UNKNOWN``.
ADOPT_UNKNOWN_MAX_POLLS: Final = 3
#: Seconds slept between those reads (injected ``sleep`` in tests).
ADOPT_UNKNOWN_POLL_INTERVAL_SECONDS: Final = 5.0
#: Bound, in seconds, on the ticket-existence ``gh issue view`` read.
_ISSUE_READ_TIMEOUT_SECONDS: Final = 15

_MERGEABLE_UNKNOWN: Final = "UNKNOWN"
_MERGEABLE_CONFLICTING: Final = "CONFLICTING"
_CHARSET_PROBE_TICKET: Final = "charset-probe"


@dataclass(frozen=True)
class AdoptIntake:
    """One classified adoption read.

    ``disposition`` is ``None`` for a green PR, else the park to stamp;
    ``reasons`` names every not-green condition (empty when green); and
    ``polls`` is how many ``gh pr view`` reads it took.
    """

    snapshot: AdoptedPrSnapshot
    disposition: str | None
    reasons: tuple[str, ...]
    polls: int

    @property
    def blocked_reason(self) -> str | None:
        if self.disposition == ADOPT_PR_STATE_UNKNOWN_DISPOSITION:
            return _PRE_DISPATCH_ADOPT_PR_STATE_UNKNOWN_REASON
        if self.disposition == ADOPT_PR_NOT_GREEN_DISPOSITION:
            return _PRE_DISPATCH_ADOPT_PR_NOT_GREEN_REASON
        return None

    @property
    def details(self) -> str:
        return "; ".join(self.reasons)


def _refuse(msg: str) -> AdoptionRefusedError:
    return AdoptionRefusedError(msg)


def _branch_charset_error(branch: str) -> str | None:
    """The row's branch validator's message for *branch*, or ``None``."""
    try:
        TicketTask(
            ticket_id=_CHARSET_PROBE_TICKET, client=_CHARSET_PROBE_TICKET, branch=branch
        )
    except ValidationError as exc:
        return "; ".join(str(err["msg"]) for err in exc.errors())
    return None


def _str_field(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _check_refusals(
    payload: dict[str, object], pr_number: int, client: ClientConfig
) -> AdoptedPrSnapshot:
    """Raise on every intake refusal; return the snapshot when none applies."""
    if payload.get("isCrossRepository") is not False:
        msg = (
            f"Refusing to adopt PR #{pr_number}: fork PRs are not adoptable (RFC 0016)."
        )
        raise _refuse(msg)
    state = _str_field(payload, "state")
    if state != GH_PR_STATE_OPEN:
        msg = (
            f"Refusing to adopt PR #{pr_number}: it is {state or 'not OPEN'}, not OPEN."
        )
        raise _refuse(msg)
    branch = _str_field(payload, "headRefName")
    charset = _branch_charset_error(branch)
    if charset is not None:
        msg = (
            f"Refusing to adopt PR #{pr_number}: head branch {branch!r} fails the"
            f" row branch charset ({charset})."
        )
        raise _refuse(msg)
    head = _str_field(payload, "headRefOid")
    if not FULL_COMMIT_ID_RE.fullmatch(head):
        msg = (
            f"Refusing to adopt PR #{pr_number}: headRefOid {head!r} is not a full SHA."
        )
        raise _refuse(msg)
    url = _str_field(payload, "url")
    parsed = _parse_pr_url(url)
    if parsed is None:
        msg = f"Refusing to adopt PR #{pr_number}: unparseable PR url {url!r}."
        raise _refuse(msg)
    mismatch = repo_slug_mismatch(parsed[0], _git_dir(client))
    if mismatch is not None:
        msg = (
            f"Refusing to adopt PR #{pr_number}: it belongs to {parsed[0]}, but"
            f" client '{client.name}' is {mismatch}."
        )
        raise _refuse(msg)
    return AdoptedPrSnapshot(
        pr_url=url,
        branch=branch,
        head_sha=head,
        is_draft=payload.get("isDraft") is True,
    )


def _not_green_reasons(payload: dict[str, object]) -> tuple[str, ...]:
    reasons: list[str] = []
    if _str_field(payload, "mergeable") == _MERGEABLE_CONFLICTING:
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


def read_adopted_pr(
    client: ClientConfig,
    pr_number: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> AdoptIntake:
    """Read PR *pr_number* for *client* and classify it (no lock held).

    Raises :class:`AdoptionRefusedError` for a refusal. While GitHub reports
    ``mergeable: UNKNOWN`` it re-reads up to :data:`ADOPT_UNKNOWN_MAX_POLLS`
    times in total, sleeping :data:`ADOPT_UNKNOWN_POLL_INTERVAL_SECONDS`
    between reads; every read re-runs the refusals.
    """
    polls = 0
    while True:
        polls += 1
        payload = _read(pr_number, client)
        snapshot = _check_refusals(payload, pr_number, client)
        unknown = _str_field(payload, "mergeable") == _MERGEABLE_UNKNOWN
        if not unknown or polls >= ADOPT_UNKNOWN_MAX_POLLS:
            break
        sleep(ADOPT_UNKNOWN_POLL_INTERVAL_SECONDS)
    if unknown:
        reason = f"mergeable: UNKNOWN after {polls} reads"
        return AdoptIntake(
            snapshot, ADOPT_PR_STATE_UNKNOWN_DISPOSITION, (reason,), polls
        )
    reasons = _not_green_reasons(payload)
    disposition = ADOPT_PR_NOT_GREEN_DISPOSITION if reasons else None
    return AdoptIntake(snapshot, disposition, reasons, polls)


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
    """The review-entry row for *intake*; parked via the seam when not green.

    ``stage=REVIEW``, ``branch=headRefName``, the pin at ``headRefOid``, and
    ``pr_url`` equal to ``adopted_pr_url``. A park is applied with
    :func:`transition_task_status` after construction (never a constructor
    ``status=``), so the row is PENDING or BLOCKED_ON_USER, never COMPLETED.
    """
    snapshot = intake.snapshot
    try:
        task = TicketTask(
            ticket_id=ticket_id,
            client=client.name,
            lane=lane,
            stage=Stage.REVIEW,
            branch=snapshot.branch,
            adopted_pr_url=snapshot.pr_url,
            adopted_head_sha=snapshot.head_sha,
            pr_url=snapshot.pr_url,
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
