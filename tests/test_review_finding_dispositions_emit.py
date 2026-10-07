"""Tests for ``cw.review_finding_dispositions.emit`` (GitHub #2232, #2498).

The audit event type and payload every disposition emitter shares. Relocated
verbatim from the flat ``tests/test_review_finding_dispositions.py`` when the
module became a package (#2498), 1:1 with the ``emit`` submodule per the
CLAUDE.md Testing convention.
"""

from __future__ import annotations

import pytest

from cw.models.enums import OrchestratorEventType
from cw.review_finding_dispositions import (
    REVERSED,
    FindingDisposition,
    _disposition_key,
    disposition_event_payload,
    disposition_event_type,
)


class TestDispositionEventShape:
    """#2232 MUST_FIX 2/4: one type choice and one payload for every emitter.

    A settle recorded by ``cw review settle`` and one synced in from the
    ticket thread must be indistinguishable to a consumer, and neither site
    may carry a raw ``"REVERSED"`` literal.
    """

    def test_a_reversed_entry_gets_the_reverted_type(self) -> None:
        entry = FindingDisposition(outcome=REVERSED, summary="Bug here")

        assert (
            disposition_event_type(entry)
            == OrchestratorEventType.REVIEW_FINDING_DISPOSITION_REVERTED
        )

    @pytest.mark.parametrize("outcome", ["REJECTED", "ACCEPTED"])
    def test_every_other_outcome_gets_the_settled_type(self, outcome: str) -> None:
        entry = FindingDisposition.model_validate(
            {"outcome": outcome, "summary": "Bug here"}
        )

        assert (
            disposition_event_type(entry)
            == OrchestratorEventType.REVIEW_FINDING_SETTLED
        )

    def test_the_payload_carries_the_full_provenance_set(self) -> None:
        key = _disposition_key("src/cw/foo.py", "Bug here")
        assert key is not None
        entry = FindingDisposition(
            outcome="REJECTED",
            rationale="intentional, see ADR-0016",
            recorded_at="2026-09-01T00:00:00Z",
            actor="matt",
            reviewed_sha="abc1234",
            summary="Bug here",
        )

        payload = disposition_event_payload(key, entry)

        assert payload == {
            "key": key,
            "file": "src/cw/foo.py",
            "summary": "Bug here",
            "outcome": "REJECTED",
            "reason": "intentional, see ADR-0016",
            "actor": "matt",
            "recorded_at": "2026-09-01T00:00:00Z",
            "reviewed_sha": "abc1234",
        }
