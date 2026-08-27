"""Sensitive-file registry reading, changed-file matching, and block rendering.

Matches a pass's changed files against the repo's sensitive-files registry and
renders the elevated-scrutiny block that result inlines into every reviewer
prompt (review.md Step 1.6). The small- and large-scope tiers read different
paths and match differently; that divergence is authoritative and lives in
:func:`_load_sensitive_hits`.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, NamedTuple

import yaml

from cw.codex_review._context._util import _load_optional_text

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_SENSITIVE_HEADER = (
    "SENSITIVE FILES TOUCHED — APPLY ELEVATED SCRUTINY\n\n"
    "These files are high blast-radius. Apply maximum scrutiny for: unintended "
    "scope changes, missing auth checks, new external write paths, error "
    "handling gaps, cross-org data leakage, and regression risk."
)


class _SensitiveHit(NamedTuple):
    """One changed file that matched a sensitive-files registry entry."""

    path: str
    category: str
    reason: str


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


def _render_sensitive_block(hits: list[_SensitiveHit]) -> str:
    """Render the elevated-scrutiny sensitive-files block (review.md Step 1.6)."""
    lines = [_SENSITIVE_HEADER, "", "Touched sensitive files:"]
    lines.extend(f"- {h.path} ({h.category}) — {h.reason}" for h in hits)
    return "\n".join(lines)
