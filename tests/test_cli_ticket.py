"""``cw ticket snapshot`` CLI: path selection, exit codes, render (RFC 0019 T1)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import ticket as cli_ticket
from cw.cli._base import main
from cw.models import OrchestratorEventType
from cw.ticket_snapshot import SnapshotRecord, _fetch, read
from tests._shared.cli_preflight import _write_tracker_config
from tests._shared.ticket_context import observed_issue_envelope
from tests._ticket_view_helpers import (
    ACME,
    events_of,
    install_acme_client,
    make_task,
    queue_row,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

GH_TICKET = "7"
_ = main  # the root group must be imported so ``ticket`` resolves lazily


@pytest.fixture
def worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.chdir(wt)
    return wt


@pytest.fixture
def github_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    install_acme_client(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    _write_tracker_config(ws, "github-issues")
    return ws


@pytest.fixture
def linear_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    install_acme_client(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    _write_tracker_config(ws, "linear")
    return ws


def _invoke(*args: str, stdin: str | None = None) -> Any:
    return CliRunner().invoke(main, ["ticket", "snapshot", *args], input=stdin)


def _stdin_doc(author_id: str = "u1") -> str:
    return json.dumps(
        {
            "ticket_url": None,
            "title": "Linear ticket",
            "body": "body text",
            "comments": [
                {
                    "id": "c1",
                    "author_id": author_id,
                    "author": "someone",
                    "created_at": "2026-01-01T00:00:00Z",
                    "body": "a comment",
                    "minimized": False,
                }
            ],
        }
    )


def test_github_happy_path_json_row_updated_and_renders(
    github_client: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_queue([make_task(ticket_id=GH_TICKET)])
    monkeypatch.setattr(
        _fetch, "fetch_issue_snapshot", lambda *_a, **_k: observed_issue_envelope()
    )
    result = _invoke(GH_TICKET, "-c", ACME, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["row_updated"] is True
    assert payload["client"] == ACME
    assert queue_row(GH_TICKET).snapshot_digest == payload["snapshot_digest"]
    rendered = json.loads(
        (worktree / ".cw" / "context.json").read_text(encoding="utf-8")
    )
    assert rendered["ticket_id"] == GH_TICKET
    assert rendered["snapshot_digest"] == payload["snapshot_digest"]


def test_github_renders_session_from_cw_context(
    github_client: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (worktree / ".claude").mkdir()
    (worktree / ".claude" / "cw-context.json").write_text(
        json.dumps({"client": ACME, "session_id": "sess-42"}), encoding="utf-8"
    )
    seed_queue([make_task(ticket_id=GH_TICKET)])
    monkeypatch.setattr(
        _fetch, "fetch_issue_snapshot", lambda *_a, **_k: observed_issue_envelope()
    )
    result = _invoke(GH_TICKET, "--json")
    assert result.exit_code == 0, result.output
    rendered = json.loads(
        (worktree / ".cw" / "context.json").read_text(encoding="utf-8")
    )
    assert rendered["materialized_by_session"] == "sess-42"


def test_no_row_exit_0_row_updated_false(
    github_client: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _fetch, "fetch_issue_snapshot", lambda *_a, **_k: observed_issue_envelope()
    )
    result = _invoke(GH_TICKET, "-c", ACME, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["row_updated"] is False


def test_github_refuses_from_json_exit_1(github_client: Path, worktree: Path) -> None:
    result = _invoke(GH_TICKET, "-c", ACME, "--from-json", "-", stdin=_stdin_doc())
    assert result.exit_code == 1
    assert "--from-json is refused" in result.output


def test_github_ticket_must_be_issue_number_exit_2(
    github_client: Path, worktree: Path
) -> None:
    result = _invoke("https://github.com/x/y/issues/1", "-c", ACME)
    assert result.exit_code == 2


def test_hash_prefixed_issue_number_accepted(
    github_client: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _fetch, "fetch_issue_snapshot", lambda *_a, **_k: observed_issue_envelope()
    )
    result = _invoke("#7", "-c", ACME, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ticket_id"] == "7"


def test_fetch_failure_exit_1(
    github_client: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_fetch, "fetch_issue_snapshot", lambda *_a, **_k: None)
    result = _invoke(GH_TICKET, "-c", ACME)
    assert result.exit_code == 1


def test_non_github_without_from_json_exit_1(
    linear_client: Path, worktree: Path
) -> None:
    result = _invoke(GH_TICKET, "-c", ACME)
    assert result.exit_code == 1


def test_non_github_from_json_stdin_happy(linear_client: Path, worktree: Path) -> None:
    seed_queue([make_task(ticket_id=GH_TICKET)])
    result = _invoke(
        GH_TICKET, "-c", ACME, "--from-json", "-", "--json", stdin=_stdin_doc()
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["comments"] == 1
    assert queue_row(GH_TICKET).snapshot_digest == payload["snapshot_digest"]


def test_stdin_refusal_exit_1(linear_client: Path, worktree: Path) -> None:
    result = _invoke(GH_TICKET, "-c", ACME, "--from-json", "-", stdin="{bad")
    assert result.exit_code == 1


def test_from_json_other_than_dash_is_usage_error_exit_2(
    linear_client: Path, worktree: Path
) -> None:
    result = _invoke(GH_TICKET, "-c", ACME, "--from-json", "file.json")
    assert result.exit_code == 2


def test_missing_client_exit_2(worktree: Path) -> None:
    result = _invoke(GH_TICKET)
    assert result.exit_code == 2


def test_unknown_client_exit_2(github_client: Path, worktree: Path) -> None:
    result = _invoke(GH_TICKET, "-c", "no-such-client")
    assert result.exit_code == 2


def test_client_falls_back_to_cw_context(
    github_client: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (worktree / ".claude").mkdir()
    (worktree / ".claude" / "cw-context.json").write_text(
        json.dumps({"client": ACME}), encoding="utf-8"
    )
    monkeypatch.setattr(
        _fetch, "fetch_issue_snapshot", lambda *_a, **_k: observed_issue_envelope()
    )
    result = _invoke(GH_TICKET, "--json")
    assert result.exit_code == 0, result.output


# --- RFC 0019 T2 (#3712): author-conflict pass-through ---


@pytest.fixture
def record_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Spy on ``cw.cli.ticket.record_snapshot``; delegates to the real one."""
    calls: list[dict[str, Any]] = []
    real = cli_ticket.record_snapshot

    def _spy(snapshot: Any, **kwargs: Any) -> SnapshotRecord:
        calls.append(kwargs)
        return real(snapshot, **kwargs)

    monkeypatch.setattr(cli_ticket, "record_snapshot", _spy)
    return calls


def test_from_json_passes_detect_author_conflicts_and_flags_a_contradiction(
    linear_client: Path, worktree: Path, record_calls: list[dict[str, Any]]
) -> None:
    seed_queue([make_task(ticket_id=GH_TICKET)])
    first = _invoke(GH_TICKET, "-c", ACME, "--from-json", "-", stdin=_stdin_doc("u1"))
    assert first.exit_code == 0, first.output
    assert events_of(OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT) == []

    second = _invoke(
        GH_TICKET, "-c", ACME, "--from-json", "-", "--json", stdin=_stdin_doc("u2")
    )

    assert second.exit_code == 0, second.output
    assert [call["detect_author_conflicts"] for call in record_calls] == [True, True]
    stored = read(queue_row(GH_TICKET))
    assert stored is not None
    assert [c.author_conflict for c in stored.comments] == [True]
    (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT)
    assert event.payload["comments"] == 1
    assert (
        event.payload["snapshot_digest"] == json.loads(second.output)["snapshot_digest"]
    )
    assert event.correlation_id == GH_TICKET


def test_github_path_never_detects_or_flags(
    github_client: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_calls: list[dict[str, Any]],
) -> None:
    seed_queue([make_task(ticket_id=GH_TICKET)])
    logins = iter(["first-login", "second-login"])

    def _envelope(*_a: object, **_k: object) -> dict[str, Any]:
        envelope = observed_issue_envelope()
        envelope["comments"][0]["author"] = {"login": next(logins)}
        return envelope

    monkeypatch.setattr(_fetch, "fetch_issue_snapshot", _envelope)
    for _ in range(2):
        result = _invoke(GH_TICKET, "-c", ACME, "--json")
        assert result.exit_code == 0, result.output

    assert [call["detect_author_conflicts"] for call in record_calls] == [False, False]
    stored = read(queue_row(GH_TICKET))
    assert stored is not None
    assert [c.author_conflict for c in stored.comments] == [False] * len(
        stored.comments
    )
    assert events_of(OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT) == []
