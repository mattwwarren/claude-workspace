"""Repo-tracked ``.claude/skills``/``.claude/commands`` drift check for cw doctor.

Split out as a leaf module (#1514). The project-level ``.claude/skills`` and
``.claude/commands`` trees checked into this source repo are the ground truth
a dispatched worker actually loads; ``~/.claude/skills``/``~/.claude/commands``
symlink into ``global-claude`` and are what an *interactive* skill/command
invocation resolves instead. This check re-derives the git-tracked file list
via ``git ls-files`` inside the local source repo (resolved through
``_resolve_cw_source_path``), maps each tracked path to its ``~/.claude/``
counterpart, and classifies each into missing / content-differs /
counterpart-is-a-symlink, aggregating the result into one non-fatal
``CheckResult``. Detection only — no sync. Leaf module — no cross-``doctor``
dependencies.
"""

from __future__ import annotations

import subprocess as _sp
from enum import StrEnum
from pathlib import Path

from cw.doctor._shared import CheckResult
from cw.doctor.versions import _resolve_cw_source_path


class _DriftCategory(StrEnum):
    """One tracked file's drift classification against its _CLAUDE_HOME counterpart."""

    MISSING = "missing"
    DIFFERS = "differs"
    SYMLINK = "symlink"


# Home-tree root that repo-tracked .claude/skills and .claude/commands are
# compared against — a module-level Path.home()-derived constant, patched in
# tests via monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", ...).
_CLAUDE_HOME = Path.home() / ".claude"

# Check name for the repo-tracked-vs-global skills/commands drift detector.
_CHECK_NAME = "skills-commands-drift"

# Repo-relative roots whose git-tracked contents are compared against
# _CLAUDE_HOME. Each entry's final path segment is also its _CLAUDE_HOME
# subdirectory name (".claude/skills" -> _CLAUDE_HOME / "skills").
_TRACKED_ROOTS = (".claude/skills", ".claude/commands")

# Bounds the number of example paths named in the aggregated detail string,
# so a large drift set doesn't dump every filename (tests/test_doctor_skills_
# drift.py::test_examples_bounded_not_all_39).
_MAX_EXAMPLES = 3

# Timeout (seconds) for the git subprocess call.
_GIT_TIMEOUT = 10


def _git_tracked_paths(source_path: Path) -> list[str] | None:
    """Return git-tracked paths under _TRACKED_ROOTS in *source_path*.

    Returns None on git-run failure (binary missing, non-zero exit, timeout).
    """
    try:
        proc = _sp.run(
            ["git", "-C", str(source_path), "ls-files", *_TRACKED_ROOTS],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT,
        )
    except (FileNotFoundError, _sp.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line]


def _counterpart_path(repo_relpath: str) -> Path:
    """Map a repo-relative tracked path to its _CLAUDE_HOME counterpart.

    ``.claude/skills/<rel>`` -> ``_CLAUDE_HOME / "skills" / <rel>`` and
    ``.claude/commands/<rel>`` -> ``_CLAUDE_HOME / "commands" / <rel>`` via a
    plain prefix strip.
    """
    for root in _TRACKED_ROOTS:
        prefix = f"{root}/"
        if repo_relpath.startswith(prefix):
            leaf_dir = root.rsplit("/", 1)[-1]
            rel = repo_relpath[len(prefix) :]
            return _CLAUDE_HOME / leaf_dir / rel
    # git ls-files was scoped to _TRACKED_ROOTS, so this shouldn't happen —
    # degrade gracefully rather than raise.
    return _CLAUDE_HOME / repo_relpath


def _classify(repo_path: Path, counterpart: Path) -> _DriftCategory | None:
    """Classify one tracked file's drift state, or None when it matches.

    Leaf-symlink check comes first — a symlinked counterpart is a special
    case of "exists" that still warrants a flag before we compare bytes.
    """
    if counterpart.is_symlink():
        return _DriftCategory.SYMLINK
    if not counterpart.exists():
        return _DriftCategory.MISSING
    if repo_path.read_bytes() != counterpart.read_bytes():
        return _DriftCategory.DIFFERS
    return None


def _build_detail(
    total: int, missing: list[str], differs: list[str], symlink: list[str]
) -> str:
    """Build the aggregated, example-bounded detail string for a drifting run."""
    parts = []
    if missing:
        parts.append(f"{len(missing)} missing")
    if differs:
        parts.append(f"{len(differs)} differ")
    if symlink:
        parts.append(f"{len(symlink)} symlink")
    examples = (missing + differs + symlink)[:_MAX_EXAMPLES]
    return f"{', '.join(parts)} of {total} tracked (e.g. {', '.join(examples)})"


def _check_skills_commands_drift() -> CheckResult:
    """Compare repo-tracked .claude/skills+commands against ~/.claude counterparts.

    Silent-skips (ok=True, warn=False) for registry/PyPI cw installs (no
    local source repo to diff against — same skip condition as
    _check_cw_version/_check_cw_deps), and when _CLAUDE_HOME/skills doesn't
    exist at all. Warns (ok=True, warn=True) when git ls-files fails, or when
    one or more tracked files are missing, content-differs, or have an
    abnormal per-file symlink on the global side.
    """
    source_path = _resolve_cw_source_path()
    if isinstance(source_path, CheckResult):
        return CheckResult(
            _CHECK_NAME,
            ok=source_path.ok,
            warn=source_path.warn,
            detail=source_path.detail,
        )

    claude_skills = _CLAUDE_HOME / "skills"
    if not claude_skills.exists():
        return CheckResult(
            _CHECK_NAME,
            ok=True,
            warn=False,
            detail=f"{claude_skills} not found; skipping drift check",
        )

    tracked = _git_tracked_paths(source_path)
    if tracked is None:
        return CheckResult(
            _CHECK_NAME,
            ok=True,
            warn=True,
            detail=f"could not list git-tracked files under {source_path}",
        )

    missing: list[str] = []
    differs: list[str] = []
    symlink: list[str] = []
    for relpath in tracked:
        category = _classify(source_path / relpath, _counterpart_path(relpath))
        if category == _DriftCategory.MISSING:
            missing.append(relpath)
        elif category == _DriftCategory.DIFFERS:
            differs.append(relpath)
        elif category == _DriftCategory.SYMLINK:
            symlink.append(relpath)

    if not missing and not differs and not symlink:
        return CheckResult(
            _CHECK_NAME,
            ok=True,
            warn=False,
            detail=f"{len(tracked)}/{len(tracked)} match",
        )

    return CheckResult(
        _CHECK_NAME,
        ok=True,
        warn=True,
        detail=_build_detail(
            total=len(tracked), missing=missing, differs=differs, symlink=symlink
        ),
    )
