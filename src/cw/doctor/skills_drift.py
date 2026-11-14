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

import importlib.metadata
import importlib.resources
import shlex
import subprocess as _sp
import tomllib
from enum import StrEnum
from pathlib import Path

from cw._git import run_git
from cw.doctor._shared import CheckResult
from cw.doctor.versions import (
    _CW_PACKAGE_NAME,
    _CW_REPO_URL,
    CwInstall,
    _classify_cw_install,
    _InstallKind,
    _resolve_cw_source_path,
)


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

# How many parents above a command-doc link target are searched for the
# checkout's pyproject.toml (#3579). Bounded so a stray link cannot walk to /.
_CHECKOUT_WALK_DEPTH = 6

# Operator-facing statement that the install did not bring skills or scripts.
_RELEASE_NOTICE = (
    "uv tool install does not install cw's skills, commands or scripts; "
    "to install them run:"
)

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
        proc = run_git(
            ["-C", str(source_path), "ls-files", *_TRACKED_ROOTS],
            capture_output=True,
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

    Non-local (git or registry) cw installs have no source repo to diff
    against, so they are checked by ``_check_release_install_skills`` instead:
    the wheel's packaged command docs and the version of any checkout the
    links point into (#3579). Detection only. Silent-skips
    (ok=True, warn=False) when _CLAUDE_HOME/skills doesn't exist at all.
    Warns (ok=True, warn=True) when git ls-files fails, or when one or more
    tracked files are missing, content-differs, or is a symlink
    pointing somewhere other than the tracked repo path. Commands and scripts
    listed in the installer's exclusion files (_EXCLUSION_FILES) are never
    installed globally, so they are excluded from tracking before
    classification.
    """
    source_path = _resolve_cw_source_path()
    if isinstance(source_path, CheckResult):
        return _check_release_install_skills(_classify_cw_install())

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


def _packaged_commands_dir() -> Path | None:
    """Return the wheel's packaged command-doc dir, or None when absent/empty.

    Duplicates the three-line lookup in ``cw.codex_plan._prompt`` on purpose:
    importing ``cw.codex_plan`` would load the codex executor stack into
    ``cw doctor`` (#3579).
    """
    candidate = Path(str(importlib.resources.files("cw").joinpath("data", "commands")))
    if candidate.is_dir() and any(candidate.glob("*.md")):
        return candidate
    return None


def _classify_release_doc(ref: Path, counterpart: Path) -> _DriftCategory | None:
    """Classify one packaged doc against its ~/.claude/commands counterpart.

    Unlike ``_classify``, a symlink counterpart is compared by bytes: a release
    install's links point into a checkout, which is the expected steady state,
    so their target path is not drift. ``Path.exists`` follows symlinks, so a
    dangling link reads as MISSING.
    """
    if not counterpart.exists():
        return _DriftCategory.MISSING
    if ref.read_bytes() != counterpart.read_bytes():
        return _DriftCategory.DIFFERS
    return None


def _linked_checkout_version(counterpart: Path) -> str | None:
    """Return the ``claude-workspace`` version of the checkout a link targets.

    Only symlinks carry a checkout. Walks up at most ``_CHECKOUT_WALK_DEPTH``
    parents from the resolved target to the nearest ``pyproject.toml``. Returns
    None when nothing usable is found or when the file cannot be read or parsed.
    """
    if not counterpart.is_symlink():
        return None
    try:
        target = counterpart.resolve()
        for parent in target.parents[:_CHECKOUT_WALK_DEPTH]:
            pyproject = parent / "pyproject.toml"
            if not pyproject.is_file():
                continue
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project")
            if not isinstance(project, dict) or project.get("name") != _CW_PACKAGE_NAME:
                return None
            version = project.get("version")
            return version if isinstance(version, str) else None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    return None


def _examples(names: list[str]) -> str:
    """Render at most ``_MAX_EXAMPLES`` sorted names, then ``, +N more``."""
    ordered = sorted(names)
    shown = ", ".join(ordered[:_MAX_EXAMPLES])
    extra = len(ordered) - _MAX_EXAMPLES
    return f"{shown}, +{extra} more" if extra > 0 else shown


def _remediation_cmd(install: CwInstall, version: str) -> str:
    """Return the clone-at-revision plus install-skills command for this install."""
    url = (
        install.url
        if install.kind is _InstallKind.GIT and install.url
        else _CW_REPO_URL
    )
    revision = install.commit_id or install.requested_revision or f"v{version}"
    return (
        f"git clone {shlex.quote(url)} && cd claude-workspace && "
        f"git checkout {shlex.quote(revision)} && ./scripts/install-skills.sh"
    )


def _skip(detail: str) -> CheckResult:
    return CheckResult(_CHECK_NAME, ok=True, warn=False, detail=detail)


def _release_findings(
    version: str, docs: list[Path], claude_commands: Path
) -> tuple[list[str], list[str], list[tuple[str, str]], int]:
    """Classify each packaged doc into missing, differs or version-mismatched."""
    missing: list[str] = []
    differs: list[str] = []
    mismatch: list[tuple[str, str]] = []
    for ref in docs:
        counterpart = claude_commands / ref.name
        category = _classify_release_doc(ref, counterpart)
        if category == _DriftCategory.MISSING:
            missing.append(ref.name)
        elif category == _DriftCategory.DIFFERS:
            differs.append(ref.name)
        else:
            linked = _linked_checkout_version(counterpart)
            if linked is not None and linked != version:
                mismatch.append((ref.name, linked))
    return missing, differs, mismatch, len(docs)


def _check_release_install_skills(install: CwInstall) -> CheckResult:
    """Check a git or registry install's packaged command docs and link versions.

    Partial by design: a wheel carries only four command docs, and skills and
    scripts cannot be verified without a checkout. Findings warn and name the
    command that installs the rest; nothing is synced (#3579).
    """
    if install.kind is _InstallKind.MALFORMED:
        return _skip("malformed direct_url.json; skipping source check")
    try:
        version = importlib.metadata.version(_CW_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return _skip("package metadata not found; skipping skills check")

    cmd = _remediation_cmd(install, version)
    packaged = _packaged_commands_dir()
    if packaged is None:
        return CheckResult(
            _CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"cannot verify command docs: installed cw {version} carries no "
                f"packaged command docs. {_RELEASE_NOTICE} {cmd}"
            ),
        )

    claude_commands = _CLAUDE_HOME / "commands"
    missing, differs, mismatch, total = _release_findings(
        version, sorted(packaged.glob("*.md")), claude_commands
    )
    findings: list[str] = []
    if missing:
        findings.append(
            f"{len(missing)} of {total} shipped command docs missing from "
            f"{claude_commands} ({_examples(missing)})"
        )
    if differs:
        findings.append(
            f"{len(differs)} of {total} shipped command docs differ from the copy "
            f"in installed cw {version} ({_examples(differs)})"
        )
    if mismatch:
        linked = [f"{name}@{linked_version}" for name, linked_version in mismatch]
        findings.append(
            f"{len(mismatch)} of {total} command doc links point at a checkout whose "
            f"version differs from installed cw {version} ({_examples(linked)})"
        )
    if not (_CLAUDE_HOME / "skills").exists():
        findings.append(f"{_CLAUDE_HOME / 'skills'} not found")

    if not findings:
        return CheckResult(
            _CHECK_NAME,
            ok=True,
            warn=False,
            detail=(
                f"{total} of {total} shipped command docs match installed cw "
                f"{version}; "
                "skills and scripts cannot be verified without a checkout"
            ),
        )
    return CheckResult(
        _CHECK_NAME,
        ok=True,
        warn=True,
        detail=f"{'; '.join(findings)}. {_RELEASE_NOTICE} {cmd}",
    )
