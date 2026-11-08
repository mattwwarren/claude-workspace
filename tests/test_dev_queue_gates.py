"""Unit tests for the finalize-park helpers in ``cw.dev_queue.gates`` (#2911, #2779)."""

from __future__ import annotations

from typing import get_args

import pytest

from cw.auto_dev_result.schema._vocab import StageReached, Status
from cw.dev_queue.gates import (
    APPROVAL_PARK_FRICTION_PREFIX,
    FINALIZE_PARK_STAGE_REACHED,
    REVIEW_PENDING_APPROVAL_STATUS,
    STALE_TIP_FRICTION_PREFIX,
    approval_park_token,
    is_finalize_park,
    stale_tip_reason,
)
from cw.dispatch.routing.stage_walk import _STAGE_REACHED_TO_STAGE
from cw.models import Stage

_APPROVAL_PARK_LINE = f"{APPROVAL_PARK_FRICTION_PREFIX} missing"


class TestVocabularyPins:
    """The gate constants are copies of vocabulary owned elsewhere (#2911).

    A rename in the owning module would silently desynchronise Rule 1 (a
    small-tier finalize park would COMPLETE with no PR again); these pins turn
    that drift into a failing test.
    """

    def test_finalize_park_stage_is_a_stage_reached_value(self) -> None:
        assert FINALIZE_PARK_STAGE_REACHED in get_args(StageReached)

    def test_review_pending_approval_is_a_sentinel_status(self) -> None:
        assert REVIEW_PENDING_APPROVAL_STATUS in get_args(Status)

    def test_stage_walk_maps_the_park_stage_to_finalize(self) -> None:
        assert _STAGE_REACHED_TO_STAGE[FINALIZE_PARK_STAGE_REACHED] is Stage.FINALIZE


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
                    f"{STALE_TIP_FRICTION_PREFIX} (tip_check_fetch_failed:"
                    " Permission denied (publickey))"
                ],
                "tip_check_fetch_failed",
                id="fetch-failed-2910",
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
        ("entry", "expected"),
        [
            pytest.param(f"  {STALE_TIP_FRICTION_PREFIX}  ", "", id="trimmed-bare"),
            pytest.param(
                f" {STALE_TIP_FRICTION_PREFIX} (no_clean_round) ",
                "no_clean_round",
                id="trimmed-reason",
            ),
            pytest.param(
                f"{STALE_TIP_FRICTION_PREFIX} (head_not_origin_tip: fetch (refused))",
                "head_not_origin_tip",
                id="detail-may-hold-parentheses",
            ),
            pytest.param(
                f"{STALE_TIP_FRICTION_PREFIX} "
                "(head_not_origin_tip: fetch failed\nremote hung up)",
                "head_not_origin_tip",
                id="detail-may-span-lines",
            ),
        ],
    )
    def test_accepts_the_three_token_shapes_after_trimming(
        self, entry: str, expected: str
    ) -> None:
        assert stale_tip_reason({"friction_highlights": [entry]}) == expected

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param(f"{STALE_TIP_FRICTION_PREFIX}ness", id="staleness"),
            pytest.param(f"{STALE_TIP_FRICTION_PREFIX} (x) trailing", id="trailing"),
            pytest.param(f"{STALE_TIP_FRICTION_PREFIX} ()", id="empty-reason"),
            pytest.param(f"{STALE_TIP_FRICTION_PREFIX} (No_Clean)", id="uppercase"),
            pytest.param(f"{STALE_TIP_FRICTION_PREFIX} (round2)", id="digit"),
            pytest.param(f"{STALE_TIP_FRICTION_PREFIX} (x", id="unclosed"),
            pytest.param(f"{STALE_TIP_FRICTION_PREFIX}: no_clean_round", id="colon"),
        ],
    )
    def test_lookalike_entry_is_not_the_stale_tip_token(self, entry: str) -> None:
        assert stale_tip_reason({"friction_highlights": [entry]}) is None

    def test_lookalike_does_not_mask_a_later_real_token(self) -> None:
        entries = [
            f"{STALE_TIP_FRICTION_PREFIX}ness",
            f"{STALE_TIP_FRICTION_PREFIX} (no_clean_round)",
        ]
        assert stale_tip_reason({"friction_highlights": entries}) == "no_clean_round"

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

    @pytest.mark.parametrize(
        "entries",
        [
            pytest.param(
                [
                    f"  {APPROVAL_PARK_FRICTION_PREFIX} missing",
                    f"{STALE_TIP_FRICTION_PREFIX} (no_clean_round)",
                ],
                id="padded-approval-park-before-stale",
            ),
            pytest.param(
                [
                    f"{STALE_TIP_FRICTION_PREFIX} (no_clean_round)",
                    f"\t{APPROVAL_PARK_FRICTION_PREFIX} missing\n",
                ],
                id="padded-approval-park-after-stale",
            ),
        ],
    )
    def test_whitespace_padded_approval_park_still_fails_closed(
        self, entries: list[str]
    ) -> None:
        assert stale_tip_reason({"friction_highlights": entries}) is None


class TestApprovalParkToken:
    """``approval_park_token`` reads Step 4.0's park token (#2779)."""

    @pytest.mark.parametrize("token", ["missing", "stale", "unreadable"])
    def test_reads_the_token(self, token: str) -> None:
        entry = (
            f"{APPROVAL_PARK_FRICTION_PREFIX} {token} — the large-tier review"
            " approval is not valid for this head"
        )
        assert approval_park_token({"friction_highlights": [entry]}) == token

    def test_reads_the_first_park_line_after_other_entries(self) -> None:
        entries = [
            f"{STALE_TIP_FRICTION_PREFIX} (no_clean_round)",
            f"  {APPROVAL_PARK_FRICTION_PREFIX} stale\n",
            _APPROVAL_PARK_LINE,
        ]
        assert approval_park_token({"friction_highlights": entries}) == "stale"

    @pytest.mark.parametrize(
        "friction",
        [
            pytest.param(["review_approval_parked: missing"], id="lookalike-prefix"),
            pytest.param([f"{APPROVAL_PARK_FRICTION_PREFIX} Missing"], id="no-token"),
            pytest.param([f"{APPROVAL_PARK_FRICTION_PREFIX}"], id="bare-prefix"),
            pytest.param([], id="no-friction"),
            pytest.param("review_approval_park: missing", id="not-a-list"),
            pytest.param([7, None], id="non-string-entries"),
        ],
    )
    def test_no_token_is_none(self, friction: object) -> None:
        assert approval_park_token({"friction_highlights": friction}) is None

    def test_absent_friction_is_none(self) -> None:
        assert approval_park_token({"status": REVIEW_PENDING_APPROVAL_STATUS}) is None
