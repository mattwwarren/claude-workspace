"""Read the engine manifest from the default branch (RFC 0018 A4, #3216).

The codex engine round reads ``cw-engine.json`` from the skill directory at the
default branch's commit, never from the worktree, so a branch that edits the
manifest cannot change which roles run (the same trust rule as
``cw.review_engine.skill``). :func:`read_default_branch_manifest` resolves the
skill, archives its directory from git objects into a temporary directory under
the ``materialize`` extraction caps, and runs the bounded loader there.

It never fetches and never touches the worktree. This is a heavy module: it
imports ``materialize`` and ``cw.review_convergence``, so it is imported by
submodule path only.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cw._git import run_git
from cw.exceptions import CwError
from cw.review_convergence import resolve_default_branch_ref
from cw.review_engine.constants import (
    CAUSE_DEFAULT_BRANCH_UNRESOLVED,
    CAUSE_MATERIALIZE_FAILED,
    CAUSE_NO_CODEX_MANIFEST,
    CAUSE_SKILL_UNREADABLE,
    ENGINE_MANIFEST_FILENAME,
)
from cw.review_engine.materialize import MaterializeError, _extract_archive
from cw.review_engine.skill import SkillResolutionError, resolve_skill
from cw.review_engine_manifest_io import (
    EngineManifestError,
    LoadedEngineManifest,
    load_engine_manifest,
    manifest_refusal_cause,
)

if TYPE_CHECKING:
    from cw.review_engine.skill import ResolvedSkill


class ManifestStatus(StrEnum):
    """States returned by the default-branch manifest reader."""

    LOADED = "loaded"
    ABSENT = "absent"
    INVALID = "invalid"
    UNRESOLVABLE = "unresolvable"


@dataclass(frozen=True)
class ManifestRead:
    """The outcome of reading a skill's manifest at the default branch.

    ``cause`` is ``""`` when loaded, ``no_codex_manifest`` when absent, the
    ``manifest_refusal_cause`` when invalid, and one of
    ``default_branch_unresolved``, ``skill_not_found``, ``skill_unreadable`` or
    ``materialize_failed`` when unresolvable. ``detail`` is one human line.
    """

    status: ManifestStatus
    cause: str
    detail: str
    loaded: LoadedEngineManifest | None = None
    error: EngineManifestError | None = None


def _unresolvable(cause: str, detail: str) -> ManifestRead:
    return ManifestRead(status=ManifestStatus.UNRESOLVABLE, cause=cause, detail=detail)


def _absent(detail: str) -> ManifestRead:
    return ManifestRead(
        status=ManifestStatus.ABSENT, cause=CAUSE_NO_CODEX_MANIFEST, detail=detail
    )


def _archive(repo: Path, commit: str, skill: ResolvedSkill) -> bytes:
    result = cast(
        "subprocess.CompletedProcess[bytes]",
        run_git(
            ["archive", "--format=tar", f"{commit}:{skill.real_path}"],
            cwd=repo,
            capture_output=True,
            check=True,
            text=False,
        ),
    )
    return result.stdout


def _load_from_archive(data: bytes, skill_name: str) -> ManifestRead:
    with tempfile.TemporaryDirectory(prefix="cw-engine-manifest-") as tmp:
        directory = Path(tmp)
        _extract_archive(data, directory)
        manifest_path = directory / ENGINE_MANIFEST_FILENAME
        if not manifest_path.is_file():
            return _absent(f"{skill_name} has no {ENGINE_MANIFEST_FILENAME}")
        try:
            loaded = load_engine_manifest(manifest_path)
        except EngineManifestError as exc:
            return ManifestRead(
                status=ManifestStatus.INVALID,
                cause=manifest_refusal_cause(exc),
                detail=f"{exc.reason}: {exc.field_path or '-'}",
                error=exc,
            )
    return ManifestRead(
        status=ManifestStatus.LOADED, cause="", detail="", loaded=loaded
    )


def read_default_branch_manifest(
    repo: Path, default_branch: str, skill: str
) -> ManifestRead:
    """Read and validate *skill*'s ``cw-engine.json`` at *default_branch*.

    Never raises: a repo git cannot run in reads as ``unresolvable``.
    """
    try:
        ref = resolve_default_branch_ref(repo, default_branch)
    except CwError:
        ref = None
    if ref is None:
        return _unresolvable(CAUSE_DEFAULT_BRANCH_UNRESOLVED, default_branch)
    try:
        resolved = resolve_skill(repo, ref.commit, skill)
    except SkillResolutionError as exc:
        return _unresolvable(exc.cause, skill)
    except (CwError, subprocess.SubprocessError, OSError, UnicodeError) as exc:
        return _unresolvable(CAUSE_SKILL_UNREADABLE, " ".join(str(exc).split()))
    if resolved.is_file:
        return _absent(f"{skill} is a single-file command; it has no manifest")
    try:
        data = _archive(repo, ref.commit, resolved)
        return _load_from_archive(data, skill)
    except (MaterializeError, subprocess.CalledProcessError, OSError) as exc:
        return _unresolvable(CAUSE_MATERIALIZE_FAILED, " ".join(str(exc).split()))
