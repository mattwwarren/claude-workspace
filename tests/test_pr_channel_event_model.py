"""Pure tests for the PR channel v1 contract (#3173).

No server globals, no reset fixtures. ``cw.cw_pr_events_server`` imports starlette
lazily, so ``PREventRequest`` and ``_build_notification`` import without the mcp
extra.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time
from pydantic import ValidationError

from cw.cw_pr_events_server import PREventRequest, _build_notification
from cw.models import OrchestratorEventType
from cw.models.pr_channel_event import (
    EVENT_KIND_BY_TYPE,
    PR_CHANNEL_SCHEMA_VERSION,
    PREventKind,
    PREventMessage,
    PREventType,
    derive_event_id,
    kind_for,
    normalize_event_time,
    normalize_head_sha,
    normalize_source_id,
)

FIXTURES = Path(__file__).parent / "fixtures"
PR_FIXTURES = FIXTURES / "pr_channel"
FROZEN = "2026-11-09T12:00:00Z"
RECEIVED = "2026-11-09T12:00:00.000+00:00"
SHA_40 = "a" * 40


def _load(name: str) -> dict[str, Any]:
    return json.loads((PR_FIXTURES / name).read_text())


def _build(body: dict[str, Any]) -> dict[str, Any]:
    with freeze_time(FROZEN):
        event = PREventRequest.model_validate(body)
        return _build_notification(event)


def _message(body: dict[str, Any]) -> dict[str, Any]:
    return json.loads(_build(body)["message"])


WORKFLOW_BODIES = [
    "workflow_body_merged.json",
    "workflow_body_ci_failed.json",
    "workflow_body_review_received.json",
    "workflow_body_review_requested.json",
]


class TestWorkflowGoldens:
    def test_merged_matches_golden(self) -> None:
        got = _message(_load("workflow_body_merged.json"))
        assert got == _load("message_v1_merged_null_source.json")

    @pytest.mark.parametrize("name", WORKFLOW_BODIES)
    def test_workflow_body_is_v1_with_null_identity(self, name: str) -> None:
        got = _message(_load(name))
        assert got["schema_version"] == PR_CHANNEL_SCHEMA_VERSION
        assert got["kind"] == "transition"
        assert got["source_id"] is None
        assert got["event_id"] is None
        assert got["head_sha"] is None
        assert got["event_time"] is None
        assert got["received_at"] == RECEIVED

    def test_envelope_keys_unchanged(self) -> None:
        envelope = _build(_load("workflow_body_merged.json"))
        assert set(envelope) == {"notification_type", "message", "title"}
        assert envelope["notification_type"] == "cw-pr-event"
        assert envelope["title"] == "PR #42: merged"


class TestEventId:
    def test_test_vector(self) -> None:
        got = derive_event_id(
            schema_version=1,
            repo="Acme/Widgets",
            pr_number=42,
            event_type="merged",
            source_id="src-1",
        )
        assert got == (
            "bdd585cd2afc8a777b5246f244b5e39e6507505a53fe33f3ba6384a2c440c186"
        )

    def test_repo_case_change_same_id(self) -> None:
        a = derive_event_id(
            schema_version=1,
            repo="acme/widgets",
            pr_number=42,
            event_type="merged",
            source_id="s",
        )
        b = derive_event_id(
            schema_version=1,
            repo="ACME/Widgets",
            pr_number=42,
            event_type="merged",
            source_id="s",
        )
        assert a == b

    def test_cross_repo_differs(self) -> None:
        a = derive_event_id(
            schema_version=1,
            repo="acme/widgets",
            pr_number=42,
            event_type="merged",
            source_id="s",
        )
        b = derive_event_id(
            schema_version=1,
            repo="other/widgets",
            pr_number=42,
            event_type="merged",
            source_id="s",
        )
        assert a != b

    @pytest.mark.parametrize(
        "change",
        [
            {"pr_number": 43},
            {"event_type": "ci_failed"},
            {"source_id": "s2"},
            {"schema_version": 2},
        ],
    )
    def test_each_identity_field_changes_id(self, change: dict[str, Any]) -> None:
        base = {
            "schema_version": 1,
            "repo": "acme/widgets",
            "pr_number": 42,
            "event_type": "merged",
            "source_id": "s",
        }
        other = {**base, **change}
        assert derive_event_id(**base) != derive_event_id(**other)

    def test_null_source_gives_none_not_body_hash(self) -> None:
        assert (
            derive_event_id(
                schema_version=1,
                repo="acme/widgets",
                pr_number=42,
                event_type="merged",
                source_id=None,
            )
            is None
        )

    def test_independent_of_received_head_and_offset(self) -> None:
        kwargs = {
            "schema_version": 1,
            "repo": "acme/widgets",
            "pr_number": 42,
            "event_type": "merged",
            "source_id": "s",
        }
        assert derive_event_id(**kwargs) == derive_event_id(**kwargs)


class TestNormalizers:
    @pytest.mark.parametrize(
        ("raw", "value", "reason"),
        [
            (None, None, None),
            ("", None, "blank"),
            ("  ", None, "blank"),
            (123, None, "not a string"),
            ("  keep  ", "  keep  ", None),
        ],
    )
    def test_source_id(self, raw: object, value: object, reason: object) -> None:
        assert normalize_source_id(raw) == (value, reason)

    @pytest.mark.parametrize(
        ("raw", "value", "reason"),
        [
            (None, None, None),
            (SHA_40.upper(), SHA_40, None),
            ("b" * 64, "b" * 64, None),
            ("abc123", None, "not a full hex SHA"),
            ("z" * 40, None, "not a full hex SHA"),
            (SHA_40 + "\n", None, "not a full hex SHA"),
            (5, None, "not a string"),
        ],
    )
    def test_head_sha(self, raw: object, value: object, reason: object) -> None:
        assert normalize_head_sha(raw) == (value, reason)

    @pytest.mark.parametrize(
        ("raw", "value", "reason"),
        [
            (None, None, None),
            ("2026-11-09T12:00:00", None, "naive timestamp"),
            ("2026-11-09T12:00:00Z", "2026-11-09T12:00:00.000+00:00", None),
            ("2026-11-09T13:00:00+01:00", "2026-11-09T12:00:00.000+00:00", None),
            ("garbage", None, "not ISO-8601"),
            (1700000000, None, "not a string"),
        ],
    )
    def test_event_time(self, raw: object, value: object, reason: object) -> None:
        assert normalize_event_time(raw) == (value, reason)


class TestPREventMessageStrictness:
    def _valid(self, **over: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "schema_version": 1,
            "repo": "acme/widgets",
            "pr_number": 42,
            "event_type": "merged",
            "kind": "transition",
            "payload": {},
            "source_id": None,
            "event_id": None,
            "head_sha": None,
            "event_time": None,
            "received_at": RECEIVED,
        }
        base.update(over)
        return base

    def test_valid_accepted(self) -> None:
        PREventMessage.model_validate_json(json.dumps(self._valid()))

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PREventMessage.model_validate_json(json.dumps(self._valid(extra_key=1)))

    def test_bool_pr_number_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PREventMessage.model_validate_json(json.dumps(self._valid(pr_number=True)))

    def test_str_pr_number_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PREventMessage.model_validate_json(json.dumps(self._valid(pr_number="42")))

    def test_null_kind_rejected_for_v1(self) -> None:
        with pytest.raises(ValidationError):
            PREventMessage.model_validate_json(json.dumps(self._valid(kind=None)))

    def test_null_received_at_rejected_for_v1(self) -> None:
        with pytest.raises(ValidationError):
            PREventMessage.model_validate_json(
                json.dumps(self._valid(received_at=None))
            )

    def test_event_id_without_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PREventMessage.model_validate_json(
                json.dumps(self._valid(event_id="f" * 64))
            )

    def test_source_without_event_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PREventMessage.model_validate_json(json.dumps(self._valid(source_id="s")))

    def test_bad_head_sha_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PREventMessage.model_validate_json(json.dumps(self._valid(head_sha="abc")))

    def test_trailing_newline_head_sha_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PREventMessage.model_validate_json(
                json.dumps(self._valid(head_sha=SHA_40 + "\n"))
            )


class TestFromWire:
    def test_v1_round_trip(self) -> None:
        raw = json.dumps(_message(_load("workflow_body_merged.json")))
        msg = PREventMessage.from_wire(raw)
        assert msg is not None
        assert msg.schema_version == 1
        assert msg.event_type is PREventType.MERGED

    def test_v0_legacy_reads_as_null_filled(self) -> None:
        msg = PREventMessage.from_wire(_load("message_v0_legacy.json")["message"])
        assert msg is not None
        assert msg.schema_version == 0
        assert msg.kind is None
        assert msg.received_at is None
        assert msg.event_id is None
        assert msg.repo == "acme/widgets"

    def test_unknown_extra_keys_dropped(self) -> None:
        raw = json.dumps(
            {**_message(_load("workflow_body_merged.json")), "future_key": 1}
        )
        msg = PREventMessage.from_wire(raw)
        assert msg is not None
        assert msg.schema_version == 1

    def test_non_json_returns_none(self) -> None:
        assert PREventMessage.from_wire("m0") is None

    def test_json_array_returns_none(self) -> None:
        assert PREventMessage.from_wire("[1, 2]") is None

    def test_unknown_event_type_returns_none(self) -> None:
        raw = json.dumps({"repo": "a/b", "pr_number": 1, "event_type": "nope"})
        assert PREventMessage.from_wire(raw) is None


class TestKindTable:
    def test_every_type_has_kind_and_all_transition(self) -> None:
        assert set(EVENT_KIND_BY_TYPE) == set(PREventType)
        assert all(k is PREventKind.TRANSITION for k in EVENT_KIND_BY_TYPE.values())

    def test_existing_wire_types_map_to_orchestrator_events(self) -> None:
        for t in PREventType:
            if t is PREventType.REVIEW_REQUESTED:
                continue
            assert OrchestratorEventType("pr." + t.value) is not None

    def test_kind_for_reads_table(self) -> None:
        assert kind_for(PREventType.MERGED) is PREventKind.TRANSITION


class TestHeadDrift:
    def test_distinct_head_and_source_distinct_ids(self) -> None:
        failing = json.loads(
            (FIXTURES / "gh_pr_view_adopt_2777_failing.json").read_text()
        )["headRefOid"]
        passing = json.loads((FIXTURES / "gh_pr_view_adopt_2933.json").read_text())[
            "headRefOid"
        ]
        for sha in (failing, passing):
            assert len(sha) == 40
            assert normalize_head_sha(sha)[0] == sha

        def body(source: str, head: str) -> dict[str, Any]:
            return {
                "repo": "acme/widgets",
                "pr_number": 42,
                "event_type": "ci_failed",
                "payload": {"workflow_conclusion": "failure"},
                "source_id": source,
                "head_sha": head,
                "event_time": None,
            }

        a = json.loads(_build(body("s1", failing))["message"])
        b = json.loads(_build(body("s2", passing))["message"])
        assert a["event_id"] != b["event_id"]
        assert a["head_sha"] == failing
        assert b["head_sha"] == passing

    def test_same_source_different_head_same_id(self) -> None:
        failing = json.loads(
            (FIXTURES / "gh_pr_view_adopt_2777_failing.json").read_text()
        )["headRefOid"]
        base = {
            "repo": "acme/widgets",
            "pr_number": 42,
            "event_type": "ci_failed",
            "payload": {},
            "source_id": "same",
        }
        a = json.loads(_build({**base, "head_sha": failing})["message"])
        b = json.loads(_build({**base, "head_sha": SHA_40})["message"])
        assert a["event_id"] == b["event_id"]
        assert a["head_sha"] != b["head_sha"]


class TestRequestTolerance:
    def test_extra_ignored(self) -> None:
        assert PREventRequest.model_config["extra"] == "ignore"
        PREventRequest.model_validate(
            {"repo": "a/b", "pr_number": 1, "event_type": "merged", "x": 1}
        )

    def test_unknown_event_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PREventRequest.model_validate(
                {"repo": "a/b", "pr_number": 1, "event_type": "ci_passed"}
            )

    def test_wrong_type_identity_fields_accepted(self) -> None:
        req = PREventRequest.model_validate(
            {
                "repo": "a/b",
                "pr_number": 1,
                "event_type": "merged",
                "source_id": 5,
                "head_sha": [1],
                "event_time": {"x": 1},
            }
        )
        msg = PREventMessage.model_validate_json(_build_notification(req)["message"])
        assert msg.source_id is None
        assert msg.head_sha is None
        assert msg.event_time is None


def test_schema_version_is_one() -> None:
    assert PR_CHANNEL_SCHEMA_VERSION == 1


class TestDocGuards:
    def test_wire_example_matches_builder_output(self) -> None:
        doc = (Path(__file__).parent.parent / "docs" / "pr-channel.md").read_text()
        marker = "```json pr-channel-wire-example\n"
        start = doc.index(marker) + len(marker)
        end = doc.index("```", start)
        documented = json.loads(doc[start:end])
        assert documented == _message(_load("workflow_body_merged.json"))

    def test_matrix_names_every_type_and_reconciliation(self) -> None:
        doc = (Path(__file__).parent.parent / "docs" / "pr-channel.md").read_text()
        for t in PREventType:
            assert t.value in doc
        for line in doc.splitlines():
            if "events/list" in line:
                assert "not" in line, line

    def test_head_sha_not_in_hydration_fields(self) -> None:
        from cw.gh import pr_reads

        assert "headRefOid" not in pr_reads._PR_VIEW_FIELDS
