"""Tests for ``cw.models.pr_ref`` and the ``_predicates`` ref parser (#3634)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cw._predicates import is_repo_slug, parse_repo_pr_ref
from cw.dev_queue import migrate_dev_queue
from cw.models import DevQueueStore, TicketTask
from cw.models.pr_ref import (
    BLOCKED_ON_PR_JSON_SCHEMA,
    PrRef,
    coerce_blocked_on,
    dump_blocked_on,
    pr_evidence_keys,
)
from cw.models.tasks import DEV_QUEUE_SCHEMA_VERSION

_REJECTED_TEXT = [
    "#12",
    "owner/repo#0",
    "owner/repo",
    "a/b/c#1",
    "o/r#07",
    "./r#1",
    "o/..#1",
    "o/r#",
    "o/r#-1",
    "",
]


def _task(**overrides: object) -> TicketTask:
    return TicketTask.model_validate(
        {"ticket_id": "ACM-1", "client": "acme", **overrides}
    )


class TestParse:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("owner/repo#12", PrRef(repo="owner/repo", number=12)),
            ("12", PrRef(number=12)),
            (12, PrRef(number=12)),
        ],
    )
    def test_accepts(self, value: object, expected: PrRef) -> None:
        assert PrRef.parse(value) == expected

    @pytest.mark.parametrize("value", [*_REJECTED_TEXT, "07", "0"])
    def test_rejects_text(self, value: str) -> None:
        assert PrRef.parse(value) is None

    @pytest.mark.parametrize("value", [True, 1.0, None, 0, -3, [1]])
    def test_rejects_non_text(self, value: object) -> None:
        assert PrRef.parse(value) is None

    def test_constructor_rejects_bad_number_and_repo(self) -> None:
        with pytest.raises(ValidationError):
            PrRef(number=0)
        with pytest.raises(ValidationError):
            PrRef.model_validate({"number": True})
        with pytest.raises(ValidationError):
            PrRef(number=1, repo="not a slug")

    def test_from_number(self) -> None:
        assert PrRef.from_number(5) == PrRef(number=5)
        assert PrRef.from_number(0) is None
        assert PrRef.from_number(True) is None
        assert PrRef.from_number(None) is None


class TestPredicates:
    def test_parse_repo_pr_ref_accepts(self) -> None:
        assert parse_repo_pr_ref("owner/repo#12") == ("owner/repo", 12)
        assert parse_repo_pr_ref("O.o/r_-x#3") == ("O.o/r_-x", 3)

    @pytest.mark.parametrize("value", [*_REJECTED_TEXT, "12"])
    def test_parse_repo_pr_ref_rejects(self, value: str) -> None:
        assert parse_repo_pr_ref(value) is None

    @pytest.mark.parametrize("value", [12, None, True, b"o/r#1"])
    def test_parse_repo_pr_ref_rejects_non_str(self, value: object) -> None:
        assert parse_repo_pr_ref(value) is None

    def test_is_repo_slug(self) -> None:
        assert is_repo_slug("foo/bar")
        assert not is_repo_slug("foo/..")
        assert not is_repo_slug(".")
        assert not is_repo_slug(3)


class TestRefHelpers:
    def test_label(self) -> None:
        assert PrRef(number=9).label() == "#9"
        assert PrRef(repo="owner/repo", number=9).label() == "owner/repo#9"

    def test_key_lowercases_repo(self) -> None:
        assert PrRef(repo="Foo/Bar", number=9).key() == ("foo/bar", 9)
        assert PrRef(number=9).key() == (None, 9)

    def test_normalized(self) -> None:
        ref = PrRef(repo="Foo/Bar", number=9)
        assert ref.normalized("foo/bar") == PrRef(number=9)
        assert ref.normalized("other/repo") is ref
        assert ref.normalized(None) is ref
        same = PrRef(number=9)
        assert same.normalized("foo/bar") is same

    def test_frozen_ref_is_not_a_bare_int(self) -> None:
        # The reason the typed asserts in the suite compare to PrRef, not ints.
        assert PrRef(number=9) != 9

    def test_pr_evidence_keys(self) -> None:
        assert pr_evidence_keys("O/R", 5, cross_repo=True) == {("o/r", 5)}
        assert pr_evidence_keys("O/R", 5, cross_repo=False) == {
            ("o/r", 5),
            (None, 5),
        }


class TestBlockedOnField:
    def test_legacy_int_loads_as_same_repo_ref(self) -> None:
        assert _task(blocked_on_pr=42).blocked_on_pr == PrRef(number=42)

    @pytest.mark.parametrize("value", [0, -3, True, False])
    def test_garbage_legacy_int_loads_as_none(self, value: object) -> None:
        assert _task(blocked_on_pr=value).blocked_on_pr is None

    def test_dict_and_ref_load(self) -> None:
        ref = PrRef(repo="o/r", number=5)
        assert _task(blocked_on_pr={"repo": "o/r", "number": 5}).blocked_on_pr == ref
        assert _task(blocked_on_pr=ref).blocked_on_pr == ref

    def test_coerce_passes_through_non_int(self) -> None:
        payload = {"number": 1}
        assert coerce_blocked_on(payload) is payload
        assert coerce_blocked_on(None) is None

    def test_dump_shapes(self) -> None:
        assert dump_blocked_on(None) is None
        assert dump_blocked_on(PrRef(number=5)) == 5
        assert dump_blocked_on(PrRef(repo="o/r", number=5)) == {
            "repo": "o/r",
            "number": 5,
        }

    @pytest.mark.parametrize(
        "ref", [PrRef(number=5), PrRef(repo="o/r", number=5), None]
    )
    def test_round_trip_is_identity(self, ref: PrRef | None) -> None:
        task = _task(blocked_on_pr=ref)
        again = TicketTask.model_validate_json(task.model_dump_json())
        assert again == task
        assert again.blocked_on_pr == ref

    def test_v63_store_with_legacy_int_is_byte_identical(self) -> None:
        row = _task(blocked_on_pr=42).model_dump(mode="json")
        row["blocked_on_pr"] = 42
        raw: dict[str, object] = {"schema_version": 63, "tasks": [row]}
        store = DevQueueStore.model_validate(migrate_dev_queue(raw))
        assert store.tasks[0].blocked_on_pr == PrRef(number=42)
        assert store.schema_version == DEV_QUEUE_SCHEMA_VERSION == 63
        dumped = json.loads(store.model_dump_json())
        assert dumped["tasks"][0]["blocked_on_pr"] == 42
        again = DevQueueStore.model_validate(dumped)
        assert again.model_dump_json() == store.model_dump_json()


def _object_arm() -> dict[str, object]:
    any_of = BLOCKED_ON_PR_JSON_SCHEMA["anyOf"]
    assert isinstance(any_of, list)
    arm = any_of[1]
    assert isinstance(arm, dict)
    return dict(arm)


def _properties(arm: dict[str, object]) -> set[str]:
    properties = arm["properties"]
    assert isinstance(properties, dict)
    return set(properties)


class TestJsonSchemaHint:
    """Hint-versus-model consistency (R10, R12)."""

    def test_object_arm_matches_pr_ref_fields(self) -> None:
        arm = _object_arm()
        assert _properties(arm) == set(PrRef.model_fields)
        required = [n for n, f in PrRef.model_fields.items() if f.is_required()]
        assert arm["required"] == required == ["number"]

    def test_written_back_shapes_are_the_advertised_arms(self) -> None:
        arm = _object_arm()
        same = _task(blocked_on_pr=5).model_dump(mode="json")["blocked_on_pr"]
        assert isinstance(same, int)
        cross = _task(blocked_on_pr={"repo": "o/r", "number": 5}).model_dump(
            mode="json"
        )["blocked_on_pr"]
        assert isinstance(cross, dict)
        assert set(cross) == _properties(arm)
        assert _task().model_dump(mode="json")["blocked_on_pr"] is None
