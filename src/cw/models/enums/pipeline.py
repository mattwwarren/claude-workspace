"""Pipeline stage, position and stage-identifier enums (#3739)."""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    """RFC 0005 pipeline stage. Mutated live by dispatch via
    ``_stage_advance_unchecked`` as a task moves through the pipeline."""

    HARDEN = "harden"
    PLAN = "plan"
    IMPL = "impl"
    REVIEW = "review"
    FINALIZE = "finalize"


class PipelinePosition(StrEnum):
    """Where a row's stage sits in its resolved lane pipeline (#2760).

    ``INTERIOR`` is also the fail-soft value when the pipeline is unknown (no
    or unreadable client config): it is the pre-#2760 behaviour of the
    next-command table.

    ``TERMINAL_NO_REVIEW`` is the last stage of a pipeline that has no
    ``review`` stage (#2779), whatever that last stage is (``finalize``, or
    ``plan``/``impl`` on a short lane). Only a next-command rule that carries
    ``no_review_commands`` reacts to the difference; everything else treats
    both terminal members alike through :attr:`is_terminal`.
    """

    INTERIOR = "interior"
    TERMINAL = "terminal"
    TERMINAL_NO_REVIEW = "terminal_no_review"
    OFF_PIPELINE = "off_pipeline"

    @property
    def is_terminal(self) -> bool:
        """True for the last stage of the pipeline, with or without review."""
        return self in (PipelinePosition.TERMINAL, PipelinePosition.TERMINAL_NO_REVIEW)


def stage_position(stage: Stage, stages: list[Stage]) -> PipelinePosition:
    """*stage*'s position in *stages* (a resolved pipeline); empty-list safe.

    The last stage is ``TERMINAL`` when the pipeline has a ``review`` stage
    and ``TERMINAL_NO_REVIEW`` when it has none, including a lane whose last
    stage is ``plan`` or ``impl`` (#2779).
    """
    if stage not in stages:
        return PipelinePosition.OFF_PIPELINE
    if stage == stages[-1]:
        if Stage.REVIEW not in stages:
            return PipelinePosition.TERMINAL_NO_REVIEW
        return PipelinePosition.TERMINAL
    return PipelinePosition.INTERIOR


class StageIdentifier(StrEnum):
    """Closed headless-contract §10.2 stage vocabulary (GitHub #2429).

    The legal values of ``payload.stage`` and ``payload.prev_stage`` on
    ``stage.entered`` / ``stage.errored`` events. ``cw event record`` rejects
    anything else at record time, and ``cw orchestrate status`` renders an
    out-of-set ``stage`` as no ``last_stage`` at all.

    Distinct from ``Stage`` above (the RFC 0005 dispatch-lane pipeline stage):
    an unrelated concept with a different producer (the ``/auto-dev`` skill,
    not ``cw dispatch``).
    """

    S0_INTAKE = "s0_intake"
    S1_PLAN_GENERATED = "s1_plan_generated"
    S1_AMBIGUITY_SCAN_COMPLETE = "s1_ambiguity_scan_complete"
    S1_AMBIGUITY_SCAN_SKIPPED = "s1_ambiguity_scan_skipped"
    S1_PLAN_REVIEWED = "s1_plan_reviewed"
    S2_IMPL_STARTED = "s2_impl_started"
    S2_IMPL_COMPLETE = "s2_impl_complete"
    S3_REVIEW_STARTED = "s3_review_started"
    S3_REVIEW_COMPLETE = "s3_review_complete"
    S4_PR_CREATED = "s4_pr_created"
    S5_CI_WAITING = "s5_ci_waiting"
    DONE = "done"


# Membership set for the closed §10.2 vocabulary. ``payload.stage`` lives in
# the open ``OrchestratorEvent.payload`` dict, so there is no model field to
# type as ``StageIdentifier``; both the CLI producer gate and the orchestrate
# consumer filter test membership against this one constant.
STAGE_IDENTIFIERS: frozenset[str] = frozenset(s.value for s in StageIdentifier)
