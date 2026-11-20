"""Tests for ``MergePrerequisite`` and ``TicketTask.merge_prerequisites`` (#3791)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cw.models.merge_prerequisite import MergePrerequisite, mark_merged, unsatisfied
from cw.models.pr_ref import PrRef
from cw.models.tasks.schema import DEV_QUEUE_SCHEMA_VERSION
from cw.models.tasks.ticket_task import TicketTask

_DECLARED = datetime(2026, 11, 20, tzinfo=UTC)
_NOW = datetime(2026, 11, 21, tzinfo=UTC)


def _entry(
    number: int, *, repo: str | None = None, satisfied: bool = False
) -> MergePrerequisite:
    return MergePrerequisite(
        pr=PrRef(number=number, repo=repo),
        repo="foo/bar",
        declared_at=_DECLARED,
        satisfied_at=_DECLARED if satisfied else None,
    )


class TestMergePrerequisite:
    def test_defaults(self) -> None:
        entry = MergePrerequisite(
            pr=PrRef(number=9), repo="foo/bar", declared_at=_DECLARED
        )

        assert entry.satisfied_at is None
        assert entry.actor == ""

    def test_json_round_trip_dumps_pr_as_an_object(self) -> None:
        entry = _entry(9)

        dumped = entry.model_dump(mode="json")
        restored = MergePrerequisite.model_validate_json(entry.model_dump_json())

        assert dumped["pr"] == {"number": 9, "repo": None}
        assert restored == entry

    def test_unknown_keys_are_ignored_not_forbidden(self) -> None:
        data = _entry(9).model_dump(mode="json")
        data["written_by_a_newer_cw"] = True

        entry = MergePrerequisite.model_validate(data)

        assert entry.pr == PrRef(number=9)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.__setitem__("pr", {"number": 0, "repo": None}),
            lambda d: d.pop("repo"),
            lambda d: d.pop("declared_at"),
        ],
        ids=["zero-pr-number", "missing-repo", "missing-declared-at"],
    )
    def test_structurally_invalid_entry_fails_validation(self, mutate: object) -> None:
        data = _entry(9).model_dump(mode="json")
        assert callable(mutate)
        mutate(data)

        with pytest.raises(ValidationError):
            MergePrerequisite.model_validate(data)


class TestTicketTaskField:
    def test_new_row_has_no_prerequisites(self) -> None:
        assert TicketTask(ticket_id="T-1", client="acme").merge_prerequisites == []

    def test_previous_schema_row_loads_with_an_empty_list(self) -> None:
        blob = TicketTask(ticket_id="T-1", client="acme").model_dump(mode="json")
        del blob["merge_prerequisites"]
        blob["schema_version"] = DEV_QUEUE_SCHEMA_VERSION - 1

        task = TicketTask.model_validate(blob)

        assert task.merge_prerequisites == []

    def test_row_round_trips_its_prerequisites(self) -> None:
        task = TicketTask(
            ticket_id="T-1", client="acme", merge_prerequisites=[_entry(9)]
        )

        restored = TicketTask.model_validate_json(task.model_dump_json())

        assert restored.merge_prerequisites == task.merge_prerequisites

    def test_field_sits_directly_after_scope_grants(self) -> None:
        names = list(TicketTask.model_fields)

        assert names.index("merge_prerequisites") == names.index("scope_grants") + 1

    def test_schema_version_is_65(self) -> None:
        assert DEV_QUEUE_SCHEMA_VERSION == 65


class TestUnsatisfied:
    def test_keeps_declared_order_and_skips_satisfied(self) -> None:
        entries = [_entry(3), _entry(1, satisfied=True), _entry(2)]

        assert [e.pr.number for e in unsatisfied(entries)] == [3, 2]

    def test_ignores_a_cross_repo_entry(self) -> None:
        entries = [_entry(5, repo="other/repo"), _entry(6)]

        assert [e.pr.number for e in unsatisfied(entries)] == [6]

    def test_empty(self) -> None:
        assert unsatisfied([]) == []


class TestMarkMerged:
    def test_stamps_only_merged_same_repo_entries(self) -> None:
        entries = [_entry(1), _entry(2), _entry(3, repo="other/repo")]

        stamped = mark_merged(entries, {(None, 1), (None, 3), ("other/repo", 3)}, _NOW)

        assert stamped == 1
        assert entries[0].satisfied_at == _NOW
        assert entries[1].satisfied_at is None
        assert entries[2].satisfied_at is None

    def test_never_restamps_a_satisfied_entry(self) -> None:
        entries = [_entry(1, satisfied=True)]

        assert mark_merged(entries, {(None, 1)}, _NOW) == 0
        assert entries[0].satisfied_at == _DECLARED

    def test_no_keys_stamps_nothing(self) -> None:
        entries = [_entry(1)]

        assert mark_merged(entries, set(), _NOW) == 0
        assert entries[0].satisfied_at is None
