"""Tests for ``cw.dev_queue.review_engine_allow`` (GitHub #3039, RFC 0017 I1).

``cw review engine allow`` records one operator allow, bound to an exact HEAD,
on a row parked ``review_engine_modified``. Modeled on the MUST_FIX override
tests: event first, then save; every refusal happens before the first write.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

import cw.config
from cw.cli import main
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dev_queue.review_engine_allow import (
    record_review_engine_allow,
    review_engine_head_allowed,
)
from cw.exceptions import CwError
from cw.models import (
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    ReviewEngineAllow,
    Stage,
)
from tests._cli_review_helpers import _enter_operator_machine, _write_session_context
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

_CLIENT = "acme"
_TICKET = "T-1"
_HEAD = "a" * 40
_HEAD_2 = "b" * 40
_REASON = "inspected the skill diff; the change is the intended tweak"
_OPERATOR = "octocat"

CapturedEvent = tuple[OrchestratorEventType, dict[str, object], str | None]


def _seed(
    tmp_path: Path,
    *,
    status: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER,
    blocked_reason: str | None = "review_engine_modified",
    allows: list[ReviewEngineAllow] | None = None,
) -> None:
    write_clients_yaml(ClientSpec(_CLIENT, tmp_path / "ws"), ensure_workspaces=True)
    task = _make_ticket_task(
        ticket_id=_TICKET,
        client=_CLIENT,
        status=status,
        stage=Stage.REVIEW,
        blocked_reason=blocked_reason,
    )
    if allows is not None:
        task.review_engine_allows = allows
    save_dev_queue(DevQueueStore(tasks=[task]))


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[CapturedEvent]:
    """Capture ``record_event`` calls made by the allow module."""
    captured: list[CapturedEvent] = []

    def _capture(
        event_type: OrchestratorEventType,
        payload: dict[str, object],
        *,
        correlation_id: str | None = None,
    ) -> None:
        captured.append((event_type, dict(payload), correlation_id))

    monkeypatch.setattr("cw.dev_queue.review_engine_allow.record_event", _capture)
    return captured


@pytest.fixture(autouse=True)
def _operator_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: _OPERATOR)


def _row_allows() -> list[ReviewEngineAllow]:
    return load_dev_queue().tasks[0].review_engine_allows


@freeze_time("2026-11-08 12:00:00")
def test_allow_records_row_entry_pinned_to_head(
    tmp_path: Path, events: list[CapturedEvent]
) -> None:
    _seed(tmp_path)

    result = record_review_engine_allow(_TICKET, _CLIENT, _HEAD, f"  {_REASON}  ")

    assert result == {
        "ticket_id": _TICKET,
        "client": _CLIENT,
        "head_sha": _HEAD,
        "actor": _OPERATOR,
    }
    assert _row_allows() == [
        ReviewEngineAllow(
            head_sha=_HEAD,
            actor=_OPERATOR,
            reason=_REASON,
            recorded_at=datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
        )
    ]
    task = load_dev_queue().tasks[0]
    assert task.status == QueueItemStatus.BLOCKED_ON_USER
    assert task.blocked_reason == "review_engine_modified"


def test_allow_same_head_replaces_entry(tmp_path: Path) -> None:
    _seed(tmp_path)
    record_review_engine_allow(_TICKET, _CLIENT, _HEAD, "first reason")

    record_review_engine_allow(_TICKET, _CLIENT, _HEAD, "second reason")

    allows = _row_allows()
    assert len(allows) == 1
    assert allows[0].reason == "second reason"


def test_allow_second_head_appends(tmp_path: Path) -> None:
    _seed(tmp_path)
    record_review_engine_allow(_TICKET, _CLIENT, _HEAD, "first reason")

    record_review_engine_allow(_TICKET, _CLIENT, _HEAD_2, "second reason")

    assert [a.head_sha for a in _row_allows()] == [_HEAD, _HEAD_2]


@freeze_time("2026-11-08 12:00:00")
def test_allow_emits_review_engine_allowed_with_actor_reason_head(
    tmp_path: Path, events: list[CapturedEvent]
) -> None:
    _seed(tmp_path)

    record_review_engine_allow(_TICKET, _CLIENT, _HEAD, _REASON)

    assert events == [
        (
            OrchestratorEventType.REVIEW_ENGINE_ALLOWED,
            {
                "ticket_id": _TICKET,
                "client": _CLIENT,
                "head_sha": _HEAD,
                "reason": _REASON,
                "actor": _OPERATOR,
                "recorded_at": datetime(2026, 11, 8, 12, 0, tzinfo=UTC).isoformat(),
            },
            _TICKET,
        )
    ]


def test_event_failure_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "event inbox unwritable"
        raise OSError(msg)

    monkeypatch.setattr("cw.dev_queue.review_engine_allow.record_event", _boom)

    with pytest.raises(OSError, match="event inbox unwritable"):
        record_review_engine_allow(_TICKET, _CLIENT, _HEAD, _REASON)

    assert _row_allows() == []


def test_save_failure_after_event_logs_and_reraises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[CapturedEvent],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(tmp_path)

    def _boom(_store: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("cw.dev_queue.review_engine_allow.save_dev_queue", _boom)

    with caplog.at_level("ERROR"), pytest.raises(OSError, match="disk full"):
        record_review_engine_allow(_TICKET, _CLIENT, _HEAD, _REASON)

    assert len(events) == 1
    assert "queue save failed" in caplog.text
    assert _row_allows() == []


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_refuses_blank_reason(
    tmp_path: Path, events: list[CapturedEvent], reason: str
) -> None:
    _seed(tmp_path)

    with pytest.raises(CwError, match="non-blank --reason"):
        record_review_engine_allow(_TICKET, _CLIENT, _HEAD, reason)

    assert events == []
    assert _row_allows() == []


@pytest.mark.parametrize(
    "head",
    ["", "abc123", "A" * 40, "g" * 40, "a" * 39, "a" * 41],
    ids=["empty", "short", "uppercase", "non-hex", "39", "41"],
)
def test_refuses_non_40hex_head(
    tmp_path: Path, events: list[CapturedEvent], head: str
) -> None:
    _seed(tmp_path)

    with pytest.raises(CwError, match="40-hex"):
        record_review_engine_allow(_TICKET, _CLIENT, head, _REASON)

    assert events == []
    assert _row_allows() == []


def test_refuses_unresolved_operator_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[CapturedEvent]
) -> None:
    _seed(tmp_path)
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)

    with pytest.raises(CwError, match="operator login"):
        record_review_engine_allow(_TICKET, _CLIENT, _HEAD, _REASON)

    assert events == []
    assert _row_allows() == []


@pytest.mark.parametrize(
    ("status", "blocked_reason"),
    [
        (QueueItemStatus.PENDING, None),
        (QueueItemStatus.RUNNING, None),
        (QueueItemStatus.BLOCKED_ON_USER, "codex_must_fix_findings"),
        (QueueItemStatus.BLOCKED_ON_USER, None),
        (QueueItemStatus.PENDING, "review_engine_modified"),
    ],
    ids=["pending", "running", "other-reason", "no-reason", "wrong-status"],
)
def test_refuses_row_not_parked_for_review_engine_modified(
    tmp_path: Path,
    events: list[CapturedEvent],
    status: QueueItemStatus,
    blocked_reason: str | None,
) -> None:
    _seed(tmp_path, status=status, blocked_reason=blocked_reason)

    with pytest.raises(CwError, match="review_engine_modified"):
        record_review_engine_allow(_TICKET, _CLIENT, _HEAD, _REASON)

    assert events == []
    assert _row_allows() == []


def test_refuses_unknown_ticket(tmp_path: Path, events: list[CapturedEvent]) -> None:
    _seed(tmp_path)

    with pytest.raises(CwError):
        record_review_engine_allow("T-404", _CLIENT, _HEAD, _REASON)

    assert events == []


def test_refuses_inside_dispatch_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI refuses a headless worker before touching the queue."""
    _seed(tmp_path)
    _write_session_context(tmp_path, headless=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TMPDIR", raising=False)

    result = CliRunner().invoke(
        main,
        [
            "review",
            "engine",
            "allow",
            "--ticket",
            _TICKET,
            "--head-sha",
            _HEAD,
            "--reason",
            _REASON,
            "-c",
            _CLIENT,
        ],
    )

    assert result.exit_code == 1
    assert "operator-only" in result.output
    assert _row_allows() == []


def test_cli_allow_prints_recorded_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    _enter_operator_machine(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        main,
        [
            "review",
            "engine",
            "allow",
            "--ticket",
            _TICKET,
            "--head-sha",
            _HEAD,
            "--reason",
            _REASON,
            "-c",
            _CLIENT,
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["head_sha"] == _HEAD
    assert [a.head_sha for a in _row_allows()] == [_HEAD]


def test_head_allowed_predicate_true_false_and_missing_row(tmp_path: Path) -> None:
    _seed(tmp_path)
    record_review_engine_allow(_TICKET, _CLIENT, _HEAD, _REASON)

    assert review_engine_head_allowed(_TICKET, _CLIENT, _HEAD) is True
    assert review_engine_head_allowed(_TICKET, _CLIENT, _HEAD_2) is False
    assert review_engine_head_allowed("T-404", _CLIENT, _HEAD) is False
    assert review_engine_head_allowed(_TICKET, "other-client", _HEAD) is False


def test_old_row_without_field_loads_with_empty_list(tmp_path: Path) -> None:
    _seed(tmp_path)
    queue_file = cw.config.dev_queue_file()
    raw = json.loads(queue_file.read_text(encoding="utf-8"))
    for task in raw["tasks"]:
        task.pop("review_engine_allows", None)
    queue_file.write_text(json.dumps(raw), encoding="utf-8")

    task = load_dev_queue().tasks[0]

    assert task.review_engine_allows == []
