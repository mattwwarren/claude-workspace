"""The IMPL turn's output document, the cw-stamped sentinel, and blocked results.

cw, never the model, emits the IMPL sentinel (#3061 R4): the turn returns a
strict ``{sentinel}`` document, the framed sentinel is parsed with the strict
:class:`~cw.auto_dev_result.AutoDevResult` validation (never the lenient
``parse_stdout``), and every field cw owns is stamped over the model's. The
REVIEW stamper (``stamp_stage_facts``) is not used: its fix-cycle commit
filter would drop the IMPL commit.

The git facts (``branch``, ``fork_point_sha``, ``commits``, ``scope.files``,
``scope.lines_actual``) are cw's measurements of the worktree, taken after the
one cw commit and push; the model's claims about them are overwritten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from cw.auto_dev_result import AutoDevResult, Blocker
from cw.auto_dev_result.parse import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    BLOCKER_REASON_VALIDATION_FAILED,
    extract_block,
)
from cw.auto_dev_result.schema._vocab import (
    EMPTY_DIFF_BLOCKER_REASON,
    EMPTY_DIFF_STATUS,
)
from cw.codex_review import (
    _CODEX_IMPL_BLOCKED_NEXT_ACTIONS,
    STAGE2_IMPL,
    make_codex_blocked,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.auto_dev_result import PlanSource, ScopeTier
    from cw.local_runner import GitFacts
    from cw.models import TicketTask

#: The git trailer cw puts on its one IMPL commit. The single definition: the
#: codex IMPL addendum receives it through a function-local import in
#: ``cw.contract_manifest._render._stage_addendum``.
IMPL_COMPLETE_TRAILER: Final = "Auto-Dev-Stage: impl-complete"

#: Existing blocker reasons the IMPL stage reuses (none is new to the vocabulary).
IMPL_FAILED: Final = "impl_failed"
PLAN_MISSING: Final = "plan_missing"
IMPL_NOT_PUSHED: Final = "impl_not_pushed"

IMPL_STAGE_COMPLETE: Final = "stage_complete"
IMPL_BLOCKED: Final = "blocked"
IMPL_STATUSES: Final = frozenset({IMPL_STAGE_COMPLETE, IMPL_BLOCKED})

_SENTINEL_OPEN: Final = "<<<AUTO_DEV_RESULT"
_SCHEMA_VERSION: Final = 4


class ImplTurnDocument(BaseModel):
    """The IMPL turn's ``-o`` document: the framed sentinel."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    sentinel: str


@dataclass(frozen=True)
class ImplBlock:
    """A cw-decided IMPL park: the reason, details and status.

    The one block value the guard, gate and feedback functions return, so no
    REVIEW-worded fence or gate text reaches an IMPL result. *status* is
    ``blocked`` except for the empty-diff park (``empty_diff_blocked``).
    """

    reason: str
    details: str
    retry_eligible: bool = False
    status: str = IMPL_BLOCKED


@dataclass(frozen=True)
class ImplTurnFailure:
    """Why a turn's output could not become a result (a blocked reason)."""

    reason: str
    details: str


def _sentinel_payload(sentinel: str) -> dict[str, Any] | str:
    """The sentinel's JSON object, or why it is not exactly one valid block."""
    count = sentinel.count(_SENTINEL_OPEN)
    if count != 1:
        return f"expected exactly one framed sentinel block, found {count}"
    block = extract_block(sentinel)
    if block is None:
        return "the sentinel block is not closed"
    try:
        payload = json.loads(block)
    except (json.JSONDecodeError, RecursionError) as exc:
        return f"the sentinel block is not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return "the sentinel block is not a JSON object"
    return payload


def parse_impl_turn(content: str | None) -> dict[str, Any] | ImplTurnFailure:
    """Parse the ``-o`` *content* strictly into the model's sentinel payload.

    A missing, empty or invalid document is ``no_result_emitted``; a sentinel
    that is not exactly one framed block, is not JSON, or carries a status
    outside :data:`IMPL_STATUSES` is ``validation_failed``. Schema validation
    happens in :func:`stamp_impl_result`, once cw's facts are in hand.
    """
    if content is None or not content.strip():
        return ImplTurnFailure(
            BLOCKER_REASON_NO_RESULT_EMITTED, "the turn wrote no -o document"
        )
    try:
        document = ImplTurnDocument.model_validate_json(content)
    except ValidationError as exc:
        details = f"the -o document is not a {{sentinel}} object: {exc}"
        return ImplTurnFailure(BLOCKER_REASON_NO_RESULT_EMITTED, details)
    payload = _sentinel_payload(document.sentinel)
    if isinstance(payload, str):
        return ImplTurnFailure(BLOCKER_REASON_VALIDATION_FAILED, payload)
    status = payload.get("status")
    if not isinstance(status, str) or status not in IMPL_STATUSES:
        details = f"status {status!r} is not an impl-stage status"
        return ImplTurnFailure(BLOCKER_REASON_VALIDATION_FAILED, details)
    return payload


def _stamped_scope(
    payload: dict[str, Any], facts: GitFacts, scope_tier: ScopeTier | None
) -> dict[str, Any]:
    scope = payload.get("scope")
    kept = scope if isinstance(scope, dict) else {}
    return {
        **kept,
        "files": facts["files"],
        "lines_actual": facts["lines_actual"],
        "tier": scope_tier,
    }


def stamp_impl_result(
    payload: dict[str, Any],
    *,
    task: TicketTask,
    worktree: Path,
    facts: GitFacts,
    scope_tier: ScopeTier | None,
    plan_source: PlanSource,
    extra_friction: Sequence[str],
) -> AutoDevResult:
    """Stamp cw's fields over *payload* and validate it strictly.

    cw-stamped: ``schema_version``, ``ticket_id``, ``stage_reached``,
    ``branch``, ``worktree_path``, ``fork_point_sha``, ``commits`` (every
    ``fork..HEAD`` commit, newest first), ``pr`` and ``pr_created`` (null),
    ``review``, ``scope.files``, ``scope.lines_actual``, ``scope.tier``,
    ``plan_source`` and ``resolution_consumed``. Model-supplied and kept:
    ``status``, ``scope.forbidden_touched`` and ``scope.lines_estimate``,
    ``health``, ``next_actions`` and ``friction_highlights`` (to which
    *extra_friction* is appended). A model-reported ``blocked`` result gets the
    IMPL label and its blocker moved to ``stage2_impl``. Raises
    ``pydantic.ValidationError`` on a schema or invariant violation.
    """
    stamped: dict[str, Any] = {
        **payload,
        "schema_version": _SCHEMA_VERSION,
        "ticket_id": task.ticket_id,
        "stage_reached": STAGE2_IMPL,
        "branch": facts["branch"],
        "worktree_path": str(worktree),
        "fork_point_sha": facts["fork_point"] or None,
        "commits": list(facts["commits"]),
        "pr": None,
        "pr_created": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "plan_source": plan_source,
        "scope": _stamped_scope(payload, facts, scope_tier),
        "resolution_consumed": False,
        "resolution_evidence": None,
    }
    friction = stamped.get("friction_highlights")
    if isinstance(friction, list):
        stamped["friction_highlights"] = [*friction, *extra_friction]
    if stamped.get("status") == IMPL_BLOCKED:
        stamped["next_actions"] = list(_CODEX_IMPL_BLOCKED_NEXT_ACTIONS)
        blocker = stamped.get("blocker")
        if isinstance(blocker, dict):
            stamped["blocker"] = {**blocker, "stage": STAGE2_IMPL}
    return AutoDevResult.model_validate(stamped)


def impl_blocked(
    *,
    ticket_id: str,
    worktree: Path,
    reason: str,
    details: str,
    retry_eligible: bool = False,
    measured: tuple[int, int] | None = None,
) -> AutoDevResult:
    """A blocked IMPL result: ``stage2_impl`` and the impl ``next_actions``.

    With *measured* ``(files, lines)``, ``scope.files`` and
    ``scope.lines_actual`` carry cw's measurement of the refused turn's staged
    diff (the schema requires a non-null ``lines_actual`` at ``stage2_impl``).
    """
    result = make_codex_blocked(
        ticket_id=ticket_id,
        worktree=worktree,
        reason=reason,
        details=details,
        retry_eligible=retry_eligible,
        stage_reached=STAGE2_IMPL,
    )
    if measured is None:
        return result
    files, lines = measured
    scope = result.scope.model_copy(update={"files": files, "lines_actual": lines})
    return result.model_copy(update={"scope": scope})


def impl_empty_diff_result(
    *,
    ticket_id: str,
    worktree: Path,
    details: str,
    measured: tuple[int, int] | None,
) -> AutoDevResult:
    """The ``empty_diff_blocked`` result: the turn changed nothing against the fork.

    Built on :func:`impl_blocked` with the status swapped and the blocker
    carrying :data:`~cw.auto_dev_result.schema._vocab.EMPTY_DIFF_BLOCKER_REASON`
    and empty ``next_actions`` (a terminal-reject status takes none), then
    re-validated.
    """
    base = impl_blocked(
        ticket_id=ticket_id,
        worktree=worktree,
        reason=EMPTY_DIFF_BLOCKER_REASON,
        details=details,
        measured=measured,
    )
    blocker = Blocker(
        stage=STAGE2_IMPL, reason=EMPTY_DIFF_BLOCKER_REASON, details=details
    )
    swapped = base.model_copy(
        update={"status": EMPTY_DIFF_STATUS, "blocker": blocker, "next_actions": []}
    )
    return AutoDevResult.model_validate(swapped.model_dump(mode="json"))


def impl_commit_message(task: TicketTask, *, repair: bool) -> str:
    """cw's one IMPL commit message: subject, a blank line, then the trailer."""
    subject = (
        f"fix: address review feedback for #{task.ticket_id}"
        if repair
        else f"feat: implement #{task.ticket_id}"
    )
    return f"{subject}\n\n{IMPL_COMPLETE_TRAILER}\n"
