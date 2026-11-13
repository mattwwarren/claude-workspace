"""The PLAN turn's output document, the cw-stamped sentinel, and the artifacts.

cw, never the model, emits the PLAN sentinel (#3060 R5): the turn returns a
strict ``{plan_markdown, sentinel}`` document, the framed sentinel is parsed
with the strict :class:`~cw.auto_dev_result.AutoDevResult` validation (never
the lenient ``parse_stdout``), and every field cw owns is stamped over the
model's. The REVIEW stamper (``stamp_stage_facts``) is not used: it would
overwrite ``plan_source``, ``commits`` and ``fork_point_sha``.

cw also owns the two artifacts. ``.cw/plan-draft.md`` keeps its leading
bookkeeping block on every rewrite (cw writes the explicit round-0 line on a
fresh draft and never increments it); ``.cw/plan.md`` is written atomically
once both review stations are clean. :func:`read_resume_draft` is the read
side of the resume check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from cw.atomic import atomic_write_text, rotate_backup
from cw.auto_dev_result import AutoDevResult
from cw.auto_dev_result.parse import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    BLOCKER_REASON_VALIDATION_FAILED,
    extract_block,
)
from cw.codex_review import STAGE1_PLAN, make_codex_blocked
from cw.dev_queue.lifecycle import (
    _PLAN_SOUNDNESS_MARKER,
    _PLAN_SOUNDNESS_MARKER_VERSION,
    _PLAN_SPEC_MARKER,
    _PLAN_SPEC_MARKER_VERSION,
)
from cw.plan_fingerprint import (
    PLAN_DRAFT_FINGERPRINT_KEY,
    PLAN_DRAFT_RELATIVE_PATH,
    bind_claimed_fingerprint,
    compute_plan_draft_fingerprint,
    leading_bookkeeping_end,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: Where cw writes the reviewed plan, relative to the worktree.
PLAN_MD_RELATIVE_PATH: Final = PLAN_DRAFT_RELATIVE_PATH.parent / "plan.md"

#: The explicit round-counter default a fresh draft starts with. cw runs no
#: round-cap engine in C1 (#3464), so it never increments it.
ROUND_ZERO_LINE: Final = "<!-- plan-stage-scan-round: 0 -->\n"

#: The two review sign-off marker prefixes; only cw's stations stamp them.
REVIEW_MARKER_PREFIXES: Final[tuple[str, str]] = (
    _PLAN_SPEC_MARKER,
    _PLAN_SOUNDNESS_MARKER,
)
PLAN_SPEC_MARKER_VERSION: Final = _PLAN_SPEC_MARKER_VERSION
PLAN_SOUNDNESS_MARKER_VERSION: Final = _PLAN_SOUNDNESS_MARKER_VERSION

#: cw-recorded friction tokens (appended to the model's ``friction_highlights``).
RESUME_FRICTION: Final = (
    "resumed plan from .cw/plan-draft.md — prior blocked/parked attempt"
)
MODEL_TEXT_DISCARDED_FRICTION: Final = "plan_resume_model_text_discarded"
DECISIONS_NOT_RECORDED_PREFIX: Final = "plan_decisions_not_recorded: "
#: A completing run replaced an existing ``.cw/plan.md`` (#3498):
#: ``plan_md_replaced: old=<fp|unreadable> new=<fp> backup=<ok|failed>``.
PLAN_MD_REPLACED_PREFIX: Final = "plan_md_replaced: "
UNREADABLE_PLAN_MD: Final = "unreadable"

# PLAN status vocabulary shared by the turn parser and gate transitions.
PLAN_STAGE_COMPLETE: Final = "stage_complete"
PLAN_PENDING_APPROVAL: Final = "plan_pending_approval"
PLAN_AMBIGUITIES_PENDING: Final = "ambiguities_pending_resolution"
PLAN_PREMISES_PENDING: Final = "premises_pending_verification"
PLAN_NO_OP: Final = "no_op"
PLAN_BLOCKED: Final = "blocked"
PLAN_STATUSES: Final = frozenset(
    {
        PLAN_STAGE_COMPLETE,
        PLAN_PENDING_APPROVAL,
        PLAN_AMBIGUITIES_PENDING,
        PLAN_PREMISES_PENDING,
        PLAN_NO_OP,
        PLAN_BLOCKED,
    }
)
PARK_STATUSES: Final = frozenset(
    {PLAN_PENDING_APPROVAL, PLAN_AMBIGUITIES_PENDING, PLAN_PREMISES_PENDING}
)
PLAN_APPROVAL_NEXT_ACTIONS: Final = ("user_decide_plan_approval",)

_SENTINEL_OPEN: Final = "<<<AUTO_DEV_RESULT"
# Any 64-char lowercase hex digest: bind_claimed_fingerprint reads a non-null
# claim only as "a draft is in hand" and replaces it with its own digest.
_FINGERPRINT_CLAIM: Final = "0" * 64


class PlanTurnDocument(BaseModel):
    """The PLAN turn's ``-o`` document: the plan text and the framed sentinel."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    plan_markdown: str
    sentinel: str


@dataclass(frozen=True)
class PlanTurn:
    """A parsed turn: the model's plan text and the stamped, validated result."""

    plan_markdown: str
    result: AutoDevResult


@dataclass(frozen=True)
class PlanTurnFailure:
    """Why a turn's output could not become a result (a blocked reason)."""

    reason: str
    details: str


@dataclass(frozen=True)
class ResumeDraft:
    """A resumable ``.cw/plan-draft.md``: its text split at the bookkeeping block."""

    text: str
    leading: str
    body: str
    fingerprint: str


class PlanDraftError(OSError):
    """An existing draft cannot safely be read before a rewrite."""

    def __init__(self, path: Path, *, empty: bool) -> None:
        state = "empty" if empty else "unreadable"
        super().__init__(
            f"existing plan draft is {state}: {path}; repair or remove it "
            "before re-planning"
        )


def _cw_fields(ticket_id: str, worktree: Path) -> dict[str, Any]:
    return {
        "schema_version": 4,
        "ticket_id": ticket_id,
        "stage_reached": STAGE1_PLAN,
        "branch": None,
        "worktree_path": str(worktree),
        "fork_point_sha": None,
        "commits": [],
        "pr": None,
        "pr_created": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "resolution_consumed": False,
        "resolution_evidence": None,
        PLAN_DRAFT_FINGERPRINT_KEY: None,
    }


def stamp_plan_result(
    payload: dict[str, Any],
    *,
    ticket_id: str,
    worktree: Path,
    extra_friction: Sequence[str] = (),
    bind_draft: bool = False,
) -> AutoDevResult:
    """Stamp cw's fields over *payload* and validate it strictly.

    cw-stamped: ``schema_version``, ``ticket_id``, ``stage_reached``,
    ``worktree_path``, ``branch``, ``fork_point_sha``, ``commits``, ``pr``,
    ``review``, ``scope.lines_actual`` (null), ``resolution_consumed`` /
    ``resolution_evidence`` (cw runs no settlement in C1, so never credited)
    and ``plan_draft_fingerprint``. Model-supplied and kept: ``status``,
    ``plan_source``, the rest of ``scope``, ``ambiguities``, ``premises``,
    ``health``, ``next_actions``, ``blocker`` and ``friction_highlights`` (to
    which *extra_friction* is appended).

    With *bind_draft* the fingerprint is cw's digest of the on-disk draft
    (``bind_claimed_fingerprint``), else null. Raises
    ``pydantic.ValidationError`` on a schema or invariant violation.
    """
    stamped = {**payload, **_cw_fields(ticket_id, worktree)}
    scope = stamped.get("scope")
    if isinstance(scope, dict):
        stamped["scope"] = {**scope, "lines_actual": None}
    friction = stamped.get("friction_highlights")
    if isinstance(friction, list):
        stamped["friction_highlights"] = [*friction, *extra_friction]
    draft_path = worktree / PLAN_DRAFT_RELATIVE_PATH
    if bind_draft and draft_path.is_file():
        stamped[PLAN_DRAFT_FINGERPRINT_KEY] = _FINGERPRINT_CLAIM
        bind_claimed_fingerprint(stamped, draft_path)
    return AutoDevResult.model_validate(stamped)


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


def parse_plan_turn(
    content: str | None, *, ticket_id: str, worktree: Path
) -> PlanTurn | PlanTurnFailure:
    """Parse the ``-o`` *content* strictly into a stamped result.

    A missing, empty or invalid document is ``no_result_emitted``; a sentinel
    that is not exactly one framed block, is not JSON, fails the schema, or
    carries a non-plan status is ``validation_failed``.
    """
    if content is None or not content.strip():
        return PlanTurnFailure(
            BLOCKER_REASON_NO_RESULT_EMITTED, "the turn wrote no -o document"
        )
    try:
        document = PlanTurnDocument.model_validate_json(content)
    except ValidationError as exc:
        details = f"the -o document is not a {{plan_markdown, sentinel}} object: {exc}"
        return PlanTurnFailure(BLOCKER_REASON_NO_RESULT_EMITTED, details)
    payload = _sentinel_payload(document.sentinel)
    if isinstance(payload, str):
        return PlanTurnFailure(BLOCKER_REASON_VALIDATION_FAILED, payload)
    status = payload.get("status")
    if not isinstance(status, str) or status not in PLAN_STATUSES:
        details = f"status {status!r} is not a plan-stage status"
        return PlanTurnFailure(BLOCKER_REASON_VALIDATION_FAILED, details)
    try:
        result = stamp_plan_result(payload, ticket_id=ticket_id, worktree=worktree)
    except ValidationError as exc:
        return PlanTurnFailure(
            BLOCKER_REASON_VALIDATION_FAILED, f"sentinel failed validation: {exc}"
        )
    return PlanTurn(plan_markdown=document.plan_markdown, result=result)


def read_resume_draft(worktree: Path) -> ResumeDraft | None:
    """The resumable draft, or ``None`` (the read side of ``plan.resume-check``).

    A draft resumes only when ``.cw/plan-draft.md`` is readable UTF-8 and
    non-empty and ``.cw/plan.md`` does not exist (a written plan supersedes it).
    """
    if (worktree / PLAN_MD_RELATIVE_PATH).exists():
        return None
    try:
        text = (worktree / PLAN_DRAFT_RELATIVE_PATH).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    end = leading_bookkeeping_end(text)
    return ResumeDraft(
        text=text,
        leading=text[:end],
        body=text[end:],
        fingerprint=compute_plan_draft_fingerprint(text),
    )


def clean_plan_text(text: str) -> str:
    """Strip a leading bookkeeping block and every review-marker line.

    Bookkeeping and sign-off markers are cw's: a model-emitted leading block
    would duplicate or shift the fingerprint, and a model-emitted marker would
    forge a sign-off. A bookkeeping-looking line later in the body is plan
    content by the grammar and stays.
    """
    body = text[leading_bookkeeping_end(text) :]
    kept = [
        line
        for line in body.splitlines(keepends=True)
        if not line.lstrip().startswith(REVIEW_MARKER_PREFIXES)
    ]
    return "".join(kept)


def _read_on_disk_draft(worktree: Path) -> str | None:
    """The on-disk draft's text, ``None`` when absent.

    Raises :class:`PlanDraftError` when it exists but is unreadable or empty,
    so a rewrite never replaces a draft cw could not read first.
    """
    path = worktree / PLAN_DRAFT_RELATIVE_PATH
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlanDraftError(path, empty=False) from exc
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanDraftError(path, empty=False) from exc
    if not text.strip():
        raise PlanDraftError(path, empty=True)
    return text


def write_plan_draft(worktree: Path, plan_text: str) -> Path:
    """Write ``.cw/plan-draft.md``: the carried leading block, then the body.

    The on-disk leading block (any lane's round line, last-evaluated,
    settled or revocation lines) is carried verbatim; with none, the explicit
    round-0 line is written. *plan_text* is cleaned first. A prior draft that
    differs from the new text is snapshotted first (``plan-draft.md.bak-<ns>``,
    best-effort, newest 5 kept), so a re-park never silently discards an
    operator's edit (#3490).
    """
    prior = _read_on_disk_draft(worktree)
    carried = prior[: leading_bookkeeping_end(prior)] if prior is not None else ""
    leading = carried or ROUND_ZERO_LINE
    body = clean_plan_text(plan_text)
    if not body.endswith("\n"):
        body += "\n"
    text = leading + body
    path = worktree / PLAN_DRAFT_RELATIVE_PATH
    if prior is not None and prior != text:
        rotate_backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)
    return path


def draft_fingerprint_on_disk(worktree: Path) -> str | None:
    """The fingerprint of the on-disk ``.cw/plan-draft.md``, ``None`` if unreadable.

    The disk is the truth (#3526): the same rule ``bind_claimed_fingerprint``
    applies, so a blocked exit never re-derives the draft's normalization.
    """
    try:
        text = (worktree / PLAN_DRAFT_RELATIVE_PATH).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return compute_plan_draft_fingerprint(text)


def write_plan_md(worktree: Path, text: str) -> Path:
    """Write ``.cw/plan.md`` atomically."""
    path = worktree / PLAN_MD_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)
    return path


def existing_plan_md_fingerprint(worktree: Path) -> str | None:
    """The existing ``.cw/plan.md``'s raw-text fingerprint (#3498).

    ``None`` when there is none, :data:`UNREADABLE_PLAN_MD` when it cannot be
    read as UTF-8 text.
    """
    path = worktree / PLAN_MD_RELATIVE_PATH
    if not path.exists():
        return None
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return UNREADABLE_PLAN_MD
    return compute_plan_draft_fingerprint(text)


def backup_plan_md(worktree: Path) -> bool:
    """Snapshot the existing ``.cw/plan.md``; ``False`` when that failed."""
    return rotate_backup(worktree / PLAN_MD_RELATIVE_PATH) is not None


def plan_md_replaced_token(old: str, new: str, *, backup_ok: bool) -> str:
    """The friction token auditing a replaced ``.cw/plan.md`` (#3498)."""
    backup = "ok" if backup_ok else "failed"
    return f"{PLAN_MD_REPLACED_PREFIX}old={old} new={new} backup={backup}"


def plan_blocked(
    *,
    ticket_id: str,
    worktree: Path,
    reason: str,
    details: str,
    retry_eligible: bool = False,
    plan_draft_fingerprint: str | None = None,
) -> AutoDevResult:
    """A blocked PLAN result: ``stage1_plan`` and the plan ``next_actions``."""
    result = make_codex_blocked(
        ticket_id=ticket_id,
        worktree=worktree,
        reason=reason,
        details=details,
        retry_eligible=retry_eligible,
        stage_reached=STAGE1_PLAN,
    )
    if plan_draft_fingerprint is not None:
        result = result.model_copy(
            update={PLAN_DRAFT_FINGERPRINT_KEY: plan_draft_fingerprint}
        )
    return result
