"""Repo-configuration reads that ground a reviewer in this repo's own rules.

Three inputs share one Markdown H2 parser and one fail-safe TOML read: the
per-reviewer ``review-policy.md`` map, the ``CLAUDE.md`` Quality Gates section,
and ``[tool.ruff.lint]``'s opt-outs and complexity-threshold overrides. The last
two are assembled here into the ``## Repo Lint Configuration`` grounding block
(#1744).
"""

from __future__ import annotations

import logging
import tomllib
from typing import TYPE_CHECKING, NamedTuple

from cw.codex_review._context._agent_spec import _REVIEWER_ROLE_AGENT_FILES
from cw.codex_review._context._prompt_text import (
    _LINT_GROUNDING_INSTRUCTION,
    _PYLINT_THRESHOLD_CODES,
)
from cw.codex_review._context._util import _load_optional_text

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger(__name__)


class _RuffLintConfig(NamedTuple):
    """The repo's ``[tool.ruff.lint]`` opt-outs and pylint-threshold overrides."""

    ignore: tuple[str, ...]
    pylint_overrides: dict[str, int]


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


def _render_ruff_ignore_section(ignore: tuple[str, ...]) -> str:
    lines = [
        "## Globally Ignored Ruff Rules (pyproject.toml `[tool.ruff.lint].ignore`)"
    ]
    lines.extend(f"- {code}" for code in ignore)
    return "\n".join(lines)


def _render_pylint_thresholds_section(overrides: dict[str, int]) -> str:
    lines = ["## Complexity Thresholds (PLR0912 / PLR0915 / PLR0911)"]
    for key, code in _PYLINT_THRESHOLD_CODES.items():
        if key in overrides:
            lines.append(
                f"- {code} ({key}): {overrides[key]} (configured in pyproject.toml)"
            )
    return "\n".join(lines)


def _render_lint_grounding_block(
    ruff_config: _RuffLintConfig | None, quality_gates_text: str | None
) -> str | None:
    """Render the ``## Repo Lint Configuration`` grounding block (#1744).

    Returns ``None`` when there is nothing to ground against: no
    ``[tool.ruff.lint].ignore`` entries, no pylint-threshold overrides, and no
    ``CLAUDE.md`` Quality Gates text. Otherwise assembles the not-a-MUST_FIX
    instruction, the ignore list (when non-empty), repo-configured
    PLR0912/PLR0915/PLR0911 threshold overrides (when present), and the
    verbatim Quality Gates text. When no overrides exist, Quality Gates is the
    sole authoritative source for numeric thresholds.
    """
    ignore = ruff_config.ignore if ruff_config is not None else ()
    overrides = ruff_config.pylint_overrides if ruff_config is not None else {}
    if not ignore and not overrides and not quality_gates_text:
        return None
    parts = [_LINT_GROUNDING_INSTRUCTION]
    if ignore:
        parts.append(_render_ruff_ignore_section(ignore))
    if overrides:
        parts.append(_render_pylint_thresholds_section(overrides))
    if quality_gates_text:
        parts.append(f"## CLAUDE.md Quality Gates (verbatim)\n{quality_gates_text}")
    return "\n\n".join(parts)
