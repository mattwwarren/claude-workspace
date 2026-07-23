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
import shutil
import subprocess
import time
import uuid
from typing import TYPE_CHECKING, NamedTuple

import yaml

from cw.auto_dev_result import AutoDevResult, Health, Scope, StageReached
from cw.config import state_dir
from cw.executor_diagnostics import (
    ExecutorFailureCategory,
    append_diagnostics_pointer,
    build_executor_failure,
    persist_diagnostics_bundle,
)
from cw.local_runner import _SCHEMA_VERSION, make_blocked, resolve_tier
from cw.models import CONTEXT_JSON_RELATIVE_PATH
from cw.openai_strict_schema import to_openai_strict_schema
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

    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.models import TicketTask
    from cw.review_findings import Severity

_log = logging.getLogger(__name__)

STAGE3_REVIEW: StageReached = "stage3_review"

# Per-role failure reason codes (Resolution 4: reuse the existing coarse
# vocabulary per role rather than building a new typed taxonomy). These are
# owned here, not re-exported by executor.py — callers (including
# tests/test_codex_executor.py) import them directly from cw.codex_review.
CODEX_TIMEOUT = "codex_timeout"
CODEX_ERROR = "codex_error"
CODEX_REVIEW_UNPARSEABLE = "codex_review_unparseable"
CODEX_MUST_FIX_FINDINGS = "codex_must_fix_findings"
CODEX_BUDGET_EXHAUSTED = "budget_exhausted"
# A partial review (some roles produced documents, but at least one selected
# role skipped or errored without one) blocks rather than silently shipping a
# reduced review pass — Decision 7 (#1236 finish spec).
CODEX_REVIEW_PARTIAL = "codex_review_partial"

# Failure reasons transient enough that a retry might succeed without any
# code/config change on our side (the role either never got a turn at all, or
# codex itself timed out) — used to set Blocker.retry_eligible so reconcile
# can self-heal instead of parking the ticket (MUST_FIX 2).
_TRANSIENT_FAILURE_REASONS = frozenset({CODEX_TIMEOUT, CODEX_BUDGET_EXHAUSTED})

# Shared-deadline loop floor (Comment 3): never hand codex a per-role timeout
# below this; a role that cannot get at least this much budget is skipped as
# budget-exhausted instead.
_MIN_ROLE_TIMEOUT_SECONDS = 30

# Exit code Popen/RealCodexRunner reports when the codex binary is not on PATH
# (FileNotFoundError → CodexRunResult(returncode=127, ...)); paired with a
# "command not found" stderr it classifies as a spawn_error (#1239).
_COMMAND_NOT_FOUND_RETURNCODE = 127

# Maps the fine-grained ExecutorFailureCategory (#1239 diagnostics taxonomy)
# to the coarse ReviewerRunFailure.reason vocabulary above — the single source
# of truth _run_codex_role delegates to instead of independently re-deriving
# the same reason via its own branch walk (#1330 item 5). Total (all 9
# category members are explicit keys, no .get() fallback) so a future category
# addition fails loudly (see test_category_to_reason_mapping_is_total) rather
# than silently KeyError-ing at runtime.
#
# spawn_error and nonzero_exit both map to CODEX_ERROR — exactly what the old
# `elif result.returncode != 0` branch produced for both shapes, so
# spawn_error's retry-eligibility (excluded from _TRANSIENT_FAILURE_REASONS)
# is unchanged by this refactor. runtime_error and semantic_validation_failure
# are unreachable through _classify_codex_failure today (the former is
# aider-only; the latter is a reserved category with no live producer) — both
# get the closest semantically-adjacent reason purely so the dict is total.
_CATEGORY_TO_REASON: dict[ExecutorFailureCategory, str] = {
    "timeout": CODEX_TIMEOUT,
    "spawn_error": CODEX_ERROR,
    "nonzero_exit": CODEX_ERROR,
    "runtime_error": CODEX_ERROR,
    "missing_output": CODEX_REVIEW_UNPARSEABLE,
    "empty_output": CODEX_REVIEW_UNPARSEABLE,
    "invalid_json": CODEX_REVIEW_UNPARSEABLE,
    "schema_mismatch": CODEX_REVIEW_UNPARSEABLE,
    "semantic_validation_failure": CODEX_REVIEW_UNPARSEABLE,
}

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
# Matches every ``diff --git a/<path> b/<path>`` header, including files with
# no added lines at all (pure deletions) — the header line is present
# regardless of what follows, unlike ``+++ b/<path>`` (absent for deletions,
# replaced with ``+++ /dev/null``). Used to derive the changed-file list from
# a single already-parsed diff instead of a second ``git diff --name-only``
# subprocess call (Performance, SHOULD_FIX 11).
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/.+ b/(.+)$")

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


def _parse_hunk_new_start(header: str) -> int:
    """Return the new-file starting line number from a ``@@`` hunk header."""
    match = _HUNK_RE.match(header)
    return int(match.group(1)) if match else 0


def _parse_unified_diff(
    diff_text: str,
) -> tuple[dict[str, str], dict[str, dict[int, str]], list[str]]:
    """Split a unified diff into per-file hunk text and per-file added-line text.

    Tracks the new-file line number through each ``@@ -a,b +c,d @@`` header:
    ``+`` and context lines advance the counter, ``-`` lines do not. Deleted
    files (``+++ /dev/null``) contribute no new-file lines. Returns
    ``(file_diffs, file_line_text, changed_files)``; the caller derives
    ``files`` from ``file_line_text`` so the two can never drift.
    ``changed_files`` is every path named by a ``diff --git`` header, in diff
    order — including pure deletions, which contribute nothing to
    ``file_diffs``/``file_line_text`` but must still appear in the changed-file
    list (SHOULD_FIX 11, #1236: this replaces a second, redundant
    ``git diff --name-only`` subprocess call).
    """
    file_diffs: dict[str, str] = {}
    file_line_text: dict[str, dict[int, str]] = {}
    changed_files: list[str] = []
    current_file: str | None = None
    current_lines: list[str] = []
    new_line_no = 0

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            if current_file is not None:
                file_diffs[current_file] = "\n".join(current_lines)
            current_file = None
            current_lines = []
            header_match = _DIFF_GIT_HEADER_RE.match(raw)
            if header_match:
                changed_files.append(header_match.group(1))
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
    return file_diffs, file_line_text, changed_files


def _capture_diff(
    worktree: Path, default_branch: str
) -> tuple[CapturedDiff, str, list[str]]:
    """Capture ``git diff <default_branch>...HEAD`` as a :class:`CapturedDiff`.

    ``files`` (on the returned ``CapturedDiff``) is derived from
    ``file_line_text`` (the added-line map) so it can never drift from the
    per-line content. Returns ``(diff, reviewed_sha, changed_files)`` —
    ``changed_files`` is the full changed-path list (including pure
    deletions), parsed from this same diff text rather than a second
    subprocess call (SHOULD_FIX 11, #1236).
    """
    reviewed_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
    ).strip()
    diff_text = subprocess.check_output(
        ["git", "diff", "--no-color", f"{default_branch}...HEAD"],
        cwd=worktree,
        text=True,
    )
    file_diffs, file_line_text, changed_files = _parse_unified_diff(diff_text)
    files = {f: sorted(lines) for f, lines in file_line_text.items()}
    diff = CapturedDiff(
        text=diff_text,
        files=files,
        file_diffs=file_diffs,
        file_line_text=file_line_text,
    )
    return diff, reviewed_sha, changed_files


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
    """Return the generic ``codex exec`` argv (no ``review``/``--base``).

    ``--sandbox read-only`` (ticket AC, MUST_FIX 4, #1236): every reviewer
    input is inlined into the prompt over stdin — a reviewer role has no
    legitimate reason to write to the worktree, so it never gets write
    access, matching the pre-#1236 ``codex exec review`` path's implicit
    read-only posture.
    """
    argv = [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
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


def _classify_codex_output_failure(content: str | None) -> ExecutorFailureCategory:
    """Classify an unparseable codex ``-o`` output into a typed category (#1239).

    Split from :func:`_classify_codex_failure` to keep both under the PLR0911
    return cap. Only called once the process exited 0, so the output itself is
    the sole failure source: ``missing_output`` / ``empty_output`` /
    ``invalid_json`` / ``schema_mismatch``. A genuinely valid document never
    reaches here (the caller returned it), so the final return is unreachable.
    """
    if content is None:
        return "missing_output"
    if not content.strip():
        return "empty_output"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return "invalid_json"
    try:
        ReviewerFindingsDocument.model_validate(data)
    except ValueError:
        return "schema_mismatch"
    return "schema_mismatch"  # pragma: no cover — unreachable on the failure path


def _classify_codex_failure(result: CodexRunResult) -> ExecutorFailureCategory:
    """Map a failed :class:`CodexRunResult` to a typed failure category (#1239).

    The single source of truth ``_run_codex_role`` delegates to (#1330 item 5)
    for the timed_out -> returncode -> output-parse ordering, refining the
    coarse ``ReviewerRunFailure.reason`` into the finer diagnostics taxonomy: a
    non-zero exit is split into ``spawn_error`` (codex binary missing) vs
    ``nonzero_exit``, and an unparseable output is delegated to
    :func:`_classify_codex_output_failure`.
    """
    if result.timed_out:
        return "timeout"
    if (
        result.returncode == _COMMAND_NOT_FOUND_RETURNCODE
        and "command not found" in result.stderr
    ):
        return "spawn_error"
    if result.returncode != 0:
        return "nonzero_exit"
    return _classify_codex_output_failure(result.output_file_content)


def _persist_codex_role_diagnostics(
    *,
    session_id: str,
    role: str,
    category: ExecutorFailureCategory,
    result: CodexRunResult,
    argv: list[str],
    duration_seconds: float,
    schema_path: Path,
    output_path: Path,
) -> None:
    """Build the typed :class:`ExecutorFailure` and write its diagnostics bundle.

    *category* is classified once by the caller (``_run_codex_role``) and
    threaded through here rather than re-derived via a second
    ``_classify_codex_failure(result)`` call (#1330 item 5).
    """
    failure = build_executor_failure(
        category=category,
        executor_name="codex",
        session_id=session_id,
        # Codex argv is content-free (prompt travels over stdin); the model's
        # own argv_sanitized field_validator leaves it unchanged, kept as raw
        # here for symmetry with the aider call sites.
        argv=argv,
        stdout_excerpt=result.stdout,
        stderr_excerpt=result.stderr,
        reviewer_role=role,
        duration_seconds=duration_seconds,
        exit_code=result.returncode,
        structured_output_excerpt=result.output_file_content,
    )
    persist_diagnostics_bundle(
        session_id=session_id,
        role_slug=_slug(role),
        failure=failure,
        scratch_schema_path=schema_path,
        scratch_output_path=output_path,
    )


def _run_codex_role(
    *,
    runner: CodexRunner,
    worktree: Path,
    role: str,
    prompt: str,
    model: str | None,
    timeout_seconds: int | None,
    scratch_dir: Path,
    session_id: str,
) -> tuple[ReviewerFindingsDocument | None, ReviewerRunFailure | None]:
    """Run one reviewer role; return ``(document, failure)`` (exactly one set).

    Logs each failure mode (timeout, non-zero exit, missing/malformed output)
    via ``_log.warning`` before constructing the ``ReviewerRunFailure``, and
    persists a typed diagnostics bundle (classified into the finer #1239
    taxonomy) under ``session_id``'s diagnostics dir on every failure branch.
    """
    slug = _slug(role)
    schema_path = scratch_dir / f"{slug}-schema.json"
    output_path = scratch_dir / f"{slug}-output.json"
    schema_path.write_text(
        json.dumps(
            to_openai_strict_schema(ReviewerFindingsDocument.model_json_schema())
        ),
        encoding="utf-8",
    )
    argv = _build_generic_codex_argv(
        model=model, schema_path=schema_path, output_path=output_path
    )
    start = time.monotonic()
    result = runner.run(worktree, argv, timeout_seconds, stdin=prompt)
    duration = time.monotonic() - start

    if not result.timed_out and result.returncode == 0:
        doc = _parse_reviewer_document(result.output_file_content)
        if doc is not None:
            return doc, None

    category = _classify_codex_failure(result)
    reason = _CATEGORY_TO_REASON[category]
    _log.warning("codex review role %r failed: %s (%s)", role, reason, category)
    _persist_codex_role_diagnostics(
        session_id=session_id,
        role=role,
        category=category,
        result=result,
        argv=argv,
        duration_seconds=duration,
        schema_path=schema_path,
        output_path=output_path,
    )
    return None, ReviewerRunFailure(role=role, reason=reason)


def run_codex_roles(
    *,
    runner: CodexRunner,
    worktree: Path,
    roles: list[str],
    prompts_by_role: dict[str, str],
    model: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
) -> tuple[list[ReviewerFindingsDocument], list[ReviewerRunFailure]]:
    """Run every role under one shared wall-clock deadline (Comment 3).

    A ``None`` budget means no deadline (unlimited per-role timeout). Otherwise a
    single deadline is computed once; each role gets the remaining budget (never
    below ``_MIN_ROLE_TIMEOUT_SECONDS``), and a role that cannot get at least the
    floor is skipped as ``budget_exhausted`` — mandatory roles that already ran
    are unaffected.

    The per-run scratch dir (schema/output files, see ``_codex_scratch_dir``)
    is removed before returning, success or failure — it lives under the
    shared, long-running ``state_dir()``, not an auto-cleaning
    ``tempfile.TemporaryDirectory()``, so leaving it behind on every call
    leaks disk on a long-running dispatch host (MUST_FIX 1, #1236).
    """
    scratch_dir = _codex_scratch_dir(uuid.uuid4().hex)
    try:
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
                session_id=session_id,
            )
            if doc is not None:
                documents.append(doc)
            if failure is not None:
                failures.append(failure)
        return documents, failures
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _format_failures_detail(
    failures: list[ReviewerRunFailure], *, session_id: str
) -> str:
    """Render *failures* as a short ``role (reason)`` summary for ``details``.

    Appends a pointer to the on-disk diagnostics bundle so an operator reading
    the blocked sentinel knows where the per-role failure artifacts landed.
    """
    summary = "; ".join(f"{f.role} ({f.reason})" for f in failures)
    return append_diagnostics_pointer(summary, session_id=session_id)


def synthesize_codex_review_result(
    *,
    task: TicketTask,
    worktree: Path,
    documents: list[ReviewerFindingsDocument],
    failures: list[ReviewerRunFailure],
    diff: CapturedDiff,
    reviewed_sha: str,
    session_id: str,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Map consolidated review documents to a typed AutoDevResult.

    Disposition:
    - zero documents (all roles failed/skipped) → blocked/CODEX_REVIEW_UNPARSEABLE,
      with ``failures`` folded into ``details`` and ``retry_eligible=True`` when
      at least one failure is transient (``codex_timeout``/``budget_exhausted``)
      (MUST_FIX 2, #1236).
    - consolidated verdict is blocking            → blocked/CODEX_MUST_FIX_FINDINGS
    - documents present but at least one selected role skipped/errored without
      producing one (a partial review) → blocked/CODEX_REVIEW_PARTIAL — a
      review that silently proceeded on an incomplete roster would be exactly
      the "spuriously clean sentinel" risk the ``agents_run`` gate exists to
      catch (Decision 7, #1236).
    - otherwise (documents complete, no MUST_FIX)  → stage_complete

    Returns ``(result, verdict)``; ``verdict`` is ``None`` only on the zero-
    documents path (nothing to render into a review comment).
    """
    if not documents:
        transient = any(f.reason in _TRANSIENT_FAILURE_REASONS for f in failures)
        result = make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_REVIEW_UNPARSEABLE,
            details=_format_failures_detail(failures, session_id=session_id),
            retry_eligible=True if transient else None,
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
            details=render_verdict_comment(verdict),
            stage_reached=STAGE3_REVIEW,
        )
        return blocked.model_copy(update={"review": verdict.review}), verdict
    if failures:
        partial = make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_REVIEW_PARTIAL,
            details=_format_failures_detail(failures, session_id=session_id),
            stage_reached=STAGE3_REVIEW,
        )
        return partial.model_copy(update={"review": verdict.review}), verdict
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


def _render_findings(
    verdict: ReviewVerdict, severity: Severity, heading: str
) -> list[str]:
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


def run_review(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Run the full per-role review pass; return ``(result, verdict)``.

    This is ``CodexExecutor.spawn()``'s Step 3 delegation target: capture the
    diff, select reviewers, materialize prompts, run the shared-deadline loop,
    and synthesize the typed result.
    """
    prepared = _prepare_review_pass(task, worktree, default_branch)
    documents, failures = run_codex_roles(
        runner=runner,
        worktree=worktree,
        roles=prepared.roles,
        prompts_by_role=prepared.prompts_by_role,
        model=model,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        session_id=session_id,
    )
    return synthesize_codex_review_result(
        task=task,
        worktree=worktree,
        documents=documents,
        failures=failures,
        diff=prepared.diff,
        reviewed_sha=prepared.reviewed_sha,
        session_id=session_id,
    )
