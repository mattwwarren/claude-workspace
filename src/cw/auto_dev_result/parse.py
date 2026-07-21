"""Parser for the ``<<<AUTO_DEV_RESULT`` sentinel block.

The headless ``/auto-dev`` skill emits a sentinel-delimited JSON block as the
final lines of stdout summarizing the pipeline outcome. ``cw`` parses that
block to persist a structured view on the worker Session. This module owns the
*parsing half* of the contract: sentinel extraction, JSON decode,
producer-drift coercion, and :func:`parse_stdout`. The *schema half* (the
:class:`AutoDevResult` model, its nested models, and the status/stage/scope
vocabulary) lives in :mod:`cw.auto_dev_result.schema`.

Spec: ``docs/headless-contract.md`` (§3 framing, §4 enum, §5 health, §6
failure modes). Package split: issue #1321.

Public surface:

- :func:`parse_stdout` — accepts raw stdout, returns either a parsed
  ``AutoDevResult`` or a synthetic ``BlockedResult`` describing why the
  payload was unusable. Never raises on malformed input.
- :func:`extract_block` — low-level helper that locates the LAST sentinel
  pair and returns the inner JSON text (no parsing).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from cw.auto_dev_result.schema import (
    _STAGE_REACHED_ALIASES,
    USER_DIRECTED_PREFIXES,
    AutoDevResult,
    BlockedResult,
    Blocker,
    _has_usable_premise_text,
    _has_usable_question,
    _is_blank,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger("cw.auto_dev_result")

# v1 is the legacy shape; v2 adds the `no_op` status; v3 adds the
# `stage1_pre_flight` stage_reached value and `none` plan_source (used
# together for pre-flight no_op exits); v4 promotes
# `ambiguities_pending_resolution` and `premises_pending_verification` to
# canonical closed-enum statuses (issue #191). v5 adds the advisory
# `review.agents_run` count (reviewer agents that ran) alongside the
# executor-neutral review-verdict contract (issue #1237). All are accepted
# during the rollout window — see docs/headless-contract.md §8.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1, 2, 3, 4, 5})
AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION: int = max(SUPPORTED_SCHEMA_VERSIONS)

_OPEN_SENTINEL = "<<<AUTO_DEV_RESULT"
_CLOSE_SENTINEL = "AUTO_DEV_RESULT>>>"
# A "complete" block runs from a line containing the open sentinel to a
# subsequent line containing only the close sentinel. We take the LAST such
# block per §3.1 — narrative above the real block may legitimately quote
# the literal sentinel string (e.g., this docstring).
_BLOCK_RE = re.compile(
    r"<<<AUTO_DEV_RESULT\s*\n(.*?)\nAUTO_DEV_RESULT>>>",
    re.DOTALL,
)
# Fallback locator for sentinels emitted as bare code-fenced JSON without
# AUTO_DEV_RESULT markers (GitHub #337). Matches ```json or ``` fenced blocks.
_LOOSE_FENCE_RE = re.compile(
    r"```(?:json)?\n(.*?)\n```",
    re.DOTALL,
)

# Keep the last-N-lines payload bounded so synthetic blocker details don't
# bloat the persisted state file. 40 lines is enough to capture a typical
# pre-crash traceback without dragging in megabytes of pane scrollback.
_TAIL_LINES = 40

# BlockedResult reason codes produced by parse_stdout.  Exported so consumers
# (e.g. cli.py) can reference them without duplicating the literal strings.
BLOCKER_REASON_MULTIPLE_RESULT_BLOCKS = "multiple_result_blocks"
BLOCKER_REASON_NO_RESULT_EMITTED = "no_result_emitted"
BLOCKER_REASON_PRIOR_PIPELINE_PR_OPEN = "prior_pipeline_pr_open"
BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
BLOCKER_REASON_STATUS_UNKNOWN = "status_unknown"
BLOCKER_REASON_VALIDATION_FAILED = "validation_failed"

# Placeholder field values from the documented illustrative example in the
# /auto-dev skill prompt. A sentinel matching all three is the example block,
# not a real result. Multiple fields required — never reject on pr.number==42
# alone (a real first PR can legitimately be #42).
_EXAMPLE_PR_NUMBER = 42
_EXAMPLE_TICKET_ID = "PROJ-1234"
_EXAMPLE_BRANCH_PREFIX = "dev/proj-1234"

# Synthetic placeholder question injected when an ambiguities array survives
# the empty-question filter with nothing left (issue #953). Parks the ticket
# visibly as a producer glitch rather than silently with a null question.
_AMBIGUITY_GLITCH_PLACEHOLDER_QUESTION = (
    "(producer emitted no usable ambiguity — operator: requeue or "
    "investigate; see #953)"
)

# Synthetic placeholder claim injected when a premises array survives the
# empty-claim/premise filter with nothing left (issue #962, sibling of #953).
# Parks the ticket visibly as a producer glitch rather than silently with no
# usable claim to verify.
_PREMISE_GLITCH_PLACEHOLDER_CLAIM = (
    "(producer emitted no usable premise — operator: requeue or investigate; see #962)"
)


def _warn_once(
    message: str,
    *args: object,
    warned_blocks: set[str] | None,
    block_key: str | None,
) -> None:
    """Log *message* at WARNING, deduped per (block_key, rendered message) pair.

    Issue #1247: ``cw dev-queue wait``'s poll loop re-parses the full
    transcript every 5s, so an unresolved malformed sentinel re-triggers the
    identical warning on every poll for the life of the wait. Callers that
    opt in by passing a caller-owned ``warned_blocks`` set (content-hash
    keyed on the sentinel text via ``block_key``) get each distinct warning
    logged exactly once per block; every other caller (``warned_blocks`` or
    ``block_key`` left ``None``, the default) gets today's un-deduped
    behavior. Keyed on ``(block_key, rendered message)`` — the message
    formatted with its args, not the bare template — so two independent
    warnings about the same block that happen to share a log template but
    carry different args (e.g. ``_filter_empty_string_items`` called for
    both ``commits`` and ``friction_highlights`` on the same payload) each
    still surface once rather than the first suppressing the second.
    """
    if warned_blocks is None or block_key is None:
        _log.warning(message, *args)
        return
    rendered = message % args if args else message
    entry_key = f"{block_key}:{rendered}"
    if entry_key not in warned_blocks:
        _log.warning(message, *args)
        warned_blocks.add(entry_key)


def is_documented_example(result: AutoDevResult) -> bool:
    """Return True iff *result* matches the illustrative example in the skill prompt.

    Used by transcript scanners to skip the example sentinel block when the
    worker quotes it before emitting the real result (GitHub #591).
    """
    return (
        result.pr is not None
        and result.pr.number == _EXAMPLE_PR_NUMBER
        and result.ticket_id == _EXAMPLE_TICKET_ID
        and result.branch is not None
        and result.branch.startswith(_EXAMPLE_BRANCH_PREFIX)
    )


_PLACEHOLDER_TICKET_ID_RE = re.compile(r'"ticket_id"\s*:\s*"<')
_PLACEHOLDER_STATUS_RE = re.compile(r'"status"\s*:\s*"<')


def _is_placeholder_sentinel_text(raw: str) -> bool:
    """Return True iff *raw* sentinel block text is an unresolved doc-example.

    Detects the .claude/commands/auto-dev-*.md worked-example blocks whose
    ticket_id/status values are angle-bracket placeholders (e.g.
    "<ticket-id>", "<stage_complete | blocked>") BEFORE the JSON is parsed.
    A placeholder payload fails schema validation (status not in
    _KNOWN_STATUSES) and returns a BlockedResult -- a type
    is_documented_example() cannot inspect, since it operates on parsed
    AutoDevResult fields that a failed parse never produces (#1266).

    Deliberately narrow (both keys, leading '<' right after the opening
    quote): no real producer ever emits ticket_id/status beginning with
    '<', so this cannot misfire on genuine output. Do not broaden to
    "looks templated" -- silently dropping a genuine result is a strictly
    worse bug than the one this fixes.
    """
    return bool(
        _PLACEHOLDER_TICKET_ID_RE.search(raw) and _PLACEHOLDER_STATUS_RE.search(raw)
    )


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _strip_code_fence(raw: str) -> str:
    """Strip a markdown code fence wrapper from a sentinel block payload.

    Only strips known-safe language specs (json or plain). Unknown specs
    (e.g. typescript) and missing closing fences are left for json.loads
    to reject loudly.
    """
    for prefix in ("```json\n", "```\n"):
        if raw.startswith(prefix) and raw.endswith("\n```"):
            return raw[len(prefix) : -4]
    return raw


def _extract_loose_sentinel_json(text: str) -> str | None:
    """Scan for the last code-fenced block that parses as an auto-dev payload.

    Used as a fallback when ``parse_stdout`` finds no AUTO_DEV_RESULT markers
    (GitHub #337 — producer occasionally emits the payload in a code fence
    without the sentinel framing). Accepts only blocks whose inner JSON is a
    dict containing both ``schema_version`` and ``status`` keys, distinguishing
    an auto-dev result from unrelated code blocks in the output.
    """
    for m in reversed(list(_LOOSE_FENCE_RE.finditer(text))):
        candidate = m.group(1).strip()
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "schema_version" in obj and "status" in obj:
            return candidate
    return None


def extract_block(text: str) -> str | None:
    """Return the JSON text inside the LAST complete sentinel pair, or None.

    Does not parse the JSON. Returns None if no complete pair is found —
    callers must distinguish "no opening sentinel at all" from "opening
    present but no close" themselves if they care (see :func:`parse_stdout`).
    """
    matches = list(_BLOCK_RE.finditer(text))
    if not matches:
        return None
    return matches[-1].group(1)


_KNOWN_STATUSES: frozenset[str] = frozenset(
    {
        "shipped",
        "stage_complete",
        "plan_pending_approval",
        "review_pending_approval",
        "merge_gate_blocked",
        "merge_pending",
        "scope_exceeded",
        "forbidden_area",
        "blocked",
        "no_op",
        "ambiguities_pending_resolution",
        "premises_pending_verification",
    }
)
_PRE_IMPL_STAGES: frozenset[str] = frozenset({"stage1_pre_flight", "stage1_plan"})


def _effective_stage(payload: dict[str, Any]) -> object:
    """Resolve ``stage_reached`` through the alias table (raw value if no alias)."""
    raw_stage = payload.get("stage_reached", "")
    if isinstance(raw_stage, str):
        return _STAGE_REACHED_ALIASES.get(raw_stage, raw_stage)
    return raw_stage


def _locate_raw_block(
    text: str,
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> str | BlockedResult:
    """Locate the single sentinel payload in *text* or describe why it's unusable.

    Returns the inner JSON text (sentinel-framed or loose code-fenced fallback)
    or a :class:`BlockedResult` for §6 (1), (2), and (6) framing failures.
    """
    # §6 (6) multi-block detection comes first: even if the LAST block is
    # well-formed, the contract says exactly one per invocation.
    matches = list(_BLOCK_RE.finditer(text))
    if len(matches) > 1:
        last_payload = matches[-1].group(1)
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_MULTIPLE_RESULT_BLOCKS,
                details=f"count={len(matches)}; last_block={last_payload}",
            ),
        )

    if matches:
        return _strip_code_fence(matches[0].group(1))

    if _OPEN_SENTINEL in text:
        # §6 (2) opening sentinel present, close missing — skill crashed mid-emit
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_NO_RESULT_EMITTED,
                details=(
                    f"opening sentinel present, close missing; tail:\n{_tail(text)}"
                ),
            ),
        )

    # §6 (1) No AUTO_DEV_RESULT markers. Tolerate bare code-fenced JSON:
    # the producer occasionally emits the payload in a ``` block without
    # sentinel framing (GitHub #337). Accept iff the last fenced block
    # parses as a JSON object with both schema_version and status.
    loose_json = _extract_loose_sentinel_json(text)
    if loose_json is None:
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_NO_RESULT_EMITTED,
                details=f"no sentinel block in stdout; tail:\n{_tail(text)}",
            ),
        )
    _warn_once(
        "auto-dev: sentinel emitted as bare code-fenced JSON without "
        "AUTO_DEV_RESULT markers; using loose fallback (GitHub #337)",
        warned_blocks=warned_blocks,
        block_key=block_key,
    )
    return loose_json


def _decode_payload(
    raw_block: str,
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> dict[str, Any] | BlockedResult:
    """Decode the sentinel JSON and pre-validate version/status (§6 (3)-(5)).

    Returns the payload dict on success or a :class:`BlockedResult` for the
    parse/shape/version/status failure modes.
    """
    # §6 (3) JSON does not parse.
    try:
        payload: Any = json.loads(raw_block)
    except json.JSONDecodeError as exc:
        _warn_once(
            "auto-dev sentinel block did not parse as JSON: %s",
            exc,
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_NO_RESULT_EMITTED,
                details=f"sentinel block JSON parse failed ({exc}); raw:\n{raw_block}",
            ),
        )

    if not isinstance(payload, dict):
        type_name = type(payload).__name__
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_NO_RESULT_EMITTED,
                details=f"sentinel block was not a JSON object (got {type_name})",
            ),
        )

    # §6 (4) schema_version higher than supported, and the related case where
    # the field is missing or non-int. Pre-validate before handing to Pydantic
    # so the caller gets a structured surface instead of a ValidationError.
    raw_version = payload.get("schema_version")

    if (
        isinstance(raw_version, int)
        and raw_version == AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION + 1
    ):
        # One-version look-ahead: schema-bump PR self-shipped while the running
        # parser is still at N. Best-effort parse with the current max schema so
        # the shipped result is recognised rather than mis-flagged as a failure.
        # (issue #395 / headless-contract.md §6(4))
        _warn_once(
            "auto-dev sentinel schema_version=%r is one ahead of parser max=%r; "
            "best-effort parse using schema %r (schema-bump skew tolerance)",
            raw_version,
            AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION,
            AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION,
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
        payload["schema_version"] = AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION

    elif (
        not isinstance(raw_version, int) or raw_version not in SUPPORTED_SCHEMA_VERSIONS
    ):
        _warn_once(
            "auto-dev sentinel schema_version=%r unsupported (parser supports %s)",
            raw_version,
            sorted(SUPPORTED_SCHEMA_VERSIONS),
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED,
                details=(
                    f"got schema_version={raw_version!r}, "
                    f"parser supports {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
                ),
            ),
        )

    # §6 (5) unknown status — short-circuit before Pydantic raises a
    # ValidationError on the closed Literal.
    raw_status = payload.get("status")
    if raw_status not in _KNOWN_STATUSES:
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_STATUS_UNKNOWN,
                details=(
                    f"got status={raw_status!r}; surface verbatim, do not auto-route"
                ),
            ),
        )

    return payload


def _has_usable_agent_id(item: dict[str, Any]) -> bool:
    """Return True iff *item* carries a non-empty, non-whitespace 'agent_id' (#1130)."""
    agent_id = item.get("agent_id")
    return isinstance(agent_id, str) and not _is_blank(agent_id)


def _coerce_no_op_strays(
    payload: dict[str, Any],
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop stray pr/branch/commits/lines_actual on a no_op payload (issue #367)."""
    stray: list[str] = []
    if payload.get("pr") is not None:
        stray.append("pr")
        payload["pr"] = None
    if payload.get("branch") is not None:
        stray.append("branch")
        payload["branch"] = None
    if payload.get("commits"):
        stray.append("commits")
        payload["commits"] = []
    # Coerce stray scope.lines_actual on pre-impl exits (issue #399).
    # A no_op at stage1_pre_flight or stage1_plan exited before any
    # implementation work; lines_actual must be null. The producer
    # sometimes emits a non-null value, tripping the §3.3 cross-field
    # invariant and causing the sentinel to fail as validation_failed.
    scope_dict = payload.get("scope")
    if (
        isinstance(scope_dict, dict)
        and scope_dict.get("lines_actual") is not None
        and _effective_stage(payload) in _PRE_IMPL_STAGES
    ):
        stray.append("scope.lines_actual")
        scope_dict["lines_actual"] = None
    if stray:
        _warn_once(
            "auto-dev: no_op sentinel carried non-null %s; coercing to clean "
            "no_op (ticket=%s, schema_version=%s)",
            stray,
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
            warned_blocks=warned_blocks,
            block_key=block_key,
        )


def _coerce_terminal_strays(
    payload: dict[str, Any],
    raw_status: str,
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop stray branch/commits/lines_actual on scope_exceeded/forbidden_area.

    Issue #430 case 4. Post-impl stages require non-null lines_actual per §3.3;
    lines_actual is only coerced on pre-impl exits (same rule as no_op).
    """
    stray_term: list[str] = []
    if payload.get("branch") is not None:
        stray_term.append("branch")
        payload["branch"] = None
    if payload.get("commits"):
        stray_term.append("commits")
        payload["commits"] = []
    scope_dict_term = payload.get("scope")
    if (
        isinstance(scope_dict_term, dict)
        and scope_dict_term.get("lines_actual") is not None
        and _effective_stage(payload) in _PRE_IMPL_STAGES
    ):
        stray_term.append("scope.lines_actual")
        scope_dict_term["lines_actual"] = None
    if stray_term:
        _warn_once(
            "auto-dev: %s sentinel carried non-null %s; coercing to clean "
            "%s (ticket=%s, schema_version=%s)",
            raw_status,
            stray_term,
            raw_status,
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
            warned_blocks=warned_blocks,
            block_key=block_key,
        )


def _coerce_empty_pending_array(
    payload: dict[str, Any],
    key: str,
    raw_status: str,
    placeholder: list[dict[str, Any]] | None = None,
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Inject a minimal placeholder for an empty ambiguities/premises array.

    Issue #430 case 1 — accept empty arrays at the parse boundary so the §4.4
    A5 invariant does not turn producer drift into validation_failed. Both
    callers now pass a labeled *placeholder* (ambiguities: #953, premises:
    #962) so an empty/missing array parks the ticket with a synthetic,
    clearly-labeled item rather than a silent ``[{}]`` default.
    """
    if not payload.get(key):  # None or [] both need coercing
        _warn_once(
            "auto-dev: %s sentinel has empty %s; coercing to minimal "
            "placeholder (ticket=%s, schema_version=%s)",
            raw_status,
            key,
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
        payload[key] = placeholder if placeholder is not None else [{}]


def _filter_empty_pending_items(
    payload: dict[str, Any],
    key: str,
    predicate: Callable[[dict[str, Any]], bool],
    empty_desc: str,
    issue_ref: str,
    log_context: dict[str, Any] | None = None,
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop *key* items failing *predicate*, generalized across #953/#962.

    Non-dict items are left in place (isinstance guard) so they fail loudly
    at strict model_validate rather than being silently dropped.

    *log_context* supplies the dict to read ``ticket_id``/``schema_version``
    from for the warning log — defaults to *payload* itself. Needed for
    #1130's ``health.agent_health_summary`` filter, where *payload* is the
    nested ``health`` dict and the ticket id lives on the outer payload.
    """
    raw = payload.get(key)
    if not isinstance(raw, list):
        return
    filtered = [item for item in raw if not isinstance(item, dict) or predicate(item)]
    if len(filtered) != len(raw):
        ctx = log_context if log_context is not None else payload
        _warn_once(
            "auto-dev: dropped %d %s item(s) with %s at parse boundary "
            "(ticket=%s, schema_version=%s); see %s",
            len(raw) - len(filtered),
            key,
            empty_desc,
            ctx.get("ticket_id", "unknown"),
            ctx.get("schema_version"),
            issue_ref,
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
    payload[key] = filtered


def _filter_empty_question_ambiguities(
    payload: dict[str, Any],
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop ambiguity items with an empty/missing question (issue #953)."""
    _filter_empty_pending_items(
        payload,
        "ambiguities",
        _has_usable_question,
        "empty/missing 'question'",
        "#953",
        warned_blocks=warned_blocks,
        block_key=block_key,
    )


def _filter_empty_claim_premises(
    payload: dict[str, Any],
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop premise items with no usable claim/premise text (issue #962)."""
    _filter_empty_pending_items(
        payload,
        "premises",
        _has_usable_premise_text,
        "no usable 'claim'/'premise' text",
        "#962",
        warned_blocks=warned_blocks,
        block_key=block_key,
    )


def _filter_empty_string_items(
    payload: dict[str, Any],
    key: str,
    issue_ref: str,
    log_context: dict[str, Any] | None = None,
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop blank/whitespace-only string items from *key*, sibling of
    _filter_empty_pending_items but for bare string items rather than dict
    items (issue #1130).

    Non-string items are left in place (isinstance guard) so they fail loudly
    at strict model_validate rather than being silently dropped.
    """
    raw = payload.get(key)
    if not isinstance(raw, list):
        return
    filtered = [item for item in raw if not isinstance(item, str) or bool(item.strip())]
    if len(filtered) != len(raw):
        ctx = log_context if log_context is not None else payload
        _warn_once(
            "auto-dev: dropped %d %s item(s) with empty/whitespace-only string "
            "at parse boundary (ticket=%s, schema_version=%s); see %s",
            len(raw) - len(filtered),
            key,
            ctx.get("ticket_id", "unknown"),
            ctx.get("schema_version"),
            issue_ref,
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
    payload[key] = filtered


def _get_health_dict(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return payload['health'] iff it is a dict, else None (issue #1130).

    Shared guard for the two health.* filters below — both need to bail out
    the same way when a payload predates the health block or has it malformed.
    """
    health = payload.get("health")
    return health if isinstance(health, dict) else None


def _filter_empty_agent_health_summary(
    payload: dict[str, Any],
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop agent_health_summary entries with a blank agent_id (issue #1130)."""
    health = _get_health_dict(payload)
    if health is None:
        return
    _filter_empty_pending_items(
        health,
        "agent_health_summary",
        _has_usable_agent_id,
        "empty/missing 'agent_id'",
        "#1130",
        log_context=payload,
        warned_blocks=warned_blocks,
        block_key=block_key,
    )


def _filter_empty_health_shortcuts(
    payload: dict[str, Any],
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop blank/whitespace-only health.shortcuts items (issue #1130)."""
    health = _get_health_dict(payload)
    if health is None:
        return
    _filter_empty_string_items(
        health,
        "shortcuts",
        "#1130",
        log_context=payload,
        warned_blocks=warned_blocks,
        block_key=block_key,
    )


def _coerce_blocked_next_actions(
    payload: dict[str, Any],
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Drop stray next_actions on a blocked payload, preserving the blocker.

    Issue #371. Two legitimate shapes carry next_actions on blocked and MUST
    NOT be coerced: pre-flight blocked (stage_reached='stage1_pre_flight'), and
    user-directed blocked (all next_actions start with user_* prefixes).
    """
    raw_next_actions = payload.get("next_actions")
    if not (isinstance(raw_next_actions, list) and raw_next_actions):
        return
    is_pre_flight = payload.get("stage_reached") == "stage1_pre_flight"
    is_user_directed = all(
        isinstance(a, str) and a.startswith(USER_DIRECTED_PREFIXES)
        for a in raw_next_actions
    )
    if not is_pre_flight and not is_user_directed:
        _warn_once(
            "auto-dev: blocked sentinel carried stray next_actions=%r; "
            "dropping next_actions, preserving blocker "
            "(ticket=%s, schema_version=%s)",
            raw_next_actions,
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
        payload["next_actions"] = []


def _coerce_blocked_with_pr(
    payload: dict[str, Any],
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Coerce status=blocked+non-null pr to merge_pending (issue #899).

    FINALIZE creates a PR then can't merge (CI pending). The producer emits
    status="blocked" with a non-null pr field — rejected by the model validator.
    Coerce to merge_pending to preserve the PR url and avoid recording failed.
    The blocker and next_actions fields are cleared since merge_pending carries
    neither.
    """
    if payload.get("pr") is None:
        return
    # §5.1: downgrade_applied=True requires status=review_pending_approval.
    # Coercing to merge_pending would swap one validation failure for another.
    health = payload.get("health")
    if isinstance(health, dict) and health.get("downgrade_applied"):
        _warn_once(
            "auto-dev: blocked+pr with downgrade_applied=True (ticket=%s); "
            "skipping merge_pending coerce — §5.1 constraint applies",
            payload.get("ticket_id", "unknown"),
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
        return
    _warn_once(
        "auto-dev: blocked sentinel has non-null pr; coercing to "
        "merge_pending to preserve PR url (issue #899, ticket=%s)",
        payload.get("ticket_id", "unknown"),
        warned_blocks=warned_blocks,
        block_key=block_key,
    )
    payload["status"] = "merge_pending"
    payload["blocker"] = None
    payload["next_actions"] = []


def _coerce_pre_impl_zero_lines(
    payload: dict[str, Any],
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Coerce lines_actual=0 to null on pre-impl stages (issue #416).

    Only integer 0 is coerced; any other non-null value stays intact (hard
    error per §3.3). Status-agnostic, unlike the no_op coerce.
    """
    scope_gen = payload.get("scope")
    if (
        isinstance(scope_gen, dict)
        and _effective_stage(payload) in _PRE_IMPL_STAGES
        and scope_gen.get("lines_actual") == 0
    ):
        _warn_once(
            "auto-dev: pre-impl sentinel had lines_actual=0; coercing to null "
            "(ticket=%s, schema_version=%s)",
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
        scope_gen["lines_actual"] = None


def _coerce_shipped_wait_for_ci(
    payload: dict[str, Any],
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Inject wait_for_ci on a shipped payload that omits it (issue #417)."""
    na = payload.get("next_actions")
    if isinstance(na, list) and "wait_for_ci" not in na:
        _warn_once(
            "auto-dev: shipped sentinel missing wait_for_ci; injecting "
            "(ticket=%s, schema_version=%s)",
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
        payload["next_actions"] = [*na, "wait_for_ci"]


def _normalize_payload(
    payload: dict[str, Any],
    raw_status: str,
    *,
    warned_blocks: set[str] | None = None,
    block_key: str | None = None,
) -> None:
    """Apply all parse-boundary leniency coercions in place (producer drift).

    Each coercion is a documented, status-gated relaxation of a §3/§4 invariant
    that the strict ``model_validate`` still enforces. See the individual
    ``_coerce_*`` helpers for the per-issue rationale.
    """
    # Status-agnostic (issue #1130): must run before any downstream coercion
    # reads these fields — in particular _coerce_blocked_next_actions (below)
    # reads next_actions to classify is_user_directed, and must see the
    # already-filtered list or a blank item could cause a wrong coercion
    # decision.
    _filter_empty_string_items(
        payload, "commits", "#1130", warned_blocks=warned_blocks, block_key=block_key
    )
    _filter_empty_string_items(
        payload,
        "friction_highlights",
        "#1130",
        warned_blocks=warned_blocks,
        block_key=block_key,
    )
    _filter_empty_string_items(
        payload,
        "next_actions",
        "#1130",
        warned_blocks=warned_blocks,
        block_key=block_key,
    )
    _filter_empty_health_shortcuts(
        payload, warned_blocks=warned_blocks, block_key=block_key
    )
    _filter_empty_agent_health_summary(
        payload, warned_blocks=warned_blocks, block_key=block_key
    )
    if raw_status == "no_op":
        _coerce_no_op_strays(payload, warned_blocks=warned_blocks, block_key=block_key)
    if raw_status in ("scope_exceeded", "forbidden_area"):
        _coerce_terminal_strays(
            payload, raw_status, warned_blocks=warned_blocks, block_key=block_key
        )
    if raw_status == "ambiguities_pending_resolution":
        _filter_empty_question_ambiguities(
            payload, warned_blocks=warned_blocks, block_key=block_key
        )
        _coerce_empty_pending_array(
            payload,
            "ambiguities",
            raw_status,
            placeholder=[{"question": _AMBIGUITY_GLITCH_PLACEHOLDER_QUESTION}],
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
    if raw_status == "premises_pending_verification":
        _filter_empty_claim_premises(
            payload, warned_blocks=warned_blocks, block_key=block_key
        )
        _coerce_empty_pending_array(
            payload,
            "premises",
            raw_status,
            placeholder=[{"claim": _PREMISE_GLITCH_PLACEHOLDER_CLAIM}],
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
    if raw_status == "blocked":
        # may change status to merge_pending
        _coerce_blocked_with_pr(
            payload, warned_blocks=warned_blocks, block_key=block_key
        )
        if payload.get("status") == "blocked":
            _coerce_blocked_next_actions(
                payload, warned_blocks=warned_blocks, block_key=block_key
            )
    # Status-agnostic: applies regardless of raw_status (distinct from above).
    _coerce_pre_impl_zero_lines(
        payload, warned_blocks=warned_blocks, block_key=block_key
    )
    if raw_status == "shipped":
        _coerce_shipped_wait_for_ci(
            payload, warned_blocks=warned_blocks, block_key=block_key
        )


def parse_stdout(
    text: str,
    *,
    warned_blocks: set[str] | None = None,
) -> AutoDevResult | BlockedResult:
    """Parse a worker's stdout and return either the result or a synthetic blocker.

    Handles all six §6 failure modes by returning a :class:`BlockedResult`
    rather than raising. Callers can branch on ``isinstance(result,
    AutoDevResult)`` or check ``result.status``.

    ``warned_blocks`` is an optional caller-owned set (issue #1247) that dedups
    the ``_log.warning`` calls this parse triggers, keyed on a content hash of
    *text*. Pass the same set across repeated calls against an unchanging
    transcript (e.g. a poll loop re-scanning the same malformed sentinel) to
    suppress duplicate identical warnings. Left ``None`` (the default), every
    call logs independently — today's behavior, preserved for every caller
    that hasn't opted in.
    """
    block_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    located = _locate_raw_block(text, warned_blocks=warned_blocks, block_key=block_key)
    if isinstance(located, BlockedResult):
        return located

    decoded = _decode_payload(located, warned_blocks=warned_blocks, block_key=block_key)
    if isinstance(decoded, BlockedResult):
        return decoded

    payload = decoded
    raw_status = payload["status"]
    _normalize_payload(
        payload, raw_status, warned_blocks=warned_blocks, block_key=block_key
    )

    try:
        return AutoDevResult.model_validate(payload)
    except ValidationError as exc:
        _warn_once(
            "auto-dev sentinel failed model validation: %s",
            exc,
            warned_blocks=warned_blocks,
            block_key=block_key,
        )
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_VALIDATION_FAILED,
                details=f"{exc}",
            ),
        )
