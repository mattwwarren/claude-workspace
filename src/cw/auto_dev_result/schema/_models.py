"""Nested sub-models of :class:`~cw.auto_dev_result.schema.AutoDevResult`.

The seven pydantic models that appear as fields on the sentinel result:
:class:`Scope`, :class:`PrInfo`, :class:`PrCreated`, :class:`Review`,
:class:`AgentHealthEntry`, :class:`Health`, :class:`Blocker`. Each owns the
field-level invariants for its own payload fragment; the cross-field
invariants that span them live on ``AutoDevResult`` in
:mod:`cw.auto_dev_result.schema._result`.

Spec: ``docs/headless-contract.md`` (§3 framing, §5 health). Package split:
issue #2193.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from cw.auto_dev_result.schema._validators import (
    _is_blank,
    _reject_empty_string_items,
)
from cw.auto_dev_result.schema._vocab import ScopeTier, is_known_blocker_reason

_log = logging.getLogger(__name__)


class Scope(BaseModel):
    tier: ScopeTier | None = None
    files: int
    lines_estimate: int
    lines_actual: int | None = None
    forbidden_touched: bool


class PrInfo(BaseModel):
    number: int
    url: str
    auto_merge: bool
    base: str


class PrCreated(BaseModel):
    """Phase D — pre-merge PR snapshot captured at PR-creation time (issue #174).

    Distinct from :class:`PrInfo` (the ``pr`` field, representing the final
    shipped state). ``PrCreated`` is emitted *before* auto-merge is triggered so
    the orchestrator can attach CI watchers or make merge decisions based on the
    CI state at the moment the PR was opened, not after the merge completes.

    ``ci_status_at_creation`` is a free-form string (open-ish enum). Observed
    producer values: ``"pending"``, ``"passing"``, ``"failing"``. Consumers
    MUST treat unknown values as opaque strings and surface verbatim.
    """

    number: int
    url: str
    ci_status_at_creation: str
    auto_merge_enabled: bool


class Review(BaseModel):
    must_fix_initial: int
    should_fix: int
    fix_cycles_used: int
    deferred: int = 0
    # v5 (#1237): count of reviewer agents that ran, reconciled against the
    # executor-neutral review-verdict's `agents_run` list
    # (`len(verdict.agents_run)`, including failed entries). Advisory optional
    # field; defaults to 0 on payloads from producers that predate v5.
    agents_run: int = 0
    # #1723: true iff at least one fix cycle in the run produced a real
    # commit (OR'd across cycles) — distinguishes a genuinely-fixed cycle-0
    # blocker from a fix loop that converged (no MUST_FIX survivors) purely
    # because every cycle's codex fix invocation was a no-op. Advisory
    # optional field; defaults to None so payloads from producers that predate
    # this field remain explicitly unknown. Finalized fix-loop results always
    # populate a concrete bool.
    had_real_commit: bool | None = None
    # #2000: the same quantity as `ReviewVerdict.rejected_count` /
    # `rejected_count_by_severity` (`review_findings/_models.py`) — findings
    # that validation deleted before adjudication ever saw them, at every
    # severity — threaded here so an orchestrator reading only the terminal
    # AUTO_DEV_RESULT sentinel (not the underlying
    # `.claude/review-verdict.json` artifact) sees it too. Without it, a pass
    # that mechanically deleted findings reports `must_fix_initial: 0` and is
    # indistinguishable from one that genuinely found nothing. Additive and
    # purely advisory, no `schema_version` bump (docs/headless-contract.md §8,
    # Note A13).
    #
    # #2098: defaults to `None`, not `0`/`{}`. The #2000 producer half
    # (`.claude/commands/auto-dev-review.md`) never actually landed a Freeze
    # rule entry or a sentinel-template key for these two fields, so every
    # real producer omitted them and this default fired on every payload —
    # including the ones with real rejections. A defaulted `0` is
    # indistinguishable from a producer-confirmed "nothing was rejected", so
    # the omitted-vs-zero ambiguity this field exists to resolve for
    # `must_fix_initial` was silently reintroduced one level up. `None` means
    # "the producer did not report this field"; a producer that ran
    # `cw review consolidate` and got a real count must emit it, never
    # round it down to omission.
    rejected_count: int | None = None
    rejected_count_by_severity: dict[str, int] | None = None
    # #2123: the sha the review actually ran against — the same quantity as
    # `ReviewVerdict.reviewed_sha` (`review_findings/_models.py`), threaded
    # here so dispatch, which sees only the terminal AUTO_DEV_RESULT sentinel,
    # can compare it against the worktree's live HEAD before releasing the
    # REVIEW->FINALIZE checkpoint. Captured after any fix cycle converges and
    # its fix claims are verified (docs/headless-contract.md Note A14), so a
    # matching value means the reviewed tree IS the shippable tree.
    #
    # Defaults to `None` on the #2098 precedent: "the producer did not report
    # this", never "it matched". The consuming gate (disposition
    # `review_artifacts_stale`) fails CLOSED on that default, so a future
    # executor that forgets the stamp parks rather than silently bypassing the
    # gate.
    #
    # Additive for schema versioning — no `schema_version` bump, because an
    # older consumer that ignores the field behaves exactly as before. NOT
    # advisory for behavior: it is the load-bearing input to that fail-closed
    # gate, and omitting it parks the ticket. "Optional to emit" and
    # "inconsequential when absent" are different claims; only the first holds.
    reviewed_sha: str | None = None


class AgentHealthEntry(BaseModel):
    """Phase C — per-agent health snapshot for orchestrator retry targeting (#174).

    Collected across all agents that ran during a pipeline (plan, impl,
    reviewers, fix-loop cycles, prep-pr) so the orchestrator can identify
    *which* agent caused a downgrade rather than just knowing that a downgrade
    occurred.

    ``scope`` mirrors the tier the agent was operating on; may be ``None`` for
    agents that don't have a scope concept (e.g. plan-reviewer). Free-form
    string rather than a closed ``ScopeTier`` enum — tolerate producer-side
    values outside ``{"small", "large"}`` rather than failing validation.
    """

    agent_id: str
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    scope: str | None = None

    # Why: mirrors the #953/#962 empty-item guard shape for a scalar field
    # (issue #1130). A blank agent_id defeats the orchestrator retry-targeting
    # use case this field exists for (§5.3).
    @field_validator("agent_id")
    @classmethod
    def _reject_blank_agent_id(cls, v: str) -> str:
        if _is_blank(v):
            msg = f"agent_id must be a non-empty, non-whitespace string (got {v!r})"
            raise ValueError(msg)
        return v


class Health(BaseModel):
    lowest_agent_confidence: Literal["HIGH", "MEDIUM", "LOW"] | None = None
    any_incomplete_risk: bool
    shortcuts: list[str] = Field(default_factory=list)
    recommendation: Literal["PROCEED", "EXIT_FOR_HUMAN_REVIEW"]
    downgrade_applied: bool = False
    fix_loop_escalated: bool = False
    # Phase C — per-agent breakdown so the orchestrator can target retries at
    # the specific agent that caused a downgrade (issue #174). Optional: absent
    # on payloads from older producers; defaults to empty list.
    agent_health_summary: list[AgentHealthEntry] = Field(default_factory=list)

    # Why: sibling of AutoDevResult's commits/friction_highlights/next_actions
    # guard (issue #1130) — shortcuts lives on Health, not AutoDevResult, so it
    # needs its own field_validator rather than joining the multi-field one.
    @field_validator("shortcuts")
    @classmethod
    def _reject_empty_shortcuts(cls, v: list[str]) -> list[str]:
        return _reject_empty_string_items(v, "shortcuts")


class Blocker(BaseModel):
    """Either an emitted blocker (``status=blocked``) or a synthetic one.

    ``reason`` is intentionally typed as ``str`` (open enum per §4.2). The
    producer may add new reasons without a schema bump; consumers surface
    unknown reasons verbatim. Since #2097 that openness is *advisory-checked*
    rather than unchecked: a reason outside :data:`KNOWN_BLOCKER_REASONS`
    still parses and is still surfaced verbatim, but logs
    ``blocker_reason_unknown`` at WARNING and is flagged ``(unrecognized)`` in
    the operator's views — so an invented reason that reads like a documented
    routing code cannot pass as one. A producer that genuinely wants a
    freeform reason declares it with the :data:`FREEFORM_BLOCKER_REASON_PREFIX`
    (``x_``) namespace, which suppresses the warning.

    Phase B and Phase E of the queue-orchestrator observability expansion
    (issue #174) added five optional fields. All default to None so v1/v2
    blocks without them parse unchanged; producers emitting v3 should
    populate them per the headless-contract spec.
    """

    stage: str
    reason: str
    details: str = ""
    # Phase B — blocker context for orchestrator routing.
    exception_type: str | None = None
    message: str | None = None
    recovery_hint: str | None = None
    # Phase E — queue-aware retry semantics. ``retry_eligible=True`` paired
    # with a non-null ``retry_delay_seconds`` means the orchestrator can
    # safely re-dispatch after the given backoff. ``retry_eligible=False``
    # means human intervention is required.
    retry_eligible: bool | None = None
    retry_delay_seconds: int | None = None

    # Why: #2097 -- warn-only, never reject. The open-enum contract (§4.2)
    # forbids failing a parse on an unrecognized reason, so this returns the
    # value untouched; the WARNING (and the routing/CLI `(unrecognized)`
    # flags fed by is_known_blocker_reason) is the entire remedy.
    @field_validator("reason")
    @classmethod
    def _warn_unknown_reason(cls, v: str) -> str:
        if not is_known_blocker_reason(v):
            _log.warning("blocker_reason_unknown reason=%s", v)
        return v

    @model_validator(mode="after")
    def _check_retry_invariants(self) -> Blocker:
        # If retry_delay_seconds is set, retry_eligible must not be False.
        # retry_eligible=None means the field was omitted by an older producer
        # (issue #430 case 5) — treat as implied True when a delay is present.
        # retry_eligible=False with a delay is still a hard error.
        if self.retry_delay_seconds is not None and self.retry_eligible is False:
            msg = (
                "retry_delay_seconds set without retry_eligible=True "
                f"(got retry_eligible={self.retry_eligible!r})"
            )
            raise ValueError(msg)
        if self.retry_delay_seconds is not None and self.retry_eligible is None:
            # Older producer omitted retry_eligible; coerce to True so the
            # invariant is satisfied and the sentinel is not discarded.
            self.retry_eligible = True
        if self.retry_delay_seconds is not None and self.retry_delay_seconds < 0:
            msg = (
                f"retry_delay_seconds must be non-negative, "
                f"got {self.retry_delay_seconds}"
            )
            raise ValueError(msg)
        return self
