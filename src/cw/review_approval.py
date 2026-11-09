"""The large-tier review-approval predicate, shared (#2773).

Finalize Step 4.0 refuses to ship a ``large``-tier ticket unless a review
approval is recorded and still valid for the head about to ship. That rule had
two evaluators: the bash fence in ``auto-dev-finalize.md`` and, for the repair
recipes, a Python port (#2771). #2773 adds a third consumer, the dispatch claim
gate, and makes all three call this one function:

- ``cw review check-approval`` (Step 4.0's fence) adapts ``cw-context.json`` and
  ``HEAD`` into :class:`ApprovalInputs` and prints :func:`render_verdict_line`.
- :mod:`cw.dispatch.review_approval_gate` reads the same inputs off the row.
- :mod:`cw.reconcile.review_recipes.repair_fence` reads them off the row too.

The rule: the recorded ``reviewed_sha`` must be an ancestor of the head, no
non-merge commit that is not already on ``origin/<default>`` landed after it,
and every merge after it must have its non-first parents on
``origin/<default>``. That accepts finalize's own pushed sync merge and rejects
any new work, a rewritten history, or a merge of any other ref.

Check order is the bash fence's, so a caller that supplies ``None`` for
``head_sha`` or ``default_branch`` gets the same token the fence printed:
tier, approver kind, head, reviewed-sha format, default branch, then git.

It also holds the review sign-off attestation verdict (#3040, RFC 0017 II1),
:func:`evaluate_review_attestation`, behind ``cw review attestation check``.
That rule is separate from, and stricter than, the approval rule above: the
attested head must equal HEAD, or every later commit must be tolerated by
:mod:`cw.review_attestation_tolerance` (merges only through the shared
review-move rule). It does not use ``_merge_parents_on_default`` /
``_new_non_merge_work``.

Lockless; subprocesses only through :func:`cw._git.run_git`, each with a
timeout. :func:`evaluate_review_approval` never raises. A git timeout, an
``OSError`` or a git error reads ``unreadable`` with ``transient=True``: the
caller may retry later (the claim gate defers) rather than treat it as a
verdict about the approval.
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING, NamedTuple, get_args

from pydantic import ValidationError

from cw._git import LOCAL_GIT_TIMEOUT_SECONDS, run_git
from cw.auto_dev_result import SCOPE_TIER_LARGE, SCOPE_TIER_SMALL
from cw.models.review_attestation import (
    REVIEW_ATTESTATION_SCHEMA_VERSION,
    ReviewAttestation,
)
from cw.models.tasks import ReviewApproverKind
from cw.review_attestation_tolerance import (
    MAX_TOLERANCE_SPAN_COMMITS,
    SpanTooLarge,
    first_untolerated_commit,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from cw.models import TicketTask
    from cw.models.client import RenumberPattern
    from cw.review_convergence import MergeTolerance

_APPROVER_KINDS = frozenset(get_args(ReviewApproverKind))
_REVIEWED_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# ``git merge-base --is-ancestor`` exits 1 for "not an ancestor"; any other
# non-zero exit is an error.
_NOT_ANCESTOR_RC = 1
# ``git rev-list --parents -n 1 <merge>`` prints ``<merge> <first> <other>...``.
_FIRST_OTHER_PARENT_FIELD = 2

APPROVAL_NOT_REQUIRED = "not_required"
APPROVAL_RECORDED = "recorded"
APPROVAL_MISSING = "missing"
APPROVAL_STALE = "stale"
APPROVAL_UNREADABLE = "unreadable"
APPROVAL_UNRESOLVED_TIER = "unresolved_tier"
#: Printed only by ``cw review check-approval`` (cw-context channel too old).
APPROVAL_CHANNEL_ABSENT = "channel_absent"
_APPROVAL_PASSES = frozenset({APPROVAL_NOT_REQUIRED, APPROVAL_RECORDED})

_NOT_REQUIRED_LINE = "REVIEW_APPROVAL_NOT_REQUIRED"
_RECORDED_LINE = "REVIEW_APPROVAL_RECORDED"
_PARK_PREFIX = "REVIEW_APPROVAL_PARK: "


class ApprovalInputs(NamedTuple):
    """What the predicate judges, each field exactly as the source holds it."""

    tier: str | None
    approver_kind: str | None
    approved_sha: str | None
    default_branch: str | None


class ApprovalVerdict(NamedTuple):
    """The predicate's answer.

    ``transient`` is True only for an ``unreadable`` caused by git failing to
    run, timing out or erroring: a later attempt may read differently. An
    absent branch, ref or head is a state, not a transient fault.
    """

    token: str
    detail: str
    transient: bool = False

    @property
    def passed(self) -> bool:
        """True for ``not_required`` and ``recorded``."""
        return self.token in _APPROVAL_PASSES


class CommitLookup(NamedTuple):
    """One ``rev-parse --verify <rev>^{commit}``: the sha, and why it is None.

    ``exec_failed`` is True when git could not run or timed out, as opposed to
    running and not finding the commit.
    """

    sha: str | None
    exec_failed: bool


class _Step(NamedTuple):
    token: str
    detail: str
    transient: bool = False


def resolve_row_scope_tier(task: TicketTask) -> str | None:
    """The row's scope tier, mirroring ``dispatch.routing._resolve_scope_tier``.

    ``large`` if either the operator ``scope_hint`` or the stamped
    ``computed_scope_tier`` is ``large``; else the stamped tier; else the hint.
    Only an exact ``small`` or ``large`` is accepted: anything else is None.
    """
    stamped = task.computed_scope_tier
    if SCOPE_TIER_LARGE in (task.scope_hint, stamped):
        return SCOPE_TIER_LARGE
    tier = stamped if stamped is not None else task.scope_hint
    return tier if tier in {SCOPE_TIER_SMALL, SCOPE_TIER_LARGE} else None


def approval_inputs_from_task(
    task: TicketTask, default_branch: str | None
) -> ApprovalInputs:
    """The predicate inputs read straight off *task* (a plain row read)."""
    kind = task.review_approver_kind
    return ApprovalInputs(
        tier=resolve_row_scope_tier(task),
        approver_kind=None if kind is None else str(kind),
        approved_sha=task.review_approved_sha,
        default_branch=default_branch,
    )


def _run(
    repo: Path, timeout: float, *argv: str
) -> subprocess.CompletedProcess[str] | None:
    """Run one read-only git call; None when it could not run or timed out."""
    try:
        return run_git(
            argv, cwd=repo, check=False, capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def resolve_commit(
    repo: Path, rev: str, *, timeout: float = LOCAL_GIT_TIMEOUT_SECONDS
) -> CommitLookup:
    """Resolve *rev* to a full commit sha in *repo* (``sha`` None if it cannot)."""
    result = _run(
        repo, timeout, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"
    )
    if result is None:
        return CommitLookup(None, True)
    if result.returncode != 0:
        return CommitLookup(None, False)
    return CommitLookup(result.stdout.strip() or None, False)


def _ancestry(
    repo: Path, timeout: float, ancestor: str, descendant: str
) -> str | _Step | None:
    """None if *ancestor* is an ancestor; else ``stale`` or an unreadable step."""
    result = _run(repo, timeout, "merge-base", "--is-ancestor", ancestor, descendant)
    if result is None:
        return _Step(APPROVAL_UNREADABLE, "git merge-base could not run", True)
    if result.returncode == 0:
        return None
    if result.returncode == _NOT_ANCESTOR_RC:
        return APPROVAL_STALE
    return _Step(APPROVAL_UNREADABLE, "git merge-base failed", True)


def _tier_kind_step(inputs: ApprovalInputs) -> _Step | None:
    """The tier and approver-kind checks (no git, no HEAD); None to continue."""
    if inputs.tier == SCOPE_TIER_SMALL:
        return _Step(APPROVAL_NOT_REQUIRED, "small tier: no review approval needed")
    if inputs.tier != SCOPE_TIER_LARGE:
        return _Step(
            APPROVAL_UNRESOLVED_TIER, "scope.tier is not exactly small or large"
        )
    if inputs.approver_kind not in _APPROVER_KINDS:
        return _Step(APPROVAL_MISSING, "no review approval is recorded on the row")
    return None


def _format_step(inputs: ApprovalInputs, head_sha: str) -> _Step | None:
    """The reviewed-sha format and default-branch checks; None to continue."""
    if not _REVIEWED_SHA_RE.match(inputs.approved_sha or ""):
        return _Step(
            APPROVAL_STALE,
            f"the recorded approval carries no usable reviewed head (head {head_sha})",
        )
    if not inputs.default_branch:
        return _Step(
            APPROVAL_UNREADABLE,
            "origin_main_branch is missing or invalid (no default branch name)",
        )
    return None


def _merge_parents_on_default(
    repo: Path,
    timeout: float,
    full_sha: str,
    head_sha: str,
    main_sha: str,
    default: str,
) -> _Step | None:
    """Every non-first parent of every merge after the approval is on main."""
    merges = _run(repo, timeout, "rev-list", "--merges", f"{full_sha}..{head_sha}")
    if merges is None or merges.returncode != 0:
        return _Step(APPROVAL_UNREADABLE, "git rev-list --merges failed", True)
    for merge_sha in merges.stdout.split():
        parents = _run(repo, timeout, "rev-list", "--parents", "-n", "1", merge_sha)
        if parents is None or parents.returncode != 0:
            return _Step(
                APPROVAL_UNREADABLE,
                f"cannot read the parents of merge {merge_sha}",
                True,
            )
        for parent in parents.stdout.split()[_FIRST_OTHER_PARENT_FIELD:]:
            outcome = _ancestry(repo, timeout, parent, main_sha)
            if outcome is None:
                continue
            if isinstance(outcome, _Step):
                return outcome
            return _Step(
                outcome,
                f"merge {merge_sha} brought in {parent}, which is not on"
                f" origin/{default}",
            )
    return None


def _new_non_merge_work(
    repo: Path, timeout: float, full_sha: str, head_sha: str, main_sha: str
) -> _Step | None:
    """Non-merge commits after the approval that are not on main: stale."""
    new_work = _run(
        repo,
        timeout,
        "rev-list",
        "--no-merges",
        f"{full_sha}..{head_sha}",
        f"^{main_sha}",
    )
    if new_work is None or new_work.returncode != 0:
        return _Step(
            APPROVAL_UNREADABLE, f"git rev-list failed for {full_sha}..{head_sha}", True
        )
    if new_work.stdout.strip():
        landed = " ".join(new_work.stdout.split())
        return _Step(
            APPROVAL_STALE, f"non-merge commit(s) landed after the approval: {landed}"
        )
    return None


class _Resolved(NamedTuple):
    """The resolved refs for the git half, or the step that stopped the walk."""

    main_sha: str | None = None
    full_sha: str | None = None
    stop: _Step | None = None


def _resolve_refs(
    inputs: ApprovalInputs, repo: Path, head_sha: str, timeout: float
) -> _Resolved:
    """Resolve ``origin/<default>`` and the reviewed sha; ``stop`` if either fails."""
    default = inputs.default_branch or ""
    sha = inputs.approved_sha or ""
    main = resolve_commit(repo, f"refs/remotes/origin/{default}", timeout=timeout)
    if main.sha is None:
        return _Resolved(
            stop=_Step(
                APPROVAL_UNREADABLE,
                f"origin/{default} is not resolvable in {repo}",
                main.exec_failed,
            )
        )
    full = resolve_commit(repo, sha, timeout=timeout)
    if full.exec_failed:
        stop = _Step(APPROVAL_UNREADABLE, f"git rev-parse {sha} could not run", True)
        return _Resolved(stop=stop)
    if full.sha is None or not full.sha.startswith(sha):
        stop = _Step(
            APPROVAL_STALE,
            f"reviewed head {sha} is not a commit in this repository (head {head_sha})",
        )
        return _Resolved(stop=stop)
    return _Resolved(main_sha=main.sha, full_sha=full.sha)


def _ancestry_step(
    inputs: ApprovalInputs, repo: Path, head_sha: str, timeout: float
) -> _Step:
    """The git half of Step 4.0, once the inputs are well-formed."""
    resolved = _resolve_refs(inputs, repo, head_sha, timeout)
    if resolved.stop is not None:
        return resolved.stop
    full_sha, main_sha = resolved.full_sha or "", resolved.main_sha or ""
    outcome = _ancestry(repo, timeout, full_sha, head_sha)
    if isinstance(outcome, _Step):
        return outcome
    if outcome is not None:
        return _Step(
            outcome, f"reviewed head {full_sha} is not an ancestor of head {head_sha}"
        )
    problem = _new_non_merge_work(repo, timeout, full_sha, head_sha, main_sha)
    if problem is None:
        problem = _merge_parents_on_default(
            repo, timeout, full_sha, head_sha, main_sha, inputs.default_branch or ""
        )
    if problem is not None:
        return problem
    return _Step(
        APPROVAL_RECORDED, f"review approval of {full_sha} still covers {head_sha}"
    )


def evaluate_review_approval(
    inputs: ApprovalInputs,
    *,
    repo: Path,
    head_sha: str | None,
    timeout: float = LOCAL_GIT_TIMEOUT_SECONDS,
) -> ApprovalVerdict:
    """Step 4.0 for *head_sha*: the verdict for *inputs*, judged in *repo*.

    *head_sha* None means HEAD could not be read (``unreadable``, not
    transient); ``inputs.default_branch`` None or empty is ``unreadable`` too.
    *timeout* bounds every git call. Never raises.
    """
    early = _tier_kind_step(inputs)
    if early is not None:
        return ApprovalVerdict(*early)
    if head_sha is None:
        return ApprovalVerdict(APPROVAL_UNREADABLE, f"HEAD is not readable in {repo}")
    early = _format_step(inputs, head_sha)
    if early is not None:
        return ApprovalVerdict(*early)
    return ApprovalVerdict(*_ancestry_step(inputs, repo, head_sha, timeout))


def render_verdict_line(verdict: ApprovalVerdict) -> str:
    """The one-line Step 4.0 output: NOT_REQUIRED, RECORDED or a PARK line."""
    if verdict.token == APPROVAL_NOT_REQUIRED:
        return _NOT_REQUIRED_LINE
    if verdict.token == APPROVAL_RECORDED:
        return _RECORDED_LINE
    return f"{_PARK_PREFIX}{verdict.token} — {verdict.detail}"


# ---------------------------------------------------------------------------
# The review sign-off attestation verdict (#3040, RFC 0017 II1)
# ---------------------------------------------------------------------------

ATTESTATION_VALID = "valid"
INVALID_MISSING = "invalid:missing"
INVALID_UNREADABLE = "invalid:unreadable"
INVALID_SCHEMA_VERSION = "invalid:schema_version"
INVALID_MALFORMED = "invalid:malformed"
INVALID_WRONG_TICKET = "invalid:wrong_ticket"
INVALID_WRONG_CLIENT = "invalid:wrong_client"
INVALID_FAILED_VERDICT = "invalid:verdict_not_pass"
INVALID_HEAD_UNREADABLE = "invalid:head_unreadable"
INVALID_NOT_ANCESTOR = "invalid:not_ancestor"
INVALID_SPAN_TOO_LARGE = "invalid:span_too_large"
INVALID_UNTOLERATED_COMMIT = "invalid:untolerated_commit"
_ATTESTED_VERDICT = "PASS"


class AttestationVerdict(NamedTuple):
    """``cw review attestation check``'s answer; the ``--json`` field set."""

    token: str
    detail: str
    offending_commit: str | None = None
    offending_path: str | None = None
    attestation_head_sha: str | None = None
    head_sha: str | None = None
    engine: str | None = None
    reviewer_backend: str | None = None
    ticket_id: str | None = None
    client: str | None = None

    @property
    def valid(self) -> bool:
        """True only for the ``valid`` token."""
        return self.token == ATTESTATION_VALID


def _load_attestation(raw: object) -> ReviewAttestation | AttestationVerdict:
    """The parsed attestation claim, or the first load-level invalid verdict."""
    if raw is None:
        return AttestationVerdict(INVALID_MISSING, "no review_attestation recorded")
    if not isinstance(raw, dict):
        return AttestationVerdict(
            INVALID_UNREADABLE, "review_attestation is not an object"
        )
    if raw.get("schema_version") != REVIEW_ATTESTATION_SCHEMA_VERSION:
        detail = (
            f"attestation schema_version {raw.get('schema_version')!r}; "
            f"expected {REVIEW_ATTESTATION_SCHEMA_VERSION}"
        )
        return AttestationVerdict(INVALID_SCHEMA_VERSION, detail)
    try:
        return ReviewAttestation.model_validate(raw)
    except ValidationError as exc:
        return AttestationVerdict(
            INVALID_MALFORMED, f"{exc.error_count()} validation error(s)"
        )


def _identity_mismatch(
    att: ReviewAttestation,
    expected_ticket: str | None,
    expected_client: str | None,
) -> tuple[str, str] | None:
    """``(token, detail)`` when the claim is for another ticket, client or verdict."""
    if expected_ticket is None or att.ticket_id != expected_ticket:
        return INVALID_WRONG_TICKET, (
            f"attestation is for ticket {att.ticket_id!r}, expected {expected_ticket!r}"
        )
    if expected_client is None or att.client != expected_client:
        return INVALID_WRONG_CLIENT, (
            f"attestation is for client {att.client!r}, expected {expected_client!r}"
        )
    if att.verdict != _ATTESTED_VERDICT:
        return INVALID_FAILED_VERDICT, f"attestation verdict is {att.verdict!r}"
    return None


def _judge_head(
    att: ReviewAttestation,
    *,
    repo: Path,
    head_sha: str,
    merge_tolerance_for: Callable[[str], MergeTolerance | None],
    renumber_patterns: Sequence[RenumberPattern],
) -> tuple[str, str, str | None, str | None]:
    """``(token, detail, offending_commit, offending_path)`` for a readable HEAD."""
    attested = att.head_sha
    if attested == head_sha:
        return ATTESTATION_VALID, f"HEAD is the attested head {attested}", None, None
    ancestry = _ancestry(repo, LOCAL_GIT_TIMEOUT_SECONDS, attested, head_sha)
    if ancestry is not None:
        detail = f"attested head {attested} is not an ancestor of HEAD {head_sha}"
        return INVALID_NOT_ANCESTOR, detail, None, None
    found = first_untolerated_commit(
        repo, attested, head_sha, merge_tolerance_for(attested), renumber_patterns
    )
    if isinstance(found, SpanTooLarge):
        detail = (
            f"{found.commits} first-parent commits after the attested head; "
            f"the cap is {MAX_TOLERANCE_SPAN_COMMITS}"
        )
        return INVALID_SPAN_TOO_LARGE, detail, None, None
    if found is not None:
        detail = f"commit {found.commit} is not tolerated (reason {found.reason})"
        return INVALID_UNTOLERATED_COMMIT, detail, found.commit, found.path
    return ATTESTATION_VALID, f"every commit after {attested} is tolerated", None, None


def evaluate_review_attestation(
    raw: object,
    *,
    expected_ticket: str | None,
    expected_client: str | None,
    repo: Path,
    head_sha: str | None,
    merge_tolerance_for: Callable[[str], MergeTolerance | None],
    renumber_patterns: Sequence[RenumberPattern],
) -> AttestationVerdict:
    """Whether the attestation claim *raw* still covers *head_sha* in *repo*.

    *raw* is ``queue_metadata.review_attestation`` from a worker-writable
    cw-context, so it is a claim: its ticket and client are cross-checked
    against *expected_ticket* / *expected_client*, and the head through git.
    Check order: missing, unreadable, schema version, model validation,
    ticket, client, verdict, HEAD (``None`` is unreadable), equal to HEAD,
    ancestry, then ``merge_tolerance_for(attested head)`` (called at most once,
    only here) and :func:`first_untolerated_commit`. Fails closed; never raises.
    """
    loaded = _load_attestation(raw)
    if isinstance(loaded, AttestationVerdict):
        return loaded
    claim = AttestationVerdict(
        "",
        "",
        attestation_head_sha=loaded.head_sha,
        engine=loaded.engine,
        reviewer_backend=loaded.reviewer_backend,
        ticket_id=loaded.ticket_id,
        client=loaded.client,
    )
    mismatch = _identity_mismatch(loaded, expected_ticket, expected_client)
    if mismatch is not None:
        return claim._replace(token=mismatch[0], detail=mismatch[1], head_sha=head_sha)
    if head_sha is None:
        return claim._replace(
            token=INVALID_HEAD_UNREADABLE, detail=f"HEAD is not readable in {repo}"
        )
    token, detail, commit, path = _judge_head(
        loaded,
        repo=repo,
        head_sha=head_sha,
        merge_tolerance_for=merge_tolerance_for,
        renumber_patterns=renumber_patterns,
    )
    return claim._replace(
        token=token,
        detail=detail,
        offending_commit=commit,
        offending_path=path,
        head_sha=head_sha,
    )
