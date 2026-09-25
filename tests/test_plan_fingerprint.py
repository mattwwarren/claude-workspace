"""Tests for cw.plan_fingerprint — the one in-cw implementation of the
*Plan-draft fingerprint rule* (#2102, #2382)."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import PlanDraftBindingError
from cw.plan_fingerprint import (
    PLAN_DRAFT_FINGERPRINT_KEY,
    bind_claimed_fingerprint,
    compute_plan_draft_fingerprint,
    is_plan_draft_fingerprint,
    sanitize_persisted_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path

_BODY = "# Plan\n\nreconciled draft body\n"
_BODY_SHA = hashlib.sha256(_BODY.encode("utf-8")).hexdigest()

_ROUND = "<!-- plan-stage-scan-round: 2 -->\n"
_LAST_EVALUATED = (
    "<!-- plan-stage-last-evaluated: operator_comment=comment:42|body_sha="
    + "a" * 64
    + " -->\n"
)
_SETTLED_A = "<!-- plan-stage-settled: A1: ADOPTED -->\n"
_SETTLED_P = "<!-- plan-stage-settled: P2: REFUTED -->\n"
_ATTEMPTED_STARTED = (
    "<!-- plan-stage-resolutions-attempted: source=comment:42; "
    "outcome=started; lease_until=2026-09-25T10:00:00Z -->\n"
)
_ATTEMPTED_DONE = (
    "<!-- plan-stage-resolutions-attempted: source=comment:42; outcome=succeeded -->\n"
)
_REVOKED = (
    "<!-- plan-stage-approval-revoked: at=2026-09-25T09:00:00Z; fingerprint="
    + "b" * 64
    + " -->\n"
)
_APPLIED = "<!-- plan-stage-resolutions-applied: source=comment:42 -->\n"


class TestIsPlanDraftFingerprint:
    def test_accepts_hashlib_hexdigest(self) -> None:
        assert is_plan_draft_fingerprint(hashlib.sha256(b"x").hexdigest())

    def test_rejects_truncated_uppercase_and_padded(self) -> None:
        # The #2382 incident shape: 62 of the 64 characters.
        assert not is_plan_draft_fingerprint("a" * 62)
        assert not is_plan_draft_fingerprint("A" * 64)
        assert not is_plan_draft_fingerprint("a" * 64 + "\n")
        assert not is_plan_draft_fingerprint("")


class TestComputePlanDraftFingerprint:
    def test_no_bookkeeping_hashes_whole_text(self) -> None:
        assert compute_plan_draft_fingerprint(_BODY) == _BODY_SHA

    def test_strips_the_full_leading_bookkeeping_block(self) -> None:
        """Every line kind the named rule lists is bookkeeping about the draft,
        not the draft: the digest equals the bare body's."""
        text = (
            _ROUND
            + _LAST_EVALUATED
            + _SETTLED_A
            + _SETTLED_P
            + _ATTEMPTED_DONE
            + _REVOKED
            + _APPLIED
            + _BODY
        )
        assert compute_plan_draft_fingerprint(text) == _BODY_SHA

    def test_started_attempt_lease_form_is_bookkeeping(self) -> None:
        assert compute_plan_draft_fingerprint(_ROUND + _ATTEMPTED_STARTED + _BODY) == (
            _BODY_SHA
        )

    def test_absent_later_lines_close_up_without_changing_order(self) -> None:
        assert compute_plan_draft_fingerprint(_ROUND + _APPLIED + _BODY) == _BODY_SHA
        assert compute_plan_draft_fingerprint(_ROUND + _REVOKED + _BODY) == _BODY_SHA

    def test_round_counter_increment_does_not_change_digest(self) -> None:
        one = compute_plan_draft_fingerprint(
            "<!-- plan-stage-scan-round: 1 -->\n" + _BODY
        )
        two = compute_plan_draft_fingerprint(
            "<!-- plan-stage-scan-round: 2 -->\n" + _BODY
        )
        assert one == two == _BODY_SHA

    def test_bookkeeping_is_a_leading_block_only(self) -> None:
        """Without the round-counter line nothing is stripped, and a marker
        inside the body is content."""
        assert compute_plan_draft_fingerprint(_SETTLED_A + _BODY) != _BODY_SHA
        leading_then_interior = _ROUND + "body\n" + _SETTLED_A
        leading_then_marker = _ROUND + _SETTLED_A + "body\n"
        assert compute_plan_draft_fingerprint(
            leading_then_interior
        ) != compute_plan_draft_fingerprint(leading_then_marker)

    def test_non_conforming_marker_line_is_content(self) -> None:
        """A line outside the closed grammar (trailing prose) is not
        bookkeeping and must change the digest."""
        malformed = "<!-- plan-stage-settled: A1: ADOPTED (operator note) -->\n"
        assert compute_plan_draft_fingerprint(_ROUND + malformed + _BODY) != _BODY_SHA


class TestBindClaimedFingerprint:
    def test_null_or_absent_claim_is_not_bound(self, tmp_path: Path) -> None:
        draft = tmp_path / "plan-draft.md"
        draft.write_text(_BODY, encoding="utf-8")
        for payload in ({PLAN_DRAFT_FINGERPRINT_KEY: None}, {}):
            assert bind_claimed_fingerprint(payload, draft) is None
            assert payload.get(PLAN_DRAFT_FINGERPRINT_KEY) is None

    def test_claim_is_replaced_by_the_computed_digest(self, tmp_path: Path) -> None:
        draft = tmp_path / "plan-draft.md"
        draft.write_text(_ROUND + _BODY, encoding="utf-8")
        payload: dict[str, object] = {PLAN_DRAFT_FINGERPRINT_KEY: _BODY_SHA[:62]}

        binding = bind_claimed_fingerprint(payload, draft)

        assert binding is not None
        assert binding.fingerprint == _BODY_SHA
        assert binding.draft_path == draft
        assert binding.replaced is True
        assert payload[PLAN_DRAFT_FINGERPRINT_KEY] == _BODY_SHA

    def test_matching_claim_reports_not_replaced(self, tmp_path: Path) -> None:
        draft = tmp_path / "plan-draft.md"
        draft.write_text(_BODY, encoding="utf-8")
        binding = bind_claimed_fingerprint(
            {PLAN_DRAFT_FINGERPRINT_KEY: _BODY_SHA}, draft
        )
        assert binding is not None
        assert binding.replaced is False

    def test_missing_draft_raises_and_leaves_payload_alone(
        self, tmp_path: Path
    ) -> None:
        payload: dict[str, object] = {PLAN_DRAFT_FINGERPRINT_KEY: "a" * 64}
        with pytest.raises(PlanDraftBindingError, match="none exists at"):
            bind_claimed_fingerprint(payload, tmp_path / "missing.md")
        assert payload[PLAN_DRAFT_FINGERPRINT_KEY] == "a" * 64

    def test_unreadable_draft_raises(self, tmp_path: Path) -> None:
        draft = tmp_path / "plan-draft.md"
        draft.write_bytes(b"\xff\xfe")
        with pytest.raises(PlanDraftBindingError, match="cannot read"):
            bind_claimed_fingerprint({PLAN_DRAFT_FINGERPRINT_KEY: "a" * 64}, draft)


class TestSanitizePersistedFingerprint:
    def test_malformed_value_is_nulled_on_a_copy(self) -> None:
        stored: dict[str, object] = {
            "status": "x",
            PLAN_DRAFT_FINGERPRINT_KEY: "a" * 62,
        }
        out = sanitize_persisted_fingerprint(stored)
        assert out is not stored
        assert out[PLAN_DRAFT_FINGERPRINT_KEY] is None
        assert out["status"] == "x"
        assert stored[PLAN_DRAFT_FINGERPRINT_KEY] == "a" * 62

    @pytest.mark.parametrize("value", ["a" * 64, None])
    def test_well_formed_or_null_returns_the_same_object(self, value: object) -> None:
        stored: dict[str, object] = {PLAN_DRAFT_FINGERPRINT_KEY: value}
        assert sanitize_persisted_fingerprint(stored) is stored

    def test_absent_key_returns_the_same_object(self) -> None:
        stored: dict[str, object] = {"status": "x"}
        assert sanitize_persisted_fingerprint(stored) is stored
