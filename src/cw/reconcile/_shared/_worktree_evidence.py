"""Worktree evidence: unsaved work, headless marker and subagent-spawn stamp.

Reads what a session's worktree says about it -- unsaved work that must
route the task to the operator, the ``cw-context.json`` headless marker, and
the unresolved agent-spawn stamp the subagent-await checks key on. Imports
nothing from its siblings. Split out of the flat ``reconcile/_shared.py``
(#2214).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, assert_never

import yaml

from cw.config import get_client
from cw.exceptions import CwError, UnknownClientError
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_SESSION_ID_KEY,
    AGENT_SPAWN_STAMP_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
    extract_unresolved_spawn_count,
)
from cw.reconcile import _deps
from cw.worktree import (
    DetachedHead,
    NotARepository,
    OnBranch,
    ProbeUnavailable,
    unsaved_work_reason,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, Session

# What a deferred dirty check could not reach: ``clients.yaml`` or ``git``.
type WorktreeProbeKind = Literal["client", "git"]
_CLIENT: WorktreeProbeKind = "client"
_GIT: WorktreeProbeKind = "git"

# Prefix of every per-row park reason the dirty check returns for a cause it
# could not resolve, as opposed to unsaved work it found (#2662).
_UNAVAILABLE_PREFIX = "dirty check unavailable: "
_UNRESOLVED_BRANCH_REASON = (
    f"{_UNAVAILABLE_PREFIX}checked-out branch could not be resolved"
    " (detached HEAD or not a git worktree)"
)
# Longest first message line a WorktreeProbeError detail keeps.
_DETAIL_MAX = 200


class WorktreeProbeError(Exception):
    """The worktree dirty check could not run, for a reason to defer on (#2662).

    A global, transient or unrecognized cause: ``kind`` is ``"client"``
    (``clients.yaml`` unreadable, unparseable, invalid, or no clients at all)
    or ``"git"`` (any ``OSError``, an unrecognized ``git branch
    --show-current`` exit, or a stray error from the unsaved-work probe,
    including a ``UnicodeDecodeError`` from undecodable git output).
    ``detail`` names the original exception. A plain ``Exception``, not a
    ``CwError``, so no broad ``except CwError`` swallows it; the dirty-check
    capture catches it by name and defers the session to the next tick.
    """

    def __init__(self, kind: WorktreeProbeKind, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind: WorktreeProbeKind = kind
        self.detail = detail


def _describe(exc: BaseException) -> str:
    """``<type>: <first line of the message>``, the line cut to 200 characters."""
    first_line = str(exc).partition("\n")[0][:_DETAIL_MAX]
    return f"{type(exc).__name__}: {first_line}"


def _is_headless(session: Session) -> bool:
    """Return True if session's worktree has a headless cw-context.json.

    Fail-open: returns False when worktree_path is None, or when the context
    file is missing or unreadable — a deleted worktree must not be falsely
    flagged as headless. Mirrors cli.py signal_stop at line 1003-1005.
    """
    if session.worktree_path is None:
        return False
    context_path = session.worktree_path / ".claude" / "cw-context.json"
    try:
        context = json.loads(context_path.read_text())
        return bool(context.get("headless")) if isinstance(context, dict) else False
    except (OSError, json.JSONDecodeError):
        return False


def _worktree_dirty_reason_by_path(
    client_name: str, worktree_path: Path | None
) -> str | None:
    """Return why the worktree at *worktree_path* must not be reverted, or None.

    Uses worktree_path (always set on DAEMON sessions) instead of
    session.branch (always None on DAEMON sessions, making the branch-based
    check a production no-op). Each failure is sorted by cause (#2662):

    - ``None`` (clean): no path, a missing directory (nothing to protect), or
      ``unsaved_work_reason`` reports clean.
    - A reason string (the row parks): unsaved work, exactly as
      ``unsaved_work_reason`` reports it; or a per-row deterministic cause
      prefixed ``dirty check unavailable:`` -- an existing directory whose
      checked-out branch is unresolved (detached HEAD or not a git worktree),
      or a client name absent from a non-empty ``clients.yaml``.
    - :class:`WorktreeProbeError` (the caller defers): a global, transient or
      unrecognized cause, ``kind="client"`` for an unreadable, unparseable or
      invalid ``clients.yaml`` (or no clients at all) and ``kind="git"`` for
      any ``OSError``, an unrecognized ``git branch --show-current`` exit, a
      stray ``CwError`` from the unsaved-work probe, or a
      ``UnicodeDecodeError`` from it (a commit subject or status path that is
      not valid UTF-8).

    Anything else propagates rather than reading clean: a genuine bug escapes
    the dirty-check capture and aborts that ``reconcile()`` call.
    """
    if not worktree_path:
        return None
    branch = _lookup_branch(worktree_path)
    if branch is None:
        return _unresolved_reason(worktree_path)
    client = _load_client(client_name)
    if isinstance(client, str):
        return client
    try:
        return unsaved_work_reason(client, branch, wt_path=worktree_path)
    except (CwError, OSError, UnicodeDecodeError) as exc:
        raise WorktreeProbeError(_GIT, _describe(exc)) from exc


def _path_exists(path: Path) -> bool:
    """Whether *path* exists: ``True``, ``False`` only on confirmed absence.

    Probes with ``path.stat()`` rather than ``path.exists()``: since Python
    3.14 ``Path.exists()`` returns ``False`` for a permission or other
    filesystem error, which would read an unreachable worktree as missing and
    therefore clean. ``FileNotFoundError``, ``NotADirectoryError`` and
    ``ValueError`` (an unrepresentable path) are confirmed absence and return
    ``False``; any other ``OSError`` (``PermissionError``, ``ELOOP``, ``EIO``,
    ``ESTALE``, ...) raises a ``git`` :class:`WorktreeProbeError`.
    """
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return False
    except OSError as exc:
        raise WorktreeProbeError(_GIT, _describe(exc)) from exc
    return True


def _lookup_branch(path: Path) -> str | None:
    """The checked-out branch at *path*; ``None`` if unresolved or missing.

    Reads the classified result of the one branch probe
    (:func:`cw.worktree.probe_checked_out_branch`, through the ``_deps``
    seam): a detached HEAD or a directory git does not see as a repository is
    unresolved (``None``). A probe that could not answer carrying an
    ``OSError`` reads as missing (``None``) only when ``path.stat()`` confirms
    the directory is gone (``FileNotFoundError`` or ``NotADirectoryError``);
    on an existing directory, or one whose ``stat`` itself fails
    (``PermissionError``, ``ELOOP``, ``EIO``, ...), it, like an unrecognized
    git exit (a :class:`~cw.worktree.BranchLookupError`), raises a ``git``
    :class:`WorktreeProbeError`.
    """
    probe = _deps.probe_checked_out_branch(path)
    match probe:
        case OnBranch(name):
            return name
        case DetachedHead() | NotARepository():
            return None
        case ProbeUnavailable(error=OSError() as exc):
            if not _path_exists(path):
                return None
            raise WorktreeProbeError(_GIT, _describe(exc)) from exc
        case ProbeUnavailable(error=exc):
            raise WorktreeProbeError(_GIT, _describe(exc)) from exc
        case _:
            assert_never(probe)


def _unresolved_reason(path: Path) -> str | None:
    """Clean for a missing directory, else the unresolved-branch park reason."""
    return _UNRESOLVED_BRANCH_REASON if _path_exists(path) else None


def _load_client(name: str) -> ClientConfig | str:
    """The client config, or a returned ``str`` naming a per-row park.

    An unknown name in a non-empty ``clients.yaml`` is per-row and parks; an
    empty one (no clients configured) and every read or validation error are
    global, so they raise a ``client`` :class:`WorktreeProbeError`.
    """
    try:
        return get_client(name)
    except UnknownClientError as exc:
        if exc.available:
            return f"{_UNAVAILABLE_PREFIX}client '{name}' is not in clients.yaml"
        raise WorktreeProbeError(_CLIENT, _describe(exc)) from exc
    except (CwError, OSError, yaml.YAMLError) as exc:
        raise WorktreeProbeError(_CLIENT, _describe(exc)) from exc


def _read_agent_spawn_stamp_context(
    worktree_path: Path | None,
) -> dict[str, Any] | None:
    """Return the parsed ``.claude/cw-context.json`` for *worktree_path*, or None.

    Shared read half of :func:`_read_unresolved_subagent_spawn` and
    :func:`_unresolved_subagent_spawn_age_seconds` (#2012) so the two readers
    of the same on-disk stamp cannot drift onto different fail-open rules.

    Fail-open (``None``) on a None path, a missing worktree, a missing or
    pre-v5 context, malformed JSON, a non-dict payload, or any other error.
    Both callers translate that ``None`` into their own "no evidence" answer.

    Reads the file directly rather than via ``cw.cli._hook_io``: reconcile must
    not import from ``cw.cli`` (the dependency runs the other way).
    """
    if not worktree_path:
        return None
    try:
        context_path = worktree_path / HOOK_CONTEXT_RELATIVE_PATH
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — fail-safe on any error; stamp readers stay fail-open
        return None
    return context if isinstance(context, dict) else None


def _unresolved_subagent_spawn_age_seconds(
    worktree_path: Path | None, now: datetime
) -> float | None:
    """Return how long *worktree_path*'s unresolved subagent spawn has run.

    Age sibling of :func:`_read_unresolved_subagent_spawn` (#2012), reading the
    ``last_stamped_at`` half of the same ``agent_spawn_stamp`` payload that
    ``cw agent-spawn-pre`` already writes — purely additive on the read side,
    no new writer. ``last_stamped_at`` advances only when the count *increases*
    (see :func:`cw.cli.agent_spawn_stamp._adjust_unresolved_count`), so it
    answers "when did the oldest outstanding spawn begin", which is exactly the
    quantity a deadline needs.

    Returns ``None`` — meaning "no bound available" — when there is no
    outstanding spawn, when the context is unreadable, or when
    ``last_stamped_at`` is missing or unparseable. Every one of those is the
    same fail-open direction as ``_read_unresolved_subagent_spawn``'s ``False``:
    the caller must treat "no bound" as "do not suppress", never as "suppress
    forever" — the unbounded-suppression bug this exists to close.

    A naive ``last_stamped_at`` is read as UTC (matching the writer, which
    stamps ``datetime.now(UTC).isoformat()``) so an old tz-less stamp cannot
    raise on the subtraction. The result may be negative if the stamp is in the
    future relative to *now*; callers compare against a positive deadline, for
    which a negative age correctly reads as "not yet exceeded".
    """
    snapshot = _agent_spawn_stamp_snapshot(worktree_path, now)
    return snapshot[0] if snapshot is not None else None


def _agent_spawn_stamp_snapshot(
    worktree_path: Path | None, now: datetime
) -> tuple[float, str | None] | None:
    """Return the validated stamp age and session id from one context read.

    The shipped-release sweep needs both values for one stop decision. Keeping
    them in one snapshot prevents the age and ownership checks from observing
    different generations of a reused worktree's context file.
    """
    context = _read_agent_spawn_stamp_context(worktree_path)
    if context is None or extract_unresolved_spawn_count(context) <= 0:
        return None
    stamp = context.get(AGENT_SPAWN_STAMP_KEY)
    raw = (
        stamp.get(AGENT_SPAWN_LAST_STAMPED_AT_KEY) if isinstance(stamp, dict) else None
    )
    if not isinstance(raw, str):
        return None
    try:
        stamped_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamped_at.tzinfo is None:
        stamped_at = stamped_at.replace(tzinfo=UTC)
    session_id = (
        stamp.get(AGENT_SPAWN_SESSION_ID_KEY) if isinstance(stamp, dict) else None
    )
    if not isinstance(session_id, str) or not session_id:
        session_id = None
    return (now - stamped_at).total_seconds(), session_id


def _agent_spawn_stamp_session_id(worktree_path: Path | None) -> str | None:
    """Return the cw session id the Stop-hook snapshot wrote into the stamp.

    The ``session_id`` sub-key of ``agent_spawn_stamp`` (#2715): the context
    owner when the Stop hook's count > 0 snapshot was written. Returns it only
    when it is a non-empty string; a None path, an unreadable context, a
    missing or non-dict stamp, and a missing, empty or non-string id all
    return None. Reads through :func:`_read_agent_spawn_stamp_context`, so it
    shares the other readers' parse. Its one caller, the shipped release,
    treats None as "hold": a stamp it cannot tie to the session it would stop
    never justifies the stop.
    """
    context = _read_agent_spawn_stamp_context(worktree_path)
    stamp = context.get(AGENT_SPAWN_STAMP_KEY) if context is not None else None
    session_id = (
        stamp.get(AGENT_SPAWN_SESSION_ID_KEY) if isinstance(stamp, dict) else None
    )
    return session_id if isinstance(session_id, str) and session_id else None


def _read_unresolved_subagent_spawn(worktree_path: Path | None) -> bool:
    """Return True iff *worktree_path* carries an unresolved subagent spawn.

    Reads the ``agent_spawn_stamp`` counter the ``cw agent-spawn-pre`` /
    ``cw agent-spawn-post`` hook pair maintains in the worktree's
    ``.claude/cw-context.json`` (#1646). A count above zero means a subagent
    spawn started and its matching Post hook never fired — the worker died or
    hung mid-spawn.

    Deliberately fail-open in one direction only (the stamp readers, unlike
    the dirty check since #2662, read evidence of a spawn, not dirt): a None
    path, a missing worktree, a missing or pre-v5 context, malformed
    JSON, a non-dict payload, a non-dict stamp, a non-int count, or any other
    error all return False. Reporting an unresolved spawn on ambiguous evidence
    would park healthy tickets under a reason that also overrides
    ``reap_policy: auto`` — strictly worse than missing one crash's precision.

    Reads the file via :func:`_read_agent_spawn_stamp_context` rather than
    ``cw.cli._hook_io``: reconcile must not import from ``cw.cli`` (the
    dependency runs the other way). The shared path constant and the
    count-extraction logic both come from ``cw.models``
    (:func:`cw.models.extract_unresolved_spawn_count`) so this reader and
    ``cw.cli.agent_spawn_stamp``'s write-side reader cannot drift onto
    different literals or validation rules for the same on-disk shape (#1646
    review finding).

    Deliberately **age-blind**, and unchanged by #2012: its callers (the
    phantom sweep) ask "did this worker die mid-spawn", for which any
    outstanding stamp is evidence no matter how old. The age-bounded question
    belongs to :func:`_unresolved_subagent_spawn_age_seconds`.
    """
    context = _read_agent_spawn_stamp_context(worktree_path)
    if context is None:
        return False
    return extract_unresolved_spawn_count(context) > 0
