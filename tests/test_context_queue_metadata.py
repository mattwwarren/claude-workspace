"""``build_queue_metadata``: the cw-context.json ``queue_metadata`` block (#2702).

Extracted from ``spawn.py``; the existing keys keep their exact values, and
``operator_resolutions`` is always a full list in round order (``[]`` when
empty, never absent or null).
"""

from __future__ import annotations

from datetime import UTC, datetime

from cw.context_queue_metadata import build_queue_metadata
from cw.models import (
    MUST_FIX_OVERRIDE_KEY,
    PLAN_APPROVED_FINGERPRINT_KEY,
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
    MustFixOverride,
    Stage,
)
from cw.models.operator_resolution import OPERATOR_RESOLUTIONS_KEY, OperatorResolution
from tests.conftest import _make_ticket_task

_APPROVED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_RECORDED_AT = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)
_PRE_EXISTING_KEYS = {
    "scope_hint",
    "plan_source",
    "headless_timeout_override",
    "regressed_into_stage",
    "pending_operator_comment",
    "plan_approved_at",
    PLAN_APPROVED_FINGERPRINT_KEY,
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
    MUST_FIX_OVERRIDE_KEY,
}


def _resolution(round_: int, text: str) -> OperatorResolution:
    return OperatorResolution(
        text=text, recorded_at=_RECORDED_AT, actor="octo", round=round_
    )


def test_key_set_is_the_old_one_plus_operator_resolutions() -> None:
    metadata = build_queue_metadata(_make_ticket_task())
    assert set(metadata) == _PRE_EXISTING_KEYS | {OPERATOR_RESOLUTIONS_KEY}


def test_populated_task_values_match_the_old_inline_dict() -> None:
    override = MustFixOverride(
        actor="octocat",
        reason="ship it",
        reviewed_sha="0123abcd" * 5,
        finding_ids=[("src/a.py", "bug here")],
        recorded_at=_RECORDED_AT,
    )
    task = _make_ticket_task(
        scope_hint="large",
        plan_source="tracker",
        headless_timeout_override=900,
        regressed_into_stage=Stage.IMPL,
        pending_operator_comment=True,
        plan_approved_at=_APPROVED_AT,
        plan_approved_fingerprint="a" * 64,
        scope_drift_approved_extra_files=["docs/x.md"],
        scope_drift_approved_head="f" * 40,
        must_fix_override=override,
    )
    metadata = build_queue_metadata(task)
    assert metadata == {
        "scope_hint": "large",
        "plan_source": "tracker",
        "headless_timeout_override": 900,
        "regressed_into_stage": Stage.IMPL,
        "pending_operator_comment": True,
        "plan_approved_at": _APPROVED_AT.isoformat(),
        PLAN_APPROVED_FINGERPRINT_KEY: "a" * 64,
        SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY: ["docs/x.md"],
        SCOPE_DRIFT_APPROVED_HEAD_KEY: "f" * 40,
        MUST_FIX_OVERRIDE_KEY: override.model_dump(mode="json"),
        OPERATOR_RESOLUTIONS_KEY: [],
    }


def test_unset_task_threads_nulls() -> None:
    metadata = build_queue_metadata(_make_ticket_task())
    assert metadata["plan_approved_at"] is None
    assert metadata[MUST_FIX_OVERRIDE_KEY] is None


def test_resolutions_are_a_list_of_dicts_in_round_order() -> None:
    task = _make_ticket_task(
        operator_resolutions=[_resolution(1, "first"), _resolution(2, "second")]
    )
    assert build_queue_metadata(task)[OPERATOR_RESOLUTIONS_KEY] == [
        {
            "text": "first",
            "recorded_at": "2026-10-02T09:30:00Z",
            "actor": "octo",
            "round": 1,
        },
        {
            "text": "second",
            "recorded_at": "2026-10-02T09:30:00Z",
            "actor": "octo",
            "round": 2,
        },
    ]


def test_empty_resolutions_are_an_empty_list() -> None:
    metadata = build_queue_metadata(_make_ticket_task())
    assert metadata[OPERATOR_RESOLUTIONS_KEY] == []
