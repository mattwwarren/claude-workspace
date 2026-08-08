"""Reviewer role selection and prompt-context assembly for the codex-review package.

Every reviewer input — the authoritative agent spec, plan/ticket context,
project rubrics, per-reviewer policy, and sensitive-file hits — is read here by
``cw`` and inlined into each role's materialized prompt. Inlining is
unconditional: it is what makes a review pass reproducible and independent of
where ``.claude/*`` happens to live.

What is **not** unconditional is whether codex can read anything *else*. That
varies by runtime — a snap-confined install cannot reach ``bwrap`` and fails
closed, while a non-snap install on the same machine reads the worktree fine
(#1709). ``_capability`` probes which one this is, and
:func:`_select_output_instructions` picks the matching ``_OUTPUT_INSTRUCTIONS``
variant, so a capable runtime is no longer told it cannot read.

Also owns file categorization, the small-/large-scope reviewer-selection
tables, and codex output-document parsing. Consumed by ``core`` (prompt
assembly) and ``_roles`` (doc parsing).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import tomllib
from typing import TYPE_CHECKING, NamedTuple

import yaml

from cw.codex_review._capability import _probe_filesystem_capability
from cw.codex_review._diff import _capture_diff
from cw.local_runner import resolve_tier
from cw.models import CONTEXT_JSON_RELATIVE_PATH
from cw.review_findings import ReviewerFindingsDocument

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.codex_review._capability import _CodexFilesystemCapability
    from cw.codex_runner import CodexRunner
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

# #1744: grounds reviewers in the repo's actual ruff opt-outs and complexity
# thresholds so they stop raising MUST_FIX findings against rules the repo
# has explicitly ignored, or against a complexity metric they've misread
# (PLR0915 gates statements, not lines — the exact #1729 failure mode).
_LINT_GROUNDING_INSTRUCTION = (
    "REPO LINT CONFIGURATION — GROUND FINDINGS IN THE REPO'S ACTUAL RUFF SETUP\n\n"
    "A finding based solely on enforcing a ruff rule this repo has explicitly "
    "opted out of below, or on treating an unmodified ruff default as if it "
    "were a repo-configured threshold, is not a MUST_FIX — downgrade it or "
    "drop it. An ignored ruff rule does not shield a concrete security or "
    "correctness failure: report such a failure as MUST_FIX when warranted, "
    "even when a related rule such as S603 is ignored. "
    "In particular, PLR0915 (too-many-statements) gates on the number of "
    "STATEMENTS in a function body, not the number of lines — a long "
    "function built from short, simple statements can sit well under the "
    "statement threshold while spanning many lines; do not flag line count "
    "as if it were the gated metric."
)

# ruff's built-in pylint-refactor defaults (max-branches/max-statements/
# max-returns). These are NOT expressed in pyproject.toml unless a
# [tool.ruff.lint.pylint] override is present — the renderer must label them
# as ruff defaults, not as repo-configured values, whenever no override
# exists for that key.
_DEFAULT_PYLINT_THRESHOLDS: dict[str, tuple[str, int]] = {
    "max-branches": ("PLR0912", 12),
    "max-statements": ("PLR0915", 50),
    "max-returns": ("PLR0911", 6),
}
_RUFF_DEFAULT_LABEL = "ruff default (not overridden in pyproject.toml)"

# The one sentence that differs between the two `_OUTPUT_INSTRUCTIONS`
# variants, named so the prompt text and its regression tests cannot drift
# apart (R8: each variant must carry its own marker and NOT the other's).
_INLINED_ONLY_MARKER = "do not rely on filesystem access"
_CAPABLE_ONLY_MARKER = (
    "read-only filesystem access to the repository worktree is available"
)

_INLINED_ONLY_PREAMBLE = (
    "## Output\n"
    "Evaluate the diff strictly from the material inlined above — "
    f"{_INLINED_ONLY_MARKER}. "
)

_CAPABLE_PREAMBLE = (
    "## Output\n"
    f"This runtime was probed and confirmed capable: {_CAPABLE_ONLY_MARKER} to "
    "you, and you MAY use it when it makes a finding stronger — searching for "
    "a changed symbol's other consumers, checking prior art before calling "
    "something a new abstraction, or verifying a regression repo-wide. Write "
    "access is neither offered nor possible. The material inlined above "
    "remains the authoritative context; reading is a supplement to it, never a "
    "replacement for evaluating the diff. "
)

# Schema/degraded/escalation rules — identical in both variants by
# construction, so a capability change can never quietly alter the contract
# codex's output is validated against.
_OUTPUT_SCHEMA_RULES = (
    "Emit a single JSON object conforming to the provided "
    "ReviewerFindingsDocument schema to the output file (`-o`): `reviewer_role`, "
    "`status` (ok/degraded/failed), `detail`, and a `findings` array. When "
    'returning `status="ok"` with an empty `findings` array, `detail` MUST '
    "briefly state what was checked (a blank `detail` on that combination is "
    "rejected by the schema) — do not emit the trivial empty case without "
    "saying what you verified. If a rubric-mandated check from the inlined "
    "agent specification could not actually be performed in this "
    'environment, use `status="degraded"` (naming the unperformed check in '
    '`detail`) rather than silently reporting `"ok"`. Every '
    "finding's `evidence` MUST be a verbatim substring of the claimed file's "
    "changed lines. Report no prose outside the JSON object."
)

_OUTPUT_SPEC_PRECEDENCE = (
    "The inlined Agent Specification section above was authored for a "
    "different execution environment (a tool-using Claude subagent). Any "
    "tool-invocation syntax or search/verification precondition it names is "
    "advisory here, not blocking — treat it as guidance for what to look for, "
    "not as a gate on whether to report. If a finding is groundable in the "
    "inlined diff but the spec's own verification step could not be "
    "performed in this environment, report the finding anyway: emit it at "
    '`confidence: "LOW"` and name the unperformed check explicitly in the '
    "finding's `consequence` field (not `evidence`, which must stay a clean "
    "verbatim quote from the diff). Never suppress a diff-groundable finding "
    "solely because a verification precondition from that spec went "
    "unperformed. Likewise, the spec's own prose output conventions — "
    "including any literal sentinel value it defines for a no-findings "
    "result — are void for this invocation; this instruction block's JSON "
    "ReviewerFindingsDocument contract governs exclusively."
)

_OUTPUT_INSTRUCTIONS_INLINED_ONLY = (
    f"{_INLINED_ONLY_PREAMBLE}{_OUTPUT_SCHEMA_RULES}\n\n{_OUTPUT_SPEC_PRECEDENCE}"
)

_OUTPUT_INSTRUCTIONS_CAPABLE = (
    f"{_CAPABLE_PREAMBLE}{_OUTPUT_SCHEMA_RULES}\n\n{_OUTPUT_SPEC_PRECEDENCE}"
)

# Back-compat alias: byte-identical to the single pre-#1709 variant, so the
# #1548 regression-lock test keeps asserting against the same string.
_OUTPUT_INSTRUCTIONS = _OUTPUT_INSTRUCTIONS_INLINED_ONLY


def _select_output_instructions(capable: bool) -> str:
    """Pick the output-instruction variant matching this runtime's capability."""
    return (
        _OUTPUT_INSTRUCTIONS_CAPABLE if capable else _OUTPUT_INSTRUCTIONS_INLINED_ONLY
    )


_CODEX_OUTPUT_FORMAT_ROLES: frozenset[str] = frozenset(
    {
        "Architecture Reviewer",
        "Test Reviewer",
        "Performance Reviewer",
        "API Contract Validator",
        "Deployment Reviewer",
    }
)

_CODEX_SEVERITY_TAXONOMY = (
    "## Severity Taxonomy (inlined — the agent specification above references "
    "`output-formats.md`, which is unreachable in this environment)\n"
    "The categorization above maps onto the JSON `severity` field as follows: "
    '"(Critical)" -> `MUST_FIX` (must fix before merge: security, correctness, '
    'test failures); "(Major)" -> `SHOULD_FIX` (should fix: performance, '
    'maintainability, technical debt); "(Low)" -> `NIT` (nice to fix: style, '
    "minor improvements). Only report actionable problems — no praise, "
    "summaries, or fluff."
)

_CODEX_TONE_GUIDE_SUPPLEMENT = (
    "## Tone Conventions (inlined — the agent specification above references "
    "`review-tone-guide.md`, which is unreachable in this environment)\n"
    "Include specific file paths and line numbers, clear problem descriptions, "
    "concrete fixes, and impact/why it matters. Do not include praise, general "
    'assessments ("code is mostly good"), or hedging ("maybe", "might want to '
    'consider"). No Praise, No Summaries, No Fluff — reviews are technical '
    "specifications, not performance evaluations."
)

_CODEX_TESTING_CHECKLIST_SUPPLEMENT = (
    "## Testing Checklist (inlined — the agent specification above references "
    "`testing-philosophy.md`, which is unreachable in this environment)\n"
    "When reviewing tests, check: AAA Pattern (clear Arrange-Act-Assert), "
    "Independence (tests don't depend on each other), Naming, Single Concept, "
    "Can Fail, Edge Cases, Error Cases, Mocking (external deps mocked "
    "appropriately, not over-mocked), Async Handled, Fast, Deterministic, "
    "Clean Up."
)


def _codex_output_format_supplement(role: str) -> str | None:
    """Return inlined replacement content for *role*'s dangling doc references,
    or ``None`` if *role*'s spec carries no such reference (#1548).

    Why: "Code Quality Reviewer" gets only the tone-guide supplement, never
    the severity taxonomy, even though its own .claude/agents/code-reviewer.md
    dangles a reference to output-formats.md like the other five roles. Its
    Output Format section already spells out "(Must Fix)"/"(Should Fix)"/
    "(Nice to Fix)" inline (code-reviewer.md:181,187,193), so the shared
    Critical/Major/Low taxonomy translation would be redundant there — unlike
    the other five roles, whose specs have no inline categorization at all.
    """
    if role == "Code Quality Reviewer":
        return _CODEX_TONE_GUIDE_SUPPLEMENT
    if role not in _CODEX_OUTPUT_FORMAT_ROLES:
        return None
    parts = [_CODEX_SEVERITY_TAXONOMY]
    if role == "Test Reviewer":
        parts.append(_CODEX_TESTING_CHECKLIST_SUPPLEMENT)
    return "\n\n".join(parts)


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


class _RuffLintConfig(NamedTuple):
    """The repo's ``[tool.ruff.lint]`` opt-outs and pylint-threshold overrides."""

    ignore: tuple[str, ...]
    pylint_overrides: dict[str, int]


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


def _parse_markdown_h2_sections(text: str) -> list[tuple[str, str]]:
    """Parse Markdown H2 sections into ``(heading, body)`` pairs."""
    sections: list[tuple[str, str]] = []
    heading: str | None = None
    body: list[str] = []

    def _commit() -> None:
        if heading is None:
            return
        sections.append((heading, "\n".join(body).strip()))

    for line in text.splitlines():
        if line.startswith("## "):
            _commit()
            heading = line[3:].strip()
            body = []
        elif heading is not None:
            body.append(line)
    _commit()
    return sections


def _parse_review_policy(text: str) -> dict[str, str]:
    """Parse ``review-policy.md`` H2 sections into a role-keyed map.

    Warn-and-skip: an H2 heading that is not a known reviewer name is logged
    and dropped; the parse never raises.
    """
    policy: dict[str, str] = {}
    for heading, body in _parse_markdown_h2_sections(text):
        if heading not in _REVIEWER_ROLE_AGENT_FILES:
            _log.warning(
                'review-policy.md: unmatched section "%s" — skipped (typo?)',
                heading,
            )
            continue
        policy[heading] = body
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


def _load_ruff_lint_config(worktree: Path) -> _RuffLintConfig | None:
    """Read ``[tool.ruff.lint]`` from *worktree*'s ``pyproject.toml`` (#1744).

    Fails safe to ``None`` on a missing file or malformed TOML — same
    ``tomllib.load`` + fail-safe idiom as ``cw.doctor.versions``' source-version
    read. A valid TOML file with no ``[tool.ruff.lint]`` section at all still
    returns a ``_RuffLintConfig`` with empty ``ignore``/``pylint_overrides``:
    absence of ruff-lint config is a fact about the repo, not a read failure.
    """
    try:
        with (worktree / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError, OSError):
        return None
    lint = data.get("tool", {}).get("ruff", {}).get("lint", {})
    ignore = lint.get("ignore", [])
    pylint = lint.get("pylint", {})
    return _RuffLintConfig(ignore=tuple(ignore), pylint_overrides=dict(pylint))


def _extract_markdown_section(text: str, heading: str) -> str | None:
    """Extract the body of *text*'s ``## {heading}`` H2 section, or ``None``.

    Uses the same H2 parser as ``review-policy.md`` so section boundaries have
    one implementation.
    """
    return next(
        (
            body
            for section_heading, body in _parse_markdown_h2_sections(text)
            if section_heading == heading
        ),
        None,
    )


def _load_claude_md_quality_gates(worktree: Path) -> str | None:
    """Return *worktree*'s ``CLAUDE.md`` ``## Quality Gates`` section, verbatim."""
    text = _load_optional_text(worktree / "CLAUDE.md")
    if text is None:
        return None
    return _extract_markdown_section(text=text, heading="Quality Gates")


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


def _render_ruff_ignore_section(ignore: tuple[str, ...]) -> str:
    lines = [
        "## Globally Ignored Ruff Rules (pyproject.toml `[tool.ruff.lint].ignore`)"
    ]
    lines.extend(f"- {code}" for code in ignore)
    return "\n".join(lines)


def _render_pylint_thresholds_section(overrides: dict[str, int]) -> str:
    lines = ["## Complexity Thresholds (PLR0912 / PLR0915 / PLR0911)"]
    for key, (code, default) in _DEFAULT_PYLINT_THRESHOLDS.items():
        if key in overrides:
            value, label = overrides[key], "configured in pyproject.toml"
        else:
            value, label = default, _RUFF_DEFAULT_LABEL
        lines.append(f"- {code} ({key}): {value} ({label})")
    return "\n".join(lines)


def _render_lint_grounding_block(
    ruff_config: _RuffLintConfig | None, quality_gates_text: str | None
) -> str | None:
    """Render the ``## Repo Lint Configuration`` grounding block (#1744).

    Returns ``None`` when there is nothing to ground against: no
    ``[tool.ruff.lint].ignore`` entries, no pylint-threshold overrides, and no
    ``CLAUDE.md`` Quality Gates text. Otherwise assembles the not-a-MUST_FIX
    instruction, the ignore list (when non-empty), the PLR0912/PLR0915/PLR0911
    thresholds (repo-configured where overridden, else the documented ruff
    defaults), and the verbatim Quality Gates text.
    """
    ignore = ruff_config.ignore if ruff_config is not None else ()
    overrides = ruff_config.pylint_overrides if ruff_config is not None else {}
    if not ignore and not overrides and not quality_gates_text:
        return None
    parts = [_LINT_GROUNDING_INSTRUCTION]
    if ignore:
        parts.append(_render_ruff_ignore_section(ignore))
    parts.append(_render_pylint_thresholds_section(overrides))
    if quality_gates_text:
        parts.append(f"## CLAUDE.md Quality Gates (verbatim)\n{quality_gates_text}")
    return "\n\n".join(parts)


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
    capable: bool = False,
    lint_grounding: str | None = None,
) -> str:
    """Materialize one reviewer's full prompt, inlining every needed section.

    *capable* selects which ``_OUTPUT_INSTRUCTIONS`` variant closes the prompt
    (#1709). It defaults to ``False`` — the pre-#1709 text — purely so this
    function's variant-agnostic unit tests stay byte-identical; the sole
    production caller (:func:`_prepare_review_pass`) always passes the probed
    value explicitly, so the default never fires in production.

    *lint_grounding* (#1744) is the rendered repo-lint-configuration block —
    the repo's ruff opt-outs and complexity thresholds — so reviewers stop
    raising MUST_FIX findings against rules the repo has explicitly ignored.
    Same safe-default convention as *capable*: defaults to ``None`` for the
    variant-agnostic unit tests; :func:`_prepare_review_pass` always passes it
    explicitly.
    """
    parts = [
        f"# Reviewer Role: {role}",
        f"## Agent Specification\n{agent_spec_text}",
    ]
    supplement = _codex_output_format_supplement(role)
    if supplement:
        parts.append(supplement)
    if ticket_text:
        parts.append(f"## Ticket Context\n{ticket_text}")
    if plan_text:
        parts.append(f"## Approved Plan\n{plan_text}")
    if project_rubrics:
        parts.append(f"## Project Rubrics\n{project_rubrics}")
    if repo_policy_section:
        parts.append(f"## Repo Policy for {role}\n{repo_policy_section}")
    if lint_grounding:
        parts.append(f"## Repo Lint Configuration\n{lint_grounding}")
    if sensitive_hits:
        parts.append(_render_sensitive_block(sensitive_hits))
    parts.append("## Changed Files\n" + "\n".join(changed_files))
    parts.append(f"## Diff\n{diff.text}")
    parts.append(_select_output_instructions(capable))
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
    """

    roles: list[str]
    prompts_by_role: dict[str, str]
    diff: CapturedDiff
    reviewed_sha: str
    capability: _CodexFilesystemCapability


def _prepare_review_pass(
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    *,
    runner: CodexRunner,
    session_id: str,
) -> _ReviewPassInputs:
    """Assemble one review pass's inputs: capture diff, select roles, build prompts.

    Extracted from ``run_review``'s former input-assembly body (everything
    before ``run_codex_roles`` was called). Before #1709 it had no side effects
    beyond the read-only git/\u200bfilesystem reads it already performed. Shared by
    ``run_review`` and ``cw.codex_fix_loop``'s per-cycle re-review (#1392).

    Lives here (not ``core.py``) so it stays co-located with
    :func:`_load_optional_text` alongside its other bare-name callers — a test
    patches ``_load_optional_text`` via module-object ``setattr`` on this
    module, which only intercepts same-module bare-name calls.

    ``runner``/``session_id`` (#1709) drive the filesystem-capability probe,
    which is what changed that: on a cold fingerprint cache it spends one real
    ``codex exec`` round-trip and writes the verdict to disk. Every subsequent
    call — notably the fix loop's per-cycle re-review — is a cache hit that
    runs nothing, which is why the probe lives here rather than at each call
    site.
    """
    capability = _probe_filesystem_capability(runner=runner, session_id=session_id)
    diff, reviewed_sha, changed_files = _capture_diff(worktree, default_branch)
    scope_tier = resolve_tier(task.scope_hint)
    categories = _categorize_changed_files(changed_files)
    sensitive_hits = _load_sensitive_hits(worktree, changed_files, scope_tier)
    repo_policy = _load_review_policy(worktree, scope_tier)
    project_rubrics = _load_optional_text(worktree / ".claude" / "review-extras.md")
    plan_text, ticket_text = _load_ticket_context(worktree)
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
            capable=capability.capable,
            lint_grounding=lint_grounding,
        )
        for role in roles
    }
    return _ReviewPassInputs(
        roles=roles,
        prompts_by_role=prompts_by_role,
        diff=diff,
        reviewed_sha=reviewed_sha,
        capability=capability,
    )
