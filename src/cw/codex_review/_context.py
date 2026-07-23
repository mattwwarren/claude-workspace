"""Reviewer role selection and prompt-context assembly for the codex-review package.

Codex has no filesystem access to ``.claude/*`` (snap sandbox), so every
reviewer input — the authoritative agent spec, plan/ticket context, project
rubrics, per-reviewer policy, and sensitive-file hits — is read here by ``cw``
and inlined into each role's materialized prompt. Also owns file categorization,
the small-/large-scope reviewer-selection tables, and codex output-document
parsing. Consumed by ``core`` (prompt assembly) and ``_roles`` (doc parsing).
"""

from __future__ import annotations

import fnmatch
import json
import logging
from typing import TYPE_CHECKING, NamedTuple

import yaml

from cw.codex_review._diff import _capture_diff
from cw.local_runner import resolve_tier
from cw.models import CONTEXT_JSON_RELATIVE_PATH
from cw.review_findings import ReviewerFindingsDocument

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.models import TicketTask
    from cw.review_findings import CapturedDiff

_log = logging.getLogger(__name__)

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

    ``categories.config`` is intentionally not a direct branch condition here:
    review.md's file-category table never assigns config its own reviewer row
    (unlike infra -> Deployment or python -> Performance) — its only defined
    effect is the Data Safety "skip on doc/config/style-only diffs" rule,
    which is already satisfied because a config-only diff also has
    ``python=False`` and ``frontend=False`` and therefore never sets
    ``mutates_persisted_state`` (see ``run_review``'s Adopted Assumption 2).
    ``categories.config`` remains a real, tested field of ``_FileCategories``
    for that categorization contract; it is simply a no-op input here.
    """
    roles: list[str] = []
    code_changed = categories.python or categories.frontend
    if scope_tier == "large":
        if code_changed:
            roles.append("Code Quality Reviewer")
        roles.append("SysAdmin Reviewer")
        if code_changed:
            roles.append("Architecture Reviewer")
        # review.md's table reads "Test files changed OR testable code
        # changed without test changes". Boolean-equivalent to `tests or
        # code_changed` by absorption (A or (B and not A) == A or B) — the
        # "without test changes" qualifier never changes the outcome, so it
        # is adopted as a simplification rather than encoded as dead-weight
        # `and not categories.tests` clauses (SHOULD_FIX 12, #1236).
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


class _ReviewPassInputs(NamedTuple):
    """Assembled, side-effect-free inputs for one per-role review pass (#1392).

    The output of :func:`_prepare_review_pass` — everything ``run_codex_roles``
    needs (selected ``roles`` and their materialized ``prompts_by_role``) plus
    the captured ``diff`` and ``reviewed_sha`` that
    ``synthesize_codex_review_result`` consumes. Extracted so the fix loop can
    re-run a fresh review pass each cycle without re-inlining ``run_review``'s
    input-assembly body.
    """

    roles: list[str]
    prompts_by_role: dict[str, str]
    diff: CapturedDiff
    reviewed_sha: str


def _prepare_review_pass(
    task: TicketTask, worktree: Path, default_branch: str
) -> _ReviewPassInputs:
    """Assemble one review pass's inputs: capture diff, select roles, build prompts.

    Pure extraction of ``run_review``'s former input-assembly body (everything
    before ``run_codex_roles`` was called) — no logic change, no side effects
    beyond the read-only git/\u200bfilesystem reads it already performed. Shared by
    ``run_review`` and ``cw.codex_fix_loop``'s per-cycle re-review (#1392).

    Lives here (not ``core.py``) so it stays co-located with
    :func:`_load_optional_text` alongside its other bare-name callers — a test
    patches ``_load_optional_text`` via module-object ``setattr`` on this
    module, which only intercepts same-module bare-name calls.
    """
    diff, reviewed_sha, changed_files = _capture_diff(worktree, default_branch)
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
    return _ReviewPassInputs(
        roles=roles,
        prompts_by_role=prompts_by_role,
        diff=diff,
        reviewed_sha=reviewed_sha,
    )
