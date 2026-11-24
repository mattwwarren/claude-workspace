"""The opt-in completion check of a codex engine round (#3885).

A manifest may declare ``completion_check``: a checker the repository commits
inside its skill directory. After the role turns pass and before the
aggregation turn, cw runs it and requires a valid ``result.json`` that binds
the reviewed head, the round id and every manifest role.

Trust and isolation, stated plainly:

- The checker is extracted from the default-branch git bytes at the commit the
  manifest was read from. It is never run from the worktree or from the
  materialized copy under ``.cw/review-engine``.
- Its cwd is a fresh directory cw creates under the worker ``TMPDIR`` and
  removes afterwards; the extraction directory is a subdirectory of it. The
  worktree is passed only as a data argument.
- The environment is ``PATH`` and ``LANG`` (each only when set) plus
  ``TMPDIR``. Nothing else is inherited.
- There is no OS sandbox. Read-only is enforced the way the engine already
  enforces it: ``engine_precheck`` runs once before the checker and once after
  it, so a tree the checker dirtied, or a HEAD it moved, fails the round.
- The checker runs in its own process group, killed at
  ``completion_check.timeout_seconds``.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cw.reconcile.review_recipes.mechanical_repair_git import run_declared_command
from cw.review_engine.constants import (
    COMPLETION_FAILED_REASONS,
    COMPLETION_REASON_CHECK_UNVERIFIABLE,
    COMPLETION_REASON_CHECKER_EXIT_NONZERO,
    COMPLETION_REASON_CHECKER_LAUNCH_FAILED,
    COMPLETION_REASON_CHECKER_MISSING,
    COMPLETION_REASON_CHECKER_NOT_EXECUTABLE,
    COMPLETION_REASON_CHECKER_TIMEOUT,
    COMPLETION_REASON_EXTRACT_FAILED,
    COMPLETION_REASON_HEAD_MOVED,
    COMPLETION_REASON_RESULT_INVALID,
    COMPLETION_REASON_RESULT_MISSING,
    COMPLETION_REASON_STATUS_FAILED,
    COMPLETION_REASON_WORKTREE_MUTATED,
    ENGINE_STATE_RELATIVE_DIR,
    FRICTION,
    EngineRoundFailure,
)
from cw.review_engine.manifest_source import _archive
from cw.review_engine.materialize import MaterializeError, _extract_archive
from cw.review_engine.validate import SummaryFailure, engine_precheck

if TYPE_CHECKING:
    from cw.review_engine.skill import ResolvedSkill

_log = logging.getLogger(__name__)

#: Directory under the round dir that holds the checker's evidence.
COMPLETION_DIR_NAME: Final = "completion"
COMPLETION_RESULT_FILENAME: Final = "result.json"
MAX_COMPLETION_RESULT_BYTES: Final = 65_536
MAX_COMPLETION_DETAIL_CHARS: Final = 200
#: Environment names the checker inherits when set; ``TMPDIR`` is always added.
_ENV_KEYS: Final = ("PATH", "LANG")
_DIR_PREFIX: Final = "cw-engine-check-"
_RUN_DIR_NAME: Final = "run"
_SKILL_DIR_NAME: Final = "skill"
_COVERAGE_PARTIAL: Final = "PARTIAL"
_STALE_ROUND: Final = "stale-round"


class CompletionResult(BaseModel):
    """The checker's ``result.json``. Strict; every key is required."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = Field(alias="schema")
    head_sha: str
    round_id: str
    status: Literal["complete", "incomplete", "failed"]
    roles_covered: list[str]
    detail: str = Field(max_length=MAX_COMPLETION_DETAIL_CHARS)


def _completion_failure(reason: str) -> SummaryFailure:
    """The ``engine_completion_check_failed`` failure for a vocabulary *reason*."""
    if reason not in COMPLETION_FAILED_REASONS:
        msg = f"unknown completion failure reason: {reason!r}"
        raise ValueError(msg)
    failure = EngineRoundFailure.ENGINE_COMPLETION_CHECK_FAILED
    return SummaryFailure(failure, FRICTION[failure].format(reason=reason))


def _fresh_evidence_dir(worktree: Path, round_dir: Path) -> Path | SummaryFailure:
    """Recreate ``<round dir>/completion`` empty, refusing any redirected ancestor.

    A ``workspace-write`` role could replace ``.cw/review-engine`` (or the round
    directory, or ``.cw``) with a symlink, so the delete below would run outside
    the worktree. One resolve-and-compare covers every ancestor; a mismatch
    deletes nothing.
    """
    try:
        expected = worktree.resolve() / ENGINE_STATE_RELATIVE_DIR / round_dir.name
        if round_dir.resolve() != expected:
            return _completion_failure(COMPLETION_REASON_CHECK_UNVERIFIABLE)
        evidence = round_dir / COMPLETION_DIR_NAME
        if evidence.is_symlink() or evidence.is_file():
            evidence.unlink()
        elif evidence.exists():
            shutil.rmtree(evidence)
        evidence.mkdir()
    except OSError:
        return _completion_failure(COMPLETION_REASON_CHECK_UNVERIFIABLE)
    return evidence


def _checker_in_tree(dest: Path, rel_path: str) -> Path | str:
    """The checker file under *dest*, or the reason it is not a runnable file."""
    checker = dest / rel_path
    if checker.is_symlink() or not checker.is_file():
        return COMPLETION_REASON_CHECKER_MISSING
    try:
        resolved = checker.resolve()
        if not resolved.is_relative_to(dest.resolve()):
            return COMPLETION_REASON_CHECKER_MISSING
        mode = resolved.stat().st_mode
    except OSError:
        return COMPLETION_REASON_CHECKER_MISSING
    if not mode & stat.S_IXUSR:
        return COMPLETION_REASON_CHECKER_NOT_EXECUTABLE
    return resolved


def _extract_checker(
    repo: Path, commit: str, skill: ResolvedSkill, rel_path: str, dest: Path
) -> Path | str:
    """Extract the skill from *commit* into *dest*; return the checker or a reason."""
    try:
        data = _archive(repo, commit, skill)
        _extract_archive(data, dest)
    except (MaterializeError, subprocess.CalledProcessError, OSError):
        return COMPLETION_REASON_EXTRACT_FAILED
    return _checker_in_tree(dest, rel_path)


def _checker_env() -> dict[str, str]:
    """The checker's whole environment: ``PATH`` and ``LANG`` when set, ``TMPDIR``."""
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    env["TMPDIR"] = tempfile.gettempdir()
    return env


def _result_bytes(evidence: Path) -> bytes | str:
    """The raw ``result.json`` bytes, bounded, or the reason it cannot be read."""
    path = evidence / COMPLETION_RESULT_FILENAME
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_COMPLETION_RESULT_BYTES:
            return COMPLETION_REASON_RESULT_INVALID
        with path.open("rb") as handle:
            raw = handle.read(MAX_COMPLETION_RESULT_BYTES + 1)
    except FileNotFoundError:
        return COMPLETION_REASON_RESULT_MISSING
    except OSError:
        return COMPLETION_REASON_RESULT_INVALID
    if len(raw) > MAX_COMPLETION_RESULT_BYTES:
        return COMPLETION_REASON_RESULT_INVALID
    return raw


def _read_result(evidence: Path) -> CompletionResult | SummaryFailure:
    """Parse the checker's ``result.json`` into the strict model."""
    raw = _result_bytes(evidence)
    if isinstance(raw, str):
        return _completion_failure(raw)
    try:
        return CompletionResult.model_validate_json(raw)
    except ValidationError:
        return _completion_failure(COMPLETION_REASON_RESULT_INVALID)


def _judge(
    result: CompletionResult, head: str, round_id: str, roles: list[str]
) -> SummaryFailure | None:
    """Bind the result to the head, the round and every manifest role."""
    if result.status == "failed":
        return _completion_failure(COMPLETION_REASON_STATUS_FAILED)
    if result.head_sha != head:
        return SummaryFailure(
            EngineRoundFailure.ENGINE_HEAD_MISMATCH,
            FRICTION[EngineRoundFailure.ENGINE_HEAD_MISMATCH].format(
                summary=result.head_sha[:12], expected=head[:12], head=head[:12]
            ),
        )
    if result.round_id != round_id:
        return SummaryFailure(
            EngineRoundFailure.ENGINE_HEAD_MISMATCH,
            FRICTION[EngineRoundFailure.ENGINE_HEAD_MISMATCH].format(
                summary=_STALE_ROUND, expected=head[:12], head=head[:12]
            ),
        )
    missing = set(roles) - set(result.roles_covered)
    if result.status == "incomplete" or missing:
        return SummaryFailure(
            EngineRoundFailure.ENGINE_COVERAGE_INCOMPLETE,
            FRICTION[EngineRoundFailure.ENGINE_COVERAGE_INCOMPLETE].format(
                coverage=_COVERAGE_PARTIAL
            ),
        )
    return None


def _post_check_failure(failure: SummaryFailure) -> SummaryFailure:
    """Attribute a tree problem found after the checker ran to the checker.

    A git error already converted to ``check_unverifiable`` is returned as is:
    it is a completion failure, not a tree state the checker produced.
    """
    if failure.reason is EngineRoundFailure.ENGINE_HEAD_MISMATCH:
        return _completion_failure(COMPLETION_REASON_HEAD_MOVED)
    if failure.reason is EngineRoundFailure.ENGINE_WORKTREE_MUTATED:
        return _completion_failure(COMPLETION_REASON_WORKTREE_MUTATED)
    return failure


def _tree_check(worktree: Path, head: str) -> SummaryFailure | None:
    """``engine_precheck``, with a git error reported as unverifiable (A10)."""
    try:
        return engine_precheck(worktree, head)
    except (subprocess.CalledProcessError, OSError):
        return _completion_failure(COMPLETION_REASON_CHECK_UNVERIFIABLE)


def _run_outcome(
    checker: Path,
    *,
    worktree: Path,
    head: str,
    round_id: str,
    evidence: Path,
    roles: list[str],
    run_dir: Path,
    timeout: int,
) -> SummaryFailure | None:
    """Run the checker; a failed run returns its reason, a clean run returns None."""
    argv = [
        str(checker),
        "--head-sha",
        head,
        "--round-id",
        round_id,
        "--worktree",
        str(worktree),
        "--evidence-dir",
        str(evidence),
        "--roles",
        ",".join(roles),
    ]
    try:
        outcome = run_declared_command(
            argv, cwd=run_dir, env=_checker_env(), timeout=timeout
        )
    except OSError:
        return _completion_failure(COMPLETION_REASON_CHECKER_LAUNCH_FAILED)
    _log.debug("completion check output chars=%d", len(outcome.tail))
    if outcome.timed_out:
        return _completion_failure(COMPLETION_REASON_CHECKER_TIMEOUT)
    if outcome.returncode != 0:
        return _completion_failure(COMPLETION_REASON_CHECKER_EXIT_NONZERO)
    return None


def _run_in_scratch(
    worktree: Path,
    scratch: Path,
    *,
    commit: str,
    resolved: ResolvedSkill,
    head: str,
    round_id: str,
    round_dir: Path,
    timeout: int,
    checker_path: str,
    roles: list[str],
) -> SummaryFailure | None:
    """Steps 3 to 8 of the check, run inside the scratch directory."""
    run_dir = scratch / _RUN_DIR_NAME
    run_dir.mkdir()
    extracted = _extract_checker(
        worktree, commit, resolved, checker_path, scratch / _SKILL_DIR_NAME
    )
    if isinstance(extracted, str):
        return _completion_failure(extracted)
    evidence = _fresh_evidence_dir(worktree, round_dir)
    if isinstance(evidence, SummaryFailure):
        return evidence
    ran = _run_outcome(
        extracted,
        worktree=worktree,
        head=head,
        round_id=round_id,
        evidence=evidence,
        roles=roles,
        run_dir=run_dir,
        timeout=timeout,
    )
    if ran is not None:
        return ran
    post = _tree_check(worktree, head)
    if post is not None:
        return _post_check_failure(post)
    result = _read_result(evidence)
    if isinstance(result, SummaryFailure):
        return result
    return _judge(result, head, round_id, roles)


def run_completion_check(
    worktree: Path,
    *,
    commit: str,
    resolved: ResolvedSkill,
    head: str,
    round_id: str,
    round_dir: Path,
    timeout: int,
    checker_path: str,
    roles: list[str],
) -> SummaryFailure | None:
    """Run the manifest's checker and judge its result; None when it passes.

    A pre-run tree failure is returned unchanged, because a role caused it.
    Every failure after that is ``engine_completion_check_failed``.
    """
    pre = _tree_check(worktree, head)
    if pre is not None:
        return pre
    try:
        scratch = Path(tempfile.mkdtemp(prefix=_DIR_PREFIX))
    except OSError:
        return _completion_failure(COMPLETION_REASON_CHECK_UNVERIFIABLE)
    try:
        return _run_in_scratch(
            worktree,
            scratch,
            commit=commit,
            resolved=resolved,
            head=head,
            round_id=round_id,
            round_dir=round_dir,
            timeout=timeout,
            checker_path=checker_path,
            roles=roles,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def checker_problem(
    repo: Path, commit: str, resolved: ResolvedSkill, checker_path: str
) -> str | None:
    """Doctor helper: the reason the checker at *commit* is unusable, or None.

    Shares :func:`_extract_checker` with the runner so the two cannot disagree.
    """
    try:
        scratch = Path(tempfile.mkdtemp(prefix=_DIR_PREFIX))
    except OSError:
        return COMPLETION_REASON_CHECK_UNVERIFIABLE
    try:
        extracted = _extract_checker(
            repo, commit, resolved, checker_path, scratch / _SKILL_DIR_NAME
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return extracted if isinstance(extracted, str) else None
