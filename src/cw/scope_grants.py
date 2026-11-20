"""Scope-drift grant ledger: exact-path rules, verification and carry (#3772).

``cw dev-queue approve --scope-drift`` records each approval as a
:class:`cw.models.scope_grant.ScopeGrant` on the row. A REVIEW-to-IMPL repair
dispatch used to lose that approval, because the per-spawn transport fields
are cleared at spawn success, and the gate re-parked with the full extra list.
:func:`carry_scope_grants` re-derives the transport from the ledger at each IMPL
dispatch. It reuses only the grants that still verify, and it fails closed:
any doubt drops the grant and the operator is asked again.

This module is a leaf: it imports ``cw.models``, ``cw._git`` and ``cw.gh``
only. Callers resolve the origin repo slug and pass it in, because
``cw.pr_hydrate`` imports ``cw.dev_queue`` at module level and so cannot be
imported from it or from ``cw.dev_queue``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC
from functools import partial
from typing import TYPE_CHECKING

from cw import _git
from cw.exceptions import CwError
from cw.gh import parse_pr_url
from cw.models.enums import Stage

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    from cw.models.client import ClientConfig, ReviewConfig
    from cw.models.scope_grant import ScopeGrant
    from cw.models.tasks.ticket_task import TicketTask

_log = logging.getLogger(__name__)

# Bounds keep operator-owned row state small and every verifier loop finite.
# The numbers are cheap to retune; a grant that exceeds one is simply not carried.
MAX_SCOPE_GRANTS = 8
MAX_GRANT_PATHS = 64
MAX_GRANT_PATH_LEN = 512

# Unicode general categories no exact path may contain: control (``Cc``, which
# covers NUL, newline and U+0080-U+009F), format (``Cf``) and the line and
# paragraph separators (``Zl``, ``Zp``). The approved-paths file is joined with
# newlines and read back with ``str.splitlines()`` plus ``.strip()``, so a
# character that splits or trims a line would authorize other paths.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_GLOB_CHARS = ("*", "?")
_EXTRA_MARKER = "Extra:"


@dataclass(frozen=True)
class GrantFacts:
    """Facts a grant records that need git or the operator's identity.

    Gathered before the queue lock is taken. ``repo`` is ``None`` when the
    origin slug cannot be resolved, which makes the grant fail closed at carry.
    ``actor`` is ``""`` when the operator login is unresolved.
    """

    repo: str | None
    policy: str
    actor: str


@dataclass(frozen=True)
class GrantContext:
    """The row and repository a grant is verified against at carry time."""

    client: str
    ticket_id: str
    row_created_at: datetime
    repo: str | None
    pr_number: int | None
    policy: str
    plan_fingerprint: str | None


@dataclass(frozen=True)
class CarriedGrants:
    """Outcome of :func:`verify_scope_grants`.

    ``head`` is the anchor head every carried grant is an ancestor of, or
    ``None`` when nothing verified. ``rejected`` holds one fixed reason per
    dropped grant; no path text is kept here, so it is safe to log.
    """

    paths: list[str] = field(default_factory=list)
    head: str | None = None
    rejected: list[str] = field(default_factory=list)


def _has_control_char(path: str) -> bool:
    return any(unicodedata.category(ch) in _FORBIDDEN_CATEGORIES for ch in path)


def _path_error(path: str) -> str | None:
    """The reason ``path`` is not an exact repo-relative path, or ``None``."""
    if not path:
        return "empty path"
    if len(path) > MAX_GRANT_PATH_LEN:
        return "over-long path"
    if _has_control_char(path) or len(path.splitlines()) != 1:
        return "control character in path"
    if path != path.strip():
        return "surrounding whitespace in path"
    return _shape_error(path)


def _shape_error(path: str) -> str | None:
    """Backslash, absolute, glob and segment rules for a non-empty path."""
    if "\\" in path:
        return "backslash in path"
    if path.startswith("/"):
        return "absolute path"
    if any(ch in path for ch in _GLOB_CHARS):
        return "glob character in path"
    return _segment_error(path)


def _segment_error(path: str) -> str | None:
    """The reason ``path``'s ``/``-separated segments are not exact, or ``None``."""
    segments = path.split("/")
    if segments[-1] == "":
        return "trailing slash in path"
    if any(seg == "" for seg in segments):
        return "empty segment in path"
    if any(seg in {".", ".."} for seg in segments):
        return "dot segment in path"
    return None


def exact_paths_error(paths: Sequence[str]) -> str | None:
    """The first reason ``paths`` is not an exact path list, or ``None``.

    Exact means literal repo-relative paths: no globs, no directories, no
    absolute paths, no control, format or line-separator characters (a newline
    would inject entries into the newline-delimited approved-paths file, and
    the gate reads it back with ``splitlines()``), and no leading or trailing
    whitespace (the gate strips each line). Brackets and braces are
    allowed, so ``app/[id]/page.tsx`` stays legal.
    """
    if not paths:
        return "no paths given"
    if len(paths) > MAX_GRANT_PATHS:
        return f"too many paths ({len(paths)}; the cap is {MAX_GRANT_PATHS})"
    for path in paths:
        error = _path_error(path)
        if error is not None:
            return error
    return None


def review_policy_fingerprint(review: ReviewConfig) -> str:
    """A stable sha256 of the review settings a grant's reuse depends on.

    Covers ``convergence`` and the engine's ``skill`` and ``guarded_paths``. A
    change in any of them means the grant is not reused.
    """
    engine = review.engine
    shape: dict[str, object] = {
        "convergence": review.convergence,
        "engine": None
        if engine is None
        else {"skill": engine.skill, "guarded_paths": list(engine.guarded_paths)},
    }
    canonical = json.dumps(shape, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _recorded_key(grant: ScopeGrant) -> datetime:
    """``recorded_at`` as an aware datetime; a naive stamp is read as UTC.

    A hand-edited or partially migrated ledger can mix naive and aware stamps,
    and comparing those raises ``TypeError`` -- which the never-raises seams
    (the carry and the approve append) must not let escape.
    """
    stamp = grant.recorded_at
    return stamp if stamp.utcoffset() is not None else stamp.replace(tzinfo=UTC)


def append_scope_grant(
    grants: Sequence[ScopeGrant], grant: ScopeGrant
) -> list[ScopeGrant]:
    """Append ``grant``, dropping an older grant with the same head and paths.

    The ledger keeps at most :data:`MAX_SCOPE_GRANTS` entries; the oldest by
    ``recorded_at`` are evicted first.
    """
    kept = [
        g
        for g in grants
        if not (g.head == grant.head and sorted(g.paths) == sorted(grant.paths))
    ]
    kept.append(grant)
    kept.sort(key=_recorded_key)
    return kept[-MAX_SCOPE_GRANTS:]


def pr_number_from_url(pr_url: str | None) -> int | None:
    if not pr_url:
        return None
    parsed = parse_pr_url(pr_url)
    return None if parsed is None else parsed[1]


def context_for_task(
    task: TicketTask, client: ClientConfig, repo: str | None, policy: str
) -> GrantContext:
    """The verification context for ``task`` on ``client`` right now."""
    return GrantContext(
        client=client.name,
        ticket_id=task.ticket_id,
        row_created_at=task.created_at,
        repo=repo,
        pr_number=pr_number_from_url(task.pr_url),
        policy=policy,
        plan_fingerprint=task.plan_approved_fingerprint,
    )


def _identity_rejection(grant: ScopeGrant, ctx: GrantContext) -> str | None:
    """Row identity, repository and PR rules. ``None`` when the grant binds."""
    if (grant.client, grant.ticket_id, grant.row_created_at) != (
        ctx.client,
        ctx.ticket_id,
        ctx.row_created_at,
    ):
        return "row identity differs"
    if grant.repo is None or ctx.repo is None:
        return "repository unbound"
    if grant.repo.casefold() != ctx.repo.casefold():
        return "repository differs"
    if (
        grant.pr_number is not None
        and ctx.pr_number is not None
        and grant.pr_number != ctx.pr_number
    ):
        return "pull request differs"
    return None


def _context_rejection(grant: ScopeGrant, ctx: GrantContext) -> str | None:
    """Review policy, plan of record and exact-path rules."""
    if grant.policy != ctx.policy:
        return "review policy changed"
    if grant.plan_fingerprint != ctx.plan_fingerprint:
        return "plan of record changed"
    if exact_paths_error(grant.paths) is not None:
        return "grant paths are not exact"
    return None


def _head_rejection(
    head: str, is_ancestor: Callable[[str, str], bool | None]
) -> str | None:
    """A verified descendant of HEAD. ``None`` when ``head`` is one."""
    if _git.FULL_COMMIT_ID_RE.fullmatch(head) is None:
        return "head is not a full commit id"
    answer = is_ancestor(head, "HEAD")
    if answer is True:
        return None
    if answer is False:
        return "head is not an ancestor of HEAD"
    return "ancestry unverifiable"


def _anchor_rejection(
    head: str, anchor_head: str, is_ancestor: Callable[[str, str], bool | None]
) -> str | None:
    """An older grant must be an ancestor of the anchor head to be unioned."""
    if is_ancestor(head, anchor_head) is True:
        return None
    return "head diverges from the anchor"


def verify_scope_grants(
    grants: Sequence[ScopeGrant],
    ctx: GrantContext,
    *,
    is_ancestor: Callable[[str, str], bool | None],
) -> CarriedGrants:
    """Reuse only the grants that still bind to this row, repo and HEAD.

    Grants are scanned newest first, up to :data:`MAX_SCOPE_GRANTS`. The
    anchor is the newest grant whose head is an ancestor of HEAD. Every other
    carried grant must also have its head as an ancestor of the anchor head, so
    the single transported head re-verifies the whole union. ``is_ancestor``
    is tri-state; ``None`` fails closed like ``False``.
    """
    rejected: list[str] = []
    anchor_head: str | None = None
    paths: set[str] = set()
    newest_first = sorted(grants, key=_recorded_key, reverse=True)
    for grant in newest_first[:MAX_SCOPE_GRANTS]:
        reason = _identity_rejection(grant, ctx) or _context_rejection(grant, ctx)
        if reason is None:
            reason = _head_rejection(grant.head, is_ancestor)
        if reason is None and anchor_head is not None:
            reason = _anchor_rejection(grant.head, anchor_head, is_ancestor)
        if reason is not None:
            rejected.append(reason)
            continue
        if anchor_head is None:
            anchor_head = grant.head
        paths.update(grant.paths)
    if anchor_head is None:
        return CarriedGrants(rejected=rejected)
    return CarriedGrants(paths=sorted(paths), head=anchor_head, rejected=rejected)


def carry_scope_grants(
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    *,
    repo: str | None,
) -> None:
    """Re-populate ``task``'s transport from its verified ledger, IMPL only.

    ``repo`` is the worktree's origin slug, resolved by the caller (this leaf
    cannot import ``cw.pr_hydrate``, which imports ``cw.dev_queue``). ``None``
    fails the carry closed. Mutates the in-memory claimed task only; the stored
    row keeps its ledger. With no verified grant the transport is left
    untouched, so a pre-v64 in-flight approval keeps working as before. Never
    raises: an unreadable git or config state logs a warning and leaves the
    transport unset, which is the fail-closed direction.
    """
    if task.stage is not Stage.IMPL or not task.scope_grants:
        return
    try:
        policy = review_policy_fingerprint(client.review)
        ctx = context_for_task(task, client, repo, policy)
        carried = verify_scope_grants(
            task.scope_grants,
            ctx,
            is_ancestor=partial(_git.is_ancestor, worktree),
        )
    except (OSError, subprocess.TimeoutExpired, ValueError, CwError):
        _log.warning(
            "scope grants not carried for %s/%s: verification unavailable",
            client.name,
            task.ticket_id,
        )
        return
    for reason in carried.rejected:
        _log.warning(
            "scope grant not carried for %s/%s: %s",
            client.name,
            task.ticket_id,
            reason,
        )
    if carried.head is None:
        return
    task.scope_drift_approved_extra_files = carried.paths
    task.scope_drift_approved_head = carried.head


def parse_drift_extra_files(details: str) -> list[str]:
    """The extra paths from a ``plan_scope_drift`` park's details.

    Best-effort and never raises; caps the result at :data:`MAX_GRANT_PATHS`
    and returns ``[]`` for text neither producer could have written. Two real
    producers, tried in order:

    * The codex gate (``impl_gates._drift_block``): a line ENDING with
      ``extra_files:`` (the marker closes a prose sentence, ``... (allowed
      extra: N). extra_files:``) followed by ``- <path>`` lines, read until
      the first non-bullet line.
    * The Claude worker's park text (``auto-dev-impl-appendix`` Step 2.5 gate
      2): one line, ``... Extra: <comma-joined paths>``; the text after the
      last ``Extra:`` on that line is split on commas.
    """
    lines = [raw.strip() for raw in details.splitlines()]
    return _bullet_block_paths(lines) or _extra_line_paths(lines)


def _bullet_block_paths(lines: list[str]) -> list[str]:
    """Paths from the ``- <path>`` lines after a line ending ``extra_files:``."""
    found: list[str] = []
    in_block = False
    for line in lines:
        if not in_block:
            in_block = line.endswith("extra_files:")
            continue
        if not line.startswith("- "):
            break
        found.append(line[2:].strip())
        if len(found) >= MAX_GRANT_PATHS:
            break
    return found


def _extra_line_paths(lines: list[str]) -> list[str]:
    """Comma-joined paths after the last ``Extra:`` of the first line holding one."""
    for line in lines:
        if _EXTRA_MARKER in line:
            tail = line.rsplit(_EXTRA_MARKER, 1)[1]
            items = [item.strip() for item in tail.split(",")]
            return [item for item in items if item][:MAX_GRANT_PATHS]
    return []


__all__ = [
    "MAX_GRANT_PATHS",
    "MAX_GRANT_PATH_LEN",
    "MAX_SCOPE_GRANTS",
    "CarriedGrants",
    "GrantContext",
    "GrantFacts",
    "append_scope_grant",
    "carry_scope_grants",
    "context_for_task",
    "exact_paths_error",
    "parse_drift_extra_files",
    "review_policy_fingerprint",
    "verify_scope_grants",
]
