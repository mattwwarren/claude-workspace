"""Per-pass context loading and the review-pass assembly entry point.

The orchestration half of the prompt-context concern: the ticket/plan and live
comment-thread reads every pass performs, the adjudication-ledger merge that
persists what a pass learned, and :func:`_prepare_review_pass`, which drives
every other ``_context`` submodule to produce one pass's
:class:`_ReviewPassInputs`.

One of those reads is deliberately not verbatim: :func:`_load_operator_comments`
drops every comment the pipeline itself wrote (#2210, widened by #2213). A
prior round's verdict must not re-enter the pipeline's own prompt as evidence
— cross-round memory flows through the structured adjudication ledger instead.
The operator's binding constraints (#2964) therefore do not ride the comment
thread at all: :func:`_prepare_review_pass` renders them from the row's
``operator_constraints`` (``cw review constrain``) on every tracker.

The other deliberate exception is the repository's own review configuration
(``review-extras.md``, ``review-policy.md``, the sensitive-files manifests):
:func:`_prepare_review_pass` resolves it once from the default branch, not the
reviewed branch's working tree, so a branch cannot weaken the review of itself
(#3038); see :mod:`~cw.codex_review._context._default_branch_config`.
"""

from __future__ import annotations

import functools
import json
import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple

from cw.codex_review._capability import _probe_filesystem_capability
from cw.codex_review._context._default_branch_config import (
    REVIEW_EXTRAS_PATH,
    resolve_review_config_source,
    review_config_status,
)
from cw.codex_review._context._file_selection import _categorize_changed_files
from cw.codex_review._context._prompt_render import _render_role_prompts
from cw.codex_review._context._repo_config import (
    _load_claude_md_quality_gates,
    _load_review_policy,
    _load_ruff_lint_config,
    _render_lint_grounding_block,
)
from cw.codex_review._context._roster import resolve_roster
from cw.codex_review._context._sensitive_files import _load_sensitive_hits
from cw.codex_review._context._util import _load_optional_text
from cw.codex_review._diff import (
    _capture_branch_context,
    _capture_delta_diff,
    _capture_diff,
    _capture_head_sha,
    _capture_tree_diff,
)
from cw.gh import (
    FETCH_COMMENTS_TIMEOUT,
    fetch_issue_comments,
    is_agent_authored,
)
from cw.local_runner import TICKET_BODY_KEY, TICKET_TITLE_KEY, resolve_tier
from cw.models import CONTEXT_JSON_RELATIVE_PATH, HOOK_CONTEXT_RELATIVE_PATH
from cw.models.enums import DEFAULT_REVIEWER_SANDBOX_MODE, SandboxMode
from cw.models.operator_constraint import (
    render_constraint_entries,
    render_decided_entries,
    split_constraints,
)
from cw.review_adjudication import parse_voided_findings_block
from cw.review_finding_dispositions import (
    REVERSED,
    disposition_event_payload,
    disposition_event_type,
    log_refused_dispositions,
    merge_finding_dispositions,
    parse_finding_disposition_block,
)
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from cw.codex_review._capability import _CodexFilesystemCapability
    from cw.codex_review._context._roster import EngineRoster
    from cw.codex_runner import CodexRunner
    from cw.executor_diagnostics import ExecutorName
    from cw.models import TicketTask
    from cw.review_adjudication import VoidedFinding
    from cw.review_convergence import ResolutionSpec
    from cw.review_finding_dispositions import FindingDisposition
    from cw.review_findings import (
        AgentSpecStatus,
        CapturedDiff,
        Finding,
        ReviewConfigStatus,
    )
    from cw.review_markers import RefusedDisposition

    #: ``render_prompts(diff, changed_files)``: every role's prompt (#2864).
    _PromptRenderer = Callable[[CapturedDiff, Sequence[str]], dict[str, str]]

_log = logging.getLogger(__name__)

#: ``_ReviewPassInputs.branch_context`` of every pass that is not a resolution round.
_NO_BRANCH_CONTEXT: Mapping[str, str] = MappingProxyType({})


def _load_ticket_context(worktree: Path) -> tuple[str | None, str | None]:
    """Return ``(plan_text, ticket_text)`` from ``.cw/plan.md`` / ``.cw/context.json``.

    Reuses ``local_runner.build_task_message``'s read pattern (no tracker/network
    call): the approved plan text and the ticket's title+body already
    materialized in the worktree at Stage 1. The ticket text is read from the
    ``ticket_title``/``ticket_body`` keys that ``auto-dev-intake.md`` Step 0d
    writes.
    """
    plan_text = _load_optional_text(worktree / ".cw" / "plan.md")
    ticket_text: str | None = None
    ctx_raw = _load_optional_text(worktree / CONTEXT_JSON_RELATIVE_PATH)
    if ctx_raw is not None:
        try:
            data = json.loads(ctx_raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            title = str(data.get(TICKET_TITLE_KEY) or "")
            body = str(data.get(TICKET_BODY_KEY) or "")
            combined = "\n\n".join(part for part in (title, body) if part)
            ticket_text = combined or None
    return plan_text, ticket_text


class _CommentsNotProvided:
    """Sentinel default for a loader's ``comments`` param: fetch it fresh.

    Distinct from ``None`` (which means "fetched, and there was nothing" —
    unresolvable tracker or an empty/failed fetch) so a caller that already
    fetched can hand that exact outcome, including ``None``, straight through
    without triggering a second, redundant fetch (#1814 SHOULD_FIX).
    """


_COMMENTS_NOT_PROVIDED = _CommentsNotProvided()


def _fetch_ticket_comments(
    worktree: Path, ticket_id: str
) -> list[dict[str, object]] | None:
    """Fetch the ticket's raw comment list once, shared by both readers below.

    :func:`_load_operator_comments` (#1730) and :func:`_load_voided_findings`
    (#1814) each need the same live comment thread every review pass. Before
    this helper existed they fetched it independently, so `_prepare_review_pass`
    shelled out to ``gh issue view`` twice per pass for identical data. Extracted
    so a caller that needs both can fetch once and pass the result to each.

    Scoped to ``github-issues`` trackers because that is the only tracker with
    a fetch op reachable from this process (``linear`` reads go through MCP
    tools only a Claude session holds). Returns ``None`` on an unresolvable
    tracker or a gh failure — never raises.

    The loaders stay pure: the "comments were not read" signal for a
    non-GitHub (or unset) tracker is emitted once per session at run level
    (``review.operator_context_undelivered``, from
    ``cw.codex_review_signals`` via ``cw.codex_background``, #3055). That event
    covers first entry; ``requeue.review_delivery_degraded`` covers re-entry.
    """
    if resolve_tracker(worktree) != TRACKER_GITHUB_ISSUES:
        return None
    return fetch_issue_comments(ticket_id, timeout=FETCH_COMMENTS_TIMEOUT, cwd=worktree)


# Built by concatenation rather than an f-string: the `{1,3}` quantifier would
# need brace-doubling, which is exactly the kind of quiet breakage this regex
# must not have. The span starts at the settle heading's own line and runs,
# lazily, to whichever comes first: the next `#`-to-`###` heading (a `####`
# line is NOT one), the provenance marker line, or the end of the body. So a
# section followed by another heading loses only itself, a section that is last
# stops before the marker line, and text after the section is never swallowed.
# The payload's own lines cannot terminate the span early -- `json.dumps(...,
# indent=2)` puts every string value on one line, so no payload line begins
# with `#`, and the label lines begin with `**`.
#
def _load_operator_comments(
    worktree: Path,
    ticket_id: str,
    *,
    comments: list[dict[str, object]] | None | _CommentsNotProvided = (
        _COMMENTS_NOT_PROVIDED
    ),
) -> str | None:
    """Render the ticket's comment thread as text, or None (#1730).

    The codex review backend previously saw only ``.cw/context.json``'s
    title/body, so an operator send-back comment posted after Stage 0 never
    reached a codex reviewer at all. This mirrors the "Comments are live, not
    cached" convention ``auto-dev-plan.md``/``auto-dev-impl.md`` already
    establish for the Claude-native path: fetched fresh on every review pass,
    never read from the cached ``comments`` array.

    ``comments`` defaults to fetching fresh via :func:`_fetch_ticket_comments`;
    pass an already-fetched list (or ``None``) to skip a redundant fetch when a
    caller (``_prepare_review_pass``) already has it for another reader.

    Degrades to ``None`` — never raises — on an unresolvable tracker, a gh
    failure, or an empty thread: a review without comments is strictly better
    than no review, and the requeue-side ``requeue.review_delivery_degraded``
    event (#1730) is what makes an undeliverable pairing operator-visible.

    One exception to "verbatim" (#2210, widened by #2213): a comment carrying
    :data:`~cw.gh.AGENT_COMMENT_MARKER` — i.e. one the pipeline itself posted —
    is dropped entirely. #2210 elided only the ``### Settle a finding``
    payload, leaving the rest of every prior "Codex Review Verdict" comment in
    the prompt: its superseded scope assessment, its stale out-of-scope file
    list and its MUST_FIX titles. On a ticket many rounds deep that was tens
    of kilobytes of the reviewer's own earlier output, rendered under a header
    the prompt tells the model to treat as adjudication input. The observed
    result (#2213) was byte-identical verdicts across runs and MUST_FIX
    findings citing files absent from the diff — the model was quoting the
    thread, not the code. Cross-round memory is the structured ledger's job
    (``adjudicated_findings``, the voided-findings record); the raw thread is
    an unfiltered second channel that swamps it. Exclusion is
    provenance-keyed, never content-keyed: an operator's own comment, a pasted
    payload included, stays visible by design, and an unmarked comment is
    treated as operator-authored unless its own text self-discloses non-operator
    authorship (``is_agent_authored``, #2617).
    """
    if isinstance(comments, _CommentsNotProvided):
        comments = _fetch_ticket_comments(worktree, ticket_id)
    if not comments:
        return None
    rendered: list[str] = []
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str) or not body.strip():
            continue
        if is_agent_authored(body):
            continue
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        created = comment.get("createdAt")
        header = f"### {login or 'unknown'}"
        if isinstance(created, str) and created:
            header += f" ({created})"
        rendered.append(f"{header}\n{body}")
    return "\n\n".join(rendered) or None


def _load_voided_findings(
    worktree: Path,
    ticket_id: str,
    *,
    comments: list[dict[str, object]] | None | _CommentsNotProvided = (
        _COMMENTS_NOT_PROVIDED
    ),
) -> list[VoidedFinding]:
    """Parse the operator-voided findings recorded on the ticket (#1814).

    The codex backend re-derives its findings mechanically every pass, so an
    operator's plain-English rejection of one has no effect here unless it was
    given a structured anchor. That anchor is a JSON sentinel inside a ticket
    comment (``review_adjudication.parse_voided_findings_block``), which this
    reads back on every Stage-3 entry.

    ``comments`` defaults to fetching fresh via :func:`_fetch_ticket_comments`;
    pass an already-fetched list (or ``None``) to skip a redundant fetch when a
    caller (``_prepare_review_pass``) already has it for another reader — same
    shared-fetch shape as :func:`_load_operator_comments` above.

    Same degrade-never-raise contract as :func:`_load_operator_comments`, plus
    one reason specific to this record: it lives on the tracker thread rather
    than in ``.cw/`` precisely because ``dispatch/gating/context_json.py`` deletes
    ``.cw/context.json`` on the rescued-respawn path this ticket exists to
    survive. Degrading to ``[]`` means a void goes unhonored and the finding
    re-appears — visible and correctable, unlike a silent false suppression.
    """
    if isinstance(comments, _CommentsNotProvided):
        comments = _fetch_ticket_comments(worktree, ticket_id)
    if not comments:
        return []
    bodies = [
        body for comment in comments if isinstance(body := comment.get("body"), str)
    ]
    return parse_voided_findings_block(bodies)


def _load_finding_dispositions(
    worktree: Path,
    ticket_id: str,
    *,
    comments: list[dict[str, object]] | None | _CommentsNotProvided = (
        _COMMENTS_NOT_PROVIDED
    ),
) -> tuple[dict[str, FindingDisposition], list[RefusedDisposition]]:
    """Parse the operator's cross-round finding adjudications off the ticket.

    Sibling of :func:`_load_voided_findings` in every respect that matters —
    same tracker gate, same shared-fetch parameter, same degrade-to-empty-and-
    never-raise contract — but reading the ``REVIEW-FINDING-DISPOSITIONS``
    marker (#1838) rather than ``VOIDED-REVIEW-FINDINGS`` (#1814).

    Kept a separate loader rather than folded into the voided one because the
    two records are separate contracts with separate identities and lifetimes:
    a void lapses when the code moves, an adjudication does not. Merging the
    reads would couple two grammars that must be free to diverge.

    Returns ``(enforceable, refused)`` for only what the CURRENT comment thread
    carries (#2210 round 3): a record that fails provenance is in ``refused``
    and never in ``enforceable``, so it cannot reach the ledger. The durable
    half of the ledger lives on ``TicketTask.finding_dispositions``, and
    :func:`_prepare_review_pass` merges the two — so a degraded fetch costs the
    pass nothing it already knew.
    """
    if isinstance(comments, _CommentsNotProvided):
        comments = _fetch_ticket_comments(worktree, ticket_id)
    if not comments:
        return {}, []
    bodies = [
        body for comment in comments if isinstance(body := comment.get("body"), str)
    ]
    return parse_finding_disposition_block(bodies)


def _load_pending_operator_comment_marker(worktree: Path) -> bool:
    """Read ``queue_metadata.pending_operator_comment`` from the hook context.

    The source is ``<worktree>/.claude/cw-context.json``
    (:data:`HOOK_CONTEXT_RELATIVE_PATH`) — the *dispatch/session* context
    ``spawn.py``'s ``_write_hook_context`` materializes at spawn time — NOT the
    sibling ``.cw/context.json`` this function's first cut read (#1730). Those
    are different layers: ``.cw/context.json`` is Stage 0's *ticket* context and
    is deleted outright by ``dispatch/gating/context_json.py``'s stale-context
    invalidation (#1046) on a rescued respawn, so ``queue_metadata`` cannot live
    there. Both ends now share the one constant so the read cannot drift off
    the write again; the reader-vs-writer path agreement is pinned by
    ``TestLoadPendingOperatorCommentMarker``, which drives the real writer.

    The queue-side field is cleared by ``dispatch/claim.py`` once a REVIEW-stage
    spawn has consumed it. True means this REVIEW re-entry followed a regress
    that may carry a pending operator send-back -- render the elevated-priority
    banner. Fail-safe to False on a missing/malformed/non-object file.
    """
    ctx_raw = _load_optional_text(worktree / HOOK_CONTEXT_RELATIVE_PATH)
    if ctx_raw is None:
        return False
    try:
        data = json.loads(ctx_raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    qm = data.get("queue_metadata")
    return bool(isinstance(qm, dict) and qm.get("pending_operator_comment"))


class _ReviewPassInputs(NamedTuple):
    """Assembled inputs for one per-role review pass (#1392).

    The output of :func:`_prepare_review_pass` — everything ``run_codex_roles``
    needs (selected ``roles`` and their materialized ``prompts_by_role``) plus
    the captured ``diff`` and ``reviewed_sha`` that
    ``synthesize_codex_review_result`` consumes. Extracted so the fix loop can
    re-run a fresh review pass each cycle without re-inlining ``run_review``'s
    input-assembly body.

    ``capability`` (#1709) is the probed filesystem-capability verdict the
    prompts were built against — returned so the caller can record it on the
    verdict rather than re-deriving (or, worse, re-probing) it.

    ``agent_spec_status`` (#1773) is the per-role agent-spec resolution record,
    in ``roles`` order — same shape and same reason as ``capability``: the
    prompts were built from it, so the caller records it on the verdict rather
    than re-reading the filesystem to reconstruct where each spec came from.

    ``voided_findings`` (#1814) is the operator-settled REJECT record fetched
    off the ticket thread. Unlike the three above it never reaches a prompt —
    it is consumed after synthesis, by ``apply_voided_suppression``. It rides
    here anyway so the fetch happens once per pass, at the one place both
    ``run_review`` and the fix loop's per-cycle re-review already share.

    ``finding_dispositions`` (#1838) is the cross-round adjudication ledger:
    the durable ``TicketTask.finding_dispositions`` merged with whatever the
    live comment thread's ``REVIEW-FINDING-DISPOSITIONS`` marker adds. Unlike
    ``voided_findings`` it reaches BOTH ends — the reviewer prompt (so the
    model is told not to re-raise) and post-synthesis suppression (so it does
    not matter if the model ignores that). Merged here, at the one place
    ``run_review`` and the fix loop's per-cycle re-review already share, so
    both paths see the same ledger.

    ``refused_dispositions`` (#2210 round 3) are the marker records this pass
    REFUSED to write into that ledger for failing provenance. They are not in
    ``finding_dispositions`` (validate first, write second), so they ride
    separately to post-synthesis, where they are stamped onto the verdict so
    the posted comment reports the attempt. Already logged by the time they
    are returned.

    ``delta_diff``/``delta_changed_files`` (#1837) are the fix-loop re-review
    pair: the diff between the previous reviewed head and this one, and its
    changed-path set. Both are ``None`` on cycle 0, which reviews the whole
    branch — the caller reads that as "use ``diff``". When they are set,
    ``diff`` is the SAME object as ``delta_diff`` (not a separately-captured
    full branch diff): the fix loop's scope-violation gate reads ``cycle0_files``,
    captured once at cycle 0 in ``run_review_with_fix_loop``, not this field, so
    a second full ``git diff``/parse on every in-loop cycle bought nothing and
    was removed (#1837 Performance SHOULD_FIX).

    ``changed_files`` and ``render_prompts`` (#2864) are the pass's full
    changed-path list and the bound prompt renderer that built
    ``prompts_by_role``: ``render_prompts(diff, changed_files)`` re-renders
    every role's prompt over another diff with this pass's roster, specs and
    context, which is how a sharded confirm round builds one prompt per shard.

    ``branch_context`` (#2980) is a conflict-resolution round's per-file
    branch hunks (the branch's own, already-reviewed change in each file the
    resolution touches), bound into ``render_prompts`` and counted by the
    shard planner. Empty on every other pass.

    ``review_config`` (#3038) records where the pass read the repository's
    review configuration from (the default-branch ref and commit) and which of
    those files the branch edited; the caller stamps it on the verdict, where
    it is recorded and never gates. ``None`` only for a hand-built record that
    never resolved configuration; :func:`_prepare_review_pass` always sets it.
    """

    roles: list[str]
    prompts_by_role: dict[str, str]
    diff: CapturedDiff
    reviewed_sha: str
    capability: _CodexFilesystemCapability
    agent_spec_status: list[AgentSpecStatus]
    voided_findings: list[VoidedFinding]
    finding_dispositions: dict[str, FindingDisposition]
    refused_dispositions: list[RefusedDisposition]
    changed_files: tuple[str, ...]
    render_prompts: _PromptRenderer
    delta_diff: CapturedDiff | None = None
    delta_changed_files: frozenset[str] | None = None
    branch_context: Mapping[str, str] = _NO_BRANCH_CONTEXT
    review_config: ReviewConfigStatus | None = None


def _delta_pass_inputs(
    worktree: Path, resolution: ResolutionSpec
) -> tuple[CapturedDiff, list[str], Mapping[str, str]]:
    """A conflict-resolution round's diff, changed files and context (#2980).

    The diff runs from the tree ``git merge-tree`` wrote (conflicts left in)
    to the merge commit's own tree, so it is only what the resolver did. The
    context is ``branch_base..branch_tip`` (the branch's own change) in the
    files that diff touches, per file.
    """
    diff, changed = _capture_tree_diff(
        worktree, resolution.conflicted_tree, resolution.resolved_tree
    )
    context = (
        _capture_branch_context(
            worktree, resolution.branch_base, resolution.branch_tip, changed
        )
        if changed
        else {}
    )
    return diff, changed, MappingProxyType(context)


def _emit_thread_reversal_events(
    task: TicketTask,
    merged: dict[str, FindingDisposition],
) -> None:
    """Audit every withdrawal this pass's comment thread brought in (#2232).

    The settle COMMAND emits one audit event per record it writes
    (``cw.cli.review.commands._emit_settle_events``); this path — the review
    pass folding the ticket thread's marker into the durable ledger — wrote
    nothing to the audit log at all. That predates this ticket (#2210's
    architecture), but #2232 is what makes a ``REVERSED`` record reachable
    through it, and a withdrawal of a load-bearing suppression is materially
    higher stakes than a hand-authored ``ACCEPTED``/``REJECTED``: it un-does a
    decision someone made, through a surface with no command invocation to
    point at afterwards. Same event type and same payload as the command
    (:func:`~cw.review_finding_dispositions.disposition_event_type` /
    :func:`~cw.review_finding_dispositions.disposition_event_payload`), so one
    ``cw event tail --type review.finding_disposition_reverted`` sees both.

    Emitted for a withdrawal that is NEW to the ledger — absent before this
    merge, or replacing a different record under the same key. The marker is
    re-parsed on every review pass, so emitting over ``parsed`` wholesale
    would re-record the same withdrawal every round; what is audited is the
    state change, which is what happened once.

    Only ``REVERSED`` is emitted here, deliberately. The other two outcomes
    reaching the ledger through this path is #2210's existing, unchanged
    behaviour, and auditing them is its own question with its own blast
    radius — not one to settle inside this ticket.

    **Degrades rather than raising**, unlike the command. There the audited act
    CREATES a durable suppression and the caller can refuse to write the
    marker; here the operator's decision already exists — durably, on the
    ticket thread — and there is nothing to hold back. Aborting a review pass
    over an event-store ``OSError`` would park a run to protect a record that
    the next pass re-reads and re-emits anyway. Same direction, and the same
    reasoning, as ``review_finding_dispositions.emit._emit_stale``.
    """
    reversals = [
        (key, entry)
        for key, entry in sorted(merged.items())
        if entry.outcome == REVERSED and task.finding_dispositions.get(key) != entry
    ]
    if not reversals:
        return
    # Deferred import: same cycle-avoidance as the sync import below.
    from cw.events import record_event

    for key, entry in reversals:
        try:
            record_event(
                disposition_event_type(entry),
                payload=disposition_event_payload(key, entry),
                correlation_id=task.ticket_id,
            )
        except OSError:
            _log.warning(
                "auto-dev: could not record the disposition-reverted audit "
                "event (ticket=%s, key=%s)",
                task.ticket_id,
                key,
                exc_info=True,
            )


def _merge_and_persist_finding_dispositions(
    task: TicketTask,
    worktree: Path,
    *,
    comments: list[dict[str, object]] | None | _CommentsNotProvided = (
        _COMMENTS_NOT_PROVIDED
    ),
) -> tuple[dict[str, FindingDisposition], list[RefusedDisposition]]:
    """Fold this pass's marker entries into *task*'s ledger, and persist them.

    Two records, one answer (#1838). The tracker marker is the operator's INPUT
    surface — hand-authored, live-fetched, and lost the moment a fetch degrades.
    ``TicketTask.finding_dispositions`` is the durable record derived from it,
    which survives the worktree teardown and regress/redispatch cycle a review
    memory has to outlive. This merges the two and writes the delta back so the
    NEXT round starts from what this one learned, even if the comment thread is
    unreachable then.

    The persistence write is skipped entirely when the thread carried no marker:
    there is nothing new to record, and every review pass paying for a
    dev-queue lock + read + write to store what is already stored would be a
    real cost for no benefit.

    **Validate first, write second** (#2210 round 3). The marker is the
    hand-pasteable surface, so what it parsed is split BEFORE anything is
    written: only the enforceable delta is merged and persisted to the running
    dev-queue row, and every record that failed provenance is logged once at
    WARNING here — naming the ticket and key — and returned as the second
    element. It is never written and never replaces an existing entry, so a
    malformed or pasted block cannot destroy the provenance of a legitimately
    settled finding. The caller carries the refusals to the verdict so the
    review output reports them too.
    """
    parsed, refused = _load_finding_dispositions(
        worktree, task.ticket_id, comments=comments
    )
    log_refused_dispositions(refused, task.ticket_id)
    merged = merge_finding_dispositions(task.finding_dispositions, parsed)
    # Audit BEFORE the write, for the same reason the settle command does
    # (#2232): the record of a withdrawal must not be able to lag the
    # withdrawal itself.
    _emit_thread_reversal_events(task, merged)
    if parsed:
        # Deferred import: cw.codex_background imports cw.codex_fix_loop, which
        # imports this package — a module-level import here would close that
        # cycle. Same shape as codex_background's own deferred cw.executor
        # import (#1727). Sanctioned via pyproject's PLC0415 per-file ignore.
        from cw.codex_background import _sync_finding_dispositions_to_running_task

        _sync_finding_dispositions_to_running_task(
            client_name=task.client,
            ticket_id=task.ticket_id,
            created_at=task.created_at,
            dispositions=parsed,
        )
    return merged, refused


def _prepare_review_pass(
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    *,
    config_branch: str,
    runner: CodexRunner | None,
    session_id: str,
    delta_from_sha: str | None = None,
    prior_open_findings: list[Finding] | None = None,
    capability: _CodexFilesystemCapability | None = None,
    backend: ExecutorName = "codex",
    resolution: ResolutionSpec | None = None,
    reviewer_sandbox_mode: SandboxMode = DEFAULT_REVIEWER_SANDBOX_MODE,
    engine_roster: EngineRoster | None = None,
) -> _ReviewPassInputs:
    """Assemble one review pass's inputs: capture diff, select roles, build prompts.

    Extracted from ``run_review``'s former input-assembly body (everything
    before ``run_codex_roles`` was called). Before #1709 it had no side effects
    beyond the read-only git/\u200bfilesystem reads it already performed. Shared by
    ``run_review`` and ``cw.codex_fix_loop``'s per-cycle re-review (#1392).

    Lives in this package's ``core`` because it is the orchestration entry point
    every other ``_context`` submodule feeds: it drives role selection, the
    repo-config and sensitive-file reads, agent-spec resolution, and prompt
    assembly, and owns none of them.

    ``runner``/``session_id`` (#1709) drive the filesystem-capability probe,
    which is what changed that: on a cold fingerprint cache it spends one real
    ``codex exec`` round-trip and writes the verdict to disk. Every subsequent
    call — notably the fix loop's per-cycle re-review — is a cache hit that
    runs nothing, which is why the probe lives here rather than at each call
    site.

    ``delta_from_sha``/``prior_open_findings`` (#1837) turn this into a
    fix-loop re-review pass: role selection, sensitive-hit scanning, and every
    prompt are built from the delta between *delta_from_sha* and the current
    head instead of the whole branch diff, and the still-open findings from
    earlier cycles are inlined for context. In this mode ``reviewed_sha`` is
    captured via a bare ``git rev-parse HEAD`` and ``diff`` is the delta
    itself — no second full ``_capture_diff`` runs, since nothing downstream
    reads a full branch diff off an in-loop pass (the fix loop's
    scope-violation gate uses its own cycle-0-captured file set). Both
    parameters default to ``None``, so ``run_review``'s cycle-0 call site is
    unchanged.

    ``capability`` (#2791) lets a non-codex reviewer (the Claude confirm round,
    whose ``Read``/``Grep``/``Glob`` tools reach the worktree by construction)
    supply its own verdict: the codex probe then never runs, and ``runner``
    may be ``None``. With no ``capability`` the probe runs as before and a
    ``runner`` is required (``ValueError`` otherwise).

    ``backend`` (#2808) names the reviewer backend every prompt is built for,
    so the Claude confirm round's prompts describe its own output channel and
    tools rather than codex's ``-o`` file and sandbox. It defaults to
    ``"codex"``, so the codex review and fix-loop call sites are unchanged.

    ``resolution`` (#2980) makes this a conflict-resolution pass: the diff
    runs from the spec's conflicted tree to its resolved tree,
    ``reviewed_sha`` is the spec's, and every prompt also carries the branch's
    own hunks in the touched files as context (``branch_context``). ``None``
    (the default) changes nothing. It is a different feature from the fix-loop
    re-review above (``delta_from_sha``) and the two are mutually exclusive:
    passing both raises ``ValueError`` before anything else runs, because the
    resolution diff would otherwise be rendered under the fix-loop re-review
    instructions.

    ``config_branch`` (#3038) is the REAL default-branch name the repository's
    review configuration (``review-extras.md``, ``review-policy.md``, the
    sensitive-files manifests) is read from, as ``origin/<config_branch>`` else
    the local ``<config_branch>``; the worktree copy is never read. It is
    separate from ``default_branch`` because the Claude confirm round hands
    ``default_branch`` a pinned diff-base sha, which is not a branch name.
    Required, so no call site can inherit a default that reads the wrong ref.

    ``reviewer_sandbox_mode`` (#1767) is the mode the capability probe runs
    under, the reviewer's resolved mode, so its verdict describes what the
    reviewers of this pass can read. Unused when ``capability`` is given.

    ``engine_roster`` (#3216) replaces role selection and agent-spec
    resolution with a repo review engine's manifest roster (see
    :func:`~cw.codex_review._context._roster.resolve_roster`).
    """
    if resolution is not None and delta_from_sha is not None:
        msg = "resolution and delta_from_sha are mutually exclusive"
        raise ValueError(msg)
    if capability is None:
        if runner is None:
            msg = "_prepare_review_pass needs a runner or a capability"
            raise ValueError(msg)
        capability = _probe_filesystem_capability(
            runner=runner, session_id=session_id, sandbox_mode=reviewer_sandbox_mode
        )
    delta_diff: CapturedDiff | None = None
    delta_changed_files: frozenset[str] | None = None
    branch_context = _NO_BRANCH_CONTEXT
    if resolution is not None:
        diff, changed_files, branch_context = _delta_pass_inputs(worktree, resolution)
        reviewed_sha = resolution.reviewed_sha
    elif delta_from_sha is not None:
        reviewed_sha = _capture_head_sha(worktree)
        delta_diff, delta_changed_list = _capture_delta_diff(
            worktree, delta_from_sha, reviewed_sha
        )
        delta_changed_files = frozenset(delta_changed_list)
        changed_files = delta_changed_list
        diff = delta_diff
    else:
        diff, reviewed_sha, changed_files = _capture_diff(worktree, default_branch)
    # #2767 non-ship-path: hint-only tier kept; ship gates use the new resolver
    scope_tier = resolve_tier(task.scope_hint)
    categories = _categorize_changed_files(changed_files)
    config_source = resolve_review_config_source(worktree, config_branch)
    sensitive_hits = _load_sensitive_hits(config_source, changed_files, scope_tier)
    repo_policy = _load_review_policy(config_source, scope_tier)
    project_rubrics = config_source.read(REVIEW_EXTRAS_PATH)
    plan_text, ticket_text = _load_ticket_context(worktree)
    # Fetched once and handed to both readers below (#1814 SHOULD_FIX) — each
    # independently called fetch_issue_comments for the same ticket, doubling
    # the gh subprocess/API cost of every review pass and fix-loop cycle.
    fetched_comments = _fetch_ticket_comments(worktree, task.ticket_id)
    operator_comments_text = _load_operator_comments(
        worktree, task.ticket_id, comments=fetched_comments
    )
    # #2964: the row's `cw review constrain` entries, read from the row
    # snapshot rather than the comment thread, so they reach every tracker.
    # #2984: plan-derived entries go to their own `## Decided Scope` section.
    operator_entries, plan_entries = split_constraints(task.operator_constraints)
    operator_constraints_text = render_constraint_entries(operator_entries) or None
    decided_scope_text = render_decided_entries(plan_entries) or None
    pending_operator_comment = _load_pending_operator_comment_marker(worktree)
    voided_findings = _load_voided_findings(
        worktree, task.ticket_id, comments=fetched_comments
    )
    finding_dispositions, refused_dispositions = (
        _merge_and_persist_finding_dispositions(
            task, worktree, comments=fetched_comments
        )
    )
    ruff_lint_config = _load_ruff_lint_config(worktree)
    quality_gates_text = _load_claude_md_quality_gates(worktree)
    lint_grounding = _render_lint_grounding_block(
        ruff_config=ruff_lint_config,
        quality_gates_text=quality_gates_text,
    )
    mutates_persisted_state = (
        bool(sensitive_hits) or categories.python or categories.frontend
    )
    roles, agent_specs, agent_spec_status = resolve_roster(
        worktree,
        engine_roster,
        scope_tier=scope_tier,
        categories=categories,
        mutates_persisted_state=mutates_persisted_state,
        has_ticket_context=ticket_text is not None,
    )
    render_prompts: _PromptRenderer = functools.partial(
        _render_role_prompts,
        roles=tuple(roles),
        agent_specs=agent_specs,
        repo_policy=repo_policy,
        plan_text=plan_text,
        ticket_text=ticket_text,
        project_rubrics=project_rubrics,
        sensitive_hits=sensitive_hits,
        capable=capability.capable,
        lint_grounding=lint_grounding,
        operator_comments_text=operator_comments_text,
        pending_operator_comment=pending_operator_comment,
        prior_open_findings=prior_open_findings,
        delta_mode=delta_from_sha is not None,
        adjudicated_findings=finding_dispositions,
        backend=backend,
        operator_constraints_text=operator_constraints_text,
        branch_context=branch_context,
        decided_scope_text=decided_scope_text,
    )
    prompts_by_role = render_prompts(diff, changed_files)
    return _ReviewPassInputs(
        roles=roles,
        prompts_by_role=prompts_by_role,
        diff=diff,
        reviewed_sha=reviewed_sha,
        capability=capability,
        agent_spec_status=agent_spec_status,
        voided_findings=voided_findings,
        finding_dispositions=finding_dispositions,
        refused_dispositions=refused_dispositions,
        delta_diff=delta_diff,
        delta_changed_files=delta_changed_files,
        changed_files=tuple(changed_files),
        render_prompts=render_prompts,
        branch_context=branch_context,
        review_config=review_config_status(config_source),
    )
