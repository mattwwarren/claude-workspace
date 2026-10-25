"""``build_queue_metadata``: the cw-context.json ``queue_metadata`` block (#2702).

Extracted from ``spawn.py``; the existing keys keep their exact values, and
``operator_resolutions`` is always a full list in round order (``[]`` when
empty, never absent or null).
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from cw.context_queue_metadata import (
    OPERATOR_RESOLUTIONS_SOURCE_KEY,
    REVIEW_APPROVAL_KEY,
    build_queue_metadata,
    operator_resolutions_source,
)
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
    assert set(metadata) == _PRE_EXISTING_KEYS | {
        OPERATOR_RESOLUTIONS_KEY,
        OPERATOR_RESOLUTIONS_SOURCE_KEY,
        REVIEW_APPROVAL_KEY,
    }


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
        OPERATOR_RESOLUTIONS_SOURCE_KEY: None,
        REVIEW_APPROVAL_KEY: None,
    }


def test_unset_task_threads_nulls() -> None:
    metadata = build_queue_metadata(_make_ticket_task())
    assert metadata["plan_approved_at"] is None
    assert metadata[MUST_FIX_OVERRIDE_KEY] is None
    assert metadata[REVIEW_APPROVAL_KEY] is None


def test_review_approval_is_an_iso_record_when_stamped() -> None:
    """#2767 (v14): the row's v48 record rides as one object for Step 4.0."""
    task = _make_ticket_task(
        review_approved_at=_APPROVED_AT,
        review_approver_kind="recipe",
        review_approved_sha="c" * 40,
    )
    assert build_queue_metadata(task)[REVIEW_APPROVAL_KEY] == {
        "approved_at": _APPROVED_AT.isoformat(),
        "approver_kind": "recipe",
        "reviewed_sha": "c" * 40,
    }


def test_review_approval_keeps_a_none_reviewed_sha() -> None:
    """A stamped kind with no head is threaded as-is (recorded absence)."""
    task = _make_ticket_task(
        review_approved_at=_APPROVED_AT, review_approver_kind="operator"
    )
    record = build_queue_metadata(task)[REVIEW_APPROVAL_KEY]
    assert record == {
        "approved_at": _APPROVED_AT.isoformat(),
        "approver_kind": "operator",
        "reviewed_sha": None,
    }


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


_IDENTITY_RE = re.compile(r"row:[0-9]+:[0-9a-f]{64}")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class TestOperatorResolutionsSource:
    """#2728: ``row:<N>:<sha256>`` -- cw computes the content-bound identity of
    the row's resolutions list and delivers it, so a re-enqueued row whose
    round 1 differs never equals a surviving draft's stale applied line."""

    def test_empty_list_has_no_identity(self) -> None:
        assert operator_resolutions_source([]) is None
        assert (
            build_queue_metadata(_make_ticket_task())[OPERATOR_RESOLUTIONS_SOURCE_KEY]
            is None
        )

    def test_one_entry_digest_covers_the_json_list_of_texts(self) -> None:
        assert operator_resolutions_source([_resolution(1, "first")]) == (
            "row:1:" + _digest(b'["first"]')
        )

    def test_two_entries_use_the_highest_round_and_digest_every_text(self) -> None:
        resolutions = [_resolution(1, "first"), _resolution(2, "second")]
        assert operator_resolutions_source(resolutions) == (
            "row:2:" + _digest(b'["first","second"]')
        )

    def test_threaded_into_queue_metadata(self) -> None:
        task = _make_ticket_task(
            operator_resolutions=[_resolution(1, "first"), _resolution(2, "second")]
        )
        assert build_queue_metadata(task)[OPERATOR_RESOLUTIONS_SOURCE_KEY] == (
            "row:2:" + _digest(b'["first","second"]')
        )

    def test_re_enqueued_row_with_different_round_one_text_differs(self) -> None:
        """The ticket's acceptance case: same ``N``, different content."""
        before = operator_resolutions_source([_resolution(1, "old text")])
        after = operator_resolutions_source([_resolution(1, "new text")])
        assert before is not None
        assert after is not None
        assert before.startswith("row:1:")
        assert after.startswith("row:1:")
        assert before != after

    def test_same_list_gives_the_same_identity(self) -> None:
        assert operator_resolutions_source(
            [_resolution(1, "a"), _resolution(2, "b")]
        ) == operator_resolutions_source([_resolution(1, "a"), _resolution(2, "b")])

    def test_same_latest_text_with_a_different_earlier_round_differs(self) -> None:
        """Latest-text-only hashing would collide on [A, B] vs [X, B]."""
        assert operator_resolutions_source(
            [_resolution(1, "A"), _resolution(2, "B")]
        ) != operator_resolutions_source([_resolution(1, "X"), _resolution(2, "B")])

    def test_entry_boundaries_are_unambiguous(self) -> None:
        assert operator_resolutions_source(
            [_resolution(1, "ab"), _resolution(2, "c")]
        ) != operator_resolutions_source([_resolution(1, "a"), _resolution(2, "bc")])

    def test_non_ascii_and_lone_surrogate_text_do_not_raise(self) -> None:
        for text in ("é✓", "\ud800"):
            identity = operator_resolutions_source([_resolution(1, text)])
            assert identity is not None
            assert _IDENTITY_RE.fullmatch(identity)

    def test_entries_out_of_round_order_are_normalized(self) -> None:
        assert operator_resolutions_source(
            [_resolution(2, "second"), _resolution(1, "first")]
        ) == ("row:2:" + _digest(b'["first","second"]'))
