"""Repo-tracked ``.claude/{skills,commands,scripts}`` drift check for cw doctor.

Split out as a leaf module (#1514). The project-level ``.claude/skills``,
``.claude/commands`` and ``.claude/scripts`` trees checked into this source
repo are the ground truth a dispatched worker actually loads; the
``~/.claude/`` counterparts symlink into ``global-claude`` and are what an
*interactive* skill/command invocation resolves instead. ``.claude/scripts``
joined the tracked set after #2090, where a ``global-claude`` copy of
``prep_pr_state.py`` three versions behind this repo's silently stripped
``/prep-pr``'s gate-timeout ladder — exactly the drift this check exists to
name. This check re-derives the git-tracked file list via ``git ls-files``
inside the local source repo (resolved through ``_resolve_cw_source_path``),
maps each tracked path to its ``~/.claude/`` counterpart, and classifies each
into missing / content-differs / wrong-target symlink, aggregating the result
into one non-fatal ``CheckResult``. Detection only — no sync. Leaf module — no
cross-``doctor`` dependencies.
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
    # Counterpart is a symlink whose resolved target is NOT the tracked repo
    # path (a symlink resolving TO the tracked repo path is the expected
    # steady state under a symlink install and is not drift — see _classify).
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
_TRACKED_ROOTS = (".claude/skills", ".claude/commands", ".claude/scripts")

# Bounds the number of example paths named in the aggregated detail string,
# so a large drift set doesn't dump every filename (tests/test_doctor_skills_
# drift.py::test_examples_bounded_not_all_39).
_MAX_EXAMPLES = 3

# Timeout (seconds) for the git subprocess call.
_GIT_TIMEOUT = 10

# Tracked root -> repo-relative path of the installer's exclusion list for it.
# Entries name paths relative to the root (a bare ``ship-it.md`` for the flat
# commands dir; ``check_imports.py`` or ``utils/foo.py`` for scripts). Both
# scripts/install-skills.sh and this check read the same files — see #1535 —
# so a never-installed-globally entry is not reported as missing here.
_EXCLUSION_FILES = {
    ".claude/commands": "scripts/excluded-commands.txt",
    ".claude/scripts": "scripts/excluded-scripts.txt",
}


def _load_exclusions(source_path: Path, relpath: str) -> set[str]:
    """Read one installer exclusion file; fail open to empty set.

    Only a missing file fails open — the expected case when the file hasn't
    been created yet. Other OSError subtypes (permissions, I/O errors)
    propagate rather than silently disabling the ship-it.md-style exclusion.
    """
    try:
        lines = (source_path / relpath).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {line for line in lines if line}


def _is_excluded(repo_relpath: str, exclusions: dict[str, set[str]]) -> bool:
    """True when *repo_relpath* is listed in its root's installer exclusion set."""
    for root, names in exclusions.items():
        prefix = f"{root}/"
        if repo_relpath.startswith(prefix) and repo_relpath[len(prefix) :] in names:
            return True
    return False


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

    Symlink resolution comes first: a counterpart symlink whose resolved
    target equals the tracked repo path is the expected steady state under
    a symlink install and is not drift. A symlink resolving anywhere else
    (including a broken link) is drift, classified SYMLINK ("wrong target").
    """
    if counterpart.is_symlink():
        if counterpart.resolve() == repo_path.resolve():
            return None
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
    """Compare repo-tracked .claude/{skills,commands,scripts} against ~/.claude.

    Silent-skips (ok=True, warn=False) for registry/PyPI cw installs (no
    local source repo to diff against — same skip condition as
    _check_cw_version/_check_cw_deps), and when _CLAUDE_HOME/skills doesn't
    exist at all. Warns (ok=True, warn=True) when git ls-files fails, or when
    one or more tracked files are missing, content-differs, or is a symlink
    pointing somewhere other than the tracked repo path. Commands and scripts
    listed in the installer's exclusion files (_EXCLUSION_FILES) are never
    installed globally, so they are excluded from tracking before
    classification.
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

    exclusions = {
        root: _load_exclusions(source_path, relpath)
        for root, relpath in _EXCLUSION_FILES.items()
    }
    tracked = [relpath for relpath in tracked if not _is_excluded(relpath, exclusions)]

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
