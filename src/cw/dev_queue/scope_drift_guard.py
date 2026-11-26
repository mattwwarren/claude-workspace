"""Compare-and-set inputs for an adopted, confirm-backed repair grant.

A token describes what cw observed; it is not proof the requested paths fix
any finding and confers no authority. The operator still owns that decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

from cw._git import FULL_COMMIT_ID_RE
from cw.auto_dev_result import PLAN_SCOPE_DRIFT_BLOCKER_REASON
from cw.exceptions import ApproveGateError, CwError
from cw.gh import fetch_pr_view, github_pr_url, parse_pr_url
from cw.models import resolve_base_branch
from cw.models.enums import QueueItemStatus, Stage
from cw.models.review_engine_summary import is_non_deferrable_rule
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    LEDGER_STATUS_IN_PROGRESS,
    LEDGER_STATUS_STOPPED,
    STOP_CONFIRM_MUST_FIX,
    finding_identity,
    ledger_transaction,
    load_bound_round_verdict,
    load_scope_grant_ledger,
    settled_applies,
)
from cw.scope_grants import (
    exact_paths_error,
    review_policy_fingerprint,
)
from cw.stage_verification.posted_text import TRUNCATION_MARKER

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager
    from pathlib import Path

    from cw.models import ClientConfig, Session, TicketTask
    from cw.review_convergence import ConvergenceLedger
    from cw.review_findings import ReviewVerdict


PARK_CHANGED: Final = "park_changed"
_HEAD_UNAVAILABLE: Final = "head_unavailable"
_STALE_HEAD: Final = "stale_head"
_CODEX_DRIFT_HEADER = re.compile(
    r"the delivered diff touches ([0-9]{1,3}) file\(s\) beyond the plan's "
    r"Files Modified allowance \(allowed extra: [0-9]+\)\. extra_files:"
)
_CLAUDE_DRIFT_LINE = re.compile(
    r"Step 2\.5 gate 2: ([0-9]{1,3}) files? outside the plan's enumeration "
    r"\(allowed [0-9]+\)\. Extra: (.+)"
)


class ScopeGrantNotAppliedError(ApproveGateError):
    """A guarded refusal raised strictly before any queue or audit mutation."""

    def __init__(
        self, reason: Literal["stale_head", "head_unavailable", "park_changed"]
    ) -> None:
        self.reason = reason
        super().__init__(
            f"Scope grant not applied ({reason});"
            " re-evaluate the park and review evidence."
        )


class ScopeParkSnapshot(NamedTuple):
    token: str
    paths: list[str]
    policy: str
    base: str


type ScopeParkReader = Callable[[], ScopeParkSnapshot | None]


@dataclass(frozen=True)
class ScopeGrantExpectation:
    """Both pins and a live park reader are required for guarded mode."""

    head: str
    park: str
    lock_park: Callable[[TicketTask], AbstractContextManager[ScopeParkReader]]


def is_scope_drift_park(task: TicketTask) -> bool:
    return (
        task.status == QueueItemStatus.BLOCKED_ON_USER
        and task.stage == Stage.IMPL
        and task.blocked_reason == PLAN_SCOPE_DRIFT_BLOCKER_REASON
    )


def _engine_stop_matches(ledger: ConvergenceLedger, verdict: ReviewVerdict) -> bool:
    # Both recorders pass decided accepted findings to route_engine_confirm.
    # Its stop identities omit settled findings, so only the recorded subset
    # must match the verdict's park-class findings.
    accepted = (item.finding for item in verdict.accepted)
    park_class = {
        finding_identity(f.severity, f.file, f.summary, f.fingerprint)
        for f in accepted
        if f.severity == "MUST_FIX"
        or (f.severity == "SHOULD_FIX" and is_non_deferrable_rule(f.rule_id))
    }
    return set(ledger.stop_identities).issubset(park_class)


def _recorded_fix_matches(ledger: ConvergenceLedger, verdict: ReviewVerdict) -> bool:
    required = set(ledger.rounds[-1].repair_identities)
    settled = {entry.identity: entry for entry in ledger.settled}
    for accepted in verdict.accepted:
        finding = accepted.finding
        if finding.severity not in ("MUST_FIX", "SHOULD_FIX"):
            continue
        identity = finding_identity(
            finding.severity, finding.file, finding.summary, finding.fingerprint
        )
        entry = settled.get(identity)
        if identity in required and (
            entry is None
            or not settled_applies(
                entry, finding.severity, finding.fingerprint, finding.rule_id
            )
        ):
            return True
    return False


def _read_confirm(
    path: Path, task: TicketTask
) -> tuple[ConvergenceLedger, ReviewVerdict] | None:
    ledger = load_scope_grant_ledger(path)
    if ledger is None or ledger.ticket_id != task.ticket_id or not ledger.rounds:
        return None
    last = ledger.rounds[-1]
    engine_stop = (
        ledger.status == LEDGER_STATUS_STOPPED
        and ledger.stop_reason == STOP_CONFIRM_MUST_FIX
        and bool(ledger.stop_identities)
    )
    repair_in_progress = ledger.status == LEDGER_STATUS_IN_PROGRESS and last.new_fix > 0
    if (
        not (engine_stop or repair_in_progress)
        or last.kind != "confirm"
        or last.clean
        or last.reviewed_sha != task.adopted_head_sha
    ):
        return None
    verdict = load_bound_round_verdict(path, last.round, last.verdict_sha256)
    if verdict is None or verdict.reviewed_sha != last.reviewed_sha:
        return None
    committed_finding = (
        _engine_stop_matches(ledger, verdict)
        if engine_stop
        else _recorded_fix_matches(ledger, verdict)
    )
    if not committed_finding:
        return None
    # The ledger is the commit marker for the verdict, not a directory scan.
    # The display path is lockless. Guarded approval holds the transaction
    # through commit and compensation.
    if load_scope_grant_ledger(path) != ledger:
        return None
    return ledger, verdict


def _scope_blocker_details(task: TicketTask, session: Session) -> str | None:
    result = session.last_result
    if (
        result is None
        or result.get("ticket_id") != task.ticket_id
        or result.get("status") != "blocked"
    ):
        return None
    blocker = result.get("blocker")
    if not isinstance(blocker, dict) or blocker.get("reason") != task.blocked_reason:
        return None
    details = blocker.get("details")
    return details if isinstance(details, str) else None


def _exact_scope_paths(details: str) -> list[str]:
    """Accept producer text only when it preserves the count and exact paths."""
    header, *lines = details.removesuffix("\n").split("\n")
    match = _CODEX_DRIFT_HEADER.fullmatch(header)
    if match is not None and all(line.startswith("- ") for line in lines):
        paths = [line[2:] for line in lines]
    elif not lines and (match := _CLAUDE_DRIFT_LINE.fullmatch(header)) is not None:
        paths = [item.removeprefix(" ") for item in match[2].split(",")]
    else:
        return []
    # Count catches commas in Claude's comma-joined format. Even a lossless
    # Codex bullet cannot be granted with the CLI's comma-separated argument.
    if (
        int(match[1]) != len(paths)
        or exact_paths_error(paths) is not None
        or any(
            "," in path or "<redacted>" in path or TRUNCATION_MARKER in path
            for path in paths
        )
    ):
        return []
    return paths


def _park_ledger_path(
    task: TicketTask, session: Session | None, *, locked_path: Path | None = None
) -> Path | None:
    if (
        session is None
        or session.id != task.session_id
        or session.client != task.client
    ):
        return None
    # Dispatch records the tree on the session. An explicit queue path must
    # agree; the current worktree configuration may point at another checkout.
    tree = session.worktree_path
    if tree is None or not tree.is_absolute() or task.worktree_path not in (None, tree):
        return None
    ledger = tree / LEDGER_RELATIVE_PATH
    try:
        if ledger.parent.is_symlink() or (
            locked_path is not None
            and ledger.parent.resolve(strict=True) != locked_path.parent
        ):
            return None
    except OSError:
        return None
    return ledger


@contextmanager
def lock_scope_park(
    task: TicketTask,
    *,
    session: Session | None,
) -> Iterator[Path]:
    """Hold the same ledger lock the evidence writers use."""
    path = _park_ledger_path(task, session)
    if path is None:
        raise ScopeGrantNotAppliedError(PARK_CHANGED)
    with ExitStack() as stack:
        try:
            # A busy ledger should prompt a fresh read and retry without
            # tying up dispatch behind the global queue lock.
            locked = stack.enter_context(ledger_transaction(path, timeout_s=0))
        except (CwError, OSError, ValueError) as exc:
            raise ScopeGrantNotAppliedError(PARK_CHANGED) from exc
        yield locked


def scope_park_snapshot(
    task: TicketTask,
    client: ClientConfig | None,
    *,
    human_owner: bool,
    session: Session | None,
    locked_path: Path | None = None,
) -> ScopeParkSnapshot | None:
    """Read the same bounded episode for show and for the locked write path."""
    if (
        human_owner
        or not is_scope_drift_park(task)
        or client is None
        or task.adopted_pr_url is None
        or task.adopted_head_sha is None
        or session is None
    ):
        return None
    path = _park_ledger_path(task, session, locked_path=locked_path)
    if path is None:
        return None
    details = _scope_blocker_details(task, session)
    paths = _exact_scope_paths(details or "")
    if not paths:
        return None
    try:
        evidence = _read_confirm(path, task)
    except (CwError, OSError, ValueError):
        return None
    if evidence is None:
        return None
    ledger, verdict = evidence
    policy = review_policy_fingerprint(client.review)
    base = resolve_base_branch(client, task)
    payload = json.dumps(
        [
            "scope-park-v2",
            task.model_dump(mode="json", exclude={"pr_state": {"hydrated_at"}}),
            session.started_at.isoformat(),
            str(session.worktree_path),
            details,
            policy,
            base,
            ledger.model_dump(mode="json"),
            verdict.model_dump(mode="json"),
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return ScopeParkSnapshot(
        hashlib.sha256(payload.encode()).hexdigest(), paths, policy, base
    )


def check_adopted_pr_head(
    task: TicketTask, expected: ScopeGrantExpectation, repo: str | None
) -> tuple[str, str]:
    """Check the live head of the adopted open PR and return its head and base."""
    pr = parse_pr_url(task.adopted_pr_url or "")
    if (
        pr is None
        or repo is None
        or pr[0].lower() != repo.lower()
        or parse_pr_url(task.pr_url or "") != pr
    ):
        raise ScopeGrantNotAppliedError(PARK_CHANGED)
    if task.adopted_head_sha != expected.head:
        raise ScopeGrantNotAppliedError(_STALE_HEAD)
    try:
        live = fetch_pr_view(
            github_pr_url(pr[0], pr[1]),
            timeout=5,
            fields="headRefName,headRefOid,baseRefName,state,isCrossRepository",
        )
    except UnicodeDecodeError as exc:
        raise ScopeGrantNotAppliedError(_HEAD_UNAVAILABLE) from exc
    oid = None if live is None else live.get("headRefOid")
    if live is None or not isinstance(oid, str) or not FULL_COMMIT_ID_RE.fullmatch(oid):
        raise ScopeGrantNotAppliedError(_HEAD_UNAVAILABLE)
    if (
        live.get("state") != "OPEN"
        or live.get("headRefName") != task.branch
        or live.get("isCrossRepository") is not False
    ):
        raise ScopeGrantNotAppliedError(PARK_CHANGED)
    if oid != expected.head:
        raise ScopeGrantNotAppliedError(_STALE_HEAD)
    base = live.get("baseRefName")
    if not isinstance(base, str) or not base:
        raise ScopeGrantNotAppliedError(_HEAD_UNAVAILABLE)
    return oid, base


def check_scope_park(
    snapshot: ScopeParkSnapshot | None,
    expected: ScopeGrantExpectation,
    paths: list[str],
    policy: str,
    base: str,
) -> None:
    """Compare the current park and scope while the caller holds the queue lock."""
    if (
        snapshot is None
        or snapshot.token != expected.park
        or not set(paths).issubset(snapshot.paths)
        or policy != snapshot.policy
        or base != snapshot.base
    ):
        raise ScopeGrantNotAppliedError(PARK_CHANGED)
