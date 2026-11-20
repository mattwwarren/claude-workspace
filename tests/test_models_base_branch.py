"""``TicketTask.base_branch`` (v66, GitHub #3779, RFC 0020 D-2).

The field is the row's merge target. It is validated with the same charset as
``branch``, defaults to ``None`` (no migration filler), and is written only
through :func:`cw.models.tasks.base_branch.set_base_branch`, which refuses any
change once the row has spawned.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cw.dev_queue.migrate import migrate_dev_queue
from cw.exceptions import CwError
from cw.models import DEV_QUEUE_SCHEMA_VERSION, TicketTask
from cw.models.tasks.base_branch import BaseBranchFrozenError, set_base_branch
from tests._shared.session_seeding import _make_ticket_task


class TestTicketTaskField:
    def test_new_row_base_branch_is_none(self) -> None:
        assert _make_ticket_task().base_branch is None

    def test_previous_schema_row_loads_none(self) -> None:
        blob = _make_ticket_task(base_branch="epic/3778").model_dump(mode="json")
        del blob["base_branch"]
        blob["schema_version"] = DEV_QUEUE_SCHEMA_VERSION - 1
        assert TicketTask.model_validate(blob).base_branch is None

    def test_json_round_trip_of_a_set_value(self) -> None:
        task = _make_ticket_task(base_branch="epic/3778")
        reloaded = TicketTask.model_validate_json(task.model_dump_json())
        assert reloaded.base_branch == "epic/3778"

    def test_field_sits_directly_after_branch(self) -> None:
        names = list(TicketTask.model_fields)
        assert names.index("base_branch") == names.index("branch") + 1

    def test_schema_version_is_66(self) -> None:
        assert DEV_QUEUE_SCHEMA_VERSION == 66


class TestBaseBranchValidator:
    def test_null_passes(self) -> None:
        assert _make_ticket_task(base_branch=None).base_branch is None

    def test_slash_path_passes(self) -> None:
        assert _make_ticket_task(base_branch="epic/3778").base_branch == "epic/3778"

    @pytest.mark.parametrize("value", ["a#b", "a@b", "a+b", "epic/..x", ""])
    def test_rejects_forbidden_values(self, value: str) -> None:
        with pytest.raises(ValidationError, match="Invalid base_branch"):
            _make_ticket_task(base_branch=value)

    def test_branch_message_is_byte_identical(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _make_ticket_task(branch="a#b")
        assert (
            "Invalid branch 'a#b': must start with alphanumeric and contain only "
            "[a-zA-Z0-9/_.-], with no '..' sequence"
        ) in str(excinfo.value)


class TestMigration:
    def test_v65_raw_row_loads_null_base_at_v66(self) -> None:
        raw = {
            "schema_version": 65,
            "tasks": [{"ticket_id": "ACM-1", "client": "acme"}],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 66
        task = TicketTask.model_validate(migrated["tasks"][0])
        assert task.base_branch is None


class TestSetBaseBranch:
    def test_unspawned_null_to_set_is_allowed(self) -> None:
        task = _make_ticket_task(ever_spawned=False)
        set_base_branch(task, "epic/3778")
        assert task.base_branch == "epic/3778"

    def test_unspawned_change_to_other_value_is_allowed(self) -> None:
        task = _make_ticket_task(ever_spawned=False, base_branch="epic/1")
        set_base_branch(task, "epic/2")
        assert task.base_branch == "epic/2"

    def test_unspawned_set_to_null_is_allowed(self) -> None:
        task = _make_ticket_task(ever_spawned=False, base_branch="epic/1")
        set_base_branch(task, None)
        assert task.base_branch is None

    def test_spawned_null_to_set_is_refused_and_row_unchanged(self) -> None:
        task = _make_ticket_task(ever_spawned=True, base_branch=None)
        with pytest.raises(BaseBranchFrozenError):
            set_base_branch(task, "epic/3778")
        assert task.base_branch is None

    def test_spawned_change_to_other_value_is_refused(self) -> None:
        task = _make_ticket_task(ever_spawned=True, base_branch="epic/1")
        with pytest.raises(BaseBranchFrozenError):
            set_base_branch(task, "epic/2")
        assert task.base_branch == "epic/1"

    def test_spawned_set_to_null_is_refused(self) -> None:
        task = _make_ticket_task(ever_spawned=True, base_branch="epic/1")
        with pytest.raises(BaseBranchFrozenError):
            set_base_branch(task, None)
        assert task.base_branch == "epic/1"

    def test_spawned_same_value_is_an_idempotent_no_op(self) -> None:
        task = _make_ticket_task(ever_spawned=True, base_branch="epic/1")
        set_base_branch(task, "epic/1")
        assert task.base_branch == "epic/1"

    def test_default_constructed_row_is_frozen(self) -> None:
        """``ever_spawned`` defaults True, so a row with no explicit False is frozen."""
        task = _make_ticket_task()
        assert task.ever_spawned is True
        with pytest.raises(BaseBranchFrozenError):
            set_base_branch(task, "epic/3778")

    def test_bad_charset_raises_value_error_and_leaves_row(self) -> None:
        task = _make_ticket_task(ever_spawned=False, base_branch="epic/1")
        with pytest.raises(ValueError, match="Invalid base_branch"):
            set_base_branch(task, "bad#value")
        assert task.base_branch == "epic/1"

    def test_frozen_error_is_a_cw_error(self) -> None:
        assert issubclass(BaseBranchFrozenError, CwError)

    def test_frozen_message_names_ticket_and_field(self) -> None:
        task = _make_ticket_task(
            ticket_id="T-42", ever_spawned=True, base_branch="epic/1"
        )
        with pytest.raises(BaseBranchFrozenError) as excinfo:
            set_base_branch(task, "epic/2")
        message = str(excinfo.value)
        assert "T-42" in message
        assert "base_branch" in message
