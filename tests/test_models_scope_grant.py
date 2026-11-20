"""Model tests for ``ScopeGrant`` and ``TicketTask.scope_grants`` (#3772)."""

from __future__ import annotations

from cw.models.scope_grant import ScopeGrant
from cw.models.tasks.schema import DEV_QUEUE_SCHEMA_VERSION
from cw.models.tasks.ticket_task import TicketTask
from tests._shared.scope_grant_builders import (
    GRANT_RECORDED_AT,
    GRANT_ROW_CREATED,
    make_grant,
)


class TestScopeGrant:
    def test_optional_fields_default_to_unbound(self) -> None:
        grant = ScopeGrant(
            client="acme",
            ticket_id="T-1",
            row_created_at=GRANT_ROW_CREATED,
            head="1" * 40,
            policy="p",
            paths=["src/a.py"],
            recorded_at=GRANT_RECORDED_AT,
        )

        assert grant.repo is None
        assert grant.pr_number is None
        assert grant.plan_fingerprint is None
        assert grant.actor == ""

    def test_unknown_keys_are_ignored_not_forbidden(self) -> None:
        """Persisted runtime state stays lenient (#1200): a newer writer's key loads."""
        data = make_grant().model_dump(mode="json")
        data["written_by_a_newer_cw"] = True

        grant = ScopeGrant.model_validate(data)

        assert grant.paths == ["src/a.py", "src/b.py"]
        assert not hasattr(grant, "written_by_a_newer_cw")

    def test_json_round_trip_is_lossless(self) -> None:
        grant = make_grant(pr_number=7, plan_fingerprint="fp", actor="op")

        restored = ScopeGrant.model_validate_json(grant.model_dump_json())

        assert restored == grant


class TestTicketTaskLedger:
    def test_new_row_has_an_empty_ledger(self) -> None:
        assert TicketTask(ticket_id="T-1", client="acme").scope_grants == []

    def test_pre_v64_blob_loads_with_an_empty_ledger(self) -> None:
        blob = TicketTask(ticket_id="T-1", client="acme").model_dump(mode="json")
        del blob["scope_grants"]

        task = TicketTask.model_validate(blob)

        assert task.scope_grants == []

    def test_row_round_trips_its_ledger(self) -> None:
        task = TicketTask(ticket_id="T-1", client="acme", scope_grants=[make_grant()])

        restored = TicketTask.model_validate_json(task.model_dump_json())

        assert restored.scope_grants == task.scope_grants

    def test_scope_grants_sits_directly_after_the_transport_fields(self) -> None:
        names = list(TicketTask.model_fields)

        assert (
            names.index("scope_grants") == names.index("scope_drift_approved_head") + 1
        )

    def test_schema_version_is_64(self) -> None:
        assert DEV_QUEUE_SCHEMA_VERSION == 64
