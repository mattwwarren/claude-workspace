"""Tests for cw.reconcile.review_recipes (RFC 0010 P1, GitHub #1096).

P1 is detect-only: the module classifies dev-queue rows whose PR came back
``changes_requested`` into ``ReviewRecipeCandidate``s but performs no act /
dispatch / event emission (deferred to P2). These tests exercise the detect
predicate, the ``_is_candidate`` gating borrowed from ``cw.pr_hydrate``, and
the dual master-switch gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    ClientConfig,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    TicketTask,
)
from cw.reconcile.review_recipes import (
    RECIPE_ADDRESS_REVIEW,
    _detect_address_review,
    resolve_review_recipe_enabled,
    run_review_recipes,
)

# Reuse the sibling test helpers rather than re-deriving TicketTask / PrState
# construction: _make_task accepts **kwargs (pr_url / pr_state / session_id /
# client / lane), _pr_state builds a PrState with sensible OPEN defaults.
# _client_with_lanes builds a ClientConfig with the given lanes (reused by the
# resolve-precedence tests below).
from tests.test_pr_hydrate import _pr_state
from tests.test_reconcile_gate_recipes import _client_with_lanes, _make_task

_SKILL_PATH = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "skills"
    / "address-review"
    / "SKILL.md"
)


def _config(**kwargs: Any) -> OrchestratorConfig:
    """OrchestratorConfig with the review-recipe master switch defaulted ON."""
    kwargs.setdefault("review_recipes_enabled", True)
    return OrchestratorConfig(**kwargs)


def _cr_task(**kwargs: Any) -> Any:
    """A changes_requested candidate task: pr_url + non-terminal PR state."""
    kwargs.setdefault("pr_url", "https://github.com/acme/widgets/pull/42")
    kwargs.setdefault(
        "pr_state", _pr_state(state="OPEN", attention_state="changes_requested")
    )
    return _make_task(**kwargs)


def _enabling_clients() -> dict[str, ClientConfig]:
    """Clients dict opting the default lane into the address-review recipe.

    The direct ``_detect_address_review`` call sites need per-lane enablement
    (RFC 0010 P3): with the default-off floor, a candidate only surfaces when a
    lane (or ticket) opts the recipe in. ``_make_task`` defaults to client
    ``acme`` on lane ``default``, so this dict resolves those tasks enabled.
    """
    return {
        "acme": _client_with_lanes(
            LaneConfig(name="default", review_recipes={RECIPE_ADDRESS_REVIEW: True})
        )
    }


@pytest.mark.parametrize(
    "attention_state",
    ["ci_failing", "merge_blocked", "no_reviewer", "ready_to_approve", None],
)
def test_detect_address_review_only_changes_requested_negative(
    attention_state: str | None,
) -> None:
    task = _make_task(
        pr_url="https://github.com/acme/widgets/pull/42",
        pr_state=_pr_state(state="OPEN", attention_state=attention_state),
    )
    assert (
        _detect_address_review([task], clients=_enabling_clients(), config=_config())
        == []
    )


def test_detect_address_review_only_changes_requested_positive() -> None:
    task = _cr_task()
    candidates = _detect_address_review(
        [task], clients=_enabling_clients(), config=_config()
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.recipe == RECIPE_ADDRESS_REVIEW
    assert candidate.attention_state == "changes_requested"
    assert candidate.pr_url == "https://github.com/acme/widgets/pull/42"
    assert candidate.ticket_id == task.ticket_id
    assert candidate.client == task.client
    assert candidate.lane == task.lane


def test_detect_address_review_requires_is_candidate() -> None:
    # No pr_url -> _is_candidate False -> not a candidate even if the state
    # would otherwise qualify.
    no_url = _make_task(
        pr_url=None,
        pr_state=_pr_state(state="OPEN", attention_state="changes_requested"),
    )
    assert (
        _detect_address_review([no_url], clients=_enabling_clients(), config=_config())
        == []
    )
    # Terminal PR state -> _is_candidate False regardless of attention_state.
    merged = _make_task(
        pr_url="https://github.com/acme/widgets/pull/42",
        pr_state=_pr_state(state="MERGED", attention_state="changes_requested"),
    )
    assert (
        _detect_address_review([merged], clients=_enabling_clients(), config=_config())
        == []
    )


def test_detect_address_review_pr_state_none_guard() -> None:
    # pr_url set but pr_state None: _is_candidate is True (hydratable), but the
    # detect phase requires an actual pr_state to read attention_state from.
    task = _make_task(pr_url="https://github.com/acme/widgets/pull/42", pr_state=None)
    assert (
        _detect_address_review([task], clients=_enabling_clients(), config=_config())
        == []
    )


def test_run_review_recipes_master_switch_off_is_noop() -> None:
    config = OrchestratorConfig()  # review_recipes_enabled defaults False
    assert config.review_recipes_enabled is False
    assert run_review_recipes(config=config) == []
    # Dual gating: _detect_address_review gates on the switch itself too, so a
    # direct call with the switch off returns [] even given a live candidate.
    assert (
        _detect_address_review([_cr_task()], clients=_enabling_clients(), config=config)
        == []
    )


def test_run_review_recipes_loads_from_dev_queue() -> None:
    # Exercises the actual wiring core.py calls: run_review_recipes's own
    # load_dev_queue() read, not just the pure _detect_address_review helper.
    # Ticket-level override opts the recipe in (highest tier) so the candidate
    # surfaces without an on-disk client config in the test env.
    task = _cr_task(review_recipes={RECIPE_ADDRESS_REVIEW: True})
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = run_review_recipes(config=_config())

    assert len(candidates) == 1
    assert candidates[0].ticket_id == task.ticket_id
    assert candidates[0].attention_state == "changes_requested"
    # P1 is detect-only: the dev-queue snapshot on disk is untouched.
    assert load_dev_queue().tasks == [task]


def test_draft_pr_never_a_candidate() -> None:
    # Draft PRs derive attention_state None (Row 0), which never qualifies.
    task = _make_task(
        pr_url="https://github.com/acme/widgets/pull/42",
        pr_state=_pr_state(state="OPEN", attention_state=None),
    )
    assert (
        _detect_address_review([task], clients=_enabling_clients(), config=_config())
        == []
    )


def test_detect_address_review_surfaces_sessionless_candidate() -> None:
    task = _cr_task(session_id=None)
    candidates = _detect_address_review(
        [task], clients=_enabling_clients(), config=_config()
    )
    assert len(candidates) == 1
    assert candidates[0].session_id is None


def test_address_review_skill_file_exists() -> None:
    assert _SKILL_PATH.is_file()
    assert _SKILL_PATH.read_text(encoding="utf-8").strip() != ""


class TestResolveReviewRecipeEnabled:
    """3-tier precedence for resolve_review_recipe_enabled (RFC 0010 P3)."""

    def test_resolve_review_recipe_enabled_ticket_overrides_lane(self) -> None:
        """A ticket-level override beats an enabling lane map (True) and a
        disabling lane map (False), in both directions."""
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(name="default", review_recipes={RECIPE_ADDRESS_REVIEW: True})
            )
        }
        task_off = _make_task(review_recipes={RECIPE_ADDRESS_REVIEW: False})
        assert (
            resolve_review_recipe_enabled(task_off, clients, RECIPE_ADDRESS_REVIEW)
            is False
        )
        clients_off = {
            "acme": _client_with_lanes(
                LaneConfig(
                    name="default", review_recipes={RECIPE_ADDRESS_REVIEW: False}
                )
            )
        }
        task_on = _make_task(review_recipes={RECIPE_ADDRESS_REVIEW: True})
        assert (
            resolve_review_recipe_enabled(task_on, clients_off, RECIPE_ADDRESS_REVIEW)
            is True
        )

    def test_resolve_review_recipe_enabled_lane_overrides_default(self) -> None:
        """With no ticket override, the lane map wins over the default floor."""
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(name="default", review_recipes={RECIPE_ADDRESS_REVIEW: True})
            )
        }
        task = _make_task()  # no ticket-level override
        assert (
            resolve_review_recipe_enabled(task, clients, RECIPE_ADDRESS_REVIEW) is True
        )

    def test_resolve_review_recipe_enabled_default_off(self) -> None:
        """No override anywhere falls through to the hardcoded default-off floor."""
        task = _make_task()
        # No client at all.
        assert resolve_review_recipe_enabled(task, {}, RECIPE_ADDRESS_REVIEW) is False
        # Lane present but carries no review_recipes map -> default floor.
        clients = {"acme": _client_with_lanes(LaneConfig(name="default"))}
        assert (
            resolve_review_recipe_enabled(task, clients, RECIPE_ADDRESS_REVIEW) is False
        )

    def test_resolve_review_recipe_enabled_missing_client_or_lane_no_raise(
        self,
    ) -> None:
        """Missing client, missing lane, and an unrecognized recipe_name all
        fall through to the default with no exception."""
        # Client absent from the map.
        ghost = _make_task(client="ghost")
        assert resolve_review_recipe_enabled(ghost, {}, RECIPE_ADDRESS_REVIEW) is False
        # Client present but the task's lane is not among its lanes.
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(name="other", review_recipes={RECIPE_ADDRESS_REVIEW: True})
            )
        }
        assert (
            resolve_review_recipe_enabled(_make_task(), clients, RECIPE_ADDRESS_REVIEW)
            is False
        )
        # Unrecognized recipe_name -> .get fallback False, not KeyError.
        assert (
            resolve_review_recipe_enabled(_make_task(), {}, "nonexistent_recipe")
            is False
        )

    def test_unrecognized_review_recipe_key_rejected(self) -> None:
        """Both TicketTask and LaneConfig fail loud on an unrecognized key."""
        with pytest.raises(ValidationError):
            TicketTask(ticket_id="X", client="acme", review_recipes={"bogus": True})
        with pytest.raises(ValidationError):
            LaneConfig(name="default", review_recipes={"bogus": True})


def test_config_reference_documents_review_recipes() -> None:
    """CONFIG_REFERENCE.md documents the review_recipes field + prose section."""
    doc = (
        Path(__file__).resolve().parent.parent / "config" / "CONFIG_REFERENCE.md"
    ).read_text(encoding="utf-8")
    assert "review_recipes" in doc
    assert "Review Recipe Enablement" in doc
