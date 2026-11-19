"""Stage-entry snapshot refresh and the current-row fallback (RFC 0019 T2, #3712)."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, get_args

import pytest

from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.ticket_snapshot import (
    REFRESH_REASONS,
    RefreshError,
    RefreshReason,
    SnapshotRefusedError,
    SnapshotUntrustedError,
    TicketSnapshot,
    read,
    read_current,
    read_for_stage,
    record_snapshot,
    refresh_snapshot,
    snapshot_for_stage,
    snapshot_path,
)
from cw.ticket_snapshot import _models as models
from cw.ticket_snapshot import _refresh as refresh_mod
from cw.ticket_snapshot._record import current_row
from cw.ticket_snapshot._refresh import emit_degraded, is_github
from tests._shared.outbox_builders import client_config
from tests._shared.ticket_context import (
    make_snapshot,
    observed_issue_envelope,
    seeded_row,
)
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    events_of,
    make_task,
    queue_row,
    seed_queue,
)
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask

STAGE = "review"
DEGRADED = OrchestratorEventType.TICKET_SNAPSHOT_DEGRADED
RECORDED = OrchestratorEventType.TICKET_SNAPSHOT_RECORDED
PAYLOAD_KEYS = {"ticket_id", "client", "kind", "reason", "snapshot_digest", "stage"}


def _client(tmp_path: Path, tracker: str | None = "github-issues") -> ClientConfig:
    return client_config(tmp_path / "ws", tracker=tracker)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    return path


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
    """A ``gh`` that serves the observed issue envelope for ``issue view``."""
    fake = FakeGhCli()
    fake.reply("issue", "view", stdout=json.dumps(_envelope()))
    return fake.install(monkeypatch)


def _envelope(**overrides: Any) -> dict[str, Any]:
    envelope = observed_issue_envelope()
    envelope["title"] = "Refreshed title"
    envelope.update(overrides)
    return envelope


def _row_snapshot(title: str = "Stored title") -> TicketSnapshot:
    return make_snapshot(title=title)


def _degraded() -> list[object]:
    return list(events_of(DEGRADED))


# --- is_github ---


def test_is_github_is_the_exact_tracker_equality(tmp_path: Path) -> None:
    assert is_github(_client(tmp_path / "a", "github-issues")) is True
    assert is_github(_client(tmp_path / "b", "linear")) is False
    assert is_github(_client(tmp_path / "c", None)) is False


# --- refresh_snapshot: the GitHub branch ---


def test_refresh_github_returns_snapshot_stamps_row_and_rewrites_context(
    tmp_path: Path, worktree: Path, gh: FakeGhCli
) -> None:
    task = seeded_row(_row_snapshot())
    previous = task.snapshot_digest
    (worktree / ".cw").mkdir()
    (worktree / ".cw" / "context.json").write_text("{}", encoding="utf-8")

    snapshot = refresh_snapshot(
        task, _client(tmp_path), worktree, stage=STAGE, session_id="sess-9"
    )

    assert snapshot.title == "Refreshed title"
    assert snapshot.ticket_id == ACME_TICKET
    assert snapshot.client == ACME
    row = queue_row()
    assert row.snapshot_digest not in (None, previous)
    assert read(row) == snapshot
    context = json.loads((worktree / ".cw" / "context.json").read_text("utf-8"))
    assert context["ticket_title"] == "Refreshed title"
    assert context["snapshot_digest"] == row.snapshot_digest
    assert context["materialized_by_session"] == "sess-9"
    (event,) = events_of(RECORDED)
    assert event.payload["refresh"] is True
    assert event.payload["previous_digest"] == previous
    assert [r.cwd for r in gh.records] == [worktree]
    assert gh.records[0].argv[:4] == ["gh", "issue", "view", ACME_TICKET]


def test_refresh_without_a_session_leaves_materialized_by_session_null(
    tmp_path: Path, worktree: Path, gh: FakeGhCli
) -> None:
    task = seeded_row(_row_snapshot())
    refresh_snapshot(task, _client(tmp_path), worktree, stage=STAGE)
    context = json.loads((worktree / ".cw" / "context.json").read_text("utf-8"))
    assert context["materialized_by_session"] is None
    assert gh.records


@pytest.mark.parametrize("tracker", ["linear", None])
def test_refresh_non_github_is_no_binding_before_any_gh_launch(
    tmp_path: Path, worktree: Path, gh: FakeGhCli, tracker: str | None
) -> None:
    task = seeded_row(_row_snapshot())
    with pytest.raises(RefreshError) as info:
        refresh_snapshot(task, _client(tmp_path, tracker), worktree, stage=STAGE)
    assert info.value.reason == "no_binding"
    assert gh.records == []
    assert events_of(RECORDED) == []
    assert not (worktree / ".cw" / "context.json").exists()


def test_refresh_gh_failure_is_gh_failed_and_changes_nothing(
    tmp_path: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeGhCli().reply("issue", "view", returncode=1, stderr="boom").install(monkeypatch)
    task = seeded_row(_row_snapshot())
    with pytest.raises(RefreshError) as info:
        refresh_snapshot(task, _client(tmp_path), worktree, stage=STAGE)
    assert info.value.reason == "gh_failed"
    assert isinstance(info.value.__cause__, SnapshotRefusedError)
    assert queue_row().snapshot_digest == task.snapshot_digest
    assert not (worktree / ".cw" / "context.json").exists()


def _many_comments_envelope() -> dict[str, Any]:
    envelope = observed_issue_envelope()
    template: dict[str, object] = envelope["comments"][0]
    envelope["comments"] = [
        {**template, "id": f"IC_{n}"} for n in range(models.MAX_COMMENTS + 1)
    ]
    return envelope


def test_refresh_over_the_comment_count_cap_is_cap(
    tmp_path: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeGhCli().reply(
        "issue", "view", stdout=json.dumps(_many_comments_envelope())
    ).install(monkeypatch)
    task = seeded_row(_row_snapshot())
    with pytest.raises(RefreshError) as info:
        refresh_snapshot(task, _client(tmp_path), worktree, stage=STAGE)
    assert info.value.reason == "cap"
    assert isinstance(info.value.__cause__, SnapshotRefusedError)
    assert queue_row().snapshot_digest == task.snapshot_digest


def test_refresh_over_the_total_size_cap_is_cap(
    tmp_path: Path, worktree: Path, gh: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = seeded_row(_row_snapshot())
    monkeypatch.setattr(models, "MAX_SNAPSHOT_BYTES", 10)
    with pytest.raises(RefreshError) as info:
        refresh_snapshot(task, _client(tmp_path), worktree, stage=STAGE)
    assert info.value.reason == "cap"
    assert gh.records


@pytest.mark.parametrize(
    "error", [OSError("disk gone"), CwError("lock trouble")], ids=["oserror", "cwerror"]
)
def test_refresh_record_step_failure_is_refused_without_text(
    tmp_path: Path,
    worktree: Path,
    gh: FakeGhCli,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise error

    monkeypatch.setattr(refresh_mod, "record_snapshot", _boom)
    task = seeded_row(_row_snapshot())
    with pytest.raises(RefreshError) as info:
        refresh_snapshot(task, _client(tmp_path), worktree, stage=STAGE)
    assert info.value.reason == "refused"
    assert type(error).__name__ in str(info.value)
    assert str(error) not in str(info.value)


def test_refresh_context_write_failure_is_refused(
    tmp_path: Path, worktree: Path, gh: FakeGhCli
) -> None:
    (worktree / ".cw").write_text("a regular file, not a directory", encoding="utf-8")
    task = seeded_row(_row_snapshot())
    with pytest.raises(RefreshError) as info:
        refresh_snapshot(task, _client(tmp_path), worktree, stage=STAGE)
    assert info.value.reason == "refused"


def test_refresh_reasons_are_the_closed_reserved_set() -> None:
    assert REFRESH_REASONS == (
        "gh_failed",
        "cap",
        "no_binding",
        "refused",
        "turn_failed",
        "courier_unreachable",
    )


def test_refresh_error_carries_reason_and_is_a_cw_error() -> None:
    error = RefreshError("message", reason="cap")
    assert error.reason == "cap"
    assert isinstance(error, CwError)


# --- snapshot_for_stage: success and the fall-back ---


def test_snapshot_for_stage_success_returns_the_fresh_snapshot_and_no_event(
    tmp_path: Path, worktree: Path, gh: FakeGhCli
) -> None:
    task = seeded_row(_row_snapshot())
    snapshot = snapshot_for_stage(task, _client(tmp_path), worktree, stage=STAGE)
    assert snapshot is not None
    assert snapshot.title == "Refreshed title"
    assert _degraded() == []


@pytest.mark.parametrize("reason", get_args(RefreshReason))
def test_failed_refresh_with_a_row_snapshot_returns_it_and_emits_one_event(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: RefreshReason,
) -> None:
    def _fail(*_a: object, **_k: object) -> TicketSnapshot:
        msg = "simulated"
        raise RefreshError(msg, reason=reason)

    monkeypatch.setattr(refresh_mod, "refresh_snapshot", _fail)
    stored = _row_snapshot()
    task = seeded_row(stored)
    result = snapshot_for_stage(task, _client(tmp_path), worktree, stage="impl")
    assert result == stored
    (event,) = events_of(DEGRADED)
    assert event.payload == {
        "ticket_id": ACME_TICKET,
        "client": ACME,
        "kind": "refresh_failed",
        "reason": reason,
        "snapshot_digest": task.snapshot_digest,
        "stage": "impl",
    }
    assert event.correlation_id == ACME_TICKET
    assert "Stored title" not in repr(event.payload)


def test_gh_failure_end_to_end_returns_last_snapshot(
    tmp_path: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeGhCli().reply("issue", "view", returncode=1).install(monkeypatch)
    stored = _row_snapshot()
    task = seeded_row(stored)
    assert snapshot_for_stage(task, _client(tmp_path), worktree, stage=STAGE) == stored
    (event,) = events_of(DEGRADED)
    assert event.payload["kind"] == "refresh_failed"
    assert event.payload["reason"] == "gh_failed"


def test_cap_failure_end_to_end_returns_last_snapshot(
    tmp_path: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeGhCli().reply(
        "issue", "view", stdout=json.dumps(_many_comments_envelope())
    ).install(monkeypatch)
    stored = _row_snapshot()
    task = seeded_row(stored)
    assert snapshot_for_stage(task, _client(tmp_path), worktree, stage=STAGE) == stored
    (event,) = events_of(DEGRADED)
    assert event.payload["reason"] == "cap"


def test_no_binding_runs_on_the_last_snapshot(tmp_path: Path, worktree: Path) -> None:
    stored = _row_snapshot()
    task = seeded_row(stored)
    result = snapshot_for_stage(
        task, _client(tmp_path, "linear"), worktree, stage=STAGE
    )
    assert result == stored
    (event,) = events_of(DEGRADED)
    assert event.payload["reason"] == "no_binding"


def test_fallback_uses_the_current_row_digest_not_the_stale_task(
    tmp_path: Path, worktree: Path
) -> None:
    stale = seeded_row(_row_snapshot("Old"))
    newer = _row_snapshot("Newer")
    record = record_snapshot(newer)
    # The queue row advanced after `stale` was loaded; the stale object still
    # points at the old, perfectly readable digest.
    assert queue_row().snapshot_digest == record.snapshot_digest
    assert stale.snapshot_digest != record.snapshot_digest
    assert read(stale) is not None
    assert read(stale) != newer

    result = snapshot_for_stage(
        stale, _client(tmp_path, "linear"), worktree, stage=STAGE
    )

    assert result == newer
    (event,) = events_of(DEGRADED)
    assert event.payload["snapshot_digest"] == record.snapshot_digest


def test_fallback_is_none_when_no_row_matches(tmp_path: Path, worktree: Path) -> None:
    stored = _row_snapshot()
    task = seeded_row(stored, seed=False)
    result = snapshot_for_stage(
        task, _client(tmp_path, "linear"), worktree, stage=STAGE
    )
    assert result is None
    assert len(events_of(DEGRADED)) == 1


# --- snapshot_for_stage: no usable snapshot ---


def test_failed_refresh_on_a_row_with_no_digest_is_none_with_one_event(
    tmp_path: Path, worktree: Path
) -> None:
    seed_queue([make_task()])
    result = snapshot_for_stage(
        make_task(), _client(tmp_path, "linear"), worktree, stage=STAGE
    )
    assert result is None
    (event,) = events_of(DEGRADED)
    assert event.payload == {
        "ticket_id": ACME_TICKET,
        "client": ACME,
        "kind": "read_untrusted",
        "reason": "missing",
        "snapshot_digest": None,
        "stage": STAGE,
    }


def _tamper(task: TicketTask) -> None:
    path = snapshot_path(ACME, task.snapshot_digest or "")
    path.write_text(path.read_text("ascii") + " ", encoding="ascii")


def _store_raw(raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    path = snapshot_path(ACME, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return digest


def _untrusted_row(case: str) -> TicketTask:
    if case == "missing":
        task = make_task(snapshot_digest="c" * 64)
    elif case == "mismatch":
        task = seeded_row(_row_snapshot())
        _tamper(task)
    elif case == "invalid":
        task = make_task(snapshot_digest=_store_raw(b"{not json"))
    else:
        task = make_task(snapshot_digest=_store_raw(b'{"schema_version": 99}'))
    seed_queue([task])
    return task


@pytest.mark.parametrize("case", ["missing", "mismatch", "invalid", "too_new"])
def test_double_failure_records_exactly_one_event_and_returns_none(
    tmp_path: Path, worktree: Path, case: str
) -> None:
    task = _untrusted_row(case)
    result = snapshot_for_stage(
        task, _client(tmp_path, "linear"), worktree, stage=STAGE
    )
    assert result is None
    (event,) = events_of(DEGRADED)
    assert set(event.payload) == PAYLOAD_KEYS
    assert event.payload["kind"] == "read_untrusted"
    assert event.payload["reason"] == case
    assert event.payload["snapshot_digest"] == task.snapshot_digest
    assert event.payload["stage"] == STAGE
    assert event.payload["ticket_id"] == ACME_TICKET
    assert event.payload["client"] == ACME


def test_malformed_row_digest_is_reported_as_null_in_the_event(
    tmp_path: Path, worktree: Path
) -> None:
    task = make_task(snapshot_digest="XYZ")
    seed_queue([task])
    assert (
        snapshot_for_stage(task, _client(tmp_path, "linear"), worktree, stage=STAGE)
        is None
    )
    (event,) = events_of(DEGRADED)
    assert event.payload["reason"] == "invalid"
    assert event.payload["snapshot_digest"] is None


def test_unexpected_oserror_on_the_fallback_read_is_read_untrusted_invalid(
    tmp_path: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_task: object) -> None:
        msg = "unreadable"
        raise PermissionError(msg)

    task = seeded_row(_row_snapshot())
    monkeypatch.setattr(refresh_mod, "read", _boom)
    result = snapshot_for_stage(
        task, _client(tmp_path, "linear"), worktree, stage=STAGE
    )
    assert result is None
    (event,) = events_of(DEGRADED)
    assert event.payload["kind"] == "read_untrusted"
    assert event.payload["reason"] == "invalid"


def test_snapshot_for_stage_never_raises_when_the_event_cannot_be_recorded(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        msg = "disk gone"
        raise OSError(msg)

    monkeypatch.setattr(refresh_mod, "record_event", _boom)
    stored = _row_snapshot()
    task = seeded_row(stored)
    with caplog.at_level(logging.WARNING, logger="cw.ticket_snapshot"):
        result = snapshot_for_stage(
            task, _client(tmp_path, "linear"), worktree, stage=STAGE
        )
    assert result == stored
    assert "ticket.snapshot_degraded event not recorded" in caplog.text
    assert "reason=no_binding" in caplog.text


def test_failure_log_carries_no_ticket_text(
    tmp_path: Path, worktree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    task = seeded_row(_row_snapshot("Secret title words"))
    with caplog.at_level(logging.WARNING, logger="cw.ticket_snapshot"):
        snapshot_for_stage(task, _client(tmp_path, "linear"), worktree, stage=STAGE)
    assert "Secret title words" not in caplog.text
    assert "Authored test ticket body" not in caplog.text
    assert "refresh failed" in caplog.text


# --- emit_degraded ---


@pytest.mark.parametrize("error", [OSError("x"), CwError("y")])
def test_emit_degraded_swallows_record_event_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise error

    monkeypatch.setattr(refresh_mod, "record_event", _boom)
    with caplog.at_level(logging.WARNING, logger="cw.ticket_snapshot"):
        emit_degraded(
            make_task(snapshot_digest="a" * 64),
            kind="refresh_failed",
            reason="cap",
            stage=STAGE,
        )
    assert "event not recorded" in caplog.text


def test_emit_degraded_payload_and_correlation() -> None:
    emit_degraded(
        make_task(snapshot_digest="a" * 64),
        kind="refresh_failed",
        reason="cap",
        stage="finalize",
    )
    (event,) = events_of(DEGRADED)
    assert event.payload == {
        "ticket_id": ACME_TICKET,
        "client": ACME,
        "kind": "refresh_failed",
        "reason": "cap",
        "snapshot_digest": "a" * 64,
        "stage": "finalize",
    }
    assert event.correlation_id == ACME_TICKET


# --- read_for_stage / read_current ---


def test_read_for_stage_returns_the_row_snapshot_silently() -> None:
    stored = _row_snapshot()
    task = seeded_row(stored)
    assert read_for_stage(task, STAGE) == stored
    assert _degraded() == []


def test_read_for_stage_no_digest_is_none_and_silent() -> None:
    seed_queue([make_task()])
    assert read_for_stage(make_task(), STAGE) is None
    assert _degraded() == []


@pytest.mark.parametrize("case", ["missing", "mismatch", "invalid", "too_new"])
def test_read_for_stage_untrusted_is_none_with_one_event(case: str) -> None:
    task = _untrusted_row(case)
    assert read_for_stage(task, "finalize") is None
    (event,) = events_of(DEGRADED)
    assert event.payload["kind"] == "read_untrusted"
    assert event.payload["reason"] == case
    assert event.payload["stage"] == "finalize"


def test_read_for_stage_reads_the_current_row_not_the_stale_task() -> None:
    stale = seeded_row(_row_snapshot("Old"))
    newer = _row_snapshot("Newer")
    record_snapshot(newer)
    assert read_for_stage(stale, STAGE) == newer


def test_read_current_returns_the_current_rows_snapshot() -> None:
    stale = seeded_row(_row_snapshot("Old"))
    newer = _row_snapshot("Newer")
    record_snapshot(newer)
    assert read_current(stale) == newer


def test_read_current_no_digest_is_none() -> None:
    seed_queue([make_task()])
    assert read_current(make_task()) is None


def test_read_current_raises_untrusted_unlike_read_for_stage() -> None:
    task = _untrusted_row("missing")
    with pytest.raises(SnapshotUntrustedError) as info:
        read_current(task)
    assert info.value.reason == "missing"
    assert _degraded() == []


def test_read_current_is_none_without_a_row() -> None:
    stored = _row_snapshot()
    task = seeded_row(stored, seed=False)
    assert read_current(task) is None


@pytest.mark.parametrize(
    "exc",
    [OSError("queue file vanished"), ValueError("Expecting value: line 1")],
    ids=["oserror", "valueerror"],
)
def test_current_row_degrades_to_none_on_queue_read_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, exc: Exception
) -> None:
    def _raise() -> object:
        raise exc

    monkeypatch.setattr("cw.ticket_snapshot._record.load_dev_queue", _raise)
    with caplog.at_level(logging.WARNING, logger="cw.ticket_snapshot"):
        assert current_row("896", "test") is None
    assert type(exc).__name__ in caplog.text
    assert str(exc) not in caplog.text
