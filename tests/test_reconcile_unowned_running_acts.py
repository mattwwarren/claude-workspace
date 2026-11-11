"""Unit tests for cw.reconcile.unowned_running_acts and _text (#3328).

Part B of #2615: an unbound RUNNING row that cannot be adopted is either
parked or reverted (NO_OWNER: the claiming process is gone, the roster is
readable and homes no worker, and the claim context ties nothing), or paged
once as UNCONFIRMABLE and latched in ``advisory_note`` without any status
change. The text builders are tested here too (``TestTexts``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from cw.reconcile._shared import _UNOWNED_UNCONFIRMED_NOTE_PREFIX
from cw.reconcile.unowned_running_text import (
    ALL_CAUSES,
    CAUSE_NON_NATIVE,
    CONTEXT_ATTEMPT_MISMATCH,
    CONTEXT_MISSING,
    CONTEXT_NO_ATTEMPT,
    CONTEXT_OTHER_CLAIM,
    CONTEXT_SESSION_ABSENT,
    backend_names_text,
    claim_age_seconds,
    context_clause,
    no_owner_blocked_reason,
    no_owner_breadcrumbs,
    unconfirmed_breadcrumbs,
    unconfirmed_evidence,
    unconfirmed_note,
    unconfirmed_note_head,
)

_T_CLAIM = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)

# The ticket's evidence table, rendered with the facts below.
_FACTS: dict[str, str] = {
    "client": "client-a",
    "ticket": "GEN-1",
    "sid": "sess-1",
    "wt": "/w/GEN-1",
    "other_created": "2026-10-07T08:00:00+00:00",
    "s_client": "client-b",
    "s_ticket": "GEN-2",
    "started": "2026-10-07T08:30:00+00:00",
    "names": "aider, codex",
    "reason": "3 RUNNING row(s) in scope are bound",
}
_EVIDENCE_EXPECTED: dict[str, str] = {
    "duplicate": (
        "another RUNNING row exists for client-a/GEN-1 (created "
        "2026-10-07T08:00:00+00:00), so no session can be matched to this one"
    ),
    "bound_elsewhere": (
        "cw-context.json names session sess-1, which another row already owns"
    ),
    "conflicting_session": (
        "cw-context.json names session sess-1, but that session belongs to "
        "client-b/GEN-2, not client-a/GEN-1"
    ),
    "stale_session": (
        "cw-context.json names session sess-1, which started at "
        "2026-10-07T08:30:00+00:00 before this claim"
    ),
    "naive_datetime": (
        "a timestamp on session sess-1 or on the claim carries no timezone, so "
        "their order cannot be checked"
    ),
    "unreadable_context": (
        "/w/GEN-1/.claude/cw-context.json exists but cannot be read as JSON"
    ),
    "non_native": (
        "client client-a configures a non-claude-native executor (aider, "
        "codex), whose workers this check cannot see on the daemon roster"
    ),
    "roster_unproven": (
        "the daemon roster lists no workers while 3 RUNNING row(s) in scope "
        "are bound, so an absent worker is not evidence of a dead one"
    ),
}


class TestTexts:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (CONTEXT_MISSING, "cw-context.json is missing"),
            (CONTEXT_NO_ATTEMPT, "cw-context.json records no attempt"),
            (
                CONTEXT_ATTEMPT_MISMATCH,
                "cw-context.json records attempt 1, not this claim's attempt 2",
            ),
            (CONTEXT_OTHER_CLAIM, "cw-context.json names another ticket or client"),
            (
                CONTEXT_SESSION_ABSENT,
                "cw-context.json names session sess-1, which is not in sessions.json",
            ),
        ],
    )
    def test_context_clause_rows(self, state: str, expected: str) -> None:
        assert (
            context_clause(
                state, claim_attempt=2, context_attempt=1, session_id="sess-1"
            )
            == expected
        )

    def test_no_owner_breadcrumb_verbatim(self) -> None:
        text = no_owner_breadcrumbs(
            client="client-a",
            ticket="GEN-1",
            wt="/w/GEN-1",
            pid=4242,
            context="cw-context.json is missing",
        )

        assert text == (
            "No worker owns this RUNNING row: the process that claimed it (pid "
            "4242) is gone, the daemon roster is readable and lists no worker "
            "homed on /w/GEN-1, and cw-context.json is missing. The row was "
            "parked BLOCKED_ON_USER; nothing was stopped, reverted or deleted. "
            "Release it with `cw dev-queue requeue GEN-1 -c client-a` (it "
            "refuses while a recorded live session exists or the daemon roster "
            "is unreadable) or with `cw doctor --reap`."
        )

    def test_blocked_reason_verbatim(self) -> None:
        assert no_owner_blocked_reason(4242) == (
            "no worker owns this RUNNING row (claiming process 4242 is gone)"
        )

    @pytest.mark.parametrize("cause", sorted(_EVIDENCE_EXPECTED))
    def test_evidence_rows_verbatim(self, cause: str) -> None:
        assert unconfirmed_evidence(cause, **_FACTS) == _EVIDENCE_EXPECTED[cause]

    def test_every_cause_has_an_evidence_row(self) -> None:
        assert set(ALL_CAUSES) == set(_EVIDENCE_EXPECTED)

    @pytest.mark.parametrize(
        ("claimed_at", "claimed"),
        [
            (_T_CLAIM, "2026-10-07T09:00:00+00:00"),
            (None, "unknown (row predates schema v44)"),
        ],
        ids=["claimed", "pre_v44"],
    )
    def test_unconfirmed_breadcrumb_verbatim(
        self, claimed_at: datetime | None, claimed: str
    ) -> None:
        text = unconfirmed_breadcrumbs(
            client="client-a", ticket="GEN-1", claimed_at=claimed_at, evidence="E"
        )

        assert text == (
            f"Ownership of RUNNING row client-a/GEN-1 (claimed {claimed}) cannot "
            "be confirmed: E. cw changed nothing; the row stays RUNNING and "
            "keeps its lane slot. If you confirm no worker is running for it, "
            "release it with `cw dev-queue cancel GEN-1 -c client-a` and then "
            "`cw dev-queue requeue GEN-1 -c client-a --from-cancelled`; that "
            "requeue refuses while a recorded live session exists or the daemon "
            "roster is unreadable."
        )

    @pytest.mark.parametrize("cause", ALL_CAUSES)
    def test_unconfirmed_text_never_names_doctor_reap(self, cause: str) -> None:
        text = unconfirmed_breadcrumbs(
            client="client-a",
            ticket="GEN-1",
            claimed_at=_T_CLAIM,
            evidence=unconfirmed_evidence(cause, **_FACTS),
        )

        assert "cw doctor --reap" not in text

    def test_prefix_fits_the_reason_cell(self) -> None:
        assert len(_UNOWNED_UNCONFIRMED_NOTE_PREFIX) == 20

    def test_names_sorted_and_deduplicated(self) -> None:
        assert backend_names_text(["codex", "aider", "codex"]) == "aider, codex"

    @pytest.mark.parametrize("cause", ALL_CAUSES)
    def test_stored_note_carries_no_count(self, cause: str) -> None:
        note = unconfirmed_note(cause)

        assert note.startswith(unconfirmed_note_head(cause))
        assert note.startswith(f"?unowned_unconfirmed: {cause}: ")
        assert not re.search(r"\d", note)

    def test_non_native_note_head(self) -> None:
        assert unconfirmed_note_head(CAUSE_NON_NATIVE) == (
            "?unowned_unconfirmed: non_native: "
        )

    @pytest.mark.parametrize(
        ("claimed_at", "expected"),
        [
            (_T_CLAIM, 90.0),
            (None, None),
            (_T_CLAIM.replace(tzinfo=None), None),
        ],
        ids=["aware", "none", "naive"],
    )
    def test_claim_age_seconds(
        self, claimed_at: datetime | None, expected: float | None
    ) -> None:
        now = datetime(2026, 10, 7, 9, 1, 30, tzinfo=UTC)

        assert claim_age_seconds(claimed_at, now) == expected
