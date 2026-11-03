"""Unit tests for the finalize-park helpers in ``cw.dev_queue.gates`` (#2911)."""

from __future__ import annotations

import pytest

from cw.dev_queue.gates import (
    APPROVAL_PARK_FRICTION_PREFIX,
    FINALIZE_PARK_STAGE_REACHED,
    REVIEW_PENDING_APPROVAL_STATUS,
    STALE_TIP_FRICTION_PREFIX,
    is_finalize_park,
    stale_tip_reason,
)

_APPROVAL_PARK_LINE = f"{APPROVAL_PARK_FRICTION_PREFIX} missing"


class TestIsFinalizePark:
    @pytest.mark.parametrize(
        ("last_result", "expected"),
        [
            pytest.param(None, False, id="none"),
            pytest.param(
                {
                    "status": "shipped",
                    "stage_reached": FINALIZE_PARK_STAGE_REACHED,
                },
                False,
                id="wrong-status",
            ),
            pytest.param(
                {
                    "status": REVIEW_PENDING_APPROVAL_STATUS,
                    "stage_reached": "stage3_review",
                },
                False,
                id="wrong-stage-reached",
            ),
            pytest.param(
                {"status": REVIEW_PENDING_APPROVAL_STATUS},
                False,
                id="stage-reached-absent",
            ),
            pytest.param(
                {
                    "status": REVIEW_PENDING_APPROVAL_STATUS,
                    "stage_reached": FINALIZE_PARK_STAGE_REACHED,
                },
                True,
                id="both-right",
            ),
        ],
    )
    def test_matches_only_the_stamped_review_pending_sentinel(
        self, last_result: dict[str, object] | None, expected: bool
    ) -> None:
        assert is_finalize_park(last_result) is expected


class TestStaleTipReason:
    @pytest.mark.parametrize(
        "last_result",
        [
            pytest.param({}, id="friction-missing"),
            pytest.param({"friction_highlights": None}, id="friction-none"),
            pytest.param({"friction_highlights": "review_tip_check: stale"}, id="str"),
            pytest.param({"friction_highlights": {"a": 1}}, id="dict"),
            pytest.param({"friction_highlights": []}, id="empty-list"),
        ],
    )
    def test_no_usable_friction_list_is_none(
        self, last_result: dict[str, object]
    ) -> None:
        assert stale_tip_reason(last_result) is None

    @pytest.mark.parametrize(
        ("entries", "expected"),
        [
            pytest.param(
                [None, 7, f"{STALE_TIP_FRICTION_PREFIX} (no_clean_round)"],
                "no_clean_round",
                id="non-str-items-skipped",
            ),
            pytest.param([STALE_TIP_FRICTION_PREFIX], "", id="bare-token"),
            pytest.param(
                [f"{STALE_TIP_FRICTION_PREFIX} (commits_after_clean_round)"],
                "commits_after_clean_round",
                id="reason",
            ),
            pytest.param(
                [f"{STALE_TIP_FRICTION_PREFIX} (head_not_origin_tip: fetch refused)"],
                "head_not_origin_tip",
                id="reason-with-detail",
            ),
            pytest.param(
                [
                    "review_approval_check: not_required",
                    f"{STALE_TIP_FRICTION_PREFIX} (no_clean_round)",
                ],
                "no_clean_round",
                id="after-unrelated-entry",
            ),
        ],
    )
    def test_reads_the_reason_from_the_stale_tip_entry(
        self, entries: list[object], expected: str
    ) -> None:
        assert stale_tip_reason({"friction_highlights": entries}) == expected

    @pytest.mark.parametrize(
        "entries",
        [
            pytest.param([_APPROVAL_PARK_LINE], id="approval-park-alone"),
            pytest.param(
                [_APPROVAL_PARK_LINE, f"{STALE_TIP_FRICTION_PREFIX} (no_clean_round)"],
                id="approval-park-before-stale",
            ),
            pytest.param(
                [f"{STALE_TIP_FRICTION_PREFIX} (no_clean_round)", _APPROVAL_PARK_LINE],
                id="approval-park-after-stale",
            ),
            pytest.param(["review_approval_check: not_required"], id="no-match"),
        ],
    )
    def test_approval_park_or_no_match_fails_closed(self, entries: list[str]) -> None:
        assert stale_tip_reason({"friction_highlights": entries}) is None
