"""Prompt-driven codex reviewer orchestration for CodexExecutor (#1236).

Replaces the single ``codex exec review`` subprocess call with a per-reviewer-
role loop of generic ``codex exec`` calls, each fed a materialized prompt over
stdin and validated through the executor-neutral ``review_findings`` library
(#1237). This module plays the same support role for ``CodexExecutor`` that
``local_runner`` plays for ``LocalExecutor``: ``executor.py``'s Step 3 shrinks
to a thin delegation into :func:`run_review`.

Codex has no filesystem access to ``.claude/*`` the way a Claude subagent does
(snap sandbox), so every reviewer input — the authoritative agent spec, the
diff, the plan/ticket context, project rubrics, per-reviewer policy, and
sensitive-file hits — is read here by ``cw`` and inlined into the prompt.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import subprocess
import time
import uuid
from typing import TYPE_CHECKING, NamedTuple

import yaml

from cw.auto_dev_result import AutoDevResult, Health, Scope, StageReached
from cw.config import state_dir
from cw.local_runner import _SCHEMA_VERSION, make_blocked, resolve_tier
from cw.models import CONTEXT_JSON_RELATIVE_PATH
from cw.review_findings import (
    CapturedDiff,
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    ReviewVerdict,
    consolidate_verdict,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.codex_runner import CodexRunner
    from cw.models import TicketTask

_log = logging.getLogger(__name__)

STAGE3_REVIEW: StageReached = "stage3_review"

# Per-role failure reason codes (Resolution 4: reuse the existing coarse
# vocabulary per role rather than building a new typed taxonomy). Reused by
# executor.py, which re-exports them for backward-compatible imports.
CODEX_TIMEOUT = "codex_timeout"
CODEX_ERROR = "codex_error"
CODEX_REVIEW_UNPARSEABLE = "codex_review_unparseable"
CODEX_MUST_FIX_FINDINGS = "codex_must_fix_findings"
CODEX_BUDGET_EXHAUSTED = "budget_exhausted"

# Shared-deadline loop floor (Comment 3): never hand codex a per-role timeout
# below this; a role that cannot get at least this much budget is skipped as
# budget-exhausted instead.
_MIN_ROLE_TIMEOUT_SECONDS = 30

# Reviewer role name -> authoritative agent-spec file under .claude/agents/.
# Role names match the `/review` Step 3 table and each agent file's `name:`.
_REVIEWER_ROLE_AGENT_FILES: dict[str, str] = {
    "Code Quality Reviewer": "code-reviewer.md",
    "SysAdmin Reviewer": "sysadmin-reviewer.md",
    "Data Safety Reviewer": "data-safety-reviewer.md",
    "Product Manager Reviewer": "product-manager-reviewer.md",
    "Architecture Reviewer": "architecture-reviewer.md",
    "Test Reviewer": "test-reviewer.md",
    "Performance Reviewer": "performance-reviewer.md",
    "API Contract Validator": "api-contract-validator.md",
    "Deployment Reviewer": "deployment-reviewer.md",
}

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_SENSITIVE_HEADER = (
    "SENSITIVE FILES TOUCHED — APPLY ELEVATED SCRUTINY\n\n"
    "These files are high blast-radius. Apply maximum scrutiny for: unintended "
    "scope changes, missing auth checks, new external write paths, error "
    "handling gaps, cross-org data leakage, and regression risk."
)

_OUTPUT_INSTRUCTIONS = (
    "## Output\n"
    "Evaluate the diff strictly from the material inlined above — do not rely "
    "on filesystem access. Emit a single JSON object conforming to the provided "
    "ReviewerFindingsDocument schema to the output file (`-o`): `reviewer_role`, "
    "`status` (ok/degraded/failed), `detail`, and a `findings` array. Every "
    "finding's `evidence` MUST be a verbatim substring of the claimed file's "
    "changed lines. Report no prose outside the JSON object."
)


class _FileCategories(NamedTuple):
    """Boolean file-category flags for reviewer selection (per /review Step 2)."""

    python: bool
    frontend: bool
    tests: bool
    infra: bool
    config: bool


class _SensitiveHit(NamedTuple):
    """One changed file that matched a sensitive-files registry entry."""

    path: str
    category: str
    reason: str


def _codex_scratch_dir(session_id: str) -> Path:
    """Return (creating if needed) a per-run scratch dir under ``state_dir()``.

    Snap-confined codex cannot read ``/tmp`` (snap private tmp namespace), so
    schema/output file paths handed to ``codex exec --output-schema ... -o ...``
    MUST live under the user's home tree. This replaces the ``executor.py`` codex
    path's former ``tempfile.TemporaryDirectory()`` (which resolves under
    ``/tmp`` on this host) with a directory under ``state_dir()``.
    """
    scratch = state_dir() / "codex-review" / session_id
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _categorize_changed_files(files: Iterable[str]) -> _FileCategories:
    """Classify *files* into the /review Step 2 category flags."""
    python = frontend = tests = infra = config = False
    for path in files:
        base = path.rsplit("/", 1)[-1]
        if path.endswith(".py"):
            python = True
        if path.endswith((".ts", ".tsx", ".js", ".jsx", ".css")):
            frontend = True
        if (
            base.startswith("test_")
            or "_test." in base
            or path.startswith("tests/")
            or "/tests/" in path
            or "__tests__/" in path
        ):
            tests = True
        if (
            base.startswith("Dockerfile")
            or path.endswith((".yaml", ".yml"))
            or ".github/" in path
            or path.startswith("k8s/")
        ):
            infra = True
        if path.endswith((".toml", ".cfg", ".ini")) or (
            path.endswith(".json") and base != "package.json"
        ):
            config = True
    return _FileCategories(python, frontend, tests, infra, config)


def _select_reviewer_roles(
    scope_tier: str,
    *,
    categories: _FileCategories,
    mutates_persisted_state: bool,
    has_ticket_context: bool,
) -> list[str]:
    """Select ordered reviewer roles, mandatory-first (Comment 3).

    Encodes the small-scope table (auto-dev-review.md Step 3a) and the
    large-scope file-category table (review.md Steps 2-3) verbatim.
    """
    roles: list[str] = []
    code_changed = categories.python or categories.frontend
    if scope_tier == "large":
        if code_changed:
            roles.append("Code Quality Reviewer")
        roles.append("SysAdmin Reviewer")
        if code_changed:
            roles.append("Architecture Reviewer")
        if categories.tests or categories.python or categories.frontend:
            roles.append("Test Reviewer")
        if categories.python:
            roles.append("Performance Reviewer")
        if categories.python and categories.frontend:
            roles.append("API Contract Validator")
        if categories.infra:
            roles.append("Deployment Reviewer")
    else:
        roles.append("Code Quality Reviewer")
        roles.append("SysAdmin Reviewer")
    if mutates_persisted_state:
        roles.append("Data Safety Reviewer")
    if has_ticket_context:
        roles.append("Product Manager Reviewer")
    return roles


def _parse_hunk_new_start(header: str) -> int:
    """Return the new-file starting line number from a ``@@`` hunk header."""
    match = _HUNK_RE.match(header)
    return int(match.group(1)) if match else 0


def _parse_unified_diff(
    diff_text: str,
) -> tuple[dict[str, str], dict[str, dict[int, str]]]:
    """Split a unified diff into per-file hunk text and per-file added-line text.

    Tracks the new-file line number through each ``@@ -a,b +c,d @@`` header:
    ``+`` and context lines advance the counter, ``-`` lines do not. Deleted
    files (``+++ /dev/null``) contribute no new-file lines. Returns
    ``(file_diffs, file_line_text)``; the caller derives ``files`` from
    ``file_line_text`` so the two can never drift.
    """
    file_diffs: dict[str, str] = {}
    file_line_text: dict[str, dict[int, str]] = {}
    current_file: str | None = None
    current_lines: list[str] = []
    new_line_no = 0

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            if current_file is not None:
                file_diffs[current_file] = "\n".join(current_lines)
            current_file = None
            current_lines = []
            continue
        if raw.startswith("+++ "):
            path = raw[4:].removeprefix("b/")
            current_file = None if path == "/dev/null" else path
            if current_file is not None:
                file_line_text.setdefault(current_file, {})
            current_lines.append(raw)
            continue
        if current_file is None:
            continue
        current_lines.append(raw)
        if raw.startswith("@@"):
            new_line_no = _parse_hunk_new_start(raw)
        elif raw.startswith("+"):
            file_line_text[current_file][new_line_no] = raw[1:]
            new_line_no += 1
        elif raw.startswith("-"):
            continue
        else:
            new_line_no += 1

    if current_file is not None:
        file_diffs[current_file] = "\n".join(current_lines)
    return file_diffs, file_line_text


def _capture_diff(worktree: Path, default_branch: str) -> tuple[CapturedDiff, str]:
    """Capture ``git diff <default_branch>...HEAD`` as a :class:`CapturedDiff`.

    ``files`` is derived from ``file_line_text`` (the added-line map) so it can
    never drift from the per-line content. Returns ``(diff, reviewed_sha)``.
    """
    reviewed_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
    ).strip()
    diff_text = subprocess.check_output(
        ["git", "diff", "--no-color", f"{default_branch}...HEAD"],
        cwd=worktree,
        text=True,
    )
    file_diffs, file_line_text = _parse_unified_diff(diff_text)
    files = {f: sorted(lines) for f, lines in file_line_text.items()}
    diff = CapturedDiff(
        text=diff_text,
        files=files,
        file_diffs=file_diffs,
        file_line_text=file_line_text,
    )
    return diff, reviewed_sha


def _changed_file_paths(worktree: Path, default_branch: str) -> list[str]:
    """Return the repo-relative changed-file list (added, modified, deleted)."""
    out = subprocess.check_output(
        ["git", "diff", "--name-only", f"{default_branch}...HEAD"],
        cwd=worktree,
        text=True,
    )
    return [line for line in out.splitlines() if line.strip()]


def _load_optional_text(path: Path) -> str | None:
    """Return *path*'s text, or ``None`` if absent/unreadable/not UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _read_sensitive_manifest(path: Path) -> list[dict[str, str]]:
    """Read a sensitive-files manifest's entries, failing safe to ``[]``."""
    text = _load_optional_text(path)
    if text is None:
        return []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("sensitive_files")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and "path" in e]


def _hit_from_entry(changed: str, entry: dict[str, str]) -> _SensitiveHit:
    return _SensitiveHit(
        path=changed,
        category=str(entry.get("category", "")),
        reason=str(entry.get("reason", "")),
    )


def _load_sensitive_hits(
    worktree: Path, changed_files: Iterable[str], scope_tier: str
) -> list[_SensitiveHit]:
    """Match changed files against the sensitive-files registry for *scope_tier*.

    small: ONE path (``.claude/sensitive-files.yml``) matched via GLOB against
    each entry's ``path`` pattern; ``.github/sensitive-files.yml`` is never
    consulted. large: TWO paths first-hit-wins (``.claude`` then ``.github``)
    matched via SUBSTRING/ENDSWITH. The divergence is authoritative — the two
    tiers' contracts genuinely differ, so this is not unified.
    """
    changed = list(changed_files)
    if scope_tier == "small":
        entries = _read_sensitive_manifest(worktree / ".claude" / "sensitive-files.yml")
        return [
            _hit_from_entry(c, entry)
            for c in changed
            for entry in entries
            if fnmatch.fnmatch(c, entry["path"])
        ]
    for rel in (".claude/sensitive-files.yml", ".github/sensitive-files.yml"):
        registry = worktree / rel
        if registry.exists():
            entries = _read_sensitive_manifest(registry)
            return [
                _hit_from_entry(c, entry)
                for c in changed
                for entry in entries
                if entry["path"] in c or c.endswith(entry["path"])
            ]
    return []


def _parse_review_policy(text: str) -> dict[str, str]:
    """Parse ``review-policy.md`` H2 sections into a role-keyed map.

    Warn-and-skip: an H2 heading that is not a known reviewer name is logged
    and dropped; the parse never raises.
    """
    policy: dict[str, str] = {}
    heading: str | None = None
    body: list[str] = []

    def _commit() -> None:
        if heading is None:
            return
        if heading in _REVIEWER_ROLE_AGENT_FILES:
            policy[heading] = "\n".join(body).strip()
        else:
            _log.warning(
                'review-policy.md: unmatched section "%s" — skipped (typo?)',
                heading,
            )

    for line in text.splitlines():
        if line.startswith("## "):
            _commit()
            heading = line[3:].strip()
            body = []
        elif heading is not None:
            body.append(line)
    _commit()
    return policy


def _load_review_policy(worktree: Path, scope_tier: str) -> dict[str, str]:
    """Load the per-reviewer policy map for *scope_tier*.

    small: returns ``{}`` unconditionally WITHOUT reading the file — small-scope
    has no REPO_POLICY concept. large: parses ``.claude/review-policy.md`` H2
    sections keyed by reviewer name; missing file → ``{}``.
    """
    if scope_tier != "large":
        return {}
    text = _load_optional_text(worktree / ".claude" / "review-policy.md")
    if text is None:
        return {}
    return _parse_review_policy(text)


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


def _load_agent_spec(worktree: Path, role: str) -> str:
    """Return the authoritative agent spec for *role*, inlined verbatim."""
    filename = _REVIEWER_ROLE_AGENT_FILES[role]
    text = _load_optional_text(worktree / ".claude" / "agents" / filename)
    return text if text is not None else ""


def _render_sensitive_block(hits: list[_SensitiveHit]) -> str:
    """Render the elevated-scrutiny sensitive-files block (review.md Step 1.6)."""
    lines = [_SENSITIVE_HEADER, "", "Touched sensitive files:"]
    lines.extend(f"- {h.path} ({h.category}) — {h.reason}" for h in hits)
    return "\n".join(lines)


def _build_reviewer_prompt(
    role: str,
    *,
    agent_spec_text: str,
    diff: CapturedDiff,
    changed_files: Iterable[str],
    plan_text: str | None,
    ticket_text: str | None,
    project_rubrics: str | None,
    repo_policy_section: str | None,
    sensitive_hits: list[_SensitiveHit],
) -> str:
    """Materialize one reviewer's full prompt, inlining every needed section."""
    parts = [
        f"# Reviewer Role: {role}",
        f"## Agent Specification\n{agent_spec_text}",
    ]
    if ticket_text:
        parts.append(f"## Ticket Context\n{ticket_text}")
    if plan_text:
        parts.append(f"## Approved Plan\n{plan_text}")
    if project_rubrics:
        parts.append(f"## Project Rubrics\n{project_rubrics}")
    if repo_policy_section:
        parts.append(f"## Repo Policy for {role}\n{repo_policy_section}")
    if sensitive_hits:
        parts.append(_render_sensitive_block(sensitive_hits))
    parts.append("## Changed Files\n" + "\n".join(changed_files))
    parts.append(f"## Diff\n{diff.text}")
    parts.append(_OUTPUT_INSTRUCTIONS)
    return "\n\n".join(parts)


def _build_generic_codex_argv(
    *, model: str | None, schema_path: Path, output_path: Path
) -> list[str]:
    """Return the generic ``codex exec`` argv (no ``review``/``--base``)."""
    argv = [
        "codex",
        "exec",
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
    if model:
        argv += ["-m", model]
    return argv


def _slug(role: str) -> str:
    """Filesystem-safe slug for a reviewer role name."""
    return re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")


def _parse_reviewer_document(
    output_file_content: str | None,
) -> ReviewerFindingsDocument | None:
    """Parse codex's ``-o`` output into a document, failing closed to ``None``."""
    if output_file_content is None:
        return None
    try:
        data = json.loads(output_file_content)
    except json.JSONDecodeError:
        return None
    try:
        return ReviewerFindingsDocument.model_validate(data)
    except ValueError:
        return None


def _run_codex_role(
    *,
    runner: CodexRunner,
    worktree: Path,
    role: str,
    prompt: str,
    model: str | None,
    timeout_seconds: int | None,
    scratch_dir: Path,
) -> tuple[ReviewerFindingsDocument | None, ReviewerRunFailure | None]:
    """Run one reviewer role; return ``(document, failure)`` (exactly one set).

    Logs each failure mode (timeout, non-zero exit, missing/malformed output)
    via ``_log.warning`` before constructing the ``ReviewerRunFailure``.
    """
    slug = _slug(role)
    schema_path = scratch_dir / f"{slug}-schema.json"
    output_path = scratch_dir / f"{slug}-output.json"
    schema_path.write_text(
        json.dumps(ReviewerFindingsDocument.model_json_schema()), encoding="utf-8"
    )
    argv = _build_generic_codex_argv(
        model=model, schema_path=schema_path, output_path=output_path
    )
    result = runner.run(worktree, argv, timeout_seconds, stdin=prompt)
    if result.timed_out:
        _log.warning("codex review role %r timed out", role)
        return None, ReviewerRunFailure(role=role, reason=CODEX_TIMEOUT)
    if result.returncode != 0:
        _log.warning("codex review role %r exited %d", role, result.returncode)
        return None, ReviewerRunFailure(role=role, reason=CODEX_ERROR)
    doc = _parse_reviewer_document(result.output_file_content)
    if doc is None:
        _log.warning("codex review role %r produced no parseable output", role)
        return None, ReviewerRunFailure(role=role, reason=CODEX_REVIEW_UNPARSEABLE)
    return doc, None


def run_codex_roles(
    *,
    runner: CodexRunner,
    worktree: Path,
    roles: list[str],
    prompts_by_role: dict[str, str],
    model: str | None,
    wall_clock_budget_seconds: int | None,
) -> tuple[list[ReviewerFindingsDocument], list[ReviewerRunFailure]]:
    """Run every role under one shared wall-clock deadline (Comment 3).

    A ``None`` budget means no deadline (unlimited per-role timeout). Otherwise a
    single deadline is computed once; each role gets the remaining budget (never
    below ``_MIN_ROLE_TIMEOUT_SECONDS``), and a role that cannot get at least the
    floor is skipped as ``budget_exhausted`` — mandatory roles that already ran
    are unaffected.
    """
    scratch_dir = _codex_scratch_dir(uuid.uuid4().hex)
    documents: list[ReviewerFindingsDocument] = []
    failures: list[ReviewerRunFailure] = []
    deadline: float | None = (
        None
        if wall_clock_budget_seconds is None
        else time.monotonic() + wall_clock_budget_seconds
    )
    for role in roles:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= _MIN_ROLE_TIMEOUT_SECONDS:
                _log.warning("codex review role %r skipped: budget exhausted", role)
                failures.append(
                    ReviewerRunFailure(role=role, reason=CODEX_BUDGET_EXHAUSTED)
                )
                continue
            timeout: int | None = max(int(remaining), _MIN_ROLE_TIMEOUT_SECONDS)
        else:
            timeout = None
        doc, failure = _run_codex_role(
            runner=runner,
            worktree=worktree,
            role=role,
            prompt=prompts_by_role[role],
            model=model,
            timeout_seconds=timeout,
            scratch_dir=scratch_dir,
        )
        if doc is not None:
            documents.append(doc)
        if failure is not None:
            failures.append(failure)
    return documents, failures


def synthesize_codex_review_result(
    *,
    task: TicketTask,
    worktree: Path,
    documents: list[ReviewerFindingsDocument],
    failures: list[ReviewerRunFailure],
    diff: CapturedDiff,
    reviewed_sha: str,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Map consolidated review documents to a typed AutoDevResult.

    Disposition:
    - zero documents (all roles failed/skipped) → blocked/CODEX_REVIEW_UNPARSEABLE
    - consolidated verdict is blocking            → blocked/CODEX_MUST_FIX_FINDINGS
    - otherwise                                   → stage_complete

    Returns ``(result, verdict)``; ``verdict`` is ``None`` only on the zero-
    documents path (nothing to render into a review comment).
    """
    if not documents:
        result = make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_REVIEW_UNPARSEABLE,
            stage_reached=STAGE3_REVIEW,
        )
        return result, None
    verdict = consolidate_verdict(
        documents, diff, reviewed_sha, failed_reviewers=failures
    )
    if verdict.blocking:
        blocked = make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_MUST_FIX_FINDINGS,
            stage_reached=STAGE3_REVIEW,
        )
        return blocked.model_copy(update={"review": verdict.review}), verdict
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree, text=True
    ).strip()
    result = AutoDevResult(
        schema_version=_SCHEMA_VERSION,
        ticket_id=task.ticket_id,
        status="stage_complete",
        stage_reached=STAGE3_REVIEW,
        scope=Scope(
            tier=resolve_tier(task.scope_hint),
            files=0,
            lines_estimate=0,
            lines_actual=0,
            forbidden_touched=False,
        ),
        plan_source="none",
        branch=branch,
        fork_point_sha=None,
        commits=[],
        review=verdict.review,
        health=Health(
            lowest_agent_confidence="HIGH",
            any_incomplete_risk=False,
            recommendation="PROCEED",
        ),
        worktree_path=str(worktree),
    )
    return result, verdict


def _render_findings(verdict: ReviewVerdict, severity: str, heading: str) -> list[str]:
    findings = [
        af.finding for af in verdict.accepted if af.finding.severity == severity
    ]
    if not findings:
        return []
    lines = [f"### {heading}", ""]
    for finding in findings:
        loc = finding.file
        if finding.line_start is not None:
            loc = f"{loc}:{finding.line_start}"
        lines.append(f"- **{loc}** — {finding.summary}")
    lines.append("")
    return lines


def render_verdict_comment(verdict: ReviewVerdict) -> str:
    """Render a consolidated verdict into a GitHub-issue-comment markdown body."""
    lines = ["## Codex Review Verdict", ""]
    if verdict.blocking:
        lines.append(
            f"**BLOCKING** — {len(verdict.must_fix)} MUST_FIX finding(s) must be "
            "addressed before this branch can proceed."
        )
    else:
        lines.append("**Non-blocking** — no MUST_FIX findings.")
    lines.append("")
    lines.extend(_render_findings(verdict, "MUST_FIX", "MUST_FIX"))
    lines.extend(_render_findings(verdict, "SHOULD_FIX", "SHOULD_FIX"))
    return "\n".join(lines).rstrip() + "\n"


def run_review(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    wall_clock_budget_seconds: int | None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Run the full per-role review pass; return ``(result, verdict)``.

    This is ``CodexExecutor.spawn()``'s Step 3 delegation target: capture the
    diff, select reviewers, materialize prompts, run the shared-deadline loop,
    and synthesize the typed result.
    """
    diff, reviewed_sha = _capture_diff(worktree, default_branch)
    changed_files = _changed_file_paths(worktree, default_branch)
    scope_tier = resolve_tier(task.scope_hint)
    categories = _categorize_changed_files(changed_files)
    sensitive_hits = _load_sensitive_hits(worktree, changed_files, scope_tier)
    repo_policy = _load_review_policy(worktree, scope_tier)
    project_rubrics = _load_optional_text(worktree / ".claude" / "review-extras.md")
    plan_text, ticket_text = _load_ticket_context(worktree)
    mutates_persisted_state = (
        bool(sensitive_hits) or categories.python or categories.frontend
    )
    roles = _select_reviewer_roles(
        scope_tier,
        categories=categories,
        mutates_persisted_state=mutates_persisted_state,
        has_ticket_context=ticket_text is not None,
    )
    prompts_by_role = {
        role: _build_reviewer_prompt(
            role,
            agent_spec_text=_load_agent_spec(worktree, role),
            diff=diff,
            changed_files=changed_files,
            plan_text=plan_text,
            ticket_text=ticket_text,
            project_rubrics=project_rubrics,
            repo_policy_section=repo_policy.get(role),
            sensitive_hits=sensitive_hits,
        )
        for role in roles
    }
    documents, failures = run_codex_roles(
        runner=runner,
        worktree=worktree,
        roles=roles,
        prompts_by_role=prompts_by_role,
        model=model,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
    )
    return synthesize_codex_review_result(
        task=task,
        worktree=worktree,
        documents=documents,
        failures=failures,
        diff=diff,
        reviewed_sha=reviewed_sha,
    )
