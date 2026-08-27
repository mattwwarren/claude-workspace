"""Per-pass context loading and the review-pass assembly entry point.

The orchestration half of the prompt-context concern: the ticket/plan and live
comment-thread reads every pass performs, the adjudication-ledger merge that
persists what a pass learned, and :func:`_prepare_review_pass`, which drives
every other ``_context`` submodule to produce one pass's
:class:`_ReviewPassInputs`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NamedTuple

from cw.codex_review._capability import _probe_filesystem_capability
from cw.codex_review._context._agent_spec import (
    _load_agent_spec_fallback_gate,
    _resolve_agent_spec,
)
from cw.codex_review._context._file_selection import (
    _categorize_changed_files,
    _select_reviewer_roles,
)
from cw.codex_review._context._prompt_render import _build_reviewer_prompt
from cw.codex_review._context._repo_config import (
    _load_claude_md_quality_gates,
    _load_review_policy,
    _load_ruff_lint_config,
    _render_lint_grounding_block,
)
from cw.codex_review._context._sensitive_files import _load_sensitive_hits
from cw.codex_review._context._util import _load_optional_text
from cw.codex_review._diff import (
    _capture_delta_diff,
    _capture_diff,
    _capture_head_sha,
)
from cw.gh import FETCH_COMMENTS_TIMEOUT, fetch_issue_comments
from cw.local_runner import resolve_tier
from cw.models import CONTEXT_JSON_RELATIVE_PATH, HOOK_CONTEXT_RELATIVE_PATH
from cw.review_adjudication import parse_voided_findings_block
from cw.review_finding_dispositions import (
    merge_finding_dispositions,
    parse_finding_disposition_block,
)
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_review._capability import _CodexFilesystemCapability
    from cw.codex_runner import CodexRunner
    from cw.models import TicketTask
    from cw.review_adjudication import VoidedFinding
    from cw.review_finding_dispositions import FindingDisposition
    from cw.review_findings import (
        AgentSpecStatus,
        CapturedDiff,
        Finding,
    )


def _load_ticket_context(worktree: Path) -> tuple[str | None, str | None]:
    """Return ``(plan_text, ticket_text)`` from ``.cw/plan.md`` / ``.cw/context.json``.

    Reuses ``local_runner.build_task_message``'s read pattern (no tracker/network
    call): the approved plan text and the ticket's title+body already
    materialized in the worktree at Stage 1.
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
            title = str(data.get("title") or "")
            body = str(data.get("body") or "")
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
    """
    if resolve_tracker(worktree) != TRACKER_GITHUB_ISSUES:
        return None
    return fetch_issue_comments(ticket_id, timeout=FETCH_COMMENTS_TIMEOUT, cwd=worktree)


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
    than in ``.cw/`` precisely because ``dispatch/gating.py`` deletes
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
) -> dict[str, FindingDisposition]:
    """Parse the operator's cross-round finding adjudications off the ticket.

    Sibling of :func:`_load_voided_findings` in every respect that matters —
    same tracker gate, same shared-fetch parameter, same degrade-to-empty-and-
    never-raise contract — but reading the ``REVIEW-FINDING-DISPOSITIONS``
    marker (#1838) rather than ``VOIDED-REVIEW-FINDINGS`` (#1814).

    Kept a separate loader rather than folded into the voided one because the
    two records are separate contracts with separate identities and lifetimes:
    a void lapses when the code moves, an adjudication does not. Merging the
    reads would couple two grammars that must be free to diverge.

    Returns only what the CURRENT comment thread carries. The durable half of
    the ledger lives on ``TicketTask.finding_dispositions``, and
    :func:`_prepare_review_pass` merges the two — so a degraded fetch costs the
    pass nothing it already knew.
    """
    if isinstance(comments, _CommentsNotProvided):
        comments = _fetch_ticket_comments(worktree, ticket_id)
    if not comments:
        return {}
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
    is deleted outright by ``dispatch/gating.py``'s stale-context invalidation
    (#1046) on a rescued respawn, so ``queue_metadata`` cannot live there. Both
    ends now share the one constant so the read cannot drift off the write
    again; the reader-vs-writer path agreement is pinned by
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

    ``delta_diff``/``delta_changed_files`` (#1837) are the fix-loop re-review
    pair: the diff between the previous reviewed head and this one, and its
    changed-path set. Both are ``None`` on cycle 0, which reviews the whole
    branch — the caller reads that as "use ``diff``". When they are set,
    ``diff`` is the SAME object as ``delta_diff`` (not a separately-captured
    full branch diff): the fix loop's scope-violation gate reads ``cycle0_files``,
    captured once at cycle 0 in ``run_review_with_fix_loop``, not this field, so
    a second full ``git diff``/parse on every in-loop cycle bought nothing and
    was removed (#1837 Performance SHOULD_FIX).
    """

    roles: list[str]
    prompts_by_role: dict[str, str]
    diff: CapturedDiff
    reviewed_sha: str
    capability: _CodexFilesystemCapability
    agent_spec_status: list[AgentSpecStatus]
    voided_findings: list[VoidedFinding]
    finding_dispositions: dict[str, FindingDisposition]
    delta_diff: CapturedDiff | None = None
    delta_changed_files: frozenset[str] | None = None


def _merge_and_persist_finding_dispositions(
    task: TicketTask,
    worktree: Path,
    *,
    comments: list[dict[str, object]] | None | _CommentsNotProvided = (
        _COMMENTS_NOT_PROVIDED
    ),
) -> dict[str, FindingDisposition]:
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
    """
    parsed = _load_finding_dispositions(worktree, task.ticket_id, comments=comments)
    merged = merge_finding_dispositions(task.finding_dispositions, parsed)
    if parsed:
        # Deferred import: cw.codex_background imports cw.codex_fix_loop, which
        # imports this package — a module-level import here would close that
        # cycle. Same shape as codex_background's own deferred cw.executor
        # import (#1727). Sanctioned via pyproject's PLC0415 per-file ignore.
        from cw.codex_background import _sync_finding_dispositions_to_running_task

        _sync_finding_dispositions_to_running_task(
            client_name=task.client,
            ticket_id=task.ticket_id,
            dispositions=parsed,
        )
    return merged


def _prepare_review_pass(
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    *,
    runner: CodexRunner,
    session_id: str,
    delta_from_sha: str | None = None,
    prior_open_findings: list[Finding] | None = None,
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
    """
    capability = _probe_filesystem_capability(runner=runner, session_id=session_id)
    delta_diff: CapturedDiff | None = None
    delta_changed_files: frozenset[str] | None = None
    if delta_from_sha is not None:
        reviewed_sha = _capture_head_sha(worktree)
        delta_diff, delta_changed_list = _capture_delta_diff(
            worktree, delta_from_sha, reviewed_sha
        )
        delta_changed_files = frozenset(delta_changed_list)
        changed_files = delta_changed_list
        diff = delta_diff
    else:
        diff, reviewed_sha, changed_files = _capture_diff(worktree, default_branch)
    scope_tier = resolve_tier(task.scope_hint)
    categories = _categorize_changed_files(changed_files)
    sensitive_hits = _load_sensitive_hits(worktree, changed_files, scope_tier)
    repo_policy = _load_review_policy(worktree, scope_tier)
    project_rubrics = _load_optional_text(worktree / ".claude" / "review-extras.md")
    plan_text, ticket_text = _load_ticket_context(worktree)
    # Fetched once and handed to both readers below (#1814 SHOULD_FIX) — each
    # independently called fetch_issue_comments for the same ticket, doubling
    # the gh subprocess/API cost of every review pass and fix-loop cycle.
    fetched_comments = _fetch_ticket_comments(worktree, task.ticket_id)
    operator_comments_text = _load_operator_comments(
        worktree, task.ticket_id, comments=fetched_comments
    )
    pending_operator_comment = _load_pending_operator_comment_marker(worktree)
    voided_findings = _load_voided_findings(
        worktree, task.ticket_id, comments=fetched_comments
    )
    finding_dispositions = _merge_and_persist_finding_dispositions(
        task, worktree, comments=fetched_comments
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
    roles = _select_reviewer_roles(
        scope_tier,
        categories=categories,
        mutates_persisted_state=mutates_persisted_state,
        has_ticket_context=ticket_text is not None,
    )
    fallback_enabled = _load_agent_spec_fallback_gate(worktree)
    resolutions = {
        role: _resolve_agent_spec(
            worktree, role, global_fallback_enabled=fallback_enabled
        )
        for role in roles
    }
    prompts_by_role = {
        role: _build_reviewer_prompt(
            role,
            agent_spec_text=resolutions[role].text,
            diff=diff,
            changed_files=changed_files,
            plan_text=plan_text,
            ticket_text=ticket_text,
            project_rubrics=project_rubrics,
            repo_policy_section=repo_policy.get(role),
            sensitive_hits=sensitive_hits,
            capable=capability.capable,
            lint_grounding=lint_grounding,
            operator_comments_text=operator_comments_text,
            pending_operator_comment=pending_operator_comment,
            prior_open_findings=prior_open_findings,
            delta_mode=delta_from_sha is not None,
            adjudicated_findings=finding_dispositions,
        )
        for role in roles
    }
    return _ReviewPassInputs(
        roles=roles,
        prompts_by_role=prompts_by_role,
        diff=diff,
        reviewed_sha=reviewed_sha,
        capability=capability,
        agent_spec_status=[resolutions[role].status for role in roles],
        voided_findings=voided_findings,
        finding_dispositions=finding_dispositions,
        delta_diff=delta_diff,
        delta_changed_files=delta_changed_files,
    )
