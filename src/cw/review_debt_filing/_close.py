"""Close debt tickets whose every linked PR closed unmerged (#3044, RFC 0017 III2).

:func:`sweep_debt_closes` is the library entry point; the dispatch loop calls
it on an interval. For each client whose tracker is exactly ``github-issues``
it lists the OPEN ``review-debt`` issues once, parses each body's
``<!-- cw-review-debt-pr: <url> -->`` markers, and closes an issue as not
planned only when every linked PR is CLOSED without being merged. One
``gh issue close --comment --reason`` call carries the redacted,
agent-marked comment, so a comment never lands without its close.

GitHub issue state is the ledger: only OPEN issues are listed, so a closed
issue is never looked at again. The sweep writes nothing to the dev-queue
store and takes no lock, so every ``gh`` call runs lock-free (ADR-0019). It
never raises on a ``gh`` failure: an unknown PR state never closes, a failed
close is re-read once (an already-closed race counts as closed) and is
otherwise retried on the next sweep.

Bounds: one :data:`DEBT_CLOSE_BUDGET_S` budget and at most
:data:`MAX_DEBT_CLOSES_PER_SWEEP` close attempts per sweep, across clients.
A listing that hits its 200-item cap is skipped for that client (no
pagination; a known limitation).
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

from cw.gh import (
    _GH_PR_STATE_CLOSED,
    _GH_PR_STATE_MERGED,
    _GH_PR_STATE_OPEN,
    agent_marked_body,
)
from cw.pr_hydrate import repo_slug_mismatch
from cw.redact_post_text import (
    git_toplevel,
    redact_post_text,
    redaction_anchors,
    resolve_home,
)
from cw.review_debt_filing._constants import (
    CLOSE_ISSUE_TIMEOUT_S,
    CLOSE_REASON_NOT_PLANNED,
    DEBT_CLOSE_BUDGET_S,
    GH_ISSUE_STATE_CLOSED,
    ISSUE_STATE_TIMEOUT_S,
    KEY_DISPLAY_CHARS,
    LIST_ISSUES_TIMEOUT_S,
    MAX_DEBT_CLOSES_PER_SWEEP,
    PR_VIEW_TIMEOUT_S,
    REVIEW_DEBT_LABEL,
)
from cw.review_debt_filing._events import emit_close_failed, emit_closed
from cw.review_debt_filing._marker import (
    parse_debt_key,
    parse_pr_markers,
    queue_ticket_ref,
)
from cw.review_debt_filing._models import (
    CallBudget,
    CloseContext,
    CloseOutcome,
    CloseResult,
    TrackerMode,
)
from cw.review_debt_filing._tracker import tracker_mode
from cw.tracker import resolve_tracker
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from cw.gh import LabelledIssue
    from cw.models import ClientConfig
    from cw.review_debt_filing._models import DebtCloseState
    from cw.review_debt_filing._tracker import DebtTracker

_log = logging.getLogger(__name__)

#: The only PR URL shape the sweep acts on. A full match also keeps a
#: flag-shaped value (``--...``) from ever reaching a ``gh`` argv.
_PR_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)/pull/[1-9][0-9]*"
)

#: Raw ``gh pr view --json state`` values mapped to the skip they cause.
_SKIP_FOR_STATE = {
    _GH_PR_STATE_OPEN: CloseOutcome.SKIPPED_PR_OPEN,
    _GH_PR_STATE_MERGED: CloseOutcome.SKIPPED_PR_MERGED,
}

#: The close comment (R11), before redaction and the agent marker.
_CLOSE_SENTENCE = (
    "Closing as not planned: the pull request that carried this review debt, "
    "{pr_url}, was closed without merging, so the deferred finding never "
    "reached the default branch. Reopen this issue if the work moves to "
    "another PR."
)


class _Sweep:
    """One sweep's shared budget, close count, PR-state cache and results."""

    def __init__(self, state: DebtCloseState) -> None:
        self.state = state
        self.budget = CallBudget(state.clock, DEBT_CLOSE_BUDGET_S)
        self.closes = 0
        self.stopped = False
        self.pr_cache: dict[str, str | None] = {}
        self.results: list[CloseResult] = []

    def timeout(self, cap: int) -> int | None:
        """The next call's timeout; a spent budget stops the whole sweep."""
        timeout = self.budget.timeout(cap)
        if timeout is None:
            self.stopped = True
        return timeout

    def done(self) -> bool:
        """True once the budget is spent or the close cap is reached."""
        if self.closes >= MAX_DEBT_CLOSES_PER_SWEEP:
            self.stopped = True
        return self.stopped


class _ClientRun:
    """One client's tracker plus the git-backed pieces, built lazily."""

    def __init__(self, cfg: ClientConfig, tracker: DebtTracker) -> None:
        self.cfg = cfg
        self.tracker = tracker
        self._mismatch: dict[str, bool] = {}
        self._ctx: CloseContext | None = None

    def mismatched(self, slug: str) -> bool:
        """True when *slug* is not this client's repo (fail-open on unknown)."""
        if slug not in self._mismatch:
            detail = repo_slug_mismatch(slug, _git_dir(self.cfg))
            self._mismatch[slug] = detail is not None
        return self._mismatch[slug]

    def context(self) -> CloseContext:
        """The redaction anchors, resolved once and only when a close is due."""
        if self._ctx is None:
            toplevel = git_toplevel(_git_dir(self.cfg))
            anchors = redaction_anchors(self.cfg, toplevel, None)
            self._ctx = CloseContext(
                client=self.cfg.name,
                home=resolve_home(os.environ),
                worktrees=tuple(anchors),
            )
        return self._ctx


def _comment(pr_url: str, key: str | None) -> str:
    """The R11 close comment; the key line only when the key parsed."""
    text = _CLOSE_SENTENCE.format(pr_url=pr_url)
    if key is None:
        return text
    return f"{text}\n\ncw review-debt key `{key[:KEY_DISPLAY_CHARS]}`"


def _pr_verdict(
    sweep: _Sweep, tracker: DebtTracker, urls: Sequence[str]
) -> CloseOutcome | None:
    """``CLOSED`` when every PR closed unmerged, else the skip; None if spent.

    Stops at the first PR that is not CLOSED. A MERGED PR is memoized for the
    life of the process; every state is cached for the rest of the sweep.
    """
    for url in urls:
        if url in sweep.state.merged_prs:
            return CloseOutcome.SKIPPED_PR_MERGED
        if url not in sweep.pr_cache:
            timeout = sweep.timeout(PR_VIEW_TIMEOUT_S)
            if timeout is None:
                return None
            sweep.pr_cache[url] = tracker.fetch_pr_state(url, timeout=timeout)
        pr_state = sweep.pr_cache[url]
        if pr_state == _GH_PR_STATE_MERGED:
            sweep.state.merged_prs.add(url)
        if pr_state != _GH_PR_STATE_CLOSED:
            return _SKIP_FOR_STATE.get(pr_state or "", CloseOutcome.SKIPPED_PR_UNKNOWN)
    return CloseOutcome.CLOSED


def _confirm_closed(sweep: _Sweep, tracker: DebtTracker, number: int) -> bool:
    """True when a re-read after a failed close finds the issue CLOSED."""
    timeout = sweep.timeout(ISSUE_STATE_TIMEOUT_S)
    if timeout is None:
        return False
    return tracker.fetch_issue_state(str(number), timeout=timeout) == (
        GH_ISSUE_STATE_CLOSED
    )


def _close_one(
    sweep: _Sweep, client: _ClientRun, issue: LabelledIssue, urls: Sequence[str]
) -> None:
    """Close *issue*, naming the newest PR; record and emit what happened."""
    timeout = sweep.timeout(CLOSE_ISSUE_TIMEOUT_S)
    if timeout is None:
        return
    pr_url = urls[-1]
    key = parse_debt_key(issue.body)
    queue_ticket = queue_ticket_ref(issue.body)
    ctx = client.context()
    text = redact_post_text(
        _comment(pr_url, key), home=ctx.home, worktrees=ctx.worktrees
    )
    sweep.closes += 1
    failure = client.tracker.close_issue(
        issue.number,
        agent_marked_body(text),
        reason=CLOSE_REASON_NOT_PLANNED,
        timeout=timeout,
    )
    match = _PR_URL_RE.fullmatch(pr_url)
    slug = match.group(1) if match is not None else ""
    url = f"https://github.com/{slug}/issues/{issue.number}"
    result = CloseResult(ctx.client, issue.number, tuple(urls), CloseOutcome.CLOSED)
    if failure is None:
        sweep.results.append(result)
        emit_closed(ctx, issue.number, url, pr_url, key, queue_ticket)
        return
    if _confirm_closed(sweep, client.tracker, issue.number):
        sweep.results.append(result)
        return
    sweep.results.append(result._replace(outcome=CloseOutcome.FAILED, failure=failure))
    memo = (ctx.client, issue.number, failure.value)
    if memo not in sweep.state.reported:
        sweep.state.reported.add(memo)
        emit_close_failed(ctx, issue.number, url, pr_url, key, queue_ticket, failure)


def _precheck(client: _ClientRun, urls: Sequence[str]) -> CloseOutcome | None:
    """The skip a malformed or foreign PR URL causes, before any PR read."""
    slugs: list[str] = []
    for url in urls:
        match = _PR_URL_RE.fullmatch(url)
        if match is None:
            return CloseOutcome.SKIPPED_PR_UNKNOWN
        slugs.append(match.group(1))
    if any(client.mismatched(slug) for slug in dict.fromkeys(slugs)):
        return CloseOutcome.SKIPPED_REPO_MISMATCH
    return None


def _sweep_issue(sweep: _Sweep, client: _ClientRun, issue: LabelledIssue) -> None:
    """Close *issue* when every PR it links closed unmerged; else record a skip."""
    urls = parse_pr_markers(issue.body)
    if not urls:
        return
    verdict = _precheck(client, urls)
    if verdict is None:
        verdict = _pr_verdict(sweep, client.tracker, urls)
    if verdict is None:
        return
    if verdict is CloseOutcome.CLOSED:
        _close_one(sweep, client, issue, urls)
        return
    sweep.results.append(
        CloseResult(client.cfg.name, issue.number, tuple(urls), verdict)
    )


def _sweep_client(sweep: _Sweep, cfg: ClientConfig, tracker: DebtTracker) -> None:
    """List *cfg*'s open debt issues once and sweep them in number order."""
    timeout = sweep.timeout(LIST_ISSUES_TIMEOUT_S)
    if timeout is None:
        return
    lookup = tracker.list_open_issues_by_label(REVIEW_DEBT_LABEL, timeout=timeout)
    if lookup.failure is not None:
        _log.warning(
            "review-debt close sweep: listing %s issues for %s failed (%s): %s",
            REVIEW_DEBT_LABEL,
            cfg.name,
            lookup.failure.value,
            lookup.detail,
        )
        return
    if lookup.truncated:
        _log.warning(
            "review-debt close sweep: skipped %s, its %s listing returned %d"
            " issues, the listing cap; nothing is closed until it shrinks",
            cfg.name,
            REVIEW_DEBT_LABEL,
            len(lookup.issues),
        )
        return
    client = _ClientRun(cfg, tracker)
    for issue in sorted(lookup.issues, key=lambda item: item.number):
        if sweep.done():
            return
        _sweep_issue(sweep, client, issue)


def sweep_debt_closes(
    clients: Mapping[str, ClientConfig],
    state: DebtCloseState,
    *,
    tracker_for: Callable[[ClientConfig], DebtTracker],
) -> list[CloseResult]:
    """Close every GitHub debt ticket whose linked PRs all closed unmerged.

    Clients whose tracker is not exactly ``github-issues`` (Linear included)
    get no ``gh`` call at all; ``auto_create_issues`` does not gate closing.
    *tracker_for* builds the per-client tracker (``GhClient(cwd=...)`` in
    production). Returns one result per issue that links a PR and was looked
    at; an issue left for the next sweep by the budget or the cap has none.
    """
    sweep = _Sweep(state)
    for cfg in clients.values():
        if sweep.done():
            break
        if tracker_mode(resolve_tracker(cfg.workspace_path)) is not TrackerMode.GITHUB:
            continue
        _sweep_client(sweep, cfg, tracker_for(cfg))
    return sweep.results
