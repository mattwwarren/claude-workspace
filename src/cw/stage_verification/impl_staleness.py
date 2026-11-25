"""The IMPL re-entry staleness check, run by cw (#3529, RFC 0018 C2).

The Claude-native Pre-Stage Detector Guard decides whether an
``Auto-Dev-Stage: impl-complete`` HEAD is still authoritative by running
``check_impl_guard_staleness.py`` against the live ticket comments and the
per-arrival ``regressed_into_stage`` marker. cw runs the same script for the
codex lane (:func:`check_staleness`), then applies the comment-provenance
filter with the snapshot's cw-computed ``agent_authored`` and ``minimized``
flags (:func:`operator_comments_after`): only an operator-authority comment
newer than HEAD makes the branch stale.

The regress signal (:func:`regress_signal`) is the marker read from
``.claude/cw-context.json`` (the stored row's marker is cleared at spawn
success, so the detached driver's row never carries it), else the durable
``task.finalize_regress_branch_head`` while HEAD still equals it. That field is
cleared only by the REVIEW repeat read-and-clear, so unbounded it would make
every later IMPL re-entry look like a regress; the HEAD-equality bound is the
dispatch backstop's own (``cw.dispatch.impl_gates``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from cw._git import git_clean_env, git_output
from cw.branch_ahead import current_head_sha
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from cw.stage_verification.impl_block import IMPL_FAILED, ImplBlock
from cw.stage_verification.impl_gates import resolve_guard_script
from cw.stage_verification.posted_text import redact_and_cap

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.models import TicketTask
    from cw.ticket_snapshot import SnapshotComment

SCRIPT_NAME: Final = "check_impl_guard_staleness.py"
#: ``# cw-script-version: N`` minimum (the version table in ``auto-dev-impl.md``).
MIN_SCRIPT_VERSION: Final = 2
SCRIPT_ABSENT_FRICTION: Final = "check_impl_guard_staleness: script absent, skipped"
CHECK_FAILED_FRICTION: Final = "impl_guard_staleness_check_failed"
COMMENTS_LOAD_FAILED_FRICTION: Final = "impl_guard_comments_load_failed"
AGENT_ONLY_FRICTION: Final = (
    "impl_guard_stale_comment_agent_authored_only: every comment newer than HEAD "
    "is agent-authored or minimized, so the branch is not stale"
)
#: The regress signal for a row reached by a regress into IMPL.
REGRESS_IMPL: Final = "impl"

_REASON_REGRESSED: Final = "regressed_to_impl"
_REASON_STALE_COMMENT: Final = "stale_comment_after_head"
_COMMENTS_FILE: Final = "impl-comments.json"
_EXIT_OK: Final = 0
_EXIT_USAGE: Final = 2
_STDERR_TAIL_CHARS: Final = 500


@dataclass(frozen=True)
class Staleness:
    """The staleness verdict: stale or not, why, and what to deliver."""

    stale: bool = False
    reasons: tuple[str, ...] = ()
    operator_comments: tuple[SnapshotComment, ...] = ()
    friction: tuple[str, ...] = ()
    block: ImplBlock | None = None


def read_regressed_into_stage(worktree: Path) -> str:
    """``queue_metadata.regressed_into_stage`` from ``.claude/cw-context.json``.

    ``""`` when the file is absent, unreadable, not a JSON object, or carries no
    non-empty string there.
    """
    try:
        raw = (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    metadata = data.get("queue_metadata")
    if not isinstance(metadata, dict):
        return ""
    marker = metadata.get("regressed_into_stage")
    return marker if isinstance(marker, str) else ""


def regress_signal(task: TicketTask, worktree: Path, *, merge_present: bool) -> str:
    """The regress marker, else ``"impl"`` while the durable regress field counts.

    The durable ``finalize_regress_branch_head`` counts while HEAD still equals
    it, while ``MERGE_HEAD`` is present, or when HEAD cannot be read (fail
    closed). Otherwise ``""``.
    """
    marker = read_regressed_into_stage(worktree)
    if marker:
        return marker
    recorded = task.finalize_regress_branch_head
    if recorded is None:
        return ""
    head = current_head_sha(worktree)
    if merge_present or head is None or head == recorded:
        return REGRESS_IMPL
    return ""


def _parse_time(value: str) -> datetime | None:
    """An ISO-8601 timestamp (``Z`` or an offset) as aware UTC, else ``None``."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def operator_comments_after(
    comments: Sequence[SnapshotComment], head_commit_at: datetime
) -> tuple[SnapshotComment, ...]:
    """The operator-authority comments posted after *head_commit_at*.

    Drops agent-authored and minimized comments (the snapshot's cw-computed
    provenance flags), comments at or before HEAD, and comments whose
    timestamp does not parse.
    """
    kept: list[SnapshotComment] = []
    for comment in comments:
        if comment.agent_authored or comment.minimized:
            continue
        posted = _parse_time(comment.created_at)
        if posted is not None and posted > head_commit_at:
            kept.append(comment)
    return tuple(kept)


def _stale_marker_block(path: Path) -> ImplBlock:
    return ImplBlock(
        IMPL_FAILED,
        f"Pre-Stage Detector Guard: HEADLESS BLOCK — {SCRIPT_NAME} at {path} — "
        f"missing/stale cw-script-version marker (need >= {MIN_SCRIPT_VERSION})",
    )


def _failed(why: str) -> Staleness:
    return Staleness(
        block=ImplBlock(
            IMPL_FAILED, f"the staleness script {SCRIPT_NAME} failed: {why}"
        )
    )


def _friction_only(friction: str, signal: str) -> Staleness:
    """Friction without a script verdict; a regress *signal* still makes it stale.

    cw computes the regress signal itself, so a script that is absent or exits 2
    cannot erase it: the branch regressed into IMPL resumes instead of advancing.
    """
    if signal:
        return Staleness(stale=True, reasons=(_REASON_REGRESSED,), friction=(friction,))
    return Staleness(friction=(friction,))


def _run_script(
    worktree: Path, script: Path, head_at: str, comments_file: Path, signal: str
) -> subprocess.CompletedProcess[str] | str:
    """The script's completed run, or why it did not run."""
    argv = [
        sys.executable,
        str(script),
        "--head-commit-at",
        head_at,
        "--comments-file",
        str(comments_file),
        "--regressed-into-stage",
        signal,
    ]
    try:
        return subprocess.run(
            argv,
            cwd=worktree,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return redact_and_cap(str(exc))


def _verdict(stdout: str) -> dict[str, Any] | None:
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _judge(
    verdict: dict[str, Any],
    comments: Sequence[SnapshotComment],
    head_at: datetime | None,
) -> Staleness:
    """The effective verdict from the script's reasons and the provenance filter."""
    raw = verdict.get("reasons")
    reasons = tuple(str(r) for r in raw) if isinstance(raw, list) else ()
    friction: list[str] = []
    if verdict.get("comments_load_failed") is True:
        friction.append(COMMENTS_LOAD_FAILED_FRICTION)
    operator: tuple[SnapshotComment, ...] = ()
    if _REASON_STALE_COMMENT in reasons and head_at is not None:
        operator = operator_comments_after(comments, head_at)
        if not operator:
            friction.append(AGENT_ONLY_FRICTION)
    regressed = _REASON_REGRESSED in reasons
    kept = tuple(
        r
        for r in reasons
        if r != _REASON_STALE_COMMENT or operator  # comment reason needs a survivor
    )
    return Staleness(
        stale=regressed or bool(operator),
        reasons=kept,
        operator_comments=operator,
        friction=tuple(friction),
    )


def _execute(
    worktree: Path,
    script: Path,
    *,
    comments: Sequence[SnapshotComment],
    signal: str,
    scratch: Path,
) -> Staleness:
    """Write the comments file, run the script and judge its verdict."""
    try:
        head_at = git_output(
            ["log", "-1", "--format=%cI", "HEAD"], cwd=worktree
        ).strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        why = redact_and_cap(str(exc))
        return _failed(f"HEAD's commit date could not be read: {why}")
    scratch.mkdir(parents=True, exist_ok=True)
    comments_file = scratch / _COMMENTS_FILE
    comments_file.write_text(
        json.dumps([{"created_at": c.created_at, "body": c.body} for c in comments]),
        encoding="utf-8",
    )
    done = _run_script(worktree, script, head_at, comments_file, signal)
    if isinstance(done, str):
        return _failed(f"it did not run: {done}")
    if done.returncode == _EXIT_USAGE:
        return _friction_only(CHECK_FAILED_FRICTION, signal)
    verdict = _verdict(done.stdout) if done.returncode == _EXIT_OK else None
    if verdict is None:
        tail = redact_and_cap(done.stderr.strip()[-_STDERR_TAIL_CHARS:])
        return _failed(f"exit {done.returncode} without a valid verdict: {tail}")
    return _judge(verdict, comments, _parse_time(head_at))


def check_staleness(
    worktree: Path,
    *,
    comments: Sequence[SnapshotComment],
    signal: str,
    scratch: Path,
) -> Staleness:
    """Run the staleness script and apply the comment-provenance filter.

    An absent script is friction and not stale (fail open, as the Claude
    contract) unless *signal* is non-empty: cw computed the regress itself, so
    the verdict is stale (``regressed_to_impl``) with the friction attached. A
    script whose marker is missing or below the minimum is a
    ``Pre-Stage Detector Guard: HEADLESS BLOCK`` block. Exit 2 is friction and
    not stale, with the same *signal* exception. Any other exit, unparseable
    output, or a script that did not run is an ``impl_failed`` block. Effective
    staleness is a regress signal, or a surviving operator-authority comment
    newer than HEAD.
    """
    script = resolve_guard_script(worktree, SCRIPT_NAME, MIN_SCRIPT_VERSION)
    if script.path is None:
        return _friction_only(SCRIPT_ABSENT_FRICTION, signal)
    if not script.marker_ok:
        return Staleness(block=_stale_marker_block(script.path))
    return _execute(
        worktree, script.path, comments=comments, signal=signal, scratch=scratch
    )
