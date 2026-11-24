"""Tests for ``remap_clean_sha``, the pure clean-sha move (#2921).

Pure ledger transition: no git, no clock. The strict tip rule is applied to the
result to prove the move is what makes ``evaluate_tip`` accept the new head,
and that the rule itself is unchanged for the un-remapped baseline.
"""

from __future__ import annotations

import json

import pytest

from cw.review_convergence import (
    ConvergenceLedger,
    evaluate_tip,
    remap_clean_sha,
)
from tests._review_convergence_helpers import (
    REMAP_NEW_SHA,
    REMAP_OLD_SHA,
    clean_sha_remap,
    confirmed_ledger,
)


def _converged() -> ConvergenceLedger:
    ledger = confirmed_ledger(sha=REMAP_OLD_SHA)
    assert ledger.last_clean_sha == REMAP_OLD_SHA
    return ledger


def test_happy_path_moves_only_the_clean_sha_and_audits_it() -> None:
    ledger = _converged()
    remap = clean_sha_remap()

    result = remap_clean_sha(ledger, remap)

    assert isinstance(result, ConvergenceLedger)
    assert result.last_clean_sha == REMAP_NEW_SHA
    assert result.clean_sha_remaps == [remap]
    assert result.rounds == ledger.rounds
    assert result.status == ledger.status
    assert result.checkpoint_3a_sha == ledger.checkpoint_3a_sha
    assert result.fork_point == ledger.fork_point
    assert ledger.last_clean_sha == REMAP_OLD_SHA
    assert ledger.clean_sha_remaps == []


def test_remapped_ledger_is_accepted_by_the_unchanged_strict_tip_rule() -> None:
    ledger = _converged()
    remapped = remap_clean_sha(ledger, clean_sha_remap())
    assert isinstance(remapped, ConvergenceLedger)

    accepted = evaluate_tip(
        remapped, head_sha=REMAP_NEW_SHA, origin_tip_sha=REMAP_NEW_SHA
    )
    baseline = evaluate_tip(
        ledger, head_sha=REMAP_NEW_SHA, origin_tip_sha=REMAP_NEW_SHA
    )

    assert accepted.decision == "accepted"
    assert baseline.decision == "rereview_required"
    assert baseline.reason == "commits_after_clean_round"


def test_no_rebuilt_changelog_commit_remaps_to_the_rebased_head() -> None:
    ledger = _converged()
    remap = clean_sha_remap(rebased_sha=REMAP_NEW_SHA)

    result = remap_clean_sha(ledger, remap)

    assert isinstance(result, ConvergenceLedger)
    assert result.last_clean_sha == REMAP_NEW_SHA


@pytest.mark.parametrize(
    ("update", "remap_fields", "reason_fragment"),
    [
        ({"status": "in_progress"}, {}, "not converged"),
        ({"status": "stopped"}, {}, "not converged"),
        ({"awaiting_confirm_sha": "pending"}, {}, "awaiting_confirm_sha"),
        ({"last_clean_sha": None}, {}, "no last_clean_sha"),
        ({"last_clean_sha": "tolerated-past"}, {}, "not the remap source"),
        ({}, {"to_sha": REMAP_OLD_SHA}, "same sha"),
    ],
)
def test_refusals_return_a_reason_and_leave_the_input_alone(
    update: dict[str, object],
    remap_fields: dict[str, object],
    reason_fragment: str,
) -> None:
    ledger = _converged().model_copy(update=update)
    before = ledger.model_dump()

    result = remap_clean_sha(ledger, clean_sha_remap(**remap_fields))

    assert isinstance(result, str)
    assert reason_fragment in result
    assert ledger.model_dump() == before


def test_ledger_without_the_audit_field_loads_with_an_empty_list() -> None:
    data = json.loads(_converged().model_dump_json())
    del data["clean_sha_remaps"]

    loaded = ConvergenceLedger.model_validate(data)

    assert loaded.clean_sha_remaps == []


def test_audit_entries_round_trip_through_json() -> None:
    remapped = remap_clean_sha(_converged(), clean_sha_remap())
    assert isinstance(remapped, ConvergenceLedger)

    reloaded = ConvergenceLedger.model_validate_json(remapped.model_dump_json())

    assert reloaded.clean_sha_remaps == [clean_sha_remap()]
